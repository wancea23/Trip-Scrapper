"""
Telegram price-hunt bot for Trip-Scrapper.

Pair your phone with the app (no chat-id digging):
    the web UI shows a one-time CODE + QR -> open the bot, send the code (or scan
    the QR, which opens Telegram with /start CODE prefilled) -> you're linked.

Then hunt prices straight from Telegram - cities, whole countries, several at
once, each hunt with its OWN target price:
    /hunt Prague 250
    /hunt Italy 300                     (a country = every city we know in it)
    /hunt Vienna, Budapest, Japan 400   (one hunt per place, all capped at 400)
    /list       your hunts + the last price seen for each
    /check      re-check every hunt right now and report
    /remove 2   stop hunt number 2 (numbers from /list)
    /clear      stop them all

A background watcher re-checks all hunts every `check_interval_minutes`
(config.json) and messages you when a hunt's watched price drops to or below
its target. Every hunt remembers the EXACT settings of the search you ran on
the site before starting it - travelers, departure window, min/max nights,
trip type, origins and the transport modes - and keeps checking with those
(a /hunt typed in Telegram uses your latest site search; without one it falls
back to the config defaults).

Setup: talk to @BotFather -> /newbot -> put the token in config.json ->
telegram.bot_token, or set the TELEGRAM_BOT_TOKEN environment variable.
Runs inside `python web.py` automatically, or standalone: `python bot.py`.
"""

import difflib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta

import requests

import trip_scraper as ts

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CODE_TTL = 15 * 60          # pairing codes live 15 minutes
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
_ME = None                  # getMe cache: {"username": ...}
_STARTED = False


def bot_token(cfg):
    """Env var -> gitignored tg_token.txt -> config.json (public repo: keep the
    real token OUT of config.json, same rule as the Travelpayouts token.txt)."""
    env = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env:
        return env.strip()
    tpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_token.txt")
    if os.path.exists(tpath):
        try:
            t = open(tpath, "r", encoding="utf-8").read().strip()
            if t:
                return t
        except OSError:
            pass
    return ((cfg.get("telegram") or {}).get("bot_token") or "").strip()


def tg(token, method, **params):
    """One Telegram Bot API call; returns the `result` or None on any failure."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          json=params, timeout=65)
        data = r.json()
        return data.get("result") if data.get("ok") else None
    except (requests.RequestException, ValueError):
        return None


def send(token, chat_id, text):
    tg(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
       disable_web_page_preview=True)


def bot_username(token):
    global _ME
    if _ME is None:
        me = tg(token, "getMe")
        _ME = me or {}
    return _ME.get("username")


# --------------------------------------------------------------------------- #
#  Storage (same SQLite file as the price history; one connection per call -
#  the web server and the two bot threads all touch this)
# --------------------------------------------------------------------------- #
def _db():
    db = sqlite3.connect(ts.DB_PATH, timeout=15)
    db.execute("""CREATE TABLE IF NOT EXISTS tg_users (
                    chat_id INTEGER PRIMARY KEY, name TEXT, linked_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS tg_codes (
                    code TEXT PRIMARY KEY, created REAL, chat_id INTEGER,
                    browser_id TEXT)""")
    # one web visitor (a random id kept in their browser's localStorage) -> their
    # Telegram chat. This is what lets each browser see/manage ONLY its own paired
    # chat's hunts, instead of everyone sharing one global list.
    db.execute("""CREATE TABLE IF NOT EXISTS web_sessions (
                    browser_id TEXT PRIMARY KEY, chat_id INTEGER, linked_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hunts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER, place TEXT, kind TEXT,
                    max_price REAL, currency TEXT, created TEXT,
                    last_price REAL, last_checked TEXT, last_alert REAL,
                    metric TEXT DEFAULT 'total', include_bag INTEGER DEFAULT 0,
                    settings TEXT)""")
    # the LAST search each browser ran (its exact form settings), so a hunt can
    # keep watching what was searched right before it was started
    db.execute("""CREATE TABLE IF NOT EXISTS web_searches (
                    browser_id TEXT PRIMARY KEY, settings TEXT, updated TEXT)""")
    for stmt in ("ALTER TABLE hunts ADD COLUMN metric TEXT DEFAULT 'total'",
                 "ALTER TABLE hunts ADD COLUMN include_bag INTEGER DEFAULT 0",
                 "ALTER TABLE hunts ADD COLUMN settings TEXT",
                 "ALTER TABLE tg_codes ADD COLUMN browser_id TEXT"):
        try:  # migrate older hunts tables
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass
    db.commit()
    return db


def new_code(browser_id=None):
    db = _db()
    db.execute("DELETE FROM tg_codes WHERE created < ?", (time.time() - CODE_TTL,))
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
    db.execute("INSERT OR REPLACE INTO tg_codes (code, created, chat_id, browser_id) "
               "VALUES (?,?,NULL,?)", (code, time.time(), browser_id))
    db.commit()
    db.close()
    return code


def try_pair(code, chat_id, name):
    """Redeem a pairing code. True if it was valid and this chat is now linked.
    If the code was minted by a web browser (carries its browser_id), also bind
    that browser to this chat so the website scopes to it."""
    db = _db()
    row = db.execute("SELECT browser_id FROM tg_codes WHERE code=? AND chat_id IS NULL "
                     "AND created >= ?", (code, time.time() - CODE_TTL)).fetchone()
    if row is None:
        db.close()
        return False
    browser_id = row[0]
    now = datetime.now().isoformat(timespec="seconds")
    db.execute("UPDATE tg_codes SET chat_id=? WHERE code=?", (chat_id, code))
    db.execute("INSERT OR REPLACE INTO tg_users (chat_id, name, linked_at) VALUES (?,?,?)",
               (chat_id, name, now))
    if browser_id:  # the browser that generated the code now owns this chat on the web
        db.execute("INSERT OR REPLACE INTO web_sessions (browser_id, chat_id, linked_at) "
                   "VALUES (?,?,?)", (browser_id, chat_id, now))
    db.commit()
    db.close()
    return True


def session_chat(browser_id):
    """The Telegram chat_id bound to one browser (its localStorage id), or None."""
    if not browser_id:
        return None
    db = _db()
    row = db.execute("SELECT chat_id FROM web_sessions WHERE browser_id=?",
                     (browser_id,)).fetchone()
    db.close()
    return row[0] if row else None


def is_paired(chat_id):
    db = _db()
    row = db.execute("SELECT 1 FROM tg_users WHERE chat_id=?", (chat_id,)).fetchone()
    db.close()
    return bool(row)


def paired_users():
    db = _db()
    rows = db.execute("SELECT chat_id, name, linked_at FROM tg_users "
                      "ORDER BY linked_at").fetchall()
    db.close()
    return [{"chat_id": r[0], "name": r[1], "linked_at": r[2]} for r in rows]


def add_hunt(chat_id, place, kind, max_price, currency, metric="total", include_bag=0,
             settings=None):
    db = _db()
    db.execute("INSERT INTO hunts (chat_id, place, kind, max_price, currency, created, metric, include_bag, settings) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (chat_id, place, kind, max_price, currency,
                datetime.now().isoformat(timespec="seconds"), metric, 1 if include_bag else 0,
                json.dumps(settings) if settings else None))
    db.commit()
    db.close()


def list_hunts(chat_id=None):
    db = _db()
    q = ("SELECT id, chat_id, place, kind, max_price, currency, last_price, "
         "last_checked, last_alert, metric, include_bag, settings FROM hunts")
    rows = (db.execute(q + " WHERE chat_id=? ORDER BY id", (chat_id,)) if chat_id
            else db.execute(q + " ORDER BY id")).fetchall()
    db.close()
    keys = ("id", "chat_id", "place", "kind", "max_price", "currency",
            "last_price", "last_checked", "last_alert", "metric", "include_bag", "settings")
    hunts = []
    for r in rows:
        h = dict(zip(keys, r))
        try:
            h["settings"] = json.loads(h["settings"]) if h["settings"] else None
        except (TypeError, ValueError):
            h["settings"] = None
        hunts.append(h)
    return hunts


def record_search(browser_id, settings):
    """Remember the last search one browser ran (called by web.py on every search),
    so a /hunt typed in Telegram can watch exactly what was just searched."""
    if not browser_id or not settings:
        return
    db = _db()
    db.execute("INSERT OR REPLACE INTO web_searches (browser_id, settings, updated) VALUES (?,?,?)",
               (browser_id, json.dumps(settings), datetime.now().isoformat(timespec="seconds")))
    db.commit()
    db.close()


def last_search_settings(chat_id):
    """The most recent search settings from any browser paired to this chat, or None."""
    if not chat_id:
        return None
    db = _db()
    row = db.execute("SELECT s.settings FROM web_searches s "
                     "JOIN web_sessions w ON w.browser_id = s.browser_id "
                     "WHERE w.chat_id=? ORDER BY s.updated DESC LIMIT 1", (chat_id,)).fetchone()
    db.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def remove_hunt(chat_id, hunt_id):
    db = _db()
    n = db.execute("DELETE FROM hunts WHERE chat_id=? AND id=?", (chat_id, hunt_id)).rowcount
    db.commit()
    db.close()
    return n > 0


def clear_hunts(chat_id):
    db = _db()
    n = db.execute("DELETE FROM hunts WHERE chat_id=?", (chat_id,)).rowcount
    db.commit()
    db.close()
    return n


def record_check(hunt_id, price, alerted=None):
    db = _db()
    if alerted is not None:
        db.execute("UPDATE hunts SET last_price=?, last_checked=?, last_alert=? WHERE id=?",
                   (price, datetime.now().isoformat(timespec="seconds"), alerted, hunt_id))
    else:
        db.execute("UPDATE hunts SET last_price=?, last_checked=? WHERE id=?",
                   (price, datetime.now().isoformat(timespec="seconds"), hunt_id))
    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
#  Places: a hunt targets a city, a whole country, or a raw IATA code
# --------------------------------------------------------------------------- #
def resolve_place(name, cities):
    """'Prague' -> ('city','Prague') · 'italy' -> ('country','Italy') ·
    'PRG' -> ('airport','PRG') · unknown -> (None, suggestion-or-None)."""
    n = name.strip().lower()
    for city in cities:
        if not city.startswith("_") and city.lower() == n:
            return "city", city
    for c in cities.values():
        if isinstance(c, dict) and c.get("country", "").lower() == n:
            return "country", c["country"]
    if re.fullmatch(r"[A-Za-z]{3}", name.strip()) and name.strip().isupper():
        return "airport", name.strip()
    pool = [c for c in cities if not c.startswith("_")] + \
           sorted({v["country"] for k, v in cities.items() if not k.startswith("_")})
    close = difflib.get_close_matches(name.strip(), pool, n=1, cutoff=0.6)
    return None, (close[0] if close else None)


def hunt_targets(hunt, cities):
    """The (label, iata, hotel_location) destinations one hunt covers."""
    if hunt["kind"] == "country":
        return [(city, c["airport"], c.get("hotel_location", city)) for city, c in cities.items()
                if not city.startswith("_") and c.get("country") == hunt["place"]]
    if hunt["kind"] == "airport":
        return [(hunt["place"], hunt["place"], hunt["place"])]
    c = cities.get(hunt["place"])
    return [(hunt["place"], c["airport"], c.get("hotel_location", hunt["place"]))] if c else []


# --------------------------------------------------------------------------- #
#  Checking a hunt: what it watches depends on its metric -
#  total = the whole trip (flights + bag + ground + stay + extras, + bus home
#          when there's no return flight - same math as the app's TRIP TOTAL)
#  flight = round-trip flights only · bus = direct bus there+back · stay = the room
# --------------------------------------------------------------------------- #
METRIC_LABEL = {"total": "full trip", "flight": "flights", "bus": "bus", "stay": "stay"}
METRIC_ALIAS = {"total": "total", "full": "total", "trip": "total",
                "flight": "flight", "flights": "flight",
                "bus": "bus", "stay": "stay", "stays": "stay", "hotel": "stay"}


def _mode_set(val):
    """'plane'/'bus'/'both' (as saved from the search form) -> allowed carriers."""
    v = str(val or "").strip().lower()
    if v in ("plane", "flight", "flights", "air", "fly"):
        return {"plane"}
    if v in ("bus", "coach", "flixbus"):
        return {"bus"}
    return {"plane", "bus"}


def hunt_cfg(hunt, cfg):
    """The config one hunt is checked with: the app config overlaid with the exact
    settings of the search that started the hunt (departure window, nights,
    travelers, trip type, origins, extras). Old hunts without settings keep the
    config defaults. Returns (config, out_modes, back_modes)."""
    s = hunt.get("settings") or {}
    if not s:
        return cfg, {"plane", "bus"}, {"plane", "bus"}
    c = json.loads(json.dumps(cfg))  # deep copy
    t = c["trip"]
    for k in ("depart_from", "depart_to", "type"):
        if s.get(k):
            t[k] = str(s[k])
    for k in ("nights_min", "nights_max", "travelers"):
        try:
            if s.get(k):
                t[k] = max(1, int(s[k]))
        except (TypeError, ValueError):
            pass
    if s.get("origins"):
        c["origins"] = [str(o).upper() for o in s["origins"] if o]
    if isinstance(s.get("extras"), list):
        c["extras"] = s["extras"]
    return c, _mode_set(s.get("out_mode")), _mode_set(s.get("back_mode"))


def settings_desc(s):
    """One short line saying what a hunt watches, e.g.
    '2 travelers · 2026-07-01 → 2026-09-30 · 3-5 nights · from IAS+RMO'."""
    if not s:
        return "app defaults - search on the site first to hunt your own dates/travelers"
    bits = []
    tv = s.get("travelers")
    if tv:
        bits.append(f"{tv} traveler{'s' if str(tv) != '1' else ''}")
    if s.get("depart_from") or s.get("depart_to"):
        bits.append(f"depart {s.get('depart_from', '?')} → {s.get('depart_to', '?')}")
    nmin, nmax = s.get("nights_min"), s.get("nights_max")
    if nmin or nmax:
        bits.append(f"{nmin}-{nmax} nights" if nmin and nmax and str(nmin) != str(nmax)
                    else f"{nmin or nmax} nights")
    if str(s.get("type", "")).lower() == "oneway":
        bits.append("one-way")
    if s.get("origins"):
        bits.append("from " + "+".join(str(o) for o in s["origins"]))
    om, bm = _mode_set(s.get("out_mode")), _mode_set(s.get("back_mode"))
    if om != {"plane", "bus"}:
        bits.append("there by " + next(iter(om)))
    if bm != {"plane", "bus"}:
        bits.append("back by " + next(iter(bm)))
    if s.get("include_bag"):
        bits.append("+bag")
    return " · ".join(bits) if bits else "app defaults"


def _best_flight(iata, cfg, include_real):
    """Cheapest fetch_flights result across the configured origins, or None."""
    best, borig = None, None
    for origin in cfg.get("origins", ["IAS", "RMO"]):
        try:
            f = ts.fetch_flights(origin, iata, cfg, include_real=include_real)
        except Exception:
            continue
        if f and (best is None or f["flight_total"] < best["flight_total"]):
            best, borig = f, origin
    return best, borig


def _price_city(metric, label, iata, hloc, cfg, include_real, include_bag=False,
                out_modes=None, back_modes=None):
    """Current price of `metric` for one destination city, or None. The checked
    bag is an opt-IN (matches the UI checkbox, unchecked by default). cfg is the
    hunt's own config (see hunt_cfg), so dates/nights/travelers/origins are the
    ones the user searched; the transport modes enforce the site's '100% a way
    home' rule - never alert on a return trip that has no way back."""
    t = cfg["trip"]
    travelers = max(1, int(t.get("travelers", 1)))
    nmin = ts.nights_bounds(t)[0]
    one_way = str(t.get("type", "return")).lower() == "oneway"
    out_modes = out_modes or {"plane", "bus"}
    back_modes = back_modes or {"plane", "bus"}

    if metric == "stay":
        try:
            s = ts.fetch_stay(hloc, t["depart_from"], nmin, cfg)
        except Exception:
            s = None
        if not s:
            return None
        return {"total": s["stay_total"], "city": label, "travelers": travelers,
                "what": f"{s['name']}, {nmin} nights from {t['depart_from']}",
                "link": s.get("link") or "https://www.airbnb.com"}

    if metric == "bus":
        back_date = (datetime.strptime(t["depart_from"], "%Y-%m-%d")
                     + timedelta(days=nmin)).strftime("%Y-%m-%d")
        try:
            b = ts.sources.bus_home_options(hloc, t["depart_from"], back_date)
        except Exception:
            b = None
        if not b or not (b.get("out") or b.get("back")):
            return None
        legs = [x for x in (b.get("out"), b.get("back")) if x]
        total = round(sum(x["price"] for x in legs) * travelers, 2)
        what = "there + back" if len(legs) == 2 else ("one direction only - check the other on infobus")
        return {"total": total, "city": label, "travelers": travelers,
                "what": f"FlixBus {what}, dep {t['depart_from']}",
                "link": legs[0]["book"] or b["infobus_out"]}

    def bus_quote(out_date, nights):
        back_date = None if one_way else (datetime.strptime(out_date, "%Y-%m-%d")
                                          + timedelta(days=nights)).strftime("%Y-%m-%d")
        try:
            return ts.sources.bus_home_options(hloc, out_date, back_date)
        except Exception:
            return None

    f = origin = None
    if "plane" in out_modes:
        f, origin = _best_flight(iata, cfg, include_real)

    bus_back = 0
    if f and not one_way:
        # the way-home rule, same as the site: a return flight when flying back
        # is allowed, else the direct bus home, else there is NO price here
        if not (f["back"] and "plane" in back_modes):
            bq = bus_quote(f["out"]["date"], f["actual_nights"] or nmin)
            if "bus" in back_modes and bq and bq.get("back"):
                bus_back = round(bq["back"]["price"] * travelers, 2)
            else:
                f = None

    if not f:
        # no (allowed) flight trip - a pure bus-there-and-back trip can still be
        # the full-trip price, like the site's direct-bus card
        if metric != "total" or "bus" not in out_modes or (not one_way and "bus" not in back_modes):
            return None
        bq = bus_quote(t["depart_from"], nmin)
        if not (bq and bq.get("out") and (one_way or bq.get("back"))):
            return None
        transport = round((bq["out"]["price"]
                           + (bq["back"]["price"] if not one_way else 0)) * travelers, 2)
        out_date = (bq["out"].get("dep") or "")[:10] or t["depart_from"]
        stay_total = 0
        try:
            s = ts.fetch_stay(hloc, out_date, nmin, cfg)
            stay_total = s["stay_total"] if s else 0
        except Exception:
            pass
        extras_total = ts.compute_extras(cfg.get("extras", []), travelers, nmin)[1]
        return {"total": round(transport + stay_total + extras_total, 2),
                "city": label, "travelers": travelers,
                "what": f"direct bus there{'' if one_way else ' + back'}, dep {out_date}"
                        + (f", stay {stay_total:g}" if stay_total else ", no stay found"),
                "link": bq["out"].get("book") or bq.get("infobus_out") or "https://infobus.eu"}

    home_by_bus = bus_back > 0
    # when the way home is the bus, only the outbound is flown - reprice the legs
    flight_total = round(f["out"]["price"] * travelers, 2) if home_by_bus else f["flight_total"]
    bag_legs = 1 if (one_way or home_by_bus) else f["bag_legs"]
    bag_total = (round(ts.airline_bag_leg(f["out"]["airline"]) * travelers, 2)
                 if home_by_bus else f["bag_total"])
    dates = f["out"]["date"] + (" (one-way)" if one_way else
                                (" → bus home" if home_by_bus else f" → {f['back']['date']}"))
    base = {"city": label, "travelers": travelers, "link": f["booking_link"],
            "what": f"from {ts.ORIGIN_NAMES.get(origin, origin)}, {dates}"}

    if metric == "flight":
        return {**base, "total": flight_total}

    # total: the app's TRIP TOTAL - flights + bag + ground + stay + extras
    # (+ the bus ride home when that's the way back)
    nights = f["actual_nights"] or nmin
    stay_total = 0
    try:
        s = ts.fetch_stay(hloc, f["out"]["date"], nights, cfg)
        stay_total = s["stay_total"] if s else 0
    except Exception:
        pass
    g = ts.sources.ground_to_airport(origin, f["out"]["date"])
    ground = round(g["price"] * travelers * bag_legs, 2) if g else 0
    extras_total = ts.compute_extras(cfg.get("extras", []), travelers, nights)[1]
    grand = round(flight_total + (bag_total if include_bag else 0)
                  + ground + bus_back + stay_total + extras_total, 2)
    return {**base, "total": grand,
            "what": base["what"] + (f", stay {stay_total:g}" if stay_total else ", no stay found")}


def check_hunt(hunt, cfg, cities):
    """Best current price for one hunt, or None - checked with the hunt's own
    saved search settings (dates, nights, travelers, origins, modes). Airline-site
    scrapers only run for small hunts (a country can be many cities - stay polite)."""
    targets = hunt_targets(hunt, cities)
    include_real = len(targets) <= 2
    metric = METRIC_ALIAS.get(hunt.get("metric") or "total", "total")
    include_bag = bool(hunt.get("include_bag"))
    hcfg, out_modes, back_modes = hunt_cfg(hunt, cfg)
    best = None
    for label, iata, hloc in targets:
        r = _price_city(metric, label, iata, hloc, hcfg, include_real, include_bag,
                        out_modes, back_modes)
        if r and (best is None or r["total"] < best["total"]):
            best = r
    return best


def fmt_best(hunt, best, cur):
    metric = METRIC_LABEL.get(METRIC_ALIAS.get(hunt.get("metric") or "total", "total"))
    watching = f"\n<i>{settings_desc(hunt['settings'])}</i>" if hunt.get("settings") else ""
    if not best:
        return (f"<b>{hunt['place']}</b> ({metric}) ≤{hunt['max_price']:g}: "
                f"no price found right now{watching}")
    hit = "✅" if best["total"] <= hunt["max_price"] else "…"
    pax = f", {best['travelers']} travelers" if best["travelers"] > 1 else ""
    return (f"{hit} <b>{hunt['place']}</b> {metric} ≤{hunt['max_price']:g}: "
            f"now <b>{best['total']:g} {cur}</b> ({best['city']} {best['what']}{pax})"
            f"{watching}\n<a href=\"{best['link']}\">book</a>")


def check_and_alert(hunt, cfg, cities, token):
    """One watcher pass for one hunt: record the price, ping the user on a hit -
    but only when the price actually DROPPED since the last alert."""
    cur = cfg["currency"].upper()
    best = check_hunt(hunt, cfg, cities)
    if not best:
        return
    hit = best["total"] <= hunt["max_price"]
    dropped = hunt["last_alert"] is None or best["total"] < hunt["last_alert"]
    if hit and dropped:
        send(token, hunt["chat_id"],
             "🎯 Price hunt hit!\n" + fmt_best(hunt, best, cur) +
             "\n(open the app for the full breakdown)")
        record_check(hunt["id"], best["total"], alerted=best["total"])
    else:
        record_check(hunt["id"], best["total"])


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #
HELP = (
    "<b>Trip-Scrapper price hunts</b>\n"
    "/hunt <i>place[, place…]</i> <i>price</i> — hunt a city, a whole country, or several "
    "at once, alert when the <b>full trip total</b> (flights + bag + ground + stay + extras) "
    "is at/below <i>price</i>\n"
    "    /hunt Prague 250\n"
    "    /hunt Italy 300\n"
    "    /hunt Vienna, Budapest, Japan 400\n"
    "Watch just one part instead - start with <b>flight</b>, <b>bus</b> or <b>stay</b>:\n"
    "    /hunt flight Prague 80\n"
    "    /hunt bus Prague 90\n"
    "    /hunt stay Italy 120\n"
    "/list — your hunts + last seen price\n"
    "/check — re-check all your hunts now\n"
    "/remove <i>n</i> — stop hunt n (numbers from /list)\n"
    "/clear — stop all hunts"
)


def _cmd_hunt(text, chat_id, cfg, cities, token):
    m = re.match(r"/hunt\s+(?:(total|full|trip|flights?|bus|stays?|hotel)\s+)?(.+?)\s+(\d+(?:[.,]\d+)?)\s*$",
                 text, re.I | re.S)
    if not m:
        send(token, chat_id, "Usage: /hunt [flight|bus|stay] <place>[, more places] <max price>\n"
                             "e.g. <code>/hunt Prague 250</code> (full trip) or "
                             "<code>/hunt flight Prague 80</code>")
        return
    metric = METRIC_ALIAS.get((m.group(1) or "total").lower(), "total")
    price = float(m.group(3).replace(",", "."))
    cur = cfg["currency"].upper()
    # hunt with the exact settings of the user's latest search on the site
    # (travelers, departure window, nights, modes) - not the config defaults
    settings = last_search_settings(chat_id)
    include_bag = bool((settings or {}).get("include_bag"))
    added, errors = [], []
    for raw in m.group(2).split(","):
        raw = raw.strip()
        if not raw:
            continue
        kind, place = resolve_place(raw, cities)
        if kind is None:
            errors.append(f"'{raw}'" + (f" — did you mean <b>{place}</b>?" if place else ""))
            continue
        add_hunt(chat_id, place, kind, price, cur, metric, include_bag, settings)
        n = len(hunt_targets({"kind": kind, "place": place}, cities))
        added.append(f"<b>{place}</b>" + (f" ({kind}, {n} cities)" if kind == "country" else ""))
    lines = []
    if added:
        lines.append(f"🔭 Hunting {', '.join(added)}: {METRIC_LABEL[metric]} at ≤{price:g} {cur}. "
                     f"I'll ping you when it drops to that. /list to see all.")
        lines.append(("Watching your last site search: " if settings else "Watching ")
                     + settings_desc(settings))
    if errors:
        lines.append("Didn't recognise " + "; ".join(errors) +
                     "\n(city or country names as in the app, e.g. Prague, Italy)")
    send(token, chat_id, "\n".join(lines))


def _cmd_list(chat_id, cfg, token):
    hunts = list_hunts(chat_id)
    if not hunts:
        send(token, chat_id, "No hunts yet. Start one: <code>/hunt Prague 250</code>")
        return
    cur = cfg["currency"].upper()
    lines = ["<b>Your price hunts</b>"]
    for i, h in enumerate(hunts, 1):
        last = (f"last {h['last_price']:g} {cur} at {h['last_checked'][5:16].replace('T', ' ')}"
                if h["last_price"] is not None else "not checked yet")
        metric = METRIC_LABEL.get(METRIC_ALIAS.get(h.get("metric") or "total", "total"))
        bag = " +bag" if h.get("include_bag") else ""
        lines.append(f"{i}. <b>{h['place']}</b> ({h['kind']}, {metric}{bag}) ≤{h['max_price']:g} {cur} — {last}")
        if h.get("settings"):
            lines.append(f"    <i>{settings_desc(h['settings'])}</i>")
    lines.append("/check to re-check now · /remove n to stop one")
    send(token, chat_id, "\n".join(lines))


def _cmd_check(chat_id, cfg, cities, token):
    hunts = list_hunts(chat_id)
    if not hunts:
        send(token, chat_id, "No hunts to check. Start one: <code>/hunt Prague 250</code>")
        return
    send(token, chat_id, f"Checking {len(hunts)} hunt(s)… this can take a minute.")

    def work():
        cur = cfg["currency"].upper()
        lines = []
        for h in hunts:
            best = check_hunt(h, cfg, cities)
            if best:
                record_check(h["id"], best["total"])
            lines.append(fmt_best(h, best, cur))
        send(token, chat_id, "\n\n".join(lines))

    threading.Thread(target=work, daemon=True).start()


def _handle(msg, cfg, cities, token):
    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id is None:
        return
    frm = msg.get("from") or {}
    name = frm.get("username") or frm.get("first_name") or str(chat_id)
    text = (msg.get("text") or "").strip()
    if not text:
        return
    low = text.lower()

    # pairing first: /start CODE (QR deep-link) or the bare code typed in
    if low.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1 and try_pair(parts[1].strip().upper(), chat_id, name):
            send(token, chat_id, f"🔗 Linked! Hi {name}.\n\n" + HELP)
        else:
            send(token, chat_id, "Hi! Open the Trip Finder app → <b>Telegram alerts</b> → "
                                 "get a code, then send it to me here.")
        return
    if re.fullmatch(r"[A-Za-z0-9]{6}", text) and try_pair(text.upper(), chat_id, name):
        send(token, chat_id, f"🔗 Linked! Hi {name}.\n\n" + HELP)
        return

    if not is_paired(chat_id):
        send(token, chat_id, "We're not linked yet. Open the Trip Finder app → "
                             "<b>Telegram alerts</b> → get a code and send it to me.")
        return

    if low.startswith("/help"):
        send(token, chat_id, HELP)
    elif low.startswith("/hunt"):
        _cmd_hunt(text, chat_id, cfg, cities, token)
    elif low.startswith("/list"):
        _cmd_list(chat_id, cfg, token)
    elif low.startswith("/check"):
        _cmd_check(chat_id, cfg, cities, token)
    elif low.startswith(("/remove", "/stop")):
        m = re.search(r"\d+", text)
        hunts = list_hunts(chat_id)
        if m and 1 <= int(m.group()) <= len(hunts):
            h = hunts[int(m.group()) - 1]
            remove_hunt(chat_id, h["id"])
            send(token, chat_id, f"Stopped hunting <b>{h['place']}</b>.")
        else:
            send(token, chat_id, "Which one? /list shows the numbers, then e.g. /remove 2")
    elif low.startswith("/clear"):
        n = clear_hunts(chat_id)
        send(token, chat_id, f"Stopped {n} hunt(s).")
    else:
        send(token, chat_id, "Didn't get that.\n\n" + HELP)


# --------------------------------------------------------------------------- #
#  The two loops: long-poll commands + periodic hunt watcher
# --------------------------------------------------------------------------- #
def poll_loop(cfg, token):
    # a leftover webhook makes getUpdates fail with 409 forever (and tg() swallows the
    # error), so the bot would look dead. Clear it before we long-poll.
    tg(token, "deleteWebhook", drop_pending_updates=False)
    # skip any backlog from before this start so we don't answer stale messages
    offset = 0
    stale = tg(token, "getUpdates", timeout=0) or []
    if stale:
        offset = stale[-1]["update_id"] + 1
    tg(token, "setMyCommands", commands=[
        {"command": "hunt", "description": "hunt a city/country at a max price"},
        {"command": "list", "description": "your hunts + last prices"},
        {"command": "check", "description": "re-check all hunts now"},
        {"command": "remove", "description": "stop one hunt"},
        {"command": "clear", "description": "stop all hunts"},
        {"command": "help", "description": "how this works"}])
    while True:
        updates = tg(token, "getUpdates", offset=offset, timeout=50)
        if updates is None:          # network trouble - don't spin
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                _handle(u.get("message") or u.get("edited_message") or {},
                        cfg, ts.load_json(ts.CITIES_PATH), token)
            except Exception as e:
                print(f"  ! tg command failed: {e}")


def watch_loop(cfg, token):
    every = max(15, int(cfg.get("check_interval_minutes", 180))) * 60
    time.sleep(120)  # let the server settle before the first sweep
    while True:
        try:
            cities = ts.load_json(ts.CITIES_PATH)
            for hunt in list_hunts():
                check_and_alert(hunt, cfg, cities, token)
        except Exception as e:
            print(f"  ! hunt watcher failed: {e}")
        time.sleep(every)


def start_in_background(cfg):
    """Start both loops as daemon threads (called by web.py). No token -> no-op."""
    global _STARTED
    token = bot_token(cfg)
    if not token or _STARTED:
        return False
    _STARTED = True
    threading.Thread(target=poll_loop, args=(cfg, token), daemon=True).start()
    threading.Thread(target=watch_loop, args=(cfg, token), daemon=True).start()
    print(f"Telegram bot running (@{bot_username(token) or '?'}) - "
          f"pair via the app's 'Telegram alerts' panel")
    return True


# --------------------------------------------------------------------------- #
#  Web-API helpers (used by web.py)
# --------------------------------------------------------------------------- #
def status(cfg, browser_id=None):
    """Bot config + THIS browser's own link state (not everyone's). `users` is 0 or
    1 entry (this browser's paired chat), so the front end's existing users.length
    check now means 'is THIS browser linked'."""
    token = bot_token(cfg)
    if not token:
        return {"configured": False}
    chat_id = session_chat(browser_id)
    mine = [u for u in paired_users() if u["chat_id"] == chat_id] if chat_id else []
    return {"configured": True, "username": bot_username(token),
            "users": mine, "hunts": len(list_hunts(chat_id)) if chat_id else 0}


def make_code(cfg, browser_id=None):
    token = bot_token(cfg)
    if not token:
        raise ValueError("no bot token - put it in config.json -> telegram.bot_token "
                         "or the TELEGRAM_BOT_TOKEN environment variable")
    user = bot_username(token)
    if not user:
        raise ValueError("the bot token doesn't work (Telegram getMe failed) - check it")
    code = new_code(browser_id)  # tie the code to this browser -> binds on redeem
    link = f"https://t.me/{user}?start={code}"
    return {"code": code, "username": user, "link": link}


def hunts_payload(browser_id=None):
    """Only the hunts + user of the browser that's asking."""
    chat_id = session_chat(browser_id)
    if not chat_id:
        return {"hunts": [], "users": []}
    return {"hunts": list_hunts(chat_id),
            "users": [u for u in paired_users() if u["chat_id"] == chat_id]}


def add_hunts_ui(places, price, cfg, metric="total", include_bag=False, browser_id=None,
                 settings=None):
    """Add hunts from the web UI for the chat THIS browser is paired to. `settings`
    is the search form the user had on screen (sent by the front end) so the hunt
    watches exactly what was searched; without one, the browser's last recorded
    search is used, then the config defaults."""
    chat_id = session_chat(browser_id)
    if not chat_id:
        raise ValueError("link your Telegram first - open the alerts panel and scan the code")
    cities = ts.load_json(ts.CITIES_PATH)
    metric = METRIC_ALIAS.get(str(metric or "total").lower(), "total")
    try:
        price = float(price)
    except (TypeError, ValueError):
        raise ValueError("the max price must be a number")
    if price <= 0:
        raise ValueError("the max price must be above 0")
    settings = settings or last_search_settings(chat_id)
    added, errors = [], []
    for raw in str(places).split(","):
        raw = raw.strip()
        if not raw:
            continue
        kind, place = resolve_place(raw, cities)
        if kind is None:
            errors.append(raw + (f" (did you mean {place}?)" if place else ""))
            continue
        add_hunt(chat_id, place, kind, price, cfg["currency"].upper(), metric, include_bag, settings)
        added.append(place)
    if not added and errors:
        raise ValueError("didn't recognise: " + "; ".join(errors))
    return {"added": added, "errors": errors, "hunts": list_hunts(chat_id),
            "watching": settings_desc(settings) if settings else None}


def update_hunt_ui(hunt_id, price=None, metric=None, include_bag=None, settings=None,
                   browser_id=None):
    """Edit a hunt from the web UI - new max price, metric, and/or replace its
    watched settings with the search form the user has on screen now. Scoped to
    THIS browser's paired chat, so nobody edits someone else's hunt. Any change
    resets the alert memory so the next check can ping at the new target."""
    chat_id = session_chat(browser_id)
    if not chat_id:
        raise ValueError("link your Telegram first - open the alerts panel and scan the code")
    db = _db()
    own = db.execute("SELECT 1 FROM hunts WHERE id=? AND chat_id=?",
                     (int(hunt_id), chat_id)).fetchone()
    if not own:
        db.close()
        raise ValueError("that hunt doesn't exist (maybe it was removed on another device)")
    sets, vals = ["last_alert=NULL"], []
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise ValueError("the max price must be a number")
        if price <= 0:
            raise ValueError("the max price must be above 0")
        sets.append("max_price=?")
        vals.append(price)
    if metric:
        sets.append("metric=?")
        vals.append(METRIC_ALIAS.get(str(metric).lower(), "total"))
    if include_bag is not None:
        sets.append("include_bag=?")
        vals.append(1 if include_bag else 0)
    if settings:
        sets.append("settings=?")
        vals.append(json.dumps(settings))
    db.execute(f"UPDATE hunts SET {', '.join(sets)} WHERE id=? AND chat_id=?",
               (*vals, int(hunt_id), chat_id))
    db.commit()
    db.close()
    return {"hunts": list_hunts(chat_id),
            "watching": settings_desc(settings) if settings else None}


def remove_hunt_ui(hunt_id, browser_id=None):
    """Delete a hunt only if it belongs to THIS browser's chat (no cross-deletes)."""
    chat_id = session_chat(browser_id)
    db = _db()
    if chat_id is not None:
        db.execute("DELETE FROM hunts WHERE id=? AND chat_id=?", (int(hunt_id), chat_id))
        db.commit()
    db.close()
    return {"hunts": list_hunts(chat_id) if chat_id else []}


if __name__ == "__main__":
    cfg = ts.load_json(ts.CONFIG_PATH)
    token = bot_token(cfg)
    if not token:
        print("!! No bot token. Talk to @BotFather -> /newbot, then put the token in")
        print("   config.json -> telegram.bot_token, or set TELEGRAM_BOT_TOKEN.")
        sys.exit(1)
    print(f"Bot @{bot_username(token) or '?'} polling. Ctrl+C to stop.")
    threading.Thread(target=watch_loop, args=(cfg, token), daemon=True).start()
    poll_loop(cfg, token)
