"""Environment helpers. Every server reads ONLY env variables (Render dashboard / .env)."""
import os


def env(name, default="", *alts):
    for k in (name,) + alts:
        v = os.environ.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def env_int(name, default=0):
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def env_float(name, default=0.0):
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_bool(name, default=False):
    v = env(name, "")
    if v == "":
        return default
    return v.lower() in ("1", "true", "yes", "on")


# ---- identity of this server ------------------------------------------------
SERVER_ID = env("SERVER_ID", "s?")            # s1 .. s5
SERVER_NAME = env("SERVER_NAME", SERVER_ID)

# ---- secrets shared by ALL servers + load balancer ---------------------------
JWT_SECRET = env("JWT_SECRET")                # signs the 1-hour session token
INTERNAL_KEY = env("INTERNAL_KEY")            # server <-> server / load balancer calls
REQUIRE_LB = env("REQUIRE_LB", "true").lower() not in ("0", "false", "no", "off")   # reject anything that did not come through the load balancer
JWT_TTL_SECONDS = env_int("JWT_TTL_SECONDS", 3600)   # 1 hour

# ---- peers (direct Render URLs) ----------------------------------------------
def peer_url(sid):
    return env(sid.upper() + "_URL").rstrip("/")


ALLOWED_ORIGINS = [o.strip() for o in env("ALLOWED_ORIGINS", "").split(",") if o.strip()]
