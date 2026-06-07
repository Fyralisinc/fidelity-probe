"""Tiny Flask host for inbound signed webhooks (Slack Events API now, GitHub later).

Slices register their own route on the shared app; the route is responsible for
signature verification *before* trusting any payload. This module just owns the
app lifecycle and a health check.
"""
from __future__ import annotations

from flask import Flask, jsonify

from .config import WebhookConfig


class WebhookServer:
    def __init__(self, name: str = "ingest-webhooks"):
        self.app = Flask(name)

        @self.app.get("/healthz")
        def _health():  # noqa: ANN202
            return jsonify({"ok": True})

    def run(self, cfg: WebhookConfig | None = None) -> None:
        cfg = cfg or WebhookConfig.from_env()
        # threaded so a slow handler can't wedge the listener; debug off = prod-like.
        self.app.run(host=cfg.host, port=cfg.port, threaded=True, debug=False)

    def serve_for(self, cfg: WebhookConfig | None, seconds: float) -> None:
        """Serve for a fixed wall-clock budget, then shut down cleanly.

        Lets a live listener run unattended (e.g. driven by an emitter) and stop
        on its own so the fidelity report is written without a manual Ctrl-C.
        """
        import threading
        import time

        from werkzeug.serving import make_server

        cfg = cfg or WebhookConfig.from_env()
        srv = make_server(cfg.host, cfg.port, self.app, threaded=True)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            time.sleep(seconds)
        finally:
            srv.shutdown()
            thread.join(timeout=5)
