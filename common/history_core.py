"""History store (DB2). Layout:  userhist/{uid}/{kind}/{id}   kind = deposits | withdrawals | balance | games | matches
(sub-collections per user, so no composite index is needed)."""
import re
from . import fbx
from .util import now_ms

KINDS = {"deposit": "deposits", "withdrawal": "withdrawals", "balance": "balance", "game": "games", "match": "matches"}
PLURAL = {v: k for k, v in KINDS.items()}


def _out(d):
    d["_id"] = d.get("srcId") or d["id"]            # the id the app already knows (the original transaction id)
    return d


def _col(uid, plural):
    return fbx.db("DB2").collection("userhist").document(uid).collection(plural)


def write_entries(entries):
    """entries: [{kind, uid, id, data}] -> idempotent upsert (merge). Returns count written."""
    if not entries:
        return 0
    db = fbx.db("DB2")
    batch, n, total = db.batch(), 0, 0
    for e in entries:
        kind = KINDS.get(str(e.get("kind")))
        uid, eid = str(e.get("uid") or ""), re.sub(r"[/\s]", "_", str(e.get("id") or ""))
        if not (kind and uid and eid):
            continue
        d = dict(e.get("data") or {})
        d.setdefault("createdAt", now_ms())
        d.update(uid=uid, entryKind=e["kind"], updatedAt=now_ms())
        batch.set(_col(uid, kind).document(eid), d, merge=True)
        n += 1
        total += 1
        if n >= 400:
            batch.commit()
            batch, n = db.batch(), 0
    if n:
        batch.commit()
    return total


def list_kind(uid, plural, limit=50, before=None):
    q = _col(uid, plural).order_by("createdAt", direction="DESCENDING")
    if before:
        q = q.start_after({"createdAt": int(before)})
    return [_out(fbx.doc_dict(s)) for s in q.limit(max(1, min(int(limit), 200))).stream()]


def wallet_feed(uid, limit=60):
    """Merged newest-first list used by the wallet page (deposits + withdrawals + balance changes, no duplicates)."""
    items = []
    for plural in ("deposits", "withdrawals", "balance"):
        items += [dict(x, _src=plural) for x in list_kind(uid, plural, limit)]
    seen, out = set(), []
    for x in sorted(items, key=lambda r: r.get("createdAt", 0), reverse=True):
        key = x.get("srcId") or x.get("trxId") or x["id"]
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out[:limit]


def summary(uid):
    out = {}
    for plural in KINDS.values():
        try:
            out[plural] = int(_col(uid, plural).count().get()[0][0].value)
        except Exception:
            out[plural] = None
    return out


def purge_old(days=60, max_users=200):
    """Remove history older than `days` (old app did a monthly cleanup). Light: scans a limited number of users per run."""
    cutoff = now_ms() - days * 86400000
    db, removed = fbx.db("DB2"), 0
    for u in db.collection("userhist").limit(max_users).stream():
        for plural in KINDS.values():
            for s in u.reference.collection(plural).where("createdAt", "<", cutoff).limit(200).stream():
                if (s.to_dict() or {}).get("status") == "pending":      # an unpaid withdrawal / unverified deposit must stay visible
                    continue
                s.reference.delete()
                removed += 1
    return removed
