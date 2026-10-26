"""
ParkVia Multi-City Scraper
==========================
Scraped Städte: Wien (VIE) und Zürich (ZRH)
- Direkte URL-Aufrufe an book.parkvia.com
- Blazor-aware: wartet auf gerenderte Karten
- Schreibt in Google Sheets:
    * Tabellenblatt "Wien" / "Zürich": aktueller Stand (wie bisher, wird überschrieben)
    * Tabellenblatt "Verlauf": jeder Lauf wird angehängt (Historie, wird nie überschrieben)

Zugangsdaten kommen NUR aus Umgebungsvariablen (GitHub Secrets):
    GOOGLE_CREDENTIALS  – kompletter Inhalt der Service-Account-JSON
    SPREADSHEET_ID      – ID des Google Sheets

Verwendung:
  python scraper_vie_ch.py                        # Wien + Zürich
  python scraper_vie_ch.py --city wien            # Nur Wien
  python scraper_vie_ch.py --city zuerich         # Nur Zürich
  python scraper_vie_ch.py --dry-run              # Nichts ins Sheet schreiben
  python scraper_vie_ch.py --no-headless          # Browser-Fenster anzeigen
  python scraper_vie_ch.py --city wien --days 2   # Schnelltest: nur 2 Tage
"""

import os
import json
import time
import logging
import re
import argparse
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# STADT-KONFIGURATIONEN
# ─────────────────────────────────────────────────────────────

CITIES = {
    "wien": {
        "name":       "Wien",
        "location":   "VIE",
        "sheet_name": "Wien",
        "currency":   "EUR",
        "min_price":  5.0,    # Untergrenze echte Preise (Gebühren = 1.99)
    },
    "zuerich": {
        "name":       "Zürich",
        "location":   "ZRH",
        "sheet_name": "Zürich",
        "currency":   "CHF",
        "min_price":  10.0,   # CHF-Gebühren = 1.49
    },
}

# ─────────────────────────────────────────────────────────────
# GEMEINSAME KONFIGURATION
# ─────────────────────────────────────────────────────────────

RESULTS_URL   = "https://book.parkvia.com/de-DE/book/results"
MAX_DAYS      = 30
WAIT_TIMEOUT  = 45
HISTORY_SHEET = "Verlauf"

# Anreise in X Tagen ab heute. [1] = morgen (wie bisher).
# Für mehr Überblick später z. B. [1, 7, 14, 30, 60, 90] eintragen.
# Das Tabellenblatt Wien/Zürich zeigt immer nur die erste Anreise, der Verlauf alle.
ANREISE_IN_TAGEN = [1]

HISTORY_HEADER = [
    "Scrape-Zeitpunkt", "Stadt", "Anreise", "Abholung",
    "Tage", "Anbieter", "Preis", "Währung",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ─────────────────────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────────────────────

def open_spreadsheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not creds_json:
        raise RuntimeError("Umgebungsvariable GOOGLE_CREDENTIALS fehlt.")
    if not spreadsheet_id:
        raise RuntimeError("Umgebungsvariable SPREADSHEET_ID fehlt.")
    try:
        creds_dict = json.loads(creds_json)
    except json.JSONDecodeError:
        # Lokal: Pfad zu einer JSON-Datei statt Inhalt
        with open(creds_json) as f:
            creds_dict = json.load(f)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(spreadsheet_id)


def get_history_sheet(spreadsheet):
    """Öffnet das Tabellenblatt 'Verlauf'. Legt es an, falls es nicht existiert."""
    try:
        return spreadsheet.worksheet(HISTORY_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=HISTORY_SHEET, rows=1000, cols=len(HISTORY_HEADER))
        ws.update(range_name="A1", values=[HISTORY_HEADER])
        logger.info(f"  Tabellenblatt '{HISTORY_SHEET}' angelegt.")
        return ws


def get_header_map(ws) -> dict:
    return {h.strip(): i + 1 for i, h in enumerate(ws.row_values(1)) if h.strip()}


def write_prices(ws, header_map: dict, days: int, prices: dict):
    """
    Schreibt Preise in Zeile `days` (Zeile 1 = Kopfzeile, wie bisher).
    Spalten ohne Preis bekommen "N/A" (Parkplatz nicht verfügbar).
    Spalte A wird nie überschrieben.
    """
    updates = []
    matched_cols = set()

    for name, price in prices.items():
        col = header_map.get(name)
        if col is None:
            for h, c in header_map.items():
                if name.lower() in h.lower() or h.lower() in name.lower():
                    col = c
                    break
        if col and col != 1:
            updates.append({
                "range":  gspread.utils.rowcol_to_a1(days, col),
                "values": [[price]],
            })
            matched_cols.add(col)
        elif not col:
            logger.warning(f"  Spalte nicht gefunden: '{name}'")

    for col in header_map.values():
        if col != 1 and col not in matched_cols:
            updates.append({
                "range":  gspread.utils.rowcol_to_a1(days, col),
                "values": [["N/A"]],
            })

    if updates:
        ws.batch_update(updates)
        na_count = len([u for u in updates if u["values"] == [["N/A"]]])
        logger.info(f"  -> {len(updates) - na_count} Preise + {na_count} N/A fuer {days} Tage geschrieben.")
    else:
        logger.warning(f"  -> Keine Updates fuer {days} Tage.")


def append_history(ws, rows: list):
    """Hängt alle Zeilen eines Laufs in einem Aufruf an das Blatt 'Verlauf' an."""
    if not rows:
        return
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    logger.info(f"  -> {len(rows)} Zeilen an '{HISTORY_SHEET}' angehängt.")


# ─────────────────────────────────────────────────────────────
# BROWSER
# ─────────────────────────────────────────────────────────────

def create_driver(headless: bool = True) -> webdriver.Chrome:
    opt = Options()
    if headless:
        opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--disable-gpu")
    opt.add_argument("--window-size=1400,900")
    opt.add_argument("--lang=de-DE")
    opt.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # Selenium Manager (ab Selenium 4.6) lädt den passenden ChromeDriver automatisch
    return webdriver.Chrome(options=opt)


def build_url(location: str, startdate: datetime, enddate: datetime) -> str:
    return (
        f"{RESULTS_URL}?producttype=P&location={location}"
        f"&startdate={startdate.strftime('%Y-%m-%d')}"
        f"&enddate={enddate.strftime('%Y-%m-%d')}"
        f"&starttime=11%3A00&endtime=11%3A00"
        f"&adults=1&children=0&infants=0"
    )


def accept_cookies(driver):
    """Akzeptiert Cookie-Banner falls vorhanden."""
    deadline = time.time() + 10
    while time.time() < deadline:
        btns = driver.find_elements(
            By.XPATH,
            "//button[normalize-space(.)='Accept All'] | "
            "//button[normalize-space(.)='Alle akzeptieren']",
        )
        for btn in btns:
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                logger.info("  Cookie-Banner akzeptiert.")
                time.sleep(1.5)
                return
        time.sleep(0.5)


# ─────────────────────────────────────────────────────────────
# PREIS-PARSING
# ─────────────────────────────────────────────────────────────

def parse_price(text: str) -> float | None:
    """Wandelt Preistext in float um. Unterstützt €/CHF, Komma/Punkt, Apostroph."""
    if not text:
        return None
    try:
        cleaned = re.sub(r"[^\d,\.]", "", text.replace("'", ""))
        if not cleaned:
            return None
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        m = re.search(r"\d+\.?\d*", cleaned)
        if m:
            val = float(m.group())
            if 1.0 < val < 99999.0:
                return val
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────
# WARTEN AUF BLAZOR
# ─────────────────────────────────────────────────────────────

def wait_for_results(driver, city_cfg: dict, timeout: int = WAIT_TIMEOUT) -> str:
    """
    Wartet bis Blazor die echten Karten geladen hat.
    Gibt den innerText des #serp Containers zurück (oder '' bei Timeout).
    """
    currency = city_cfg["currency"]
    min_price = city_cfg["min_price"]

    if currency == "CHF":
        price_re = re.compile(r"CHF\s*([\d'][0-9']*[.,][0-9]{2})")
    else:
        price_re = re.compile(r"[€\$]\s*([\d]+[.,][0-9]{2})")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            serp_text = driver.execute_script(
                "var s = document.getElementById('serp'); "
                "return s ? s.innerText : '';"
            )
            if serp_text:
                real = []
                for n in price_re.findall(serp_text):
                    p = parse_price(n)
                    if p and p > min_price:
                        real.append(p)
                if len(real) >= 3:
                    time.sleep(0.5)
                    return serp_text
        except Exception:
            pass
        time.sleep(0.8)

    logger.warning(f"  Timeout ({timeout}s) - keine Ergebnisse.")
    try:
        os.makedirs("debug", exist_ok=True)
        driver.save_screenshot(
            f"debug/timeout_{city_cfg['location']}_{datetime.now().strftime('%H%M%S')}.png"
        )
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────────────────────
# PREISEXTRAKTION - WIEN (€)
# ─────────────────────────────────────────────────────────────

def extract_wien(driver, serp_text: str) -> dict:
    """Extrahiert €-Preise via JS. Nimmt kleinsten Preis > 5 pro Karte."""
    result = driver.execute_script(r"""
        var out = {};
        var serp = document.getElementById('serp');
        if (!serp) return out;

        var allEls = serp.querySelectorAll('*');
        for (var i = 0; i < allEls.length; i++) {
            var el    = allEls[i];
            var text  = el.innerText || '';
            if (text.indexOf('\u20ac') === -1) continue;

            var pm = (text.match(/\u20ac\s*([\d]+[.,][\d]{2})/g) || []);
            var hasReal = false;
            for (var j = 0; j < pm.length; j++) {
                var n = parseFloat(pm[j].replace('\u20ac','').replace(',','.').trim());
                if (n > 5) { hasReal = true; break; }
            }
            if (!hasReal) continue;

            var nameEl = el.querySelector('h2,h3,h4,h5,strong');
            if (!nameEl) continue;
            var name = (nameEl.innerText || '').trim();
            if (name.length < 4 || name.indexOf('\u20ac') !== -1) continue;

            var lines = text.split('\n').filter(function(l){return l.trim().length>0;});
            if (lines.length > 25) continue;
            if (out[name] !== undefined) continue;

            var minP = 0;
            for (var k = 0; k < pm.length; k++) {
                var val = parseFloat(pm[k].replace('\u20ac','').replace(',','.').trim());
                if (val > 5 && (minP === 0 || val < minP)) minP = val;
            }
            if (minP > 0) out[name] = minP;
        }
        return out;
    """)

    if result and len(result) >= 3:
        return result

    logger.warning("  JS-Extraktion leer, nutze Text-Fallback (Wien)...")
    return _text_fallback(serp_text, currency_char="€", min_price=5.0,
                          price_re=r"€\s*([\d]+[.,]\d{2})")


# ─────────────────────────────────────────────────────────────
# PREISEXTRAKTION - ZÜRICH (CHF)
# ─────────────────────────────────────────────────────────────

def extract_zuerich(driver, serp_text: str) -> dict:
    """Extrahiert CHF-Preise via JS. Apostroph als Tausendertrenner wird behandelt."""
    result = driver.execute_script(r"""
        var out = {};
        var serp = document.getElementById('serp');
        if (!serp) return out;

        var allEls = serp.querySelectorAll('*');
        for (var i = 0; i < allEls.length; i++) {
            var el   = allEls[i];
            var text = el.innerText || '';
            if (text.indexOf('CHF') === -1) continue;

            var pm = (text.match(/CHF\s*([\d']+[.,]\d{2})/g) || []);
            var hasReal = false;
            for (var j = 0; j < pm.length; j++) {
                var n = parseFloat(
                    pm[j].replace('CHF','').replace(/'/g,'').replace(',','.').trim()
                );
                if (n > 10) { hasReal = true; break; }
            }
            if (!hasReal) continue;

            var nameEl = el.querySelector('h2,h3,h4,h5,strong');
            if (!nameEl) continue;
            var name = (nameEl.innerText || '').trim();
            if (name.length < 4 || name.indexOf('CHF') !== -1) continue;

            var lines = text.split('\n').filter(function(l){return l.trim().length>0;});
            if (lines.length > 25) continue;
            if (out[name] !== undefined) continue;

            var minP = 0;
            for (var k = 0; k < pm.length; k++) {
                var val = parseFloat(
                    pm[k].replace('CHF','').replace(/'/g,'').replace(',','.').trim()
                );
                if (val > 10 && (minP === 0 || val < minP)) minP = val;
            }
            if (minP > 0) out[name] = minP;
        }
        return out;
    """)

    if result and len(result) >= 3:
        return result

    logger.warning("  JS-Extraktion leer, nutze Text-Fallback (Zürich)...")
    return _text_fallback(serp_text, currency_char="CHF", min_price=10.0,
                          price_re=r"CHF\s*([\d']+[.,]\d{2})")


# ─────────────────────────────────────────────────────────────
# GEMEINSAMER TEXT-FALLBACK
# ─────────────────────────────────────────────────────────────

def _text_fallback(text: str, currency_char: str,
                   min_price: float, price_re: str) -> dict:
    """Generischer Text-Fallback: parst Name/Preis aus rohem innerText."""
    prices = {}
    SKIP = {"bewertung", "transfer", "zahlung", "stornierbar", "außenstell",
            "überdacht", "freifläche", "valetpark", "zu fuß", "view on",
            "billig", "nächst", "sortier", "suche", "filter", "empfohlen",
            "cookie", "accept", "buchen", "parktyp"}
    compiled = re.compile(price_re)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    current = None

    for line in lines:
        low = line.lower()
        if currency_char in line:
            m = compiled.search(line)
            if m:
                price = parse_price(m.group(1))
                if price and price > min_price and current:
                    if current not in prices:
                        prices[current] = price
                    current = None
        elif (5 < len(line) < 80
              and not any(w in low for w in SKIP)
              and not re.match(r"^[\d,.]", line)
              and currency_char not in line):
            current = line

    return prices


# ─────────────────────────────────────────────────────────────
# DISPATCHER
# ─────────────────────────────────────────────────────────────

def extract_prices(driver, city_cfg: dict) -> dict:
    """Wartet auf Blazor und ruft die stadtspezifische Extraktionsfunktion auf."""
    serp_text = wait_for_results(driver, city_cfg)
    if not serp_text:
        return {}
    time.sleep(0.5)
    if city_cfg["location"] == "VIE":
        return extract_wien(driver, serp_text)
    if city_cfg["location"] == "ZRH":
        return extract_zuerich(driver, serp_text)
    logger.error(f"Unbekannte Location: {city_cfg['location']}")
    return {}


# ─────────────────────────────────────────────────────────────
# SCRAPER PRO STADT
# ─────────────────────────────────────────────────────────────

def scrape_city(city_cfg: dict, headless: bool, dry_run: bool,
                spreadsheet=None, single_days: int = 0):
    """Scraped eine Stadt komplett (2–30 Tage je Anreisedatum) und schreibt ins Sheet."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    city_name = city_cfg["name"]
    currency = city_cfg["currency"]

    logger.info("\n" + "=" * 60)
    logger.info(f"  STADT: {city_name} ({city_cfg['location']})")
    logger.info(f"  Anreise in Tagen: {ANREISE_IN_TAGEN}")
    logger.info(f"  Modus: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("=" * 60)

    ws, hmap, ws_hist = None, {}, None
    if not dry_run:
        ws = spreadsheet.worksheet(city_cfg["sheet_name"])
        hmap = get_header_map(ws)
        ws_hist = get_history_sheet(spreadsheet)
        logger.info(f"  Spalten: {list(hmap.keys())}")

    days_range = [single_days] if single_days else range(2, MAX_DAYS + 1)
    driver = create_driver(headless=headless)
    cookie_done = False
    history_rows = []

    try:
        for idx, lead in enumerate(ANREISE_IN_TAGEN):
            dropoff = today + timedelta(days=lead)
            write_snapshot = (idx == 0)   # Blatt Wien/Zürich = nur erste Anreise
            logger.info(f"\n  ▶ Anreise {dropoff.strftime('%d.%m.%Y')} (in {lead} Tagen)")

            for days in days_range:
                pickup = dropoff + timedelta(days=days - 1)
                url = build_url(city_cfg["location"], dropoff, pickup)
                logger.info(f"  {days} Tage | Abholung: {pickup.strftime('%d.%m.%Y')}")

                driver.get(url)
                time.sleep(2.5)
                if not cookie_done:
                    accept_cookies(driver)
                    cookie_done = True
                    time.sleep(1)

                prices = extract_prices(driver, city_cfg)
                logger.info(f"  Preise: {len(prices)}")
                for n, p in sorted(prices.items()):
                    logger.info(f"    {n}: {currency} {p:.2f}")
                if not prices:
                    logger.warning(f"  KEINE PREISE fuer {days} Tage!")

                for n, p in prices.items():
                    history_rows.append([
                        stamp, city_name,
                        dropoff.strftime("%Y-%m-%d"), pickup.strftime("%Y-%m-%d"),
                        days, n, p, currency,
                    ])

                if not dry_run and prices and write_snapshot:
                    write_prices(ws, hmap, days, prices)

                time.sleep(1.5)

        if not dry_run:
            append_history(ws_hist, history_rows)
        else:
            logger.info(f"  [DRY RUN] {len(history_rows)} Verlaufszeilen (nicht geschrieben)")

    except Exception as e:
        logger.error(f"\n  {city_name} FEHLER: {e}", exc_info=True)
        # Bereits gesammelte Preise trotzdem sichern
        if not dry_run and history_rows:
            try:
                append_history(ws_hist, history_rows)
            except Exception:
                pass
        try:
            os.makedirs("debug", exist_ok=True)
            driver.save_screenshot(
                f"debug/fehler_{city_cfg['location']}_{datetime.now().strftime('%H%M%S')}.png"
            )
        except Exception:
            pass
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    logger.info(f"\n  {city_name}: Fertig!")


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ParkVia Multi-City Scraper (Wien + Zürich)")
    parser.add_argument("--city", choices=["wien", "zuerich", "alle"], default="alle",
                        help="Welche Stadt scrapen: wien | zuerich | alle (default: alle)")
    parser.add_argument("--no-headless", action="store_true",
                        help="Browser-Fenster anzeigen (zum Debuggen)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nichts ins Sheet schreiben, nur Preise ausgeben")
    parser.add_argument("--days", type=int, default=0,
                        help="Nur diese Tage-Anzahl testen (z.B. --days 2)")
    args = parser.parse_args()

    cities_to_run = list(CITIES.values()) if args.city == "alle" else [CITIES[args.city]]
    logger.info(f"ParkVia Scraper | Städte: {', '.join(c['name'] for c in cities_to_run)}")

    spreadsheet = None if args.dry_run else open_spreadsheet()

    failed = []
    for city_cfg in cities_to_run:
        try:
            scrape_city(
                city_cfg=city_cfg,
                headless=not args.no_headless,
                dry_run=args.dry_run,
                spreadsheet=spreadsheet,
                single_days=args.days,
            )
        except Exception:
            failed.append(city_cfg["name"])   # nächste Stadt trotzdem scrapen

    if failed:
        logger.error(f"\n=== Fehlgeschlagen: {', '.join(failed)} ===")
        raise SystemExit(1)
    logger.info("\n=== Alle Städte fertig ===")
