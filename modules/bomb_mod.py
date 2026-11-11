"""S3 - BOMB & GEMS (5x5 mine game) - server-authoritative, ported from the Main panel (bombgame.py).

Bomb positions are created and kept ON THE SERVER (memory + DB3 bomb_secret/{gid}); the phone only gets {box, gem|bomb} after a box is opened.
Same wallet / matchmaking / BOT rules as Coin Battle: money moves on Server-1 (or Server-4) through svc.wallet_debit / svc.wallet_credit."""
import time, random, threading, uuid
from flask import Blueprint, g as flask_g
from firebase_admin import firestore as fs
from common import svc
from common.authz import user_required, admin_required
from common.util import ok, err, body, rate_ok
from modules.game_mod import C, NOW, debit, credit, lb_add, player_info, bot_info, today

bp = Blueprint("bomb", __name__)

N = 25
DEFAULT_CFG = {"enabled": True, "entries": [2, 5, 10], "cutPct": 10, "practicePerDay": 3, "botsEnabled": True, "botAfterFails": 2,
               "searchSec": 12, "paidSearchSec": 25, "turnSec": 15, "maxAuto": 3, "botMs": 900, "maxStake": 100, "minBombs": 1, "maxBombs": 10}
INTRO_MS = 11000      # extra time before the very first turn (reveal screen + coin toss + bomb roll on the phone)

_cfg = {"t": 0, "v": dict(DEFAULT_CFG)}
LOCK = threading.RLock()
QUEUE, TICKETS, FAILS, GAMES, GLOCKS = {}, {}, {}, {}, {}

def cfg(force=False):
    if force or time.time() - _cfg["t"] > 20:
        try:
            d = C().collection("config").document("bomb").get()
            v = dict(DEFAULT_CFG); v.update((d.to_dict() or {}) if d.exists else {}); _cfg["v"] = v
        except Exception: pass
        _cfg["t"] = time.time()
    return _cfg["v"]

def save_cfg(patch):
    v = dict(DEFAULT_CFG); v.update(cfg(True))
    ent = patch.get("entries")
    if ent is not None: v["entries"] = sorted({int(x) for x in ent if 1 <= int(x) <= int(v.get("maxStake", 100))})[:6] or [2, 5, 10]
    for k, lo, hi in (("cutPct", 0, 50), ("practicePerDay", 0, 50), ("botAfterFails", 1, 5), ("searchSec", 4, 40), ("paidSearchSec", 4, 120), ("turnSec", 6, 60), ("maxAuto", 0, 10), ("botMs", 300, 3000), ("maxStake", 1, 1000), ("minBombs", 1, 24), ("maxBombs", 1, 24)):
        if k in patch: v[k] = max(lo, min(hi, int(patch[k])))
    if v["minBombs"] > v["maxBombs"]: v["minBombs"] = v["maxBombs"]
    for k in ("enabled", "botsEnabled"):
        if k in patch: v[k] = bool(patch[k])
    C().collection("config").document("bomb").set(v)
    _cfg.update(t=time.time(), v=v); return v

def public_cfg():
    c = cfg(); return {k: c[k] for k in ("enabled", "entries", "cutPct", "practicePerDay", "botsEnabled", "searchSec", "paidSearchSec", "turnSec", "maxAuto", "botAfterFails", "minBombs", "maxBombs")}

def glock(gid):
    with LOCK: return GLOCKS.setdefault(gid, threading.RLock())

# ---------------------------------------------------------------- game state
PUBLIC = ("id", "uids", "players", "stake", "practice", "nb", "turn", "status", "winner", "reason", "moves", "deadline", "auto", "createdAt", "finishedAt", "prize", "cutPct", "turnSec", "settled", "reveal")

def public_game(g): return {k: g.get(k) for k in PUBLIC}

def save_game(g, secret=False):
    GAMES[g["id"]] = g
    d = {k: v for k, v in g.items() if k != "id" and k != "bombs"}          # bomb map NEVER goes into the public document
    db = C(); db.collection("bomb_games").document(g["id"]).set(d)
    if secret: db.collection("bomb_secret").document(g["id"]).set({"bombs": g["bombs"]})

def load_game(gid):
    g = GAMES.get(gid)
    if g: return g
    db = C(); s = db.collection("bomb_games").document(gid).get()
    if not s.exists: return None
    g = s.to_dict(); g["id"] = gid
    sec = db.collection("bomb_secret").document(gid).get().to_dict() or {}
    g["bombs"] = sec.get("bombs", g.get("reveal") or []); GAMES[gid] = g; return g

def active_game_of(uid):
    with LOCK:
        for g in GAMES.values():
            if g["status"] == "playing" and uid in g["uids"]: return g
    return None

def stat_add(uid, **inc):
    try: C().collection("bomb_stats").document(uid).set({k: fs.Increment(v) for k, v in inc.items()}, merge=True)
    except Exception as e: print("[bomb] stats:", e, flush=True)

def create_game(p0, p1, stake, practice):
    c = cfg(); gid = uuid.uuid4().hex[:14]; taken = []
    if not practice:
        for p in (p0, p1):
            if p["bot"]: continue
            if not debit(p["uid"], stake, "Bomb & Gems entry"):
                for q in taken: credit(q, stake, "Bomb & Gems refund", "game_refund")
                return None, p["uid"]
            taken.append(p["uid"])
    nb = random.randint(int(c["minBombs"]), int(c["maxBombs"]))
    bombs = sorted(random.sample(range(N), nb))
    first = random.choice((0, 1))                                  # the coin toss on the phone only SHOWS this result
    g = {"id": gid, "uids": [p0["uid"], p1["uid"]], "players": [p0, p1], "stake": stake, "practice": practice, "nb": nb, "bombs": bombs, "turn": first, "status": "playing",
         "winner": -1, "reason": "", "moves": [], "auto": [0, 0], "createdAt": NOW(), "cutPct": c["cutPct"], "turnSec": c["turnSec"], "settled": False, "reveal": [],
         "deadline": NOW() + c["turnSec"] * 1000 + INTRO_MS}
    save_game(g, secret=True)
    for p in (p0, p1):
        if not p["bot"]: stat_add(p["uid"], games=1)
    if p0["bot"] or p1["bot"]: _bot_turns(g)
    return g, None

def _prize(g): return round(g["stake"] * 2 * (100 - g["cutPct"]) / 100.0, 2)

def finish(g, winner, reason):
    """idempotent — pays out exactly once. winner 0/1 (no draws in this game)."""
    with glock(g["id"]):
        if g["status"] == "done": return g
        g.update(status="done", winner=winner, reason=reason, finishedAt=NOW(), settled=False, reveal=list(g["bombs"]))
        humans = [i for i, p in enumerate(g["players"]) if not p["bot"]]
        stake, practice = g["stake"], g["practice"]
        prize = _prize(g) if not practice else 0
        g["prize"] = prize
        try:
            if not practice:
                if winner in humans:
                    p = g["players"][winner]
                    credit(p["uid"], prize, "Bomb & Gems — You won", "game_win"); lb_add(p["uid"], p["name"], p["photo"], prize)
                    stat_add(p["uid"], wins=1, won=prize)
                for i in humans:
                    if winner != i: stat_add(g["uids"][i], losses=1, lost=stake)
                tot = {"games": 1, "entries": stake * len(humans), "botGames": 1 if len(humans) == 1 else 0, "payouts": prize if winner in humans else 0}
                C().collection("bomb_totals").document("all").set({k: fs.Increment(v) for k, v in tot.items()}, merge=True)
            else:
                for i in humans: stat_add(g["uids"][i], practiceGames=1)
            g["settled"] = True
        except Exception as e:
            print("[bomb] SETTLE ERROR", g["id"], e, flush=True); g["settleError"] = str(e)[:200]
        save_game(g)
        try: C().collection("bomb_secret").document(g["id"]).delete()
        except Exception: pass
        return g

def apply_open(g, pi, box, delay=0):
    if not (0 <= box < N): return "invalid box"
    if any(m["i"] == box for m in g["moves"]): return "box already open"
    bomb = 1 if box in g["bombs"] else 0
    g["moves"].append({"p": pi, "i": box, "b": bomb, "d": int(delay)})
    if bomb: finish(g, 1 - pi, "bomb"); return None
    g["turn"] = 1 - pi; g["deadline"] = NOW() + int(delay) + g["turnSec"] * 1000 + 600
    return None

def _closed(g):
    o = {m["i"] for m in g["moves"]}; return [i for i in range(N) if i not in o]

def _bot_turns(g):
    c = cfg(); guard = 0
    while g["status"] == "playing" and g["players"][g["turn"]]["bot"] and guard < 3:
        guard += 1; pi = g["turn"]; cl = _closed(g)
        if not cl: break
        apply_open(g, pi, random.choice(cl), delay=random.randint(int(c["botMs"]), int(c["botMs"]) + 1800))
    if g["status"] == "playing": save_game(g)

def human_open(uid, gid, box):
    with glock(gid):
        g = load_game(gid)
        if not g: return None, "game not found"
        if uid not in g["uids"]: return None, "not your game"
        if g["status"] != "playing": return public_game(g), None
        pi = g["uids"].index(uid)
        if g["turn"] != pi: return public_game(g), "not your turn"
        try: box = int(box)
        except Exception: return public_game(g), "invalid box"
        err = apply_open(g, pi, box)
        if err: return public_game(g), err
        if g["status"] == "playing": _bot_turns(g)
        save_game(g); return public_game(g), None

def tick(gid):
    with glock(gid):
        g = load_game(gid)
        if not g or g["status"] != "playing": return public_game(g) if g else None
        if NOW() <= g["deadline"]: return public_game(g)
        pi = g["turn"]
        if g["players"][pi]["bot"]: _bot_turns(g); return public_game(g)
        g["auto"][pi] += 1
        if g["auto"][pi] > cfg()["maxAuto"]: finish(g, 1 - pi, "afk"); return public_game(g)
        cl = _closed(g)
        if cl: apply_open(g, pi, random.choice(cl))
        if g["status"] == "playing": _bot_turns(g)
        save_game(g); return public_game(g)

def leave(uid, gid):
    with glock(gid):
        g = load_game(gid)
        if not g or uid not in g["uids"] or g["status"] != "playing": return public_game(g) if g else None
        pi = g["uids"].index(uid); finish(g, 1 - pi, "forfeit"); return public_game(g)

# ---------------------------------------------------------------- matchmaking (same rules as Coin Battle)
def _key(practice, stake): return "p" if practice else f"s{stake}"

def _practice_used(uid):
    ref = C().collection("bomb_stats").document(uid); s = ref.get().to_dict() or {}
    n = (int(s.get("practiceUsed", 0) or 0) + 1) if s.get("practiceDay") == today() else 1
    ref.set({"practiceDay": today(), "practiceUsed": n}, merge=True)

def _drop_ticket(t):
    t["status"] = "cancelled"
    try: QUEUE.get(t["key"], []).remove(t)
    except ValueError: pass

def _wait(c, practice):
    return c["searchSec"] if practice else c["paidSearchSec"]      # cash matches wait longer: they only ever get a REAL player

def find(uid, entry, practice):
    c = cfg()
    if not c["enabled"]: return {"error": "Bomb & Gems is switched off right now"}
    if practice:
        st = C().collection("bomb_stats").document(uid).get().to_dict() or {}
        used = int(st.get("practiceUsed", 0) or 0) if st.get("practiceDay") == today() else 0
        if used >= c["practicePerDay"]: return {"error": f"Daily practice limit reached ({c['practicePerDay']}/day)"}
        entry = 0
    else:
        entry = int(entry or 0)
        if entry not in c["entries"]: return {"error": "Invalid entry amount"}
        b = svc.user_brief(uid) or {}
        if b.get("banned"): return {"error": "Account blocked"}
        if b.get("paidGameBlocked"): return {"error": b.get("geoMessage") or "Paid games are not available in your state - play free practice games.", "code": "payment_blocked_state"}
        dep, wd = float(b.get("dep", 0) or 0), float(b.get("wd", 0) or 0)
        if dep + wd < entry: return {"error": "Not enough balance — add money first"}
    with LOCK:
        ag = active_game_of(uid)
        if ag: return {"status": "matched", "gid": ag["id"], "resume": True}
        for t in [t for t in TICKETS.values() if t["uid"] == uid and t["status"] == "waiting"]: _drop_ticket(t)
        key = _key(practice, entry); me = player_info(uid)
        for t in list(QUEUE.get(key, [])):
            if t["uid"] != uid and time.time() - t["ts"] < _wait(c, practice) + 4:
                opp = t["player"]
                g, bad = create_game(opp, me, entry, practice)
                QUEUE[key].remove(t)
                if g is None:
                    if bad == me["uid"]: return {"error": "Not enough balance — add money first"}
                    t["status"] = "cancelled"; continue
                t.update(status="matched", gid=g["id"]); FAILS.pop(uid, None); FAILS.pop(opp["uid"], None)
                if practice: _practice_used(uid); _practice_used(opp["uid"])
                return {"status": "matched", "gid": g["id"]}
        tk = uuid.uuid4().hex[:12]
        t = {"id": tk, "uid": uid, "key": key, "entry": entry, "practice": practice, "player": me, "ts": time.time(), "status": "waiting", "gid": None}
        TICKETS[tk] = t; QUEUE.setdefault(key, []).append(t)
        return {"status": "waiting", "ticket": tk, "searchSec": _wait(c, practice), "attempt": FAILS.get(uid, {}).get("n", 0) + 1, "botAfter": c["botAfterFails"], "practice": practice}

def poll(uid, tk):
    c = cfg()
    with LOCK:
        t = TICKETS.get(tk)
        if not t or t["uid"] != uid: return {"status": "none"}
        if t["status"] == "matched": return {"status": "matched", "gid": t["gid"]}
        if t["status"] != "waiting": return {"status": "cancelled"}
        w = _wait(c, t["practice"])
        if time.time() - t["ts"] < w: return {"status": "waiting", "left": int(w - (time.time() - t["ts"]))}
        _drop_ticket(t)
        if not t["practice"]:                      # CASH match: never a bot. Nothing was charged yet (the entry is taken only when a real opponent is found).
            return {"status": "nomatch", "paid": True}
        f = FAILS.setdefault(uid, {"n": 0, "t": 0})
        if time.time() - f["t"] > 900: f["n"] = 0
        f["n"] += 1; f["t"] = time.time()
        if c["botsEnabled"] and t["practice"] and f["n"] >= c["botAfterFails"]:
            g, bad = create_game(t["player"], bot_info(), t["entry"], t["practice"])
            if g is None: return {"status": "error", "error": "Not enough balance — add money first"}
            f["n"] = 0
            if t["practice"]: _practice_used(uid)
            return {"status": "matched", "gid": g["id"], "bot": True}
        return {"status": "nomatch", "fails": f["n"], "botAfter": c["botAfterFails"]}


def restore():
    n = 0
    db = C()
    for s in db.collection("bomb_games").where("status", "==", "playing").limit(200).stream():
        g = s.to_dict(); g["id"] = s.id
        sec = db.collection("bomb_secret").document(s.id).get().to_dict() or {}
        if not sec.get("bombs"): continue
        g["bombs"] = sec["bombs"]; GAMES[s.id] = g; n += 1
    return n

def janitor():
    now = NOW()
    for gid in [k for k, g in list(GAMES.items()) if g.get("status") == "playing" and now > g.get("deadline", 0) + 2500]:
        try: tick(gid)
        except Exception as e: print("[bomb] janitor", gid, e, flush=True)
    with LOCK:
        for k in [k for k, t in TICKETS.items() if time.time() - t["ts"] > 180]: TICKETS.pop(k, None)
        for k, v in list(QUEUE.items()): QUEUE[k] = [t for t in v if time.time() - t["ts"] < 180]

def purge_old(days=14):
    db = C(); cutoff = NOW() - days * 86400000; n = 0
    for s in db.collection("bomb_games").where("createdAt", "<", cutoff).limit(300).stream():
        s.reference.delete(); n += 1
    for gid in [k for k, g in GAMES.items() if g.get("status") == "done" and NOW() - (g.get("finishedAt") or 0) > 3600000]: GAMES.pop(gid, None); GLOCKS.pop(gid, None)
    return n

# ---------------------------------------------------------------- HTTP
def _guard(uid):
    return None if rate_ok("bomb:" + uid, 60, 10) else err("Too many requests - slow down", 429)


def _mine(gid, uid):
    gm = load_game(str(gid or ""))
    return gm if gm and uid in gm["uids"] else None


@bp.route("/game/bomb/config", methods=["GET", "POST"])
@user_required
def r_config():
    uid = flask_g.uid
    st = C().collection("bomb_stats").document(uid).get().to_dict() or {}
    used = int(st.get("practiceUsed", 0) or 0) if st.get("practiceDay") == today() else 0
    ag = active_game_of(uid)
    return ok(cfg=public_cfg(), practiceLeft=max(0, cfg()["practicePerDay"] - used), active=ag["id"] if ag else None,
              stats={k: st.get(k, 0) for k in ("games", "wins", "losses", "won", "lost")}, now=NOW())


@bp.post("/game/bomb/find")
@user_required
def r_find():
    uid, d = flask_g.uid, body()
    bad = _guard(uid)
    if bad: return bad
    r = find(uid, d.get("entry"), bool(d.get("practice")))
    if "error" in r: return err(r["error"], 400, **{k: v for k, v in r.items() if k != "error"})
    return ok(**r)


@bp.post("/game/bomb/poll")
@user_required
def r_poll():
    r = poll(flask_g.uid, str(body().get("ticket", "")))
    if r.get("status") == "error": return err(r.get("error", "error"), 400)
    return ok(**r)


@bp.post("/game/bomb/cancel")
@user_required
def r_cancel():
    with LOCK:
        t = TICKETS.get(str(body().get("ticket", "")))
        if t and t["uid"] == flask_g.uid and t["status"] == "waiting": _drop_ticket(t)
    return ok()


@bp.post("/game/bomb/open")
@user_required
def r_open():
    uid, d = flask_g.uid, body()
    bad = _guard(uid)
    if bad: return bad
    gm, e = human_open(uid, str(d.get("gid", "")), d.get("box", -1))
    if e: return err(e, 400, game=gm)
    return ok(game=gm, now=NOW())


@bp.post("/game/bomb/tick")
@user_required
def r_tick():
    gm = _mine(body().get("gid"), flask_g.uid)
    if not gm: return err("game not found", 404)
    return ok(game=tick(gm["id"]), now=NOW())


@bp.post("/game/bomb/leave")
@user_required
def r_leave():
    return ok(game=leave(flask_g.uid, str(body().get("gid", ""))), now=NOW())


@bp.post("/game/bomb/game")
@user_required
def r_game():
    gm = _mine(body().get("gid"), flask_g.uid)
    if not gm: return err("game not found", 404)
    return ok(game=public_game(gm), now=NOW())


@bp.get("/game/bomb/admin/overview")
@admin_required
def a_overview():
    db = C(); tot = db.collection("bomb_totals").document("all").get().to_dict() or {}
    games = []
    for s in db.collection("bomb_games").order_by("createdAt", direction="DESCENDING").limit(40).stream():
        x = s.to_dict(); games.append({"id": s.id, "t": x.get("createdAt"), "stake": x.get("stake"), "practice": x.get("practice"), "status": x.get("status"), "winner": x.get("winner"),
                                       "reason": x.get("reason"), "prize": x.get("prize"), "nb": x.get("nb"), "players": [{"name": p.get("name"), "bot": p.get("bot")} for p in x.get("players", [])],
                                       "moves": len(x.get("moves", []))})
    hn = (tot.get("entries", 0) or 0) - (tot.get("payouts", 0) or 0)
    return ok(cfg=cfg(True), totals={**{k: tot.get(k, 0) for k in ("games", "botGames", "entries", "payouts")}, "houseNet": round(hn, 2)}, games=games,
              live=sum(1 for x in GAMES.values() if x["status"] == "playing"), queue=sum(len(v) for v in QUEUE.values()))


@bp.post("/game/bomb/admin/config")
@admin_required
def a_config():
    return ok(cfg=save_cfg(body()))


def start_jobs():
    def loop():
        time.sleep(15)
        try: print("[bomb] restored", restore(), "running game(s)", flush=True)
        except Exception as e: print("[bomb] restore:", e, flush=True)
        n = 0
        while True:
            time.sleep(5)
            n += 1
            try: janitor()
            except Exception as e: print("[bomb] janitor:", e, flush=True)
            if n % 720 == 0:
                try: purge_old(14)
                except Exception as e: print("[bomb] purge:", e, flush=True)
    threading.Thread(target=loop, daemon=True, name="bomb-jobs").start()
