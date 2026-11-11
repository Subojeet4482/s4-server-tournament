"""S5 - user log + account verification. Called by S1 at every login/register (internal), plus admin endpoints (X-Admin-Key)."""
from flask import Blueprint, request
from common import fbx, svc, fraud_core as F
from common.authz import internal_required, admin_required
from common.util import ok, err, body, now_ms, clean_str

bp = Blueprint("fraud", __name__)


@bp.post("/internal/fraud/check")
@internal_required
def check():
    return ok(**{k: v for k, v in F.check(body()).items() if k != "ok"})


@bp.get("/fraud/stats")
@admin_required
def stats():
    db = F.D()
    def cnt(q):
        try:
            return int(q.count().get()[0][0].value)
        except Exception:
            return None
    return ok(users=cnt(db.collection("fraud_users")), banned=cnt(db.collection("fraud_users").where("status", "==", "banned")),
              casesOpen=cnt(db.collection("fraud_cases").where("status", "==", "open")), devices=cnt(db.collection("fraud_devices")))


@bp.get("/fraud/cases")
@admin_required
def cases():
    status = request.args.get("status", "open")
    q = F.D().collection("fraud_cases")
    if status != "all":
        q = q.where("status", "==", status)
    rows = [fbx.doc_dict(s) for s in q.limit(100).stream()]
    rows.sort(key=lambda r: r.get("updatedAt", 0), reverse=True)
    return ok(cases=rows)


@bp.post("/fraud/cases/<cid>/resolve")
@admin_required
def resolve(cid):
    """action = confirm (keep the bans)  |  unban (unban `uid`, or all victims, and whitelist them)."""
    d = body()
    action = d.get("action")
    ref = F.D().collection("fraud_cases").document(cid)
    s = ref.get()
    if not s.exists:
        return err("case not found", 404)
    c = s.to_dict() or {}
    if action == "unban":
        targets = [d["uid"]] if d.get("uid") else list(c.get("bannedUids") or [])
        for u in targets:
            svc.unban_user(u, "admin-fraud-review")
            F.D().collection("fraud_users").document(u).set({"status": "ok", "unbannedAt": now_ms()}, merge=True)
            F.D().collection("fraud_whitelist").document("uid-" + u).set({"kind": "uid", "value": u, "note": "unbanned from case " + cid, "at": now_ms()})
        ref.update({"status": "reversed", "resolvedAt": now_ms(), "note": clean_str(d.get("note"), 200)})
        return ok(unbanned=targets)
    if action == "confirm":
        for u in c.get("victims") or []:
            if u not in (c.get("bannedUids") or []):
                svc.ban_user(u, "Multiple accounts (admin confirmed)", "admin")
                F.D().collection("fraud_users").document(u).set({"status": "banned", "bannedAt": now_ms()}, merge=True)
        ref.update({"status": "confirmed", "resolvedAt": now_ms(), "note": clean_str(d.get("note"), 200)})
        return ok()
    return err("action must be confirm or unban")


@bp.get("/fraud/user/<uid>")
@admin_required
def user(uid):
    db = F.D()
    u = db.collection("fraud_users").document(uid).get().to_dict() or {}
    ev = [fbx.doc_dict(s) for s in db.collection("fraud_events").where("uid", "==", uid).limit(60).stream()]
    ev.sort(key=lambda r: r.get("at", 0), reverse=True)
    return ok(record=fbx.enc(u), events=ev)


@bp.get("/fraud/events")
@admin_required
def events():
    q = F.D().collection("fraud_events").order_by("at", direction="DESCENDING").limit(min(int(request.args.get("limit", 100) or 100), 300))
    return ok(events=[fbx.doc_dict(s) for s in q.stream()])


@bp.post("/fraud/whitelist")
@admin_required
def wl_add():
    d = body()
    kind, value = clean_str(d.get("kind"), 4), clean_str(d.get("value"), 120)
    if kind not in ("uid", "ip", "fp") or not value:
        return err("kind must be uid, ip or fp and value is required")
    if kind == "ip":
        value = F._ipkey(value)
    F.D().collection("fraud_whitelist").document(f"{kind}-{value}").set({"kind": kind, "value": value, "note": clean_str(d.get("note"), 200), "at": now_ms()})
    return ok()


@bp.delete("/fraud/whitelist/<kind>/<value>")
@admin_required
def wl_del(kind, value):
    F.D().collection("fraud_whitelist").document(f"{kind}-{value}").delete()
    return ok()
