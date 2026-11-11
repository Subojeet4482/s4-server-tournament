"""Tiny HS256 JWT (no external dependency). Session token valid for 1 hour (JWT_TTL_SECONDS)."""
import hmac, hashlib, json, base64, time
from .config import JWT_SECRET, JWT_TTL_SECONDS


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sig(msg):
    return _b64(hmac.new(JWT_SECRET.encode(), msg.encode(), hashlib.sha256).digest())


def issue(uid, extra=None, ttl=None):
    if len(JWT_SECRET) < 16:
        raise RuntimeError("JWT_SECRET is missing or too short (min 16 chars)")
    now = int(time.time())
    payload = {"sub": uid, "iat": now, "exp": now + int(ttl or JWT_TTL_SECONDS)}
    payload.update(extra or {})
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return head + "." + body + "." + _sig(head + "." + body), payload


def verify(token):
    """-> payload dict or None (bad signature / expired)."""
    try:
        if len(JWT_SECRET) < 16:
            return None
        h, b, s = token.split(".")
        if not hmac.compare_digest(s, _sig(h + "." + b)):
            return None
        p = json.loads(_unb64(b))
        if int(p.get("exp", 0)) < time.time():
            return None
        return p
    except Exception:
        return None
