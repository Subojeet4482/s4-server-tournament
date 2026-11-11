import time, re, datetime as dt
from flask import request, jsonify

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def now_ms():
    return int(time.time() * 1000)


def ok(**kw):
    kw["ok"] = True
    return jsonify(kw)


def err(msg, _status=400, **kw):
    """Error response. Extra keyword args (e.g. code="banned") become JSON fields."""
    kw.update(ok=False, error=str(msg))
    r = jsonify(kw)
    r.status_code = _status
    return r


def body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def num(v, default=0.0):
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return f
    except (TypeError, ValueError):
        return default


def money(v):
    return round(num(v), 2)


def clean_str(v, n=200):
    return str(v if v is not None else "").strip()[:n]


_RATE = {}


def rate_ok(key, limit, window=60):
    """Sliding window limiter (in memory, per process)."""
    now = time.time()
    arr = [t for t in _RATE.get(key, []) if now - t < window]
    if len(arr) >= limit:
        _RATE[key] = arr
        return False
    arr.append(now)
    _RATE[key] = arr
    if len(_RATE) > 20000:                    # keep memory small
        for k in list(_RATE)[:5000]:
            _RATE.pop(k, None)
    return True


def sanitize_text(s, n=500):
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(s or ""))
    return s.strip()[:n]


def fmt_inr(v):
    v = float(v)
    return f"{v:g}" if v == int(v) else f"{v:.2f}"
