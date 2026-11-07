"""Generate crawlable league pages, the leagues index, the sitemap and the footer links on the home page.
Run after build_fixtures.py. Reads docs/data/fixtures.json and config/leagues.json."""
import json, re, html
from datetime import datetime, timezone, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SITE = "https://ashwillis97.github.io/football-finder/"   # change when the real domain is set
NAME = "Local Kick Off"
e = html.escape

leagues = json.loads((ROOT / "config" / "leagues.json").read_text())
data = json.loads((DOCS / "data" / "fixtures.json").read_text())
today = date.today().isoformat()
matches = [m for m in data["matches"] if m["status"] != "postponed" and m["date"] >= today]

# each club's next match; a match is listed if it is next for at least one of its clubs
nxt = {}
for m in sorted(matches, key=lambda m: (m["date"], m["time"] or "99:99")):
    for club in (m["home"], m["away"]):
        nxt.setdefault((m["gender"], club), id(m))  # across league and cup
by_id = {id(m): m for m in matches}


def fmt_date(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%A %-d %B %Y")


CSS = """:root{--bg:#fff;--tint:#f3f5f2;--ink:#101512;--muted:#626c66;--line:#dfe3df;--accent:#0a7a3d}
@media(prefers-color-scheme:dark){:root{--bg:#0f1411;--tint:#18201b;--ink:#f0f3f0;--muted:#98a39b;--line:#2a342d;--accent:#3fc07a}}
*{box-sizing:border-box;border-radius:0}body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
.bar{background:#0d1a12;color:#fff;padding:26px 32px 22px;border-bottom:6px solid #3fc07a}.bar a{color:#fff;text-decoration:none;font:400 2.2rem/1 "Bebas Neue",Impact,sans-serif;letter-spacing:.05em}.bar a span{color:#3fc07a}
main{max-width:820px;margin:0 auto;padding:48px 24px 72px}h1{font:400 2.6rem/1.05 "Bebas Neue",Impact,sans-serif;letter-spacing:.04em;margin:0 0 12px}
h2{font:400 1.3rem/1 "Bebas Neue",Impact,sans-serif;letter-spacing:.06em;color:var(--muted);margin:44px 0 6px}p.lead{color:var(--muted);margin:0 0 28px}
a{color:var(--accent);font-weight:600}.cta{display:inline-block;background:var(--accent);color:var(--bg);padding:12px 24px;text-decoration:none;margin:8px 0 8px}
ul{list-style:none;margin:0;padding:0}li.m{padding:18px 0;border-bottom:1px solid var(--line)}.t{font-weight:700}.s{color:var(--muted);font-size:.92rem}
.cols{columns:2 260px;column-gap:40px}.cols li{padding:5px 0;break-inside:avoid}footer{padding:28px 24px 48px;color:var(--muted);font-size:.85rem;border-top:1px solid var(--line);text-align:center}"""
FONT = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bebas+Neue&display=swap">'


def page(title, desc, path, body, ld=None):
    url = SITE + path
    ldj = f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>' if ld else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title><meta name="description" content="{e(desc)}"><link rel="canonical" href="{url}">
<meta property="og:type" content="website"><meta property="og:site_name" content="{NAME}"><meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}"><meta property="og:url" content="{url}"><meta property="og:locale" content="en_GB">
<meta name="theme-color" content="#0d1a12">{FONT}<style>{CSS}</style>{ldj}</head><body>
<div class="bar"><a href="{SITE}">Local <span>Kick Off</span></a></div>
<main>{body}</main>
<footer><a href="{SITE}leagues/">All leagues</a> · Dates and kick-off times can change, so check with the club before travelling.</footer>
</body></html>
"""


urls, groups = [""], {}
for lg in leagues:
    ms = [m for m in by_id.values() if m["league"] == lg["name"] and m["gender"] == lg["gender"]
          and (nxt[(m["gender"], m["home"])] == id(m) or nxt[(m["gender"], m["away"])] == id(m))]
    ms.sort(key=lambda m: (m["date"], m["time"] or "99:99", m["home"]))
    pretty = lg["name"] + (" (women's)" if lg["gender"] == "women" and "women" not in lg["name"].lower() else "")
    groups.setdefault(lg["level"], []).append((lg, pretty, len(ms)))
    parts, last = [], None
    for m in ms:
        if m["date"] != last:
            if last is not None:
                parts.append("</ul>")
            parts.append(f"<h2>{fmt_date(m['date'])}</h2><ul>")
            last = m["date"]
        site = f' · <a href="{e(m["home_site"])}" rel="noopener">{e(m["home"])} website</a>' if m.get("home_site", "").startswith("http") else ""
        where = ", ".join(x for x in (m.get("ground"), m.get("town"), m.get("postcode")) if x)
        parts.append(f'<li class="m"><div class="t">{e(m["home"])} v {e(m["away"])}</div>'
                     f'<div class="s">Kick-off {e(m["time"] or "TBC")}{(" · " + e(where)) if where else ""}{site}</div></li>')
    if ms:
        parts.append("</ul>")
    listing = "".join(parts) or "<p>No upcoming fixtures are listed right now. Check back soon.</p>"
    body = (f"<h1>{e(pretty)} fixtures</h1><p class=\"lead\">The next {'cup ties' if lg.get('cup') else 'match for each club'} in the {e(pretty)}, "
            f"with kick-off times and ground addresses. Updated daily.</p>"
            f'<a class="cta" href="{SITE}">See these on the map</a>{listing}'
            f'<p><a href="{SITE}leagues/">Other leagues</a></p>')
    ld = {"@context": "https://schema.org", "@type": "ItemList", "name": f"{pretty} fixtures",
          "itemListElement": [{"@type": "ListItem", "position": i + 1, "item": {
              "@type": "SportsEvent", "name": f"{m['home']} v {m['away']}",
              "startDate": m["date"] + (f"T{m['time']}" if m["time"] else ""),
              "location": {"@type": "Place", "name": m.get("ground") or m["home"],
                           "address": ", ".join(x for x in (m.get("town"), m.get("postcode")) if x) or "England"},
              "homeTeam": {"@type": "SportsTeam", "name": m["home"]}, "awayTeam": {"@type": "SportsTeam", "name": m["away"]}}}
              for i, m in enumerate(ms[:25])]}
    out = DOCS / "leagues" / f"{lg['id']}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page(f"{pretty} fixtures – next matches, kick-off times | {NAME}",
                        f"Next {pretty} fixtures with kick-off times, dates and ground addresses. Find your nearest match on the map.",
                        f"leagues/{lg['id']}.html", body, ld), encoding="utf-8")
    urls.append(f"leagues/{lg['id']}.html")

idx = "<h1>Football fixtures by league</h1><p class=\"lead\">Pick a league to see the next match for every club, from the Premier League to Step 6 and the women's game.</p>"
for level, items in groups.items():
    idx += f"<h2>{e(level)}</h2><ul class=\"cols\">" + "".join(
        f'<li><a href="{SITE}leagues/{lg["id"]}.html">{e(p)}</a></li>' for lg, p, n in items) + "</ul>"
(DOCS / "leagues" / "index.html").write_text(page(f"Football fixtures by league – Premier League to non-league | {NAME}",
    "Fixtures for every league from the Premier League to Step 6 of non-league football, plus the women's game.", "leagues/", idx), encoding="utf-8")
urls.insert(1, "leagues/")

(DOCS / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
    "".join(f"  <url><loc>{SITE}{u}</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq></url>\n" for u in urls) + "</urlset>\n")

# footer links on the home page (crawlable)
links = "".join(f'<a href="leagues/{lg["id"]}.html">{e(p)}</a> ' for items in groups.values() for lg, p, n in items)
ix = DOCS / "index.html"
s = ix.read_text(encoding="utf-8")
block = f'<!--LEAGUES--><nav class="leaguelinks"><b>Fixtures by league</b> {links}</nav><!--/LEAGUES-->'
s = re.sub(r"<!--LEAGUES-->.*?<!--/LEAGUES-->", lambda _: block, s, flags=re.S)
ix.write_text(s, encoding="utf-8")
print(f"{len(urls)} pages written")
