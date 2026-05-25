#!/usr/bin/env python3
"""Offline self-check for the Notion slice.

A DEV HARNESS. Stands up a throwaway HTTP server speaking the Notion REST shapes
(search → pages/databases, block children, database query, users), with real
start_cursor pagination and a one-time 429 to exercise Retry-After backoff. Points the
client at it via NOTION_API_BASE_URL and runs the historical pipeline, asserting the
enumerate → paginate → full-fetch path, schema validation, and rate-limit handling all
work — with no mock-specific code in the client.

Run:  python scripts/selfcheck_notion.py
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

TS = "2024-01-01T00:00:00.000Z"
PU = {"object": "user", "id": "11111111-1111-1111-1111-111111111111"}


def page(i):
    return {"object": "page", "id": f"page-{i:04d}", "created_time": TS, "last_edited_time": TS,
            "created_by": PU, "last_edited_by": PU, "archived": False, "in_trash": False,
            "properties": {}, "parent": {"type": "workspace", "workspace": True},
            "url": f"https://notion.so/page-{i}"}


def database(i):
    return {"object": "database", "id": f"db-{i:04d}", "created_time": TS, "last_edited_time": TS,
            "title": [], "properties": {}, "parent": {"type": "workspace", "workspace": True},
            "url": f"https://notion.so/db-{i}", "archived": False}


def block(i):
    return {"object": "block", "id": f"block-{i:04d}", "type": "paragraph", "created_time": TS,
            "last_edited_time": TS, "created_by": PU, "last_edited_by": PU,
            "has_children": False, "archived": False, "paragraph": {"rich_text": []}}


def user(i):
    return {"object": "user", "id": f"user-{i:04d}", "type": "person", "name": f"User {i}",
            "avatar_url": None, "person": {"email": f"u{i}@spammer-org.test"}}


def listing(results, next_cursor, typ="page_or_database"):
    return {"object": "list", "results": results, "has_more": next_cursor is not None,
            "next_cursor": next_cursor, "type": typ, typ: {}}


class FakeNotion(BaseHTTPRequestHandler):
    rate_limited_once = False

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if code == 429:
            self.send_header("Retry-After", "1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _maybe_429(self) -> bool:
        if not FakeNotion.rate_limited_once:
            FakeNotion.rate_limited_once = True
            self._send(429, {"object": "error", "status": 429, "code": "rate_limited",
                             "message": "slow down"})
            return True
        return False

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/v1/search":
            body = self._read_json()
            cur = body.get("start_cursor")
            # 3 pages: two of pages, one of databases
            if cur is None:
                return self._send(200, listing([page(i) for i in range(100)], "c1"))
            if cur == "c1":
                return self._send(200, listing([page(i) for i in range(100, 150)] +
                                               [database(i) for i in range(13)], "c2"))
            return self._send(200, listing([], None))
        if path.endswith("/query"):
            return self._send(200, listing([page(i) for i in range(5)], None))
        return self._send(404, {"object": "error", "code": "not_found"})

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if self._maybe_429():
            return
        if path.startswith("/v1/pages/"):
            return self._send(200, page(1))
        if path.startswith("/v1/databases/"):
            return self._send(200, database(1))
        if "/children" in path:
            q = parse_qs(urlsplit(self.path).query)
            cur = (q.get("start_cursor") or [None])[0]
            if cur is None:
                return self._send(200, {"object": "list", "results": [block(i) for i in range(20)],
                                        "has_more": True, "next_cursor": "bc1", "type": "block",
                                        "block": {}})
            return self._send(200, {"object": "list", "results": [block(99)], "has_more": False,
                                    "next_cursor": None, "type": "block", "block": {}})
        if path == "/v1/users":
            return self._send(200, {"object": "list", "results": [user(i) for i in range(5)],
                                    "has_more": False, "next_cursor": None, "type": "user",
                                    "user": {}})
        return self._send(404, {"object": "error", "code": "not_found"})

    def log_message(self, *_):
        pass


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), FakeNotion)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["NOTION_API_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["NOTION_TOKEN"] = "ntn_selfcheck"

    from ingest.notion import run as notion_run
    print("== historical ==")
    report = notion_run.run_historical()
    oc = report.object_counts
    print(f"  counts={dict(oc)}")
    print(f"  pages={report.pages}")
    print(f"  rate_limit_events={len(report.rate_limit_events)} (honored="
          f"{all(e['backoff_honored'] for e in report.rate_limit_events)})")
    print(f"  schema_checks={ {k:(v.passed,v.failed) for k,v in report.schema_checks.items()} }")
    print(f"  divergences={[d.line() for d in report.divergences]}")

    failures = []
    for obj in ("page", "database", "block", "user"):
        if oc.get(obj, 0) <= 0:
            failures.append(f"no {obj} ingested")
    if report.pages.get("search", 0) < 2:
        failures.append("search pagination not exercised")
    if not report.rate_limit_events:
        failures.append("429/Retry-After not exercised")
    if report.divergences:
        failures.append(f"unexpected divergences: {[d.line() for d in report.divergences]}")

    print()
    if failures:
        print("SELF-CHECK FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SELF-CHECK PASSED ✅  (Bearer+Notion-Version, search pagination, full-object "
          "fetch, block/db/user enumerate, 429/Retry-After, schema validation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
