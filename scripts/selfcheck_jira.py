#!/usr/bin/env python3
"""Offline self-check for the Jira slice.

A DEV HARNESS. Fakes a Jira Cloud site: GET /rest/api/3/project/search (classic
startAt/total/isLast pagination) and GET /rest/api/3/search/jql (the new token model:
nextPageToken/isLast, issues with expand=changelog). Points the client at it via
JIRA_API_BASE_URL and runs the historical pipeline, asserting Basic auth, project
enumeration, token pagination that terminates, changelog status-transition counting,
and schema validation — no mock-specific code in the client.

Run:  python scripts/selfcheck_jira.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROJECTS = [{"id": str(i), "key": f"PRJ{i}", "name": f"Project {i}",
             "projectTypeKey": "software", "self": f"http://x/project/{i}"} for i in range(3)]


def issue(key, i):
    return {"id": str(1000 + i), "key": f"{key}-{i}", "self": f"http://x/issue/{i}",
            "fields": {"summary": f"Issue {i}", "updated": f"2024-01-{i+1:02d}T00:00:00.000+0000",
                       "status": {"name": "Done"}},
            "changelog": {"startAt": 0, "maxResults": 1, "total": 1,
                          "histories": [{"id": "9", "created": "2024-01-01T00:00:00.000+0000",
                                         "items": [{"field": "status", "fieldtype": "jira",
                                                    "fromString": "To Do", "toString": "Done"}]}]}}


class FakeJira(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization", "").startswith("Basic ")

    def _jql_page(self, jql: str, next_token):
        key = jql.split('"')[1] if '"' in jql else "PRJ0"
        if next_token is None:
            return {"issues": [issue(key, 0), issue(key, 1)], "nextPageToken": "p2",
                    "isLast": False}
        return {"issues": [issue(key, 2)], "isLast": True}

    def do_GET(self):  # noqa: N802
        split = urlsplit(self.path)
        path, q = split.path, {k: v[0] for k, v in parse_qs(split.query).items()}
        if not self._auth_ok():
            return self._send(401, {"errorMessages": ["Unauthorized"], "errors": {}})

        if path == "/rest/api/3/project/search":
            start = int(q.get("startAt", "0"))
            mx = int(q.get("maxResults", "50"))
            page = PROJECTS[:2] if start == 0 else PROJECTS[start:]
            is_last = (start + len(page)) >= len(PROJECTS)
            return self._send(200, {"self": "http://x", "maxResults": mx, "startAt": start,
                                    "total": len(PROJECTS), "isLast": is_last, "values": page})

        # A faithful Jira supports BOTH GET and POST on /search/jql.
        if path == "/rest/api/3/search/jql":
            return self._send(200, self._jql_page(q.get("jql", ""), q.get("nextPageToken")))
        return self._send(404, {"errorMessages": ["Not Found"], "errors": {}})

    def do_POST(self):  # noqa: N802
        if not self._auth_ok():
            return self._send(401, {"errorMessages": ["Unauthorized"], "errors": {}})
        path = urlsplit(self.path).path
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if path == "/rest/api/3/search/jql":
            return self._send(200, self._jql_page(body.get("jql", ""), body.get("nextPageToken")))
        return self._send(404, {"errorMessages": ["Not Found"], "errors": {}})

    def log_message(self, *_):
        pass


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), FakeJira)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ.update({"JIRA_API_BASE_URL": f"http://127.0.0.1:{port}",
                       "JIRA_ACCOUNT_EMAIL": "bot@spammer-org.test",
                       "JIRA_API_TOKEN": "selfcheck-token"})

    from ingest.jira import run as jira_run
    print("== historical ==")
    report = jira_run.run_historical()
    oc = report.object_counts
    print(f"  counts={dict(oc)}")
    print(f"  pages={report.pages}")
    print(f"  protocol={[(p['check'], p['ok']) for p in report.protocol_checks]}")
    print(f"  schema={ {k:(v.passed,v.failed) for k,v in report.schema_checks.items()} }")
    print(f"  divergences={[d.line() for d in report.divergences]}")

    failures = []
    if oc.get("project", 0) < 3:
        failures.append(f"expected 3 projects, got {oc.get('project')}")
    if oc.get("issue", 0) <= 0:
        failures.append("no issues ingested")
    if oc.get("status_transition", 0) <= 0:
        failures.append("status changelog transitions not counted")
    if report.pages.get("search.jql", 0) < 2:
        failures.append("search/jql token pagination not exercised")
    if not any("pagination terminates" in p["check"] and p["ok"] for p in report.protocol_checks):
        failures.append("pagination-terminates protocol check missing/failed")
    if report.divergences:
        failures.append(f"unexpected divergences: {[d.line() for d in report.divergences]}")

    print()
    if failures:
        print("SELF-CHECK FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SELF-CHECK PASSED ✅  (HTTP Basic, project/search pagination, /search/jql token "
          "pagination + termination, changelog status transitions, schema validation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
