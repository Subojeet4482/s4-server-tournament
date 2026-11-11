"""S1 - profile, public config, notifications."""
import re, time
from flask import Blueprint, request, g
from common import fbx, geo, wallet_core as W
from firebase_admin import auth as fb_auth
from common.authz import user_required, user_optional
from common.util import ok, err, body, rate_ok, now_ms, clean_str, sanitize_text
from .auth_mod import public_profile

bp = Blueprint("profile", __name__)
_cache = {}


def cached(key, ttl, fn):
    e = _cache.get(key)
    if e and time.time() - e[0] < ttl:
        return e[1]
    v = fn()
    _cache[key] = (time.time(), v)
    return v


@bp.get("/user/me")
@user_required
def me():
    s = W.uref(g.uid).get()
    if not s.exists:
        return err("Profile not found", 404, code="no_profile")
    u = s.to_dict() or {}
    if W.is_banned(u):
        return err("Your account is blocked.", 403, code="banned", **W.ban_info(u))
    return ok(user=public_profile(g.uid, u), geo=geo.evaluate(u))


@bp.patch("/user/me")
@user_required
def update_me():
    d = body()
    ref = W.uref(g.uid)
    u = ref.get().to_dict() or {}
    if W.is_banned(u):
        return err("Your account is blocked.", 403, code="banned")
    up = {}
    if "gameName" in d:
        v = clean_str(d["gameName"], 30)
        if not v:
            return err("Enter your in-game name.")
        if v != u.get("gameName"):
            up["gameName"] = v
    if "gameUid" in d:
        v = clean_str(d["gameUid"], 20)
        if not re.fullmatch(r"\d{6,12}", v):                      # same rule as the app: 6-12 digits
            return err("FF UID invalid (6-12 digits)")
        if v != str(u.get("gameUid") or ""):
            if u.get("isUidVerified"):
                return err("Your FF UID is verified and cannot be changed. Contact support.", 403, code="uid_verified")
            up["gameUid"] = v
    if "photoUrl" in d:
        v = clean_str(d["photoUrl"], 130000)
        # the app sends a small resized picture as a data: URL (max ~90 KB); https links and bundled avatars are fine too
        if v and not (v.startswith("https://") or v.startswith("assets/") or (len(v) <= 120000 and re.match(r"^data:image/(png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=]+$", v))):
            return err("We could not process this image. Please try a different one.")
        up["photoUrl"] = v
    if "appName" in d:
        v = sanitize_text(d["appName"], 20)
        if v != u.get("appName"):
            if len(v) < 3:
                return err("Name must be at least 3 characters.")
            left = int(u.get("nameChangesLeft", 0) or 0)
            if left <= 0:
                return err("Name change limit reached.", 403)
            up["appName"], up["nameChangesLeft"] = v, left - 1
    if not up:
        return err("Nothing to update.")
    ref.set(up, merge=True)
    return ok(user=public_profile(g.uid, dict(u, **up)))


PHONE_RE = re.compile(r"^[6-9]\d{9}$")


def _save_security(uid, d):
    """phone (unique, Indian mobile) + 'hasPassword' (only if Firebase really has a password for this account)."""
    up = {}
    if "phone" in d:
        ph = "".join(c for c in clean_str(d.get("phone"), 20) if c.isdigit())[-10:]
        if not PHONE_RE.match(ph):
            return None, err("Enter a valid 10-digit mobile number", 400, code="bad_phone")
        cur = (W.uref(uid).get().to_dict() or {}).get("phone")
        if ph != cur:
            if any(x.id != uid for x in W.D().collection("users").where("phone", "==", ph).limit(2).stream()):
                return None, err("This number is already used by another account", 409, code="phone_taken")
            up["phone"] = ph
    if d.get("hasPassword"):
        try:
            prov = [p.provider_id for p in fb_auth.get_user(uid, app=fbx.app_of("DB1")).provider_data]
        except Exception:
            return None, err("Could not verify the password with Firebase. Try again.", 503)
        if "password" not in prov:
            return None, err("Password is not set for this account yet.", 400, code="no_password")
        up["hasPassword"] = True
    return up, None


@bp.post("/user/security")
@user_required
def security_settings():
    """After the app links an email+password (Firebase SDK) it calls this to save the phone number and the hasPassword flag."""
    if not rate_ok("sec:" + g.uid, 6, 3600):
        return err("Too many changes. Try later.", 429)
    up, e = _save_security(g.uid, body())
    if e:
        return e
    if up:
        W.uref(g.uid).set(up, merge=True)
    return ok(saved=sorted(up))


@bp.post("/user/phone")
@user_required
def set_phone():                                     # kept for older clients
    return security_settings()


@bp.post("/user/payment-pref")
@user_required
def payment_pref():
    d = body()
    m, n = clean_str(d.get("method"), 8).upper(), sanitize_text(d.get("name"), 60)
    if m not in ("UPI", "BANK") or len(n) < 3:
        return err("Choose a method and enter the account name.")
    W.uref(g.uid).set({"paymentMethod": m, "paymentName": n}, merge=True)
    return ok()


@bp.get("/user/sync")
@user_required
def sync():
    """Replacement for the old realtime listener: one small call every ~8 s -> balance, stats, unread count."""
    s = W.uref(g.uid).get()
    if not s.exists:
        return err("Profile not found", 404, code="no_profile")
    u = s.to_dict() or {}
    if W.is_banned(u):
        return err("Your account is blocked.", 403, code="banned", **W.ban_info(u))
    uid = g.uid
    import threading
    threading.Thread(target=lambda: W.sync_user_history(uid), daemon=True).start()       # admin credits / approvals -> history (throttled 20 s)
    unread = None
    try:
        unread = int(W.uref(g.uid).collection("notifications").where("read", "==", False).count().get()[0][0].value)
    except Exception:
        pass
    return ok(user=public_profile(g.uid, u), geo=geo.evaluate(u), unread=unread, now=now_ms())


@bp.get("/user/lookup")
@user_required
def lookup():
    pid = clean_str(request.args.get("playerId"), 20)
    if len(pid) < 5 or not rate_ok("lk:" + g.uid, 30, 60):
        return ok(found=False)
    docs = list(W.D().collection("users").where("playerId", "==", pid).limit(1).stream())
    if not docs or docs[0].id == g.uid:
        return ok(found=False)
    return ok(found=True, name=(docs[0].to_dict() or {}).get("appName") or "User")


@bp.get("/public/config")
def public_config():
    def load():
        d = W.D().collection("config").document("banner").get().to_dict() or {}
        banners = [b for b in (d.get("banners") or []) if b] or ([d["url"]] if d.get("url") else [])
        return {"banners": banners, "upi": d.get("upi"), "qrUrl": d.get("qrUrl"),
                "supportNumber": d.get("supportNumber"), "notice": d.get("notice"),
                "minWithdraw": d.get("minWithdraw"), "minDeposit": d.get("minDeposit"),
                # admin ON/OFF switches + the message to show (Settings -> Deposit / Withdrawal ON-OFF)
                "depositEnabled": d.get("depositEnabled") is not False, "depositNote": d.get("depositNote") or "",
                "withdrawEnabled": d.get("withdrawEnabled") is not False, "withdrawNote": d.get("withdrawNote") or "",
                "wdUpiEnabled": d.get("wdUpiEnabled") is not False, "wdUpiNote": d.get("wdUpiNote") or "",
                "wdGpEnabled": d.get("wdGpEnabled") is not False, "wdGpNote": d.get("wdGpNote") or "",
                "whatsapp": {"name": d.get("whatsappName", ""), "link": d.get("whatsappLink") or d.get("whatsappNumber") or ""},
                "telegram": {"name": d.get("telegramName", ""), "link": d.get("telegramLink") or d.get("telegramChannel") or ""}}
    return ok(config=cached("cfg", 10, load))


@bp.get("/public/verified")
def verified_list():
    """Blue-tick users (config/verified {uids:[...]} - written by the admin panel). Cached 30 s."""
    def load():
        return list((W.D().collection("config").document("verified").get().to_dict() or {}).get("uids") or [])[:5000]
    return ok(uids=cached("verified", 30, load))


@bp.get("/public/rules")
def rules():
    return ok(rules=cached("rules", 60, lambda: fbx.enc(W.D().collection("config").document("rules").get().to_dict() or {})))


@bp.get("/public/categories")
def categories():
    def load():
        return [fbx.doc_dict(s) for s in W.D().collection("categories").stream()]
    return ok(categories=cached("cats", 60, load))


def _ms(n):
    t = n.get("timestamp")
    return int(t if isinstance(t, (int, float)) else (n.get("createdAt") or 0))


@bp.get("/user/notifications")
@user_required
def notifications():
    """items = personal notifications, broadcast = admin messages for everybody. Both carry `ms` (time in ms)."""
    lim = max(1, min(int(request.args.get("limit", 50) or 50), 100))
    q = W.uref(g.uid).collection("notifications").order_by("createdAt", direction="DESCENDING").limit(lim)
    items = [fbx.doc_dict(s) for s in q.stream()]
    for i in items:
        i["ms"] = _ms(i)
    def load():
        rows = [fbx.doc_dict(s) for s in W.D().collection("notifications").order_by("timestamp", direction="DESCENDING").limit(15).stream()]
        for r in rows:
            r["ms"] = _ms(r)
        return rows
    return ok(items=items, broadcast=cached("bcast", 20, load), unread=sum(1 for i in items if not i.get("read")))


@bp.get("/public/notifications")
def public_notifications():
    """Guests also see the admin broadcast messages."""
    def load():
        rows = [fbx.doc_dict(s) for s in W.D().collection("notifications").order_by("timestamp", direction="DESCENDING").limit(15).stream()]
        for r in rows:
            r["ms"] = _ms(r)
        return rows
    return ok(broadcast=cached("bcast", 20, load))


@bp.delete("/user/notifications")
@user_required
def notifications_clear():
    col = W.uref(g.uid).collection("notifications")
    b, n = W.D().batch(), 0
    for s in col.limit(400).stream():
        b.delete(s.reference)
        n += 1
    if n:
        b.commit()
    return ok(deleted=n)


@bp.post("/user/notifications/<nid>/claim")
@user_required
def notification_claim(nid):
    """Old 'claim' cards only get marked as claimed - balance is never changed from the app (the admin credits rewards)."""
    ref = W.uref(g.uid).collection("notifications").document(nid)
    if not ref.get().exists:
        return err("Notification not found", 404)
    ref.update({"claimed": True, "claimedAt": now_ms()})
    return ok()


@bp.post("/user/notifications/read")
@user_required
def notifications_read():
    col = W.uref(g.uid).collection("notifications")
    ids = body().get("ids")
    docs = [col.document(str(i)) for i in ids[:100]] if isinstance(ids, list) and ids else [s.reference for s in col.where("read", "==", False).limit(100).stream()]
    b = W.D().batch()
    for r in docs:
        b.update(r, {"read": True})
    b.commit()
    return ok(marked=len(docs))


@bp.delete("/user/notifications/<nid>")
@user_required
def notification_delete(nid):
    W.uref(g.uid).collection("notifications").document(nid).delete()
    return ok()
