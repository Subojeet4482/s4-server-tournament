"""S1 - matches: list, details (room id/password only for joined players), join (atomic fee debit), slot pick, roster."""
import re, time, datetime as dt
from flask import Blueprint, request, g
from firebase_admin import firestore as fs
from common import fbx, geo, wallet_core as W
from common.authz import user_required, user_optional, client_ip
from common.util import ok, err, body, rate_ok, now_ms, clean_str, num, money, IST

bp = Blueprint("match", __name__)
CLOSED = ("completed", "live", "cancelled", "canceled", "closed")
_cache = {"t": 0, "items": []}
SECRET = ("roomId", "roomPass", "password", "participants")


def _all_matches():
    if time.time() - _cache["t"] > 5:
        items = [fbx.doc_dict(s) for s in W.D().collection("matches").stream()]
        items.sort(key=lambda m: m.get("startTime") if isinstance(m.get("startTime"), (int, float)) else 0)
        _cache.update(t=time.time(), items=items)
    return _cache["items"]


def _my_slots(m, uid):
    for p in m.get("participants") or []:
        if p.get("uid") == uid:
            s = p.get("slots") or ([p["slot"]] if p.get("slot") else [])
            return [int(x) for x in s]
    return []


def public_match(m, uid=None, joined_ids=(), ev=None):
    parts = m.get("participants") or []
    is_in = bool(uid) and (m["id"] in joined_ids or any(p.get("uid") == uid for p in parts))
    out = {k: v for k, v in m.items() if k not in SECRET}
    out["joined"] = int(m.get("joined") or len(parts))
    out["hasPassword"] = bool(str(m.get("password") or "").strip())
    out["isJoined"] = is_in
    out["mySlots"] = _my_slots(m, uid) if is_in else []
    mine = next((p for p in parts if p.get("uid") == uid), None) if uid else None
    out["mine"] = {k: v for k, v in mine.items() if k not in ("uid", "gameUid")} if mine else None       # own slot / kills / winAmount (needed for the result toast)
    out["participants"] = [dict(out["mine"], uid=uid)] if out["mine"] else []          # ONLY the user's own record (others: GET /matches/{id}/players)
    out["isFree"] = money(m.get("fee")) <= 0
    out["locked"] = bool(ev and not ev["canJoinPaid"] and not out["isFree"] and not is_in)      # paid match hidden-state user: show the 'free matches only' message
    out["lockReason"] = ev["message"] if out["locked"] else ""
    if is_in and m.get("status") != "live" and m.get("roomId") and m.get("roomPass"):
        out["roomId"], out["roomPass"] = m["roomId"], m["roomPass"]
    return out


def _ctx(uid):
    """-> (joined match ids, geo evaluation) for the logged-in user, ((), None) for guests."""
    if not uid:
        return (), None
    u = W.uref(uid).get().to_dict() or {}
    return tuple(u.get("joined_matches") or []), geo.evaluate(u)


def _joined_ids(uid):
    return _ctx(uid)[0]


@bp.get("/matches")
@user_optional
def list_matches():
    status, cat = request.args.get("status"), request.args.get("category")
    ids, ev = _ctx(g.uid)
    items = _all_matches()
    if status == "completed":
        items = [m for m in items if m.get("status") == "completed"]
    elif status == "active":
        items = [m for m in items if m.get("status") != "completed"]
    if cat and cat != "All":
        items = [m for m in items if m.get("category") == cat]
    return ok(matches=[public_match(m, g.uid, ids, ev) for m in items], geo=ev)


@bp.get("/matches/mine")
@user_required
def my_matches():
    ids = _joined_ids(g.uid)
    items = [m for m in _all_matches() if m["id"] in ids]
    return ok(matches=[public_match(m, g.uid, ids) for m in items])


def _get(mid):
    s = W.D().collection("matches").document(mid).get()
    return fbx.doc_dict(s) if s.exists else None


@bp.get("/matches/<mid>")
@user_optional
def match_detail(mid):
    m = _get(mid)
    if not m:
        return err("Match not found", 404)
    ids, ev = _ctx(g.uid)
    return ok(match=public_match(m, g.uid, ids, ev))


@bp.get("/matches/<mid>/players")
@user_optional
def roster(mid):
    m = _get(mid)
    if not m:
        return err("Match not found", 404)
    out = []
    for p in m.get("participants") or []:
        s = p.get("slots") or ([p["slot"]] if p.get("slot") else [])
        out.append({"appName": p.get("appName") or "Player", "slots": [int(x) for x in s], "isMe": bool(g.uid) and p.get("uid") == g.uid})     # never game name / UID of others
    return ok(players=out, joined=int(m.get("joined") or len(out)), total=int(m.get("total") or 48))


@bp.post("/matches/<mid>/join")
@user_required
def join(mid):
    d = body()
    game_name, game_uid = clean_str(d.get("gameName"), 30), clean_str(d.get("gameUid"), 20)
    if len(game_name) < 2:
        return err("Enter your Free Fire name.")
    if not re.fullmatch(r"\d{6,12}", game_uid):
        return err("FF UID invalid (6-12 digits)")
    if not rate_ok("join:" + g.uid, 12, 60):
        return err("Too many requests. Wait a moment.", 429)
    db, uid = W.D(), g.uid
    mref, uref = db.collection("matches").document(mid), W.uref(uid)
    tref = uref.collection("transactions").document()
    try:                                                     # fresh state check (current IP) before a paid join
        u0 = uref.get().to_dict() or {}
        geo.refresh(uid, u0, client_ip())
    except Exception as e:
        print("[geo] join refresh failed:", repr(e), flush=True)
    gcfg = geo.get_cfg()

    @fs.transactional
    def run(tx):
        ms, us = mref.get(transaction=tx), uref.get(transaction=tx)
        if not ms.exists:
            return {"error": "Match not found", "http": 404}
        if not us.exists:
            return {"error": "Profile not found", "http": 404}
        m, u = ms.to_dict() or {}, us.to_dict() or {}
        if W.is_banned(u):
            return {"error": "Your account is blocked.", "http": 403, "code": "banned"}
        if m.get("status") in CLOSED:
            return {"error": "Registration for this match is closed.", "code": "closed"}
        parts = m.get("participants") or []
        if mid in (u.get("joined_matches") or []) or any(p.get("uid") == uid for p in parts):
            return {"error": "You have already joined this match.", "code": "already"}
        if int(m.get("joined") or len(parts)) >= int(m.get("total") or 48):
            return {"error": "This match is full.", "code": "full"}
        fee = money(m.get("fee"))
        if fee > 0:
            ev = geo.evaluate(u, gcfg)
            if not ev["canJoinPaid"]:
                return {"error": ev["message"], "code": "payment_blocked_state", "http": 403, "geo": ev}
        dep, wd = W.bal(u)
        if dep + wd + 1e-9 < fee:
            return {"error": "Insufficient balance.", "code": "insufficient", "dep": dep, "wd": wd}
        fd = min(dep, fee)
        fw = round(fee - fd, 2)
        part = {"uid": uid, "appName": u.get("appName") or "Player", "gameName": game_name, "gameUid": game_uid, "payDep": fd, "payWd": fw}
        tx.update(mref, {"joined": fs.Increment(1), "participants": fs.ArrayUnion([part])})
        tx.update(uref, {"depositBalance": round(dep - fd, 2), "withdrawBalance": round(wd - fw, 2), "joined_matches": fs.ArrayUnion([mid]),
                         "matchesPlayed": fs.Increment(1), "gameName": game_name, "gameUid": game_uid})
        t = W.trx_doc(f"Joined {m.get('title', 'match')}", -fee, "game", "success", kind="match_join", matchId=mid, matchTitle=m.get("title"), fee=fee,
                      balanceType="deposit" if fw == 0 else "mixed")
        tx.set(tref, dict(t, timestamp=fs.SERVER_TIMESTAMP))
        return {"ok": True, "dep": round(dep - fd, 2), "wd": round(wd - fw, 2), "trx": t, "fee": fee, "total": int(m.get("total") or 48),
                "teamSize": int(m.get("teamSize") or 1), "mode": m.get("mode")}
    res = run(db.transaction())
    if not res.get("ok"):
        return err(res["error"], res.get("http", 400), code=res.get("code"), geo=res.get("geo"))
    _cache["t"] = 0
    W.push(W.entries_from_trx(uid, tref.id, res["trx"]))
    return ok(joined=True, fee=res["fee"], depositBalance=res["dep"], withdrawBalance=res["wd"], total=res["total"], teamSize=res["teamSize"], mode=res["mode"])


@bp.post("/matches/<mid>/slot")
@user_required
def pick_slot(mid):
    raw = body().get("slots")
    try:
        slots = sorted({int(x) for x in (raw if isinstance(raw, list) else [raw])})
    except (TypeError, ValueError):
        return err("Select a slot first.")
    if not slots:
        return err("Select a slot first.")
    db, uid = W.D(), g.uid
    mref = db.collection("matches").document(mid)

    @fs.transactional
    def run(tx):
        ms = mref.get(transaction=tx)
        if not ms.exists:
            return {"error": "Match not found", "http": 404}
        m = ms.to_dict() or {}
        parts = m.get("participants") or []
        me = next((p for p in parts if p.get("uid") == uid), None)
        if not me:
            return {"error": "Join the match first.", "http": 403}
        if me.get("slot") or me.get("slots"):
            return {"error": "Your slot is already confirmed.", "code": "slot_set"}
        total, size = int(m.get("total") or 48), max(1, int(m.get("teamSize") or 1))
        if len(slots) > size or slots[0] < 1 or slots[-1] > total:
            return {"error": "Invalid slot selection."}
        taken = [int(x) for x in (m.get("takenSlots") or [])]
        clash = [s for s in slots if s in taken]
        if clash:
            return {"error": f"Slot {clash[0]} was just taken. Your entry is safe — choose another slot.", "code": "slot_taken", "slot": clash[0]}
        newp = [dict(p, slot=slots[0], slots=slots) if p.get("uid") == uid else p for p in parts]
        tx.update(mref, {"participants": newp, "takenSlots": taken + slots})
        return {"ok": True}
    res = run(db.transaction())
    if not res.get("ok"):
        return err(res["error"], res.get("http", 409 if res.get("code") == "slot_taken" else 400), code=res.get("code"), slot=res.get("slot"))
    _cache["t"] = 0
    return ok(slots=slots)
