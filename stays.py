"""
Accommodation prices  -  scraped straight from Airbnb (no key, no signup).

Airbnb's public search page (/s/<city>/homes) server-renders all results, prices included,
inside a <script id="data-deferred-state-0"> JSON blob. We fetch that page with a normal
browser User-Agent and read the cheapest stay out of it - no API key, no headless browser.

If you ever DO add a RapidAPI key (config.json -> "rapidapi_key"), the old Airbnb/Booking
RapidAPI calls are kept as an automatic fallback for when the scrape comes back empty.

    python stays.py <city>      # probe: scrape a city and print what it found
"""

import os
import re
import sys
import json
import base64
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
AIRBNB_HOST = "airbnb19.p.rapidapi.com"        # only used if a RapidAPI key is set
BOOKING_HOST = "booking-com15.p.rapidapi.com"  # only used if a RapidAPI key is set


def get_key(cfg):
    return os.environ.get("RAPIDAPI_KEY") or cfg.get("rapidapi_key", "")


def have_key(cfg):
    k = get_key(cfg)
    return bool(k) and not k.startswith("PUT_YOUR")


def _walk(obj):
    """Yield every dict found anywhere in a nested JSON structure."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            yield o
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)


# --------------------------------------------------------------------------- #
#  Airbnb scrape (the no-key path)
# --------------------------------------------------------------------------- #
def _airbnb_url(location, check_in, check_out, adults, currency):
    return (f"https://www.airbnb.com/s/{quote(location)}/homes"
            f"?checkin={check_in}&checkout={check_out}&adults={max(1, adults)}"
            f"&currency={currency.upper()}&locale=en")


def _deferred_state(html):
    m = re.search(r'<script id="data-deferred-state-0"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        m = re.search(r'id="data-deferred-state"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _result_price(result):
    """The '€277 for 4 nights' total for one StaySearchResult."""
    for d in _walk(result):
        pl = d.get("primaryLine")
        if isinstance(pl, dict):
            label = pl.get("discountedPrice") or pl.get("price") or pl.get("accessibilityLabel", "")
            m = re.search(r"\d[\d,]*(?:\.\d+)?", str(label))
            if m:
                return float(m.group().replace(",", ""))
    return None


def _result_name(result):
    names = []
    for d in _walk(result):
        v = d.get("localizedStringWithTranslationPreference")
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
    if names:
        return max(names, key=len)
    for d in _walk(result):
        t = d.get("title")
        if isinstance(t, str) and t.strip() and t.lower() not in ("price details", "total"):
            return t.strip()
    return "Airbnb stay"


def _result_id(result):
    """The real /rooms/<id> is base64-encoded inside an id like
    'DemandStayListing:1431164709347816389' - decode it. (Plain numeric ids in the
    blob are pricing/photo entities and 404 on /rooms/.)"""
    for d in _walk(result):
        v = d.get("id")
        if isinstance(v, str) and len(v) > 12:
            try:
                m = re.search(r"StayListing:(\d+)", base64.b64decode(v).decode("utf-8", "ignore"))
                if m:
                    return m.group(1)
            except Exception:
                pass
    for d in _walk(result):  # fallback: a long numeric listingId field
        v = d.get("listingId")
        if isinstance(v, str) and v.isdigit() and len(v) >= 8:
            return v
    return None


def _result_type(result):
    """Stay type from the result title: 'Apartment in Prague 2' -> 'Apartment',
    'Hotel in Prague' -> 'Hotel', 'Room in Prague' -> 'Room', etc."""
    for d in _walk(result):
        t = d.get("title")
        if isinstance(t, str) and " in " in t:
            head = t.split(" in ")[0].strip()
            head = re.sub(r"\s+\d+$", "", head).strip()
            if head:
                return head
    return "Stay"


def airbnb_list(location, check_in, check_out, nights, currency, adults, limit=14):
    """Every stay the Airbnb search returns (homes, rooms, hotels, hostels, B&Bs...),
    cheapest first, each tagged with its type."""
    r = requests.get(_airbnb_url(location, check_in, check_out, adults, currency),
                     headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                     timeout=25)
    r.raise_for_status()
    data = _deferred_state(r.text)
    if not data:
        return []
    results = [d for d in _walk(data) if d.get("__typename") == "StaySearchResult"]
    stays, seen = [], set()
    for res in results:
        price = _result_price(res)
        if price is None:
            continue
        lid = _result_id(res)
        key = lid or (_result_name(res), price)
        if key in seen:           # Airbnb returns promoted + organic copies of the same listing
            continue
        seen.add(key)
        stays.append({
            "name": _result_name(res),
            "type": _result_type(res),
            "stars": 0,
            "stay_total": round(price, 2),
            "per_night": round(price / max(nights, 1), 2),
            "source": "Airbnb",
            "link": (f"https://www.airbnb.com/rooms/{lid}"
                     f"?check_in={check_in}&check_out={check_out}&adults={max(1, adults)}"
                     if lid else None),
        })
    stays.sort(key=lambda s: s["stay_total"])
    return stays[:limit]


# --------------------------------------------------------------------------- #
#  RapidAPI fallback (only runs if a key is configured AND the scrape was empty)
# --------------------------------------------------------------------------- #
def _rapid_get(host, path, params, key, timeout=25):
    r = requests.get(f"https://{host}{path}",
                     headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": host},
                     params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _biggest_list(data):
    best = []
    for d in _walk(data):
        for v in d.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and len(v) > len(best):
                best = v
    return best


def _deep_price(listing):
    best = None
    for d in _walk(listing):
        for k, v in d.items():
            if any(w in k.lower() for w in ("total", "gross", "amount", "price", "value")):
                try:
                    n = float(v)
                except (TypeError, ValueError):
                    continue
                if 5 < n < 100000:
                    best = n if best is None else min(best, n)
    return best


def _deep_name(listing):
    for key in ("name", "title", "hotelName", "listingName"):
        for d in _walk(listing):
            v = d.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return "?"


def _rapid_cheapest(location, check_in, check_out, nights, currency, adults, key):
    try:  # Airbnb via RapidAPI
        data = _rapid_get(AIRBNB_HOST, "/api/v1/searchPropertyByLocation",
                          {"location": location, "checkin": check_in, "checkout": check_out,
                           "adults": adults, "currency": currency.upper(), "totalRecords": 20}, key)
        best = _cheapest_rapid(_biggest_list(data), nights, "Airbnb")
        if best:
            return best
    except requests.RequestException:
        pass
    try:  # Booking via RapidAPI
        dest = _rapid_get(BOOKING_HOST, "/api/v1/hotels/searchDestination", {"query": location}, key)
        dl = _biggest_list(dest)
        if dl and (dl[0].get("dest_id") or dl[0].get("destId")):
            d0 = dl[0]
            hotels = _rapid_get(BOOKING_HOST, "/api/v1/hotels/searchHotels", {
                "dest_id": d0.get("dest_id") or d0.get("destId"),
                "search_type": str(d0.get("search_type") or d0.get("dest_type") or "CITY").upper(),
                "arrival_date": check_in, "departure_date": check_out,
                "adults": adults, "room_qty": 1, "currency_code": currency.upper(),
                "page_number": 1}, key)
            return _cheapest_rapid(_biggest_list(hotels), nights, "Booking")
    except requests.RequestException:
        pass
    return None


def _cheapest_rapid(listings, nights, source):
    best = None
    for it in listings:
        price = _deep_price(it)
        if price is None:
            continue
        if best is None or price < best["stay_total"]:
            best = {"name": _deep_name(it), "type": source, "stars": 0, "stay_total": round(price, 2),
                    "per_night": round(price / max(nights, 1), 2), "source": source, "link": None}
    return best


# --------------------------------------------------------------------------- #
#  Public entry point
# --------------------------------------------------------------------------- #
def cheapest_stay(location, check_in, nights, currency, cfg):
    """Cheapest place to stay + a list of options (homes/rooms/hotels/hostels...).
    Scrapes Airbnb first; falls back to RapidAPI Airbnb/Booking only if a key is set."""
    check_out = (datetime.strptime(check_in, "%Y-%m-%d") + timedelta(days=nights)).strftime("%Y-%m-%d")
    adults = int(cfg.get("stay", {}).get("adults") or cfg.get("trip", {}).get("travelers", 1))
    options = []
    try:
        options = airbnb_list(location, check_in, check_out, nights, currency, adults)
    except requests.RequestException:
        options = []
    if not options and have_key(cfg):
        s = _rapid_cheapest(location, check_in, check_out, nights, currency, adults, get_key(cfg))
        if s:
            return {**s, "type": s.get("type", "Stay"), "options": [s]}
    if not options:
        return None
    return {**options[0], "options": options}


def _probe(location):
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(os.path.join(here, "config.json"), encoding="utf-8"))
    ci = cfg["trip"]["depart_from"]
    nights = cfg["trip"]["nights"]
    co = (datetime.strptime(ci, "%Y-%m-%d") + timedelta(days=nights)).strftime("%Y-%m-%d")
    print(f"Scraping Airbnb for {location}  {ci} .. {co}  ({nights} nights)")
    s = cheapest_stay(location, ci, nights, cfg["currency"], cfg)
    if not s:
        print("none found")
        return
    print(f"Cheapest: {s['type']} - {s['name']}  {s['stay_total']} {cfg['currency'].upper()}")
    print(f"{len(s['options'])} options found:")
    for o in s["options"]:
        print(f"  {o['stay_total']:7.0f}  {o['type']:12s}  {o['name'][:45]}")


if __name__ == "__main__":
    _probe(sys.argv[1] if len(sys.argv) > 1 else "Prague")
