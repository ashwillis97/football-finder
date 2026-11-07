# Local Kick Off

An interactive map of upcoming football matches in England. Search by postcode or place, then filter by distance, date, level and men's/women's.

## How it works

- `config/leagues.json` lists the leagues to pull and the page each comes from.
- `config/grounds.csv` maps each club to its ground and postcode.
- `scripts/build_fixtures.py` fetches the pages, parses fixtures, looks up postcode coordinates (postcodes.io) and writes `docs/data/fixtures.json`.
- `docs/index.html` is the map page, served by GitHub Pages from the `docs` folder.
- `scripts/build_pages.py` writes a crawlable page per league (`docs/leagues/`), the sitemap and the footer links on the home page. Change `SITE` at the top when the real domain is set.
- `.github/workflows/update-fixtures.yml` runs the build once a day (05:30 UTC) and commits the result.

## Setup

1. Repo Settings > Pages: deploy from branch `main`, folder `/docs`.
2. Actions > Update fixtures > Run workflow, for a first run.
3. Check `docs/data/report.json` for parsing problems and clubs missing from `grounds.csv`.

## Adding leagues

Add an entry to `config/leagues.json` and add each club's ground to `config/grounds.csv`. Clubs without a ground are left off the map and listed under `unmatched_clubs` in the report.

## Testing the parser offline

    python scripts/build_fixtures.py --html tests/sample_fixtures.html --league national-league

## Notes

- The map shows only each team's next match (not the whole season). A match appears only when it is the next one for BOTH clubs, so no club is ever listed twice. If a club's opponent plays another game first (cup weekend, midweek game), that club's match appears once the opponent's earlier game has passed. The full season stays in the data file; the page works this out each time it loads.
- Postponed matches are kept in the data but hidden on the map, and skipped when working out a team's next match.
- Match dates and kick-off times change; the page tells visitors to check with the club.
- Check the source site's terms before launching publicly.
