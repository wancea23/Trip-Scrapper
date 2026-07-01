"""
Extra price sources, all key-free:

- Ryanair fares API  -> real cheapest-per-day fares (where Ryanair flies). A second,
  ACCURATE flight source merged into the Travelpayouts (cached) results.
- Wizz Air API       -> real per-day fares from their timetable endpoint (the main
  carrier at Chisinau RMO; also flies from IAS/OTP/SCV/BCM).
- FlyOne API         -> real per-day fares from the Moldovan airline's fare-calendar
  endpoint (token scraped fresh from flyone.eu each run).
- zbor.md            -> "from X EUR" teaser prices per route from the local Moldovan
  agency's homepage; route-level (not date-specific), used as a cross-check + book link.
- HiSky calendar     -> real per-day fares scraped from their Videcom booking engine's
  server-rendered fare-calendar page (deeplink.aspx -> FlightCal.aspx).
- FlixBus API        -> real intercity bus prices, incl. direct Europe <-> Chisinau
  coaches (bus_home_options) and getting from home to the flight's airport.
- GROUND_EST         -> typical minibus prices for the short Chisinau -> airport hops that
  no site exposes cleanly (e.g. Chisinau -> Iasi), with a booking link.
"""

import re
import sys
from datetime import datetime, timedelta

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
RYANAIR = "https://services-api.ryanair.com/farfnd/v4"
FLIX = "https://global.api.flixbus.com"


def _months(date_from, date_to):
    a = datetime.strptime(date_from, "%Y-%m-%d")
    b = datetime.strptime(date_to, "%Y-%m-%d")
    out, cur = [], a.replace(day=1)
    while cur <= b:
        out.append(cur.strftime("%Y-%m-01"))
        cur = (cur.replace(day=28).toordinal() + 4)
        cur = datetime.fromordinal(cur).replace(day=1)
    return out


# --------------------------------------------------------------------------- #
#  Ryanair: real cheapest-per-day fares (only for routes Ryanair operates)
# --------------------------------------------------------------------------- #
def ryanair_leg_options(origin, dest, date_from, date_to, currency="EUR"):
    legs = []
    for m in _months(date_from, date_to):
        try:
            r = requests.get(f"{RYANAIR}/oneWayFares/{origin}/{dest}/cheapestPerDay",
                             params={"outboundMonthOfDate": m, "currency": currency.upper()},
                             headers={"User-Agent": UA}, timeout=20)
            if r.status_code != 200:
                continue
            fares = (r.json().get("outbound") or {}).get("fares") or []
        except (requests.RequestException, ValueError):
            continue
        for f in fares:
            if f.get("unavailable") or f.get("soldOut"):
                continue
            day = f.get("day")
            price = (f.get("price") or {}).get("value")
            if not day or price is None or not (date_from <= day <= date_to):
                continue
            legs.append({
                "price": round(float(price), 2), "date": day, "airline": "FR",
                "transfers": 0, "duration": 0, "route": [origin, dest], "stops": [],
                "link": (f"https://www.ryanair.com/gb/en/trip/flights/select"
                         f"?adults=1&dateOut={day}&originIata={origin}&destinationIata={dest}"),
                "source": "Ryanair",
            })
    return legs


# --------------------------------------------------------------------------- #
#  Currency: airline sites answer in their local currency (Wizz from RMO -> MDL)
# --------------------------------------------------------------------------- #
FX_FALLBACK = {"EUR": 1.0, "MDL": 20.1, "RON": 5.24, "HUF": 395.0, "PLN": 4.26,
               "CZK": 24.6, "GBP": 0.86, "USD": 1.17, "CHF": 0.93}
_FX = {}


def to_eur(amount, code):
    """Convert `amount` in currency `code` to EUR (live ECB-ish rates, static fallback)."""
    code = (code or "EUR").upper()
    if code == "EUR":
        return round(float(amount), 2)
    if not _FX:
        try:
            r = requests.get("https://open.er-api.com/v6/latest/EUR",
                             headers={"User-Agent": UA}, timeout=15)
            _FX.update(r.json().get("rates") or {})
        except (requests.RequestException, ValueError):
            pass
    rate = _FX.get(code) or FX_FALLBACK.get(code)
    return round(float(amount) / rate, 2) if rate else None


# --------------------------------------------------------------------------- #
#  Wizz Air: real per-day fares (timetable API; no key, just browser-ish headers)
# --------------------------------------------------------------------------- #
_WIZZ_VER = None


def _wizz_version():
    """The API version is baked into wizzair.com's homepage (be.wizzair.com/X.Y.Z)."""
    global _WIZZ_VER
    if _WIZZ_VER:
        return _WIZZ_VER
    try:
        r = requests.get("https://www.wizzair.com/en-gb",
                         headers={"User-Agent": UA}, timeout=20)
        m = re.search(r"be\.wizzair\.com/(\d+\.\d+\.\d+)", r.text)
        if m:
            _WIZZ_VER = m.group(1)
    except requests.RequestException:
        pass
    return _WIZZ_VER or "29.4.0"  # last seen 2026-07


def wizz_leg_options(origin, dest, date_from, date_to, currency="EUR"):
    """Real Wizz Air cheapest-per-day fares for the whole window in ONE call. Prices
    come back in the departure country's currency (MDL from Chisinau) -> EUR."""
    try:
        r = requests.post(
            f"https://be.wizzair.com/{_wizz_version()}/Api/search/timetable",
            json={"flightList": [{"departureStation": origin, "arrivalStation": dest,
                                  "from": date_from, "to": date_to}],
                  "priceType": "regular", "adultCount": 1, "childCount": 0, "infantCount": 0},
            headers={"User-Agent": UA, "Origin": "https://www.wizzair.com",
                     "Referer": "https://www.wizzair.com/"}, timeout=25)
        if r.status_code != 200:
            return []
        flights = r.json().get("outboundFlights") or []
    except (requests.RequestException, ValueError):
        return []
    legs = []
    for f in flights:
        p = f.get("price") or {}
        day = (f.get("departureDate") or "")[:10]
        if not p.get("amount") or not day or not (date_from <= day <= date_to):
            continue
        eur = to_eur(p["amount"], p.get("currencyCode"))
        if eur is None:
            continue
        legs.append({
            "price": eur, "date": day, "airline": "W6",
            "transfers": 0, "duration": 0, "route": [origin, dest], "stops": [],
            "link": (f"https://www.wizzair.com/en-gb/booking/select-flight"
                     f"/{origin}/{dest}/{day}/null/1/0/0/null"),
            "source": "Wizz Air",
        })
    return legs


# --------------------------------------------------------------------------- #
#  FlyOne: real per-day fares (fare-calendar API; bearer token lives in the homepage)
# --------------------------------------------------------------------------- #
_FLYONE_TOKEN = None


def _flyone_token():
    global _FLYONE_TOKEN
    if _FLYONE_TOKEN:
        return _FLYONE_TOKEN
    try:
        r = requests.get("https://flyone.eu/en/", headers={"User-Agent": UA}, timeout=20)
        m = re.search(r"CookieToken\('([^']+)'\)", r.text)
        if m:
            _FLYONE_TOKEN = m.group(1)
    except requests.RequestException:
        pass
    return _FLYONE_TOKEN


def flyone_leg_options(origin, dest, date_from, date_to, currency="EUR"):
    """Real FlyOne cheapest-per-day fares (Moldova's own airline, hub = RMO)."""
    tok = _flyone_token()
    if not tok:
        return []
    legs = []
    for month in _months(date_from, date_to):
        try:
            r = requests.post(
                "https://api4.flyone.eu/api/search/fare-calendar-schedule",
                json={"ipAddress": "", "currencyCode": currency.upper(),
                      "searchCriteria": {
                          "paxInfo": [{"paxType": 1, "paxKey": "pax1"}],
                          "journeyInfo": {"journeyType": 1, "routeInfo": [
                              {"depCity": origin, "arrCity": dest, "travelDate": month}]}},
                      "qsParams": [{"key": "", "value": ""}], "languageCode": "en-GB",
                      "currency": currency.upper(), "paxInfoId": 0, "reservationType": 0},
                headers={"User-Agent": UA, "Authorization": f"Bearer {tok}",
                         "Content-Type": "application/json; charset=utf-8"}, timeout=25)
            if r.status_code != 200:
                continue
            sched = r.json().get("flightSchedule") or []
        except (requests.RequestException, ValueError):
            continue
        for year in sched:
            for mo in (year.get("month") or []):
                for d in (mo.get("days") or []):
                    price = d.get("price")
                    if not price or price == "0" or not d.get("isFlightAvailable") or d.get("isSoldOut"):
                        continue
                    try:
                        day = (f"{int(year['year'])}-{int(mo['month']):02d}"
                               f"-{int(d['date']):02d}")
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not (date_from <= day <= date_to):
                        continue
                    legs.append({
                        "price": round(float(price), 2), "date": day, "airline": "5F",
                        "transfers": 0, "duration": 0, "route": [origin, dest], "stops": [],
                        "link": "https://bookings.flyone.eu/",
                        "source": "FlyOne",
                    })
    return legs


# --------------------------------------------------------------------------- #
#  HiSky: real per-day fares scraped from their Videcom booking calendar
# --------------------------------------------------------------------------- #
# The deeplink redirects to FlightCal.aspx, a server-rendered fare calendar with ~7
# day-tabs around the asked date: <a class="dayTab" data-newday="13 Jul 2026" ...>
# ... <div class="cal-tab3" data-original-amount="€89.99">. No API key, no JS needed.
HISKY_HUBS = {"RMO", "OTP"}  # only probe routes touching a HiSky base (keeps calls low)
_HISKY_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                 "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _hisky_deeplink(origin, dest, date):
    return ("https://booking.hisky.aero/VARS/Public/deeplink.aspx"
            f"?TripType=O&Origin1={origin}&Destination1={dest}&DepartureDate1={date}"
            "&Adult=1&Child=0&InfantLap=0&UserCurrency=EUR&UserLanguage=EN")


def hisky_leg_options(origin, dest, date_from, date_to, currency="EUR"):
    """Real HiSky cheapest-per-day fares. Each request shows ~7 days, so we step
    through the window (max 5 requests per direction)."""
    if origin not in HISKY_HUBS and dest not in HISKY_HUBS:
        return []
    a = datetime.strptime(date_from, "%Y-%m-%d")
    b = datetime.strptime(date_to, "%Y-%m-%d")
    by_date = {}
    probe = a + timedelta(days=3)
    for _ in range(5):
        if probe - timedelta(days=3) > b:
            break
        url = _hisky_deeplink(origin, dest, probe.strftime("%Y-%m-%d"))
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            html = r.text
        except requests.RequestException:
            break
        for chunk in html.split('class="dayTab"')[1:]:
            md = re.search(r'data-newday="(\d{1,2}) (\w{3}) (\d{4})"', chunk[:200])
            mp = re.search(r'data-original-amount="€?([\d]+(?:[.,]\d{1,2})?)"', chunk[:900])
            if not md or not mp:
                continue  # a day tab with no flight / no fare
            try:
                day = (f"{int(md.group(3))}-{_HISKY_MONTHS[md.group(2)]:02d}"
                       f"-{int(md.group(1)):02d}")
                price = float(mp.group(1).replace(",", ""))
            except (KeyError, ValueError):
                continue
            if not (date_from <= day <= date_to):
                continue
            if day not in by_date or price < by_date[day]["price"]:
                by_date[day] = {
                    "price": round(price, 2), "date": day, "airline": "H4",
                    "transfers": 0, "duration": 0, "route": [origin, dest], "stops": [],
                    "link": _hisky_deeplink(origin, dest, day),
                    "source": "HiSky",
                }
        probe += timedelta(days=7)
    return list(by_date.values())


# --------------------------------------------------------------------------- #
#  SkyUp (U5): real fares from their Nuxt site's air-availability JSON API
# --------------------------------------------------------------------------- #
# The site fronts an IBS availability API at skyup.aero/api/... . It needs a
# ?language=en query param (the proxy otherwise 404s) and a session cookie from the
# homepage. One request returns TravelDate +/- N days, so we step through the window.
_SKYUP_SESSION = None
_SKYUP_ROUTES = None
SKYUP_AVAIL = "https://skyup.aero/api/site/air-availability-for-site?language=en"
SKYUP_CONN = "https://skyup.aero/api/v2/site/airport-connections?language=en"


def _skyup_session():
    global _SKYUP_SESSION
    if _SKYUP_SESSION is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json",
                          "Referer": "https://skyup.aero/en/booking/flights"})
        try:
            s.get("https://skyup.aero/en", timeout=20)  # seed cookies
        except requests.RequestException:
            pass
        _SKYUP_SESSION = s
    return _SKYUP_SESSION


def _skyup_routes():
    """Set of (origin, dest) SkyUp actually flies, so we skip pointless requests."""
    global _SKYUP_ROUTES
    if _SKYUP_ROUTES is not None:
        return _SKYUP_ROUTES
    _SKYUP_ROUTES = set()
    try:
        r = _skyup_session().get(SKYUP_CONN, timeout=20)
        for c in (r.json().get("connections") or []):
            o = (c.get("originAirport") or {}).get("airportCode")
            d = (c.get("destinationAirport") or {}).get("airportCode")
            if o and d:
                _SKYUP_ROUTES.add((o, d))
    except (requests.RequestException, ValueError):
        pass
    return _SKYUP_ROUTES


def skyup_leg_options(origin, dest, date_from, date_to, currency="EUR"):
    """Real SkyUp cheapest-per-day fares (tax included). Steps through the window in
    7-day windows (TravelDate +/-3)."""
    routes = _skyup_routes()
    if routes and (origin, dest) not in routes:
        return []
    s = _skyup_session()
    a = datetime.strptime(date_from, "%Y-%m-%d")
    b = datetime.strptime(date_to, "%Y-%m-%d")
    by_date, probe = {}, a + timedelta(days=3)
    for _ in range(6):
        if probe - timedelta(days=3) > b:
            break
        payload = {"AvailabilitySearches": [{
            "Origin": origin, "Destination": dest,
            "TravelDate": probe.strftime("%Y-%m-%d"),
            "PositiveDaysOut": 3, "NegativeDaysOut": 3}],
            "PaxCountDetails": [{"PaxType": "ADULT", "PaxCount": 1}]}
        try:
            r = s.post(SKYUP_AVAIL, json=payload,
                       headers={"Content-Type": "application/json"}, timeout=30)
            odi = r.json().get("OriginDestinationInfo") or []
        except (requests.RequestException, ValueError):
            break
        for od in odi:
            for trip in (od.get("TripInfo") or []):
                segs = trip.get("SegmentInfo") or []
                if not segs:
                    continue
                day = ((segs[0].get("FlightIdentifierInfo") or {}).get("FlightDate") or "")[:10]
                prices = [p.get("TotalDisplayAmount") for p in (trip.get("PricingInfo") or [])
                          if p.get("TotalDisplayAmount")]
                if not day or not prices or not (date_from <= day <= date_to):
                    continue
                price = round(float(min(prices)), 2)
                stops = [s2.get("ArrivalInfo", {}).get("AirportCode")
                         for s2 in segs[:-1]]
                if day not in by_date or price < by_date[day]["price"]:
                    by_date[day] = {
                        "price": price, "date": day, "airline": "U5",
                        "transfers": len(segs) - 1, "duration": 0,
                        "route": [origin] + [s2.get("ArrivalInfo", {}).get("AirportCode") for s2 in segs],
                        "stops": [s for s in stops if s],
                        "link": ("https://skyup.aero/en/booking/flights"
                                 f"?from={origin}&to={dest}&departure={day}&adults=1"),
                        "source": "SkyUp",
                    }
        probe += timedelta(days=7)
    return list(by_date.values())


# --------------------------------------------------------------------------- #
#  zbor.md: local Moldovan agency teaser prices ("from X EUR" per route from Chisinau)
# --------------------------------------------------------------------------- #
_ZBOR = None
# zbor.md keys routes by metro/city code; map the few that differ from our airport codes
_ZBOR_ALIASES = {"OTP": "BUH", "BBU": "BUH"}


def zbor_offers():
    """dest code -> {price (EUR, teaser), name, link} scraped from zbor.md's homepage
    RSC payload (entries look like \"sku\":\"RMO-MIL-RO\",...,\"price\":\"40\")."""
    global _ZBOR
    if _ZBOR is not None:
        return _ZBOR
    _ZBOR = {}
    try:
        r = requests.get("https://www.zbor.md/", headers={"User-Agent": UA}, timeout=20)
        html = r.text
    except requests.RequestException:
        return _ZBOR
    pat = (r'\\"sku\\":\\"RMO-([A-Z]{3})-[A-Z]{2}\\",\\"name\\":\\"([^"\\]+)\\"'
           r'[^}]*?\\"price\\":\\"(\d+(?:\.\d+)?)\\"')
    for m in re.finditer(pat, html):
        dest, name, price = m.group(1), m.group(2), float(m.group(3))
        cur = _ZBOR.get(dest)
        if cur is None or price < cur["price"]:
            _ZBOR[dest] = {"price": round(price, 2), "name": name,
                           "link": "https://www.zbor.md/"}
    return _ZBOR


def zbor_offer(dest_iata):
    """Teaser 'from' price for Chisinau -> dest from zbor.md, or None."""
    offers = zbor_offers()
    return offers.get(dest_iata) or offers.get(_ZBOR_ALIASES.get(dest_iata, ""))


# --------------------------------------------------------------------------- #
#  FlixBus: real intercity bus prices
# --------------------------------------------------------------------------- #
def _flix_city_id(name):
    try:
        r = requests.get(f"{FLIX}/search/autocomplete/cities",
                         params={"q": name, "lang": "en"}, headers={"User-Agent": UA}, timeout=15)
        data = r.json()
        return data[0]["id"] if data else None
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


def flixbus_quote(from_city, to_city, date, currency="EUR"):
    """Cheapest FlixBus fare from_city -> to_city on `date` (YYYY-MM-DD), or None."""
    fid, tid = _flix_city_id(from_city), _flix_city_id(to_city)
    if not fid or not tid:
        return None
    dd = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
    try:
        r = requests.get(f"{FLIX}/search/service/v4/search", params={
            "from_city_id": fid, "to_city_id": tid, "departure_date": dd,
            "products": '{"adult":1}', "currency": currency.upper(), "locale": "en",
            "search_by": "cities", "include_after_midnight_rides": 1},
            headers={"User-Agent": UA}, timeout=20)
        res = ((r.json().get("trips") or [{}])[0]).get("results") or {}
    except (requests.RequestException, ValueError, IndexError):
        return None
    book = (f"https://shop.flixbus.com/search?departureCity={fid}&arrivalCity={tid}"
            f"&rideDate={dd}&adult=1")
    best = None
    for v in res.values():
        p = (v.get("price") or {}).get("total")
        # 0 = "fare needs seat selection" (some regional routes) - not a real price
        if not p or float(p) <= 0:
            continue
        if best is None or p < best["price"]:
            best = {"price": round(float(p), 2), "mode": "FlixBus",
                    "dep": (v.get("departure") or {}).get("date"),
                    "arr": (v.get("arrival") or {}).get("date"),
                    "book": book}
    return best


# --------------------------------------------------------------------------- #
#  Direct long-distance bus home <-> destination (the flight-free / return-fix option)
# --------------------------------------------------------------------------- #
def bus_home_options(city, date_out, date_back=None, home="Chisinau"):
    """Direct bus Chisinau <-> `city` (destination city name, e.g. 'Prague'):
    real FlixBus quotes for each direction + infobus.eu route links (more local
    carriers, prices only on their site). Returns None when no direction has a
    real quote. NOT the short MD->RO airport-feeder hops (see ground_to_airport)."""
    out = flixbus_quote(home, city, date_out)
    back = flixbus_quote(city, home, date_back) if date_back else None
    if not out and not back:
        return None
    slug = city.replace(" ", "-")
    return {"city": city, "out": out, "back": back,
            "infobus_out": f"https://infobus.eu/en/{home}/{slug}",
            "infobus_back": f"https://infobus.eu/en/{slug}/{home}"}


# --------------------------------------------------------------------------- #
#  Ground transport home(Chisinau) -> departure airport
# --------------------------------------------------------------------------- #
# Typical one-way minibus/bus price (EUR) for hops no site prices cleanly. Editable.
# price = typical ONE-WAY ticket, per person, in EUR (from real infobus.eu fares; e.g.
# Chisinau->Bucharest ~700 MDL ~= 36 EUR). The exact live price is on the booking link.
GROUND_EST = {
    "IAS": {"price": 13, "mode": "minibus", "hours": 3, "book": "https://infobus.eu/en/Chisinau/Iasi"},
    "OTP": {"price": 36, "mode": "bus",     "hours": 9, "book": "https://infobus.eu/en/Chisinau/Bucharest"},
    "SCV": {"price": 18, "mode": "minibus", "hours": 4, "book": "https://infobus.eu/en/Chisinau/Suceava"},
    "BCM": {"price": 20, "mode": "minibus", "hours": 5, "book": "https://infobus.eu/en/Chisinau/Bacau"},
    "RMO": {"price": 0,  "mode": "you are here", "hours": 0, "book": ""},
}
AIRPORT_CITY = {"IAS": "Iasi", "OTP": "Bucharest", "SCV": "Suceava", "BCM": "Bacau",
                "RMO": "Chisinau", "KIV": "Chisinau"}


def ground_to_airport(airport_iata, date, currency="EUR", home_city="Chisinau"):
    """Typical price + booking link to get from home (Chisinau) to the flight's departure
    airport. FlixBus list prices proved unreliable, and the Chisinau->Iasi minibus has no
    clean API, so this is a clear estimate with a place to book. Returns a dict or None."""
    if airport_iata in ("RMO", "KIV"):
        return None  # already in Chisinau (KIV = the airport's pre-2024 IATA code)
    est = GROUND_EST.get(airport_iata)
    if est and est["price"]:
        return {"to": AIRPORT_CITY.get(airport_iata, airport_iata), "price": est["price"],
                "mode": est["mode"], "hours": est["hours"], "book": est["book"]}
    return None


if __name__ == "__main__":
    print("Ryanair OTP->BER Aug:", len(ryanair_leg_options("OTP", "BER", "2026-08-01", "2026-08-31")), "fares")
    wl = wizz_leg_options("RMO", "BUD", "2026-08-01", "2026-08-31")
    print("Wizz RMO->BUD Aug:", len(wl), "fares", ("cheapest " + str(wl[0]["price"]) + " EUR " + wl[0]["date"]) if wl else "")
    fl = flyone_leg_options("RMO", "CDG", "2026-08-01", "2026-08-31")
    print("FlyOne RMO->CDG Aug:", len(fl), "fares", ("cheapest " + str(fl[0]["price"]) + " EUR " + fl[0]["date"]) if fl else "")
    hl = hisky_leg_options("RMO", "DUB", "2026-07-10", "2026-07-24")
    print("HiSky RMO->DUB Jul 10-24:", len(hl), "fares:", sorted((l["date"], l["price"]) for l in hl))
    su = skyup_leg_options("RMO", "HER", "2026-08-01", "2026-08-20")
    print("SkyUp RMO->HER Aug:", len(su), "fares", ("cheapest " + str(min(l["price"] for l in su)) + " EUR") if su else "")
    print("Bus home options Prague:", bus_home_options("Prague", "2026-08-10", "2026-08-17"))
    zo = zbor_offers()
    print("zbor.md offers:", len(zo), "routes;", {k: v["price"] for k, v in list(zo.items())[:6]})
    print("FlixBus Chisinau->Bucharest:", flixbus_quote("Chisinau", "Bucharest", "2026-08-01"))
    print("Ground to IAS:", ground_to_airport("IAS", "2026-08-01"))
    print("Ground to RMO:", ground_to_airport("RMO", "2026-08-01"))
