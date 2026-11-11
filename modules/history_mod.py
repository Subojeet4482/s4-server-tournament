"""S2 - deposit / withdrawal / balance / game / match history (DB2). Users read it; other servers write via /internal/history/write."""
import threading, time
from flask import Blueprint, request, g
from common import history_core as H
from common.authz import user_required, internal_required
from common.config import env_int
from common.util import ok, err, body, rate_ok

bp = Blueprint("history", __name__)


def _page(plural):
    lim = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    before = request.args.get("before")
    items = H.list_kind(g.uid, plural, lim, before)
    return ok(items=items, nextBefore=items[-1]["createdAt"] if len(items) == lim else None)


@bp.get("/history/deposits")
@user_required
def deposits():
    return _page("deposits")


@bp.get("/history/withdrawals")
@user_required
def withdrawals():
    return _page("withdrawals")


@bp.get("/history/balance")
@user_required
def balance():
    return _page("balance")


@bp.get("/history/games")
@user_required
def games():
    return _page("games")


@bp.get("/history/matches")
@user_required
def matches():
    return _page("matches")


@bp.get("/history/wallet")
@user_required
def wallet():
    """Merged list for the wallet page (newest first)."""
    return ok(items=H.wallet_feed(g.uid, max(1, min(int(request.args.get("limit", 60) or 60), 150))))


@bp.get("/history/summary")
@user_required
def summary():
    return ok(counts=H.summary(g.uid))


@bp.post("/internal/history/write")
@internal_required
def write():
    n = H.write_entries((body().get("entries") or [])[:500])
    return ok(written=n)


@bp.post("/internal/history/purge")
@internal_required
def purge():
    return ok(removed=H.purge_old(env_int("HISTORY_RETENTION_DAYS", 60)))


def start_jobs():
    def loop():
        time.sleep(120)
        while True:
            try:
                H.purge_old(env_int("HISTORY_RETENTION_DAYS", 60))
            except Exception as e:
                print("[history purge]", e, flush=True)
            time.sleep(24 * 3600)
    threading.Thread(target=loop, daemon=True, name="history-purge").start()
