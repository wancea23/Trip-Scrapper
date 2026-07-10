"""
Trip-Scrapper web UI  -  a small local browser app to pick a country/city and find
the cheapest flights, reusing all the logic from trip_scraper.py.

Run it:
    python web.py

It starts a local server and opens http://127.0.0.1:8765 in your browser.
Nothing is installed and nothing leaves your machine except the same flight-price API
calls the command-line tool already makes. Press Ctrl+C in the terminal to stop it.
"""

import json
import os
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

import trip_scraper as ts
import bot as tgbot  # Telegram pairing + price hunts

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
PORT = 8765

# load config + cities once; re-read cities each request so edits show up without restart
CFG = ts.load_json(ts.CONFIG_PATH)


def _cities():
    return ts.load_json(ts.CITIES_PATH)


def cities_payload():
    """Group cities by country for the picker."""
    cities = _cities()
    by_country = {}
    for name, c in cities.items():
        if name.startswith("_"):
            continue
        by_country.setdefault(c["country"], []).append({"city": name, "airport": c["airport"]})
    out = [{"country": k, "cities": sorted(v, key=lambda x: x["city"])}
           for k, v in sorted(by_country.items())]
    return {"countries": out, "currency": CFG["currency"].upper(),
            "origins": CFG.get("origins", ["IAS", "RMO"]),
            "extras": CFG.get("extras", []),
            "defaults": {"depart_from": CFG["trip"]["depart_from"],
                         "depart_to": CFG["trip"]["depart_to"],
                         "nights_min": ts.nights_bounds(CFG["trip"])[0],
                         "nights_max": ts.nights_bounds(CFG["trip"])[1],
                         "travelers": CFG["trip"].get("travelers", 1)}}


def _cfg_for(body):
    """Build a config dict from the base config overridden by the form values.
    Raises ValueError on bad input -> the handler answers 400 with the message."""
    c = json.loads(json.dumps(CFG))  # deep copy
    t = c["trip"]
    t["depart_from"] = body.get("depart_from") or t["depart_from"]
    t["depart_to"] = body.get("depart_to") or t["depart_to"]
    try:
        a = datetime.strptime(t["depart_from"], "%Y-%m-%d")
        b = datetime.strptime(t["depart_to"], "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError("dates must look like 2026-08-01")
    if a > b:
        raise ValueError("the departure window starts after it ends - swap the dates")
    try:
        t["nights_min"] = int(body.get("nights_min") or t.get("nights_min") or t.get("nights") or 3)
        t["nights_max"] = max(t["nights_min"], int(body.get("nights_max") or t.get("nights_max") or t["nights_min"]))
        t["travelers"] = int(body.get("travelers") or 1)
    except (TypeError, ValueError):
        raise ValueError("nights and travelers must be whole numbers")
    if t["nights_min"] < 1:
        raise ValueError("nights must be at least 1")
    t["travelers"] = max(1, t["travelers"])
    t["type"] = "oneway" if str(body.get("type", "")).lower() == "oneway" else "return"
    return c


def _ground(origin, out_date, travelers, legs):
    """Home (Chisinau) -> the flight's airport, scaled by travelers and legs (there & back)."""
    g = ts.sources.ground_to_airport(origin, out_date)
    if not g:
        return None
    return {**g, "total": round(g["price"] * travelers * legs, 2), "legs": legs}


def _one(origin, dest_iata, c, include_real=True):
    """Run one origin -> destination flight lookup, return a JSON-friendly dict.
    Any failure comes back as a per-origin error entry - one origin's network
    hiccup must not 500 the whole search."""
    oname = ts.ORIGIN_NAMES.get(origin, origin)
    try:
        f = ts.fetch_flights(origin, dest_iata, c, include_real=include_real)
    except ts.RouteUnavailable as e:
        return {"origin": origin, "name": oname, "error": str(e)}
    except Exception as e:
        return {"origin": origin, "name": oname, "error": f"lookup failed: {e}"}
    if not f:
        return {"origin": origin, "name": oname, "error": "no flights found for these dates"}
    out, back = f["out"], f["back"]
    return {
        "origin": origin, "name": oname,
        "out": out, "back": back,
        "out_options": f["out_options"], "back_options": f["back_options"],
        "flight_total": f["flight_total"],
        "actual_nights": f["actual_nights"], "travelers": f["travelers"],
        "one_way": f["one_way"], "booking_link": f["booking_link"],
        "bag_total": f["bag_total"], "bag_airline": f["bag_airline"], "bag_legs": f["bag_legs"],
        "ground": _ground(origin, out["date"], f["travelers"], f["bag_legs"]),
        "agency": f.get("agency"),
        # so the front end can derive the stay dates / display them
        "out_date": out["date"], "back_date": back["date"] if back else None,
    }


# --------------------------------------------------------------------------- #
#  Transport modes: fly / bus / both, per direction. The '100% a way home' rule.
# --------------------------------------------------------------------------- #
def _modes(val):
    """Parse a transport-mode choice into the set of allowed carriers.
    'plane'/'flight' -> flights only; 'bus' -> bus only; anything else
    ('both'/'mixed'/'either'/empty) -> both. Empty defaults to both, which keeps
    the pre-modes behaviour: show flights and fall back to the bus."""
    v = str(val or "").strip().lower()
    if v in ("plane", "flight", "flights", "air", "fly"):
        return {"plane"}
    if v in ("bus", "coach", "flixbus"):
        return {"bus"}
    return {"plane", "bus"}


def _mode_name(modes):
    if modes == {"plane"}:
        return "plane"
    if modes == {"bus"}:
        return "bus"
    return "both"


def _apply_modes(r, bus, one_way, out_modes, back_modes, dest_iata):
    """Filter + re-price ONE flight card for the chosen transport modes.
    Returns the (mutated) card, or None if it should be HIDDEN.

    A flight card always flies the OUTBOUND, so it needs 'plane' in out_modes.
    On a return trip it must also have an acceptable RETURN - a return flight
    (plane) or, failing that, the direct bus home (bus) - otherwise it's hidden.
    That is the '100% needs a way back' rule. Fares/bag/ground/booking link are
    recomputed for the legs actually travelled, so a bus-return card isn't billed
    a phantom return flight."""
    tv = r["travelers"]
    out, back = r["out"], r["back"]
    if "plane" not in out_modes:
        return None  # user isn't flying out -> this flight card doesn't apply

    def bag_for(legs):
        pp = sum(ts.airline_bag_leg(l["airline"]) for l in legs)
        names = []
        for l in legs:
            nm = ts.airline_name(l["airline"])
            if nm not in names:
                names.append(nm)
        return round(pp * tv, 2), " + ".join(names)

    if one_way:
        r["return_mode"] = None
        r["flight_total"] = round(out["price"] * tv, 2)
        r["bag_total"], r["bag_airline"] = bag_for([out])
        r["bag_legs"] = 1
        r["bus_back"] = 0
        r["booking_link"] = ts.aviasales_link(r["origin"], dest_iata, out["date"], None, tv)
    else:
        back_plane = ("plane" in back_modes) and (back is not None)
        back_bus = ("bus" in back_modes) and bool(bus and bus.get("back"))
        if not (back_plane or back_bus):
            return None  # no acceptable way home -> hide this destination
        if back_plane:  # a real return flight beats the long bus when both are allowed
            r["return_mode"] = "flight"
            r["flight_total"] = round((out["price"] + back["price"]) * tv, 2)
            r["bag_total"], r["bag_airline"] = bag_for([out, back])
            r["bag_legs"] = 2
            r["bus_back"] = 0
            r["booking_link"] = ts.aviasales_link(r["origin"], dest_iata, out["date"], back["date"], tv)
        else:  # ride home by the direct bus
            r["return_mode"] = "bus"
            r["flight_total"] = round(out["price"] * tv, 2)
            r["bag_total"], r["bag_airline"] = bag_for([out])
            r["bag_legs"] = 1
            r["bus_back"] = round(bus["back"]["price"] * tv, 2)
            r["booking_link"] = ts.aviasales_link(r["origin"], dest_iata, out["date"], None, tv)
            # keep the flight-derived trip length: the destination bus is one shared
            # quote (priced on the cheapest card's dates), so its dep date can't be
            # pinned to THIS card's outbound without going negative on later departures
    # ground transport home->airport scales with the flight legs actually flown
    r["ground"] = _ground(r["origin"], out["date"], tv, r["bag_legs"])
    return r


def _bus_card(bus, label, one_way, travelers, nights, extras_def, out_date, back_date):
    """Shape the direct Chisinau<->destination bus as a normal result card, so the
    front end ranks and totals it exactly like a flight (transport in flight_total)."""
    def leg(q, frm, to):
        return {"price": q["price"], "date": (q.get("dep") or "")[:10] or out_date,
                "airline": "FlixBus", "transfers": 0, "duration": 0,
                "route": [frm, to], "stops": [], "link": q.get("book"),
                "source": "FlixBus", "dep": q.get("dep")}
    out_leg = leg(bus["out"], "Chisinau", label)
    back_leg = leg(bus["back"], label, "Chisinau") if (not one_way and bus.get("back")) else None
    if back_leg and not back_leg["date"]:
        back_leg["date"] = back_date
    transport = round((bus["out"]["price"] + (back_leg["price"] if back_leg else 0)) * travelers, 2)
    items, etot = ts.compute_extras(extras_def, travelers, nights)
    return {
        "origin": "BUS", "name": "Direct bus", "bus_only": True,
        "out": out_leg, "back": back_leg,
        "out_options": [out_leg], "back_options": [back_leg] if back_leg else [],
        "flight_total": transport, "travelers": travelers,
        "actual_nights": None if one_way else nights,
        "one_way": one_way, "return_mode": None if one_way else "bus",
        "booking_link": bus["out"].get("book"),
        "bag_total": 0, "bag_airline": "", "bag_legs": 0,
        "ground": None, "agency": None, "bus_back": 0,
        "extras": items, "extras_total": etot,
        "out_date": out_leg["date"] or None,
        "back_date": back_leg["date"] if back_leg else None,
    }


def _pick_flight(iata, c, origins, one_way, out_modes, back_modes, bus, include_real=False):
    """Cheapest VALID flight card across origins for one destination, honouring the
    modes + the '100% a way home' rule. Returns a re-priced card or None."""
    if "plane" not in out_modes:
        return None
    best = None
    for origin in origins:
        r = _one(origin, iata, c, include_real=include_real)
        if "flight_total" not in r:
            continue
        kept = _apply_modes(r, bus, one_way, out_modes, back_modes, iata)
        if kept is None:
            continue
        eff = kept["flight_total"] + (kept.get("bus_back") or 0)
        if best is None or eff < best[0]:
            best = (eff, kept)
    return best[1] if best else None


def do_search(body):
    cities = _cities()
    c = _cfg_for(body)
    dest_iata, hotel_loc, label = ts.resolve_destination(body["destination"], cities)
    origins = body.get("origins") or c["origins"]
    one_way = c["trip"]["type"] == "oneway"
    out_modes = _modes(body.get("out_mode"))
    back_modes = _modes(body.get("back_mode"))
    extras_def = body.get("extras") or c.get("extras", [])
    travelers = c["trip"].get("travelers", 1)
    nmin = ts.nights_bounds(c["trip"])[0]
    with_stay = body.get("with_stay", True)

    raw = [_one(o, dest_iata, c) for o in origins]

    # itemise extra costs (transfer, tax...) per result, scaled by travelers/nights
    for r in raw:
        if "flight_total" in r:
            items, etot = ts.compute_extras(extras_def, travelers, r.get("actual_nights") or nmin)
            r["extras"], r["extras_total"] = items, etot

    # direct bus Chisinau <-> destination, needed BOTH to price a bus outbound/return
    # and to decide whether a place has any way back at all. Fetch it once, on the
    # cheapest flight's dates (or the search window when nothing flies).
    flying_raw = [r for r in raw if "flight_total" in r]
    if flying_raw:
        ref = min(flying_raw, key=lambda r: r["flight_total"])
        ref_out, ref_nights = ref["out_date"], (ref.get("actual_nights") or nmin)
    else:
        ref_out, ref_nights = c["trip"]["depart_from"], nmin
    ref_back = (datetime.strptime(ref_out, "%Y-%m-%d")
                + timedelta(days=ref_nights)).strftime("%Y-%m-%d")
    try:
        bus = ts.sources.bus_home_options(hotel_loc, ref_out, None if one_way else ref_back)
    except Exception:
        bus = None

    # apply the transport-mode filter + re-pricing to every flight card
    results = []
    for r in raw:
        if "error" in r:
            if "plane" in out_modes:  # a failed flight lookup is only worth showing
                results.append(r)     # when the user actually wants to fly out
            continue
        kept = _apply_modes(r, bus, one_way, out_modes, back_modes, dest_iata)
        if kept is not None:
            results.append(kept)

    # a pure-bus journey (bus there, bus back) as its own card, when the user allows
    # the bus for the outbound (and, on a return trip, for the way home)
    bus_out_ok = bool(bus and bus.get("out")) and ("bus" in out_modes)
    bus_back_ok = one_way or (bool(bus and bus.get("back")) and ("bus" in back_modes))
    bus_card_added = False
    if bus_out_ok and bus_back_ok:
        results.append(_bus_card(bus, label, one_way, travelers, ref_nights,
                                 extras_def, ref_out, ref_back))
        bus_card_added = True

    # stays: one live Airbnb scrape per DATE WINDOW, shared across cards that share it
    stay, stay_dates, stay_cache = None, None, {}
    for r in results:
        if not r.get("out_date"):
            continue
        nights = r.get("actual_nights") or nmin
        check_in = r["out_date"]
        check_out = (datetime.strptime(check_in, "%Y-%m-%d")
                     + timedelta(days=nights)).strftime("%Y-%m-%d")
        r["stay_dates"] = {"check_in": check_in, "check_out": check_out, "nights": nights}
        r["stay"] = None
        if with_stay:
            key = (check_in, nights)
            if key not in stay_cache:
                try:
                    stay_cache[key] = ts.fetch_stay(hotel_loc, check_in, nights, c)
                except Exception:
                    stay_cache[key] = None
            r["stay"] = stay_cache[key]

    # headline stay = the cheapest FULL trip's stay (flights + ground + bus + stay + extras)
    def full_total(r):
        st = r["stay"]["stay_total"] if r.get("stay") else 0
        ground = r["ground"]["total"] if r.get("ground") else 0
        return r["flight_total"] + ground + st + (r.get("bus_back") or 0) + r.get("extras_total", 0)

    priced = [r for r in results if "flight_total" in r]
    if priced:
        best = min(priced, key=full_total)
        stay, stay_dates = best.get("stay"), best.get("stay_dates")

    return {"label": label, "currency": c["currency"].upper(),
            "results": results, "stay": stay, "stay_dates": stay_dates, "bus": bus,
            "bus_card": bus_card_added, "one_way": one_way,
            "out_mode": _mode_name(out_modes), "back_mode": _mode_name(back_modes),
            "no_trip": len(priced) == 0, "have_key": ts.stays.have_key(c)}


def do_multi(body):
    """Compare only the places the user selected (cities and/or whole countries):
    cheapest round-trip flights per city across the chosen origins, so the front
    end can show the overall winner and the cheapest per country. Cached API only
    (a selection can expand to many cities); the single-city search still gets
    real scraped fares when the user drills in."""
    cities = _cities()
    c = _cfg_for(body)
    origins = body.get("origins") or c["origins"]
    targets, unknown = {}, []
    for p in (body.get("places") or []):
        kind, name = tgbot.resolve_place(str(p), cities)
        if kind == "city":
            targets[name] = cities[name]
        elif kind == "country":
            for ct, info in cities.items():
                if not ct.startswith("_") and info.get("country") == name:
                    targets[ct] = info
        elif kind == "airport":
            targets[name] = {"country": name, "airport": name, "hotel_location": name}
        else:
            unknown.append(p)
    if not targets:
        raise ValueError("select at least one known city or country")
    travelers = c["trip"].get("travelers", 1)
    nmin = ts.nights_bounds(c["trip"])[0]
    extras_def = body.get("extras") or c.get("extras", [])
    include_bag = bool(body.get("include_bag"))  # bag is opt-in, like the UI checkbox
    one_way = c["trip"]["type"] == "oneway"
    out_modes = _modes(body.get("out_mode"))
    back_modes = _modes(body.get("back_mode"))
    # the bus is only needed when a direction can use it - skip the FlixBus calls
    # for a plain fly-there/fly-back selection
    need_bus = ("bus" in out_modes) or (not one_way and "bus" in back_modes)
    back_est = (datetime.strptime(c["trip"]["depart_from"], "%Y-%m-%d")
                + timedelta(days=nmin)).strftime("%Y-%m-%d")
    rows = []
    for name, info in targets.items():
        loc = info.get("hotel_location", name)
        city_bus = None
        if need_bus:
            try:
                city_bus = ts.sources.bus_home_options(
                    loc, c["trip"]["depart_from"], None if one_way else back_est)
            except Exception:
                city_bus = None
        # cheapest VALID flight trip (honours modes + the way-home rule); the direct
        # bus only stands in for the whole trip when the user won't fly out
        best = _pick_flight(info["airport"], c, origins, one_way, out_modes,
                            back_modes, city_bus, include_real=False) if "plane" in out_modes else None
        if best is None and "bus" in out_modes and city_bus and city_bus.get("out") \
                and (one_way or city_bus.get("back")):
            best = _bus_card(city_bus, name, one_way, travelers, nmin, extras_def,
                             c["trip"]["depart_from"], back_est)
        if best is None:
            rows.append({"total": None, "flights": None, "city": name,
                         "country": info.get("country", "?")})
            continue
        # the WHOLE price, same math as the single-city page: flights/bus + bag +
        # ground + stay (live Airbnb, matched to this trip's dates) + extras
        nights = best["actual_nights"] or nmin
        stay = None
        if body.get("with_stay", True):
            try:
                stay = ts.fetch_stay(loc, best["out_date"], nights, c)
            except Exception:
                stay = None
        stay_total = stay["stay_total"] if stay else 0
        ground = best["ground"]["total"] if best.get("ground") else 0
        extras_total = ts.compute_extras(extras_def, travelers, nights)[1]
        grand = round(best["flight_total"] + (best["bag_total"] if include_bag else 0)
                      + ground + (best.get("bus_back") or 0) + stay_total + extras_total, 2)
        # show the way home honestly: the return-flight date only when we're actually
        # flying back; a bus return (or the pure-bus card) shows its own bus date / none
        if best.get("bus_only"):
            back_date = best["back"]["date"] if best.get("back") else None
        elif best.get("return_mode") == "bus":
            back_date = None
        else:
            back_date = best["back"]["date"] if best.get("back") else None
        rows.append({"total": grand, "flights": best["flight_total"],
                     "bag": best["bag_total"], "ground": ground,
                     "bus_back": best.get("bus_back") or 0,
                     "return_mode": best.get("return_mode"),
                     "stay": stay_total, "stay_name": stay["name"] if stay else None,
                     "extras": extras_total, "city": name,
                     "country": info.get("country", "?"), "origin": best["origin"],
                     "name": best["name"], "out": best["out"]["date"],
                     "back": back_date,
                     "nights": best["actual_nights"], "link": best["booking_link"]})
    rows.sort(key=lambda x: x["total"] if x["total"] is not None else float("inf"))
    return {"currency": c["currency"].upper(), "rows": rows, "unknown": unknown,
            "one_way": one_way, "out_mode": _mode_name(out_modes),
            "back_mode": _mode_name(back_modes)}


def do_compare(body):
    cities = _cities()
    c = _cfg_for(body)
    origins = body.get("origins") or c["origins"]
    travelers = c["trip"].get("travelers", 1)
    nmin = ts.nights_bounds(c["trip"])[0]
    extras_def = body.get("extras") or c.get("extras", [])
    include_bag = bool(body.get("include_bag"))  # bag is opt-in, like the UI checkbox
    with_stay = body.get("with_stay", True)
    # ranking every city by bus would mean FlixBus calls x129 cities - too slow and
    # ban-prone. So compare-all ranks round-trip FLIGHTS only, but still enforces the
    # way-home rule: in return mode a city with no return flight is dropped. Bus /
    # mixed options still show when you open a place (that runs the full do_search).
    one_way = c["trip"]["type"] == "oneway"
    stay_cache = {}   # hotel_location -> stay dict; scrape live Airbnb once per city
    label_cache = {}  # city name -> display label (e.g. "Bucharest (OTP)")
    rows = []
    for name, info in cities.items():
        if name.startswith("_"):
            continue
        for origin in origins:
            # cached API only: ~129 cities x 5 airline scrapers would take forever
            # and likely get the server's IP blocked
            r = _one(origin, info["airport"], c, include_real=False)
            if "flight_total" not in r:
                continue
            # way-home rule: no bus here (too many cities), so a return trip needs a
            # real return flight or it's dropped
            if _apply_modes(r, None, one_way, {"plane"}, {"plane"}, info["airport"]) is None:
                continue
            nights = r["actual_nights"] or nmin
            # full trip = flights + (optional bag) + ground transport + stay + extras,
            # the same math the single-city and multi-compare pages use
            loc = info.get("hotel_location", name)
            stay, stay_ci, stay_n = None, r["out"]["date"], nights
            if with_stay:
                if loc not in stay_cache:
                    try:
                        stay_cache[loc] = (ts.fetch_stay(loc, r["out"]["date"], nights, c),
                                           r["out"]["date"], nights)
                    except Exception:
                        stay_cache[loc] = (None, r["out"]["date"], nights)
                # the cache is per city (one Airbnb scrape each), so a later origin's
                # row may reuse a stay scraped for the FIRST origin's dates - keep the
                # dates it was really scraped for, so the drill-down never lies
                stay, stay_ci, stay_n = stay_cache[loc]
            stay_total = stay["stay_total"] if stay else 0
            ground = r["ground"]["total"] if r.get("ground") else 0
            items, extras_total = ts.compute_extras(extras_def, travelers, nights)
            r["extras"], r["extras_total"] = items, extras_total  # for the drill-down detail
            grand = round(r["flight_total"] + (r["bag_total"] if include_bag else 0)
                          + ground + stay_total + extras_total, 2)
            if name not in label_cache:
                try:
                    label_cache[name] = ts.resolve_destination(name, cities)[2]
                except Exception:
                    label_cache[name] = name
            check_out = (datetime.strptime(stay_ci, "%Y-%m-%d")
                         + timedelta(days=stay_n)).strftime("%Y-%m-%d")
            sd = {"check_in": stay_ci, "check_out": check_out, "nights": stay_n}
            r["stay"], r["stay_dates"] = stay, sd  # per-card stay, like /api/search
            # the FULL page for this exact row (same origin/dates/fares/stay), so the
            # UI dropdown renders it directly - no second search, numbers always match
            detail = {"label": label_cache[name], "results": [r], "stay": stay,
                      "stay_dates": sd, "bus": None}
            rows.append({"total": grand, "flights": r["flight_total"],
                         "bag": r["bag_total"], "ground": ground,
                         "stay": stay_total, "stay_name": stay["name"] if stay else None,
                         "extras": extras_total, "city": name,
                         "country": info["country"], "origin": origin,
                         "name": r["name"], "out": r["out"]["date"],
                         "back": r["back"]["date"] if r["back"] else None,
                         "nights": r["actual_nights"], "detail": detail})
    rows.sort(key=lambda x: x["total"])
    out_mode = _mode_name(_modes(body.get("out_mode")))
    back_mode = _mode_name(_modes(body.get("back_mode")))
    return {"currency": c["currency"].upper(), "rows": rows, "one_way": one_way,
            "out_mode": out_mode, "back_mode": back_mode,
            "flights_only": (out_mode != "plane" or back_mode != "plane")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet console
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # always serve the current UI
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        # each browser sends its own localStorage id as ?bid=... so the Telegram
        # panel scopes to that visitor's paired chat, not a shared global list
        bid = (parse_qs(parsed.query).get("bid") or [None])[0]
        if path in ("/", "/index.html"):
            with open(INDEX, "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        elif path == "/api/cities":
            self._send(200, cities_payload())
        elif path == "/api/tg/status":
            self._send(200, tgbot.status(CFG, bid))
        elif path == "/api/tg/hunts":
            self._send(200, tgbot.hunts_payload(bid))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            bid = body.get("browser_id")  # the caller's browser identity (see do_GET)
            if self.path == "/api/search":
                self._send(200, do_search(body))
            elif self.path == "/api/compare":
                self._send(200, do_compare(body))
            elif self.path == "/api/multi":
                self._send(200, do_multi(body))
            elif self.path == "/api/tg/code":
                self._send(200, tgbot.make_code(CFG, bid))
            elif self.path == "/api/tg/hunt":
                self._send(200, tgbot.add_hunts_ui(body.get("places", ""),
                                                   body.get("price"), CFG,
                                                   body.get("metric", "total"),
                                                   bool(body.get("include_bag")), bid))
            elif self.path == "/api/tg/hunt_delete":
                self._send(200, tgbot.remove_hunt_ui(body.get("id"), bid))
            else:
                self._send(404, {"error": "not found"})
        except ValueError as e:  # bad input (dates, destination, malformed JSON)
            self._send(400, {"error": str(e)})
        except Exception as e:  # never crash the server on a bad request
            self._send(500, {"error": str(e)})


def _public_url():
    """The app's own public URL on a cloud host, if we can find it. Render exposes
    RENDER_EXTERNAL_URL; Railway exposes RAILWAY_PUBLIC_DOMAIN; PUBLIC_URL overrides."""
    url = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL")
        if dom:
            url = dom if dom.startswith("http") else "https://" + dom
    return (url or "").rstrip("/")


def _keep_awake():
    """Free hosts (Render/Railway) sleep after ~15 min idle, which stops the Telegram
    bot from polling and sending price-drop alerts. Ping our own public URL every 10
    min so the dyno stays awake. No-op when we don't know a public URL (e.g. local)."""
    url = _public_url()
    if not url:
        return

    def loop():
        while True:
            time.sleep(600)
            try:
                urllib.request.urlopen(url + "/api/cities", timeout=20).read(64)
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True).start()
    print(f"Keep-alive: pinging {url} every 10 min so the free host doesn't sleep (bot stays live)")


def main():
    if ts.resolve_token(CFG).startswith("PUT_YOUR") and not os.environ.get("TRAVELPAYOUTS_TOKEN"):
        print("!! No API token (config.json or TRAVELPAYOUTS_TOKEN env) - searches will fail.")
    # Cloud hosts (Render/Railway/Fly) set PORT and expect us to bind 0.0.0.0; locally we
    # bind localhost and pop open the browser.
    cloud = "PORT" in os.environ
    port = int(os.environ.get("PORT", PORT))
    host = "0.0.0.0" if cloud else "127.0.0.1"
    print(f"Trip Finder UI running on {host}:{port}")
    started = tgbot.start_in_background(CFG)  # no-op until a bot token is configured
    if cloud and not started:
        print("!! Telegram bot OFF on this host: no token. Set TELEGRAM_BOT_TOKEN in the "
              "host's Environment tab (see DEPLOY.md), then redeploy.")
    if cloud:
        _keep_awake()  # keep the free dyno (and the bot) alive between visits
    if not cloud:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        print("Opening your browser... (press Ctrl+C here to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
