"""STATE PAYMENT BAN.

Users from the blocked states (default: Andhra Pradesh, Assam, Odisha, Telangana, Nagaland, Sikkim) cannot use payment features.
The app hides the payment option, shows a safety message and the user can still join FREE matches.

How the user's state is known (all three are combined, the STRICTEST wins):
  1. declared  - the user picks his state in the app (POST /user/state).  Changing it later is limited (STATE_CHANGE_DAYS).
  2. ip        - IP geolocation (looked up at every login and before every money action; cached).
  3. sticky    - if an IP in a blocked state was seen in the last GEO_STICKY_DAYS days the block stays (stops "switch VPN after login").
An admin can override one user ("allow" / "block") and edit the list / switches / message from the Telegram bot.
Config lives in DB1  config/geo.   Per user (DB1 users/{uid}):  state, stateChangedAt, geoState, geoCountry, geoCheckedAt, geoSeenBlockedAt, geoOverride.
"""
import re, time, ipaddress, threading
import requests
from . import fbx
from .config import env, env_bool, env_int
from .util import now_ms

STATES = sorted([
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry"])
_CANON = {re.sub(r"[^a-z]", "", s.lower()): s for s in STATES}
_ALIASES = {"orissa": "Odisha", "uttaranchal": "Uttarakhand", "pondicherry": "Puducherry", "nctofdelhi": "Delhi", "newdelhi": "Delhi",
            "nationalcapitalterritoryofdelhi": "Delhi", "andamanandnicobar": "Andaman and Nicobar Islands", "andamannicobarislands": "Andaman and Nicobar Islands",
            "dadraandnagarhaveli": "Dadra and Nagar Haveli and Daman and Diu", "damananddiu": "Dadra and Nagar Haveli and Daman and Diu",
            "dadranagarhavelidamandiu": "Dadra and Nagar Haveli and Daman and Diu", "jammukashmir": "Jammu and Kashmir", "telengana": "Telangana",
            "andhra": "Andhra Pradesh", "bengal": "West Bengal", "chattisgarh": "Chhattisgarh"}
DEFAULT_BLOCKED = ["Andhra Pradesh", "Assam", "Odisha", "Telangana", "Nagaland", "Sikkim"]
DEFAULT_MESSAGE = ("🛡️ Aapki safety ke liye aapke state me payment system hide hai. "
                   "Aap free matches join kar sakte ho aur free game khel sakte ho.")
FLAGS = ("blockDeposit", "blockGift", "blockPaidJoin", "blockPaidGame", "blockWithdraw")   # what a blocked user cannot do
DEFAULT_CFG = {"enabled": True, "blockedStates": list(DEFAULT_BLOCKED), "blockDeposit": True, "blockGift": True, "blockPaidJoin": True,
               "blockPaidGame": True, "blockWithdraw": False, "message": DEFAULT_MESSAGE}


def norm_state(text):
    """'orissa' / 'TELANGANA ' / 'State of Assam' -> canonical state name or ''."""
    t = re.sub(r"\bstate of\b|\bstate\b|\bunion territory of\b", " ", str(text or "").lower().replace("&", " and "))
    k = re.sub(r"[^a-z]", "", t)
    return _CANON.get(k) or _ALIASES.get(k) or ""


# ------------------------------------------------------------------ config (DB1 config/geo, cached)
_cfg = {"t": 0, "v": None}
_lock = threading.Lock()


def get_cfg(force=False):
    if not force and _cfg["v"] is not None and time.time() - _cfg["t"] < 20:
        return _cfg["v"]
    v = dict(DEFAULT_CFG)
    env_list = [norm_state(x) for x in env("GEO_BLOCKED_STATES").split(",") if x.strip()]
    if env_list:
        v["blockedStates"] = [x for x in env_list if x]
    if not env_bool("GEO_ENABLED", True):
        v["enabled"] = False
    try:
        d = fbx.db("DB1").collection("config").document("geo").get()
        if d.exists:
            v.update({k: x for k, x in (d.to_dict() or {}).items() if k in DEFAULT_CFG})
    except Exception as e:
        print("[geo] config read failed:", e, flush=True)
        if _cfg["v"] is not None:
            return _cfg["v"]
    v["blockedStates"] = sorted({norm_state(x) for x in v.get("blockedStates", []) if norm_state(x)})
    _cfg.update(t=time.time(), v=v)
    return v


def save_cfg(patch):
    cur = dict(get_cfg(True))
    if "blockedStates" in patch:
        cur["blockedStates"] = sorted({norm_state(x) for x in (patch["blockedStates"] or []) if norm_state(x)})
    for k in ("enabled",) + FLAGS:
        if k in patch:
            cur[k] = bool(patch[k])
    if "message" in patch:
        m = str(patch["message"] or "").strip()[:300]
        cur["message"] = m or DEFAULT_MESSAGE
    fbx.db("DB1").collection("config").document("geo").set({k: cur[k] for k in DEFAULT_CFG})
    _cfg.update(t=time.time(), v=cur)
    return cur


# ------------------------------------------------------------------ IP -> state
_ip_cache = {}
PROVIDERS = {
    "ipwhois": lambda ip: _parse(requests.get(f"https://ipwho.is/{ip}", params={"fields": "success,country_code,region"}, timeout=4).json(), "region", "country_code", "success"),
    "ipapi": lambda ip: _parse(requests.get(f"https://ipapi.co/{ip}/json/", timeout=4).json(), "region", "country_code"),
    "ipinfo": lambda ip: _parse(requests.get(f"https://ipinfo.io/{ip}/json", params={"token": env("IPINFO_TOKEN")}, timeout=4).json(), "region", "country"),
}


def _parse(j, region_key, country_key, ok_key=None):
    if not isinstance(j, dict) or (ok_key and not j.get(ok_key)) or j.get("error"):
        raise RuntimeError("provider error")
    return {"state": norm_state(j.get(region_key)), "country": str(j.get(country_key) or "").upper(), "raw": str(j.get(region_key) or "")}


def _public_ip(ip):
    try:
        a = ipaddress.ip_address(ip)
        return not (a.is_private or a.is_loopback or a.is_reserved or a.is_link_local or a.is_multicast)
    except ValueError:
        return False


def lookup_ip(ip):
    """-> {"state": "Telangana"|"", "country": "IN"} or None when no provider could answer. Cached 24 h (failures 5 min)."""
    if not ip or not _public_ip(ip):
        return None
    e = _ip_cache.get(ip)
    if e and time.time() - e[0] < (86400 if e[1] else 300):
        return e[1]
    names = [p.strip() for p in env("GEO_PROVIDERS", "ipwhois,ipapi").split(",") if p.strip()]
    if env("IPINFO_TOKEN") and "ipinfo" not in names:
        names.append("ipinfo")
    res = None
    for n in names:
        fn = PROVIDERS.get(n)
        if not fn:
            continue
        try:
            res = fn(ip)
            if res is not None:
                break
        except Exception as ex:
            print(f"[geo] {n} failed for {ip}: {type(ex).__name__}", flush=True)
    if len(_ip_cache) > 20000:
        _ip_cache.clear()
    _ip_cache[ip] = (time.time(), res)
    return res


# ------------------------------------------------------------------ decision
def evaluate(u, cfg=None, ip_state=None):
    """Pure function (no network). `u` = the user document. -> dict used by the API and the app."""
    cfg = cfg or get_cfg()
    declared = norm_state(u.get("state"))
    ipst = norm_state(ip_state if ip_state is not None else u.get("geoState"))
    bset = set(cfg["blockedStates"])
    blocked, reason = False, None
    if cfg["enabled"]:
        ov = u.get("geoOverride")
        if ov == "allow":
            pass
        elif ov == "block":
            blocked, reason = True, "admin"
        elif declared in bset:
            blocked, reason = True, "declared"
        elif ipst in bset:
            blocked, reason = True, "ip"
        else:
            days = env_int("GEO_STICKY_DAYS", 3)
            seen = int(u.get("geoSeenBlockedAt") or 0)
            if days > 0 and seen and now_ms() - seen < days * 86400000:
                blocked, reason = True, "sticky"
    f = lambda flag: not (blocked and cfg.get(flag, True))
    return {"enabled": cfg["enabled"], "state": declared or ipst or None, "declaredState": declared or None, "ipState": ipst or None, "blocked": blocked,
            "reason": reason, "needsState": not declared, "paymentAllowed": f("blockDeposit"), "canDeposit": f("blockDeposit"), "canRedeemGift": f("blockGift"),
            "canJoinPaid": f("blockPaidJoin"), "canPlayPaidGame": f("blockPaidGame"), "canWithdraw": f("blockWithdraw"), "canJoinFree": True,
            "message": cfg["message"] if blocked else ""}


def refresh(uid, u, ip):
    """Look up the CURRENT ip, remember it on the user (only when something changed) and return (updated user dict, evaluation)."""
    cfg = get_cfg()
    res = lookup_ip(ip) if cfg["enabled"] else None
    ip_state = None
    if res is not None:
        ip_state = res["state"]
        upd, now = {}, now_ms()
        if res["state"] != (u.get("geoState") or "") or res["country"] != (u.get("geoCountry") or ""):
            upd.update(geoState=res["state"], geoCountry=res["country"], geoCheckedAt=now)
        elif now - int(u.get("geoCheckedAt") or 0) > 3600000:
            upd["geoCheckedAt"] = now
        if res["state"] in cfg["blockedStates"] and now - int(u.get("geoSeenBlockedAt") or 0) > 3600000:
            upd["geoSeenBlockedAt"] = now
        if upd:
            try:
                fbx.db("DB1").collection("users").document(uid).set(upd, merge=True)
            except Exception as e:
                print("[geo] save failed:", e, flush=True)
            u = dict(u, **upd)
    elif cfg["enabled"] and env_bool("GEO_FAIL_CLOSED", False) and not norm_state(u.get("state")) and not norm_state(u.get("geoState")):
        return u, dict(evaluate(u, cfg), blocked=True, reason="unknown", paymentAllowed=False, canDeposit=False, canJoinPaid=False, canPlayPaidGame=False,
                       canRedeemGift=False, message=cfg["message"])
    return u, evaluate(u, cfg, ip_state)


FEATURE_FLAG = {"deposit": "canDeposit", "gift": "canRedeemGift", "join": "canJoinPaid", "game": "canPlayPaidGame", "withdraw": "canWithdraw"}


def deny(ev):
    """Body for the error response when a blocked user tries a paid feature."""
    return {"code": "payment_blocked_state", "message": ev["message"], "state": ev["state"], "reason": ev["reason"]}


def stored_allows(u, feature):
    """No-network check from the data already saved on the user (used inside Firestore transactions / other servers)."""
    return evaluate(u)[FEATURE_FLAG[feature]]
