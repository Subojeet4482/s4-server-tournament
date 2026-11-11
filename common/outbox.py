"""If a peer (e.g. S2/S4 for history) cannot be reached, the event is parked in an outbox and retried every 30 s.
Stored in Firestore (survives Render restarts) on the first DB this server owns."""
import json, threading, time
from . import fbx
from .util import now_ms

_started = False


def _db():
    for n in ("DB1", "DB3", "DB2"):
        if fbx.has_db(n):
            return fbx.db(n)
    return None


def put(topic, payload):
    db = _db()
    if db is None:
        print("[outbox] no DB for outbox, dropped", topic, flush=True)
        return False
    db.collection("_outbox").add({"topic": topic, "payload": json.dumps(payload), "at": now_ms(), "tries": 0})
    return True


def drain(handlers, limit=40):
    db = _db()
    if db is None:
        return 0
    sent = 0
    for s in db.collection("_outbox").order_by("at").limit(limit).stream():
        d = s.to_dict() or {}
        h = handlers.get(d.get("topic"))
        if not h:
            continue
        try:
            if h(json.loads(d.get("payload") or "null")):
                s.reference.delete()
                sent += 1
            else:
                s.reference.update({"tries": int(d.get("tries", 0)) + 1})
        except Exception:
            pass
    return sent


def start(handlers, every=30):
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(20)
        while True:
            try:
                drain(handlers)
            except Exception as e:
                print("[outbox]", e, flush=True)
            time.sleep(every)
    threading.Thread(target=loop, daemon=True, name="outbox").start()
