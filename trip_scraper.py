"""
Trip-Scrapper  -  cheapest flight + place-to-stay finder.

What it does
------------
For each origin airport you list (default: IAS = Iasi, RMO = Chisinau) it finds the
cheapest flight to your destination across a date range, adds your baggage estimate,
then finds the cheapest place to stay for N nights, and reports the TOTAL trip cost
(flights + stay). It stores every check in a small SQLite file so it can tell you when
the price drops, and it can ping you on Telegram when the total falls below a threshold.

Why Travelpayouts (and not "scrape the airline site")
-----------------------------------------------------
Airline / Booking / Google Flights pages are wrapped in Cloudflare + CAPTCHAs and ban
scrapers. Travelpayouts (Aviasales flight data + Hotellook hotels) is a FREE official
API that hands you the same cheapest-price data with no bypassing needed. The prices are
cached (refreshed from real searches, ~7-day window) which is exactly what you want for
"watch a route and alert me on a drop". Always confirm the final price on the real site
before you pay.

SETUP (one time)
----------------
1) pip install -r requirements.txt
2) Get a free token: https://www.travelpayouts.com  ->  sign up  ->  Aviasales program
   ->  Tools / API  ->  copy your API token. EITHER paste it into config.json ->
   travelpayouts_token, OR set an environment variable TRAVELPAYOUTS_TOKEN (better -
   keeps the secret out of the committed file).
3) (optional) Telegram alerts: talk to @BotFather to make a bot, get the bot_token;
   get your chat_id from @userinfobot. Put both in config.json -> telegram.
4) Edit config.json: destination, dates, nights, baggage estimate, alert threshold.

USAGE
-----
    python trip_scraper.py                 # check once, print the cheapest combos
    python trip_scraper.py --city Vienna   # override the destination for this run
    python trip_scraper.py --compare       # rank EVERY city in cities.json by total
    python trip_scraper.py --watch         # keep checking on the interval in config.json
    python trip_scraper.py --list-cities   # show destinations you can pick by name
    python trip_scraper.py --history       # show the cheapest total seen so far per route
    python trip_scraper.py --export out.csv# dump the whole price history to CSV
    python trip_scraper.py --demo          # run on built-in sample data (no token needed)
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import requests

import stays  # accommodation prices (Airbnb + Booking via RapidAPI)
import sources  # extra price sources: Ryanair real fares, ground transport

try:  # don't crash printing non-Latin city/listing names on a Windows console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CITIES_PATH = os.path.join(HERE, "cities.json")
DB_PATH = os.path.join(HERE, "trip_prices.db")

FLIGHT_API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
HOTEL_API = "https://engine.hotellook.com/api/v2/cache.json"

ORIGIN_NAMES = {"IAS": "Iasi", "RMO": "Chisinau", "KIV": "Chisinau", "OTP": "Bucharest",
                "SCV": "Suceava", "BCM": "Bacau", "CLJ": "Cluj"}

# Chisinau airport's IATA code changed KIV -> RMO in March 2024; the flight API only
# knows RMO ("airport KIV: not flightable"). Accept the old code anywhere and translate.
AIRPORT_ALIASES = {"KIV": "RMO"}

# Estimated checked-bag (~20 kg) price per FLIGHT LEG, per person, in EUR, keyed by the
# airline's IATA code. Aviasales only exposes the real bag price via a live in-browser
# search (loads by XHR, can't be scraped cheaply), so we estimate from the airline and
# point the user to the booking page for the exact figure.
AIRLINE_BAG = {
    "W4": 50, "W6": 50, "W9": 50, "WZ": 50,        # Wizz Air
    "FR": 42, "RK": 42,                             # Ryanair
    "U2": 45, "EC": 45, "DS": 45,                   # easyJet
    "VY": 45, "TO": 45, "EW": 45, "PC": 40,         # Vueling, Transavia, Eurowings, Pegasus
    "5F": 45, "H4": 40, "U5": 45,                   # FlyOne, HiSky, SkyUp
}
DEFAULT_BAG_LEG = 40


def airline_name(code):
    return {"W4": "Wizz Air", "W6": "Wizz Air", "W9": "Wizz Air", "FR": "Ryanair",
            "U2": "easyJet", "VY": "Vueling", "TO": "Transavia", "EW": "Eurowings",
            "PC": "Pegasus", "5F": "FlyOne", "H4": "HiSky", "H9": "HiSky",
            "U5": "SkyUp", "PQ": "SkyUp"}.get((code or "").upper(), code or "?")


def airline_bag_leg(code):
    return AIRLINE_BAG.get((code or "").upper(), DEFAULT_BAG_LEG)

# Flipped on by --demo: requests are answered from built-in sample JSON shaped exactly
# like the real APIs, so you can see the output and check the parsing without a token.
DEMO = False


class RouteUnavailable(Exception):
    """Raised when the flight data API says an airport/route isn't covered at all
    (e.g. 'airport KIV: not flightable') - retrying other months won't help."""


# --------------------------------------------------------------------------- #
#  Config / cities
# --------------------------------------------------------------------------- #
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_token(cfg):
    """Find the Travelpayouts token without committing it: env var first, then a local
    gitignored token.txt, then config.json (which should hold only a placeholder in a
    public repo)."""
    env = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if env:
        return env.strip()
    tpath = os.path.join(HERE, "token.txt")
    if os.path.exists(tpath):
        try:
            t = open(tpath, "r", encoding="utf-8").read().strip()
            if t:
                return t
        except OSError:
            pass
    return cfg.get("travelpayouts_token", "")


def validate_config(cfg):
    """Catch the date/nights mistakes that would otherwise just return 'nothing found'."""
    t = cfg["trip"]
    try:
        a = datetime.strptime(t["depart_from"], "%Y-%m-%d")
        b = datetime.strptime(t["depart_to"], "%Y-%m-%d")
    except (KeyError, ValueError):
        raise SystemExit("config.trip.depart_from / depart_to must be dates like 2026-08-01")
    if a > b:
        raise SystemExit("config.trip.depart_from is after depart_to - swap them")
    nmin, nmax = nights_bounds(t)
    if nmin < 1:
        raise SystemExit("config.trip.nights_min must be at least 1")
    if nmax < nmin:
        raise SystemExit("config.trip.nights_max must be >= nights_min")
    if b < datetime.now() and not DEMO:
        print("  ! warning: your whole departure window is in the past - no fares will match")


def resolve_destination(name, cities):
    """Accept a city name, a country name, or a raw IATA code. Return (iata, hotel_location, label)."""
    # raw 3-letter airport code, e.g. "PRG"
    if len(name) == 3 and name.isupper():
        return name, name, name

    # exact city match
    if name in cities and not name.startswith("_"):
        c = cities[name]
        return c["airport"], c["hotel_location"], f"{name} ({c['airport']})"

    # country match -> first city in that country
    for city, c in cities.items():
        if city.startswith("_"):
            continue
        if c.get("country", "").lower() == name.lower():
            return c["airport"], c["hotel_location"], f"{city}, {c['country']} ({c['airport']})"

    raise SystemExit(
        f"Don't know destination '{name}'. Add it to cities.json, or use a 3-letter "
        f"airport code (e.g. PRG). Run --list-cities to see what's available."
    )


def parse_routes(raw_link):
    """Aviasales encodes the routing in the link's t= token as runs of 3-letter codes,
    e.g. ...IASFCOBCN... = IAS->FCO->BCN. Return [[IAS,FCO,BCN], ...] (outbound first)."""
    m = re.search(r"[?&]t=([^&]+)", raw_link or "")
    if not m:
        return []
    tok = m.group(1).split("_")[0]
    runs = re.findall(r"[A-Z]{6,}", tok)  # >=2 airports
    return [[r[i:i + 3] for i in range(0, len(r), 3)] for r in runs]


def aviasales_link(origin, dest, out_date, back_date, travelers):
    """Build an Aviasales search deep-link. With a back_date it's a real ROUND TRIP search
    (return included in the ticket); without it, a one-way. Format: ORIG+DDMM+DEST(+DDMM)+pax."""
    def ddmm(d):
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d%m")
    code = f"{origin}{ddmm(out_date)}{dest}"
    if back_date:
        code += ddmm(back_date)
    return f"https://www.aviasales.com/search/{code}{max(1, int(travelers))}"


def fmt_duration(minutes):
    """115 -> '1h55', 295 -> '4h55', 0/None -> '?'."""
    if not minutes:
        return "?"
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}" if h else f"{m}m"


def stops_label(leg):
    """'direct' or '2 stops via BRU, CRL' - counted from the actual route when we have it."""
    via = leg.get("stops") or []
    if via:
        n = len(via)
        return f"{n} stop" + ("s" if n > 1 else "") + f" via {', '.join(via)}"
    n = leg.get("transfers", 0)  # no parsed route - fall back to the API's count
    return "direct" if not n else f"{n} stop" + ("s" if n > 1 else "")


def fmt_leg(leg, cur):
    """One-line summary of a flight leg: date price airline route duration stops."""
    return (f"{leg['date']}  {leg['price']} {cur}  {leg['airline']}  "
            f"{' > '.join(leg['route'])}  {fmt_duration(leg['duration'])}  {stops_label(leg)}")


def nights_bounds(t):
    """Min/max trip length from config. Prefers nights_min/nights_max; falls back to the
    older nights / nights_flex keys so old configs still work."""
    nmin = int(t.get("nights_min") or t.get("nights") or 3)
    nmax = int(t.get("nights_max") or (nmin + int(t.get("nights_flex", 14))))
    return nmin, max(nmin, nmax)


def compute_extras(extras, travelers, nights):
    """Turn a list of extra-cost definitions into itemised totals.
    Each item: {name, amount, per} where per is trip / person / night / person_night."""
    mult = {"trip": 1, "person": travelers, "night": nights,
            "person_night": travelers * nights}
    items = []
    for e in extras or []:
        amount = float(e.get("amount", 0) or 0)
        per = e.get("per", "trip")
        total = round(amount * mult.get(per, 1), 2)
        items.append({"name": e.get("name", "extra"), "per": per,
                      "unit": amount, "total": total})
    return items, round(sum(i["total"] for i in items), 2)


def months_in_range(date_from, date_to):
    """Return the list of 'YYYY-MM' strings the [from, to] window touches."""
    a = datetime.strptime(date_from, "%Y-%m-%d")
    b = datetime.strptime(date_to, "%Y-%m-%d")
    months, cur = [], a.replace(day=1)
    while cur <= b:
        months.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


# --------------------------------------------------------------------------- #
#  HTTP with retry / 429 back-off (and the --demo short-circuit)
# --------------------------------------------------------------------------- #
def request_json(url, params, retries=3):
    if DEMO:
        return _demo_response(url, params)
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:  # rate limited - honour Retry-After, then back off
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 30))
                continue
            # the flight API returns structured JSON errors on 400/422 (e.g. an airport
            # that isn't flightable) - hand those back so the caller can react cleanly
            if r.status_code in (400, 422):
                try:
                    return r.json()
                except ValueError:
                    pass
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last if last else RuntimeError("request failed")


# --------------------------------------------------------------------------- #
#  Travelpayouts: flights
# --------------------------------------------------------------------------- #
def _leg_dict(f, origin, dest):
    """Turn one API fare into a leg dict (price/date/route/duration/stops)."""
    routes = parse_routes(f.get("link", ""))
    route = routes[0] if routes else [origin, dest]
    return {
        "price": f["price"],
        "date": (f.get("departure_at") or "")[:10],
        "airline": f.get("airline", "?"),
        "transfers": f.get("transfers", 0),
        "duration": f.get("duration_to") or f.get("duration") or 0,
        "route": route,
        "stops": route[1:-1],  # intermediate airports
        "link": "https://www.aviasales.com" + f.get("link", ""),
        "source": "cached",  # Travelpayouts cached estimate (vs Ryanair real)
    }


def fetch_leg_options(origin, dest, date_from, date_to, currency, token, limit=6):
    """Cheapest legs origin->dest departing in [from, to], sorted by price, one (the
    cheapest) per date, up to `limit` - so the caller can show 2nd/3rd/... cheapest."""
    origin = AIRPORT_ALIASES.get(origin, origin)
    dest = AIRPORT_ALIASES.get(dest, dest)
    origin = AIRPORT_ALIASES.get(origin, origin)
    dest = AIRPORT_ALIASES.get(dest, dest)
    legs = []
    for month in months_in_range(date_from, date_to):
        params = {
            "origin": origin, "destination": dest, "departure_at": month,
            "one_way": "true", "currency": currency, "sorting": "price",
            "limit": 30, "token": token,
        }
        try:
            resp = request_json(FLIGHT_API, params)
        except (requests.RequestException, ValueError) as e:
            print(f"  ! flight lookup failed ({origin}->{dest} {month}): {e}")
            continue
        if isinstance(resp, dict) and resp.get("error") and not resp.get("data"):
            raise RouteUnavailable(resp["error"])  # e.g. "airport KIV: not flightable"
        for f in (resp.get("data") or []):
            day = (f.get("departure_at") or "")[:10]
            if date_from <= day <= date_to:
                legs.append(_leg_dict(f, origin, dest))
    legs.sort(key=lambda leg: leg["price"])
    by_date = {}
    for leg in legs:                      # cheapest cached fare per date
        by_date.setdefault(leg["date"], leg)
    # merge in REAL fares scraped from the airlines' own sites (Ryanair, Wizz Air,
    # FlyOne); a real fare beats a cached estimate for the same date, and between two
    # real fares the cheaper one wins
    for real_source in (sources.ryanair_leg_options, sources.wizz_leg_options,
                        sources.flyone_leg_options, sources.hisky_leg_options,
                        sources.skyup_leg_options):
        try:
            for leg in real_source(origin, dest, date_from, date_to, currency):
                cur = by_date.get(leg["date"])
                if cur is None or cur.get("source") == "cached" or leg["price"] < cur["price"]:
                    by_date[leg["date"]] = leg
        except Exception:
            pass
    return sorted(by_date.values(), key=lambda l: l["price"])[:limit]


def fetch_cheapest_oneway(origin, dest, date_from, date_to, currency, token):
    opts = fetch_leg_options(origin, dest, date_from, date_to, currency, token, limit=1)
    return opts[0] if opts else None


def fetch_flights(origin, dest, cfg):
    """Cheapest outbound, plus a return leg unless this is a one-way trip.

    trip.type = "return" (default) searches a return leg between (chosen outbound + nights)
    and (+ nights + nights_flex) so the trip really is ~N nights instead of an accidental
    1- or 50-night gap. trip.type = "oneway" skips the return entirely. LCCs price each leg
    separately, so two one-ways is usually the cheapest combo. Fares scale by travelers;
    baggage is your per-route estimate (also per traveler).
    """
    t = cfg["trip"]
    cur, token = cfg["currency"], resolve_token(cfg)
    travelers = max(1, int(t.get("travelers", 1)))
    one_way = str(t.get("type", "return")).lower() == "oneway"
    nmin, nmax = nights_bounds(t)  # user's min/max trip length

    out_options = fetch_leg_options(origin, dest, t["depart_from"], t["depart_to"], cur, token)
    if not out_options:
        return None
    out = out_options[0]

    back, back_options = None, []
    if not one_way:
        # return must be between (outbound + min nights) and (outbound + max nights)
        out_day = datetime.strptime(out["date"], "%Y-%m-%d")
        ret_from = (out_day + timedelta(days=nmin)).strftime("%Y-%m-%d")
        ret_to = (out_day + timedelta(days=nmax)).strftime("%Y-%m-%d")
        back_options = fetch_leg_options(dest, origin, ret_from, ret_to, cur, token)
        back = back_options[0] if back_options else None

    fare_pp = out["price"] + (back["price"] if back else 0)
    fare = round(fare_pp * travelers, 2)
    # checked-bag estimate from the outbound airline, per leg actually flown
    legs = 2 if (not one_way and back) else 1
    bag_per_person = airline_bag_leg(out["airline"]) * legs
    bag_total = round(bag_per_person * travelers, 2)
    baggage = round(cfg.get("baggage_fee_per_route", {}).get(origin, 0) * travelers, 2)
    actual_nights = None
    if back:
        actual_nights = (datetime.strptime(back["date"], "%Y-%m-%d")
                         - datetime.strptime(out["date"], "%Y-%m-%d")).days
    booking_link = aviasales_link(origin, dest, out["date"],
                                  back["date"] if back else None, travelers)
    # flying from Chisinau: also show the local agency's (zbor.md) teaser price
    agency = None
    if AIRPORT_ALIASES.get(origin, origin) == "RMO":
        try:
            agency = sources.zbor_offer(dest)
        except Exception:
            agency = None
    return {
        "fare": fare,
        "baggage": baggage,
        "flight_total": round(fare + baggage, 2),
        "out": out,
        "back": back,
        "out_options": out_options,
        "back_options": back_options,
        "travelers": travelers,
        "actual_nights": actual_nights,
        "one_way": one_way,
        "booking_link": booking_link,
        "bag_total": bag_total,
        "bag_airline": airline_name(out["airline"]),
        "bag_legs": legs,
        "agency": agency,
    }


# --------------------------------------------------------------------------- #
#  Hotellook: place to stay
# --------------------------------------------------------------------------- #
def fetch_stay(location, check_in, nights, cfg):
    """Cheapest place to stay for the period, via the stays module (Airbnb + Booking)."""
    return stays.cheapest_stay(location, check_in, nights, cfg["currency"], cfg)


# --------------------------------------------------------------------------- #
#  SQLite storage
# --------------------------------------------------------------------------- #
def db_init():
    # keep --demo runs out of the real price history
    db = sqlite3.connect(os.path.join(HERE, "trip_prices_demo.db") if DEMO else DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS checks (
                ts TEXT, origin TEXT, dest TEXT,
                fare REAL, baggage REAL, flight_total REAL,
                out_date TEXT, back_date TEXT, airline TEXT,
                stay_name TEXT, stay_total REAL, nights INTEGER,
                grand_total REAL )"""
    )
    db.commit()
    return db


def db_save(db, dest, origin, flights, stay, nights, grand_total):
    db.execute(
        "INSERT INTO checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            datetime.now().isoformat(timespec="seconds"),
            origin, dest,
            flights["fare"], flights["baggage"], flights["flight_total"],
            flights["out"]["date"], (flights["back"] or {}).get("date"), flights["out"]["airline"],
            stay["name"] if stay else None, stay["stay_total"] if stay else None,
            nights,
            grand_total,
        ),
    )
    db.commit()


def db_best(db, origin, dest):
    row = db.execute(
        "SELECT MIN(grand_total) FROM checks WHERE origin=? AND dest=?", (origin, dest)
    ).fetchone()
    return row[0]


# --------------------------------------------------------------------------- #
#  Telegram
# --------------------------------------------------------------------------- #
def send_telegram(cfg, text):
    tg = cfg.get("telegram", {})
    if not tg.get("bot_token") or not tg.get("chat_id"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
            json={"chat_id": tg["chat_id"], "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  ! telegram failed: {e}")


# --------------------------------------------------------------------------- #
#  One full check
# --------------------------------------------------------------------------- #
def run_once(cfg, cities, dest_override=None, quiet=False):
    dest_name = dest_override or cfg["destination"]
    dest_iata, hotel_loc, label = resolve_destination(dest_name, cities)
    nmin, nmax = nights_bounds(cfg["trip"])

    def say(*a, **k):
        if not quiet:
            print(*a, **k)

    say(f"\n=== Trip to {label}  |  {cfg['trip']['depart_from']} .. {cfg['trip']['depart_to']}  |  {nmin}-{nmax} nights ===")
    cur = cfg["currency"].upper()
    db = db_init()

    # 1) flights first (for every origin), so the stay can match the real travel dates
    flight_rows = []  # (origin, oname, flights)
    for origin in cfg["origins"]:
        oname = ORIGIN_NAMES.get(origin, origin)
        try:
            flights = fetch_flights(origin, dest_iata, cfg)
        except RouteUnavailable as e:
            say(f"\n[{origin} {oname}]  not covered by the flight data API ({e})."
                f"\n               -> try a nearby airport (e.g. IAS Iasi) or reach it by bus.")
            continue
        if not flights:
            say(f"\n[{origin} {oname}]  no flights found for these dates")
            continue
        flight_rows.append((origin, oname, flights))

    # 2) stay matched to the cheapest flight's ACTUAL dates (skipped in quiet/compare mode)
    stay, stay_nights = None, nmin
    if flight_rows and not quiet:
        cheapest_f = min(flight_rows, key=lambda r: r[2]["flight_total"])[2]
        check_in = cheapest_f["out"]["date"]
        stay_nights = cheapest_f["actual_nights"] or nmin
        check_out = (datetime.strptime(check_in, "%Y-%m-%d")
                     + timedelta(days=stay_nights)).strftime("%Y-%m-%d")
        stay = fetch_stay(hotel_loc, check_in, stay_nights, cfg)
        if stay:
            say(f"Cheapest stay : {stay['name']}  {stay['stay_total']} {cur}  "
                f"({stay['per_night']}/night, via {stay['source']})  "
                f"[{check_in} -> {check_out}, {stay_nights} nights, matches the flight]")
        else:
            say(f"Cheapest stay : none found for {check_in} -> {check_out} - tracking FLIGHTS only")

    # 3) print each origin's flights (with alternatives) and record
    results = []
    for origin, oname, flights in flight_rows:
        stay_total = stay["stay_total"] if stay else 0
        extra_items, extra_total = compute_extras(cfg.get("extras", []), flights["travelers"], stay_nights)
        bag_total = flights["bag_total"]
        g = sources.ground_to_airport(origin, flights["out"]["date"])
        ground_total = round(g["price"] * flights["travelers"] * flights["bag_legs"], 2) if g else 0
        grand = round(flights["flight_total"] + bag_total + ground_total + stay_total + extra_total, 2)
        results.append((origin, oname, flights, grand))

        prev_best = db_best(db, origin, dest_iata)
        db_save(db, dest_iata, origin, flights, stay, stay_nights, grand)

        out, back = flights["out"], flights["back"]
        kind = "one-way" if flights["one_way"] else "round trip"
        say(f"\n[{origin} {oname} -> {dest_iata}]  ({flights['travelers']} traveler(s), {kind})")
        say(f"  Outbound : {fmt_leg(out, cur)}")
        for alt in flights["out_options"][1:4]:
            say(f"      or  : {fmt_leg(alt, cur)}")
        if flights["one_way"]:
            pass  # one-way: no return leg by design
        elif back:
            note = f"  ({flights['actual_nights']} nights)" if flights["actual_nights"] is not None else ""
            say(f"  Return   : {fmt_leg(back, cur)}{note}")
            for alt in flights["back_options"][1:4]:
                say(f"      or  : {fmt_leg(alt, cur)}")
        else:
            say(f"  Return   : none found (try one-way, or widen dates/nights_flex)")
        say(f"  FLIGHTS  : {flights['flight_total']} {cur} (cached estimate, baggage NOT included)")
        say(f"  Checked bag : +{bag_total} {cur} ({flights['bag_airline']} estimate, {flights['bag_legs']} leg(s) - exact price on booking page)")
        if g:
            say(f"  Get to airport : +{ground_total} {cur} (Chisinau -> {g['to']} by {g['mode']}, ~{g['hours']}h est. - book {g['book']})")
        link_kind = "one-way" if flights["one_way"] else "round-trip (return included)"
        say(f"  Book {link_kind}: {flights['booking_link']}")
        if flights.get("agency"):
            ag = flights["agency"]
            say(f"  Local agency : zbor.md sells {ag['name']} from ~{ag['price']} EUR - {ag['link']}")
        if stay:
            say(f"  Stay     : +{stay_total} {cur}")
        for it in extra_items:
            if it["total"]:
                say(f"  + {it['name']:28s}: +{it['total']} {cur}")
        line = f"  TRIP TOTAL (everything): ~{grand} {cur}"
        if prev_best is not None and grand < prev_best:
            say(line + f"   <-- NEW LOW (was {prev_best})")
        else:
            say(line)

        # alert: below threshold, or a fresh all-time low
        threshold = cfg.get("alert_threshold_total")
        is_new_low = prev_best is not None and grand < prev_best
        if (threshold and grand <= threshold) or is_new_low:
            tag = "below your threshold" if (threshold and grand <= threshold) else "a new low"
            msg = (
                f"&#9992;&#65039; <b>{oname} -> {label}</b> total <b>{grand} {cur}</b> ({tag})\n"
                f"Flights {flights['flight_total']} (out {out['date']}"
                + (f", back {back['date']}" if back else "")
                + f") + stay {stay_total}\n{flights['booking_link']}"
            )
            send_telegram(cfg, msg)

    # direct long-distance bus Chisinau <-> destination (skipped in quiet/compare mode)
    if flight_rows and not quiet:
        cheapest_f = min(flight_rows, key=lambda r: r[2]["flight_total"])[2]
        bus_back_date = (datetime.strptime(cheapest_f["out"]["date"], "%Y-%m-%d")
                         + timedelta(days=cheapest_f["actual_nights"] or nmin)).strftime("%Y-%m-%d")
        try:
            bus = sources.bus_home_options(hotel_loc, cheapest_f["out"]["date"], bus_back_date)
        except Exception:
            bus = None
        if bus:
            say(f"\n  Direct bus Chisinau <-> {label}:")
            if bus["out"]:
                say(f"    There : {bus['out']['price']} EUR FlixBus dep {bus['out']['dep']} - book {bus['out']['book']}")
            if bus["back"]:
                say(f"    Back  : {bus['back']['price']} EUR FlixBus dep {bus['back']['dep']} - book {bus['back']['book']}")
            say(f"    More carriers/prices: {bus['infobus_out']}")

    if results:
        best = min(results, key=lambda r: r[3])
        say(f"\n>>> Cheapest route right now: {best[1]} ({best[0]})  =  {best[3]} {cur}")
        say("    (flight prices = Aviasales CACHED lowest fares, often outdated - confirm the real")
        say("     price via the booking link; stay prices are live from Airbnb)")
    db.close()
    return results


def run_compare(cfg, cities):
    """Check every city in cities.json and rank the cheapest totals across all of them."""
    cur = cfg["currency"].upper()
    _nmin, _nmax = nights_bounds(cfg["trip"])
    print(f"Comparing every destination | {cfg['trip']['depart_from']} .. {cfg['trip']['depart_to']} "
          f"| {_nmin}-{_nmax} nights ...")
    rows = []
    for city, c in cities.items():
        if city.startswith("_"):
            continue
        try:
            res = run_once(cfg, cities, city, quiet=True)
        except SystemExit:
            continue
        for origin, oname, flights, grand in res:
            rows.append((grand, city, origin, oname))

    if not rows:
        print("No results (check your token and dates).")
        return
    rows.sort(key=lambda r: r[0])
    print(f"\n=== Cheapest totals across all destinations ({cur}) ===")
    for grand, city, origin, oname in rows[:20]:
        print(f"  {grand:9.2f}   {oname:9s} -> {city}")


def show_history(cfg, cities):
    db = db_init()
    dest_iata, _, label = resolve_destination(cfg["destination"], cities)
    print(f"Cheapest TOTAL ever seen per origin -> {label}:")
    for origin in cfg["origins"]:
        b = db_best(db, origin, dest_iata)
        oname = ORIGIN_NAMES.get(origin, origin)
        print(f"  {origin} {oname}: {b if b is not None else 'no data yet'}")
    db.close()


def export_csv(path):
    db = db_init()
    cur = db.execute("SELECT * FROM checks ORDER BY ts")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"Wrote {len(rows)} row(s) -> {path}")
    db.close()


# --------------------------------------------------------------------------- #
#  --demo sample data (shaped exactly like the real API responses)
# --------------------------------------------------------------------------- #
def _demo_response(url, params):
    if url == FLIGHT_API:
        origin, dest = params["origin"], params["destination"]
        base = {"IAS": 78, "RMO": 64, "KIV": 64}.get(origin, 70)
        month = params["departure_at"]            # 'YYYY-MM'
        # spread fares across the month so both the outbound and the (narrow) return
        # window each catch a candidate - exercises the full round-trip path.
        days = [("05", base, "W6", 0), ("09", base + 15, "W6", 0),
                ("12", base + 22, "FR", 1), ("20", base + 30, "FR", 1)]
        return {
            "success": True,
            "data": [
                {"origin": origin, "destination": dest, "price": price, "airline": air,
                 "transfers": tr, "departure_at": f"{month}-{d}T06:15:00+03:00",
                 "link": f"/search/{origin}{d}08{dest}1"}
                for d, price, air, tr in days
            ],
        }
    if url == HOTEL_API:
        return [
            {"hotelName": "Old Town Hostel", "stars": 2, "priceFrom": 116.0, "priceAvg": 140.0},
            {"hotelName": "City Center Apartments", "stars": 3, "priceFrom": 168.0, "priceAvg": 175.0},
        ]
    return {}


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def main():
    global DEMO
    ap = argparse.ArgumentParser(description="Cheapest flight + stay finder")
    ap.add_argument("--city", help="override destination (city, country, or IATA code)")
    ap.add_argument("--oneway", action="store_true", help="one-way only (skip the return leg)")
    ap.add_argument("--compare", action="store_true", help="rank every city in cities.json by total cost")
    ap.add_argument("--watch", action="store_true", help="keep checking on the interval in config.json")
    ap.add_argument("--list-cities", action="store_true", help="list destinations you can pick by name")
    ap.add_argument("--history", action="store_true", help="show the cheapest total seen so far")
    ap.add_argument("--export", metavar="FILE.csv", help="dump the whole price history to a CSV file")
    ap.add_argument("--demo", action="store_true", help="run on built-in sample data (no token needed)")
    args = ap.parse_args()

    DEMO = args.demo

    cfg = load_json(CONFIG_PATH)
    cities = load_json(CITIES_PATH)

    if args.oneway:
        cfg["trip"]["type"] = "oneway"

    if args.list_cities:
        for city, c in cities.items():
            if not city.startswith("_"):
                print(f"  {city:12s} {c['country']:18s} {c['airport']}")
        return

    if args.export:
        export_csv(args.export)
        return

    if args.history:
        show_history(cfg, cities)
        return

    if not DEMO and resolve_token(cfg).startswith("PUT_YOUR"):
        print("!! No API token yet. Paste your free Travelpayouts token into config.json,")
        print("   or set the TRAVELPAYOUTS_TOKEN environment variable.")
        print("   Get one at https://www.travelpayouts.com (Aviasales program -> API).")
        print("   Tip: run  python trip_scraper.py --demo  to see the output with sample data.")
        sys.exit(1)

    validate_config(cfg)

    if args.compare:
        run_compare(cfg, cities)
        return

    if args.watch:
        every = cfg.get("check_interval_minutes", 180)
        print(f"Watching every {every} min. Ctrl+C to stop.")
        while True:
            try:
                run_once(cfg, cities, args.city)
            except Exception as e:  # keep the watcher alive
                print(f"  ! check failed: {e}")
            print(f"\n...sleeping {every} min...")
            time.sleep(every * 60)
    else:
        run_once(cfg, cities, args.city)


if __name__ == "__main__":
    main()
