"""Server-5 logic (stored in DB3): user log + multi-account detection.

Rules (all configurable by env):
  * MAX_ACCOUNTS_PER_DEVICE  accounts allowed for one device fingerprint     (default 1 = one account per device)
  * MAX_ACCOUNTS_PER_IP      accounts allowed for one IP address              (default 3)  [weaker signal: shared Wi-Fi / mobile data]
  * When a limit is crossed the OLDEST account is kept, the newer ones are banned (FRAUD_ACTION=auto_ban)
    and a case is opened for the admin to review / unban.  FRAUD_ACTION=review only opens the case (no auto ban).
  * Only accounts newer than AUTO_BAN_WINDOW_DAYS (default 30) are auto-banned, whitelisted uid/ip/device are never touched.
Collections (DB3): fraud_users/{uid} · fraud_devices/{sha} · fraud_ips/{ip} · fraud_events/{auto} · fraud_cases/{id} · fraud_whitelist/{id}
"""
import hashlib, re
from firebase_admin import firestore as fs
from . import fbx, svc
from .config import env, env_int
from .util import now_ms

MAX_DEV = lambda: env_int("MAX_ACCOUNTS_PER_DEVICE", 1)
MAX_IP = lambda: env_int("MAX_ACCOUNTS_PER_IP", 3)
WINDOW_DAYS = lambda: env_int("AUTO_BAN_WINDOW_DAYS", 30)
ACTION = lambda: env("FRAUD_ACTION", "auto_ban").lower()


def D():
    return fbx.db("DB3")


def _ipkey(ip):
    return re.sub(r"[^A-Za-z0-9]", "_", ip or "unknown")[:80]


def _fph(fp):
    fp = (fp or "").strip()
    return hashlib.sha256(fp.encode()).hexdigest()[:40] if len(fp) >= 8 else ""


def _wl(kind, value):
    if not value:
        return False
    return D().collection("fraud_whitelist").document(f"{kind}-{value}").get().exists


def _log(p, verdict, reason):
    try:
        D().collection("fraud_events").add({"uid": p.get("uid"), "event": p.get("event", "login"), "ip": p.get("ip"), "fp": _fph(p.get("fp")),
                                           "ua": str(p.get("ua") or "")[:160], "email": p.get("email"), "verdict": verdict, "reason": reason, "at": now_ms()})
    except Exception as e:
        print("[fraud log]", e, flush=True)


def check(p):
    uid = str(p.get("uid") or "")
    ip, fph, event = str(p.get("ip") or ""), _fph(p.get("fp")), p.get("event", "login")
    if not uid:
        return {"ok": True, "verdict": "allow", "reason": "no-uid"}
    now = now_ms()
    db = D()
    uref = db.collection("fraud_users").document(uid)
    cur = uref.get()
    u = cur.to_dict() if cur.exists else {}
    first = u.get("firstSeen") or now
    ipk = _ipkey(ip)
    up = {"uid": uid, "firstSeen": first, "lastSeen": now, "email": p.get("email") or u.get("email"), "lastIp": ip,
          "logins": fs.Increment(1)}
    if ip:
        up["ips"] = fs.ArrayUnion([ipk])
    if fph:
        up["fps"] = fs.ArrayUnion([fph])
    if event == "register" or p.get("isNew"):
        up["registeredAt"] = u.get("registeredAt") or now
    uref.set(up, merge=True)
    if fph:
        db.collection("fraud_devices").document(fph).set({"uids": fs.ArrayUnion([uid]), "lastSeen": now}, merge=True)
    if ip:
        db.collection("fraud_ips").document(ipk).set({"uids": fs.ArrayUnion([uid]), "lastSeen": now, "ip": ip}, merge=True)

    if u.get("status") == "banned":
        _log(p, "block", "already-banned")
        return {"ok": True, "verdict": "block", "reason": "banned", "message": u.get("banReason") or "This account is blocked. Contact support."}

    if _wl("uid", uid) or _wl("ip", ipk) or _wl("fp", fph):
        _log(p, "allow", "whitelisted")
        return {"ok": True, "verdict": "allow", "reason": "whitelisted"}

    group, why = set(), []
    if fph:
        dev = (db.collection("fraud_devices").document(fph).get().to_dict() or {}).get("uids", [])
        if len(dev) > MAX_DEV():
            group |= set(dev)
            why.append(f"{len(dev)} accounts on one device")
    if ip:
        ips = (db.collection("fraud_ips").document(ipk).get().to_dict() or {}).get("uids", [])
        if len(ips) > MAX_IP():
            group |= set(ips)
            why.append(f"{len(ips)} accounts from one IP")
    if len(group) <= 1:
        _log(p, "allow", "ok")
        return {"ok": True, "verdict": "allow", "reason": "ok"}

    # ---- multi-account: keep the oldest, ban the newer ones
    recs = {}
    for x in list(group)[:30]:
        s = db.collection("fraud_users").document(x).get()
        recs[x] = s.to_dict() if s.exists else {}
    order = sorted(group, key=lambda x: ((recs.get(x) or {}).get("firstSeen") or now, x))
    keep = next((x for x in order if (recs.get(x) or {}).get("status") != "banned"), order[0])
    window = now - WINDOW_DAYS() * 86400000
    victims = [x for x in order if x != keep and (recs.get(x) or {}).get("status") != "banned"
               and ((recs.get(x) or {}).get("firstSeen") or now) >= window
               and not _wl("uid", x)]
    reason = "Multiple accounts detected (" + "; ".join(why) + "). Only your first account stays active."
    action = ACTION()
    banned = []
    if action == "auto_ban":
        for x in victims:
            try:
                svc.ban_user(x, reason, "fraud-server")
                db.collection("fraud_users").document(x).set({"status": "banned", "banReason": reason, "bannedAt": now, "keptUid": keep}, merge=True)
                banned.append(x)
            except Exception as e:
                print("[fraud ban]", x, e, flush=True)
    case_id = hashlib.sha1((fph or ipk).encode()).hexdigest()[:20]       # one case per device / IP
    db.collection("fraud_cases").document(case_id).set({
        "id": case_id, "type": "multi_account", "keepUid": keep, "victims": fs.ArrayUnion(victims), "bannedUids": fs.ArrayUnion(banned), "group": fs.ArrayUnion(sorted(group)),
        "ip": ip, "fp": fph, "why": why, "action": action, "status": "open", "updatedAt": now, "createdAt": now}, merge=True)
    blocked = uid in banned
    _log(p, "block" if blocked else "warn", "multi-account")
    return {"ok": True, "verdict": "block" if blocked else ("warn" if uid in victims or uid != keep else "allow"),
            "reason": "multi-account", "message": reason if blocked else "", "caseId": case_id, "keep": keep}
