#!/usr/bin/env python3
"""
Skrypt do monitorowania ofert samochodowych na IAAI i Copart.
Wyszukuje oferty na podstawie marki i modelu, zapisuje nowe wyniki do pliku JSON.

Używa:
  - Copart JSON API (POST /public/lots/search-results)
  - IAAI HTML (GET /Search?Keyword=...) + BeautifulSoup
"""

import json
import os
import re
import warnings
from datetime import datetime
from typing import Dict, List, Optional, Set
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# Wycisz ostrzeżenia urllib3/LibreSSL
warnings.filterwarnings("ignore", message=".*urllib3.*")

# ── Konfiguracja ──────────────────────────────────────────────────────────────
SEARCH_MAKE = "volvo"
SEARCH_MODEL = "xc40"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATA_FILE = os.path.join(DATA_DIR, "listings.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30  # sekundy


# ── Zarządzanie danymi ────────────────────────────────────────────────────────
def load_existing_listings() -> List[Dict]:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_listings(listings: List[Dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(listings, f, indent=2, ensure_ascii=False)


def get_existing_ids(listings: List[Dict]) -> Set[str]:
    return {item["link"] for item in listings}


# ── IAAI (HTML scraping via requests) ─────────────────────────────────────────
def scrape_iaai(session: requests.Session) -> List[Dict]:
    """Pobiera oferty z IAAI.com przez HTTP GET + parsowanie HTML."""
    query = f"{SEARCH_MAKE} {SEARCH_MODEL}"
    url = f"https://www.iaai.com/Search?Keyword={quote_plus(query)}&CountPerPage=200"

    print(f"[IAAI] Pobieram: {url}")

    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        print(f"[IAAI] Błąd HTTP: {resp.status_code}")
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    cards = soup.select(".table-row.table-row-border")

    results = []
    for card in cards:
        item = _parse_iaai_card(card)
        if item:
            results.append(item)

    print(f"[IAAI] Znaleziono {len(results)} ofert.")
    return results


def _parse_iaai_card(card) -> Optional[Dict]:
    """Parsuje kartę pojazdu z IAAI HTML."""
    h4 = card.select_one("h4")
    if not h4:
        return None
    name = h4.get_text(strip=True)

    # Link
    link_tag = card.select_one("a[href*='VehicleDetail']")
    link = ""
    if link_tag and link_tag.get("href"):
        href = link_tag["href"]
        link = f"https://www.iaai.com{href}" if href.startswith("/") else href

    # Rok, marka, model
    parts = name.split()
    year = parts[0] if parts and parts[0].isdigit() else ""
    make_model = " ".join(parts[1:]) if year else name

    # Pola z data-list
    odometer = title_doc = acv = location = damage = vin = drive_status = keys = ""
    for span in card.select(".data-list__item span[title]"):
        t = span.get("title", "")
        txt = span.get_text(strip=True)
        if "Odometer" in t:
            odometer = txt
        elif "Title/Sale Doc" in t:
            title_doc = txt
        elif "ACV" in t or "Actual Cash" in t:
            acv = txt
        elif "Branch" in t:
            location = txt
        elif "Damage" in t or "Primary" in t:
            damage = txt
        elif "Please log in" in t and len(txt) > 6:
            vin = txt
        elif t.startswith("Start Code"):
            drive_status = _map_iaai_drive_status(txt)
        elif t.startswith("Key"):
            keys = txt

    img = card.select_one("img")
    image_url = ""
    if img:
        image_url = img.get("src", "") or img.get("data-src", "") or ""

    return {
        "source": "IAAI",
        "name": name,
        "year": year,
        "make_model": make_model,
        "vin": vin.replace("******", "***"),
        "odometer": odometer,
        "title_doc": title_doc,
        "damage": damage,
        "acv": acv,
        "location": location,
        "drive_status": drive_status,
        "keys": keys,
        "link": link,
        "image_url": image_url,
        "date_found": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _map_iaai_drive_status(txt: str) -> str:
    """Mapuje IAAI Start Code na czytelny status."""
    if not txt:
        return ""
    t = txt.lower()
    if "run" in t and "drive" in t:
        return "Run & Drive"
    if "start" in t and "drive" not in t:
        return "Starts"
    if "stationary" in t:
        return "Stationary"
    return txt.strip()


# ── Copart (JSON API) ────────────────────────────────────────────────────────
def scrape_copart(session: requests.Session) -> List[Dict]:
    """Pobiera oferty z Copart.com przez ich wewnętrzne JSON API."""
    query = f"{SEARCH_MAKE} {SEARCH_MODEL}"
    model_upper = SEARCH_MODEL.upper()
    api_url = "https://www.copart.com/public/lots/search-results"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://www.copart.com/lotSearchResults",
        "Origin": "https://www.copart.com",
    }

    payload = {
        "query": [query],
        "filter": {},
        "sort": ["auction_date_type desc", "auction_date_utc asc"],
        "page": 0,
        "size": 100,
        "start": 0,
        "watchListOnly": False,
        "freeFormSearch": True,
        "hideImages": False,
        "defaultSort": False,
        "specificRowProvided": False,
        "displayName": "",
        "searchName": "",
        "backUrl": "",
        "includeTagByField": {},
        "rawParams": {},
    }

    # Najpierw ustanów sesję (cookies)
    print("[Copart] Ustanawiam sesję...")
    session.get("https://www.copart.com/", timeout=REQUEST_TIMEOUT)

    results = []
    page = 0
    max_pages = 40  # bezpieczny limit

    print(f"[Copart] Pobieram oferty przez API...")

    while page < max_pages:
        payload["page"] = page
        payload["start"] = page * 100

        resp = session.post(api_url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"[Copart] Błąd HTTP: {resp.status_code}")
            break

        data = resp.json()
        if data.get("returnCode") != 1:
            print(f"[Copart] API error: {data.get('returnCodeDesc')}")
            break

        content = data.get("data", {}).get("results", {}).get("content", [])
        total = data.get("data", {}).get("results", {}).get("totalElements", 0)

        if not content:
            break

        # Filtruj tylko szukany model
        for lot in content:
            mmod = lot.get("mmod", "")
            ld = lot.get("ld", "")
            if model_upper in mmod.upper() or model_upper in ld.upper():
                item = _parse_copart_lot(lot)
                if item:
                    results.append(item)

        page_count = page + 1
        print(
            f"[Copart] Strona {page_count}: "
            f"pobrano {len(content)}, XC40: {len(results)} łącznie "
            f"(z {total} total)"
        )

        if len(content) < 100 or (page + 1) * 100 >= total:
            break
        page += 1

    print(f"[Copart] Znaleziono {len(results)} ofert {model_upper}.")
    return results


def _parse_copart_lot(lot: Dict) -> Optional[Dict]:
    """Parsuje obiekt lota z Copart API do ujednoliconego formatu."""
    ln = lot.get("ln", "")  # lot number
    ld = lot.get("ld", "")  # lot description (np. "2025 VOLVO XC40 CORE")

    if not ld:
        return None

    # Link
    link = f"https://www.copart.com/lot/{ln}"

    # Rok
    year = ""
    year_match = re.search(r"\b(19|20)\d{2}\b", ld)
    if year_match:
        year = year_match.group(0)

    make_model = ld.replace(year, "").strip() if year else ld

    # Odometer
    odometer = ""
    orr = lot.get("orr")
    if orr:
        odometer = f"{int(orr):,}" if isinstance(orr, float) else str(orr)
    ord_type = lot.get("ord", "")
    if ord_type:
        odometer += f" ({ord_type})"

    # Wartość
    acv = lot.get("la")
    est_value = f"${acv:,.0f}" if acv else ""

    # Repair cost
    rc = lot.get("rc")
    repair_cost = f"${rc:,.0f}" if rc else ""

    # Damage
    damage = lot.get("dd", "")

    # Title
    title_doc = lot.get("td", "")
    tgd = lot.get("tgd", "")
    if tgd and tgd not in title_doc:
        title_doc = tgd

    # Location
    location = lot.get("yn", "")

    # Bid
    hb = lot.get("hb", 0)
    bid = f"${hb:,.0f}" if hb else ""

    # Auction date (epoch ms -> date string)
    ad = lot.get("ad")
    auction_date = ""
    if ad:
        try:
            auction_date = datetime.fromtimestamp(ad / 1000).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

    # VIN
    vin = lot.get("fv", "").replace("******", "***")

    # Image
    image_url = lot.get("tims", "")

    # Engine
    engine = lot.get("egn", "")

    # Secondary damage
    secondary_damage = lot.get("sdd", "")

    # Drive / condition status
    lcd = lot.get("lcd", "")  # np. "ENHANCED VEHICLES", "RUN AND DRIVE"
    drive_status = _map_copart_drive_status(lcd)

    # Keys
    keys = lot.get("hk", "")  # "YES" / "NO" / ""

    # Buy now price
    bnp = lot.get("bnp", 0)
    buy_now = f"${bnp:,.0f}" if bnp and bnp > 0 else ""

    return {
        "source": "Copart",
        "name": ld,
        "year": year,
        "make_model": make_model,
        "vin": vin,
        "lot_number": str(ln),
        "odometer": odometer,
        "est_value": est_value,
        "repair_cost": repair_cost,
        "title_doc": title_doc,
        "damage": damage,
        "secondary_damage": secondary_damage,
        "bid": bid,
        "buy_now": buy_now,
        "auction_date": auction_date,
        "location": location,
        "engine": engine,
        "drive_status": drive_status,
        "keys": keys,
        "link": link,
        "image_url": image_url,
        "date_found": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _map_copart_drive_status(lcd: str) -> str:
    """Mapuje Copart lcd (lot condition description) na czytelny status."""
    if not lcd:
        return ""
    lcd_upper = lcd.upper()
    if "RUN" in lcd_upper and "DRIVE" in lcd_upper:
        return "Run & Drive"
    if "ENHANCED" in lcd_upper:
        return "Enhanced"
    if "ENGINE START" in lcd_upper or "STARTS" in lcd_upper:
        return "Starts"
    if "STATIONARY" in lcd_upper:
        return "Stationary"
    # Zwróć oryginalną wartość w title case jeśli nieznana
    return lcd.strip().title()


# ── Główna logika ─────────────────────────────────────────────────────────────
def run_scraper():
    """Uruchamia scraper i zapisuje nowe oferty."""
    print("=" * 60)
    print(f"  Szukam: {SEARCH_MAKE.upper()} {SEARCH_MODEL.upper()}")
    print(f"  Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    existing = load_existing_listings()
    existing_ids = get_existing_ids(existing)
    print(f"\nZapisanych ofert w bazie: {len(existing)}")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    new_listings = []

    # ── IAAI ──
    print("\n" + "─" * 40)
    print("  IAAI.com")
    print("─" * 40)
    try:
        iaai_results = scrape_iaai(session)
        for item in iaai_results:
            if item["link"] and item["link"] not in existing_ids:
                new_listings.append(item)
                existing_ids.add(item["link"])
    except Exception as e:
        print(f"[IAAI] Błąd: {e}")

    # ── Copart ──
    print("\n" + "─" * 40)
    print("  Copart.com")
    print("─" * 40)
    try:
        copart_results = scrape_copart(session)
        for item in copart_results:
            if item["link"] and item["link"] not in existing_ids:
                new_listings.append(item)
                existing_ids.add(item["link"])
    except Exception as e:
        print(f"[Copart] Błąd: {e}")

    # ── Podsumowanie ──
    print("\n" + "=" * 60)
    print("  PODSUMOWANIE")
    print("=" * 60)

    if new_listings:
        print(f"\n  Nowe oferty: {len(new_listings)}\n")
        for i, item in enumerate(new_listings, 1):
            src = item["source"]
            print(f"  {i}. [{src}] {item['name']}")
            if item.get("damage"):
                print(f"     Uszkodzenie: {item['damage']}")
            if item.get("acv"):
                print(f"     Wartość: {item['acv']}")
            if item.get("est_value"):
                print(f"     Wartość: {item['est_value']}")
            if item.get("bid"):
                print(f"     Aktualna oferta: {item['bid']}")
            if item.get("location"):
                print(f"     Lokalizacja: {item['location']}")
            print(f"     Link: {item['link']}")
            print()

        all_listings = existing + new_listings
        save_listings(all_listings)
        print(f"  Zapisano do: {DATA_FILE}")
        print(f"  Łącznie ofert w bazie: {len(all_listings)}")
    else:
        print("\n  Brak nowych ofert.")
        print(f"  Łącznie ofert w bazie: {len(existing)}")

    print()
    return new_listings


if __name__ == "__main__":
    run_scraper()
