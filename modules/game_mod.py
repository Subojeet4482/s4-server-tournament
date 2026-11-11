"""S3 - COIN BATTLE (6x6 connect-four) - server-authoritative game, ported from the old Main panel.

Game state lives in DB3 (ball_games / ball_stats / ball_totals / config/ball).  Wallet money (entry fee, prize, refund) is moved on
Server-1 (or Server-4) through svc.wallet_debit / svc.wallet_credit, so S3 never touches the balance database.
The phone polls  POST /game/ball/game  (instead of listening to Firestore)."""
import os, time, random, threading, uuid
import datetime as dt
from flask import Blueprint, g as flask_g
from firebase_admin import firestore as fs
from common import fbx, svc
from common.authz import user_required
from common.util import ok, err, body, rate_ok

bp = Blueprint("game", __name__)


def C():
    return fbx.db("DB3")


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
W = H = 6                      # board size
LINE = 4                       # balls in a row to win
NOW = lambda: int(time.time() * 1000)

DEFAULT_CFG = {"enabled": True, "entries": [2, 5, 10], "cutPct": 10, "practicePerDay": 3, "botsEnabled": True, "botAfterFails": 2,
               "searchSec": 12, "turnSec": 15, "maxAuto": 3, "botDepth": 7, "botMs": 700, "maxStake": 100}

BOT_NAMES = ["Arjun", "Rohit", "Vikram", "Karan", "Aman", "Neha", "Priya", "Sneha", "Ravi", "Dev", "Isha", "Rahul", "Zoya", "Kabir", "Meera", "Ayaan", "Tanya", "Yash", "Riya", "Sahil"]
BOT_AVATARS = 5                # assets/img/game/bots/b1..b5.webp  (separate list from player avatars)

# ==================================================================== engine
def lines():
    L = []
    for r in range(H):
        for c in range(W):
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                er, ec = r + dr * (LINE - 1), c + dc * (LINE - 1)
                if 0 <= er < H and 0 <= ec < W: L.append(tuple((r + dr * i) * W + (c + dc * i) for i in range(LINE)))
    return L
LINES = lines()
CELL_LINES = [[] for _ in range(W * H)]
for _i, _l in enumerate(LINES):
    for _c in _l: CELL_LINES[_c].append(_i)
CENTER = [3 - abs(c - (W - 1) / 2) for c in range(W)]
ORDER = sorted(range(W), key=lambda c: -CENTER[c])


def new_board(): return [0] * (W * H)
def drop_row(b, col):
    if not (0 <= col < W): return None
    for r in range(H - 1, -1, -1):
        if b[r * W + col] == 0: return r
    return None
def valid_cols(b): return [c for c in range(W) if b[c] == 0]
def win_cells(b, p, last=None):
    """cells of a winning line for player p (1/2) or None. If `last` (cell idx) is given only lines through it are checked."""
    idxs = CELL_LINES[last] if last is not None else range(len(LINES))
    for i in idxs:
        l = LINES[i]
        if b[l[0]] == p and b[l[1]] == p and b[l[2]] == p and b[l[3]] == p: return list(l)
    return None


# ==================================================================== bot (alpha-beta, iterative deepening, time-boxed)
class Timeout(Exception): pass

def _eval(b, me):
    opp = 3 - me; s = 0
    for l in LINES:
        m = o = 0
        for i in l:
            v = b[i]
            if v == me: m += 1
            elif v == opp: o += 1
        if m and o: continue
        if m == 3: s += 60
        elif m == 2: s += 9
        elif m == 1: s += 1
        elif o == 3: s -= 70
        elif o == 2: s -= 10
        elif o == 1: s -= 1
    for r in range(H):
        for c in range(W):
            v = b[r * W + c]
            if v == me: s += CENTER[c]
            elif v == opp: s -= CENTER[c]
    return s

class Searcher:
    def __init__(self, me, deadline):
        self.me, self.deadline, self.nodes, self.tt = me, deadline, 0, {}
    def negamax(self, b, player, depth, alpha, beta, last):
        self.nodes += 1
        if self.nodes & 511 == 0 and time.time() > self.deadline: raise Timeout()
        opp = 3 - player
        if last is not None and win_cells(b, opp, last): return -100000 - depth
        cols = [c for c in ORDER if b[c] == 0]
        if not cols: return 0
        if depth == 0: return _eval(b, player)
        key = (tuple(b), player)
        hit = self.tt.get(key)
        if hit and hit[0] >= depth: return hit[1]
        best = -10 ** 9
        for c in cols:
            r = drop_row(b, c); i = r * W + c; b[i] = player
            v = -self.negamax(b, opp, depth - 1, -beta, -alpha, i)
            b[i] = 0
            if v > best: best = v
            if best > alpha: alpha = best
            if alpha >= beta: break
        self.tt[key] = (depth, best)
        return best

def bot_move(board, me, depth=7, ms=700):
    """Best column for `me` (1/2). Immediate win > block > deep search (time-boxed). No cheating: only sees the board."""
    cols = valid_cols(board)
    if not cols: return None
    b = list(board); opp = 3 - me
    for p in (me, opp):                                   # win now, else block now
        for c in ORDER:
            if b[c] == 0:
                r = drop_row(b, c); i = r * W + c; b[i] = p; w = win_cells(b, p, i); b[i] = 0
                if w: return c
    deadline = time.time() + max(80, ms) / 1000.0
    best, bestv = None, None
    safe = []
    for c in ORDER:                                       # avoid moves that hand the opponent an instant win
        if b[c] != 0: continue
        r = drop_row(b, c); i = r * W + c; b[i] = me
        bad = False
        r2 = drop_row(b, c)
        if r2 is not None:
            j = r2 * W + c; b[j] = opp; bad = bool(win_cells(b, opp, j)); b[j] = 0
        b[i] = 0
        if not bad: safe.append(c)
    cand = safe or cols
    for d in range(2, max(2, int(depth)) + 1):
        s = Searcher(me, deadline); results = {}
        try:
            alpha = -10 ** 9
            for c in cand:
                r = drop_row(b, c); i = r * W + c; b[i] = me
                v = -s.negamax(b, opp, d - 1, -10 ** 9, -alpha, i)
                b[i] = 0; results[c] = v
                if v > alpha: alpha = v
        except Timeout:
            break
        if results:
            bestv = max(results.values()); top = [c for c, v in results.items() if v >= bestv - 1]
            best = random.choice(top) if len(top) > 1 and d >= 3 else max(results, key=results.get)
            cand = sorted(results, key=lambda c: -results[c]) or cand
        if bestv is not None and bestv > 90000: break
    return best if best is not None else cand[0]


_cfg = {"t": 0, "v": dict(DEFAULT_CFG)}
LOCK = threading.RLock()
QUEUE = {}          # key -> list of tickets
TICKETS = {}        # ticket -> dict
FAILS = {}          # uid -> {"n": consecutive failed searches, "t": last}
GAMES = {}          # gid -> game dict (cache; Firestore is the truth)
GLOCKS = {}         # gid -> RLock


# ==================================================================== config
def cfg(force=False):
    if force or time.time() - _cfg["t"] > 20:
        try:
            d = C().collection("config").document("ball").get()
            v = dict(DEFAULT_CFG); v.update((d.to_dict() or {}) if d.exists else {}); _cfg["v"] = v
        except Exception: pass
        _cfg["t"] = time.time()
    return _cfg["v"]

def save_cfg(patch):
    v = dict(DEFAULT_CFG); v.update(cfg(True))
    ent = patch.get("entries")
    if ent is not None: v["entries"] = sorted({int(x) for x in ent if 1 <= int(x) <= int(v.get("maxStake", 100))})[:6] or [2, 5, 10]
    for k, lo, hi in (("cutPct", 0, 50), ("practicePerDay", 0, 50), ("botAfterFails", 1, 5), ("searchSec", 4, 40), ("turnSec", 6, 60), ("maxAuto", 0, 10), ("botDepth", 2, 9), ("botMs", 80, 2500), ("maxStake", 1, 1000)):
        if k in patch: v[k] = max(lo, min(hi, int(patch[k])))
    for k in ("enabled", "botsEnabled"):
        if k in patch: v[k] = bool(patch[k])
    C().collection("config").document("ball").set(v)
    _cfg.update(t=time.time(), v=v); return v

def public_cfg():
    c = cfg(); return {k: c[k] for k in ("enabled", "entries", "cutPct", "practicePerDay", "botsEnabled", "searchSec", "turnSec", "maxAuto", "botAfterFails")}

def today():  return dt.datetime.now(IST).strftime("%Y%m%d")
def glock(gid):
    with LOCK: return GLOCKS.setdefault(gid, threading.RLock())


# ==================================================================== wallet (through Server-1 / Server-4)
def debit(uid, amt, title):
    """take `amt` (deposit balance first, then winnings). False if the wallet is too low."""
    try: return bool(svc.wallet_debit(uid, amt, title, "game_entry")[0])
    except Exception as e: print("[ball] debit", e, flush=True); return False

def credit(uid, amt, title, kind):
    for attempt in range(3):                      # a payout must not be lost on a short network error
        try:
            if svc.wallet_credit(uid, amt, title, kind)[0]: return True
        except Exception as e: print("[ball] credit", e, flush=True)
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError("credit failed for " + uid)

def lb_add(uid, name, photo, amt):
    try: svc.lb_add(uid, name, photo, amt)
    except Exception as e: print("[ball] lb", e, flush=True)

def stat_add(uid, **inc):
    try: C().collection("ball_stats").document(uid).set({k: fs.Increment(v) for k, v in inc.items()}, merge=True)
    except Exception as e: print("[ball] stats:", e, flush=True)

def player_info(uid):
    b = svc.user_brief(uid) or {}
    return {"uid": uid, "name": (b.get("appName") or "Player")[:20], "photo": b.get("photoUrl") or "", "bot": False}

def bot_info():
    return {"uid": "bot:" + uuid.uuid4().hex[:8], "name": random.choice(BOT_NAMES), "photo": "", "avatar": random.randint(1, BOT_AVATARS), "bot": True}


# ==================================================================== games
def public_game(g):
    keep = ("id", "uids", "players", "stake", "practice", "board", "turn", "status", "winner", "reason", "moves", "deadline", "auto", "createdAt", "finishedAt", "prize", "cutPct", "turnSec", "winCells", "settled")
    return {k: g.get(k) for k in keep}

def load_game(gid):
    g = GAMES.get(gid)
    if g: return g
    s = C().collection("ball_games").document(gid).get()
    if not s.exists: return None
    g = s.to_dict(); g["id"] = gid; GAMES[gid] = g; return g

def save_game(g):
    GAMES[g["id"]] = g
    d = {k: v for k, v in g.items() if k != "id"}
    C().collection("ball_games").document(g["id"]).set(d)

def active_game_of(uid):
    with LOCK:
        for g in GAMES.values():
            if g["status"] == "playing" and uid in g["uids"]: return g
    return None

def create_game(p0, p1, stake, practice):
    """p0/p1 player dicts. Takes the entry fee from every human first; refunds if the 2nd debit fails."""
    c = cfg(); gid = uuid.uuid4().hex[:14]; taken = []
    if not practice:
        for p in (p0, p1):
            if p["bot"]: continue
            if not debit(p["uid"], stake, "Coin Battle entry"):
                for q in taken: credit(q, stake, "Coin Battle refund", "game_refund")
                return None, p["uid"]
            taken.append(p["uid"])
    first = random.choice((0, 1))
    g = {"id": gid, "uids": [p0["uid"], p1["uid"]], "players": [p0, p1], "stake": stake, "practice": practice, "board": new_board(), "turn": first, "status": "playing",
         "winner": -1, "reason": "", "moves": [], "auto": [0, 0], "createdAt": NOW(), "cutPct": c["cutPct"], "turnSec": c["turnSec"], "winCells": [], "settled": False,
         "deadline": NOW() + c["turnSec"] * 1000 + 8000}      # + reveal screen & coin toss on the phone
    save_game(g)
    for p in (p0, p1):
        if not p["bot"]: stat_add(p["uid"], games=1)
    if p0["bot"] or p1["bot"]: _bot_turns(g)
    return g, None

def _prize(g): return round(g["stake"] * 2 * (100 - g["cutPct"]) / 100.0, 2)

def finish(g, winner, reason):
    """winner: 0/1 or -1 draw. Idempotent — pays out exactly once."""
    with glock(g["id"]):
        if g["status"] == "done": return g
        g.update(status="done", winner=winner, reason=reason, finishedAt=NOW(), settled=False)
        humans = [i for i, p in enumerate(g["players"]) if not p["bot"]]
        stake, practice = g["stake"], g["practice"]
        prize = _prize(g) if not practice else 0
        g["prize"] = prize
        try:
            if not practice:
                if winner == -1:
                    for i in humans: credit(g["uids"][i], stake, "Coin Battle — draw refund", "game_refund")
                elif winner in humans:
                    p = g["players"][winner]
                    credit(p["uid"], prize, "Coin Battle — You won", "game_win"); lb_add(p["uid"], p["name"], p["photo"], prize)
                    stat_add(p["uid"], wins=1, won=prize)
                for i in humans:
                    if winner not in (i, -1): stat_add(g["uids"][i], losses=1, lost=stake)
                tot = {"games": 1, "entries": stake * len(humans), "botGames": 1 if len(humans) == 1 else 0}
                tot["payouts"] = prize if winner in humans else 0
                tot["refunds"] = stake * len(humans) if winner == -1 else 0
                C().collection("ball_totals").document("all").set({k: fs.Increment(v) for k, v in tot.items()}, merge=True)
            else:
                for i in humans: stat_add(g["uids"][i], practiceGames=1)
            g["settled"] = True
        except Exception as e:
            print("[ball] SETTLE ERROR", g["id"], e, flush=True); g["settleError"] = str(e)[:200]
        save_game(g); return g

def apply_move(g, pi, col, delay=0):
    """apply a drop for player index pi. returns error string or None."""
    b = g["board"]; r = drop_row(b, col)
    if r is None: return "column full"
    i = r * W + col; b[i] = pi + 1
    g["moves"].append({"p": pi, "c": col, "r": r, "d": int(delay)})
    w = win_cells(b, pi + 1, i)
    if w: g["winCells"] = w; finish(g, pi, "connect4"); return None
    if all(b): finish(g, -1, "draw"); return None
    g["turn"] = 1 - pi; g["deadline"] = NOW() + int(delay) + g["turnSec"] * 1000 + 600
    return None

def _bot_turns(g):
    """if it is a bot's turn, play it now (the move carries a 'think' delay so the phone animates it naturally)."""
    c = cfg(); guard = 0
    while g["status"] == "playing" and g["players"][g["turn"]]["bot"] and guard < 3:
        guard += 1; pi = g["turn"]
        col = bot_move(g["board"], pi + 1, c["botDepth"], c["botMs"])
        apply_move(g, pi, col if col is not None else random.choice(valid_cols(g["board"])), delay=random.randint(650, 1500))
    if g["status"] == "playing": save_game(g)

def human_move(uid, gid, col):
    with glock(gid):
        g = load_game(gid)
        if not g: return None, "game not found"
        if uid not in g["uids"]: return None, "not your game"
        if g["status"] != "playing": return public_game(g), None
        pi = g["uids"].index(uid)
        if g["turn"] != pi: return public_game(g), "not your turn"
        err = apply_move(g, pi, int(col))
        if err: return public_game(g), err
        g["auto"][pi] = g["auto"][pi]                     # a real move keeps the AFK counter (only resets on 3 clean moves? keep simple)
        if g["status"] == "playing": _bot_turns(g)
        save_game(g); return public_game(g), None

def tick(gid):
    """anyone in the game may call: handles a missed turn (auto-move; too many misses = forfeit)."""
    with glock(gid):
        g = load_game(gid)
        if not g or g["status"] != "playing": return public_game(g) if g else None
        if NOW() <= g["deadline"]: return public_game(g)
        pi = g["turn"]
        if g["players"][pi]["bot"]: _bot_turns(g); return public_game(g)
        g["auto"][pi] += 1
        if g["auto"][pi] > cfg()["maxAuto"]: finish(g, 1 - pi, "afk"); return public_game(g)
        cols = valid_cols(g["board"]); col = bot_move(g["board"], pi + 1, 3, 150) or random.choice(cols)
        apply_move(g, pi, col)
        if g["status"] == "playing": _bot_turns(g)
        save_game(g); return public_game(g)

def leave(uid, gid):
    with glock(gid):
        g = load_game(gid)
        if not g or uid not in g["uids"] or g["status"] != "playing": return public_game(g) if g else None
        pi = g["uids"].index(uid); finish(g, 1 - pi, "forfeit"); return public_game(g)


# ==================================================================== matchmaking
def _key(practice, stake): return "p" if practice else f"s{stake}"

def find(uid, entry, practice):
    c = cfg()
    if not c["enabled"]: return {"error": "Coin Battle is switched off right now"}
    if practice:
        st = C().collection("ball_stats").document(uid).get().to_dict() or {}
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
            if t["uid"] != uid and time.time() - t["ts"] < c["searchSec"] + 4:
                opp = t["player"]
                g, bad = create_game(opp, me, entry, practice)
                QUEUE[key].remove(t)
                if g is None:
                    if bad == me["uid"]: return {"error": "Not enough balance — add money first"}
                    t["status"] = "cancelled"; continue
                t.update(status="matched", gid=g["id"]); FAILS.pop(uid, None); FAILS.pop(opp["uid"], None)
                if practice:
                    _practice_used(uid); _practice_used(opp["uid"])
                return {"status": "matched", "gid": g["id"]}
        tk = uuid.uuid4().hex[:12]
        t = {"id": tk, "uid": uid, "key": key, "entry": entry, "practice": practice, "player": me, "ts": time.time(), "status": "waiting", "gid": None}
        TICKETS[tk] = t; QUEUE.setdefault(key, []).append(t)
        attempt = FAILS.get(uid, {}).get("n", 0) + 1
        return {"status": "waiting", "ticket": tk, "searchSec": c["searchSec"], "attempt": attempt, "botAfter": c["botAfterFails"]}

def _practice_used(uid):
    ref = C().collection("ball_stats").document(uid); s = ref.get().to_dict() or {}
    n = (int(s.get("practiceUsed", 0) or 0) + 1) if s.get("practiceDay") == today() else 1
    ref.set({"practiceDay": today(), "practiceUsed": n}, merge=True)

def _drop_ticket(t):
    t["status"] = "cancelled"
    try: QUEUE.get(t["key"], []).remove(t)
    except ValueError: pass

def poll(uid, tk):
    c = cfg()
    with LOCK:
        t = TICKETS.get(tk)
        if not t or t["uid"] != uid: return {"status": "none"}
        if t["status"] == "matched": return {"status": "matched", "gid": t["gid"]}
        if t["status"] != "waiting": return {"status": "cancelled"}
        if time.time() - t["ts"] < c["searchSec"]: return {"status": "waiting", "left": int(c["searchSec"] - (time.time() - t["ts"]))}
        _drop_ticket(t)
        f = FAILS.setdefault(uid, {"n": 0, "t": 0})
        if time.time() - f["t"] > 900: f["n"] = 0
        f["n"] += 1; f["t"] = time.time()
        if c["botsEnabled"] and f["n"] >= c["botAfterFails"]:
            g, bad = create_game(t["player"], bot_info(), t["entry"], t["practice"])
            if g is None: return {"status": "error", "error": "Not enough balance — add money first"}
            f["n"] = 0
            if t["practice"]: _practice_used(uid)
            return {"status": "matched", "gid": g["id"], "bot": True}
        return {"status": "nomatch", "fails": f["n"], "botAfter": c["botAfterFails"]}




# ==================================================================== HTTP
def _guard(uid):
    return None if rate_ok("ball:" + uid, 60, 10) else err("Too many requests - slow down", 429)

def _mine(gid, uid):
    gm = load_game(str(gid or ""))
    return gm if gm and uid in gm["uids"] else None


@bp.post("/game/ball/config")
@user_required
def r_config():
    uid = flask_g.uid
    st = C().collection("ball_stats").document(uid).get().to_dict() or {}
    used = int(st.get("practiceUsed", 0) or 0) if st.get("practiceDay") == today() else 0
    ag = active_game_of(uid)
    return ok(cfg=public_cfg(), practiceLeft=max(0, cfg()["practicePerDay"] - used), active=ag["id"] if ag else None,
              stats={k: st.get(k, 0) for k in ("games", "wins", "losses", "won", "lost")}, now=NOW())


@bp.post("/game/ball/find")
@user_required
def r_find():
    uid, d = flask_g.uid, body()
    bad = _guard(uid)
    if bad: return bad
    r = find(uid, d.get("entry"), bool(d.get("practice")))
    if "error" in r: return err(r["error"], 400, **{k: v for k, v in r.items() if k != "error"})
    return ok(**r)


@bp.post("/game/ball/poll")
@user_required
def r_poll():
    r = poll(flask_g.uid, str(body().get("ticket", "")))
    if r.get("status") == "error": return err(r.get("error", "error"), 400)
    return ok(**r)


@bp.post("/game/ball/cancel")
@user_required
def r_cancel():
    with LOCK:
        t = TICKETS.get(str(body().get("ticket", "")))
        if t and t["uid"] == flask_g.uid and t["status"] == "waiting": _drop_ticket(t)
    return ok()


@bp.post("/game/ball/move")
@user_required
def r_move():
    uid, d = flask_g.uid, body()
    bad = _guard(uid)
    if bad: return bad
    gm, e = human_move(uid, str(d.get("gid", "")), d.get("col", -1))
    if e: return err(e, 400, game=gm)
    return ok(game=gm, now=NOW())


@bp.post("/game/ball/tick")
@user_required
def r_tick():
    gm = _mine(body().get("gid"), flask_g.uid)
    if not gm: return err("game not found", 404)
    return ok(game=tick(gm["id"]), now=NOW())


@bp.post("/game/ball/leave")
@user_required
def r_leave():
    return ok(game=leave(flask_g.uid, str(body().get("gid", ""))))


@bp.post("/game/ball/game")
@user_required
def r_game():
    gm = _mine(body().get("gid"), flask_g.uid)
    if not gm: return err("game not found", 404)
    return ok(game=public_game(gm), now=NOW())


# ---- admin / internal (used by the future admin panel through the load balancer with the admin key)
from common.authz import admin_required

@bp.get("/game/ball/admin/overview")
@admin_required
def a_overview():
    tot = C().collection("ball_totals").document("all").get().to_dict() or {}
    games = []
    for s in C().collection("ball_games").order_by("createdAt", direction="DESCENDING").limit(40).stream():
        x = s.to_dict(); games.append({"id": s.id, "t": x.get("createdAt"), "stake": x.get("stake"), "practice": x.get("practice"), "status": x.get("status"),
                                       "winner": x.get("winner"), "reason": x.get("reason"), "prize": x.get("prize"), "moves": len(x.get("moves", [])),
                                       "players": [{"name": p.get("name"), "bot": p.get("bot")} for p in x.get("players", [])]})
    hn = (tot.get("entries", 0) or 0) - (tot.get("payouts", 0) or 0) - (tot.get("refunds", 0) or 0)
    return ok(cfg=cfg(True), totals={**{k: tot.get(k, 0) for k in ("games", "botGames", "entries", "payouts", "refunds")}, "houseNet": round(hn, 2)}, games=games,
              live=sum(1 for x in GAMES.values() if x["status"] == "playing"))


@bp.post("/game/ball/admin/config")
@admin_required
def a_config():
    return ok(cfg=save_cfg(body()))


def restore():
    """after a restart: reload games that were still being played (money is already taken - they must finish and settle)."""
    n = 0
    for s in C().collection("ball_games").where("status", "==", "playing").limit(200).stream():
        x = s.to_dict(); x["id"] = s.id; GAMES[s.id] = x; n += 1
    return n


def start_jobs():
    def loop():
        time.sleep(15)
        try: print("[ball] restored games:", restore(), flush=True)
        except Exception as e: print("[ball] restore", e, flush=True)
        while True:                          # finish games nobody ticks any more (AFK / closed app)
            time.sleep(10)
            try:
                for gid in [k for k, x in list(GAMES.items()) if x["status"] == "playing" and NOW() > x["deadline"] + 20000]:
                    tick(gid)
            except Exception as e: print("[ball] sweep", e, flush=True)
    threading.Thread(target=loop, daemon=True, name="ball-sweep").start()


# ------------------------------------------------------------------ ON/OFF switches for the game cards (the app hides a game the admin switched off)
@bp.post("/game/flags")
@user_required
def game_flags():
    from . import bomb_mod
    return ok(ball=bool(cfg().get("enabled", True)), bomb=bool(bomb_mod.cfg().get("enabled", True)))
