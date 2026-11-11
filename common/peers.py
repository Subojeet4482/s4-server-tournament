"""Server -> server HTTP calls with automatic fallback (e.g. S2 down -> S4)."""
import requests
from .config import INTERNAL_KEY, SERVER_ID, peer_url

_sess = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20)
_sess.mount("https://", _adapter)
_sess.mount("http://", _adapter)
ALL = ["s1", "s2", "s3", "s4", "s5"]


def call(sid, method, path, payload=None, timeout=10, headers=None):
    """-> (status_code, json|None). status_code 0 = could not connect."""
    base = peer_url(sid)
    if not base:
        return 0, {"ok": False, "error": f"{sid.upper()}_URL not set"}
    h = {"X-Internal-Key": INTERNAL_KEY, "X-From": SERVER_ID}
    h.update(headers or {})
    try:
        r = _sess.request(method, base + path, json=payload, headers=h, timeout=timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, None
    except Exception as e:
        return 0, {"ok": False, "error": str(e)[:160]}


def chain(order, method, path, payload=None, timeout=10):
    """Try servers in order (skipping myself). Returns (status, json, used_server).
    Connection errors and 502/503/504 move on to the next server; any other answer is final."""
    last = (0, {"ok": False, "error": "no server reachable"}, None)
    for sid in order:
        if sid == SERVER_ID:
            continue
        st, js = call(sid, method, path, payload, timeout)
        if st and st not in (502, 503, 504):
            return st, js, sid
        last = (st, js, sid)
    return last


def broadcast(path, payload=None, timeout=4, skip_self=True):
    res = {}
    for sid in ALL:
        if skip_self and sid == SERVER_ID:
            continue
        if peer_url(sid):
            res[sid] = call(sid, "POST", path, payload, timeout)[0]
    return res
