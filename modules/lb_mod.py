"""S1 - leaderboard (Daily / Weekly / Monthly) read from lb/{d|w|m}-key, written when prizes are credited."""
import time, datetime as dt
from flask import Blueprint, request, g
from common import fbx, wallet_core as W
from common.authz import user_optional
from common.util import ok, err, IST

bp = Blueprint("leaderboard", __name__)
_c = {}


def lb_key(p, d=None):
    d = d or dt.datetime.now(IST)
    if p == "d":
        return "d-" + d.strftime("%Y%m%d")
    if p == "w":
        return "w-" + (d - dt.timedelta(days=d.weekday())).strftime("%Y%m%d")
    return "m-" + d.strftime("%Y%m")


@bp.get("/leaderboard")
@user_optional
def leaderboard():
    p = request.args.get("period", "d")
    if p not in ("d", "w", "m"):
        return err("period must be d, w or m")
    key = lb_key(p)
    e = _c.get(key)
    if not e or time.time() - e[0] > 10:
        db = W.D()
        pl = (db.collection("lb").document(key).get().to_dict() or {}).get("players") or {}
        # admin controls (admin panel -> Leaderboard): hidden accounts are left out, blue-tick users get the tick even if the entry was added by hand
        hidden = set((db.collection("config").document("leaderboard").get().to_dict() or {}).get("hidden") or [])
        ver = set((db.collection("config").document("verified").get().to_dict() or {}).get("uids") or [])
        rows = [{"id": uid, "name": v.get("n") or "User", "score": float(v.get("a") or 0), "isVerified": bool(v.get("v")) or uid in ver, "game": float(v.get("g") or 0),
                 "img": v.get("p") or ""} for uid, v in pl.items() if float(v.get("a") or 0) > 0 and uid not in hidden]
        rows.sort(key=lambda r: r["score"], reverse=True)
        e = _c[key] = (time.time(), rows)
    rows = e[1]
    top = [dict(r, rank=i + 1, isMe=r["id"] == g.uid) for i, r in enumerate(rows[:50])]
    me = next((dict(r, rank=i + 1) for i, r in enumerate(rows) if r["id"] == g.uid), None) if g.uid else None
    for r in top:
        r.pop("id", None) if r["id"] != g.uid else None      # other players' uids are not exposed
    return ok(period=p, key=key, players=top, me=me)


@bp.get("/leaderboard/config")
def lb_config():
    c = W.D().collection("config").document("leaderboard").get().to_dict() or {}
    now = dt.datetime.now(IST)
    end = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return ok(rewards=[r for r in (c.get("rewards") or []) if float(r.get("amount") or 0) > 0], monthEndsAt=int(end.timestamp() * 1000))
