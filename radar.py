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


# Befristung erkennen: Formulierungen in Titel und Beschreibung
TEMP_V = 3  # erhöhen, wenn sich die Erkennung ändert -> bekannte Inserate werden neu geprüft
_NUM = r"(?:\d{1,2}|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|elf|zwölf)"
TEMP_PATTERNS = [
    r"befrist\w*",
    r"\bauf\s+zeit\b",
    r"zwischen\s*-?\s*miete\w*|zwischenvermiet\w*",
    r"unter\s*-?\s*miete\w*|untervermiet\w*|untermieter\w*",
    r"\btempor[äa]r\w*|\btemporary\b|\btemporaire\b|\bfixed[- ]term\b|\blimited\s+(?:period|time|duration)\b",
    rf"\b(?:für|for|während)\s+(?:ca\.\s*|rund\s+|circa\s+)?{_NUM}\s*(?:monate|months|wochen|weeks)\b",
    r"\b(?:max(?:imal)?\.?|höchstens)\s+\d{1,2}\s*(?:monate|jahre)\b",
    r"\b(?:mietdauer|mietzeit|mietende|vertragsdauer|vermietungsdauer)\b[^.\n]{0,40}?(?:\d{1,2}\s*monate|\bbis\b)",
    r"\b(?:vom|ab|von)\s+\d{1,2}\.\s*\d{1,2}\.(?:\s*\d{2,4})?\s*(?:bis|-|–)\s*\d{1,2}\.\s*\d{1,2}\.\s*\d{2,4}",
    r"\b(?:vermietet|verfügbar|frei|mietbar)\s+bis\s+(?:ende\s+)?(?:\d{1,2}\.\s*\d{1,2}\.|\w+\s+\d{4})",
    r"\bsabbatical\w*|\bausland(?:s)?aufenthalt\w*",
]
TEMP_RE = re.compile("|".join(f"(?:{p})" for p in TEMP_PATTERNS), re.I)
# «unbefristet», «nicht befristet», «keine Untermiete»
NEGATION_RE = re.compile(r"(?:un|\bnicht\s+|\bkeine?\s+|\bohne\s+|\bnot\s+|\bno\s+)$", re.I)
# «Untermiete nicht gestattet», «Kaution für 3 Monate»
FORBIDDEN_AFTER_RE = re.compile(
    r"^\W{0,3}\s*(?:ist\s+|sind\s+)?(?:(?:nicht|nur\s+mit|not)\s+(?:gestattet|erlaubt|möglich|zulässig|allowed)"
    r"|vorbehalten)",  # Standardklausel «Irrtümer und Zwischenvermietung vorbehalten»
    re.I)
DEPOSIT_BEFORE_RE = re.compile(r"(?:kaution|depot|sicherheit|garantie|mietzinsdepot|deposit)\w*\W+(?:\w+\W+){0,3}$", re.I)


def temp_reason(text: str) -> str:
    """Gibt einen kurzen Textausschnitt zurück, falls das Inserat befristet wirkt, sonst ''."""
    text = text or ""
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text)
    for m in TEMP_RE.finditer(text):
        if NEGATION_RE.search(text[max(0, m.start() - 8):m.start()]):
            continue
        if FORBIDDEN_AFTER_RE.search(text[m.end():m.end() + 40]):
            continue
        if DEPOSIT_BEFORE_RE.search(text[max(0, m.start() - 40):m.start()]):
            continue
        return "…" + text[max(0, m.start() - 50):m.end() + 50].strip() + "…"
    return ""


def listing_key(address: str, zipcode, rooms) -> str:
    r = to_float(rooms)
    return f"{address}-{zipcode or ''}-{f'{r:g}' if r is not None else ''}"


def make_listing(*, source, address, zipcode, rooms, rent, rent_type="brutto",
                 area=None, url="", landlord="", quartier="", move_in="",
                 temporary=False, ext_id="", description="", lat=None, lng=None) -> dict:
    zip_int = int(zipcode) if str(zipcode or "").isdigit() else None
    addr = (address or "").strip()
    key_base = listing_key(addr, zip_int, rooms) if addr else f"{source}-{ext_id}"
    text = f"{landlord} {description}".lower()
    reason = temp_reason(description)
    if temporary and not reason:
        reason = "laut Portal befristet"
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
        "temporary": bool(temporary or reason),
        "tempHint": reason,
        "tempV": TEMP_V,
        "url": url,
        "note": "",
        "lat": to_float(lat),
        "lng": to_float(lng),
    }


def matches(listing: dict, flt: dict) -> bool:
    rooms, rent, zip_ = listing.get("rooms"), listing.get("rent"), listing.get("zip")
    if rooms is None or rooms < float(flt["min_rooms"]):
        return False
    if rent is None or rent > float(flt["max_rent"]):
        return False
    if zip_ is None:
        # z. B. Ron Orp: Inserat ist der Stadt Zürich zugeordnet, nennt aber keine Adresse
        if not listing.get("zipOptional"):
            return False
    elif not (int(flt["zip_from"]) <= zip_ <= int(flt["zip_to"])):
        return False
    if flt.get("only_coops") and listing.get("kind") != "genossenschaft":
        return False
    if not flt.get("include_temporary", True) and listing.get("temporary"):
        return False
    return True


# --------------------------------------------------------------------------
# Koordinaten für die Karte (amtliches Gebäudeadressverzeichnis via geo.admin.ch)
# --------------------------------------------------------------------------

GEO_URL = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
GEOCACHE: dict[str, list | None] = {}


def _geo_query(text: str, origins: str):
    key = f"{origins}:{text}"
    if key in GEOCACHE:
        return GEOCACHE[key]
    result = None
    try:
        r = session.get(GEO_URL, params={"searchText": text, "type": "locations", "origins": origins,
                                         "limit": 1, "sr": 4326}, timeout=TIMEOUT)
        r.raise_for_status()
        hits = r.json().get("results") or []
        if hits:
            a = hits[0]["attrs"]
            m = re.search(r"\b(\d{4})\b", re.sub(r"<[^>]+>", "", a.get("label", "")))
            result = [round(a["lat"], 6), round(a["lon"], 6), int(m.group(1)) if m else None]
        time.sleep(0.2)
    except (requests.RequestException, KeyError, ValueError):
        return None  # nicht zwischenspeichern, beim nächsten Lauf erneut versuchen
    GEOCACHE[key] = result
    return result


def locate(listing: dict) -> None:
    """Ergänzt fehlende Koordinaten (und PLZ) anhand der Adresse; sonst Mittelpunkt der PLZ."""
    addr, zip_ = listing.get("address") or "", listing.get("zip")
    has_number = bool(re.search(r"\d", addr))
    if listing.get("lat") and listing.get("lng") and zip_:
        return
    if addr and has_number:
        hit = _geo_query(f"{addr} {zip_ or ''} Zürich".strip(), "address")
        if hit:
            if not (listing.get("lat") and listing.get("lng")):
                listing["lat"], listing["lng"] = hit[0], hit[1]
            if not zip_ and hit[2]:
                listing["zip"] = hit[2]
            return
    if listing.get("zip") and not (listing.get("lat") and listing.get("lng")):
        hit = _geo_query(str(listing["zip"]), "zipcode")
        if hit:
            listing["lat"], listing["lng"] = hit[0], hit[1]
            listing["approx"] = True


def clean_url(url: str) -> str:
    return url.split("?utm_")[0].split("&utm_")[0]


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
        if cached and cached.get("tempV") == TEMP_V:  # Details schon bekannt -> nicht erneut abrufen
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
            temporary=bool(d.get("is_temporary")),
            ext_id=str(pk),
            description=(d.get("description_title") or "") + " " + (d.get("description") or ""),
            lat=d.get("latitude"),
            lng=d.get("longitude"),
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
            ext_id=lid,
            description=title + " " + text,
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
# Quellen: ImmoStreet und alle-immobilien (Ableger von Homegate, Seiten als HTML)
# --------------------------------------------------------------------------

ADDR_RE = re.compile(r"^(?P<street>.*?)[,\s]+(?P<zip>\d{4})\s+(?P<city>.+)$")


def split_address(text: str) -> tuple[str, str, str]:
    """'Weingartenweg 2, 8049 Zürich' -> (Strasse, PLZ, Ort)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    m = ADDR_RE.match(text)
    if m:
        return m.group("street").strip(" ,"), m.group("zip"), m.group("city").strip()
    m = re.match(r"^(\d{4})\s+(.+)$", text)
    if m:
        return "", m.group(1), m.group(2)
    return text, "", ""


def smg_satellite_url(host: str, flt: dict, sort: bool = True) -> str:
    rooms = f"{float(flt['min_rooms']):g}"
    # alle-immobilien liefert mit Sortier-Parameter eine Fehlerseite
    return (f"https://{host}/de/mieten/wohnung/in-zuerich/preis--{float(flt['max_rent']):.1f}"
            f"/zimmer-{rooms}-?{'sort=cd&' if sort else ''}pageNum={{page}}")


def parse_immostreet(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for art in soup.select("article.results-item"):
        link = art.select_one("a.link[href]")
        loc = art.select_one(".location")
        if not link or not loc:
            continue
        street, zipcode, city = split_address(loc.get_text(" ", strip=True))
        attrs = {}
        for li in art.select(".results-attributes li"):
            k, v = li.select_one(".key"), li.select_one(".value")
            if k and v:
                attrs[k.get_text(strip=True)] = v.get_text(" ", strip=True)
        price = art.select_one(".results-price .amount")
        try:
            coords = json.loads(art.get("data-map-location") or "{}")
        except json.JSONDecodeError:
            coords = {}
        title = art.select_one(".title")
        out.append(make_listing(
            source="ImmoStreet", address=street, zipcode=zipcode, quartier=city,
            rooms=attrs.get("Zimmer"), area=attrs.get("Wohnfläche"),
            rent=price.get_text(strip=True) if price else None,
            url=clean_url(link["href"]), ext_id=link.get("data-ad-id", ""),
            description=title.get_text(" ", strip=True) if title else "",
            lat=coords.get("lat"), lng=coords.get("lng"),
        ))
    return out


def parse_alleimmo(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a.search-results__list__item[href]"):
        tags = {}
        for holder in a.select(".tag-holder"):
            label = holder.select_one(".tags__tag")
            if label:
                name = label.get_text(strip=True)
                label.extract()
                tags[name] = holder.get_text(" ", strip=True)
        street, zipcode, city = split_address(tags.get("Adresse", ""))
        title = a.select_one(".search-results__list__item__meta__title")
        desc = a.select_one(".search-results__list__item__meta__description")
        out.append(make_listing(
            source="alle-immobilien", address=street, zipcode=zipcode, quartier=city,
            rooms=tags.get("Zimmer"), area=tags.get("Fläche"), rent=tags.get("Preis"),
            url=clean_url(a["href"]), ext_id=a.get("data-ad-id", ""),
            description=(title.get_text(" ", strip=True) if title else "") + " "
                        + (desc.get_text(" ", strip=True) if desc else ""),
        ))
    return out


def source_paged(name: str, url_tpl: str, parser, pages: int = 5) -> list[dict]:
    results, seen = [], set()
    for page in range(1, pages + 1):
        try:
            html = fetch(url_tpl.format(page=page))
        except requests.HTTPError as e:
            if page > 1 and e.response is not None and e.response.status_code == 404:
                break  # hinter der letzten Seite antwortet ImmoStreet mit 404
            raise
        found = [l for l in parser(html) if l["url"] not in seen]
        if not found:
            if page == 1:
                raise RuntimeError("keine Inserate lesbar (Seitenaufbau geändert oder Abruf gesperrt)")
            break
        seen.update(l["url"] for l in found)
        results.extend(found)
        time.sleep(2)
    return results


# --------------------------------------------------------------------------
# Quelle: RealAdvisor (sammelt Inserate vieler Portale)
# robots.txt verbietet Such-Parameter -> nur die erste Seite je Zimmer-Kategorie
# --------------------------------------------------------------------------

DETAIL_CACHE: dict[str, str] = {}  # "v<TEMP_V>:<Detailseite>" -> Befristungs-Hinweis ('' = unbefristet)
_UESC = re.compile(r"\\u([0-9a-fA-F]{4})")


def realadvisor_temp(url: str) -> str:
    """Prüft die volle Beschreibung auf der Detailseite (steht im eingebetteten Next.js-Datenteil)."""
    key = f"v{TEMP_V}:{url}"  # neue Erkennung -> Seite neu prüfen
    if key in DETAIL_CACHE:
        return DETAIL_CACHE[key]
    try:
        raw = fetch(url)
    except Exception:  # noqa: BLE001
        return ""  # beim nächsten Lauf erneut versuchen
    time.sleep(1)
    soup = BeautifulSoup(raw, "html.parser")
    more = soup.find(string=re.compile(r"Mehr lesen"))
    para = more.find_parent().find_previous("p") if more else None
    text = para.get_text(" ", strip=True).rstrip(".… ") if para else ""
    tail = text[-40:]
    if len(tail) >= 20:
        # sichtbar ist nur der Anfang; die Fortsetzung steht als JSON-String im Datenteil
        flat = _UESC.sub(lambda m: chr(int(m.group(1), 16)), raw)
        rest = ""
        for m in re.finditer(re.escape(tail), flat):
            end = flat.find('"', m.end())
            seg = flat[m.end():end if end > 0 else m.end() + 8000]
            if seg.startswith("..."):  # das ist die gekürzte, sichtbare Fassung
                continue
            seg = re.sub(r"<[^>]+>", " ", seg.replace("\\n", " ").replace("\\", ""))
            if len(seg) > len(rest):
                rest = seg
        text += rest
    h1 = soup.find("h1")
    hint = temp_reason((h1.get_text(" ", strip=True) if h1 else "") + " " + text)
    DETAIL_CACHE[key] = hint
    return hint


def source_realadvisor(flt: dict) -> list[dict]:
    min_whole = max(1, int(float(flt["min_rooms"])))
    cats = [f"{w}-zimmer" for w in range(min_whole, 6)] + ["uber-6-zimmer"]
    results, seen = [], set()
    for slug_ in cats:
        html = fetch(f"https://realadvisor.ch/de/mieten/stadt-zurich/wohnung/{slug_}")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select('a[href^="/de/mieten/wohnung/"]'):
            href = a["href"]
            if href in seen:
                continue
            card = a
            for _ in range(8):
                txt = card.get_text(" | ", strip=True)
                if "Zimmer" in txt and "CHF" in txt:
                    break
                card = card.parent
            else:
                continue
            seen.add(href)
            parts = [p.strip() for p in card.get_text(" | ", strip=True).split(" | ")]
            rooms = area = rent = None
            addr, landlord = "", ""
            for i, p in enumerate(parts):
                m = re.match(r"^([\d.,]+)\s*Zimmer\b(?:\s*•\s*([\d.,]+)\s*m²)?", p)
                if m and rooms is None:
                    rooms, area = m.group(1), m.group(2)
                    landlord = parts[i - 1] if i and parts[i - 1] not in ("Top", "Neu") else ""
                    if i + 1 < len(parts):
                        addr = parts[i + 1]
                elif rent is None and re.match(r"^CHF\s*[\d']+$", p):
                    rent = p
            street, zipcode, city = split_address(addr)
            listing = make_listing(
                source="RealAdvisor", address=street, zipcode=zipcode, quartier=city,
                rooms=rooms, area=area, rent=rent, landlord=landlord,
                url="https://realadvisor.ch" + href, ext_id=href,
                description=" ".join(parts[:12]),
            )
            # Detailseite nur für Inserate abrufen, die sonst passen würden
            rough = dict(listing, zip=listing.get("zip") or int(flt["zip_from"]))
            if not listing["temporary"] and matches(rough, flt):
                hint = realadvisor_temp(listing["url"])
                if hint:
                    listing["temporary"], listing["tempHint"] = True, hint
            results.append(listing)
        time.sleep(2)
    if not results:
        raise RuntimeError("keine Inserate lesbar (Seitenaufbau geändert oder Abruf gesperrt)")
    return results


# --------------------------------------------------------------------------
# Quelle: immobilier.ch (alle Inserate der Stadt Zürich, Filter lokal)
# --------------------------------------------------------------------------

def parse_immobilier(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("div.filter-item[data-id]"):
        link = next((a for a in item.select("a[href]")
                     if re.search(r"-\d{6,}$", a["href"])), None)
        if not link:
            continue
        txt = item.get_text(" | ", strip=True)
        m_rent = re.search(r"CHF\s*([\d']+)(?:\.-)?\s*/\s*Monat(?:\s*\(\+\s*([\d']+))?", txt)
        rent, rent_type = None, "brutto"
        if m_rent:
            rent = to_float(m_rent.group(1))
            if m_rent.group(2):
                rent += to_float(m_rent.group(2)) or 0
            else:
                rent_type = "netto"
        m_rooms = re.search(r"(\d+(?:[.,]\d)?|ein)\s+Zimmer", txt)
        rooms = None
        if m_rooms:
            rooms = 1 if m_rooms.group(1) == "ein" else m_rooms.group(1)
        m_addr = re.search(r"\|\s*Zürich,\s*([^|]+?)\s*\|", txt)
        m_area = re.search(r"\|\s*(\d+)\s*m\s*\|", txt)
        lat = lng = None
        if item.get("data-latlng"):
            try:
                lat, lng = (float(x) for x in item["data-latlng"].split(","))
            except ValueError:
                pass
        out.append(make_listing(
            source="immobilier.ch", address=m_addr.group(1) if m_addr else "", zipcode=None,
            quartier="Zürich", rooms=rooms, rent=rent, rent_type=rent_type,
            area=m_area.group(1) if m_area else None,
            url=urljoin("https://www.immobilier.ch", link["href"]), ext_id=item["data-id"],
            landlord=link["href"].split("/")[-2].rsplit("-", 1)[0].replace("-", " "),
            description=link.get_text(" ", strip=True), lat=lat, lng=lng,
        ))
    return out


# --------------------------------------------------------------------------
# Quelle: Ron Orp (Kleinanzeigen; ohne JavaScript nur die ~10 neuesten Wohn-Inserate)
# --------------------------------------------------------------------------

def source_ronorp() -> list[dict]:
    html = fetch("https://ronorp.net/zurich/market/housing")
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        raise RuntimeError("keine Inserate lesbar (Seitenaufbau geändert)")
    posts = (json.loads(tag.string)["props"]["pageProps"].get("ssrPostTypeListPosts") or {}).get("posts") or []
    results = []
    for p in posts:
        hd = p.get("housing_detail") or {}
        if not p.get("id") or (p.get("sub_category") or {}).get("name") != "Wohnung" or hd.get("looking_to") != "rent":
            continue
        loc = p.get("location") or {}
        street, zipcode, city = split_address(re.sub(r",\s*Schweiz$", "", loc.get("address") or ""))
        desc = BeautifulSoup(p.get("description") or "", "html.parser").get_text(" ", strip=True)
        listing = make_listing(
            source="Ron Orp", address=street, zipcode=zipcode or p.get("zip_code"), quartier=city or "Zürich",
            rooms=hd.get("num_of_rooms"), rent=p.get("price"), rent_type="",
            area=hd.get("floor_size"), url=f"https://ronorp.net/market/posts/{p.get('seo_slug') or p['id']}",
            temporary=hd.get("contract") not in (None, "permanent"),
            ext_id=str(p["id"]), description=(p.get("title") or "") + " " + desc,
            lat=loc.get("latitude"), lng=loc.get("longitude"),
        )
        listing["coop"] = listing["coop"] or "privat"
        listing["zipOptional"] = hd.get("city_or_agglo") == "city"
        listing["note"] = (p.get("title") or "")[:200]
        results.append(listing)
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


def page_lines(raw: str) -> list[str]:
    """Zerlegt eine Seite (HTML oder JSON-Schnittstelle) in Textzeilen."""
    raw = raw or ""
    if raw.lstrip()[:1] in "{[":
        try:
            parts = []

            def walk(o):
                if isinstance(o, dict):
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
                elif isinstance(o, str) and not re.match(r"^\d{4}-\d\d-\d\dT|^https?://|^[a-z0-9]{20,}$", o):
                    parts.append(BeautifulSoup(o, "html.parser").get_text("\n") if "<" in o else o)
            walk(json.loads(raw))
            raw_lines = "\n".join(parts).split("\n")
        except json.JSONDecodeError:
            raw_lines = raw.split("\n")
    else:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form", "svg"]):
            tag.decompose()
        raw_lines = soup.get_text("\n").split("\n")
    lines = [re.sub(r"\s+", " ", l).strip(" |·-") for l in raw_lines]
    return [l for l in lines if len(l) > 2]


def page_text(html: str) -> str:
    text = " ".join(page_lines(html))
    # Datumsangaben, Zähler u. ä. glätten, damit nicht jede Kleinigkeit als Änderung gilt
    return re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b", "", text)


# Zeile erwähnt irgendeine Zimmerzahl (unabhängig vom Filter, damit Filteränderungen nichts «neu» machen)
ANY_ROOMS_RE = re.compile(r"(?<![\d.,])\d{1,2}(?:[.,]5|\s?½|\s?1/2)?\s*-?\s*(?:zimmer|zi\b|zi\.)", re.I)
# Hinweise auf ein konkretes Inserat in der Umgebung einer Zimmerzahl
RENT_HINT_RE = re.compile(
    r"\b(?:mietzins|miete|bruttomiete|nettomiete|netto|brutto|nebenkosten|akonto)\b\W{0,5}(?:chf|fr\.?)?\s*[\d'’]{3,}"
    r"|chf\s*[\d'’]{3,}[^·.]{0,15}?(?:/|pro\s+monat|mtl|monatlich|netto|brutto|inkl|exkl|zzgl|\+\s*nk)"
    r"|(?:^|·\s)(?:chf|fr\.)\s*[\d'’]{3,6}(?:\.-|\.–|\.00)?\s*(?:·|$)", re.I)  # eigene kurze Zeile «CHF 2290»
DATE_HINT_RE = re.compile(
    r"\b(?:bezug\w*|frei\s+ab|verfügbar\s+ab|ab\s+sofort|per\s+sofort|mietbeginn|einzug\w*|einzugstermin)\b"
    r"|\b(?:per|ab)\s+(?:\d{1,2}\.\s*\d{1,2}\.|\d{1,2}\.\s+[a-zä]+\s+\d{4}|anfang|mitte|ende)", re.I)
# Regeln und Tabellen, die Zimmerzahlen nennen, ohne eine Wohnung anzubieten
RULE_LINE_RE = re.compile(r"anteil\w*|mindestens\s+\d+\s+person|belegung\w*|richtet\s+sich|halbe\s+zimmer"
                          r"|pro\s+zimmer|minus\s+1|max\.\s*\d+\s*zimmer|personen", re.I)
RULE_WINDOW_RE = re.compile(r"belegungsvorschrift|mindestbelegung|belegungsrichtlinie|vermietungsreglement", re.I)
DIGIT_RE = re.compile(r"\d")


def room_value(text: str):
    m = re.search(r"(\d{1,2})(?:[.,](5)|\s?(½|1/2))?", text)
    if not m:
        return None
    return int(m.group(1)) + (0.5 if (m.group(2) or m.group(3)) else 0)
COOP_NEW_DAYS = 14  # so lange bleibt eine neu aufgetauchte Erwähnung als Hinweis stehen


def coop_hint_rent(window: str):
    m = re.search(r"(?:mietzins|miete|brutto|bruttomiete|netto|nettomiete)\D{0,25}?(?:chf|fr\.?)?\s*([\d'’]{3,6})", window, re.I) \
        or re.search(r"chf\s*([\d'’]{3,6})(?:\.-|\.–|\.00)?\s*(?:/\s*mt|/\s*monat|pro\s+monat|mtl|monatlich)", window, re.I)
    if not m and not re.search(r"anteil|kaution|depot", window, re.I):
        m = re.search(r"(?:chf|fr\.)\s*([\d'’]{3,6})", window, re.I)
    val = to_float(m.group(1)) if m else None
    return val if val and 500 <= val <= 15000 else None


def source_coops(coops: list, flt: dict, prev_state: dict) -> tuple[list[dict], dict, list[str]]:
    rx = rooms_pattern(float(flt["min_rooms"]))
    hits, state, failed = [], {}, []
    now = datetime.now(TZ)
    for name, url, *rest in coops:
        # optional dritter Eintrag: Adresse, unter der die Daten tatsächlich stehen (z. B. Schnittstelle)
        fetch_url = rest[0] if rest else url
        old = prev_state.get(url, {})
        entry = {"name": name, "url": url, "status": "unbekannt", "hash": None,
                 "lastChange": old.get("lastChange"), "checked": now_iso()}
        try:
            raw = fetch(fetch_url)
            lines = page_lines(raw)
            text = page_text(raw)
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
        if old.get("hash") not in (None, h):
            entry["lastChange"] = now_iso()
        entry["hash"] = h

        # Zeilen mit Zahlen merken: wann tauchten sie zum ersten Mal auf?
        baseline = "lines" not in old  # erster Lauf mit dieser Logik: bestehender Inhalt gilt als alt
        seen_before = old.get("lines", {})
        # Seiten wie «Freie 4.5-Zimmer-Wohnungen» nennen die Grösse nur in der Überschrift
        title_rooms = next((rx.search(l) for l in lines[:5] if rx.search(l)), None)
        line_state, found = {}, []
        last_hit = -10
        for i, line in enumerate(lines):
            if not DIGIT_RE.search(line) or len(line_state) >= 400:
                continue
            key = hashlib.sha1(line.lower().encode()).hexdigest()[:12]
            first = seen_before.get(key) or ("alt" if baseline else now_iso())
            line_state[key] = first
            if i - last_hit <= 3:  # gehört noch zum selben Inserat bzw. zur selben Tabellenzeile
                continue
            is_new = first != "alt" and (now - datetime.fromisoformat(first)).days < COOP_NEW_DAYS
            window = " · ".join(lines[max(0, i - 3): i + 4])
            m = rx.search(line)
            if not m:
                # neue Tabellenzeile ohne Zimmerzahl auf einer Seite, deren Überschrift die Grösse nennt
                if is_new and title_rooms and not ANY_ROOMS_RE.search(line) and (
                        RENT_HINT_RE.search(window) or DATE_HINT_RE.search(window)):
                    found.append((key, title_rooms.group(0), line, window, True, first))
                    last_hit = i
                continue
            if RULE_LINE_RE.search(line) or RULE_WINDOW_RE.search(window):
                continue
            is_listing = bool(RENT_HINT_RE.search(window) or DATE_HINT_RE.search(window))
            if is_listing or is_new:
                found.append((key, m.group(0), line, window, is_listing, first))
                last_hit = i
        entry["lines"] = line_state

        says_none = bool(NONE_RE.search(text))
        if found:
            entry["status"] = "Inserat erkannt" if any(f[4] for f in found) else "neue Erwähnung"
        else:
            entry["status"] = "keine freien Wohnungen" if says_none else "nichts Passendes"
        entry["sizes"] = sorted({f[1] for f in found})[:5]
        state[url] = entry

        for key, size, line, window, is_listing, first in found[:6]:
            reason = temp_reason(window)
            label = "Inserat auf der Seite" if is_listing else "Neu auf der Seite"
            if first != "alt":
                label += f" (seit {datetime.fromisoformat(first).strftime('%d.%m.')})"
            hits.append({
                "id": slug(f"coop-{name}-{key}"),
                "kind": "genossenschaft",
                "coop": name,
                "source": "Website",
                "address": "",
                "zip": None,
                "quartier": "",
                "rooms": room_value(size),
                "rent": coop_hint_rent(window),
                "rentType": "",
                "area": None,
                "moveIn": "",
                "temporary": bool(reason),
                "tempHint": reason,
                "url": url,
                "note": f"{label}: {line[:200]}" + (f" | Umfeld: {window[:300]}" if window != line else ""),
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

PARTIAL_SOURCES = {"RealAdvisor", "Ron Orp"}


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
    GEOCACHE.update(data.get("geocache", {}))
    DETAIL_CACHE.update({k: v for k, v in data.get("detailcache", {}).items() if k.startswith(f"v{TEMP_V}:")})

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
    if src.get("immostreet"):
        run_source("ImmoStreet", lambda: source_paged(
            "ImmoStreet", smg_satellite_url("www.immostreet.ch", flt), parse_immostreet))
    if src.get("alleimmobilien"):
        run_source("alle-immobilien", lambda: source_paged(
            "alle-immobilien", smg_satellite_url("www.alle-immobilien.ch", flt, sort=False), parse_alleimmo))
    if src.get("realadvisor"):
        run_source("RealAdvisor", lambda: source_realadvisor(flt))
    if src.get("immobilier"):
        run_source("immobilier.ch", lambda: source_paged(
            "immobilier.ch", "https://www.immobilier.ch/de/mieten/wohnung/zurich/zurich/seite-{page}",
            parse_immobilier, pages=15))
    if src.get("ronorp"):
        run_source("Ron Orp", source_ronorp)

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
        if not l.get("isHint"):
            # Koordinaten/PLZ nur für Inserate nachschlagen, die bei Zimmern und Miete passen
            rough = dict(l, zip=l.get("zip") or int(flt["zip_from"]))
            if not matches(rough, flt):
                continue
            had_zip = bool(l.get("zip"))
            locate(l)
            if l["address"] and not had_zip and l.get("zip"):  # PLZ erst durch Nachschlagen bekannt
                l["id"] = slug(listing_key(l["address"], l["zip"], l["rooms"]))
        hint_ok = l.get("isHint") and not (l.get("rent") and l["rent"] > float(flt["max_rent"]))
        if hint_ok or (not l.get("isHint") and matches(l, flt)):
            if l["id"] in current:  # dieselbe Wohnung auf mehreren Portalen
                cur = current[l["id"]]
                if l["source"] not in cur["source"]:
                    cur["source"] += ", " + l["source"]
                if cur["kind"] == "markt" and l["kind"] == "genossenschaft":
                    cur["kind"], cur["coop"] = "genossenschaft", l["coop"]
                if l.get("temporary") and not cur.get("temporary"):
                    cur["temporary"], cur["tempHint"] = True, l.get("tempHint", "")
                for k in ("lat", "lng", "area", "moveIn", "coop"):
                    if not cur.get(k) and l.get(k):
                        cur[k] = l[k]
                if cur.get("approx") and l.get("lat") and not l.get("approx"):
                    cur["lat"], cur["lng"], cur["approx"] = l["lat"], l["lng"], False
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
        complete = sources - PARTIAL_SOURCES
        if complete:
            gone = all((relevant(s) or s) in ok_sources for s in complete)
        else:
            # Quellen, von denen nur die neuesten Inserate gelesen werden: erst nach 14 Tagen als weg werten
            last = l.get("lastSeen") or l.get("firstSeen")
            gone = bool(last) and datetime.fromisoformat(last).timestamp() < time.time() - 14 * 86400
        if gone:
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
        "geocache": dict(list(GEOCACHE.items())[-3000:]),
        "detailcache": dict(list(DETAIL_CACHE.items())[-3000:]),
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
