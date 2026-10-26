# ParkVia Scraper (Wien + Zürich)

Holt täglich die Parkpreise von book.parkvia.com für Wien (VIE) und Zürich (ZRH)
und schreibt sie in ein Google Sheet.

- **Tabellenblatt `Wien` / `Zürich`:** aktueller Stand (wird bei jedem Lauf überschrieben)
- **Tabellenblatt `Verlauf`:** jeder Lauf wird angehängt (Historie). Wird automatisch angelegt.

## Einrichtung

1. Neues **öffentliches** Repository auf GitHub anlegen und alle Dateien hochladen
   (inkl. Ordner `.github/workflows/`).
2. **Settings → Secrets and variables → Actions → New repository secret**:
   - `GOOGLE_CREDENTIALS` = kompletter Inhalt der Service-Account-JSON-Datei
   - `SPREADSHEET_ID` = ID des Google Sheets (der Teil zwischen `/d/` und `/edit` im Link)
3. **Actions** → „ParkVia Scraper“ → **Run workflow** zum Testen.
4. Danach läuft er automatisch jeden Tag um 05:00 UTC.

## Sicherheit

Das Repository ist öffentlich. Zugangsdaten gehören **nur** in die GitHub Secrets,
niemals als Datei ins Repository. `.gitignore` blockiert `*.json` zusätzlich.

## Lokal testen

```
pip install -r requirements.txt
set GOOGLE_CREDENTIALS=C:\pfad\zu\credentials.json
set SPREADSHEET_ID=...
python scraper_vie_ch.py --city wien --days 2 --dry-run
```
