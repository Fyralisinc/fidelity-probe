#!/usr/bin/env python3
"""Offline self-check for the Slack slice.

This is a DEV HARNESS, not part of the shipping client. It stands up a throwaway
HTTP server that returns Slack-spec-faithful payloads (taken from the official
spec's own examples) with real cursor pagination, points the client at it via
SLACK_BASE_URL, and exercises the full historical + live pipeline. The point is to
prove the client's pagination / schema-validation / signature / report machinery
works end-to-end before pointing it at the real mock — without any mock-specific
code in the client itself.

Run:  python scripts/selfcheck_slack.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SPEC = json.loads((ROOT / "specs" / "slack.openapi.json").read_text())


def _example(method_path: str) -> dict:
    ex = SPEC["paths"][method_path]["get"]["responses"]["200"].get("examples") or {}
    return json.loads(json.dumps(next(iter(ex.values())))) if ex else {"ok": True}


class FakeSlack(BaseHTTPRequestHandler):
    def _send(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _params(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode() if length else ""
        params = parse_qs(urlsplit(self.path).query)
        params.update(parse_qs(raw))
        return {k: (v[0] if v else "") for k, v in params.items()}

    def _is_user_token(self) -> bool:
        return (self.headers.get("Authorization") or "").startswith("Bearer xoxp-")

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        params = self._params()
        cursor = params.get("cursor", "")
        types = params.get("types", "")
        channel = params.get("channel", "")
        user_token = self._is_user_token()

        if path.endswith("/auth.test"):
            payload = _example("/auth.test")
            if user_token:
                # A user token authenticates AS the human: user_id present, NO bot_id.
                payload.pop("bot_id", None)
            else:
                payload["bot_id"] = payload.get("bot_id") or "B0SELFBOT0"
            return self._send(payload)

        if channel.startswith(("D", "G")):
            # A DM/MPIM read: only the user token may read it. A bot token is
            # refused exactly as real Slack refuses it.
            if not user_token:
                return self._send({"ok": False, "error": "not_in_channel"})
            payload = _example("/conversations.history")
            payload["response_metadata"] = {"next_cursor": ""}
            return self._send(payload)

        if path.endswith("/conversations.list") and ("im" in types or "mpim" in types):
            # User-token DM listing: an im (counterpart `user`, no name) + an mpim.
            return self._send({
                "ok": True,
                "channels": [
                    {"id": "D0SELFDM01", "created": 1609459200, "is_im": True,
                     "is_org_shared": False, "user": "U0COUNTER0", "is_user_deleted": False,
                     "priority": 0},
                    {"id": "G0SELFMP01", "created": 1609459200, "is_mpim": True,
                     "is_group": False, "is_im": False, "name": "mpdm-a--b--c-1",
                     "is_private": True},
                ],
                "response_metadata": {"next_cursor": ""},
            })

        if path.endswith("/users.list"):
            payload = _example("/users.list")
            # two-page cursor chain to exercise pagination
            payload["response_metadata"] = {"next_cursor": "" if cursor == "PAGE2" else "PAGE2"}
            return self._send(payload)

        if path.endswith("/conversations.list"):
            payload = _example("/conversations.list")
            payload["response_metadata"] = {"next_cursor": ""}
            return self._send(payload)

        if path.endswith("/conversations.history"):
            payload = _example("/conversations.history")
            # ensure one thread parent so replies get exercised
            msgs = payload.get("messages") or []
            if msgs:
                msgs[0]["thread_ts"] = msgs[0].get("ts", "1512085950.000216")
                msgs[0]["reply_count"] = 1
            payload["response_metadata"] = {"next_cursor": ""}
            return self._send(payload)

        if path.endswith("/conversations.replies"):
            payload = _example("/conversations.replies")
            payload["response_metadata"] = {"next_cursor": ""}
            return self._send(payload)

        return self._send({"ok": False, "error": "unknown_method"})

    def log_message(self, *_):
        pass


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), FakeSlack)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ["SLACK_BASE_URL"] = f"http://127.0.0.1:{port}/api/"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-selfcheck"
    os.environ["SLACK_USER_TOKEN"] = "xoxp-selfcheck"   # exercises the DM (im/mpim) path
    os.environ["SLACK_SIGNING_SECRET"] = "selfcheck-secret"

    from ingest.slack import run as slack_run
    from ingest.slack import live
    from ingest.config import SlackConfig
    from ingest.fidelity import FidelityReport
    from ingest.webhook_server import WebhookServer
    from slack_sdk.signature import SignatureVerifier

    print("== historical ==")
    report = slack_run.run_historical()
    server.shutdown()

    failures = []
    if report.object_counts.get("user", 0) <= 0:
        failures.append("no users ingested")
    if report.object_counts.get("channel", 0) <= 0:
        failures.append("no channels ingested")
    if report.object_counts.get("message", 0) <= 0:
        failures.append("no messages ingested")
    if report.pages.get("users.list", 0) < 2:
        failures.append(f"users.list pagination not exercised (pages={report.pages.get('users.list')})")
    if report.pages.get("conversations.replies", 0) < 1:
        failures.append("conversations.replies not exercised")
    # Two-token DM path coverage.
    if report.object_counts.get("im", 0) <= 0:
        failures.append("no im (1:1 DM) ingested via user token")
    if report.object_counts.get("mpim", 0) <= 0:
        failures.append("no mpim (group DM) ingested via user token")
    proto = {c["check"]: c["ok"] for c in report.protocol_checks}
    if not proto.get("two_token.bot_refused_on_dm"):
        failures.append("bot token was NOT refused on a DM (false-green guard failed)")
    if not proto.get("user_token.no_bot_id"):
        failures.append("auth.test on user token wrongly carried bot_id")
    if not proto.get("im.object.has_user"):
        failures.append("im object missing `user` counterpart")
    if not proto.get("im.object.no_name"):
        failures.append("im object wrongly carried a `name`")
    if report.divergences:
        failures.append(f"unexpected divergences: {[d.line() for d in report.divergences]}")

    print(f"  users={report.object_counts.get('user')} channels={report.object_counts.get('channel')} "
          f"messages={report.object_counts.get('message')} replies={report.object_counts.get('reply')}")
    print(f"  im={report.object_counts.get('im')} mpim={report.object_counts.get('mpim')} "
          f"bot_refused_on_dm={proto.get('two_token.bot_refused_on_dm')}")
    print(f"  pages={report.pages}")
    print(f"  divergences={len(report.divergences)}")

    print("== live (signature verification) ==")
    cfg = SlackConfig.from_env()
    live_report = FidelityReport("slack", cfg.base_url)
    srv = WebhookServer("selfcheck")
    live.register(srv, cfg, live_report)
    client = srv.app.test_client()
    verifier = SignatureVerifier(signing_secret=cfg.signing_secret)

    # url_verification handshake (valid signature)
    body = json.dumps({"type": "url_verification", "challenge": "xyz"}).encode()
    ts = str(int(time.time()))
    sig = verifier.generate_signature(timestamp=ts, body=body.decode())
    r = client.post(live.ENDPOINT, data=body,
                    headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                             "Content-Type": "application/json"})
    if r.status_code != 200 or r.get_json().get("challenge") != "xyz":
        failures.append(f"url_verification handshake failed: {r.status_code} {r.data!r}")

    # event_callback (valid signature)
    body2 = json.dumps({"type": "event_callback",
                        "event": {"type": "message", "channel": "C1", "channel_type": "channel",
                                  "ts": "1.2", "text": "hi"}}).encode()
    ts2 = str(int(time.time()))
    sig2 = verifier.generate_signature(timestamp=ts2, body=body2.decode())
    client.post(live.ENDPOINT, data=body2,
                headers={"X-Slack-Request-Timestamp": ts2, "X-Slack-Signature": sig2,
                         "Content-Type": "application/json"})

    # tampered signature -> must be rejected and recorded as a divergence
    r3 = client.post(live.ENDPOINT, data=body2,
                     headers={"X-Slack-Request-Timestamp": ts2, "X-Slack-Signature": "v0=bad",
                              "Content-Type": "application/json"})
    if r3.status_code != 403:
        failures.append(f"tampered signature not rejected: {r3.status_code}")
    if not any(not c["valid"] for c in live_report.signature_checks):
        failures.append("bad signature not recorded as failure")
    if not any(c["valid"] for c in live_report.signature_checks):
        failures.append("valid signature not recorded as ok")

    print(f"  signature_checks={live_report.signature_checks}")
    print(f"  live_events={[e['kind'] for e in live_report.live_events]}")

    print()
    if failures:
        print("SELF-CHECK FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SELF-CHECK PASSED ✅  (historical pagination, schema validation, signature verification)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
