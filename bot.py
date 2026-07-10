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
(config.json) and messages you when a hunt's cheapest ROUND-TRIP FLIGHT total
(all travelers, your config dates/origins) drops to or below its target price.
Prices are flights-only - open the web app for the all-in trip total.

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
                    code TEXT PRIMARY KEY, created REAL, chat_id INTEGER)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hunts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER, place TEXT, kind TEXT,
                    max_price REAL, currency TEXT, created TEXT,
                    last_price REAL, last_checked TEXT, last_alert REAL,
                    metric TEXT DEFAULT 'total', include_bag INTEGER DEFAULT 0)""")
    for stmt in ("ALTER TABLE hunts ADD COLUMN metric TEXT DEFAULT 'total'",
                 "ALTER TABLE hunts ADD COLUMN include_bag INTEGER DEFAULT 0"):
        try:  # migrate older hunts tables
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass
    db.commit()
    return db


def new_code():
    db = _db()
    db.execute("DELETE FROM tg_codes WHERE created < ?", (time.time() - CODE_TTL,))
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
    db.execute("INSERT OR REPLACE INTO tg_codes (code, created, chat_id) VALUES (?,?,NULL)",
               (code, time.time()))
    db.commit()
    db.close()
    return code


def try_pair(code, chat_id, name):
    """Redeem a pairing code. True if it was valid and this chat is now linked."""
    db = _db()
    row = db.execute("SELECT code FROM tg_codes WHERE code=? AND chat_id IS NULL "
                     "AND created >= ?", (code, time.time() - CODE_TTL)).fetchone()
    if not row:
        db.close()
        return False
    db.execute("UPDATE tg_codes SET chat_id=? WHERE code=?", (chat_id, code))
    db.execute("INSERT OR REPLACE INTO tg_users (chat_id, name, linked_at) VALUES (?,?,?)",
               (chat_id, name, datetime.now().isoformat(timespec="seconds")))
    db.commit()
    db.close()
    return True


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


def add_hunt(chat_id, place, kind, max_price, currency, metric="total", include_bag=0):
    db = _db()
    db.execute("INSERT INTO hunts (chat_id, place, kind, max_price, currency, created, metric, include_bag) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (chat_id, place, kind, max_price, currency,
                datetime.now().isoformat(timespec="seconds"), metric, 1 if include_bag else 0))
    db.commit()
    db.close()


def list_hunts(chat_id=None):
    db = _db()
    q = ("SELECT id, chat_id, place, kind, max_price, currency, last_price, "
         "last_checked, last_alert, metric, include_bag FROM hunts")
    rows = (db.execute(q + " WHERE chat_id=? ORDER BY id", (chat_id,)) if chat_id
            else db.execute(q + " ORDER BY id")).fetchall()
    db.close()
    keys = ("id", "chat_id", "place", "kind", "max_price", "currency",
            "last_price", "last_checked", "last_alert", "metric", "include_bag")
    return [dict(zip(keys, r)) for r in rows]


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


def _price_city(metric, label, iata, hloc, cfg, include_real, include_bag=False):
    """Current price of `metric` for one destination city, or None. The checked
    bag is an opt-IN (matches the UI checkbox, unchecked by default)."""
    t = cfg["trip"]
    travelers = max(1, int(t.get("travelers", 1)))
    nmin = ts.nights_bounds(t)[0]

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

    f, origin = _best_flight(iata, cfg, include_real)
    if not f:
        return None
    dates = f["out"]["date"] + (f" → {f['back']['date']}" if f["back"] else " (one-way)")
    base = {"city": label, "travelers": travelers, "link": f["booking_link"],
            "what": f"from {ts.ORIGIN_NAMES.get(origin, origin)}, {dates}"}

    if metric == "flight":
        return {**base, "total": f["flight_total"]}

    # total: the app's TRIP TOTAL - flights + bag + ground + stay + extras
    # (+ the bus ride home when there's no return flight)
    nights = f["actual_nights"] or nmin
    stay_total = 0
    try:
        s = ts.fetch_stay(hloc, f["out"]["date"], nights, cfg)
        stay_total = s["stay_total"] if s else 0
    except Exception:
        pass
    g = ts.sources.ground_to_airport(origin, f["out"]["date"])
    ground = round(g["price"] * travelers * f["bag_legs"], 2) if g else 0
    extras_total = ts.compute_extras(cfg.get("extras", []), travelers, nights)[1]
    bus_back = 0
    if not f["one_way"] and not f["back"]:
        try:
            back_date = (datetime.strptime(f["out"]["date"], "%Y-%m-%d")
                         + timedelta(days=nights)).strftime("%Y-%m-%d")
            b = ts.sources.bus_home_options(hloc, f["out"]["date"], back_date)
            if b and b.get("back"):
                bus_back = round(b["back"]["price"] * travelers, 2)
        except Exception:
            pass
    grand = round(f["flight_total"] + (f["bag_total"] if include_bag else 0)
                  + ground + bus_back + stay_total + extras_total, 2)
    return {**base, "total": grand,
            "what": base["what"] + (f", stay {stay_total:g}" if stay_total else ", no stay found")}


def check_hunt(hunt, cfg, cities):
    """Best current price for one hunt, or None. Airline-site scrapers only run
    for small hunts (a country can be many cities - stay polite)."""
    targets = hunt_targets(hunt, cities)
    include_real = len(targets) <= 2
    metric = METRIC_ALIAS.get(hunt.get("metric") or "total", "total")
    include_bag = bool(hunt.get("include_bag"))
    best = None
    for label, iata, hloc in targets:
        r = _price_city(metric, label, iata, hloc, cfg, include_real, include_bag)
        if r and (best is None or r["total"] < best["total"]):
            best = r
    return best


def fmt_best(hunt, best, cur):
    metric = METRIC_LABEL.get(METRIC_ALIAS.get(hunt.get("metric") or "total", "total"))
    if not best:
        return f"<b>{hunt['place']}</b> ({metric}) ≤{hunt['max_price']:g}: no price found right now"
    hit = "✅" if best["total"] <= hunt["max_price"] else "…"
    pax = f", {best['travelers']} travelers" if best["travelers"] > 1 else ""
    return (f"{hit} <b>{hunt['place']}</b> {metric} ≤{hunt['max_price']:g}: "
            f"now <b>{best['total']:g} {cur}</b> ({best['city']} {best['what']}{pax})\n"
            f"<a href=\"{best['link']}\">book</a>")


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
    added, errors = [], []
    for raw in m.group(2).split(","):
        raw = raw.strip()
        if not raw:
            continue
        kind, place = resolve_place(raw, cities)
        if kind is None:
            errors.append(f"'{raw}'" + (f" — did you mean <b>{place}</b>?" if place else ""))
            continue
        add_hunt(chat_id, place, kind, price, cur, metric)
        n = len(hunt_targets({"kind": kind, "place": place}, cities))
        added.append(f"<b>{place}</b>" + (f" ({kind}, {n} cities)" if kind == "country" else ""))
    lines = []
    if added:
        lines.append(f"🔭 Hunting {', '.join(added)}: {METRIC_LABEL[metric]} at ≤{price:g} {cur}. "
                     f"I'll ping you when it drops to that. /list to see all.")
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
def status(cfg):
    token = bot_token(cfg)
    if not token:
        return {"configured": False}
    return {"configured": True, "username": bot_username(token),
            "users": paired_users(), "hunts": len(list_hunts())}


def make_code(cfg):
    token = bot_token(cfg)
    if not token:
        raise ValueError("no bot token - put it in config.json -> telegram.bot_token "
                         "or the TELEGRAM_BOT_TOKEN environment variable")
    user = bot_username(token)
    if not user:
        raise ValueError("the bot token doesn't work (Telegram getMe failed) - check it")
    code = new_code()
    link = f"https://t.me/{user}?start={code}"
    return {"code": code, "username": user, "link": link}


def hunts_payload():
    return {"hunts": list_hunts(), "users": paired_users()}


def add_hunts_ui(places, price, cfg, metric="total", include_bag=False):
    """Add hunts from the web UI for the most recently paired chat."""
    users = paired_users()
    if not users:
        raise ValueError("no Telegram linked yet - pair with the code first")
    chat_id = users[-1]["chat_id"]
    cities = ts.load_json(ts.CITIES_PATH)
    metric = METRIC_ALIAS.get(str(metric or "total").lower(), "total")
    try:
        price = float(price)
    except (TypeError, ValueError):
        raise ValueError("the max price must be a number")
    if price <= 0:
        raise ValueError("the max price must be above 0")
    added, errors = [], []
    for raw in str(places).split(","):
        raw = raw.strip()
        if not raw:
            continue
        kind, place = resolve_place(raw, cities)
        if kind is None:
            errors.append(raw + (f" (did you mean {place}?)" if place else ""))
            continue
        add_hunt(chat_id, place, kind, price, cfg["currency"].upper(), metric, include_bag)
        added.append(place)
    if not added and errors:
        raise ValueError("didn't recognise: " + "; ".join(errors))
    return {"added": added, "errors": errors, "hunts": list_hunts()}


def remove_hunt_ui(hunt_id):
    db = _db()
    db.execute("DELETE FROM hunts WHERE id=?", (int(hunt_id),))
    db.commit()
    db.close()
    return {"hunts": list_hunts()}


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
