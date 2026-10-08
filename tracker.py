#!/usr/bin/env python3
"""Watch Twitch's top categories and pick the game to buy.

A category qualifies when it is a game that is on PS5, is not free to play,
and was released this year (either its first release anywhere or its PS5
release). The pick is the highest-ranked qualifying game in the current top N;
when nothing qualifies, the previous pick stays. Every run appends a snapshot
so the history can be charted later. Standard library only.

Env: TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Usage: python tracker.py              # one run
       python tracker.py --test-telegram
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
SNAPSHOTS = DATA / "snapshots.jsonl"
GAMES = DATA / "games.json"
STATE = DATA / "state.json"
CONFIG = ROOT / "config.json"
PAGE_DATA = DOCS / "data.json"

TOP_N = 6
PS5_PLATFORM_ID = 167          # IGDB platform id for PlayStation 5
CACHE_HOURS = 24               # re-check a game's metadata at most once a day
PAGE_DAYS = 120                # how much history the web page loads
FREE_KEYWORD = re.compile(r"free[\s-]*(to|2)[\s-]*play|^f2p$", re.I)
UA = "twitch-ps5-tracker/1.0"
IGDB_FIELDS = ("fields name,first_release_date,platforms,"
               "release_dates.platform,release_dates.date,"
               "keywords.slug,keywords.name,external_games.url;")
# Twitch categories that are not games; everything else without an igdb_id is looked up by name.
NON_GAMES = {n.casefold() for n in [
    "Just Chatting", "IRL", "Music", "Art", "ASMR", "Talk Shows & Podcasts", "Sports",
    "Travel & Outdoors", "Special Events", "Pools, Hot Tubs, and Beaches", "Food & Drink",
    "Science & Technology", "Software and Game Development", "Makers & Crafting", "Politics",
    "Games + Demos", "I'm Only Sleeping", "Animals, Aquariums, and Zoos", "Fitness & Health",
    "Beauty & Body Art", "Co-working & Studying", "Slots", "Virtual Casino", "Retro",
]}


# ---------- small helpers ----------

def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def http_json(method, url, headers=None, body=None, timeout=20):
    data = None
    headers = {"User-Agent": UA, **(headers or {})}
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif isinstance(body, str):
        data = body.encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing environment variable {name}")
    return value


# ---------- Twitch + IGDB ----------

def twitch_token(client_id, secret):
    qs = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": secret,
        "grant_type": "client_credentials",
    })
    return http_json("POST", f"https://id.twitch.tv/oauth2/token?{qs}")["access_token"]


def top_categories(client_id, token, n=TOP_N):
    url = f"https://api.twitch.tv/helix/games/top?first={n}"
    res = http_json("GET", url, {"Client-ID": client_id, "Authorization": f"Bearer {token}"})
    return [{"id": g["id"], "name": g["name"], "igdb_id": g.get("igdb_id") or ""}
            for g in res["data"][:n]]


def igdb_games(client_id, token, igdb_ids):
    if not igdb_ids:
        return {}
    query = f"{IGDB_FIELDS} where id = ({','.join(igdb_ids)}); limit 50;"
    res = http_json("POST", "https://api.igdb.com/v4/games",
                    {"Client-ID": client_id, "Authorization": f"Bearer {token}"}, query)
    return {str(g["id"]): g for g in res}


def igdb_by_name(client_id, token, name):
    """Fallback for categories Twitch didn't link to IGDB: exact (case-insensitive) name match.
    Prefers a PS5 entry, then the most recent release, so a 2026 remake wins over an old namesake."""
    safe = name.replace("\\", "").replace('"', '\\"')
    query = f'{IGDB_FIELDS} where name ~ "{safe}"; limit 10;'
    res = http_json("POST", "https://api.igdb.com/v4/games",
                    {"Client-ID": client_id, "Authorization": f"Bearer {token}"}, query)
    if not res:
        return None
    return max(res, key=lambda g: (PS5_PLATFORM_ID in (g.get("platforms") or []),
                                   g.get("first_release_date") or 0))


def steam_is_free(igdb_game):
    """Steam's is_free flag is a reliable free-to-play signal when the game is on Steam."""
    for ext in igdb_game.get("external_games") or []:
        m = re.search(r"store\.steampowered\.com/app/(\d+)", ext.get("url") or "")
        if not m:
            continue
        app = m.group(1)
        try:
            res = http_json("GET", f"https://store.steampowered.com/api/appdetails?appids={app}&filters=basic")
            info = res.get(app) or {}
            if info.get("success"):
                return bool(info["data"].get("is_free"))
        except Exception as e:  # Steam is only a hint; never fail the run on it
            print(f"  steam lookup failed for app {app}: {e}")
        return None
    return None


def describe(cat, igdb_game):
    """Turn raw IGDB data into the facts the rules need."""
    if not igdb_game:
        return {"name": cat["name"], "igdb_id": cat["igdb_id"], "is_game": False}
    ps5_dates = [rd["date"] for rd in igdb_game.get("release_dates") or []
                 if rd.get("platform") == PS5_PLATFORM_ID and rd.get("date")]
    keywords = [k.get("slug") or k.get("name") or "" for k in igdb_game.get("keywords") or []]
    free_kw = any(FREE_KEYWORD.search(k) for k in keywords)
    steam_free = steam_is_free(igdb_game)
    if free_kw:
        free, free_source = True, "IGDB tags it free-to-play"
    elif steam_free is not None:
        free, free_source = steam_free, "Steam says free" if steam_free else "Steam says paid"
    else:
        free, free_source = False, "no free-to-play signal found"
    return {
        "name": cat["name"],
        "igdb_id": cat["igdb_id"],
        "is_game": True,
        "on_ps5": PS5_PLATFORM_ID in (igdb_game.get("platforms") or []) or bool(ps5_dates),
        "first_release": igdb_game.get("first_release_date"),
        "ps5_release": min(ps5_dates) if ps5_dates else None,
        "free": free,
        "free_source": free_source,
    }


# ---------- rules ----------

def evaluate(info, now, cfg):
    """Return (qualifies, reason)."""
    name = info["name"].casefold()
    lower = lambda key: {n.casefold() for n in cfg.get(key, [])}
    if name in lower("ignore"):
        return False, "ignored in config.json"
    if not info.get("is_game"):
        return False, "not a game"
    if not info.get("on_ps5"):
        return False, "not on PS5"

    year = now.year
    released = []
    for label, ts in (("first release", info.get("first_release")),
                      ("PS5 release", info.get("ps5_release"))):
        if ts:
            dt = datetime.fromtimestamp(ts, timezone.utc)
            if dt.year == year and dt <= now:
                released.append(f"{label} {dt:%Y-%m-%d}")
    if not released:
        return False, f"not released in {year}"

    if name in lower("force_paid"):
        free, src = False, "marked paid in config.json"
    elif name in lower("force_free"):
        free, src = True, "marked free in config.json"
    else:
        free, src = info.get("free"), info.get("free_source", "")
    if free:
        return False, f"free to play ({src})"
    return True, f"PS5 · {released[0]} · paid ({src})"


# ---------- Telegram ----------

def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("Telegram not configured; message was:\n" + text)
        return False
    try:
        http_json("POST", f"https://api.telegram.org/bot{token}/sendMessage", body={
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        return True
    except urllib.error.HTTPError as e:
        print(f"Telegram send failed: {e} {e.read().decode(errors='replace')}")
    except Exception as e:
        print(f"Telegram send failed: {e}")
    return False


def store_link(name):
    return "https://store.playstation.com/search/" + urllib.parse.quote(name)


# ---------- main run ----------

def run():
    now = now_utc()
    cfg = load_json(CONFIG, {})
    games = load_json(GAMES, {})
    state = load_json(STATE, {"pick": None, "pick_history": []})

    client_id = env("TWITCH_CLIENT_ID")
    token = twitch_token(client_id, env("TWITCH_CLIENT_SECRET"))
    top = top_categories(client_id, token)

    # Refresh metadata for categories we haven't checked recently.
    stale = [c for c in top
             if c["id"] not in games
             or now - parse_iso(games[c["id"]]["checked_at"]) > timedelta(hours=CACHE_HOURS)]
    if stale:
        ids = [c["igdb_id"] for c in stale if c["igdb_id"].isdigit()]
        try:
            igdb = igdb_games(client_id, token, ids)
        except Exception as e:
            print(f"IGDB lookup failed, using cached data where possible: {e}")
            igdb = None
        if igdb is not None:
            for c in stale:
                if c["igdb_id"] and c["igdb_id"] not in igdb:
                    continue  # IGDB didn't return it this time; retry next run
                game = igdb.get(c["igdb_id"])
                if not c["igdb_id"] and c["name"].casefold() not in NON_GAMES:
                    try:
                        game = igdb_by_name(client_id, token, c["name"])
                    except Exception as e:
                        print(f"IGDB name lookup failed for {c['name']}: {e}")
                        continue
                info = describe(c, game)
                info["checked_at"] = iso(now)
                games[c["id"]] = info

    entries = []
    for rank, c in enumerate(top, 1):
        info = games.get(c["id"])
        if info is None:
            q, why = False, "metadata unavailable (will retry)"
        else:
            info["name"] = c["name"]
            q, why = evaluate(info, now, cfg)
        entries.append({"id": c["id"], "name": c["name"], "q": q, "why": why})
        print(f"#{rank} {c['name']}: {'QUALIFIES' if q else 'no'} - {why}")

    SNAPSHOTS.parent.mkdir(parents=True, exist_ok=True)
    with SNAPSHOTS.open("a") as f:
        f.write(json.dumps({"t": iso(now), "top": entries}, ensure_ascii=False) + "\n")

    # Pick = highest-ranked qualifying game right now; otherwise keep the old pick.
    best = next(((rank, e) for rank, e in enumerate(entries, 1) if e["q"]), None)
    old = state.get("pick")
    if best and (old is None or old["id"] != best[1]["id"]):
        rank, e = best
        for h in state["pick_history"]:
            if h["to"] is None:
                h["to"] = iso(now)
        state["pick"] = {"id": e["id"], "name": e["name"], "since": iso(now), "why": e["why"]}
        state["pick_history"].append({"id": e["id"], "name": e["name"], "from": iso(now), "to": None})
        others = [f"#{r} {html.escape(x['name'])}" for r, x in enumerate(entries, 1)
                  if x["q"] and x["id"] != e["id"]]
        msg = (f"🎮 <b>New pick: {html.escape(e['name'])}</b>\n"
               f"#{rank} on Twitch right now\n{html.escape(e['why'])}\n"
               f"<a href=\"{store_link(e['name'])}\">Search PS Store</a>")
        if old:
            msg += f"\n\nReplaces: {html.escape(old['name'])}"
        if others:
            msg += "\nAlso qualifying: " + ", ".join(others)
        telegram(msg)
        print(f"Pick changed -> {e['name']}")

    save_json(GAMES, games)
    save_json(STATE, state)
    write_page_data(now, games, state)


def write_page_data(now, games, state):
    cutoff = now - timedelta(days=PAGE_DAYS)
    snaps = []
    if SNAPSHOTS.exists():
        for line in SNAPSHOTS.read_text().splitlines():
            if line.strip():
                s = json.loads(line)
                if parse_iso(s["t"]) >= cutoff:
                    snaps.append(s)
    save_json(PAGE_DATA, {
        "generated": iso(now),
        "top_n": TOP_N,
        "pick": state.get("pick"),
        "pick_history": state.get("pick_history", []),
        "games": games,
        "snapshots": snaps,
    })


if __name__ == "__main__":
    if "--test-telegram" in sys.argv:
        ok = telegram("✅ Twitch PS5 tracker can reach you on Telegram.")
        print("Test message sent." if ok else "Test message NOT sent - check the Telegram secrets.")
        sys.exit(0 if ok else 1)
    else:
        run()
