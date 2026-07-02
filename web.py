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
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def do_search(body):
    cities = _cities()
    c = _cfg_for(body)
    dest_iata, hotel_loc, label = ts.resolve_destination(body["destination"], cities)
    origins = body.get("origins") or c["origins"]
    results = [_one(o, dest_iata, c) for o in origins]

    # itemise extra costs (transfer, tax, bus...) per result, scaled by travelers/nights
    extras_def = body.get("extras") or c.get("extras", [])
    travelers = c["trip"].get("travelers", 1)
    nmin = ts.nights_bounds(c["trip"])[0]
    for r in results:
        if "flight_total" in r:
            items, etot = ts.compute_extras(extras_def, travelers, r.get("actual_nights") or nmin)
            r["extras"], r["extras_total"] = items, etot

    # match the stay to the CHEAPEST flight's real dates + nights
    stay, stay_dates = None, None
    flying = [r for r in results if "flight_total" in r]
    if body.get("with_stay", True) and flying:
        best = min(flying, key=lambda r: r["flight_total"])
        check_in = best["out_date"]
        nights = best["actual_nights"] or ts.nights_bounds(c["trip"])[0]
        check_out = (datetime.strptime(check_in, "%Y-%m-%d")
                     + timedelta(days=nights)).strftime("%Y-%m-%d")
        stay = ts.fetch_stay(hotel_loc, check_in, nights, c)
        stay_dates = {"check_in": check_in, "check_out": check_out, "nights": nights}

    # direct long-distance bus Chisinau <-> destination (real FlixBus quotes),
    # both a flight alternative and the return fix when no return flight exists
    bus = None
    if flying:
        best = min(flying, key=lambda r: r["flight_total"])
        nights = best["actual_nights"] or nmin
        date_back = (datetime.strptime(best["out_date"], "%Y-%m-%d")
                     + timedelta(days=nights)).strftime("%Y-%m-%d")
        try:
            bus = ts.sources.bus_home_options(hotel_loc, best["out_date"], date_back)
        except Exception:
            bus = None
    return {"label": label, "currency": c["currency"].upper(),
            "results": results, "stay": stay, "stay_dates": stay_dates, "bus": bus,
            "have_key": ts.stays.have_key(c)}


def do_compare(body):
    cities = _cities()
    c = _cfg_for(body)
    origins = body.get("origins") or c["origins"]
    rows = []
    for name, info in cities.items():
        if name.startswith("_"):
            continue
        for origin in origins:
            # cached API only: ~129 cities x 5 airline scrapers would take forever
            # and likely get the server's IP blocked
            r = _one(origin, info["airport"], c, include_real=False)
            if "flight_total" in r:
                rows.append({"total": r["flight_total"], "city": name,
                             "country": info["country"], "origin": origin,
                             "name": r["name"], "out": r["out"]["date"],
                             "back": r["back"]["date"] if r["back"] else None,
                             "nights": r["actual_nights"]})
    rows.sort(key=lambda x: x["total"])
    return {"currency": c["currency"].upper(), "rows": rows}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet console
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(INDEX, "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        elif self.path == "/api/cities":
            self._send(200, cities_payload())
        elif self.path == "/api/tg/status":
            self._send(200, tgbot.status(CFG))
        elif self.path == "/api/tg/hunts":
            self._send(200, tgbot.hunts_payload())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/search":
                self._send(200, do_search(body))
            elif self.path == "/api/compare":
                self._send(200, do_compare(body))
            elif self.path == "/api/tg/code":
                self._send(200, tgbot.make_code(CFG))
            elif self.path == "/api/tg/hunt":
                self._send(200, tgbot.add_hunts_ui(body.get("places", ""),
                                                   body.get("price"), CFG))
            elif self.path == "/api/tg/hunt_delete":
                self._send(200, tgbot.remove_hunt_ui(body.get("id")))
            else:
                self._send(404, {"error": "not found"})
        except ValueError as e:  # bad input (dates, destination, malformed JSON)
            self._send(400, {"error": str(e)})
        except Exception as e:  # never crash the server on a bad request
            self._send(500, {"error": str(e)})


def main():
    if ts.resolve_token(CFG).startswith("PUT_YOUR") and not os.environ.get("TRAVELPAYOUTS_TOKEN"):
        print("!! No API token (config.json or TRAVELPAYOUTS_TOKEN env) - searches will fail.")
    # Cloud hosts (Render/Railway/Fly) set PORT and expect us to bind 0.0.0.0; locally we
    # bind localhost and pop open the browser.
    cloud = "PORT" in os.environ
    port = int(os.environ.get("PORT", PORT))
    host = "0.0.0.0" if cloud else "127.0.0.1"
    print(f"Trip Finder UI running on {host}:{port}")
    tgbot.start_in_background(CFG)  # no-op until a bot token is configured
    if not cloud:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        print("Opening your browser... (press Ctrl+C here to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
