"""S1 - balance, withdraw, deposit verification, gift codes, internal wallet API (used by S3 game / S5 fraud)."""
import re, hashlib, threading, datetime as dt
from flask import Blueprint, request, g
from firebase_admin import firestore as fs
from common import fbx, svc, geo, wallet_core as W
from common.authz import user_required, internal_required, client_ip
from common.config import env_float
from common.util import ok, err, body, rate_ok, now_ms, clean_str, num, money, IST

bp = Blueprint("wallet", __name__)
MAX_WD = lambda: env_float("MAX_WITHDRAW", 50000)
WD_FEE = lambda: env_float("WITHDRAW_FEE", 2)           # UPI fee (Google Play gift cards have no fee)


def _banner():
    """config/banner (admin panel -> Settings): wallet limits + ON/OFF switches. Cached 10 s so an admin change shows up fast."""
    import time
    c = _BANNER
    if time.time() - c["t"] > 10:
        try:
            c["v"] = W.D().collection("config").document("banner").get().to_dict() or {}
        except Exception as e:
            print("[banner]", repr(e), flush=True)
        c["t"] = time.time()
    return c["v"]


_BANNER = {"t": 0.0, "v": {}}


def MIN_WD():
    v = num(_banner().get("minWithdraw"))
    return max(3.0, v) if v >= 1 else env_float("MIN_WITHDRAW", 10)


def MIN_DEP():
    v = num(_banner().get("minDeposit"))
    return max(1.0, v) if v >= 1 else env_float("MIN_DEPOSIT", 5)


def _flag(key, msg_default):
    """None when the feature is ON, else the message to show (admin note or default)."""
    b = _banner()
    fld = {"dep": ("depositEnabled", "depositNote"), "wd": ("withdrawEnabled", "withdrawNote"), "upi": ("wdUpiEnabled", "wdUpiNote"), "gp": ("wdGpEnabled", "wdGpNote")}[key]
    if b.get(fld[0]) is False:
        return str(b.get(fld[1]) or msg_default)[:300]
    return None
GIFT_MAXTRY, GIFT_LOCK_MS = 5, 24 * 3600 * 1000


def inr(v):
    v = float(v)
    return "₹" + (f"{v:g}" if v == int(v) else f"{v:.2f}")


def _geo_guard(uid, u, feature):
    """None if allowed, else a 403 response ('payment hidden for your safety'). Looks at the current IP, the declared state and the saved state."""
    try:
        u2, ev = geo.refresh(uid, u, client_ip())
    except Exception as e:
        print("[geo] refresh failed:", repr(e), flush=True)
        ev = geo.evaluate(u)
    if ev[geo.FEATURE_FLAG[feature]]:
        return None
    return err(ev["message"], 403, **{k: v for k, v in geo.deny(ev).items() if k != "message"}, geo=ev)


def _user_ok(uid):
    s = W.uref(uid).get()
    if not s.exists:
        return None, err("Profile not found", 404, code="no_profile")
    u = s.to_dict() or {}
    if W.is_banned(u):
        return None, err("Your account is blocked.", 403, code="banned")
    return u, None


# ------------------------------------------------------------------ balance
@bp.get("/wallet/balance")
@user_required
def balance():
    u, e = _user_ok(g.uid)
    if e:
        return e
    dep, wd = W.bal(u)
    uid = g.uid
    threading.Thread(target=lambda: W.sync_user_history(uid), daemon=True).start()     # picks up admin approvals (throttled 20s)
    return ok(depositBalance=dep, withdrawBalance=wd, balance=round(dep + wd, 2), totalEarned=num(u.get("totalEarned")),
              totalDeposited=num(u.get("totalDeposited")))


@bp.post("/wallet/sync")
@user_required
def sync():
    return ok(synced=W.sync_user_history(g.uid, force=True))


# ------------------------------------------------------------------ withdraw
UPI_RE = re.compile(r"^[a-z0-9.\-_]{2,64}@[a-z][a-z0-9]{1,30}$")


@bp.post("/wallet/withdraw")
@user_required
def withdraw():
    """UPI (fee) or Google Play gift card (no fee; the admin sends the redeem code later). Bank transfer was removed from the app."""
    d = body()
    method = clean_str(d.get("method"), 10).upper()
    amt = money(d.get("amount"))
    gp = method == "GIFTCARD"
    if method not in ("UPI", "GIFTCARD"):
        return err("Choose UPI or Google Play gift card.")
    off = _flag("wd", "Withdrawals are temporarily disabled. Please try again later.") or (
        _flag("gp", "Google Play gift card is temporarily disabled. Please try again later.") if gp else _flag("upi", "UPI withdrawal is temporarily disabled. Please try again later."))
    if off:
        return err(off, 403, code="disabled")
    if amt < MIN_WD():
        return err(f"Minimum withdrawal is {inr(MIN_WD())}.")
    if amt > MAX_WD():
        return err(f"Maximum withdrawal is {inr(MAX_WD())}.")
    fee = 0.0 if gp else WD_FEE()
    if gp:
        fields = {"giftCard": "Google Play", "giftValue": amt}
        masked = "Google Play Gift Card"
    else:
        name = clean_str(d.get("name"), 60)
        if len(name) < 3:
            return err("Enter the account holder name.")
        upi = clean_str(d.get("upiId"), 80).lower()
        if not UPI_RE.match(upi):
            return err("Enter a valid UPI ID.")
        fields = {"accountName": name, "upiId": upi}
        masked = "UPI " + (upi[:2] + "xxxx" + upi[upi.index("@"):] if len(upi) > 6 else upi)
    u0, e0 = _user_ok(g.uid)
    if e0:
        return e0
    bad = _geo_guard(g.uid, u0, "withdraw")
    if bad:
        return bad
    if not rate_ok("wd:" + g.uid, 3, 60):
        return err("Please wait a minute before another request.", 429)
    ref = W.uref(g.uid)
    tref = ref.collection("transactions").document()

    @fs.transactional
    def run(tx):
        s = ref.get(transaction=tx)
        if not s.exists:
            return {"error": "Profile not found"}
        u = s.to_dict() or {}
        if W.is_banned(u):
            return {"error": "Your account is blocked.", "code": "banned"}
        dep, wd = W.bal(u)
        if amt > wd + 1e-9:
            return {"error": f"Only {inr(wd)} is available to withdraw.", "code": "insufficient", "wd": wd}
        tx.update(ref, {"withdrawBalance": round(wd - amt, 2)})
        t = {"title": f"Withdrawal {inr(amt)} · {masked}", "amount": f"-₹{amt:g}", "net": f"₹{max(amt - fee, 0):.2f}", "type": "withdraw",
             "status": "pending", "method": method, "balanceType": "withdraw", **fields,
             "date": dt.datetime.now(IST).strftime("%d/%m/%Y"), "createdAt": now_ms()}
        tx.set(tref, dict(t, timestamp=fs.SERVER_TIMESTAMP))
        return {"ok": True, "dep": dep, "wd": round(wd - amt, 2), "trx": t}
    res = run(W.D().transaction())
    if not res.get("ok"):
        return err(res["error"], 403 if res.get("code") == "banned" else 400, code=res.get("code"))
    W.push(W.entries_from_trx(g.uid, tref.id, res["trx"]))
    if gp:
        W.notify(g.uid, "Gift Card Request Received", f"We have received your request for a Google Play gift card of {inr(amt)}. The redeem code will appear in this app (Wallet and Notifications) with a Copy button, usually within 15–20 minutes.", "withdraw")
    else:
        W.notify(g.uid, "Withdrawal Request Received", f"We have received your request to withdraw {inr(amt)}. You will receive {inr(max(amt - fee, 0))} within 1-3 working days.", "withdraw")
    return ok(ref=tref.id[:8].upper(), id=tref.id, net=max(amt - fee, 0), depositBalance=res["dep"], withdrawBalance=res["wd"])


# ------------------------------------------------------------------ deposit
def extract_ref(raw):
    t = str(raw or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9]{8,30}", t):              # the app already sends the clean reference
        return t
    toks = re.findall(r"[A-Z0-9]{8,30}", str(raw or "").upper())
    toks = [t for t in toks if t not in ("TRANSACTION", "REFERENCE") and re.search(r"\d", t)]
    return toks[-1] if toks else ""


@bp.post("/wallet/deposit/request")
@user_required
def deposit_request():
    d = body()
    amt = money(d.get("amount"))
    off = _flag("dep", "Deposit is temporarily disabled. Please try again later.")
    if off:
        return err(off, 403, code="disabled")
    if amt < MIN_DEP():
        return err(f"Minimum deposit is {inr(MIN_DEP())}.")
    u, e = _user_ok(g.uid)
    if e:
        return e
    bad = _geo_guard(g.uid, u, "deposit")
    if bad:
        return bad
    if not rate_ok("dep:" + g.uid, 10, 60):
        return err("Too many requests. Wait a minute.", 429)
    now = now_ms()
    ref = W.uref(g.uid)
    utr = extract_ref(d.get("ref"))
    if not utr:
        return err("Enter a valid UTR / Transaction ID.")
    rref = W.D().collection("deposit_requests").document(utr)

    @fs.transactional
    def run(tx):
        cur = rref.get(transaction=tx)
        if cur.exists:
            c = cur.to_dict() or {}
            if c.get("status") == "success":
                return {"error": "This payment reference was already used."}
            if c.get("uid") and c["uid"] != g.uid:
                return {"error": "This payment reference belongs to another account."}
            if c.get("status") in ("failed", "mismatch", "rejected"):
                return {"error": "This payment reference was rejected. Contact support."}
            if c.get("amount") and abs(num(c["amount"]) - amt) > 0.01:
                return {"error": "Amount does not match the first request for this reference."}
            return {"resumed": True}
        tx.set(rref, {"utr": utr, "amount": amt, "uid": g.uid, "name": u.get("appName") or "User", "status": "pending", "createdAt": now})
        return {"resumed": False}
    res = run(W.D().transaction())
    if res.get("error"):
        return err(res["error"], 409)
    if not res["resumed"]:
        t = {"title": f"Deposit {inr(amt)} · verifying", "amount": f"+₹{amt:g}", "type": "deposit", "status": "pending", "trxId": utr, "method": "UPI",
             "date": dt.datetime.now(IST).strftime("%d/%m/%Y"), "createdAt": now}
        tr = ref.collection("transactions").document()
        tr.set(dict(t, timestamp=fs.SERVER_TIMESTAMP))
        rref.update({"trxDocId": tr.id})
        W.push(W.entries_from_trx(g.uid, tr.id, t))
    return ok(status="pending", ref=utr, resumed=res["resumed"])


@bp.get("/wallet/deposit/pending")
@user_required
def deposit_pending():
    out = []
    for st in ("pending", "processing"):
        for d in W.D().collection("deposit_requests").where("uid", "==", g.uid).where("status", "==", st).limit(10).stream():
            r = d.to_dict() or {}
            out.append({"utr": r.get("utr") or d.id, "amount": r.get("amount"), "status": st, "createdAt": r.get("createdAt")})
    return ok(items=out)


@bp.post("/wallet/deposit/check")
@user_required
def deposit_check():
    d = body()
    utr = extract_ref(d.get("ref"))
    if not utr:
        return err("ref required")
    if not rate_ok("depchk:" + g.uid, 40, 60):
        return err("Slow down.", 429)
    db = W.D()
    rref, fref, uref = db.collection("deposit_requests").document(utr), db.collection("fampay_deposits").document(utr), W.uref(g.uid)

    @fs.transactional
    def run(tx):
        rs = rref.get(transaction=tx)
        if not rs.exists:
            return {"status": "gone"}
        r = rs.to_dict() or {}
        if r.get("uid") != g.uid:
            return {"status": "failed"}
        if r.get("status") == "success":
            return {"status": "done", "trxDocId": r.get("trxDocId")}
        if r.get("status") not in ("pending", "processing"):
            return {"status": "failed", "trxDocId": r.get("trxDocId")}
        fsn = fref.get(transaction=tx)
        if not fsn.exists:
            return {"status": "notfound", "trxDocId": r.get("trxDocId"), "createdAt": r.get("createdAt")}
        f = fsn.to_dict() or {}
        famt = num(f.get("amount"), -1)
        if famt <= 0 or abs(famt - num(r.get("amount"))) > 0.01:
            return {"status": "notfound", "trxDocId": r.get("trxDocId"), "received": famt, "createdAt": r.get("createdAt")}
        pay_id = str(f.get("payId") or utr)
        used_ref = db.collection("fampay_used").document(pay_id)
        used = used_ref.get(transaction=tx)
        us = uref.get(transaction=tx)
        if used.exists:
            return {"status": "used", "trxDocId": r.get("trxDocId")}
        if not us.exists:
            return {"status": "failed"}
        dep, wd = W.bal(us.to_dict() or {})
        new_dep = round(dep + famt, 2)
        tx.set(used_ref, {"ref": utr, "uid": g.uid, "at": now_ms()})
        tx.update(uref, {"depositBalance": new_dep, "totalDeposited": fs.Increment(famt), "lastDepositUtr": utr})
        tx.update(rref, {"status": "success", "verifiedAt": now_ms(), "receivedAmount": famt, "claimed": True})
        tx.delete(fref)
        return {"status": "success", "amount": famt, "trxDocId": r.get("trxDocId"), "depositBalance": new_dep, "withdrawBalance": wd}
    res = run(db.transaction())
    st, tid = res["status"], res.get("trxDocId")
    if st in ("success", "used", "failed") and tid:
        patch = {"status": "success", "title": f"Deposit {inr(res.get('amount', 0))} · credited"} if st == "success" else (
            {"status": "failed", "title": "Deposit · not verified"} if st == "used" else None)
        try:
            if patch:
                uref.collection("transactions").document(tid).set(patch, merge=True)
            t = uref.collection("transactions").document(tid).get().to_dict() or {}
            W.push(W.entries_from_trx(g.uid, tid, fbx.enc({k: v for k, v in t.items() if k != "timestamp"})))
        except Exception as ex:
            print("[deposit trx]", ex, flush=True)
    if st == "used":
        W.notify(g.uid, "Deposit Could Not Be Verified", f"This payment (Ref: {utr}) was already credited earlier, so it cannot be credited again.", "deposit")
    if st == "success":
        W.notify(g.uid, "Deposit Successful", f"{inr(res['amount'])} has been credited to your Deposit Balance (Ref: {utr}). Good luck in your next match!", "deposit")
    if st == "notfound" and d.get("giveUp") and now_ms() - int(res.get("createdAt") or now_ms()) > env_float("DEPOSIT_GIVEUP_MIN", 10) * 60000:
        rref.set({"status": "failed", "failedAt": now_ms()}, merge=True)
        if tid:
            uref.collection("transactions").document(tid).set({"status": "failed", "title": "Deposit · not verified"}, merge=True)
            W.sync_user_history(g.uid, force=True)
        st = "failed"
        W.notify(g.uid, "Deposit Could Not Be Verified", f"We could not match your payment (Ref: {utr}). If money was debited, contact support with the reference.", "deposit")
    out = {k: v for k, v in res.items() if k not in ("createdAt", "status")}
    return ok(status=st, **out)


# ------------------------------------------------------------------ gift code
def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


@bp.get("/wallet/gift/tries")
@user_required
def gift_tries():
    t = W.D().collection("gift_tries").document(g.uid).get().to_dict() or {}
    if int(t.get("resetAt") or 0) <= now_ms():
        return ok(fails=0, resetAt=0)
    return ok(fails=int(t.get("fails") or 0), resetAt=int(t.get("resetAt") or 0))


@bp.post("/wallet/gift/redeem")
@user_required
def gift_redeem():
    exact = str(body().get("code") or "")[:40]              # the app hashes the code exactly as typed (24 chars, any symbols)
    raw = exact.strip()
    if len(raw) < 4:
        return err("Enter the gift code.")
    db, uid = W.D(), g.uid
    u, e = _user_ok(uid)
    if e:
        return e
    bad = _geo_guard(uid, u, "gift")
    if bad:
        return bad
    tries_ref = db.collection("gift_tries").document(uid)
    t = tries_ref.get().to_dict() or {"fails": 0, "resetAt": 0}
    now = now_ms()
    if t.get("resetAt", 0) <= now:
        t = {"fails": 0, "resetAt": 0}
    if t["fails"] >= GIFT_MAXTRY:
        return err("Too many wrong tries. Try again later.", 429, code="locked", resetAt=t["resetAt"])
    cands = [_h(exact), _h(raw), _h(raw.upper().replace(" ", "")), _h(raw.replace(" ", ""))]
    h = next((c for c in cands if db.collection("gift_codes").document(c).get().exists), cands[0])
    code_ref = db.collection("gift_codes").document(h)
    red_ref = code_ref.collection("redeems").document(uid)
    uref = W.uref(uid)
    trx_ref = uref.collection("transactions").document()
    n_ref = uref.collection("notifications").document()

    @fs.transactional
    def run(tx):
        cs = code_ref.get(transaction=tx)
        if not cs.exists:
            return {"kind": "invalid"}
        c = cs.to_dict() or {}
        if c.get("status") != "active":
            return {"kind": "invalid"}
        if c.get("expiresAt") and c["expiresAt"] < now_ms():
            return {"kind": "expired"}
        used = int(c.get("used", 0) or 0)
        if used >= int(c.get("max", 1) or 1):
            return {"kind": "full"}
        if red_ref.get(transaction=tx).exists:
            return {"kind": "already"}
        pool = c.get("pool") or []
        amount = num(pool[used]) if used < len(pool) else 0
        if amount <= 0:
            return {"kind": "invalid"}
        us = uref.get(transaction=tx)
        dep, wd = W.bal(us.to_dict() or {})
        to_wd = c.get("balance") == "withdraw"
        field = "withdrawBalance" if to_wd else "depositBalance"
        newv = round((wd if to_wd else dep) + amount, 2)
        order = "GF-" + dt.datetime.now(IST).strftime("%Y%m%d") + "-" + trx_ref.id[-6:].upper()
        now = now_ms()
        tx.set(red_ref, {"uid": uid, "amount": amount, "at": now, "name": (us.to_dict() or {}).get("appName") or "User", "orderNo": order})
        tx.update(code_ref, {"used": fs.Increment(1)})
        if c.get("reward") == "scratch":      # no instant credit: a scratch card is issued below, the prize is revealed by scratching
            tx.update(uref, {"lastGiftHash": h})
            return {"kind": "scratch"}
        tx.update(uref, {field: newv, "lastGiftHash": h})
        trx = {"title": f"Gift code redeemed: ₹{amount:g}", "amount": f"+₹{amount:g}", "type": "deposit", "status": "success", "kind": "gift", "giftHash": h,
               "orderNo": order, "balanceType": "withdraw" if to_wd else "deposit", "date": dt.datetime.now(IST).strftime("%d/%m/%Y"), "createdAt": now}
        tx.set(trx_ref, dict(trx, timestamp=fs.SERVER_TIMESTAMP))
        tx.set(n_ref, {"title": f"🎁 Gift code: ₹{amount:g} added", "message": f"Order {order} — added to your {'withdrawal' if to_wd else 'deposit'} balance.",
                       "type": "gift", "read": False, "createdAt": now})
        return {"kind": "ok", "amount": amount, "balance": "withdraw" if to_wd else "deposit", "newBal": newv, "trx": trx}
    res = run(db.transaction())
    if res["kind"] == "scratch":
        from . import ref_mod
        card = None
        for _ in range(2):
            try:
                card = (ref_mod.giftcard(uid, h) or {}).get("card")
            except Exception as e:
                print("[gift scratch]", repr(e), flush=True)
            if card:
                break
        return ok(scratch=True, card=card, h=h)
    if res["kind"] != "ok":
        fails = t["fails"] + 1
        tries_ref.set({"fails": fails, "resetAt": now_ms() + GIFT_LOCK_MS})
        msg = {"invalid": "Invalid gift code.", "expired": "This gift code has expired.", "full": "This gift code is fully used.", "already": "You have already used this code."}[res["kind"]]
        return err(msg, 400, code=res["kind"], triesLeft=max(0, GIFT_MAXTRY - fails))
    W.push(W.entries_from_trx(uid, trx_ref.id, res["trx"]))
    return ok(amount=res["amount"], balance=res["balance"], newBalance=res["newBal"])


# ------------------------------------------------------------------ internal (other servers)
@bp.post("/internal/wallet/debit")
@internal_required
def i_debit():
    d = body()
    good, info = W.debit(str(d.get("uid")), d.get("amount"), clean_str(d.get("title"), 120) or "Debit", clean_str(d.get("kind"), 30) or "game_entry")
    return (ok(**info) if good else err(info.get("error", "failed"), 409, **{k: v for k, v in info.items() if k != "error"}))


@bp.post("/internal/wallet/credit")
@internal_required
def i_credit():
    d = body()
    good, info = W.credit(str(d.get("uid")), d.get("amount"), clean_str(d.get("title"), 120) or "Credit", clean_str(d.get("kind"), 30) or "game_win")
    return (ok(**info) if good else err(info.get("error", "failed"), 409))


@bp.post("/internal/user/brief")
@internal_required
def i_brief():
    b = W.user_brief(str(body().get("uid")))
    return ok(user=b) if b else err("not found", 404)


@bp.post("/internal/user/verified")
@internal_required
def i_verified():
    return ok(verified=sorted(W.verified_uids(body().get("uids") or [])))


@bp.post("/internal/user/ban")
@internal_required
def i_ban():
    d = body()
    W.ban_user(str(d.get("uid")), d.get("reason", ""), d.get("by", "internal"))
    return ok()


@bp.post("/internal/user/unban")
@internal_required
def i_unban():
    d = body()
    W.unban_user(str(d.get("uid")), d.get("by", "internal"))
    return ok()


@bp.post("/internal/lb/add")
@internal_required
def i_lb():
    d = body()
    W.lb_add(str(d.get("uid")), d.get("name"), d.get("photo"), num(d.get("amount")))
    return ok()
