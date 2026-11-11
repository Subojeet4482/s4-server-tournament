"""S1 / S4 - state payment ban: user endpoints (my state, declare state) + admin endpoints (used by the Telegram bot)."""
from flask import Blueprint, request, g
from common import geo, wallet_core as W, fbx
from common.authz import user_required, admin_required, client_ip
from common.config import env_int
from common.util import ok, err, body, rate_ok, now_ms, clean_str

bp = Blueprint("geo", __name__)


@bp.get("/public/states")
def states():
    return ok(states=geo.STATES)


@bp.get("/user/geo")
@user_required
def my_geo():
    """What the app needs to draw the payment screen: ev.paymentAllowed false -> hide the payment option and show ev.message."""
    s = W.uref(g.uid).get()
    if not s.exists:
        return err("Profile not found", 404, code="no_profile")
    _, ev = geo.refresh(g.uid, s.to_dict() or {}, client_ip())
    return ok(geo=ev)


@bp.post("/user/state")
@user_required
def set_state():
    st = geo.norm_state(body().get("state"))
    if not st:
        return err("Please choose your state from the list.", 400, code="bad_state")
    if not rate_ok("state:" + g.uid, 10, 3600):
        return err("Too many attempts.", 429)
    ref = W.uref(g.uid)
    u = ref.get().to_dict() or {}
    days = env_int("STATE_CHANGE_DAYS", 30)
    last = int(u.get("stateChangedAt") or 0)
    if u.get("state") and st != u.get("state") and days > 0 and now_ms() - last < days * 86400000:
        return err(f"You can change your state once in {days} days.", 403, code="state_locked", canChangeAt=last + days * 86400000)
    if st != u.get("state"):
        ref.set({"state": st, "stateChangedAt": now_ms()}, merge=True)
        u = dict(u, state=st)
    _, ev = geo.refresh(g.uid, u, client_ip())
    return ok(geo=ev)


# ------------------------------------------------------------------ admin (X-Admin-Key; the Telegram bot calls these through the load balancer)
@bp.get("/geo/admin/config")
@admin_required
def a_get():
    return ok(config=geo.get_cfg(True), states=geo.STATES, defaults=geo.DEFAULT_BLOCKED)


@bp.post("/geo/admin/config")
@admin_required
def a_set():
    return ok(config=geo.save_cfg(body()))


def _find(ident):
    ident = clean_str(ident, 80)
    s = W.uref(ident).get() if ident else None
    if s is not None and s.exists:
        return s.id, s.to_dict() or {}
    docs = list(W.D().collection("users").where("playerId", "==", ident).limit(1).stream()) if ident else []
    return (docs[0].id, docs[0].to_dict() or {}) if docs else (None, None)


@bp.get("/geo/admin/user/<ident>")
@admin_required
def a_user(ident):
    uid, u = _find(ident)
    if not uid:
        return err("user not found", 404)
    return ok(uid=uid, name=u.get("appName"), playerId=u.get("playerId"), declaredState=u.get("state"), ipState=u.get("geoState"), ipCountry=u.get("geoCountry"),
              checkedAt=u.get("geoCheckedAt"), seenBlockedAt=u.get("geoSeenBlockedAt"), override=u.get("geoOverride"), geo=geo.evaluate(u))


@bp.post("/geo/admin/user/<ident>/override")
@admin_required
def a_override(ident):
    uid, u = _find(ident)
    if not uid:
        return err("user not found", 404)
    mode = body().get("mode")
    if mode not in ("allow", "block", "clear"):
        return err("mode must be allow, block or clear")
    up = {"geoOverride": None if mode == "clear" else mode, "geoOverrideAt": now_ms()}
    if mode in ("allow", "clear"):
        up["geoSeenBlockedAt"] = 0                      # also forget the 'sticky' memory
    W.uref(uid).set(up, merge=True)
    return ok(uid=uid, override=up["geoOverride"], geo=geo.evaluate(dict(u, **up)))
