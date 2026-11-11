"""S1 - REFER & EARN with SCRATCH CARDS  (port of the Main panel's referral.py to the Server-1 / load-balancer API).

All money + rules live here; the phone only shows the result.
  * Every user's referral code = their Player ID. Link: SITE_URL/?ref=<playerId>
  * A new user adds a friend's code (auto from the link at sign-up, or later within `claimWindowHrs`)  -> referrals/{newUid}
  * The referral becomes "qualified" when depositRequired == 0 OR the friend's totalDeposited >= depositRequired
  * Every `referralsPerCard` qualified referrals = 1 scratch card -> ref_cards/{referrerUid}_{n}
  * Scratching a card: the amount is chosen HERE (fixed or random min..max) and credited once.
Admin controls (Main/admin panel, same Firestore DB-1): config/referral, send / cancel cards, void a referral.
Firestore: config/referral · referrals/{referredUid} · ref_cards/{id} · ref_totals/all · users/{uid}.referredBy

Routes (through the load balancer, prefix /ref):  POST /ref/info  /ref/claim  /ref/giftcard  /ref/scratch
"""
import random, threading, datetime as dt
from flask import Blueprint, g
from firebase_admin import firestore as fs, auth as fb_auth
from google.api_core.exceptions import AlreadyExists
from common import wallet_core as W
from common.authz import user_required
from common.config import env
from common.util import ok, err, body, rate_ok, now_ms, IST

bp = Blueprint("ref", __name__)
SITE_URL = env("SITE_URL", "https://ffcashbattle.in").rstrip("/")
DEFAULT_CFG = {"enabled": True, "referralsPerCard": 1, "depositRequired": 50, "amountMode": "random", "fixedAmount": 10, "minAmount": 2, "maxAmount": 10,
               "creditTo": "deposit", "claimWindowHrs": 48, "maxReferralsPerUser": 100, "maxCardsPerUser": 0}
_cfg = {"t": 0.0, "v": dict(DEFAULT_CFG)}
LOCKS, LOCK = {}, threading.RLock()


def NOW():
    return now_ms()


def cfg(force=False):
    import time
    if force or time.time() - _cfg["t"] > 15:
        try:
            d = W.D().collection("config").document("referral").get()
            v = dict(DEFAULT_CFG)
            v.update((d.to_dict() or {}) if d.exists else {})
            _cfg["v"] = v
        except Exception as e:
            print("[ref] cfg:", repr(e), flush=True)
        _cfg["t"] = time.time()
    return _cfg["v"]


def public_cfg():
    c = cfg()
    return {k: c[k] for k in ("enabled", "referralsPerCard", "depositRequired", "amountMode", "fixedAmount", "minAmount", "maxAmount", "creditTo", "claimWindowHrs")}


def ulock(uid):
    with LOCK:
        return LOCKS.setdefault(uid, threading.RLock())


def mask(name):
    n = (name or "Player").strip() or "Player"
    return (n[:2] + "•••") if len(n) > 2 else n[0] + "••"


def _notify(uid, title, msg, card=None):
    try:
        d = {"title": title, "message": msg, "date": dt.datetime.now(IST).strftime("%d %b %Y"), "timestamp": fs.SERVER_TIMESTAMP, "type": "reward", "createdAt": NOW()}
        if card:
            d["scratchCard"] = card
        W.uref(uid).collection("notifications").document().set(d)
    except Exception as e:
        print("[ref] notify:", repr(e), flush=True)


def _tot(**inc):
    try:
        W.D().collection("ref_totals").document("all").set({k: fs.Increment(v) for k, v in inc.items()}, merge=True)
    except Exception as e:
        print("[ref] totals:", repr(e), flush=True)


def _created_ms(uid):
    try:
        return int(fb_auth.get_user(uid).user_metadata.creation_timestamp)
    except Exception:
        return None


# ------------------------------------------------------------------ cards
def evaluate(uid):
    """Make sure the user holds exactly floor(qualified / referralsPerCard) referral cards (issue new ones, take back UNSCRATCHED extras after a void)."""
    c = cfg()
    with ulock(uid):
        db = W.D()
        q = sum(1 for _ in db.collection("referrals").where("referrer", "==", uid).where("status", "==", "qualified").stream())
        earned = q // max(1, int(c["referralsPerCard"]))
        if int(c["maxCardsPerUser"]) > 0:
            earned = min(earned, int(c["maxCardsPerUser"]))
        cards = {d.id: d.to_dict() for d in db.collection("ref_cards").where("uid", "==", uid).stream() if (d.to_dict() or {}).get("src", "ref") == "ref"}
        top = max([int(x.get("seq", 0)) for x in cards.values()] or [0])
        new = 0
        while len(cards) < earned:
            top += 1
            cid = f"{uid}_{top}"
            if cid in cards:
                continue
            data = {"uid": uid, "seq": top, "status": "new", "amount": 0, "createdAt": NOW()}
            try:
                db.collection("ref_cards").document(cid).create(data)
            except AlreadyExists:
                pass
            cards[cid] = data
            new += 1
        if new:
            _tot(cardsIssued=new)
            _notify(uid, "🎁 You won a Scratch Card!", f"Your friend's referral unlocked {new} scratch card{'s' if new > 1 else ''}. Open Refer & Earn from the menu and scratch to win real wallet money.", f"{uid}_{top}")
        if len(cards) > earned:
            for cid, x in sorted(cards.items(), key=lambda kv: -int(kv[1].get("seq", 0))):
                if len(cards) <= earned:
                    break
                if x.get("status") == "new":
                    db.collection("ref_cards").document(cid).delete()
                    cards.pop(cid)
        return cards, q


def _roll(c):
    if c["amountMode"] == "fixed":
        return float(c["fixedAmount"])
    lo, hi = int(c["minAmount"]), int(c["maxAmount"])
    return float(random.randint(min(lo, hi), max(lo, hi)))


def scratch(uid, cid):
    c = cfg()
    if not cid.startswith(uid + "_"):
        return {"ok": False, "error": "Card not found"}
    db = W.D()
    cref = db.collection("ref_cards").document(cid)
    uref = W.uref(uid)
    tref = uref.collection("transactions").document()

    @fs.transactional
    def run(tx):
        cs = cref.get(transaction=tx)
        if not cs.exists or (cs.to_dict() or {}).get("uid") != uid:
            return ("err", "Card not found")
        cd = cs.to_dict()
        src = cd.get("src", "ref")
        if cd.get("status") == "cancelled":
            return ("err", "This card was cancelled")
        if cd.get("status") != "new":
            return ("done", float(cd.get("amount", 0)))        # already scratched: show the same amount again, never pay twice
        if src == "ref" and not c["enabled"]:
            return ("err", "Refer & Earn is switched off right now")
        us = uref.get(transaction=tx)
        if not us.exists:
            return ("err", "Account not found")
        u = us.to_dict() or {}
        if W.is_banned(u):
            return ("err", "Account restricted")
        if src == "ref":
            amt, to = _roll(c), c["creditTo"]
        else:   # gift-code / admin card: fixed prize (gift code pool) or a random amount inside the min..max the admin set
            to = cd.get("creditTo", "deposit")
            amt = float(cd["prize"]) if cd.get("prize") else float(random.randint(int(cd.get("min", 1)), max(int(cd.get("min", 1)), int(cd.get("max", 1)))))
        dep, wd = W.bal(u)
        up = ({"withdrawBalance": round(wd + amt, 2), "totalEarned": round(float(u.get("totalEarned", 0) or 0) + amt, 2)} if to == "withdraw"
              else {"depositBalance": round(dep + amt, 2)})
        tx.update(uref, up)
        tx.update(cref, {"status": "scratched", "amount": amt, "scratchedAt": NOW(), "creditedTo": to})
        title = {"ref": "Referral Scratch Card 🎁", "gift": "Gift Scratch Card 🎁"}.get(src, "Reward Scratch Card 🎁")
        t = {"title": title, "amount": f"+₹{amt:g}", "type": "game", "kind": "gift", "status": "success", "date": dt.datetime.now(IST).strftime("%d/%m/%Y"), "createdAt": NOW()}
        tx.set(tref, dict(t, timestamp=fs.SERVER_TIMESTAMP))
        return ("ok", amt, t)
    with ulock(uid):
        res = run(db.transaction())
    st, val = res[0], res[1]
    if st == "err":
        return {"ok": False, "error": val}
    if st == "ok":
        _tot(cardsScratched=1, paid=val)
        try:
            W.push(W.entries_from_trx(uid, tref.id, res[2]))
        except Exception as e:
            print("[ref] history push:", repr(e), flush=True)
    return {"ok": True, "amount": val, "already": st == "done"}


# ------------------------------------------------------------------ gift-code cards (src = "gift")
def giftcard(uid, h):
    """After a user redeemed a SCRATCH-type gift code: turn the redeem record into a scratch card (idempotent)."""
    h = str(h or "").strip().lower()
    if not (len(h) == 64 and all(ch in "0123456789abcdef" for ch in h)):
        return {"ok": False, "error": "Invalid code"}
    db = W.D()
    code = db.collection("gift_codes").document(h).get()
    if not code.exists or (code.to_dict() or {}).get("reward") != "scratch":
        return {"ok": False, "error": "Not a scratch code"}
    red = db.collection("gift_codes").document(h).collection("redeems").document(uid).get()
    if not red.exists:
        return {"ok": False, "error": "Redeem record not found"}
    cd, r = code.to_dict(), red.to_dict()
    cid = f"{uid}_g{h[:10]}"
    try:
        db.collection("ref_cards").document(cid).create({"uid": uid, "seq": 0, "status": "new", "amount": 0, "prize": float(r.get("amount", 0)), "src": "gift", "code": h,
                                                           "creditTo": "withdraw" if cd.get("balance") == "withdraw" else "deposit", "createdAt": NOW()})
    except AlreadyExists:
        return {"ok": True, "card": cid, "already": True}
    _tot(cardsIssued=1)
    _notify(uid, "🎁 You won a Scratch Card!", "Your gift code gave you a scratch card. Open it from here or from Refer & Earn and scratch to reveal your prize.", cid)
    return {"ok": True, "card": cid}


def sync_gift(uid, u):
    """Safety net: if the redeem call could not issue the card, the last redeemed gift hash is checked when the user opens Refer & Earn."""
    h = str((u or {}).get("lastGiftHash") or "")
    if h:
        try:
            giftcard(uid, h)
        except Exception as e:
            print("[ref] sync_gift:", repr(e), flush=True)


# ------------------------------------------------------------------ binding a friend
def _find_by_code(code):
    for d in W.D().collection("users").where("playerId", "==", code).limit(1).stream():
        return d
    return None


def claim(uid, code):
    c = cfg()
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not c["enabled"]:
        return {"ok": False, "error": "Refer & Earn is switched off right now"}
    if not (4 <= len(code) <= 12):
        return {"ok": False, "error": "Enter a valid referral code"}
    db = W.D()
    with ulock(uid):
        me = W.uref(uid).get()
        if not me.exists:
            return {"ok": False, "error": "Account not ready — try again in a moment"}
        m = me.to_dict() or {}
        if W.is_banned(m):
            return {"ok": False, "error": "Account restricted"}
        if m.get("referredBy"):
            return {"ok": False, "error": "You have already used a referral code"}
        ref = _find_by_code(code)
        if not ref or ref.id == uid:
            return {"ok": False, "error": "Invalid referral code" if not ref else "You cannot use your own code"}
        r = ref.to_dict() or {}
        if W.is_banned(r):
            return {"ok": False, "error": "Invalid referral code"}
        if r.get("referredBy") == uid:
            return {"ok": False, "error": "This code cannot be used"}
        created = _created_ms(uid) or NOW()
        if NOW() - created > int(c["claimWindowHrs"]) * 3600000:
            return {"ok": False, "error": f"Referral codes can only be added within {c['claimWindowHrs']} hours of joining"}
        cap = int(c["maxReferralsPerUser"])
        if cap > 0 and sum(1 for _ in db.collection("referrals").where("referrer", "==", ref.id).stream()) >= cap:
            return {"ok": False, "error": "This referral code has reached its limit"}
        need = int(c["depositRequired"])
        dep = float(m.get("totalDeposited", 0) or 0)
        good = need <= 0 or dep >= need
        data = {"referrer": ref.id, "referred": uid, "referredName": m.get("appName") or "Player", "referrerCode": code, "createdAt": NOW(), "status": "qualified" if good else "pending"}
        if good:
            data["qualifiedAt"] = NOW()
        try:
            db.collection("referrals").document(uid).create(data)
        except AlreadyExists:
            return {"ok": False, "error": "You have already used a referral code"}
        W.uref(uid).set({"referredBy": ref.id, "referredAt": NOW(), "referredByCode": code}, merge=True)
        _tot(referrals=1)
    evaluate(ref.id)
    return {"ok": True, "qualified": good, "friend": mask(r.get("appName")), "need": need}


def qualify_for(uid):
    """When a referrer opens the page: check ONLY his own pending friends (a few reads) so a fresh deposit shows up at once."""
    c = cfg()
    db = W.D()
    need = int(c["depositRequired"])
    n = 0
    for s in db.collection("referrals").where("referrer", "==", uid).where("status", "==", "pending").stream():
        try:
            u = W.uref(s.id).get().to_dict() or {}
            if need <= 0 or float(u.get("totalDeposited", 0) or 0) >= need:
                s.reference.update({"status": "qualified", "qualifiedAt": NOW()})
                n += 1
        except Exception as e:
            print("[ref] qualify_for:", repr(e), flush=True)
    return n


# ------------------------------------------------------------------ info for the app
def info(uid):
    c = cfg()
    db = W.D()
    u = W.uref(uid).get().to_dict() or {}
    if c["enabled"]:
        try:
            qualify_for(uid)
        except Exception as e:
            print("[ref] info-qualify:", repr(e), flush=True)
    sync_gift(uid, u)
    cards0, q = evaluate(uid) if c["enabled"] else ({}, 0)
    cards = {d.id: d.to_dict() for d in db.collection("ref_cards").where("uid", "==", uid).stream()}      # referral + gift + admin cards
    refs = [d.to_dict() for d in db.collection("referrals").where("referrer", "==", uid).stream()]
    refs.sort(key=lambda x: -int(x.get("createdAt", 0) or 0))
    per = max(1, int(c["referralsPerCard"]))
    code = str(u.get("playerId") or "")
    can = False
    if c["enabled"] and not u.get("referredBy"):
        cm = _created_ms(uid)
        can = bool(cm) and NOW() - cm <= int(c["claimWindowHrs"]) * 3600000
    by = None
    if u.get("referredBy"):
        rb = W.uref(u["referredBy"]).get().to_dict() or {}
        by = {"name": mask(rb.get("appName")), "code": u.get("referredByCode", "")}
    card_rows = [{"id": k, "seq": x.get("seq"), "status": x.get("status"), "amount": x.get("amount", 0), "src": x.get("src", "ref"), "to": x.get("creditTo"),
                  "min": x.get("min"), "max": x.get("max"), "at": x.get("scratchedAt") or x.get("createdAt")}
                 for k, x in sorted(cards.items(), key=lambda kv: (kv[1].get("status") != "new", -int(kv[1].get("seq", 0) or 0), -int(kv[1].get("createdAt", 0) or 0)))]
    return {"ok": True, "cfg": public_cfg(), "code": code, "link": f"{SITE_URL}/?ref={code}" if code else SITE_URL,
            "stats": {"joined": len(refs), "qualified": q, "pending": sum(1 for r in refs if r.get("status") == "pending"), "cards": len(cards),
                      "fresh": sum(1 for x in cards.values() if x.get("status") == "new"),
                      "earned": round(sum(float(x.get("amount", 0) or 0) for x in cards.values() if x.get("status") == "scratched"), 2)},
            "progress": {"per": per, "have": q % per, "toNext": per - (q % per)}, "referredBy": by, "canClaim": can,
            "cards": card_rows,
            "friends": [{"name": (r.get("referredName") or "Player"), "status": r.get("status"), "at": r.get("createdAt")} for r in refs[:30]]}


# ------------------------------------------------------------------ HTTP
def _guard(fn):
    if not rate_ok("ref:" + g.uid, 40, 60):
        return err("Too many requests — slow down", 429)
    try:
        res = fn()
        res["now"] = NOW()
        if res.get("ok") is False:
            return err(res.get("error") or "Failed", 400, **{k: v for k, v in res.items() if k not in ("ok", "error")})
        return ok(**{k: v for k, v in res.items() if k != "ok"})
    except Exception as e:
        print("[ref] error", repr(e), flush=True)
        return err("Server error — try again", 500)


@bp.post("/ref/info")
@user_required
def r_info():
    return _guard(lambda: info(g.uid))


@bp.post("/ref/claim")
@user_required
def r_claim():
    d = body()
    return _guard(lambda: claim(g.uid, d.get("code")))


@bp.post("/ref/giftcard")
@user_required
def r_giftcard():
    d = body()
    return _guard(lambda: giftcard(g.uid, d.get("hash")))


@bp.post("/ref/scratch")
@user_required
def r_scratch():
    d = body()
    return _guard(lambda: scratch(g.uid, str(d.get("card", ""))))
