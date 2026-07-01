# ✈️ Trip-Scrapper

A tiny self-hosted trip finder: pick a destination and it finds the **cheapest flights**
from Iași / Chișinău, scrapes **live Airbnb stays**, adds your **extra costs** (baggage,
transfers, city tax…), and shows an **all-in trip total** — with a clean local web UI.

Built around free / no-key data sources, so anyone can run it.

## Features
- 🌍 Browse **57 cities / 25 countries**, filter, and pick a destination.
- 🛫 **Cheapest flights** (Aviasales/Travelpayouts data) with **duration, full route, and every stop airport** shown — direct vs connecting is obvious.
- 🔁 **One-way or return**, with a real **round-trip booking link** (return included).
- 📊 **2nd / 3rd cheapest** flight options per leg.
- 🏠 **Live Airbnb stays** scraped directly (no key) — homes, rooms, hotels, hostels, B&Bs — each with its type, price, and a working listing link. Tap one to use it in the total.
- 📅 The room dates **always match the chosen flight**.
- ⏱ **Min / max nights** control.
- ➕ Editable **extra costs** (checked bag, airport transfer, city/tourist tax, bus, insurance, food) itemised into the total.
- 🌐 **Compare all destinations** ranked by price.
- 💾 SQLite price history + optional **Telegram alerts** on price drops (`--watch`).

> ⚠️ **Honest note on prices:** the flight figures come from a free *cached* API and are
> often **lower than the real bookable price** — use them to compare/spot deals, then confirm
> the true price on the booking link. Routes, durations, stops and Airbnb prices are accurate.

## Quick start (local)
```bash
pip install -r requirements.txt

# add your free Travelpayouts token (https://www.travelpayouts.com -> Tools -> Data API):
#   easiest: create a file token.txt containing just the token (it's gitignored)
echo "YOUR_TOKEN_HERE" > token.txt

python web.py        # opens the UI at http://127.0.0.1:8765
```

### CLI
```bash
python trip_scraper.py                 # check the default destination
python trip_scraper.py --city Vienna   # one destination
python trip_scraper.py --compare       # rank every city
python trip_scraper.py --oneway        # one-way only
python trip_scraper.py --watch         # keep checking + Telegram alerts
python trip_scraper.py --demo          # sample data, no token needed
```

## Configuration — `config.json`
- `origins`, `destination`, `trip` dates, `nights_min` / `nights_max`, `travelers`
- `extras` — list of `{name, amount, per}` (`per` = `trip` / `person` / `night` / `person_night`)
- `telegram` — optional `bot_token` + `chat_id` for drop alerts
- The Travelpayouts token is **not** stored here in this repo — see below.

## Token / secrets
The app looks for the Travelpayouts token in this order:
1. `TRAVELPAYOUTS_TOKEN` environment variable
2. a local `token.txt` file (gitignored)
3. `config.json` → `travelpayouts_token`

For a public repo or a deploy, use option 1 or 2 so the token never gets committed.

## Deploy it online
See **[DEPLOY.md](DEPLOY.md)** — runs as-is on Render / Railway / Fly.io (free tiers).
Set `TRAVELPAYOUTS_TOKEN` as an environment variable on the host.

## How it works
- Flights: Travelpayouts `prices_for_dates` (cached). The route/stops are decoded from the
  Aviasales link token; duration comes from the API.
- Real fares scraped straight from the airlines (no keys) and merged over the cache:
  **Ryanair** (cheapest-per-day API), **Wizz Air** (timetable API, prices converted
  from the local currency), **FlyOne** (fare-calendar API, token scraped from their
  homepage). **zbor.md** (local Moldovan agency) "from €X" teasers are shown per route
  from Chișinău as a cross-check with a booking link.
- Stays: Airbnb's public search page embeds all results as JSON — fetched with a normal
  request and parsed (no key, no headless browser).
- `web.py` is a stdlib `http.server` (no web framework) that reuses `trip_scraper.py` +
  `stays.py` + `sources.py`.

## Notes & limits
- Chișinău's airport code changed **KIV → RMO** in 2024; the tool uses RMO (and quietly
  translates KIV if you type it). Both Iași (IAS) and Chișinău (RMO) are searched.
- HiSky has no public fare feed (ASP.NET booking engine) — no prices from them.
- Airbnb scraping can be rate-limited / blocked from cloud IPs (works locally).
- Free cached flight prices are indicative — confirm on the booking page.

For personal use. Be respectful of the upstream sites' rate limits.
