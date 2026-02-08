#!/usr/bin/env python3
"""
Panel WWW do przeglądania ofert samochodowych z IAAI i Copart.
Uruchom: python3 app.py
Otwórz: http://localhost:8080
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import requests as http_requests
from flask import Flask, jsonify, render_template_string, request

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
DATA_FILE = os.path.join(DATA_DIR, "listings.json")
IS_VERCEL = bool(os.environ.get("VERCEL"))

app = Flask(__name__)

# ── Kurs walutowy USD/PLN ─────────────────────────────────────────────────────

_usd_pln_cache = {"rate": None, "fetched_at": 0}
_CACHE_TTL = 3600  # odświeżaj kurs co 1h


def _get_usd_pln_rate():
    """Pobiera aktualny kurs USD/PLN z API NBP. Cache na 1h."""
    now = time.time()
    if _usd_pln_cache["rate"] and now - _usd_pln_cache["fetched_at"] < _CACHE_TTL:
        return _usd_pln_cache["rate"]

    try:
        resp = http_requests.get(
            "https://api.nbp.pl/api/exchangerates/rates/a/usd/?format=json",
            timeout=5,
        )
        if resp.status_code == 200:
            rate = resp.json()["rates"][0]["mid"]
            _usd_pln_cache["rate"] = rate
            _usd_pln_cache["fetched_at"] = now
            print(f"[Kurs] USD/PLN = {rate}")
            return rate
    except Exception as e:
        print(f"[Kurs] Błąd pobierania kursu NBP: {e}")

    # Fallback jeśli API niedostępne
    if _usd_pln_cache["rate"]:
        return _usd_pln_cache["rate"]
    return 4.10  # awaryjny kurs


def _usd_to_pln(usd_str):
    """Konwertuje string z ceną USD na PLN. Np. '$27,541 USD' -> '112,918 zł'"""
    if not usd_str:
        return ""
    m = re.search(r"-?[\d,]+", usd_str)
    if not m:
        return ""
    try:
        usd = float(m.group(0).replace(",", ""))
        if usd <= 0:
            return ""
        rate = _get_usd_pln_rate()
        pln = int(usd * rate)
        return f"{pln:,} zł"
    except (ValueError, TypeError):
        return ""


# ── Dane ──────────────────────────────────────────────────────────────────────

def load_listings():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _miles_to_km(odo_str):
    """Konwertuje string z milami na kilometry."""
    if not odo_str:
        return ""
    m = re.search(r"[\d,]+", odo_str)
    if not m:
        return odo_str
    try:
        miles = int(m.group(0).replace(",", ""))
        km = int(miles * 1.60934)
        return f"{km:,} km"
    except ValueError:
        return odo_str


_DAMAGE_PL = {
    "all over": "Cały pojazd",
    "biohazard": "Zagrożenie biologiczne",
    "burn": "Spalony",
    "burn - Loss": "Spalony",
    "electrical": "Elektryczne",
    "front & rear": "Przód i tył",
    "front end": "Przód",
    "left & right side": "Lewa i prawa strona",
    "left front": "Lewy przód",
    "left rear": "Lewy tył",
    "left side": "Lewa strona",
    "mechanical": "Mechaniczne",
    "minor dent/scratches": "Drobne wgniecenia/rysy",
    "minor dents/scratches": "Drobne wgniecenia/rysy",
    "normal wear": "Normalne zużycie",
    "none": "Brak",
    "rear": "Tył",
    "rear end": "Tył",
    "right front": "Prawy przód",
    "right rear": "Prawy tył",
    "right side": "Prawa strona",
    "rollover": "Dachowanie",
    "side": "Bok",
    "suspension": "Zawieszenie",
    "top/roof": "Dach",
    "undercarriage": "Podwozie",
    "vandalism": "Wandalizm",
    "water/flood": "Zalanie",
    "hail": "Grad",
    "replaced": "Wymieniony",
    "unknown": "Nieznane",
    "missing/altered vin": "Brak/zmieniony VIN",
    "stripped": "Ogołocony",
    "partial repair": "Częściowa naprawa",
}


def _translate_damage(dmg):
    """Tłumaczy typ uszkodzenia z angielskiego na polski."""
    if not dmg:
        return ""
    return _DAMAGE_PL.get(dmg.lower().strip(), dmg)


def normalize(item):
    """Ujednolica pola IAAI i Copart do jednego formatu."""
    price_usd = item.get("acv", "") or item.get("est_value", "") or ""
    bid_usd = item.get("bid", "") or ""
    repair = item.get("repair_cost", "") or ""
    auction = item.get("auction_date", "") or ""
    buy_now_usd = item.get("buy_now", "") or ""

    return {
        "source": item.get("source", ""),
        "name": item.get("name", "").strip(),
        "year": item.get("year", ""),
        "make_model": item.get("make_model", "").strip(),
        "vin": item.get("vin", ""),
        "odometer": _miles_to_km(item.get("odometer", "")),
        "price": _usd_to_pln(price_usd),
        "bid": _usd_to_pln(bid_usd),
        "buy_now": _usd_to_pln(buy_now_usd),
        "repair_cost": repair,
        "title_doc": item.get("title_doc", ""),
        "damage": _translate_damage(item.get("damage", "")),
        "drive_status": item.get("drive_status", ""),
        "keys": item.get("keys", ""),
        "auction_date": auction,
        "link": item.get("link", ""),
        "image_url": item.get("image_url", ""),
        "date_found": item.get("date_found", ""),
    }


# ── Trasy ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/listings")
def api_listings():
    data = load_listings()
    normalized = [normalize(item) for item in data]
    return jsonify(normalized)


_REFRESH_COOLDOWN = 3600  # 1 godzina w sekundach
_last_refresh_ts = 0.0


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Uruchamia scraper w tle i zwraca status."""
    global _last_refresh_ts

    if IS_VERCEL:
        return jsonify({
            "status": "skipped",
            "message": "Dane aktualizowane automatycznie co 6h. Nastepny skan wkrotce."
        })

    now = time.time()
    elapsed = now - _last_refresh_ts
    if elapsed < _REFRESH_COOLDOWN:
        remaining = int(_REFRESH_COOLDOWN - elapsed)
        mins = remaining // 60
        secs = remaining % 60
        return jsonify({
            "status": "cooldown",
            "remaining": remaining,
            "message": f"Poczekaj jeszcze {mins}m {secs:02d}s do nastepnego skanowania."
        })

    _last_refresh_ts = now

    def run_scraper():
        scraper_path = os.path.join(_PROJECT_ROOT, "scraper.py")
        subprocess.run([sys.executable, scraper_path], capture_output=True, text=True)

    t = threading.Thread(target=run_scraper, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "Scraper uruchomiony w tle..."})


@app.route("/api/stats")
def api_stats():
    data = load_listings()
    iaai = [x for x in data if x.get("source") == "IAAI"]
    copart = [x for x in data if x.get("source") == "Copart"]

    dates = [x.get("date_found", "") for x in data if x.get("date_found")]
    last_scan = max(dates) if dates else "nigdy"

    rate = _get_usd_pln_rate()

    return jsonify({
        "total": len(data),
        "iaai": len(iaai),
        "copart": len(copart),
        "last_scan": last_scan,
        "usd_pln": round(rate, 2),
    })


# ── HTML Template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Wyszukiwarka Andrzeja</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Roboto+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --font: 'Space Grotesk', -apple-system, BlinkMacSystemFont, sans-serif;
            --bg-base: #0c0c0e;
            --bg-raised: #141416;
            --bg-surface: #1a1a1e;
            --bg-hover: #222228;
            --border: rgba(255,255,255,0.06);
            --border-strong: rgba(255,255,255,0.10);
            --text-1: #f0f0f2;
            --text-2: #9494a0;
            --text-3: #6e6e7e;
            --accent: #c9a55a;
            --accent-dim: rgba(201,165,90,0.12);
            --green: #6bcf7f;
            --green-dim: rgba(107,207,127,0.10);
            --yellow: #e0b44a;
            --yellow-dim: rgba(224,180,74,0.10);
            --red: #e06060;
            --red-dim: rgba(224,96,96,0.10);
            --blue: #6ba3cf;
            --blue-dim: rgba(107,163,207,0.10);
            --purple: #a78bda;
            --purple-dim: rgba(167,139,218,0.10);
            --r: 8px;
            --r-sm: 5px;
            --shadow: 0 12px 40px rgba(0,0,0,0.55);
            --ease: cubic-bezier(0.16, 1, 0.3, 1);
        }
        *, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
        html { overflow-x:hidden; }
        ::selection { background: var(--accent); color: var(--bg-base); }

        body {
            font-family: var(--font); background: var(--bg-base); color: var(--text-1);
            min-height: 100vh; -webkit-font-smoothing: antialiased;
            overflow-x:hidden; max-width:100vw;
            opacity:0; animation: fadeIn .6s var(--ease) .1s forwards;
        }
        @keyframes fadeIn { to { opacity:1; } }

        /* ── Header ── */
        .header { padding: 32px 24px 0; }
        .header-top {
            display:flex; justify-content:space-between; align-items:flex-end;
            margin-bottom: 32px;
        }
        .header h1 {
            font-size:32px; font-weight:700; letter-spacing:-0.03em; line-height:1;
        }
        .header h1 span { color: var(--accent); font-weight:400; }
        .header-meta {
            font-size:13px; color: var(--text-3); text-align:right; line-height:1.6;
        }
        .header-meta strong { color: var(--text-2); font-weight:500; }

        /* ── Stat cards ── */
        .stats {
            display:grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr));
            gap:12px; margin-bottom:32px;
        }
        .stat-card {
            background: var(--bg-raised); border:1px solid var(--border);
            border-radius: var(--r); padding:20px 24px;
            transition: border-color .3s var(--ease);
        }
        .stat-card:hover { border-color: var(--border-strong); }
        .stat-label {
            font-size:11px; font-weight:500; text-transform:uppercase;
            letter-spacing:.08em; color: var(--text-3); margin-bottom:8px;
        }
        .stat-value {
            font-size:28px; font-weight:700; letter-spacing:-0.02em; line-height:1;
        }
        .stat-value.accent { color: var(--accent); }

        /* ── Toolbar ── */
        .toolbar {
            padding:0 24px; margin-bottom:20px;
            display:flex; gap:10px; flex-wrap:wrap; align-items:center;
        }
        .toolbar input, .toolbar select {
            font-family: var(--font); background: var(--bg-raised);
            border:1px solid var(--border); color: var(--text-1);
            padding:10px 14px; border-radius: var(--r-sm); font-size:13px;
            outline:none; transition: border-color .25s var(--ease), background .25s var(--ease);
        }
        .toolbar input:focus, .toolbar select:focus {
            border-color: var(--accent); background: var(--bg-surface);
        }
        .toolbar input::placeholder { color: var(--text-3); }
        .toolbar input[type="text"] { width:220px; }
        .toolbar select { min-width:130px; cursor:pointer; }
        .toolbar select option { background: var(--bg-surface); }

        .btn {
            font-family: var(--font); background: var(--accent); color: var(--bg-base);
            border:none; padding:10px 20px; border-radius: var(--r-sm);
            font-size:13px; font-weight:600; cursor:pointer;
            transition: transform .25s var(--ease), opacity .25s;
        }
        .btn:hover { transform:translateY(-1px); }
        .btn:active { transform:translateY(0); }
        .btn:disabled { opacity:.4; cursor:not-allowed; transform:none; }
        .btn-outline {
            background:transparent; border:1px solid var(--border-strong);
            color: var(--text-2); font-weight:500;
        }
        .btn-outline:hover { background: var(--bg-surface); color: var(--text-1); }

        /* ── Multi-select ── */
        .multi-select { position:relative; }
        .multi-select-btn {
            font-family: var(--font); background: var(--bg-raised);
            border:1px solid var(--border); color: var(--text-1);
            padding:10px 14px; border-radius: var(--r-sm); font-size:13px;
            cursor:pointer; min-width:170px; text-align:left;
            transition: border-color .25s var(--ease);
        }
        .multi-select-btn:hover, .multi-select-btn:focus { border-color: var(--accent); outline:none; }
        .multi-select-btn.active-filter { border-color: var(--accent); color: var(--accent); }
        .multi-select-dropdown {
            display:none; position:absolute; top:calc(100% + 6px); left:0;
            background: var(--bg-surface); border:1px solid var(--border-strong);
            border-radius: var(--r); min-width:240px; max-height:340px;
            overflow-y:auto; z-index:100; box-shadow: var(--shadow); padding:6px 0;
        }
        .multi-select-dropdown.open { display:block; }
        .multi-select-item {
            display:flex; align-items:center; gap:10px;
            padding:8px 16px; font-size:13px; cursor:pointer;
            color: var(--text-2); transition: all .15s;
        }
        .multi-select-item:hover { background: var(--bg-hover); color: var(--text-1); }
        .multi-select-all { border-bottom:1px solid var(--border); padding-bottom:10px; margin-bottom:4px; }
        .multi-select-item input[type="checkbox"] {
            accent-color: var(--accent); width:15px; height:15px; cursor:pointer;
        }

        .spacer { flex:1; }
        .result-count { font-size:12px; color: var(--text-3); font-weight:500; letter-spacing:.02em; }

        /* ── Table ── */
        .table-wrap { overflow-x:auto; padding:0 24px 48px; }
        table { width:100%; border-collapse:collapse; font-size:12px; table-layout:auto; }
        thead th {
            background: var(--bg-base); color: var(--text-3); font-weight:500;
            text-transform:uppercase; font-size:9px; letter-spacing:.1em;
            padding:10px 8px 8px; text-align:left;
            border-bottom:1px solid var(--border-strong);
            position:sticky; top:0; cursor:pointer; user-select:none;
            white-space:nowrap; z-index:10;
        }
        thead th:hover { color: var(--accent); }
        thead th .arrow { font-size:8px; margin-left:3px; opacity:.3; }
        thead th.sorted .arrow { opacity:1; color: var(--accent); }
        tbody tr {
            border-bottom:1px solid var(--border);
            transition: background .2s var(--ease);
        }
        tbody tr:hover { background: var(--bg-raised); }
        td { padding:10px 8px; vertical-align:middle; }
        td.num, th.num { text-align:right; }

        /* ── Miniatura ── */
        .thumb {
            width:72px; height:48px; object-fit:cover;
            border-radius: var(--r-sm); background: var(--bg-surface); display:block;
            transition: transform .3s var(--ease);
        }
        tr:hover .thumb { transform:scale(1.05); }
        .no-img {
            width:72px; height:48px; border-radius: var(--r-sm);
            background: var(--bg-surface); display:flex; align-items:center;
            justify-content:center; font-size:9px; color: var(--text-3);
            letter-spacing:.05em; text-transform:uppercase;
        }

        /* ── Tags & badges ── */
        .badge {
            display:inline-block; padding:3px 10px; border-radius:20px;
            font-size:10px; font-weight:600; letter-spacing:.04em; text-transform:uppercase;
        }
        .badge-iaai { background: var(--blue-dim); color: var(--blue); }
        .badge-copart { background: var(--purple-dim); color: var(--purple); }

        .damage-tag {
            display:inline-block; padding:3px 10px; border-radius: var(--r-sm);
            font-size:11px; font-weight:500; background: var(--red-dim); color: var(--red);
        }
        .damage-tag.minor { background: var(--green-dim); color: var(--green); }
        .damage-tag.none { background: var(--green-dim); color: var(--green); }

        .title-tag { font-size:11px; color: var(--text-3); font-weight:500; }
        .title-tag.salvage { color: var(--red); }
        .title-tag.clean { color: var(--green); }
        .title-tag.rebuilt { color: var(--yellow); }

        .status-tag {
            display:inline-block; padding:3px 10px; border-radius: var(--r-sm);
            font-size:11px; font-weight:600; white-space:nowrap;
        }
        .status-tag.run { background: var(--green-dim); color: var(--green); }
        .status-tag.enhanced { background: var(--green-dim); color: var(--green); }
        .status-tag.starts { background: var(--yellow-dim); color: var(--yellow); }
        .status-tag.stationary { background: var(--red-dim); color: var(--red); }

        .price-val { font-weight:600; color: var(--green); white-space:nowrap; font-variant-numeric:tabular-nums; }
        .bid-val { color: var(--yellow); white-space:nowrap; font-weight:500; font-variant-numeric:tabular-nums; }
        .buy-val { font-weight:700; color: var(--accent); white-space:nowrap; font-variant-numeric:tabular-nums; }
        .odo-val { white-space:nowrap; color: var(--text-2); font-variant-numeric:tabular-nums; }
        .year-val { font-weight:600; font-size:14px; }
        .date-val { color: var(--text-3); font-size:11px; white-space:nowrap; }
        .vehicle-name { font-weight:500; max-width:200px; }
        .vehicle-name a { display:inline-block; max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .empty { color: var(--text-3); }

        a.vehicle-link {
            color: var(--text-1); text-decoration:none; font-weight:500;
            transition: color .2s var(--ease);
        }
        a.vehicle-link:hover { color: var(--accent); }
        a.vin-link {
            color: var(--text-2); text-decoration:none; font-family:'Roboto Mono',monospace;
            font-size:11px; letter-spacing:.03em; transition: color .2s var(--ease);
            white-space:nowrap;
        }
        a.vin-link:hover { color: var(--accent); text-decoration:underline; }
        .auction-val { white-space:nowrap; color: var(--text-2); font-size:12px; }

        /* ── Loading / Empty states ── */
        .loading-bar {
            position:fixed; top:0; left:0; width:100%; height:3px; z-index:10000;
            background: var(--bg-raised); overflow:hidden;
        }
        .loading-bar::after {
            content:''; display:block; width:30%; height:100%;
            background: var(--accent); border-radius:2px;
            animation: loadSlide 1.2s ease-in-out infinite;
        }
        .loading-bar.hide { display:none; }
        @keyframes loadSlide {
            0% { transform:translateX(-100%); }
            100% { transform:translateX(400%); }
        }
        .empty-state {
            text-align:center; padding:64px 24px; color: var(--text-3);
        }
        .empty-state-icon { font-size:48px; margin-bottom:16px; opacity:.4; }
        .empty-state-text { font-size:15px; font-weight:500; margin-bottom:8px; color: var(--text-2); }
        .empty-state-sub { font-size:13px; }

        /* ── Active filter indicator ── */
        .toolbar select.active-filter {
            border-color: var(--accent); color: var(--accent);
        }

        /* ── Reset filters ── */
        .btn-reset {
            background:transparent; border:1px solid var(--border);
            color: var(--text-3); font-size:11px; font-weight:500; padding:8px 14px;
            border-radius: var(--r-sm); cursor:pointer; font-family: var(--font);
            transition: all .2s var(--ease); display:none;
        }
        .btn-reset.visible { display:inline-flex; align-items:center; gap:5px; }
        .btn-reset:hover { border-color: var(--red); color: var(--red); }

        /* ── Scroll hint for table on mobile ── */
        .table-scroll-hint {
            display:none; text-align:center; padding:8px;
            font-size:11px; color: var(--text-3); letter-spacing:.04em;
        }

        /* ── Focus states (WCAG) ── */
        *:focus-visible {
            outline:2px solid var(--accent); outline-offset:2px;
        }
        .toolbar input:focus-visible, .toolbar select:focus-visible, .multi-select-btn:focus-visible {
            outline:2px solid var(--accent); outline-offset:1px;
        }

        /* ── Responsive: tablet ── */
        @media (max-width:1024px) {
            .header { padding:24px 16px 0; }
            .toolbar { padding:0 16px; }
            .table-wrap { padding:0 12px 32px; }
            .stat-card { padding:14px 16px; }
            .stat-value { font-size:22px; }
        }

        /* ── Responsive: mobile ── */
        @media (max-width:768px) {
            .header { padding:20px 16px 0; }
            .header-top { flex-direction:column; align-items:flex-start; gap:8px; }
            .header h1 { font-size:24px; }
            .header-meta { text-align:left; font-size:12px; }
            .stats { grid-template-columns: repeat(3,1fr); gap:8px; margin-bottom:20px; }
            .stat-card { padding:12px 14px; }
            .stat-label { font-size:9px; margin-bottom:4px; }
            .stat-value { font-size:20px; }

            .toolbar {
                padding:0 16px; gap:8px; margin-bottom:16px;
            }
            .toolbar input, .toolbar select, .multi-select-btn {
                padding:12px 14px; font-size:14px; min-height:44px;
            }
            .toolbar input[type="text"] { width:100%; }
            .toolbar select { min-width:0; flex:1; }
            .btn, .btn-outline, .btn-reset { min-height:44px; padding:12px 16px; font-size:14px; }

            .table-scroll-hint { display:block; }
            .table-wrap { padding:0 0 32px; overflow-x:auto; -webkit-overflow-scrolling:touch; }
            table { min-width:800px; font-size:11px; }
            td, th { padding:8px 6px; }
            .thumb, .no-img { width:48px; height:34px; }
            .badge { font-size:9px; padding:2px 7px; }
            .damage-tag, .status-tag { font-size:10px; padding:2px 7px; }
            a.vin-link { font-size:10px; }
        }

        /* ── Responsive: small mobile ── */
        @media (max-width:480px) {
            .stats { grid-template-columns: repeat(3,1fr); gap:6px; }
            .stat-card { padding:10px 10px; }
            .stat-value { font-size:18px; }
            .stat-label { font-size:8px; letter-spacing:.06em; }
            .toolbar { gap:6px; }
            .toolbar select { flex:1 1 45%; min-width:0; }
        }

        /* ── Lightbox ── */
        .lightbox {
            position:fixed; inset:0; z-index:9999;
            background:rgba(0,0,0,.85); backdrop-filter:blur(8px);
            display:flex; align-items:center; justify-content:center;
            opacity:0; visibility:hidden;
            transition: opacity .3s var(--ease), visibility .3s;
            cursor:zoom-out;
        }
        .lightbox.open { opacity:1; visibility:visible; }
        .lightbox img {
            max-width:90vw; max-height:85vh; object-fit:contain;
            border-radius: var(--r); box-shadow: 0 24px 80px rgba(0,0,0,.6);
            transform:scale(.92); transition: transform .35s var(--ease);
        }
        .lightbox.open img { transform:scale(1); }
        .lightbox-close {
            position:absolute; top:24px; right:28px;
            width:40px; height:40px; border:none; border-radius:50%;
            background:rgba(255,255,255,.12); color:#fff; font-size:20px;
            cursor:pointer; display:flex; align-items:center; justify-content:center;
            transition: background .2s;
        }
        .lightbox-close:hover { background:rgba(255,255,255,.25); }
        .lightbox-info {
            position:absolute; bottom:28px; left:50%; transform:translateX(-50%);
            color:rgba(255,255,255,.5); font-size:12px; letter-spacing:.04em;
        }

        .thumb { cursor:zoom-in; }

        .toast {
            position:fixed; bottom:32px; right:32px;
            background: var(--bg-surface); color: var(--text-1);
            border:1px solid var(--border-strong);
            padding:14px 24px; border-radius: var(--r);
            font-size:13px; font-weight:500; box-shadow: var(--shadow);
            opacity:0; transform:translateY(12px);
            transition: all .4s var(--ease); z-index:1000;
        }
        .toast.show { opacity:1; transform:translateY(0); }

        ::-webkit-scrollbar { width:6px; height:6px; }
        ::-webkit-scrollbar-track { background:transparent; }
        ::-webkit-scrollbar-thumb { background: var(--text-3); border-radius:3px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--text-2); }
    </style>
</head>
<body>

<div id="loading-bar" class="loading-bar"></div>

<div class="header">
    <div class="header-top">
        <h1>Wyszukiwarka <span>Andrzeja</span></h1>
        <div class="header-meta">
            Kurs <strong id="stat-rate">&mdash;</strong> PLN/USD<br>
            Skan: <strong id="stat-scan">&mdash;</strong>
        </div>
    </div>
    <div class="stats">
        <div class="stat-card">
            <div class="stat-label">IAAI</div>
            <div class="stat-value" id="stat-iaai">&mdash;</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Copart</div>
            <div class="stat-value" id="stat-copart">&mdash;</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Razem</div>
            <div class="stat-value accent" id="stat-total">&mdash;</div>
        </div>
    </div>
</div>

<div class="toolbar">
    <input type="text" id="search" placeholder="Szukaj...">
    <select id="filter-source">
        <option value="">Wszystkie</option>
        <option value="IAAI">IAAI</option>
        <option value="Copart">Copart</option>
    </select>
    <div class="multi-select" id="filter-damage-wrap">
        <button class="multi-select-btn" id="filter-damage-btn" type="button">Uszkodzenia &#9662;</button>
        <div class="multi-select-dropdown" id="filter-damage-dropdown">
            <label class="multi-select-item multi-select-all">
                <input type="checkbox" id="dmg-select-all" checked> <strong>Wszystkie</strong>
            </label>
            <div id="filter-damage-list"></div>
        </div>
    </div>
    <select id="filter-status">
        <option value="">Status</option>
    </select>
    <select id="filter-year-from">
        <option value="">Od roku</option>
    </select>
    <select id="filter-year-to">
        <option value="">Do roku</option>
    </select>
    <select id="filter-odo-from">
        <option value="">Przebieg od</option>
        <option value="0">0 km</option>
        <option value="50000">50 tys. km</option>
        <option value="100000">100 tys. km</option>
        <option value="150000">150 tys. km</option>
        <option value="200000">200 tys. km</option>
        <option value="250000">250 tys. km</option>
    </select>
    <select id="filter-odo-to">
        <option value="">Przebieg do</option>
        <option value="50000">50 tys. km</option>
        <option value="100000">100 tys. km</option>
        <option value="150000">150 tys. km</option>
        <option value="200000">200 tys. km</option>
        <option value="250000">250 tys. km</option>
    </select>
    <button class="btn-reset" id="btn-reset" onclick="resetFilters()" title="Wyczysc wszystkie filtry">&#10005; Reset</button>
    <div class="spacer"></div>
    <span class="result-count" id="result-count"></span>
    <button class="btn" id="btn-refresh" onclick="refreshData()">Skanuj</button>
</div>

<div class="table-scroll-hint">&#8592; przewin tabele &#8594;</div>
<div class="table-wrap">
    <table>
        <thead>
            <tr>
                <th></th>
                <th data-sort="source">Zrodlo <span class="arrow">&#9650;</span></th>
                <th data-sort="year">Rok <span class="arrow">&#9650;</span></th>
                <th data-sort="name">Pojazd <span class="arrow">&#9650;</span></th>
                <th data-sort="vin">VIN <span class="arrow">&#9650;</span></th>
                <th data-sort="damage">Uszkodzenie <span class="arrow">&#9650;</span></th>
                <th data-sort="drive_status">Status <span class="arrow">&#9650;</span></th>
                <th data-sort="title_doc">Tytul <span class="arrow">&#9650;</span></th>
                <th data-sort="price" class="num">Wartosc <span class="arrow">&#9650;</span></th>
                <th data-sort="bid" class="num">Oferta <span class="arrow">&#9650;</span></th>
                <th data-sort="buy_now" class="num">Kup teraz <span class="arrow">&#9650;</span></th>
                <th data-sort="odometer" class="num">Przebieg <span class="arrow">&#9650;</span></th>
                <th data-sort="auction_date">Aukcja <span class="arrow">&#9650;</span></th>
                <th data-sort="date_found">Dodano <span class="arrow">&#9650;</span></th>
            </tr>
        </thead>
        <tbody id="table-body"></tbody>
    </table>
</div>

<div class="toast" id="toast"></div>

<div id="lightbox" class="lightbox">
    <button id="lightbox-close" class="lightbox-close" aria-label="Zamknij">&times;</button>
    <img id="lightbox-img" src="" alt="">
    <div id="lightbox-info" class="lightbox-info"></div>
</div>

<script>
let allData = [];
let sortCol = 'date_found';
let sortAsc = false;

async function loadData() {
    document.getElementById('loading-bar').classList.remove('hide');
    try {
        const [listRes, statRes] = await Promise.all([fetch('/api/listings'), fetch('/api/stats')]);
        allData = await listRes.json();
        const s = await statRes.json();
        document.getElementById('stat-iaai').textContent = s.iaai;
        document.getElementById('stat-copart').textContent = s.copart;
        document.getElementById('stat-total').textContent = s.total;
        document.getElementById('stat-rate').textContent = s.usd_pln;
        document.getElementById('stat-scan').textContent = s.last_scan;
        populateFilters();
        renderTable();
    } catch(e) {
        showToast('Blad ladowania danych. Sprobuj odswiezyc strone.');
    } finally {
        document.getElementById('loading-bar').classList.add('hide');
    }
}

let selectedDamages = new Set();

function populateFilters() {
    const damages = [...new Set(allData.map(d => d.damage).filter(Boolean))].sort();
    const statuses = [...new Set(allData.map(d => d.drive_status).filter(Boolean))].sort();
    const years = [...new Set(allData.map(d => d.year).filter(Boolean))].sort();

    const dl = document.getElementById('filter-damage-list');
    dl.innerHTML = '';
    damages.forEach(d => {
        const lb = document.createElement('label'); lb.className = 'multi-select-item';
        const cb = document.createElement('input');
        cb.type = 'checkbox'; cb.checked = true; cb.value = d;
        cb.addEventListener('change', onDmgChange);
        const sp = document.createElement('span'); sp.textContent = d;
        lb.appendChild(cb); lb.appendChild(sp); dl.appendChild(lb);
    });
    selectedDamages = new Set();

    const ss = document.getElementById('filter-status');
    ss.innerHTML = '<option value="">Status</option>';
    statuses.forEach(v => { const o = document.createElement('option'); o.value=v; o.textContent=v; ss.appendChild(o); });

    const yf = document.getElementById('filter-year-from');
    yf.innerHTML = '<option value="">Od roku</option>';
    years.forEach(v => { const o = document.createElement('option'); o.value=v; o.textContent=v; yf.appendChild(o); });

    const yt = document.getElementById('filter-year-to');
    yt.innerHTML = '<option value="">Do roku</option>';
    [...years].reverse().forEach(v => { const o = document.createElement('option'); o.value=v; o.textContent=v; yt.appendChild(o); });
}

function getFiltered() {
    const q = document.getElementById('search').value.toLowerCase();
    const src = document.getElementById('filter-source').value;
    const st = document.getElementById('filter-status').value;
    const yF = document.getElementById('filter-year-from').value;
    const yT = document.getElementById('filter-year-to').value;
    const odoFrom = document.getElementById('filter-odo-from').value;
    const odoTo = document.getElementById('filter-odo-to').value;
    return allData.filter(d => {
        if (src && d.source !== src) return false;
        if (selectedDamages.size > 0 && !selectedDamages.has(d.damage)) return false;
        if (st && d.drive_status !== st) return false;
        if (yF && parseInt(d.year) < parseInt(yF)) return false;
        if (yT && parseInt(d.year) > parseInt(yT)) return false;
        if (odoFrom || odoTo) {
            const km = parseInt(String(d.odometer||'').replace(/[^0-9]/g,'')) || 0;
            if (odoFrom && km < parseInt(odoFrom)) return false;
            if (odoTo && km > parseInt(odoTo)) return false;
        }
        if (q) {
            const h = (d.name+d.vin+d.damage+d.title_doc+d.drive_status).toLowerCase();
            if (!h.includes(q)) return false;
        }
        return true;
    });
}

function onDmgChange() {
    const all = document.querySelectorAll('#filter-damage-list input[type=checkbox]');
    const chk = document.querySelectorAll('#filter-damage-list input[type=checkbox]:checked');
    const sa = document.getElementById('dmg-select-all');
    const btn = document.getElementById('filter-damage-btn');
    if (chk.length === all.length || chk.length === 0) {
        selectedDamages = new Set(); sa.checked=true; sa.indeterminate=false;
        btn.textContent='Uszkodzenia \\u25BE'; btn.classList.remove('active-filter');
    } else {
        selectedDamages = new Set([...chk].map(c=>c.value));
        sa.checked=false; sa.indeterminate=true;
        btn.textContent='Uszkodzenia ('+chk.length+') \\u25BE'; btn.classList.add('active-filter');
    }
    renderTable();
}

document.getElementById('dmg-select-all').addEventListener('change', function() {
    document.querySelectorAll('#filter-damage-list input[type=checkbox]').forEach(c => c.checked=this.checked);
    selectedDamages = new Set();
    const btn = document.getElementById('filter-damage-btn');
    btn.textContent='Uszkodzenia \\u25BE'; btn.classList.remove('active-filter');
    this.indeterminate=false; renderTable();
});

document.getElementById('filter-damage-btn').addEventListener('click', function(e) {
    e.stopPropagation();
    document.getElementById('filter-damage-dropdown').classList.toggle('open');
});
document.addEventListener('click', function(e) {
    if (!document.getElementById('filter-damage-wrap').contains(e.target))
        document.getElementById('filter-damage-dropdown').classList.remove('open');
});

function parseNum(s) {
    if (!s) return -1;
    const n = parseFloat(String(s).replace(/[^0-9.-]/g, ''));
    return isNaN(n) ? -1 : n;
}

function sortData(data) {
    return [...data].sort((a, b) => {
        let va = a[sortCol]||'', vb = b[sortCol]||'';
        if (['price','bid','buy_now','year','odometer'].includes(sortCol)) { va=parseNum(va); vb=parseNum(vb); }
        else { va=String(va).toLowerCase(); vb=String(vb).toLowerCase(); }
        if (va < vb) return sortAsc ? -1 : 1;
        if (va > vb) return sortAsc ? 1 : -1;
        return 0;
    });
}

function renderTable() {
    const filtered = sortData(getFiltered());
    const total = allData.length;
    const count = filtered.length;
    const rc = document.getElementById('result-count');
    rc.textContent = count === total ? total + ' ofert' : count + ' z ' + total + ' ofert';

    const tbody = document.getElementById('table-body');

    if (count === 0 && total > 0) {
        tbody.innerHTML = '<tr><td colspan="14" class="empty-state">'
            +'<div class="empty-state-icon">&#128269;</div>'
            +'<div class="empty-state-text">Brak wynikow dla wybranych filtrow</div>'
            +'<div class="empty-state-sub">Zmien kryteria wyszukiwania lub kliknij Reset</div>'
            +'</td></tr>';
    } else if (total === 0) {
        tbody.innerHTML = '<tr><td colspan="14" class="empty-state">'
            +'<div class="empty-state-icon">&#128666;</div>'
            +'<div class="empty-state-text">Brak ofert w bazie</div>'
            +'<div class="empty-state-sub">Kliknij Skanuj aby pobrac oferty</div>'
            +'</td></tr>';
    } else {
        tbody.innerHTML = filtered.map(d => {
            const sc = d.source==='IAAI' ? 'badge-iaai' : 'badge-copart';
            const img = d.image_url
                ? '<img class="thumb" src="'+esc(d.image_url)+'" loading="lazy" alt="'+esc(d.make_model)+'" onerror="this.outerHTML=\\'<div class=no-img>brak</div>\\'">'
                : '<div class="no-img">brak</div>';
            return '<tr>'
                +'<td>'+img+'</td>'
                +'<td><span class="badge '+sc+'">'+esc(d.source)+'</span></td>'
                +'<td class="year-val">'+esc(d.year)+'</td>'
                +'<td class="vehicle-name">'+(d.link ? '<a class="vehicle-link" href="'+esc(d.link)+'" target="_blank" rel="noopener">'+esc(d.make_model)+'</a>' : esc(d.make_model))+'</td>'
                +'<td>'+(d.vin ? '<a class="vin-link" href="https://nextcar-usa.pl/pl/auta-z-usa/oferty/aktualne-oferty?szukaj='+encodeURIComponent(d.vin)+'" target="_blank" rel="noopener">'+esc(d.vin)+'</a>' : '<span class="empty">&mdash;</span>')+'</td>'
                +'<td>'+(d.damage ? '<span class="damage-tag '+getDmgClass(d.damage)+'">'+esc(d.damage)+'</span>' : '<span class="empty">&mdash;</span>')+'</td>'
                +'<td>'+(d.drive_status ? '<span class="status-tag '+getStatusClass(d.drive_status)+'">'+esc(d.drive_status)+'</span>' : '<span class="empty">&mdash;</span>')+'</td>'
                +'<td><span class="title-tag '+getTitleClass(d.title_doc)+'">'+(esc(d.title_doc)||'&mdash;')+'</span></td>'
                +'<td class="num price-val">'+(esc(d.price)||'<span class="empty">&mdash;</span>')+'</td>'
                +'<td class="num bid-val">'+(esc(d.bid)||'<span class="empty">&mdash;</span>')+'</td>'
                +'<td class="num buy-val">'+(esc(d.buy_now)||'')+'</td>'
                +'<td class="num odo-val">'+(esc(d.odometer)||'<span class="empty">&mdash;</span>')+'</td>'
                +'<td class="auction-val">'+(esc(d.auction_date)||'<span class="empty">&mdash;</span>')+'</td>'
                +'<td class="date-val">'+esc(d.date_found)+'</td>'
                +'</tr>';
        }).join('');
    }

    document.querySelectorAll('thead th').forEach(th => {
        th.classList.toggle('sorted', th.dataset.sort===sortCol);
        const a = th.querySelector('.arrow');
        if (a && th.dataset.sort===sortCol) a.innerHTML = sortAsc ? '&#9650;' : '&#9660;';
    });

    updateFilterIndicators();
}

function updateFilterIndicators() {
    const ids = ['filter-source','filter-status','filter-year-from','filter-year-to','filter-odo-from','filter-odo-to'];
    let anyActive = false;
    ids.forEach(id => {
        const el = document.getElementById(id);
        const active = el.value !== '';
        el.classList.toggle('active-filter', active);
        if (active) anyActive = true;
    });
    const q = document.getElementById('search').value;
    if (q) anyActive = true;
    if (selectedDamages.size > 0) anyActive = true;
    document.getElementById('btn-reset').classList.toggle('visible', anyActive);
}

function resetFilters() {
    document.getElementById('search').value = '';
    document.getElementById('filter-source').value = '';
    document.getElementById('filter-status').value = '';
    document.getElementById('filter-year-from').value = '';
    document.getElementById('filter-year-to').value = '';
    document.getElementById('filter-odo-from').value = '';
    document.getElementById('filter-odo-to').value = '';
    selectedDamages = new Set();
    const sa = document.getElementById('dmg-select-all');
    sa.checked = true; sa.indeterminate = false;
    document.querySelectorAll('#filter-damage-list input[type=checkbox]').forEach(c => c.checked = true);
    document.getElementById('filter-damage-btn').textContent = 'Uszkodzenia \\u25BE';
    document.getElementById('filter-damage-btn').classList.remove('active-filter');
    renderTable();
    showToast('Filtry wyczyszczone');
}

function getDmgClass(d) {
    if (!d) return 'none';
    const l = d.toLowerCase();
    if (l==='brak'||l.includes('drobne')||l.includes('normalne')) return 'minor';
    return '';
}
function getTitleClass(t) {
    if (!t) return '';
    const l = t.toLowerCase();
    if (l.includes('salvage')) return 'salvage';
    if (l.includes('clean')) return 'clean';
    if (l.includes('rebuild')) return 'rebuilt';
    return '';
}
function getStatusClass(s) {
    if (!s) return '';
    const l = s.toLowerCase();
    if (l.includes('run')&&l.includes('drive')) return 'run';
    if (l.includes('enhanced')) return 'enhanced';
    if (l.includes('start')) return 'starts';
    if (l.includes('stationary')) return 'stationary';
    return '';
}
function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

document.querySelectorAll('thead th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
        const c = th.dataset.sort;
        if (sortCol===c) sortAsc=!sortAsc; else { sortCol=c; sortAsc=true; }
        renderTable();
    });
});

document.getElementById('search').addEventListener('input', renderTable);
document.getElementById('filter-source').addEventListener('change', renderTable);
document.getElementById('filter-status').addEventListener('change', renderTable);
document.getElementById('filter-year-from').addEventListener('change', renderTable);
document.getElementById('filter-year-to').addEventListener('change', renderTable);
document.getElementById('filter-odo-from').addEventListener('change', renderTable);
document.getElementById('filter-odo-to').addEventListener('change', renderTable);

let _cooldownInterval = null;

function startCooldownTimer(btn, remaining) {
    btn.disabled = true;
    function tick() {
        if (remaining <= 0) {
            clearInterval(_cooldownInterval);
            _cooldownInterval = null;
            btn.disabled = false;
            btn.textContent = 'Skanuj';
            return;
        }
        const m = Math.floor(remaining / 60);
        const s = remaining % 60;
        btn.textContent = m + ':' + String(s).padStart(2,'0');
        remaining--;
    }
    tick();
    _cooldownInterval = setInterval(tick, 1000);
}

async function refreshData() {
    const btn = document.getElementById('btn-refresh');
    btn.disabled = true; btn.textContent = 'Skanowanie...';
    const resp = await fetch('/api/refresh', {method:'POST'});
    const result = await resp.json();

    if (result.status === 'cooldown') {
        showToast(result.message);
        if (_cooldownInterval) clearInterval(_cooldownInterval);
        startCooldownTimer(btn, result.remaining);
        return;
    }
    if (result.status === 'skipped') {
        showToast(result.message);
        btn.disabled = false; btn.textContent = 'Skanuj';
        return;
    }

    showToast('Scraper uruchomiony...');
    const oldCount = allData.length; let attempts = 0;
    const poll = setInterval(async () => {
        attempts++;
        const res = await fetch('/api/stats');
        const stats = await res.json();
        if (stats.total !== oldCount || attempts > 10) {
            clearInterval(poll); await loadData();
            btn.disabled = false; btn.textContent = 'Skanuj';
            const diff = stats.total - oldCount;
            showToast(diff > 0 ? 'Znaleziono ' + diff + ' nowych ofert' : 'Brak nowych ofert');
        }
    }, 3000);
}


function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent=msg; t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 4000);
}

loadData();

/* ── Lightbox ── */
const lightbox = document.getElementById('lightbox');
const lightboxImg = document.getElementById('lightbox-img');
const lightboxInfo = document.getElementById('lightbox-info');

function getFullResUrl(thumbUrl) {
    if (!thumbUrl) return '';
    /* IAAI: resizer endpoint - request large size */
    if (thumbUrl.includes('vis.iaai.com')) {
        return thumbUrl.replace(/width=\\d+/, 'width=1600').replace(/height=\\d+/, 'height=1200');
    }
    /* Copart: _thb.jpg -> _ful.jpg for full resolution */
    if (thumbUrl.includes('copart.com')) {
        return thumbUrl.replace('_thb.jpg', '_ful.jpg');
    }
    return thumbUrl;
}

document.getElementById('table-body').addEventListener('click', e => {
    const thumb = e.target.closest('.thumb');
    if (!thumb) return;
    const src = getFullResUrl(thumb.src);
    lightboxImg.src = src;
    const row = thumb.closest('tr');
    const name = row ? row.querySelector('.vehicle-name') : null;
    lightboxInfo.textContent = name ? name.textContent : '';
    lightbox.classList.add('open');
    document.body.style.overflow = 'hidden';
});

function closeLightbox() {
    lightbox.classList.remove('open');
    document.body.style.overflow = '';
    setTimeout(() => { lightboxImg.src = ''; }, 300);
}

lightbox.addEventListener('click', e => {
    if (e.target === lightboxImg) return;
    closeLightbox();
});
document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && lightbox.classList.contains('open')) closeLightbox();
});
</script>
</body>
</html>
"""

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  Wyszukiwarka Andrzeja")
    print("  http://localhost:8080")
    print("=" * 50)
    app.run(debug=True, host="0.0.0.0", port=8080)
