#!/usr/bin/env python3
"""Fetch league fixture pages, parse them, attach ground coordinates and write
docs/data/fixtures.json for the map page.

Run from the repo root:  python scripts/build_fixtures.py
Test the parser offline: python scripts/build_fixtures.py --html sample.html --league national-league
"""
from urllib.parse import urljoin
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
CACHE = ROOT / "data" / "postcode_cache.json"
PLACE_CACHE = ROOT / "data" / "place_cache.json"
DEBUG = ROOT / "data" / "debug"
MISSING = ROOT / "data" / "missing_grounds.csv"
CLUBINFO = ROOT / "data" / "club_info_cache.json"
CLUB_PAGE_BUDGET = int(__import__("os").environ.get("CLUB_PAGE_BUDGET", "450"))  # club pages fetched per run
MAX_PLACE_LOOKUPS = 250  # per run, so a first run on a big list stays quick; the rest follow next run

USER_AGENT = "football-finder/0.1 (+https://github.com/ashwillis97/football-finder)"

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

DATE_HEADER = re.compile(
    r"([A-Za-z]+day)\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})")
NUMERIC_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
TIME_CELL = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)$|^(\d{1,2})[:.](\d{2})$", re.I)
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


def parse_structured(soup, league):
    """Parser for the real Football Web Pages markup:
    <tr class="title"><th>Saturday 14th November 2026</th></tr> then
    <tr data-href=... title="A v B[ - Postponed]"><td class="status">3pm</td>
    <td class="home-team" data-export="A">..<td class="away-team" data-export="B">.."""
    fixtures = []
    current_date = None
    found = False  # True once any fixture/result row is seen, even if all are already played
    for row in soup.find_all("tr"):
        th = row.find("th")
        if th and not row.get("data-href"):
            h = DATE_HEADER.search(clean(th.get_text(" ")))
            if h and MONTHS.get(h.group(3).lower()):
                current_date = datetime(int(h.group(4)), MONTHS[h.group(3).lower()], int(h.group(2))).date()
            continue
        home_td = row.find("td", class_="home-team")
        away_td = row.find("td", class_="away-team")
        if not (home_td and away_td and current_date):
            continue
        found = True
        home = clean(home_td.get("data-export") or home_td.get_text(" "))
        away = clean(away_td.get("data-export") or away_td.get_text(" "))
        status_td = row.find("td", class_="status")
        status_text = clean((status_td.get_text(" ") if status_td else "").replace("\xa0", " "))
        title = row.get("title", "")
        score_cells = [clean(c.get_text(" ")) for c in row.find_all("td", class_="score")]
        kickoff = norm_time(status_text) if status_text else None
        if "postponed" in title.lower() or any(s.upper() in POSTPONED for s in score_cells):
            state = "postponed"
        elif status_text and not kickoff and RESULT_STATUS.match(status_text):
            continue  # finished
        elif any(s.isdigit() for s in score_cells):
            continue  # has a score, so already played
        else:
            state = "scheduled"
        def slug_of(td):
            a = td.find("a", href=True)
            h = a["href"].strip("/").split("/") if a else []
            return h[-1] if len(h) == 1 or (len(h) > 1 and h[-2].startswith("footballwebpages")) else ""
        fixtures.append({
            "date": current_date.isoformat(), "time": kickoff, "home": home, "away": away,
            "status": state, "league": league["name"], "level": league["level"], "gender": league["gender"],
            "_hs": slug_of(home_td), "_as": slug_of(away_td),
        })
    return fixtures if found else None


def future_months(html):
    """Months after the currently selected one in the page's month dropdown.
    Earlier months only hold matches that were already played."""
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", id="month")
    if not sel:
        return []
    opts = sel.find_all("option")
    idx = next((i for i, o in enumerate(opts) if o.has_attr("selected")), None)
    if idx is None:
        return []
    return [o.get("value", "") for o in opts[idx + 1:] if o.get("value")]


def month_options(html):
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", id="month")
    return [o.get("value", "") for o in sel.find_all("option")] if sel else []


def parse_fixtures(html, league):
    """Return (fixtures, month_links). Tries the structured parser first, then a tolerant fallback."""
    soup = BeautifulSoup(html, "html.parser")
    structured = parse_structured(soup, league)
    if structured is not None:
        links = sorted({a["href"] for a in soup.find_all("a", href=True) if "fixtures-results" in a["href"]})
        return structured, links
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
    """config/grounds.csv columns: club, gender (men/women, blank = both), ground, town, postcode.
    A postcode is preferred. Without one, the ground and town are looked up on OpenStreetMap."""
    grounds = {}
    with open(CONFIG / "grounds.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            club = (row.get("club") or "").strip()
            if not club:
                continue
            gender = (row.get("gender") or "").strip().lower()
            grounds[(club.lower(), gender)] = {k: (v or "").strip() for k, v in row.items() if k}
    return grounds


def find_ground(grounds, club, gender):
    return grounds.get((club.lower(), gender)) or grounds.get((club.lower(), ""))


def geocode(postcodes):
    """Look up postcodes via postcodes.io (cached). Returns {postcode: [lat, lon] or None}."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    cache = {k: (v if v and v[0] is not None else None) for k, v in cache.items()}
    missing = [p for p in postcodes if p not in cache]
    for i in range(0, len(missing), 100):
        batch = missing[i:i + 100]
        try:
            r = requests.post("https://api.postcodes.io/postcodes", json={"postcodes": batch},
                              headers={"User-Agent": USER_AGENT}, timeout=30)
            r.raise_for_status()
            for item in r.json().get("result", []):
                res = item.get("result")
                cache[item["query"]] = [res["latitude"], res["longitude"]] if res and res.get("latitude") is not None else None
        except requests.RequestException as e:
            print(f"postcode lookup failed: {e}", file=sys.stderr)
    # Postcodes missing from the live database may still be in the terminated list.
    for pc in [p for p in postcodes if p in cache and cache[p] is None]:
        try:
            r = requests.get("https://api.postcodes.io/terminated_postcodes/" + pc.replace(" ", ""),
                             headers={"User-Agent": USER_AGENT}, timeout=30)
            if r.ok and r.json().get("result"):
                res = r.json()["result"]
                if res.get("latitude"):
                    cache[pc] = [res["latitude"], res["longitude"]]
        except requests.RequestException:
            pass
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1))
    return cache


def geocode_places(places):
    """Look up (ground, town) pairs on OpenStreetMap Nominatim, one per second, cached."""
    cache = json.loads(PLACE_CACHE.read_text()) if PLACE_CACHE.exists() else {}
    todo = [p for p in sorted(places) if f"{p[0]}|{p[1]}" not in cache]
    done = 0
    for ground, town in todo:
        if done >= MAX_PLACE_LOOKUPS:
            print(f"place lookups capped at {MAX_PLACE_LOOKUPS}; {len(todo) - done} left for next run")
            break
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"q": f"{ground}, {town}, United Kingdom", "format": "jsonv2", "limit": 1,
                                     "addressdetails": 1, "countrycodes": "gb"},
                             headers={"User-Agent": USER_AGENT}, timeout=30)
            r.raise_for_status()
            res = r.json()
            if res:
                a = res[0]
                addr = a.get("address", {})
                first_town = town.split(",")[0].strip().lower()
                cache[f"{ground}|{town}"] = {
                    "lat": float(a["lat"]), "lon": float(a["lon"]),
                    "postcode": addr.get("postcode", ""), "display": a.get("display_name", ""),
                    "confident": bool(first_town) and first_town in a.get("display_name", "").lower(),
                }
            else:
                cache[f"{ground}|{town}"] = None
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"place lookup failed ({ground}, {town}): {e}", file=sys.stderr)
            break
        done += 1
        time.sleep(1.1)  # Nominatim allows at most one request per second
    PLACE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PLACE_CACHE.write_text(json.dumps(cache, indent=1))
    return cache


def locate(g, coords, places):
    pc = g.get("postcode", "")
    try:  # manual coordinates in grounds.csv (columns lat, lon) override everything
        if g.get("lat") and g.get("lon"):
            return {"lat": float(g["lat"]), "lon": float(g["lon"]), "postcode": pc, "confident": True}
    except ValueError:
        pass
    if pc and coords.get(pc):
        return {"lat": coords[pc][0], "lon": coords[pc][1], "postcode": pc, "confident": True}
    p = places.get(f"{g.get('ground', '')}|{g.get('town', '')}")
    if p:
        return {"lat": p["lat"], "lon": p["lon"], "postcode": pc or p.get("postcode", ""), "confident": p["confident"]}
    return None


def fetch(url):
    last = None
    for attempt in range(3):  # the site sometimes answers 5xx or times out; retry with a pause
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            if r.status_code in (401, 403, 429):
                raise RuntimeError(f"{url} refused the request (HTTP {r.status_code}); not retrying")
            if r.status_code >= 500:
                last = requests.HTTPError(f"HTTP {r.status_code} for {url}")
                time.sleep(8 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.text
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            time.sleep(8 * (attempt + 1))
    raise last


POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*([0-9O][A-Z]{2})\b")
KNOWN_LABELS = {"ground", "address", "capacity", "manager", "colours (home)", "colours (away)",
                "official club website", "social media accounts", "club information"}


def team_slugs(html):
    """{club title: page slug} from the teams list on a league page."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for ul in soup.find_all("ul", class_="teams"):
        for a in ul.find_all("a", href=True):
            title = clean(a.get("title") or a.get_text(" "))
            href = a["href"].strip("/")
            if title and href and "/" not in href:
                out[title] = href
    return out


def parse_club_info(html):
    """Read Ground and Address from a club's club-information page. Works whether the
    label and value are table cells or separate blocks, since it reads the page text line by line."""
    soup = BeautifulSoup(html, "html.parser")
    lines = [clean(l) for l in soup.get_text("\n").split("\n")]
    lines = [l for l in lines if l]

    def value(label):
        for i, l in enumerate(lines):
            if l.lower().rstrip(":").strip() == label:
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                return "" if nxt.lower().rstrip(":").strip() in KNOWN_LABELS else nxt
        for l in lines:  # label and value on one line
            m = re.match(rf"^{label}\s*:\s*(.+)$", l, re.I)
            if m:
                return m.group(1)
        return ""

    ground, address = value("ground"), value("address")
    m = POSTCODE.search(address.upper())
    postcode = f"{m.group(1)} {m.group(2).replace('O', '0', 1) if m.group(2)[0] == 'O' else m.group(2)}" if m else ""
    website = ""
    for el in soup.find_all(string=re.compile(r"official club website", re.I)):
        for a in el.find_all_next("a", href=re.compile(r"^https?://", re.I), limit=6):
            if not re.search(r"footballwebpages|facebook\.com|twitter\.com|x\.com|instagram\.com|youtube\.com|tiktok\.com", a["href"], re.I):
                website = a["href"].strip()
                break
        if website:
            break
    return {"ground": ground, "address": address, "postcode": postcode, "website": website}


def fetch_club_info(slugs, cache, budget):
    """Fetch club-information pages not yet cached (or stale). Stops on any refusal."""
    today = datetime.now(timezone.utc).date()
    fetched = 0
    for slug in sorted(slugs):
        entry = cache.get(slug)
        if entry and "website" in entry:
            age = (today - datetime.fromisoformat(entry["fetched"]).date()).days
            if age < (90 if (entry.get("ground") or entry.get("postcode")) else 14):
                continue
        if fetched >= budget:
            break
        try:
            html = fetch(f"https://www.footballwebpages.co.uk/{slug}/club-information")
        except RuntimeError as e:
            print(f"club pages: {e}", file=sys.stderr)
            break
        except requests.RequestException as e:
            print(f"club page {slug}: {e}", file=sys.stderr)
            time.sleep(2)
            continue
        info = parse_club_info(html)
        info["fetched"] = today.isoformat()
        cache[slug] = info
        fetched += 1
        time.sleep(1.5)
    return fetched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", help="parse a local HTML file instead of fetching")
    ap.add_argument("--league", help="league id to use with --html")
    args = ap.parse_args()

    leagues = json.loads((CONFIG / "leagues.json").read_text())
    if args.league:
        leagues = [l for l in leagues if l["id"] == args.league]

    all_fixtures, report, league_clubs, club_slugs = [], {}, {}, {}
    for league in leagues:
        if args.html:
            html = Path(args.html).read_text(encoding="utf-8")
        else:
            try:
                html = fetch(league["url"])
            except (RuntimeError, requests.RequestException) as e:
                print(f"{league['id']}: {e}", file=sys.stderr)
                report[league["id"]] = {"error": str(e)}
                time.sleep(2)
                continue
            time.sleep(2)  # be polite between requests
        fixtures, _ = parse_fixtures(html, league)
        for f in fixtures:
            for side, key in (("home", "_hs"), ("away", "_as")):
                if f.get(key):
                    club_slugs.setdefault((f[side].lower(), league["gender"]), f[key])
        for title, slug in team_slugs(html).items():
            club_slugs.setdefault((title.lower(), league["gender"]), slug)
        months = {}
        if league.get("cup") and not args.html:
            # cups are organised by round, not month: follow the page's other fixtures-results links
            base = league["url"].rstrip("/")
            seen = {(f["date"], f["home"], f["away"]) for f in fixtures}
            soup0 = BeautifulSoup(html, "html.parser")
            ROUND = re.compile(r"(round|qualifying|preliminary|quarter|semi|final|play-?off|replay|group|area|first|second|third|fourth|fifth|sixth|seventh|eighth)", re.I)
            pages = sorted({urljoin(base + "/", a["href"]) for a in soup0.find_all("a", href=True)
                            if re.search(rf"/{re.escape(league['id'])}/fixtures-results/([a-z0-9-]+)/?$", urljoin(base + "/", a["href"]))
                            and ROUND.search(urljoin(base + "/", a["href"]).rstrip("/").rsplit("/", 1)[-1])})
            for url in pages[:10]:
                try:
                    page = fetch(url)
                except (RuntimeError, requests.RequestException) as e:
                    months[url] = f"error: {e}"
                    time.sleep(2)
                    continue
                time.sleep(2)
                extra, _ = parse_fixtures(page, league)
                new = [f for f in extra if (f["date"], f["home"], f["away"]) not in seen]
                months[url.rsplit("/", 1)[-1]] = len(new)
                fixtures.extend(new)
                seen |= {(f["date"], f["home"], f["away"]) for f in new}
                for title, slug in team_slugs(page).items():
                    club_slugs.setdefault((title.lower(), league["gender"]), slug)
        if not args.html:
            base = league["url"].rstrip("/")
            seen = {(f["date"], f["home"], f["away"]) for f in fixtures}
            for month in future_months(html):
                try:
                    page = fetch(f"{base}/{month}")
                except (RuntimeError, requests.RequestException) as e:
                    months[month] = f"error: {e}"
                    time.sleep(2)
                    continue
                time.sleep(2)
                extra, _ = parse_fixtures(page, league)
                new = [f for f in extra if (f["date"], f["home"], f["away"]) not in seen]
                months[month] = len(new)
                fixtures.extend(new)
                seen |= {(f["date"], f["home"], f["away"]) for f in new}
            if not fixtures:
                DEBUG.mkdir(parents=True, exist_ok=True)
                (DEBUG / f"{league['id']}.html").write_text(html, encoding="utf-8")
        print(f"{league['id']}: {len(fixtures)} fixtures parsed")
        report[league["id"]] = {"fixtures": len(fixtures), "months": months}
        league_clubs[league["id"]] = (league["gender"], sorted({c for f in fixtures for c in (f["home"], f["away"])}))
        all_fixtures.extend(fixtures)

    grounds = load_grounds()
    club_info = json.loads(CLUBINFO.read_text()) if CLUBINFO.exists() else {}
    need = set(club_slugs.values())  # every club: grounds for some, websites for all
    fetched = 0
    if not args.html:
        fetched = fetch_club_info(need, club_info, CLUB_PAGE_BUDGET)
        CLUBINFO.parent.mkdir(parents=True, exist_ok=True)
        CLUBINFO.write_text(json.dumps(club_info, indent=1, sort_keys=True))
    no_info = []
    for (c, g), slug in club_slugs.items():
        if (c, g) in grounds or (c, "") in grounds:
            continue
        info = club_info.get(slug)
        if info and (info.get("ground") or info.get("postcode") or info.get("address")):
            grounds[(c, g)] = {"club": c, "gender": g, "ground": info.get("ground", ""),
                               "town": "" if info.get("postcode") else info.get("address", ""),
                               "postcode": info.get("postcode", "")}
        else:
            no_info.append(c)
    report["club_pages"] = {"fetched_this_run": fetched, "not_yet_fetched": len([s_ for s_ in need if s_ not in club_info]),
                            "page_has_no_ground_info": sorted(no_info)[:200]}
    coords = geocode(sorted({g["postcode"] for g in grounds.values() if g.get("postcode")}))
    need_places = {(g["ground"], g.get("town", "")) for g in grounds.values()
                   if g.get("ground") and g.get("town") and not (g.get("postcode") and coords.get(g["postcode"]))}
    places = geocode_places(need_places)

    sites = {(c, g): (club_info.get(slug) or {}).get("website", "") for (c, g), slug in club_slugs.items()}
    for f in all_fixtures:
        for side in ("home", "away"):
            w = sites.get((f[side].lower(), f["gender"]))
            if w:
                f[side + "_site"] = w
    for f in all_fixtures:
        g = find_ground(grounds, f["home"], f["gender"])
        loc = locate(g, coords, places) if g else None
        if loc:
            f.update(ground=g.get("ground", ""), town=g.get("town", ""), postcode=loc["postcode"],
                     lat=loc["lat"], lon=loc["lon"])
            f["_confident"] = loc["confident"]

    missing_rows, low_conf = [], set()
    for lid, (gender, clubs) in league_clubs.items():
        no_ground = [c for c in clubs if not find_ground(grounds, c, gender)]
        unlocated = [c for c in clubs if find_ground(grounds, c, gender)
                     and not locate(find_ground(grounds, c, gender), coords, places)]
        report[lid].update(clubs=len(clubs), missing_grounds=len(no_ground), unlocated=unlocated)
        missing_rows += [(lid, c, "no ground") for c in no_ground] + [(lid, c, "could not locate") for c in unlocated]
    for f in all_fixtures:
        if f.get("_confident") is False:
            low_conf.add(f"{f['home']} ({f.get('ground', '')}, {f.get('town', '')})")
    report["low_confidence_places"] = sorted(low_conf)
    report["bad_postcodes"] = sorted(p for p in {g["postcode"] for g in grounds.values() if g.get("postcode")}
                                     if not coords.get(p))

    MISSING.parent.mkdir(parents=True, exist_ok=True)
    with open(MISSING, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["league", "club", "problem"])
        w.writerows(missing_rows)

    placed = [{k: v for k, v in f.items() if not k.startswith("_")} for f in all_fixtures if "lat" in f]
    # If the source site failed for a league this run, keep that league's earlier upcoming fixtures
    # rather than wiping them from the map.
    if not args.html and (OUT / "fixtures.json").exists():
        try:
            old = json.loads((OUT / "fixtures.json").read_text())["matches"]
        except (ValueError, KeyError):
            old = []
        today_iso = datetime.now(timezone.utc).date().isoformat()
        got = {(f["league"], f["gender"]) for f in all_fixtures}
        kept = 0
        for lg in leagues:
            failed = "error" in report.get(lg["id"], {}) or (lg["name"], lg["gender"]) not in got
            if failed:
                for m in old:
                    if m["league"] == lg["name"] and m["gender"] == lg["gender"] and m["date"] >= today_iso:
                        placed.append(m)
                        kept += 1
                report.setdefault(lg["id"], {})["carried_over_from_last_run"] = True
        report["carried_over_matches"] = kept
    placed.sort(key=lambda f: (f["date"], f["time"] or "99:99", f["home"]))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "fixtures.json").write_text(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "matches": placed,
    }))
    (OUT / "report.json").write_text(json.dumps(report, indent=1))
    print(f"wrote {len(placed)} fixtures with coordinates; {len(missing_rows)} club problems in {MISSING.name}")


if __name__ == "__main__":
    main()
