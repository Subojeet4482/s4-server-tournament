"""S4 - extras of the all-in-one backup server: load-balancer state store + database backups (JSON .gz)."""
import os, io, gzip, json, time, threading, datetime as dt
from flask import Blueprint, request, send_file
from common import fbx
from common.authz import internal_required, admin_required
from common.config import env, env_bool, env_int
from common.util import ok, err, body, now_ms

bp = Blueprint("sys", __name__)
BACKUP_DIR = env("BACKUP_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "backups"))
DEFAULT_COLS = {
    "DB1": "users,matches,categories,config,deposit_requests,gift_codes,lb",
    "DB2": "",                                    # history is big and per-user (sub-collections) - not exported by default
    "DB3": "cprofiles,cfriends,cblocks,cconfig,fraud_users,fraud_cases,fraud_whitelist",
}


# ------------------------------------------------------------------ load balancer state (survives LB restarts)
@bp.get("/internal/sys/lbstate")
@internal_required
def lb_get():
    s = fbx.db("DB3").collection("sys").document("lbstate").get()
    return ok(state=s.to_dict() if s.exists else {})


@bp.put("/internal/sys/lbstate")
@internal_required
def lb_put():
    fbx.db("DB3").collection("sys").document("lbstate").set({"state": body().get("state") or {}, "at": now_ms()})
    return ok()


# ------------------------------------------------------------------ backups
def export(dbname, cols, per_col=20000):
    db, out = fbx.db(dbname), {}
    for c in [x.strip() for x in cols.split(",") if x.strip()]:
        rows = {}
        for i, s in enumerate(db.collection(c).stream()):
            if i >= per_col:
                break
            rows[s.id] = fbx.enc(s.to_dict() or {})
        out[c] = rows
    return out


def run_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    snap = {"createdAt": now_ms(), "dbs": {}}
    for n in ("DB1", "DB2", "DB3"):
        if fbx.has_db(n):
            cols = env(n + "_BACKUP_COLLECTIONS", DEFAULT_COLS[n])
            if cols:
                snap["dbs"][n] = export(n, cols)
    path = os.path.join(BACKUP_DIR, f"backup-{stamp}.json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(snap, f)
    keep = env_int("BACKUP_KEEP", 5)
    files = sorted(x for x in os.listdir(BACKUP_DIR) if x.startswith("backup-"))
    for old in files[:-keep]:
        os.remove(os.path.join(BACKUP_DIR, old))
    if fbx.has_db("DB4"):                          # optional: also keep the latest snapshot inside a 4th Firebase project
        try:
            d4 = fbx.db("DB4")
            for n, cols in snap["dbs"].items():
                for c, rows in cols.items():
                    b, k = d4.batch(), 0
                    for did, data in rows.items():
                        b.set(d4.collection(f"snap_{n}_{c}").document(did), data)
                        k += 1
                        if k >= 400:
                            b.commit(); b, k = d4.batch(), 0
                    if k:
                        b.commit()
        except Exception as e:
            print("[backup DB4]", e, flush=True)
    return os.path.basename(path), os.path.getsize(path)


@bp.post("/backup/run")
@admin_required
def backup_run():
    name, size = run_backup()
    return ok(file=name, bytes=size)


@bp.get("/backup/list")
@admin_required
def backup_list():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return ok(files=[{"name": f, "bytes": os.path.getsize(os.path.join(BACKUP_DIR, f))} for f in sorted(os.listdir(BACKUP_DIR)) if f.startswith("backup-")])


@bp.get("/backup/download/<name>")
@admin_required
def backup_download(name):
    p = os.path.join(BACKUP_DIR, os.path.basename(name))
    if not os.path.isfile(p):
        return err("not found", 404)
    return send_file(p, as_attachment=True)


def start_jobs():
    if not env_bool("BACKUP_AUTO", False):
        return

    def loop():
        time.sleep(300)
        while True:
            try:
                run_backup()
            except Exception as e:
                print("[backup]", e, flush=True)
            time.sleep(env_int("BACKUP_EVERY_HOURS", 24) * 3600)
    threading.Thread(target=loop, daemon=True, name="backup").start()
