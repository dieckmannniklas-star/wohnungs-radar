#!/usr/bin/env python3
"""Wohnungs-Radar Zürich.

Sucht passende Mietwohnungen auf Flatfox, Homegate und ImmoScout24 und
beobachtet die Seiten der Wohnbaugenossenschaften. Ergebnisse landen in
docs/data.json (für die Webseite); neue Treffer werden per E-Mail gemeldet.

Filter und Quellen stehen in config.json. Zugangsdaten für den Mailversand
kommen aus Umgebungsvariablen (in GitHub als "Secrets" hinterlegt):
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_TO.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "docs" / "data.json"
TZ = ZoneInfo("Europe/Zurich")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 WohnungsRadar/1.0 (private Wohnungssuche)"
)
ROBOTS_AGENT = "WohnungsRadar"
TIMEOUT = 25

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "de-CH,de;q=0.9",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
})

_robots: dict[str, robotparser.RobotFileParser | None] = {}


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:120] or "x"


def to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:[.,]\d+)?", str(value).replace("'", "").replace("’", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def allowed(url: str) -> bool:
    """Respektiert robots.txt der jeweiligen Website."""
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = session.get(base + "/robots.txt", timeout=TIMEOUT)
            if r.status_code >= 400:
                _robots[base] = None  # keine robots.txt -> erlaubt
            else:
                rp.parse(r.text.splitlines())
                _robots[base] = rp
        except requests.RequestException:
            _robots[base] = None
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(ROBOTS_AGENT, url)


class Blocked(Exception):
    pass


def fetch(url: str, *, as_json: bool = False):
    if not allowed(url):
        raise Blocked("robots.txt erlaubt keinen automatischen Abruf")
    for attempt in range(2):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code in (403, 429):
                raise Blocked(f"Website blockiert (HTTP {r.status_code})")
            r.raise_for_status()
            return r.json() if as_json else r.text
        except requests.RequestException:
            if attempt == 1:
                raise
            time.sleep(3)


def make_listing(*, source, address, zipcode, rooms, rent, rent_type="brutto",
                 area=None, url="", landlord="", quartier="", move_in="",
                 temporary=False, ext_id="", description="") -> dict:
    zip_int = int(zipcode) if str(zipcode or "").isdigit() else None
    addr = (address or "").strip()
    key_base = f"{addr}-{zip_int or ''}-{rooms or ''}" if addr else f"{source}-{ext_id}"
    text = f"{landlord} {description}".lower()
    is_coop = bool(re.search(r"genossenschaft|anteilschein|gemeinn[uü]tzig|wogeno|kraftwerk1|\babz\b|\basig\b", text))
    return {
        "id": slug(key_base),
        "kind": "genossenschaft" if is_coop else "markt",
        "coop": landlord or "",
        "source": source,
        "address": addr,
        "zip": zip_int,
        "quartier": quartier,
        "rooms": to_float(rooms),
        "rent": to_float(rent),
        "rentType": rent_type,
        "area": to_float(area),
        "moveIn": move_in or "",
        "temporary": bool(temporary),
        "url": url,
        "note": "",
    }


def matches(listing: dict, flt: dict) -> bool:
    rooms, rent, zip_ = listing.get("rooms"), listing.get("rent"), listing.get("zip")
    if rooms is None or rooms < float(flt["min_rooms"]):
        return False
    if rent is None or rent > float(flt["max_rent"]):
        return False
    if zip_ is None or not (int(flt["zip_from"]) <= zip_ <= int(flt["zip_to"])):
        return False
    if flt.get("only_coops") and listing.get("kind") != "genossenschaft":
        return False
    if not flt.get("include_temporary", True) and listing.get("temporary"):
        return False
    return True


# --------------------------------------------------------------------------
# Quelle: Flatfox (offizielle öffentliche Schnittstelle)
# --------------------------------------------------------------------------

FLATFOX_SKIP: set[int] = set()  # pks, die bekanntermassen nicht passen


def source_flatfox(flt: dict, known_ids: dict) -> list[dict]:
    pins = fetch(
        "https://flatfox.ch/api/v1/pin/?east=8.63&west=8.44&north=47.44&south=47.32"
        f"&max_count=500&min_rooms={flt['min_rooms']}&max_price={int(flt['max_rent'])}"
        "&object_category=APARTMENT&offer_type=RENT",
        as_json=True,
    )
    results = []
    for pin in pins:
        pk = pin.get("pk")
        if pk is None or pk in FLATFOX_SKIP:
            continue
        cached = known_ids.get(f"flatfox:{pk}")
        if cached:  # Details schon bekannt -> nicht erneut abrufen
            results.append(cached)
            continue
        try:
            d = fetch(f"https://flatfox.ch/api/v1/public-listing/{pk}/", as_json=True)
        except Exception:
            continue
        if d.get("offer_type") != "RENT" or d.get("object_category") != "APARTMENT":
            continue
        rent = d.get("rent_gross") or d.get("price_display")
        listing = make_listing(
            source="Flatfox",
            address=d.get("street") or d.get("public_address") or "",
            zipcode=d.get("zipcode"),
            rooms=d.get("number_of_rooms"),
            rent=rent,
            rent_type="brutto" if d.get("rent_gross") else "netto",
            area=d.get("surface_living"),
            url="https://flatfox.ch" + d.get("url", ""),
            landlord=(d.get("agency") or {}).get("name") or "",
            quartier=d.get("city") or "",
            move_in=d.get("moving_date") or ("sofort" if d.get("moving_date_type") == "imm" else ""),
            temporary=bool(d.get("is_temporary")) or "befristet" in (d.get("description_title") or "").lower(),
            ext_id=str(pk),
            description=(d.get("description_title") or "") + " " + (d.get("description") or "")[:1500],
        )
        listing["extKey"] = f"flatfox:{pk}"
        if not matches(listing, flt):
            FLATFOX_SKIP.add(pk)
            continue
        results.append(listing)
        time.sleep(0.5)
    return results


# --------------------------------------------------------------------------
# Quellen: Homegate und ImmoScout24 (gleiche Plattform, Daten im Seiten-JSON)
# --------------------------------------------------------------------------

STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def parse_smg_page(html: str, source: str, base_url: str) -> list[dict]:
    """Liest die Inserate aus dem eingebetteten JSON einer Homegate-/ImmoScout-Seite."""
    m = STATE_RE.search(html)
    if not m:
        return []
    raw = m.group(1).replace(":undefined", ":null")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out, seen = [], set()
    for node in _walk(state):
        lst = node.get("listing") if isinstance(node.get("listing"), dict) else None
        if not lst or "characteristics" not in lst or "id" not in lst:
            continue
        lid = str(lst["id"])
        if lid in seen:
            continue
        seen.add(lid)
        addr = lst.get("address") or {}
        chars = lst.get("characteristics") or {}
        rent_info = ((lst.get("prices") or {}).get("rent") or {})
        gross, net = rent_info.get("gross"), rent_info.get("net")
        lister = node.get("lister") or lst.get("lister") or {}
        localized = lst.get("localization") or {}
        title, text = "", ""
        for loc in localized.values() if isinstance(localized, dict) else []:
            if isinstance(loc, dict):
                t = loc.get("text") or {}
                title = title or (t.get("title") or "")
                text = text or (t.get("description") or "")
        path = "/mieten/" + lid
        listing = make_listing(
            source=source,
            address=addr.get("street") or "",
            zipcode=addr.get("postalCode"),
            rooms=chars.get("numberOfRooms"),
            rent=gross if gross else net,
            rent_type="brutto" if gross else "netto",
            area=chars.get("livingSpace"),
            url=urljoin(base_url, path),
            landlord=(lister.get("name") if isinstance(lister, dict) else "") or "",
            quartier=addr.get("locality") or "",
            move_in=str((lst.get("availableFrom") or "")),
            temporary="befristet" in (title + " " + text).lower(),
            ext_id=lid,
            description=title + " " + text[:1500],
        )
        out.append(listing)
    return out


def source_smg(name: str, url_tpl: str, base_url: str, pages: int = 4) -> list[dict]:
    results = []
    for page in range(1, pages + 1):
        html = fetch(url_tpl.format(page=page))
        found = parse_smg_page(html, name, base_url)
        if not found:
            if page == 1:
                raise RuntimeError("keine Inserate lesbar (Seitenaufbau geändert oder Abruf gesperrt)")
            break
        results.extend(found)
        time.sleep(2)
    return results


# --------------------------------------------------------------------------
# Quelle: Genossenschafts-Websites (Änderungen erkennen)
# --------------------------------------------------------------------------

NONE_RE = re.compile(
    r"(keine|kein)\s+(freie[nr]?|verfügbare[nr]?)\s+(wohnung|objekt|mietobjekt)|"
    r"zur\s?zeit\s+(alles\s+)?vermietet|aktuell\s+keine|zurzeit\s+keine|momentan\s+keine",
    re.I,
)


def rooms_pattern(min_rooms: float) -> re.Pattern:
    vals = []
    r = min_rooms
    while r <= 8:
        whole = int(r)
        if r == whole:
            vals += [rf"{whole}(?:[.,]0)?"]
        else:
            vals += [rf"{whole}[.,]5", rf"{whole}\s?½", rf"{whole}\s?1/2"]
        r += 0.5
    return re.compile(r"(?<![\d.,])(" + "|".join(vals) + r")\s*-?\s*(zimmer|zi\b|zi\.|-zimmer)", re.I)


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    # Datumsangaben, Zähler u. ä. glätten, damit nicht jede Kleinigkeit als Änderung gilt
    return re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b", "", text)


def source_coops(coops: list, flt: dict, prev_state: dict) -> tuple[list[dict], dict, list[str]]:
    rx = rooms_pattern(float(flt["min_rooms"]))
    hits, state, failed = [], {}, []
    for name, url in coops:
        entry = {"name": name, "url": url, "status": "unbekannt", "hash": None,
                 "lastChange": prev_state.get(url, {}).get("lastChange"), "checked": now_iso()}
        try:
            text = page_text(fetch(url))
        except Blocked as e:
            entry["status"] = "gesperrt"
            entry["error"] = str(e)
            failed.append(name)
            state[url] = entry
            continue
        except Exception as e:  # noqa: BLE001
            entry["status"] = "Fehler"
            entry["error"] = type(e).__name__
            failed.append(name)
            state[url] = entry
            continue
        h = hashlib.sha256(text.encode()).hexdigest()
        old = prev_state.get(url, {})
        changed = old.get("hash") not in (None, h)
        if changed:
            entry["lastChange"] = now_iso()
        entry["hash"] = h
        size_hits = sorted({m.group(0) for m in rx.finditer(text)})
        says_none = bool(NONE_RE.search(text))
        entry["status"] = "keine freien Wohnungen" if says_none and not size_hits else (
            "mögliche Treffer" if size_hits else "keine passende Grösse erkannt")
        entry["sizes"] = size_hits[:5]
        state[url] = entry
        if size_hits:
            snippet = ""
            m = rx.search(text)
            if m:
                snippet = text[max(0, m.start() - 120): m.end() + 160]
            hits.append({
                "id": slug("coop-" + name + "-" + h[:10]),
                "kind": "genossenschaft",
                "coop": name,
                "source": "Website",
                "address": "",
                "zip": None,
                "quartier": "",
                "rooms": to_float(size_hits[0]),
                "rent": None,
                "rentType": "",
                "area": None,
                "moveIn": "",
                "temporary": False,
                "url": url,
                "note": "Seite erwähnt " + ", ".join(size_hits[:3]) + ". Bitte prüfen. Auszug: " + snippet,
                "isHint": True,
            })
        time.sleep(1)
    return hits, state, failed


# --------------------------------------------------------------------------
# E-Mail
# --------------------------------------------------------------------------

def fmt_listing(l: dict) -> str:
    parts = [
        f"{l.get('coop') or 'Vermieter unbekannt'} ({l.get('source')})",
        f"{l.get('address') or ''} {l.get('zip') or ''} {l.get('quartier') or ''}".strip(),
    ]
    facts = []
    if l.get("rooms"):
        facts.append(f"{l['rooms']:g} Zimmer")
    if l.get("rent"):
        facts.append(f"CHF {l['rent']:,.0f}".replace(",", "'") + (" netto" if l.get("rentType") == "netto" else ""))
    if l.get("area"):
        facts.append(f"{l['area']:g} m²")
    if l.get("moveIn"):
        facts.append(f"Bezug {l['moveIn']}")
    if l.get("temporary"):
        facts.append("befristet")
    parts.append(" · ".join(facts))
    if l.get("note"):
        parts.append(l["note"][:400])
    parts.append(l.get("url", ""))
    return "\n".join(p for p in parts if p)


def send_mail(subject: str, body: str) -> None:
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not host or not to:
        print("E-Mail nicht eingerichtet (SMTP_HOST/MAIL_TO fehlen) – überspringe Versand.")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_USER", to)
    msg["To"] = to
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "465"))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    print(f"E-Mail an {to} gesendet: {subject}")


# --------------------------------------------------------------------------
# Hauptablauf
# --------------------------------------------------------------------------

def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    flt, src = cfg["filter"], cfg["sources"]

    data = {"listings": [], "coops": {}, "runs": []}
    first_run = True
    if DATA_PATH.exists():
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        first_run = False
    stored = {l["id"]: l for l in data.get("listings", [])}
    if data.get("filter") == flt:  # bei geänderten Filtern alles neu prüfen
        FLATFOX_SKIP.update(data.get("flatfoxSkip", []))
    known_ext = {l["extKey"]: l for l in stored.values() if l.get("extKey")}

    found: list[dict] = []
    ok_sources, failed = [], []

    def run_source(name, fn):
        try:
            items = fn()
            found.extend(items)
            ok_sources.append(name)
            print(f"{name}: {len(items)} Inserate gelesen")
        except Exception as e:  # noqa: BLE001
            failed.append(f"{name}: {e}")
            print(f"{name}: FEHLER {e}", file=sys.stderr)

    if src.get("flatfox"):
        run_source("Flatfox", lambda: source_flatfox(flt, known_ext))
    if src.get("homegate"):
        run_source("Homegate", lambda: source_smg(
            "Homegate",
            "https://www.homegate.ch/mieten/wohnung/ort-zuerich/trefferliste?ac="
            f"{flt['min_rooms']}&ah={int(flt['max_rent'])}&o=dateCreated-desc&ep={{page}}",
            "https://www.homegate.ch"))
    if src.get("immoscout24"):
        run_source("ImmoScout24", lambda: source_smg(
            "ImmoScout24",
            "https://www.immoscout24.ch/de/wohnung/mieten/ort-zuerich?nrf="
            f"{flt['min_rooms']}&pt={int(flt['max_rent'])}&pn={{page}}",
            "https://www.immoscout24.ch"))

    coop_state = data.get("coops", {})
    if src.get("genossenschaften"):
        try:
            hints, coop_state, coop_failed = source_coops(cfg["coops"], flt, data.get("coops", {}))
            found.extend(hints)
            ok_sources.append("Genossenschaften")
            if coop_failed:
                failed.append("Genossenschaften nicht lesbar: " + ", ".join(coop_failed))
        except Exception as e:  # noqa: BLE001
            failed.append(f"Genossenschaften: {e}")

    # Treffer filtern (Hinweise von Genossenschafts-Seiten haben keine Miete/PLZ und gehen direkt durch)
    current: dict[str, dict] = {}
    for l in found:
        if l.get("isHint") or matches(l, flt):
            if l["id"] in current:  # dieselbe Wohnung auf mehreren Portalen
                cur = current[l["id"]]
                if l["source"] not in cur["source"]:
                    cur["source"] += ", " + l["source"]
                if cur["kind"] == "markt" and l["kind"] == "genossenschaft":
                    cur["kind"], cur["coop"] = "genossenschaft", l["coop"]
                continue
            current[l["id"]] = l

    new_items = []
    ts = now_iso()
    for lid, l in current.items():
        if lid in stored:
            old = stored[lid]
            old.update({k: v for k, v in l.items() if k not in ("firstSeen",)})
            old["status"] = "aktiv"
            old["lastSeen"] = ts
        else:
            l["firstSeen"] = ts
            l["lastSeen"] = ts
            l["status"] = "aktiv"
            stored[lid] = l
            new_items.append(l)

    # Nicht mehr gefundene Inserate als "weg" markieren (nur wenn ihre Quelle gelesen wurde)
    for lid, l in stored.items():
        if lid in current or l.get("status") == "weg":
            continue
        sources = {s.strip() for s in l.get("source", "").split(",")}
        relevant = {"Website": "Genossenschaften"}.get
        if all((relevant(s) or s) in ok_sources for s in sources):
            l["status"] = "weg"
            l["goneSince"] = ts

    # Alte "weg"-Einträge nach 30 Tagen entfernen
    cutoff = time.time() - 30 * 86400
    def keep(l):
        if l.get("status") != "weg" or not l.get("goneSince"):
            return True
        return datetime.fromisoformat(l["goneSince"]).timestamp() > cutoff
    listings = [l for l in stored.values() if keep(l)]

    runs = (data.get("runs") or [])[-29:]
    runs.append({"at": ts, "ok": ok_sources, "failed": failed, "new": len(new_items)})
    out = {
        "updated": ts,
        "filter": flt,
        "listings": sorted(listings, key=lambda x: x.get("firstSeen", ""), reverse=True),
        "coops": coop_state,
        "runs": runs,
        "flatfoxSkip": sorted(FLATFOX_SKIP)[-3000:],
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Gespeichert: {len(listings)} Einträge, davon {len(new_items)} neu.")

    # E-Mail
    mail_cfg = cfg.get("email", {})
    if mail_cfg.get("enabled") and new_items:
        to_send = [l for l in new_items if not mail_cfg.get("only_coops") or l["kind"] == "genossenschaft"]
        if first_run:
            coops_now = [l for l in new_items if l["kind"] == "genossenschaft"]
            body = (f"Der Wohnungs-Radar läuft. Er hat {len(new_items)} passende Inserate erfasst "
                    f"(davon {len(coops_now)} von Genossenschaften). Ab jetzt melde ich nur noch neue.\n\n")
            body += "\n\n".join(fmt_listing(l) for l in coops_now[:20])
            send_mail("Wohnungs-Radar eingerichtet", body)
        elif to_send:
            to_send.sort(key=lambda l: l["kind"] != "genossenschaft")
            n_coop = sum(l["kind"] == "genossenschaft" for l in to_send)
            subject = f"{len(to_send)} neue Wohnung(en) in Zürich" + (f", {n_coop} Genossenschaft" if n_coop else "")
            body = "\n\n---\n\n".join(fmt_listing(l) for l in to_send)
            pages = os.environ.get("PAGES_URL")
            if pages:
                body += f"\n\nÜbersicht: {pages}"
            send_mail(subject, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
