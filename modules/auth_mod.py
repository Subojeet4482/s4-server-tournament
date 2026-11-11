"""S1 - login / session.
Flow: the app registers / logs in with the Firebase SDK (email+password or Google) -> gets a Firebase ID token ->
POST /auth/login {idToken, deviceFp}. This server verifies that token with Firebase, creates the profile on first login,
asks the fraud server (S5) about IP / device, and returns OUR session token (valid 1 hour). Every other call uses it."""
import random, threading, time
from flask import Blueprint, request, g
from common import fbx, jwtlite, svc, geo, wallet_core as W
from common.authz import user_required, client_ip, device_fp
from common.util import ok, err, body, rate_ok, now_ms, clean_str, num
from common import authz
from common.config import JWT_TTL_SECONDS

bp = Blueprint("auth", __name__)

PUBLIC_FIELDS = ("state", "appName", "email", "phone", "playerId", "gameName", "gameUid", "isUidVerified", "photoUrl", "nameChangesLeft",
                 "matchesPlayed", "matchesWon", "totalEarned", "score", "kills", "isVerified", "joined_matches", "isBanned", "bannedUntil", "banReason",
                 "hasPassword", "paymentMethod", "paymentName")


def public_profile(uid, u):
    dep, wd = W.bal(u)
    out = {k: u.get(k) for k in PUBLIC_FIELDS if k in u}
    out.update(uid=uid, depositBalance=dep, withdrawBalance=wd, balance=round(dep + wd, 2))
    return fbx.enc(out)


def _new_player_id(db):
    for _ in range(25):
        pid = str(random.randint(1000000, 9999999))
        if not list(db.collection("users").where("playerId", "==", pid).limit(1).stream()):
            return pid
    return str(int(time.time() * 1000))[-8:]


def create_profile(uid, dec, d):
    db = W.D()
    phone = "".join(ch for ch in clean_str(d.get("phone"), 20) if ch.isdigit())[-10:]
    name = clean_str(d.get("name"), 30) or dec.get("name") or (dec.get("email") or "Player").split("@")[0]
    doc = {"appName": name, "email": dec.get("email") or "", "phone": phone if len(phone) == 10 else "", "balance": 0, "depositBalance": 0,
           "withdrawBalance": 0, "uid": uid, "playerId": _new_player_id(db), "joined_matches": [], "score": 0, "kills": 0, "matchesPlayed": 0,
           "matchesWon": 0, "totalEarned": 0, "gameName": "", "gameUid": "", "isUidVerified": False, "photoUrl": dec.get("picture") or "",
           "nameChangesLeft": 2, "createdAt": now_ms()}
    W.uref(uid).create(doc)
    return doc


@bp.post("/auth/login")
def login():
    ip = client_ip()
    if not rate_ok("login:" + ip, 30, 60):
        return err("Too many attempts. Wait a minute.", 429)
    d = body()
    tok = clean_str(d.get("idToken"), 4000)
    if not tok:
        return err("idToken required", 400)
    try:
        dec = fbx.verify_id_token(tok)
    except Exception:
        return err("Login could not be verified. Please try again.", 401, code="bad_id_token")
    uid = dec["uid"]
    provider = (dec.get("firebase") or {}).get("sign_in_provider")
    if provider == "password" and not dec.get("email_verified"):
        return err("Please verify your email first.", 403, code="email_not_verified")
    ref = W.uref(uid)
    snap = ref.get()
    is_new = not snap.exists
    try:
        u = create_profile(uid, dec, d) if is_new else (snap.to_dict() or {})
    except Exception:
        u, is_new = (ref.get().to_dict() or {}), False      # created by a parallel request
    if W.is_banned(u):
        return err("Your account is blocked.", 403, code="banned", **W.ban_info(u))
    v = svc.fraud_check({"uid": uid, "ip": ip, "fp": device_fp() or clean_str(d.get("deviceFp"), 128), "event": "register" if is_new else "login",
                         "email": dec.get("email"), "ua": request.headers.get("User-Agent", "")[:160], "isNew": is_new})
    if v.get("verdict") == "block":
        return err(v.get("message") or "This account is blocked. Contact support.", 403, code="banned", reason=v.get("reason"))
    try:
        u, ev = geo.refresh(uid, u, ip)                      # which state is this user in right now?
    except Exception as e:
        print("[geo] login refresh failed:", repr(e), flush=True)
        ev = geo.evaluate(u)
    token, p = jwtlite.issue(uid, {"email": dec.get("email"), "name": u.get("appName")})
    threading.Thread(target=lambda: W.sync_user_history(uid, force=True, limit=150), daemon=True).start()
    return ok(token=token, expiresAt=p["exp"] * 1000, expiresIn=JWT_TTL_SECONDS, isNew=is_new, user=public_profile(uid, u), geo=ev)


@bp.get("/auth/session")
@user_required
def session():
    c = g.claims
    return ok(uid=g.uid, expiresAt=c["exp"] * 1000, remaining=max(0, int(c["exp"] - time.time())))


@bp.post("/auth/logout")
@user_required
def logout():
    authz.revoke_user(g.uid)
    from common import peers
    peers.broadcast("/internal/revoke", {"uid": g.uid, "at": time.time()})
    return ok()
