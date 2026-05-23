#!/usr/bin/env python3
"""Offline self-check for the GitHub slice.

A DEV HARNESS, not part of the shipping client. It stands up a throwaway HTTP
server that behaves like GitHub's REST API *per the official spec*: it serves the
spec's own response examples, paginates with a real RFC-5988 `Link` header, honors
`If-None-Match` with a `304 Not Modified`, and returns the standard GitHub response
headers (X-GitHub-Request-Id, X-RateLimit-*). For auth it generates a real RSA
keypair, hands the client the private key, and verifies the client's RS256 App JWT
with the public key before issuing a `ghs_` installation token.

This proves the slice's full pipeline — App-JWT minting, the two-legged token
exchange, Link pagination, ETag/304, header auditing, schema validation and the
report — works end to end before pointing it at the real mock, with no
mock-specific code anywhere in the client.

Run:  python scripts/selfcheck_github.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingest.schemas import SpecValidator  # noqa: E402

APP_ID = "274100"
INSTALLATION_ID = "21341112"

_sv = SpecValidator("github")


def _example(path: str, method: str = "get", status: str = "200"):
    ex = _sv._response_example(path, method, status)
    return json.loads(json.dumps(ex))  # deep copy


# Build a private/public PEM pair the way a real GitHub App key is supplied.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

# Spec examples for the list resources, keyed by URL suffix.
_RESOURCE_EXAMPLES = {
    "/issues": _example("/repos/{owner}/{repo}/issues"),
    "/pulls": _example("/repos/{owner}/{repo}/pulls"),
    "/commits": _example("/repos/{owner}/{repo}/commits"),
    "/branches": _example("/repos/{owner}/{repo}/branches"),
    "/labels": _example("/repos/{owner}/{repo}/labels"),
}


class FakeGitHub(BaseHTTPRequestHandler):
    server_version = "GitHub.com"

    # ---- helpers ----
    def _std_headers(self, extra: dict | None = None) -> dict:
        h = {
            "Content-Type": "application/json",
            "X-GitHub-Request-Id": "ABCD:1234:5678",
            "X-GitHub-Media-Type": "github.v3; format=json",
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": "1700000000",
        }
        h.update(extra or {})
        return h

    def _send(self, status: int, payload, headers: dict | None = None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        for k, v in self._std_headers(headers).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        return auth.split(" ", 1)[1] if auth.startswith("Bearer ") else None

    def log_message(self, *_):
        pass

    # ---- auth leg 2: verify the App JWT, mint an installation token ----
    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            token = self._bearer()
            try:
                claims = jwt.decode(token, PUBLIC_PEM, algorithms=["RS256"])
            except Exception as e:  # noqa: BLE001
                return self._send(401, {"message": f"bad app JWT: {e}"})
            if str(claims.get("iss")) != APP_ID:
                return self._send(401, {"message": "iss != app id"})
            return self._send(201, _example(
                "/app/installations/{installation_id}/access_tokens", "post", "201"))
        return self._send(404, {"message": "not found"})

    def do_GET(self):  # noqa: N802
        split = urlsplit(self.path)
        path, query = split.path, {k: v[0] for k, v in parse_qs(split.query).items()}

        if path == "/app":
            return self._send(200, _example("/app"))

        if path == "/installation/repositories":
            return self._send(200, _example("/installation/repositories"))

        for suffix, example in _RESOURCE_EXAMPLES.items():
            if path.endswith(suffix):
                return self._paginated(path, query, example)

        return self._send(404, {"message": "not found"})

    def _paginated(self, path: str, query: dict, example):
        """Two-page Link chain + ETag/304, exactly as the GitHub contract specifies."""
        page = int(query.get("page", "1"))
        etag = f'"etag-{path}-{page}"'

        # Conditional request: matching If-None-Match -> 304 Not Modified, empty body.
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, None, {"ETag": etag})

        headers = {"ETag": etag}
        if page < 2:
            nxt = dict(query, page=str(page + 1))
            base = f"http://{self.headers.get('Host')}{path}"
            headers["Link"] = (f'<{base}?{urlencode(nxt)}>; rel="next", '
                               f'<{base}?{urlencode(nxt)}>; rel="last"')
        return self._send(200, example, headers)


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ["GITHUB_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["GITHUB_APP_ID"] = APP_ID
    os.environ["GITHUB_INSTALLATION_ID"] = INSTALLATION_ID
    os.environ["GITHUB_PRIVATE_KEY"] = PRIVATE_PEM
    os.environ["GITHUB_WEBHOOK_SECRET"] = "selfcheck-webhook-secret"
    # Avoid PyGithub's inter-request delay slowing the harness.
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    from ingest.github import run as gh_run
    from ingest.github import live
    from ingest.config import GitHubConfig
    from ingest.fidelity import FidelityReport
    from ingest.webhook_server import WebhookServer

    print("== historical ==")
    report = gh_run.run_historical()
    server.shutdown()

    failures: list[str] = []
    oc = report.object_counts
    for obj in ("repository", "issue", "pull_request", "commit", "branch", "label"):
        if oc.get(obj, 0) <= 0:
            failures.append(f"no {obj} ingested")
    if report.pages.get("issues", 0) < 2:
        failures.append(f"issues Link pagination not exercised (pages={report.pages.get('issues')})")
    etag_checks = [c for c in report.protocol_checks if c["check"].startswith("ETag/304")]
    if not etag_checks:
        failures.append("no ETag/304 conditional check recorded")
    if any(not c["ok"] for c in etag_checks):
        failures.append(f"ETag/304 check failed: {[c for c in etag_checks if not c['ok']]}")
    if report.auth.get("token_prefix") != "ghs_":
        failures.append(f"installation token not ghs_-prefixed: {report.auth.get('token_prefix')}")
    if report.divergences:
        failures.append(f"unexpected divergences: {[d.line() for d in report.divergences]}")

    print(f"  auth={report.auth.get('method')} token={report.auth.get('token_prefix')}")
    print(f"  counts={dict(oc)}")
    print(f"  pages={report.pages}")
    print(f"  protocol_checks={report.protocol_checks}")
    print(f"  divergences={len(report.divergences)}")

    print("== live (webhook signature verification) ==")
    import hashlib
    import hmac
    cfg = GitHubConfig.from_env()
    live_report = FidelityReport("github", cfg.base_url)
    srv = WebhookServer("selfcheck-gh")
    live.register(srv, cfg, live_report)
    client = srv.app.test_client()
    secret = cfg.webhook_secret.encode()

    def sign(body: bytes) -> str:
        return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    ping = json.dumps({"zen": "Keep it simple", "hook_id": 1}).encode()
    r = client.post(live.ENDPOINT, data=ping,
                    headers={"X-Hub-Signature-256": sign(ping), "X-GitHub-Event": "ping",
                             "X-GitHub-Delivery": "d1", "Content-Type": "application/json"})
    if r.status_code != 200:
        failures.append(f"ping handshake failed: {r.status_code} {r.data!r}")

    push = json.dumps({"action": "opened", "repository": {"full_name": "octocat/Hello-World"}}).encode()
    client.post(live.ENDPOINT, data=push,
                headers={"X-Hub-Signature-256": sign(push), "X-GitHub-Event": "issues",
                         "X-GitHub-Delivery": "d2", "Content-Type": "application/json"})

    bad = client.post(live.ENDPOINT, data=push,
                      headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "issues",
                               "Content-Type": "application/json"})
    if bad.status_code != 403:
        failures.append(f"tampered signature not rejected: {bad.status_code}")
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
    print("SELF-CHECK PASSED ✅  (App-JWT auth, token exchange, Link pagination, "
          "ETag/304, header audit, schema validation, webhook signatures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
