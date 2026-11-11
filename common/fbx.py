"""Firebase / Firestore access.

Each logical database has its own service-account key (base64 of the JSON):
    DB1_FIREBASE_CREDENTIALS_B64   -> main DB   (users, matches, wallet, leaderboard)  + Firebase Auth (token verify)
    DB2_FIREBASE_CREDENTIALS_B64   -> history DB (deposit/withdraw/balance/game/match history)
    DB3_FIREBASE_CREDENTIALS_B64   -> chat DB   (chat, friends, game rooms) + fraud logs
    DB4_FIREBASE_CREDENTIALS_B64   -> OPTIONAL snapshot DB used by Server-4 backups
A server only needs the keys of the DBs it really uses.
"""
import os, json, base64, math, threading, datetime as dt
import firebase_admin
from firebase_admin import credentials, firestore, auth
from google.cloud.firestore_v1 import GeoPoint, DocumentReference
from .config import env

_lock = threading.Lock()
_apps = {}
_dbs = {}
_errors = {}


def _load_key(name):
    raw = env(name + "_FIREBASE_CREDENTIALS_B64", "", name + "_CREDENTIALS_B64")
    if not raw:
        return None
    if raw.startswith("{"):
        return json.loads(raw)
    return json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))


def has_db(name):
    """True when this server has the key for DB `name` (DB1/DB2/DB3/DB4)."""
    return bool(env(name + "_FIREBASE_CREDENTIALS_B64", "", name + "_CREDENTIALS_B64"))


def db(name):
    """Firestore client for DB1/DB2/DB3/DB4 (created lazily, thread-safe)."""
    name = name.upper()
    if name in _dbs:
        return _dbs[name]
    with _lock:
        if name in _dbs:
            return _dbs[name]
        try:
            info = _load_key(name)
            if info is None:
                raise RuntimeError(f"{name}_FIREBASE_CREDENTIALS_B64 env is not set")
            app = firebase_admin.initialize_app(credentials.Certificate(info), name=name.lower())
            _apps[name] = app
            _dbs[name] = firestore.client(app=app)
            _errors.pop(name, None)
            return _dbs[name]
        except Exception as e:
            _errors[name] = str(e)
            raise


def app_of(name):
    db(name)
    return _apps[name.upper()]


def verify_id_token(id_token):
    """Verify a Firebase ID token (register / Google login done by the client SDK) with the DB1 project."""
    return auth.verify_id_token(id_token, app=app_of("DB1"))


def project_id(name):
    try:
        return (_load_key(name) or {}).get("project_id")
    except Exception:
        return None


def status():
    out = {}
    for n in ("DB1", "DB2", "DB3", "DB4"):
        if has_db(n):
            out[n] = {"configured": True, "ok": n in _dbs, "error": _errors.get(n)}
    return out


def ping(name):
    """Cheap read to prove the DB works."""
    try:
        next(iter(db(name).collection("_health").limit(1).stream()), None)
        return True
    except Exception as e:
        _errors[name.upper()] = str(e)
        return False


# ------------------------------------------------------------------ JSON codec (Firestore <-> JSON)
def enc(v):
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=dt.timezone.utc)
        return int(v.timestamp() * 1000)
    if isinstance(v, GeoPoint):
        return {"lat": v.latitude, "lng": v.longitude}
    if isinstance(v, DocumentReference):
        return v.path
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(bytes(v)).decode()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, dict):
        return {k: enc(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [enc(x) for x in v]
    return v


def doc_dict(snap):
    d = enc(snap.to_dict() or {})
    d["id"] = snap.id
    return d
