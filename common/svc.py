"""Service dispatchers: run locally when this server owns the capability (S4 owns all), otherwise call the right peer
with fallback (wallet: S1 -> S4, history: S2 -> S4, fraud: S5 -> S4)."""
from . import registry, peers, outbox


# ------------------------------------------------------------------ history
def _history_deliver(entries):
    """True when delivered. No outbox here."""
    if registry.local("history"):
        from . import history_core
        try:
            history_core.write_entries(entries)
            return True
        except Exception as e:
            print("[history local]", e, flush=True)
            return False
    st, js, used = peers.chain(registry.CHAIN["history"], "POST", "/internal/history/write", {"entries": entries}, timeout=15)
    return st == 200


def history_push(entries):
    if not entries:
        return True
    try:
        if _history_deliver(entries):
            return True
    except Exception as e:
        print("[history]", e, flush=True)
    try:
        outbox.put("history", {"entries": entries})
    except Exception as e:
        print("[history outbox]", e, flush=True)
    return False


OUTBOX_HANDLERS = {"history": lambda p: _history_deliver((p or {}).get("entries") or [])}


# ------------------------------------------------------------------ wallet (DB1)
def _remote(cap, path, payload, timeout=12):
    st, js, used = peers.chain(registry.CHAIN[cap], "POST", path, payload, timeout)
    return st, (js or {})


def wallet_debit(uid, amount, title, kind="game_entry"):
    if registry.local("wallet"):
        from . import wallet_core
        return wallet_core.debit(uid, amount, title, kind)
    st, js = _remote("wallet", "/internal/wallet/debit", {"uid": uid, "amount": amount, "title": title, "kind": kind})
    return bool(js.get("ok")), js


def wallet_credit(uid, amount, title, kind="game_win"):
    if registry.local("wallet"):
        from . import wallet_core
        return wallet_core.credit(uid, amount, title, kind)
    st, js = _remote("wallet", "/internal/wallet/credit", {"uid": uid, "amount": amount, "title": title, "kind": kind})
    return bool(js.get("ok")), js


def user_brief(uid):
    if registry.local("wallet"):
        from . import wallet_core
        return wallet_core.user_brief(uid)
    st, js = _remote("wallet", "/internal/user/brief", {"uid": uid})
    return js.get("user") if js.get("ok") else None


def verified_uids(uids):
    if registry.local("wallet"):
        from . import wallet_core
        return wallet_core.verified_uids(uids)
    st, js = _remote("wallet", "/internal/user/verified", {"uids": list(uids)[:100]})
    return set(js.get("verified") or []) if js.get("ok") else None


def lb_add(uid, name, photo, amount):
    if registry.local("wallet"):
        from . import wallet_core
        return wallet_core.lb_add(uid, name, photo, amount)
    _remote("wallet", "/internal/lb/add", {"uid": uid, "name": name, "photo": photo, "amount": amount})


def ban_user(uid, reason, by="fraud-server"):
    if registry.local("wallet"):
        from . import wallet_core
        return wallet_core.ban_user(uid, reason, by)
    st, js = _remote("wallet", "/internal/user/ban", {"uid": uid, "reason": reason, "by": by})
    return bool(js.get("ok"))


def unban_user(uid, by="admin"):
    if registry.local("wallet"):
        from . import wallet_core
        return wallet_core.unban_user(uid, by)
    st, js = _remote("wallet", "/internal/user/unban", {"uid": uid, "by": by})
    return bool(js.get("ok"))


def users_by_ids(uids):
    """-> {uid: brief} for a list of uids (used by fraud admin views)."""
    out = {}
    for u in uids:
        b = user_brief(u)
        if b:
            out[u] = b
    return out


# ------------------------------------------------------------------ fraud (DB3)
def fraud_check(payload, fail_open=True):
    """-> {verdict: allow|warn|block, ...}. If no fraud server is reachable the login is allowed (fail_open)."""
    if registry.local("fraud"):
        from . import fraud_core
        try:
            return fraud_core.check(payload)
        except Exception as e:
            print("[fraud local]", e, flush=True)
            return {"verdict": "allow", "reason": "fraud-error"} if fail_open else {"verdict": "block", "reason": "fraud-error"}
    st, js, used = peers.chain(registry.CHAIN["fraud"], "POST", "/internal/fraud/check", payload, timeout=15)
    if st == 200 and js and js.get("ok"):
        return js
    return {"verdict": "allow", "reason": "fraud-unreachable"} if fail_open else {"verdict": "block", "reason": "fraud-unreachable"}
