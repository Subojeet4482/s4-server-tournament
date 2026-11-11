"""Wallet / user core on DB1 (same collections as the existing app + admin panel, so the admin panel keeps working):
   users/{uid}  (+ subcollections transactions, notifications) · lb/{d|w|m}-key · config/*
All money moves inside Firestore transactions and are mirrored to the history server (S2) via svc.history_push."""
import re, time, datetime as dt
from firebase_admin import firestore as fs
from . import fbx, authz, peers, geo
from .util import now_ms, money, num, IST

SERVER_TS = fs.SERVER_TIMESTAMP


def D():
    return fbx.db("DB1")


def uref(uid):
    return D().collection("users").document(uid)


def bal(u):
    dep = u.get("depositBalance")
    dep = num(dep if dep is not None else u.get("balance", 0))
    return round(dep, 2), round(num(u.get("withdrawBalance", 0)), 2)


def ban_info(u):
    """Everything the app's ban screen shows (Player ID, reason, length). Sent with the 403 'banned' error."""
    u = u or {}
    return {"bannedUntil": u.get("bannedUntil") or "permanent", "reason": u.get("banReason", ""), "banDays": u.get("banDays"), "bannedAt": u.get("bannedAt"), "playerId": u.get("playerId", "")}


def is_banned(u):
    if u.get("blocked") or u.get("banned"):
        return True
    if u.get("isBanned") is True:
        until = u.get("bannedUntil")
        if until and until != "permanent":
            try:
                ts = dt.datetime.fromisoformat(str(until).replace("Z", "+00:00")).timestamp()
                return ts > dt.datetime.now().timestamp()
            except Exception:
                return True
        return True
    return False


def parse_amount(s):
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"([+-])?\s*₹?\s*([\d.,]+)", str(s or ""))
    if not m:
        return 0.0
    v = float(m.group(2).replace(",", "") or 0)
    return -v if m.group(1) == "-" else v


def trx_doc(title, signed_amount, ttype, status="success", **extra):
    a = float(signed_amount)
    sign = "+" if a >= 0 else "-"
    d = {"title": title, "amount": f"{sign}₹{abs(a):g}", "type": ttype, "status": status,
         "date": dt.datetime.now(IST).strftime("%d/%m/%Y"), "createdAt": now_ms()}
    d.update(extra)
    return d


def entries_from_trx(uid, tid, t):
    """One DB1 transaction doc -> history entries for S2. The ORIGINAL fields are kept as they are (title, amount "+₹5", type, status, kind,
    method, net, upiId ... - the app's wallet / receipt screens read exactly these) + amountNum. Ids are idempotent."""
    typ, status, kind = t.get("type"), t.get("status"), t.get("kind") or ""
    amt = parse_amount(t.get("amount"))
    base = {k: v for k, v in fbx.enc(dict(t)).items() if k != "timestamp" and v is not None}
    ts = t.get("timestamp")                                  # older docs only have the server timestamp
    ts_ms = int(ts.timestamp() * 1000) if hasattr(ts, "timestamp") else (int(ts) if isinstance(ts, (int, float)) else 0)
    base.update(amountNum=amt, srcId=tid, createdAt=t.get("createdAt") or ts_ms or now_ms())
    base.setdefault("trxId", tid)
    out = []
    if typ == "deposit":
        out.append({"kind": "deposit", "uid": uid, "id": tid, "data": base})
    elif typ == "withdraw":
        out.append({"kind": "withdrawal", "uid": uid, "id": tid, "data": base})
    elif typ == "game":
        if kind.startswith("game_"):
            out.append({"kind": "game", "uid": uid, "id": tid, "data": base})
        elif kind == "match_join":
            out.append({"kind": "match", "uid": uid, "id": tid, "data": base})
    if status == "success" or typ == "withdraw":
        out.append({"kind": "balance", "uid": uid, "id": "b-" + tid, "data": dict(base)})
    return out


def push(entries):
    from . import svc
    return svc.history_push(entries)


def notify(uid, title, body, ntype="wallet"):
    try:
        uref(uid).collection("notifications").add({
            "title": title, "body": body, "message": body, "type": ntype, "read": False,
            "date": dt.datetime.now(IST).strftime("%d %b %Y"), "createdAt": now_ms(), "timestamp": SERVER_TS})
    except Exception as e:
        print("[notify]", e, flush=True)


def _clean(t):
    return {k: v for k, v in t.items() if k != "timestamp"}


# ------------------------------------------------------------------ debit / credit
def debit(uid, amount, title, kind="game_entry", ttype="game", extra=None):
    """Take `amount` (deposit balance first, then winnings). -> (ok, info)"""
    amount = money(amount)
    if amount <= 0:
        return False, {"error": "Invalid amount"}
    ref = uref(uid)
    tref = ref.collection("transactions").document()
    gcfg = geo.get_cfg()

    @fs.transactional
    def run(tx):
        s = ref.get(transaction=tx)
        if not s.exists:
            return {"error": "User not found"}
        u = s.to_dict() or {}
        if is_banned(u):
            return {"error": "Account blocked", "code": "banned"}
        if kind == "game_entry":                          # paid game entry: blocked in banned states (defence in depth - S3 checks too)
            ev = geo.evaluate(u, gcfg)
            if not ev["canPlayPaidGame"]:
                return {"error": ev["message"], "code": "payment_blocked_state"}
        dep, wd = bal(u)
        if dep + wd + 1e-9 < amount:
            return {"error": "Insufficient balance", "code": "insufficient", "dep": dep, "wd": wd}
        fd = min(dep, amount)
        fw = round(amount - fd, 2)
        tx.update(ref, {"depositBalance": round(dep - fd, 2), "withdrawBalance": round(wd - fw, 2)})
        t = trx_doc(title, -amount, ttype, "success", kind=kind, balanceType="deposit" if fw == 0 else "mixed", **(extra or {}))
        tx.set(tref, dict(t, timestamp=SERVER_TS))
        return {"ok": True, "dep": round(dep - fd, 2), "wd": round(wd - fw, 2), "payDep": fd, "payWd": fw, "tid": tref.id, "trx": t}
    res = run(D().transaction())
    if not res.get("ok"):
        return False, res
    push(entries_from_trx(uid, res["tid"], res["trx"]))
    return True, {k: v for k, v in res.items() if k != "trx"}


def credit(uid, amount, title, kind="game_win", ttype="game", extra=None):
    """kind game_win -> winnings (withdraw) balance, anything else -> deposit balance."""
    amount = money(amount)
    if amount <= 0:
        return False, {"error": "Invalid amount"}
    ref = uref(uid)
    tref = ref.collection("transactions").document()
    to_wd = kind in ("game_win", "match_win", "prize")

    @fs.transactional
    def run(tx):
        s = ref.get(transaction=tx)
        if not s.exists:
            return {"error": "User not found"}
        u = s.to_dict() or {}
        dep, wd = bal(u)
        up = {}
        if to_wd:
            wd = round(wd + amount, 2)
            up["withdrawBalance"] = wd
            up["totalEarned"] = round(num(u.get("totalEarned", 0)) + amount, 2)
        else:
            dep = round(dep + amount, 2)
            up["depositBalance"] = dep
        tx.update(ref, up)
        t = trx_doc(title, amount, ttype, "success", kind=kind, balanceType="withdraw" if to_wd else "deposit", **(extra or {}))
        tx.set(tref, dict(t, timestamp=SERVER_TS))
        return {"ok": True, "dep": dep, "wd": wd, "tid": tref.id, "trx": t}
    res = run(D().transaction())
    if not res.get("ok"):
        return False, res
    push(entries_from_trx(uid, res["tid"], res["trx"]))
    return True, {k: v for k, v in res.items() if k != "trx"}


# ------------------------------------------------------------------ misc helpers
def user_brief(uid):
    s = uref(uid).get()
    if not s.exists:
        return None
    u = s.to_dict() or {}
    dep, wd = bal(u)
    ev = geo.evaluate(u)
    return {"uid": uid, "appName": u.get("appName") or "Player", "photoUrl": u.get("photoUrl") or "", "playerId": u.get("playerId"),
            "banned": is_banned(u), "dep": dep, "wd": wd, "isVerified": bool(u.get("isVerified")),
            "paidGameBlocked": not ev["canPlayPaidGame"], "geoMessage": ev["message"]}


def lb_add(uid, name, photo, amount):
    n = dt.datetime.now(IST)
    mon = n - dt.timedelta(days=n.weekday())
    for key in ("d-" + n.strftime("%Y%m%d"), "w-" + mon.strftime("%Y%m%d"), "m-" + n.strftime("%Y%m")):
        pl = {"n": name or "Player", "a": fs.Increment(amount), "g": fs.Increment(amount)}
        if photo:
            pl["p"] = photo
        D().collection("lb").document(key).set({"players": {uid: pl}, "updatedAt": now_ms()}, merge=True)


def ban_user(uid, reason="", by="system", until="permanent"):
    uref(uid).set({"isBanned": True, "bannedUntil": until, "banReason": str(reason)[:300], "bannedBy": by, "bannedAt": now_ms()}, merge=True)
    authz.revoke_user(uid, time.time() + 2)
    try:
        peers.broadcast("/internal/revoke", {"uid": uid})
    except Exception:
        pass
    return True


def unban_user(uid, by="admin"):
    uref(uid).set({"isBanned": False, "bannedUntil": None, "unbannedBy": by, "unbannedAt": now_ms()}, merge=True)
    return True


_sync_last = {}
_sync_state = {}          # uid -> {"max": newest createdAt seen, "pending": {trx ids still pending}, "n": syncs since the last full read}


def _entries_for(uid, docs):
    out = []
    for sn in docs:
        out += entries_from_trx(uid, sn.id, _clean(fbx.enc(sn.to_dict() or {})))
    return out


def sync_user_history(uid, force=False, limit=40):
    """Copy the user's DB1 transactions to the history DB (also picks up admin credits / approvals / rejections).
    Cheap by design (the app calls this every few seconds): after the first full read only NEW transactions and the ones still
    'pending' are read (1 + #pending reads). Every 10th run does a full read again as a safety net."""
    import time
    now = time.time()
    if not force and now - _sync_last.get(uid, 0) < 20:
        return 0
    _sync_last[uid] = now
    col = uref(uid).collection("transactions")
    st = _sync_state.get(uid)
    entries, docs = [], []
    if force or st is None or st["n"] >= 10:
        docs = list(col.order_by("createdAt", direction="DESCENDING").limit(limit).stream())
        st = {"max": 0, "pending": set(), "n": 0}
    else:
        docs = list(col.where("createdAt", ">", st["max"]).order_by("createdAt").limit(100).stream())
        if st["pending"]:
            seen = {d.id for d in docs}
            docs += [d for d in W_get_all(uid, [col.document(i) for i in st["pending"] if i not in seen]) if d.exists]
        st["n"] += 1
    for d in docs:
        t = d.to_dict() or {}
        ms = int(t.get("createdAt") or 0)
        if ms > st["max"]:
            st["max"] = ms
        if str(t.get("status")) in ("pending", "processing"):
            st["pending"].add(d.id)
        else:
            st["pending"].discard(d.id)
    _sync_state[uid] = st
    if len(_sync_state) > 5000:
        _sync_state.clear()
    entries = _entries_for(uid, docs)
    if entries:
        push(entries)
    return len(docs)


def W_get_all(uid, refs):
    return list(D().get_all(refs)) if refs else []


def verified_uids(uids):
    """-> set of uids whose users/{uid}.isVerified is true (blue tick in chat)."""
    uids = [u for u in dict.fromkeys(uids) if u][:100]
    if not uids:
        return set()
    refs = [uref(u) for u in uids]
    return {snap.id for snap in D().get_all(refs) if snap.exists and (snap.to_dict() or {}).get("isVerified") is True}
