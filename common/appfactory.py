"""Builds the Flask app of a server: CORS, JSON errors, /health, maintenance switch (admin on/off), background jobs."""
import os, time, threading
import requests as _rq
from flask import Flask, request, jsonify, g
from . import config, fbx, registry, authz, outbox, svc
from .util import ok, err, body

STARTED = time.time()
_state = {"maintenance": False, "deep": (0, {})}


def create_app(server_id, title, modules, caps, dbs):
    """modules: imported blueprint modules; caps: capabilities provided; dbs: ('DB1',...) this server needs."""
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024          # 1 MB request limit
    registry.provide(*caps)
    me = {"id": server_id, "title": title}

    warn = [m for m in ("JWT_SECRET", "INTERNAL_KEY") if not config.env(m)]
    warn += [f"{d}_FIREBASE_CREDENTIALS_B64" for d in dbs if not fbx.has_db(d)]
    if warn:
        print("[config] MISSING env:", ", ".join(warn), flush=True)

    # ---------- CORS (the load balancer normally does it; servers answer too so a direct fallback works)
    def origin_ok(o):
        return bool(o) and ("*" in config.ALLOWED_ORIGINS or o in config.ALLOWED_ORIGINS)

    @app.before_request
    def pre():
        if request.method == "OPTIONS":
            return ("", 204)
        # Anti-bypass: the Render URL of a server is public, so every request except /health must carry the secret key
        # that only the load balancer (and the other servers) know. Domain verification happens in the load balancer.
        if config.REQUIRE_LB and request.path not in ("/health", "/") and not authz.internal_ok():
            return err("Direct access is not allowed. Use the official API.", 403, code="use_load_balancer")
        if _state["maintenance"] and not (request.path in ("/health", "/") or request.path.startswith("/internal/")):
            return err("Server is switched off by admin.", 503, code="maintenance")

    @app.after_request
    def post(r):
        o = request.headers.get("Origin")
        if origin_ok(o):
            r.headers["Access-Control-Allow-Origin"] = o if "*" not in config.ALLOWED_ORIGINS else "*"
            r.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Device-FP, X-Admin-Key"
            r.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            r.headers["Vary"] = "Origin"
        r.headers["X-Served-By"] = server_id
        r.headers["Cache-Control"] = "no-store"
        return r

    # ---------- errors -> JSON
    @app.errorhandler(404)
    def e404(e):
        return err("Not found", 404)

    @app.errorhandler(405)
    def e405(e):
        return err("Method not allowed", 405)

    @app.errorhandler(Exception)
    def e500(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return err(e.description, e.code)
        print("[error]", request.method, request.path, repr(e), flush=True)
        return err("Server error - please try again.", 500)

    # ---------- common routes
    @app.get("/")
    def root():
        return ok(server=server_id, title=title)

    @app.get("/health")
    def health():
        if not authz.internal_ok():                       # public callers only learn that the service is alive
            return jsonify(ok=not _state["maintenance"]), (503 if _state["maintenance"] else 200)
        out = {"server": server_id, "title": title, "caps": sorted(registry.CAPS), "uptime": int(time.time() - STARTED),
               "maintenance": _state["maintenance"], "time": int(time.time() * 1000)}
        if request.args.get("deep"):
            t, cached = _state["deep"]
            if time.time() - t > 45:
                cached = {d: fbx.ping(d) for d in dbs if fbx.has_db(d)}
                _state["deep"] = (time.time(), cached)
            out["db"] = cached
            if cached and not all(cached.values()):
                return jsonify(dict(out, ok=False)), 503
        elif warn:
            out["warn"] = warn
        return jsonify(dict(out, ok=not _state["maintenance"])), (503 if _state["maintenance"] else 200)

    @app.post("/internal/revoke")
    def revoke():
        if not authz.internal_ok():
            return err("forbidden", 403)
        d = body()
        authz.revoke_user(str(d.get("uid")), d.get("at"))
        return ok()

    @app.post("/internal/maintenance")
    def maintenance():
        if not authz.internal_ok():
            return err("forbidden", 403)
        _state["maintenance"] = bool(body().get("on"))
        return ok(maintenance=_state["maintenance"])

    @app.get("/internal/status")
    def status():
        if not authz.internal_ok():
            return err("forbidden", 403)
        return ok(server=server_id, caps=sorted(registry.CAPS), db=fbx.status(), maintenance=_state["maintenance"], uptime=int(time.time() - STARTED))

    # ---------- modules + jobs
    for m in modules:
        app.register_blueprint(m.bp)
    for m in modules:
        if hasattr(m, "start_jobs"):
            try:
                m.start_jobs()
            except Exception as e:
                print("[jobs]", m.__name__, e, flush=True)
    if any(fbx.has_db(d) for d in ("DB1", "DB2", "DB3")):
        outbox.start(svc.OUTBOX_HANDLERS)
    _keepalive()
    print(f"[{server_id}] ready - caps={sorted(registry.CAPS)} routes={len(list(app.url_map.iter_rules()))}", flush=True)
    return app


def _keepalive():
    url = config.env("KEEPALIVE_URL", "", "RENDER_EXTERNAL_URL")
    if not url or not config.env_bool("KEEPALIVE", True):
        return

    def loop():
        time.sleep(60)
        while True:
            try:
                _rq.get(url.rstrip("/") + "/health", timeout=15)
            except Exception:
                pass
            time.sleep(config.env_int("KEEPALIVE_INTERVAL", 600))
    threading.Thread(target=loop, daemon=True, name="keepalive").start()
