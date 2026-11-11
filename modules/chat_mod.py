"""S3 - full chat system on DB3 (same collections as before: cprofiles, cusernames, cfriends, cblocks, crequests,
cchats/{id}/msgs, cgroups/{id}/msgs, cworld, creports, cconfig).  The phone polls GET /chat/messages?after=... instead of listening to Firestore."""
import re, time, threading
from flask import Blueprint, request, g
from firebase_admin import firestore as fs
from common import fbx, svc
from common.authz import user_required
from common.config import env_int
from common.util import ok, err, body, rate_ok, now_ms, clean_str, sanitize_text

bp = Blueprint("chat", __name__)
MAX_FRIENDS, EDIT_MS, DM_TTL_MS, MAX_TEXT = 20, 10 * 60 * 1000, 7 * 86400000, 1000
WORLD_DEFAULT = {"name": "Booyah Lounge", "emoji": "🔥", "tagline": "Meet players, flex wins, find your squad", "slow": 0, "locked": False, "ttlHours": 24}
_cache = {}


def C():
    return fbx.db("DB3")


def chat_id(a, b):
    return "_".join(sorted([a, b]))


def cached(key, ttl, fn):
    e = _cache.get(key)
    if e and time.time() - e[0] < ttl:
        return e[1]
    v = fn()
    _cache[key] = (time.time(), v)
    return v


def world_cfg():
    return cached("world", 15, lambda: {**WORLD_DEFAULT, **(C().collection("cconfig").document("world").get().to_dict() or {})})


def verified_set():
    return cached("ver", 30, lambda: set((C().collection("cconfig").document("verified").get().to_dict() or {}).get("uids") or []))


def ttl_ms(kind):
    return DM_TTL_MS if kind == "dm" else max(1, min(168, int(world_cfg().get("ttlHours") or 24))) * 3600000


def doc(col, _id):
    return C().collection(col).document(_id)


def get(col, _id):
    s = doc(col, _id).get()
    return s.to_dict() if s.exists else None


def profile(uid):
    return get("cprofiles", uid)


_VER = {}          # uid -> (time, bool)   blue tick copied from the main database, cached 5 min


def warm_verified(uids):
    """Look up the blue tick of many users with ONE call to Server-1 (or Server-4)."""
    now = time.time()
    need = [u for u in dict.fromkeys(uids) if u and (u not in _VER or now - _VER[u][0] > 300)]
    if not need:
        return
    try:
        got = svc.verified_uids(need)
    except Exception as e:
        print("[chat] verified lookup failed:", repr(e), flush=True)
        got = None
    if got is None:
        return                                           # keep the old value, try again later
    for u in need:
        _VER[u] = (now, u in got)


def pub_profile(p, viewer=None, friend=False):
    if not p:
        return None
    private = bool(p.get("isPrivate")) and not friend and p.get("uid") != viewer
    out = {"uid": p.get("uid"), "username": p.get("username"), "name": p.get("name"), "photo": p.get("photo", ""),
           "verified": p.get("uid") in verified_set() or bool(_VER.get(p.get("uid"), (0, p.get("verified")))[1]), "online": bool(p.get("online")), "lastSeen": p.get("lastSeen"),
           "isPrivate": bool(p.get("isPrivate"))}
    out["bio"] = "" if private else p.get("bio", "")
    return out


def friends_of(uid):
    return get("cfriends", uid) or {"list": [], "starred": [], "nicks": {}}


def blocked_of(uid):
    return (get("cblocks", uid) or {}).get("list") or []


def need_profile():
    p = profile(g.uid)
    return p, (None if p else err("Create your chat profile first.", 409, code="no_chat_profile"))


# ------------------------------------------------------------------ boot / profile
@bp.get("/chat/boot")
@user_required
def boot():
    uid = g.uid
    p = profile(uid)
    me = pub_profile(p, uid) if p else None
    out = {"me": me, "world": world_cfg(), "verified": sorted(verified_set())}
    if p:
        fr = friends_of(uid)
        out["friends"] = fr
        out["blocked"] = blocked_of(uid)
        inc = [dict(s.to_dict(), id=s.id) for s in C().collection("crequests").where("to", "==", uid).where("status", "==", "pending").limit(50).stream()]
        outg = [dict(s.to_dict(), id=s.id) for s in C().collection("crequests").where("from", "==", uid).where("status", "==", "pending").limit(50).stream()]
        out["requests"] = {"incoming": inc, "outgoing": outg}
        chats = [dict(s.to_dict(), id=s.id) for s in C().collection("cchats").where("members", "array_contains", uid).limit(100).stream()]
        groups = [dict(s.to_dict(), id=s.id) for s in C().collection("cgroups").where("members", "array_contains", uid).limit(100).stream()]
        out["chats"], out["groups"] = fbx.enc(chats), fbx.enc(groups)
        if not request.args.get("light"):                # light=1 is used by the 5-second polling: profiles are fetched once, then cached by the app
            ids = {uid} | set(fr.get("list", [])) | {c for ch in chats for c in ch.get("members", [])} | {r["from"] for r in inc}
            warm_verified(list(ids)[:60] + [uid])
            out["profiles"] = {i: pub_profile(profile(i), uid, True) for i in list(ids)[:60] if i != uid}
        else:
            warm_verified([uid])
        out["me"] = pub_profile(p, uid)
    return ok(**fbx.enc(out))


@bp.post("/chat/profile")
@user_required
def create_profile():
    uid = g.uid
    if profile(uid):
        return err("Chat profile already exists.", 409)
    uname = clean_str(body().get("username"), 30).lower().lstrip("@")
    if not re.fullmatch(r"[a-z0-9_]{3,20}", uname):
        return err("Username: 3-20 letters, numbers or _")
    brief = svc.user_brief(uid) or {}
    name = brief.get("appName") or clean_str(body().get("name"), 30) or "Player"
    photo = brief.get("photoUrl") or clean_str(body().get("photo"), 300)
    if not (photo.startswith("https://") or photo.startswith("assets/")):
        photo = ""                                       # big base64 profile pictures stay in the main profile; chat uses the default avatar then
    prof = {"uid": uid, "username": uname, "name": name, "photo": photo, "isPrivate": False, "bio": "", "createdAt": now_ms(), "lastSeen": now_ms(), "online": True}
    if brief.get("isVerified"):
        prof["verified"] = True

    @fs.transactional
    def run(tx):
        uref = doc("cusernames", uname)
        if uref.get(transaction=tx).exists:
            return False
        tx.set(uref, {"uid": uid})
        tx.set(doc("cprofiles", uid), prof)
        tx.set(doc("cfriends", uid), {"list": [], "starred": [], "nicks": {}, "lastAdded": ""})
        tx.set(doc("cblocks", uid), {"list": []})
        return True
    if not run(C().transaction()):
        return err("That username is taken - try another.", 409)
    return ok(me=pub_profile(prof, uid))


@bp.patch("/chat/profile")
@user_required
def update_profile():
    p, e = need_profile()
    if e:
        return e
    d, up = body(), {}
    if "bio" in d:
        up["bio"] = sanitize_text(d["bio"], 120)
    if "isPrivate" in d:
        up["isPrivate"] = bool(d["isPrivate"])
    if "photo" in d:
        v = clean_str(d["photo"], 300)
        if v and not (v.startswith("https://") or v.startswith("assets/")):
            return err("Invalid photo.")
        up["photo"] = v
    if "name" in d:
        up["name"] = sanitize_text(d["name"], 30) or p.get("name")
    if up:
        doc("cprofiles", g.uid).update(up)
    return ok(me=pub_profile(dict(p, **up), g.uid))


@bp.post("/chat/presence")
@user_required
def presence():
    if profile(g.uid):
        doc("cprofiles", g.uid).update({"online": bool(body().get("online", True)), "lastSeen": now_ms()})
    return ok()


@bp.get("/chat/user/<uid>")
@user_required
def user_view(uid):
    p = profile(uid)
    if not p:
        return err("User not found", 404)
    warm_verified([uid])
    return ok(user=pub_profile(p, g.uid, uid in (friends_of(g.uid).get("list") or [])), isFriend=uid in (friends_of(g.uid).get("list") or []),
              blocked=uid in blocked_of(g.uid))


@bp.get("/chat/search")
@user_required
def search():
    q = clean_str(request.args.get("q"), 30).lower().lstrip("@")
    if len(q) < 2 or not rate_ok("cs:" + g.uid, 30, 60):
        return ok(users=[])
    rows = C().collection("cprofiles").where("username", ">=", q).where("username", "<=", q + "\uf8ff").limit(15).stream()
    found = [s.to_dict() for s in rows if s.id != g.uid]
    warm_verified([x.get("uid") for x in found])
    return ok(users=[pub_profile(x, g.uid) for x in found])


# ------------------------------------------------------------------ friends
@bp.post("/chat/friends/request")
@user_required
def fr_request():
    p, e = need_profile()
    if e:
        return e
    to = clean_str(body().get("to"), 128)
    if not to or to == g.uid or not profile(to):
        return err("User not found.", 404)
    if to in blocked_of(g.uid) or g.uid in blocked_of(to):
        return err("Couldn't send the request.", 403)
    if len(friends_of(g.uid).get("list") or []) >= MAX_FRIENDS or len(friends_of(to).get("list") or []) >= MAX_FRIENDS:
        return err("Friend list is full.", 403, code="full")
    if not rate_ok("frq:" + g.uid, 20, 3600):
        return err("Too many requests. Try later.", 429)
    rid = g.uid + "_" + to
    doc("crequests", rid).set({"from": g.uid, "to": to, "status": "pending", "createdAt": now_ms(), "fromName": p.get("name"),
                               "fromPhoto": p.get("photo", ""), "fromUsername": p.get("username")})
    return ok(id=rid)


@bp.post("/chat/friends/accept")
@user_required
def fr_accept():
    rid = clean_str(body().get("id"), 300)
    r = get("crequests", rid)
    if not r or r.get("to") != g.uid or r.get("status") != "pending":
        return err("Request not found.", 404)
    if len(friends_of(g.uid).get("list") or []) >= MAX_FRIENDS or len(friends_of(r["from"]).get("list") or []) >= MAX_FRIENDS:
        return err("Friend list is full.", 403, code="full")
    b = C().batch()
    b.update(doc("crequests", rid), {"status": "accepted", "respondedAt": now_ms()})
    b.set(doc("cfriends", g.uid), {"list": fs.ArrayUnion([r["from"]]), "lastAdded": r["from"]}, merge=True)
    b.set(doc("cfriends", r["from"]), {"list": fs.ArrayUnion([g.uid])}, merge=True)
    b.commit()
    return ok()


@bp.post("/chat/friends/reject")
@user_required
def fr_reject():
    rid = clean_str(body().get("id"), 300)
    r = get("crequests", rid)
    if not r or r.get("to") != g.uid:
        return err("Request not found.", 404)
    doc("crequests", rid).update({"status": "rejected", "respondedAt": now_ms()})
    return ok()


@bp.delete("/chat/friends/request/<rid>")
@user_required
def fr_cancel(rid):
    r = get("crequests", rid)
    if r and r.get("from") == g.uid:
        doc("crequests", rid).delete()
    return ok()


@bp.delete("/chat/friends/<uid>")
@user_required
def fr_remove(uid):
    b = C().batch()
    b.set(doc("cfriends", g.uid), {"list": fs.ArrayRemove([uid]), "starred": fs.ArrayRemove([uid])}, merge=True)
    b.set(doc("cfriends", uid), {"list": fs.ArrayRemove([g.uid])}, merge=True)
    b.commit()
    for rid in (g.uid + "_" + uid, uid + "_" + g.uid):
        doc("crequests", rid).delete()
    return ok()


@bp.post("/chat/friends/star")
@user_required
def fr_star():
    uid = clean_str(body().get("uid"), 128)
    on = bool(body().get("star", True))
    doc("cfriends", g.uid).set({"starred": fs.ArrayUnion([uid]) if on else fs.ArrayRemove([uid])}, merge=True)
    return ok()


@bp.post("/chat/friends/nick")
@user_required
def fr_nick():
    uid, nick = clean_str(body().get("uid"), 128), sanitize_text(body().get("nick"), 24)
    doc("cfriends", g.uid).set({"nicks": {uid: nick}}, merge=True)
    return ok()


@bp.post("/chat/block")
@user_required
def block():
    uid, on = clean_str(body().get("uid"), 128), bool(body().get("block", True))
    doc("cblocks", g.uid).set({"list": fs.ArrayUnion([uid]) if on else fs.ArrayRemove([uid])}, merge=True)
    return ok(blocked=on)


# ------------------------------------------------------------------ messages
def _thread(kind, tid):
    """-> (msgs collection ref, parent doc ref|None, error response|None)"""
    uid = g.uid
    if kind == "world":
        return C().collection("cworld"), None, None
    if kind == "dm":
        if uid not in tid.split("_"):
            return None, None, err("Not your chat.", 403)
        return doc("cchats", tid).collection("msgs"), doc("cchats", tid), None
    if kind == "group":
        gr = get("cgroups", tid)
        if not gr or uid not in (gr.get("members") or []):
            return None, None, err("You are not in this group.", 403)
        return doc("cgroups", tid).collection("msgs"), doc("cgroups", tid), None
    return None, None, err("Unknown chat type.")


def _msg_out(s):
    d = fbx.enc(s.to_dict() or {})
    d["id"] = s.id
    return d


@bp.get("/chat/messages")
@user_required
def messages():
    kind, tid = request.args.get("kind", "world"), clean_str(request.args.get("id"), 300)
    col, parent, e = _thread(kind, tid)
    if e:
        return e
    after = int(request.args.get("after", 0) or 0)
    cutoff = max(after, now_ms() - ttl_ms(kind))
    rows = col.where("at", ">", cutoff).order_by("at", direction="DESCENDING").limit(max(1, min(int(request.args.get("limit", 80) or 80), 150))).stream()
    items = [_msg_out(s) for s in rows]
    items.reverse()
    if request.args.get("markRead") and parent is not None:
        try:
            parent.update({f"unread.{g.uid}": 0})
        except Exception:
            pass
    return ok(items=items, now=now_ms())


@bp.post("/chat/messages")
@user_required
def send():
    p, e = need_profile()
    if e:
        return e
    d = body()
    kind, tid = d.get("kind", "world"), clean_str(d.get("id"), 300)
    if kind == "dm" and not tid and d.get("to"):
        tid = chat_id(g.uid, clean_str(d["to"], 128))
    text = sanitize_text(d.get("text"), MAX_TEXT)
    if not text:
        return err("Message is empty.")
    if not rate_ok("msg:" + g.uid, 20, 10):
        return err("You are sending too fast.", 429)
    now = now_ms()
    if kind == "world":
        w = world_cfg()
        if w.get("locked"):
            return err("World chat is locked by admin right now.", 403, code="locked")
        slow = int(w.get("slow") or 0)
        if slow and not rate_ok("slow:" + g.uid, 1, slow):
            return err(f"Slow mode: wait {slow}s between messages.", 429, code="slow")
    other = None
    if kind == "dm":
        parts = tid.split("_")
        if g.uid not in parts or len(parts) != 2:
            return err("Not your chat.", 403)
        other = parts[0] if parts[1] == g.uid else parts[1]
        if other in blocked_of(g.uid):
            return err("You blocked this user.", 403)
        if g.uid in blocked_of(other):
            return err("You cannot message this user.", 403)
        op = profile(other)
        if not op:
            return err("User not found.", 404)
        if op.get("isPrivate") and g.uid not in (friends_of(other).get("list") or []):
            return err("This profile is private - send a friend request first.", 403, code="private")
        if not doc("cchats", tid).get().exists:
            doc("cchats", tid).set({"members": sorted([g.uid, other]), "createdAt": now, "lastMsg": "", "lastAt": 0, "lastFrom": "", "unread": {}})
    col, parent, e = _thread(kind, tid)
    if e:
        return e
    msg = {"from": g.uid, "text": text, "at": now}
    if kind == "world":
        msg.update(name=p.get("name"), photo=p.get("photo", ""))
    rp = d.get("reply")
    if isinstance(rp, dict) and rp.get("id"):
        msg.update(rid=clean_str(rp["id"], 60), rn=clean_str(rp.get("n"), 40), rt=clean_str(rp.get("t"), 140), ru=clean_str(rp.get("u"), 128))
    ref = col.document()
    ref.set(msg)
    last = ("↪ " if rp else "") + text
    if kind == "dm":
        parent.update({"lastMsg": last[:60], "lastAt": now, "lastFrom": g.uid, f"unread.{other}": fs.Increment(1)})
    elif kind == "group":
        gr = get("cgroups", tid) or {}
        up = {"lastMsg": last[:60], "lastAt": now, "lastFrom": g.uid}
        for m in gr.get("members") or []:
            if m != g.uid:
                up[f"unread.{m}"] = fs.Increment(1)
        parent.update(up)
    return ok(message=dict(msg, id=ref.id))


@bp.patch("/chat/messages/<mid>")
@user_required
def edit(mid):
    d = body()
    col, _, e = _thread(d.get("kind", "world"), clean_str(d.get("id"), 300))
    if e:
        return e
    ref = col.document(mid)
    s = ref.get()
    m = s.to_dict() if s.exists else None
    if not m or m.get("deleted"):
        return err("Message not found.", 404)
    mod = g.uid in verified_set()
    own = m.get("from") == g.uid and now_ms() - int(m.get("at", 0)) < EDIT_MS
    if not (own or mod):
        return err("You can only edit your own message for 10 minutes.", 403)
    text = sanitize_text(d.get("text"), MAX_TEXT)
    if not text:
        return err("Message is empty.")
    ref.update({"text": text, "edited": True, **({"editBy": g.uid} if m.get("from") != g.uid else {})})
    return ok()


@bp.delete("/chat/messages/<mid>")
@user_required
def delete(mid):
    col, _, e = _thread(request.args.get("kind", "world"), clean_str(request.args.get("id"), 300))
    if e:
        return e
    ref = col.document(mid)
    s = ref.get()
    m = s.to_dict() if s.exists else None
    if not m:
        return ok()
    mod = g.uid in verified_set()
    own = m.get("from") == g.uid and now_ms() - int(m.get("at", 0)) < EDIT_MS
    if not (own or mod):
        return err("You can only delete your own message for 10 minutes.", 403)
    ref.update({"deleted": True, "text": "", **({"delBy": g.uid} if m.get("from") != g.uid else {})})
    return ok()


@bp.post("/chat/read")
@user_required
def mark_read():
    d = body()
    _, parent, e = _thread(d.get("kind", "dm"), clean_str(d.get("id"), 300))
    if e:
        return e
    if parent is not None:
        parent.update({f"unread.{g.uid}": 0})
    return ok()


# ------------------------------------------------------------------ groups / reports / world
@bp.post("/chat/groups")
@user_required
def group_create():
    p, e = need_profile()
    if e:
        return e
    d = body()
    name = sanitize_text(d.get("name"), 30)
    if len(name) < 2:
        return err("Enter a group name.")
    fr = set(friends_of(g.uid).get("list") or [])
    members = [g.uid] + [m for m in dict.fromkeys(d.get("members") or []) if m in fr and m != g.uid][:19]
    if not rate_ok("grp:" + g.uid, 5, 3600):
        return err("Too many groups created. Try later.", 429)
    ref = C().collection("cgroups").document()
    ref.set({"name": name, "emoji": "🎮", "owner": g.uid, "members": members, "createdAt": now_ms(), "lastMsg": "", "lastAt": now_ms(), "lastFrom": "", "unread": {}})
    return ok(id=ref.id)


@bp.post("/chat/groups/<gid>/leave")
@user_required
def group_leave(gid):
    doc("cgroups", gid).update({"members": fs.ArrayRemove([g.uid])})
    return ok()


@bp.post("/chat/groups/<gid>/add")
@user_required
def group_add(gid):
    gr = get("cgroups", gid)
    if not gr or gr.get("owner") != g.uid:
        return err("Only the group owner can add people.", 403)
    fr = set(friends_of(g.uid).get("list") or [])
    new = [m for m in (body().get("uids") or []) if m in fr and m not in gr.get("members", [])]
    if len(gr.get("members", [])) + len(new) > 20:
        return err("Group is full (20).", 403)
    if new:
        doc("cgroups", gid).update({"members": fs.ArrayUnion(new)})
    return ok(added=len(new))


@bp.post("/chat/report")
@user_required
def report():
    p, _ = profile(g.uid), None
    d = body()
    against = clean_str(d.get("against"), 128)
    if not against or not rate_ok("rep:" + g.uid, 10, 3600):
        return err("Could not send the report.", 429)
    ap = profile(against) or {}
    C().collection("creports").add({"from": g.uid, "against": against, "chatId": clean_str(d.get("chatId"), 300) or "world", "kind": clean_str(d.get("kind"), 12),
                                    "reason": sanitize_text(d.get("reason"), 300), "createdAt": now_ms(), "status": "open",
                                    "fromName": (p or {}).get("name"), "againstName": ap.get("name")})
    return ok()


@bp.get("/chat/replies")
@user_required
def replies_to_me():
    """World messages that are replies to ME (red dot + notification in the app)."""
    cut = now_ms() - ttl_ms("world")
    rows = [_msg_out(s) for s in C().collection("cworld").where("ru", "==", g.uid).limit(30).stream()]
    rows = [m for m in rows if m.get("at", 0) > cut and m.get("from") != g.uid and not m.get("deleted")]
    rows.sort(key=lambda m: m["at"], reverse=True)
    return ok(items=rows[:20])


@bp.get("/chat/world")
@user_required
def world():
    return ok(world=world_cfg())


def cleanup_expired():
    """Delete expired messages (world + a limited number of groups / DMs per run)."""
    n = 0
    cut = now_ms() - ttl_ms("world")
    for s in C().collection("cworld").where("at", "<", cut).limit(300).stream():
        s.reference.delete()
        n += 1
    for coll, kind in (("cgroups", "group"), ("cchats", "dm")):
        c = now_ms() - ttl_ms(kind)
        for parent in C().collection(coll).order_by("lastAt").limit(40).stream():
            for s in parent.reference.collection("msgs").where("at", "<", c).limit(100).stream():
                s.reference.delete()
                n += 1
    return n


def start_jobs():
    def loop():
        time.sleep(180)
        while True:
            try:
                cleanup_expired()
            except Exception as e:
                print("[chat cleanup]", e, flush=True)
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True, name="chat-cleanup").start()
