"""Auth helpers: user session token (1h JWT), internal server key, admin key, real client IP."""
import hmac, time, threading
from functools import wraps
from flask import request, g
from . import jwtlite
from .config import INTERNAL_KEY, env
from .util import err

_revoked = {}                 # uid -> revoked_at (seconds). Tokens issued BEFORE this time are rejected.
_rlock = threading.Lock()


def revoke_user(uid, at=None):
    with _rlock:
        _revoked[uid] = int(at or time.time())
        if len(_revoked) > 50000:
            cutoff = time.time() - 7200
            for k in [k for k, v in _revoked.items() if v < cutoff]:
                _revoked.pop(k, None)


def is_revoked(uid, iat):
    t = _revoked.get(uid)
    return bool(t and iat < t)


def _eq(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(str(a).encode(), str(b).encode())


def internal_ok():
    return bool(INTERNAL_KEY) and _eq(request.headers.get("X-Internal-Key", ""), INTERNAL_KEY)


def client_ip():
    """Real user IP. Behind our load balancer the LB sets X-Real-IP (trusted only with the internal key)."""
    if internal_ok() and request.headers.get("X-Real-IP"):
        return request.headers["X-Real-IP"].strip()[:64]
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[-1].strip()[:64]          # last hop = added by Render's edge, cannot be spoofed
    return (request.remote_addr or "?")[:64]


def device_fp():
    return (request.headers.get("X-Device-FP") or "").strip()[:128]


def bearer():
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def user_required(fn):
    @wraps(fn)
    def w(*a, **k):
        claims = jwtlite.verify(bearer())
        if not claims:
            return err("Session expired. Please login again.", 401, code="token_expired")
        if is_revoked(claims["sub"], claims.get("iat", 0)):
            return err("Session ended. Please login again.", 401, code="token_revoked")
        g.uid = claims["sub"]
        g.claims = claims
        return fn(*a, **k)
    return w


def user_optional(fn):
    """Sets g.uid = None when there is no valid token."""
    @wraps(fn)
    def w(*a, **k):
        claims = jwtlite.verify(bearer())
        if claims and not is_revoked(claims["sub"], claims.get("iat", 0)):
            g.uid, g.claims = claims["sub"], claims
        else:
            g.uid, g.claims = None, None
        return fn(*a, **k)
    return w


def internal_required(fn):
    @wraps(fn)
    def w(*a, **k):
        if not internal_ok():
            return err("forbidden", 403)
        return fn(*a, **k)
    return w


def admin_required(fn):
    """Admin API key only (ADMIN_API_KEY). The internal server key does NOT grant admin rights."""
    @wraps(fn)
    def w(*a, **k):
        key = env("ADMIN_API_KEY")
        if key and _eq(request.headers.get("X-Admin-Key", ""), key):
            return fn(*a, **k)
        return err("forbidden", 403)
    return w
