"""
Extra price sources, all key-free:

- Ryanair fares API  -> real cheapest-per-day fares (where Ryanair flies). A second,
  ACCURATE flight source merged into the Travelpayouts (cached) results.
- FlixBus API        -> real intercity bus prices (Chisinau -> a departure city), used
  to price getting from home to the flight's airport.
- GROUND_EST         -> typical minibus prices for the short Chisinau -> airport hops that
  no site exposes cleanly (e.g. Chisinau -> Iasi), with a booking link.
"""

import sys
from datetime import datetime

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
    best = None
    for v in res.values():
        p = (v.get("price") or {}).get("total")
        if p is None:
            continue
        if best is None or p < best["price"]:
            best = {"price": round(float(p), 2), "mode": "FlixBus",
                    "dep": (v.get("departure") or {}).get("date"),
                    "arr": (v.get("arrival") or {}).get("date"),
                    "book": "https://www.flixbus.com/"}
    return best


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
    "KIV": {"price": 0,  "mode": "you are here", "hours": 0, "book": ""},
}
AIRPORT_CITY = {"IAS": "Iasi", "OTP": "Bucharest", "SCV": "Suceava", "BCM": "Bacau", "KIV": "Chisinau"}


def ground_to_airport(airport_iata, date, currency="EUR", home_city="Chisinau"):
    """Typical price + booking link to get from home (Chisinau) to the flight's departure
    airport. FlixBus list prices proved unreliable, and the Chisinau->Iasi minibus has no
    clean API, so this is a clear estimate with a place to book. Returns a dict or None."""
    if airport_iata == "KIV":
        return None  # already in Chisinau
    est = GROUND_EST.get(airport_iata)
    if est and est["price"]:
        return {"to": AIRPORT_CITY.get(airport_iata, airport_iata), "price": est["price"],
                "mode": est["mode"], "hours": est["hours"], "book": est["book"]}
    return None


if __name__ == "__main__":
    print("Ryanair OTP->BER Aug:", len(ryanair_leg_options("OTP", "BER", "2026-08-01", "2026-08-31")), "fares")
    print("Ryanair OTP->BCN Aug:", len(ryanair_leg_options("OTP", "BCN", "2026-08-01", "2026-08-31")), "fares")
    print("FlixBus Chisinau->Bucharest:", flixbus_quote("Chisinau", "Bucharest", "2026-08-01"))
    print("Ground to IAS:", ground_to_airport("IAS", "2026-08-01"))
    print("Ground to OTP:", ground_to_airport("OTP", "2026-08-01"))
