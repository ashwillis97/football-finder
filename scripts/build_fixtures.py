#!/usr/bin/env python3
"""Fetch league fixture pages, parse them, attach ground coordinates and write
docs/data/fixtures.json for the map page.

Run from the repo root:  python scripts/build_fixtures.py
Test the parser offline: python scripts/build_fixtures.py --html sample.html --league national-league
"""
import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
OUT = ROOT / "docs" / "data"
RAW = ROOT / "data" / "raw"
CACHE = ROOT / "data" / "postcode_cache.json"

USER_AGENT = "football-finder/0.1 (+https://github.com/ashwillis97/football-finder)"

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

DATE_HEADER = re.compile(
    r"([A-Za-z]+day)\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})")
NUMERIC_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
TIME_CELL = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$|^(\d{1,2}):(\d{2})$", re.I)
HALF_TIME = re.compile(r"\(\d+\)")
POSTPONED = {"P", "PP", "POSTPONED", "PPD"}
RESULT_STATUS = re.compile(r"^(FT|HT|AET|PEN|ABD|AB|A)\b", re.I)


def clean(text):
    return re.sub(r"\s+", " ", HALF_TIME.sub("", text or "")).strip()


def norm_time(cell):
    """'3pm' -> '15:00', '19:45' -> '19:45', otherwise None."""
    m = TIME_CELL.match(cell.strip())
    if not m:
        return None
    if m.group(3):
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"
    return f"{int(m.group(4)):02d}:{m.group(5)}"


def parse_fixtures(html, league):
    """Return (fixtures, month_links). Tolerant: works on table rows or div rows."""
    soup = BeautifulSoup(html, "html.parser")
    fixtures = []
    current_date = None

    rows = soup.find_all("tr")
    if not rows:  # fall back to generic row-like containers
        rows = soup.select("div.row, li")

    for row in rows:
        cells = [clean(c.get_text(" ")) for c in row.find_all(["td", "th", "div", "span"], recursive=True)
                 if not c.find(["td", "th", "div", "span"])]
        cells = [c for c in cells if c]
        if not cells:
            continue

        joined = " ".join(cells)
        header = DATE_HEADER.search(joined)
        if header and len(cells) <= 2:
            month = MONTHS.get(header.group(3).lower())
            if month:
                current_date = datetime(int(header.group(4)), month, int(header.group(2))).date()
            continue

        match_date = current_date
        for c in cells:
            nd = NUMERIC_DATE.match(c)
            if nd:
                match_date = datetime(int(nd.group(3)), int(nd.group(2)), int(nd.group(1))).date()
        if match_date is None:
            continue

        kickoff = None
        status = "scheduled"
        names = []
        for c in cells:
            if NUMERIC_DATE.match(c):
                continue
            t = norm_time(c)
            if t and kickoff is None:
                kickoff = t
                continue
            up = c.upper()
            if up in POSTPONED:
                status = "postponed"
                continue
            if RESULT_STATUS.match(c) and len(c) <= 8:
                status = "finished"
                continue
            if c.lower() == "v" or c.isdigit() or c.replace(",", "").isdigit():
                continue
            if re.fullmatch(r"[\d\s\-:']+", c):
                continue
            names.append(c)

        if len(names) < 2:
            continue
        home, away = names[0], names[-1]
        if status == "finished":
            continue
        fixtures.append({
            "date": match_date.isoformat(),
            "time": kickoff,
            "home": home,
            "away": away,
            "status": status,
            "league": league["name"],
            "level": league["level"],
            "gender": league["gender"],
        })

    links = sorted({a["href"] for a in soup.find_all("a", href=True)
                    if "fixtures-results" in a["href"]})
    return fixtures, links


def load_grounds():
    grounds = {}
    with open(CONFIG / "grounds.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            grounds[row["club"].strip().lower()] = row
    return grounds


def geocode(postcodes):
    """Look up postcodes via postcodes.io (cached). Returns {postcode: (lat, lon)}."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    missing = [p for p in postcodes if p not in cache]
    for i in range(0, len(missing), 100):
        batch = missing[i:i + 100]
        try:
            r = requests.post("https://api.postcodes.io/postcodes", json={"postcodes": batch},
                              headers={"User-Agent": USER_AGENT}, timeout=30)
            r.raise_for_status()
            for item in r.json().get("result", []):
                res = item.get("result")
                cache[item["query"]] = [res["latitude"], res["longitude"]] if res else None
        except requests.RequestException as e:
            print(f"postcode lookup failed: {e}", file=sys.stderr)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1))
    return cache


def fetch(url):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"{url} refused the request (HTTP {r.status_code}); not retrying")
    r.raise_for_status()
    return r.text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", help="parse a local HTML file instead of fetching")
    ap.add_argument("--league", help="league id to use with --html")
    args = ap.parse_args()

    leagues = json.loads((CONFIG / "leagues.json").read_text())
    if args.league:
        leagues = [l for l in leagues if l["id"] == args.league]

    all_fixtures, report = [], {}
    for league in leagues:
        if args.html:
            html = Path(args.html).read_text(encoding="utf-8")
        else:
            try:
                html = fetch(league["url"])
            except (RuntimeError, requests.RequestException) as e:
                print(f"{league['id']}: {e}", file=sys.stderr)
                report[league["id"]] = {"error": str(e)}
                continue
            RAW.mkdir(parents=True, exist_ok=True)
            (RAW / f"{league['id']}.html").write_text(html, encoding="utf-8")
            time.sleep(2)  # be polite between requests
        fixtures, links = parse_fixtures(html, league)
        print(f"{league['id']}: {len(fixtures)} fixtures parsed")
        report[league["id"]] = {"fixtures": len(fixtures), "month_links": links[:40]}
        all_fixtures.extend(fixtures)

    grounds = load_grounds()
    coords = geocode(sorted({g["postcode"] for g in grounds.values()}))
    unmatched = set()
    for f in all_fixtures:
        g = grounds.get(f["home"].lower())
        pc = coords.get(g["postcode"]) if g else None
        if g and pc:
            f.update(ground=g["ground"], postcode=g["postcode"], lat=pc[0], lon=pc[1])
        else:
            unmatched.add(f["home"])
    report["unmatched_clubs"] = sorted(unmatched)

    placed = [f for f in all_fixtures if "lat" in f]
    placed.sort(key=lambda f: (f["date"], f["time"] or "99:99", f["home"]))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "fixtures.json").write_text(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "matches": placed,
    }, indent=1))
    (OUT / "report.json").write_text(json.dumps(report, indent=1))
    print(f"wrote {len(placed)} fixtures with coordinates; unmatched clubs: {sorted(unmatched) or 'none'}")


if __name__ == "__main__":
    main()
