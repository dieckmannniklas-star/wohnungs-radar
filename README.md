# Wohnungs-Radar Zürich

Sucht alle 4 Stunden nach Mietwohnungen in der Stadt Zürich und meldet neue Treffer per E-Mail. Läuft kostenlos auf GitHub, ohne Claude und ohne eigenen Computer.

**Was es durchsucht**

- **Flatfox, ImmoStreet, alle-immobilien, RealAdvisor, immobilier.ch und Ron Orp**: alle Inserate, die deinen Filtern entsprechen (Standard: ab 4,5 Zimmer, bis CHF 4'000 brutto, PLZ 8000–8099). Inserate von Genossenschaften werden markiert. ImmoStreet und alle-immobilien zeigen die Inserate von Homegate und ImmoScout24, die selbst automatische Abrufe sperren.
- **Rund 70 Genossenschafts-Websites**: Das Programm merkt sich jede Zeile der Seiten und meldet zwei Dinge: Zeilen, die wie ein Inserat aussehen (passende Zimmerzahl plus Miete oder Bezugstermin), und neu aufgetauchte Erwähnungen einer passenden Zimmerzahl (14 Tage lang). Regeln und Tabellen wie Anteilkapital oder Belegungsvorschriften werden ignoriert. Genaue Angaben siehst du auf der Seite der Genossenschaft.

**Was du bekommst**

- eine Webseite mit allen Treffern, einer Karte, Filtern und einem Button «Aktualisieren»
- eine E-Mail bei jedem neuen Treffer

---

## Einrichtung (ca. 20 Minuten, einmalig)

### 1. Repository anlegen

1. Konto auf [github.com](https://github.com) erstellen (gratis), falls du noch keins hast.
2. Oben rechts **+ → New repository**. Name z. B. `wohnungs-radar`. Sichtbarkeit **Public** (die Gratis-Webseite funktioniert nur bei öffentlichen Repositories; es stehen keine persönlichen Daten darin, nur Inserate und Filter).
3. Auf der leeren Repository-Seite **uploading an existing file** wählen und den *Inhalt* des entpackten Ordners hineinziehen, inklusive des Ordners `.github` (in Chrome/Edge kann man ganze Ordner ziehen). **Commit changes** klicken.
   - Wird `.github` nicht mit hochgeladen (versteckter Ordner): **Add file → Create new file**, als Namen `.github/workflows/radar.yml` eintippen und den Inhalt der Datei einfügen.

### 2. E-Mail-Versand einrichten

Am einfachsten mit einem Gmail-Konto:

1. Im Google-Konto die Zwei-Faktor-Anmeldung aktivieren, dann unter [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) ein **App-Passwort** erstellen.
2. Im Repository: **Settings → Secrets and variables → Actions → New repository secret**. Diese fünf Einträge anlegen:

| Name | Wert |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | deine Gmail-Adresse |
| `SMTP_PASSWORD` | das App-Passwort (16 Zeichen) |
| `MAIL_TO` | Adresse, an die die Treffer gehen (mehrere mit Komma trennen) |

Andere Anbieter funktionieren auch (z. B. Outlook: `smtp-mail.outlook.com`, Port `587`).

### 3. Webseite einschalten

**Settings → Pages**: bei *Source* «Deploy from a branch», Branch `main`, Ordner `/docs`, **Save**. Nach ein bis zwei Minuten ist die Seite unter `https://DEIN-NAME.github.io/wohnungs-radar/` erreichbar.

### 4. Erste Suche starten

**Actions** → falls gefragt, Workflows aktivieren → links «Wohnungs-Radar» → **Run workflow**. Nach 3–6 Minuten sind die ersten Ergebnisse da und du bekommst eine Mail «Wohnungs-Radar eingerichtet». Danach läuft die Suche alle 4 Stunden von selbst.

### 5. Aktualisieren-Button und Filter auf der Webseite freischalten

Damit die Webseite eine Suche sofort starten und Filter speichern kann, braucht sie einen Zugangsschlüssel:

1. [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new) öffnen (Fine-grained token).
2. Name z. B. «Wohnungs-Radar», Ablaufdatum wählen, bei *Repository access* «Only select repositories» → dein `wohnungs-radar`.
3. Unter *Permissions → Repository permissions*: **Actions: Read and write** und **Contents: Read and write**.
4. **Generate token**, den Schlüssel kopieren.
5. Auf der Radar-Webseite «Verbindung zu GitHub» aufklappen, Schlüssel einfügen, **Speichern**.

Der Schlüssel bleibt nur in deinem Browser. Ohne Schlüssel lädt «Aktualisieren» die letzten Ergebnisse neu; mit Schlüssel startet es eine neue Suche.

---

## Bedienung

- **Aktualisieren**: startet sofort eine Suche und lädt danach die Ergebnisse (3–6 Minuten).
- **Filter oben**: ändern nur die Anzeige, z. B. «Nur Genossenschaften» oder ein bestimmtes Quartier.
- **Suchfilter für E-Mail-Alarm**: legt fest, wonach gesucht und was gemailt wird (Zimmer, Miete, PLZ-Bereich, Quellen). Speichern startet automatisch eine neue Suche.
- Filter kannst du auch direkt in `config.json` auf GitHub ändern (Stift-Symbol).
- **Pausieren**: Actions → Wohnungs-Radar → «…» → Disable workflow.

## Gut zu wissen

- Das Programm respektiert die `robots.txt` der Websites. Seiten, die automatische Abrufe verbieten, erscheinen in der Liste als «gesperrt» und werden übersprungen. Newhome ist aus diesem Grund nicht dabei.
- Homegate, ImmoScout24, Comparis, Newhome, Tutti und Anibis sperren Abrufe von GitHub-Servern (HTTP 403) und sind deshalb aus.
- RealAdvisor erlaubt nur die erste Ergebnisseite je Zimmer-Kategorie, Ron Orp liefert ohne JavaScript nur die rund 10 neuesten Wohn-Inserate. Inserate dieser beiden Quellen gelten erst nach 14 Tagen ohne Sichtung als vergeben.
- Die Karte nutzt OpenStreetMap; jeder Treffer hat zusätzlich einen Link «Google Maps». Standorte ohne genaue Adresse werden über das amtliche Adressverzeichnis (geo.admin.ch) ermittelt oder ungefähr am PLZ-Mittelpunkt gezeigt.
- Portale ändern gelegentlich ihren Seitenaufbau. Liefert eine Quelle plötzlich nichts mehr, steht das bei «Letzte Suche … teilweise nicht erreichbar».
- GitHub pausiert geplante Abläufe in Repositories ohne Aktivität nach 60 Tagen. Da jede Suche Ergebnisse speichert, passiert das hier normalerweise nicht; falls doch, unter Actions wieder aktivieren.
- Zeiten der automatischen Suche: alle 4 Stunden um :43 (UTC), also z. B. 02:43, 06:43, 10:43, 14:43, 18:43, 22:43 Uhr Schweizer Sommerzeit.
