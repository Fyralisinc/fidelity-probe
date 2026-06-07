"""GitHub live ingestion — webhook delivery with X-Hub-Signature-256 verification.

Production-faithful: GitHub signs each webhook delivery with HMAC-SHA256 over the
raw request body, keyed by the App's webhook secret, and sends it as
`X-Hub-Signature-256: sha256=<hex>`. We recompute it over the *raw* bytes and
compare in constant time *before* trusting the payload; the event type comes from
the `X-GitHub-Event` header and the delivery id from `X-GitHub-Delivery`.

Schema note: the api.github.com OpenAPI description does not model webhook *event*
payloads (GitHub publishes those in a separate webhooks spec), so — exactly as on
the Slack live slice — events are verified and structurally summarized but not
schema-validated here. Instead we assert the *fields a consumer actually depends
on* per event type (the dedup key, the timestamps, the blast-radius file lists,
the lifecycle action), recording any that are missing/malformed as a divergence.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from flask import Response, request

from ..config import GitHubConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer

ENDPOINT = "/github/events"

_SHA_RE_LEN = 40  # full git object id length


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of X-Hub-Signature-256 (`sha256=<hexdigest>`)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _looks_like_sha(v: Any) -> bool:
    return isinstance(v, str) and len(v) == _SHA_RE_LEN and all(c in "0123456789abcdef" for c in v)


def _missing(body: dict, *paths: str) -> list[str]:
    """Return the dotted paths that are absent/empty in ``body``."""
    out: list[str] = []
    for path in paths:
        cur: Any = body
        ok = True
        for seg in path.split("."):
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                ok = False
                break
        if not ok or cur is None or cur == "":
            out.append(path)
    return out


def _validate_event(event: str, body: dict, report: FidelityReport, seen_ok: set) -> None:
    """Assert the consumer-critical fields per event type; diverge on anything missing.

    Records one "ok" protocol line per event type (so a burst of pushes doesn't
    repeat it) but always records a failure.
    """
    problems: list[str] = []

    if event == "push":
        problems += _missing(body, "ref", "after", "head_commit", "head_commit.timestamp", "repository.full_name")
        if not _looks_like_sha(body.get("after")):
            problems.append("after is not a 40-hex sha")
        if not isinstance(body.get("ref"), str) or not body["ref"].startswith("refs/"):
            problems.append("ref is not a refs/ ref")
        commits = body.get("commits")
        if not isinstance(commits, list) or not commits:
            problems.append("commits[] missing/empty")
        else:
            c0 = commits[0]
            # The blast-radius layer keys on these per-commit path lists.
            for k in ("added", "removed", "modified"):
                if not isinstance(c0.get(k), list):
                    problems.append(f"commits[0].{k} is not a list")
            if not (c0.get("added") or c0.get("removed") or c0.get("modified")):
                problems.append("commits[0] carries no changed-file paths")

    elif event == "pull_request":
        problems += _missing(body, "action", "pull_request.node_id", "pull_request.state", "number")
        pr = body.get("pull_request") or {}
        if body.get("action") == "closed" and pr.get("merged") and not pr.get("merged_at"):
            problems.append("merged pull_request has no merged_at")

    elif event == "issues":
        problems += _missing(body, "action", "issue.node_id", "issue.state", "issue.number")

    elif event == "pull_request_review":
        problems += _missing(body, "action", "review.node_id", "review.state", "pull_request.number")

    elif event == "issue_comment":
        problems += _missing(body, "action", "comment.node_id", "comment.issue_url", "comment.body")

    elif event == "check_run":
        problems += _missing(body, "action", "check_run.node_id", "check_run.status", "check_run.head_sha")

    elif event == "installation":
        problems += _missing(body, "action", "installation.id")

    elif event == "installation_repositories":
        problems += _missing(body, "action", "installation.id")
        if not isinstance(body.get("repositories_added"), list) or \
           not isinstance(body.get("repositories_removed"), list):
            problems.append("repositories_added/removed not both lists")

    elif event == "ping":
        problems += _missing(body, "zen", "hook_id", "hook")

    else:
        report.note(f"live {event}: no structural contract asserted (recorded only)")
        return

    check = f"live {event} payload contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def register(server: WebhookServer, cfg: GitHubConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def github_events():  # noqa: ANN202
        raw = request.get_data()  # raw bytes — required for a correct HMAC
        sig = request.headers.get("X-Hub-Signature-256")
        valid = verify_signature(secret, raw, sig)
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "X-Hub-Signature-256 mismatch / missing")
        if not valid:
            return Response("invalid signature", status=403)

        # GitHub sends both the legacy SHA-1 and the SHA-256 signature; assert the
        # SHA-1 is present and well-formed (we trust SHA-256, but its absence is a
        # protocol gap a stricter consumer would reject on).
        sha1 = request.headers.get("X-Hub-Signature")
        if not (sha1 and sha1.startswith("sha1=")):
            report.record_protocol("X-Hub-Signature (SHA-1) header", False,
                                   "absent/malformed; GitHub sends it alongside SHA-256")
        elif "sha1-header" not in seen_ok:
            seen_ok.add("sha1-header")
            report.record_protocol("X-Hub-Signature (SHA-1) header", True, "")

        # Delivery headers a consumer relies on.
        for hdr in ("X-GitHub-Delivery", "X-GitHub-Hook-ID"):
            if not request.headers.get(hdr) and f"hdr:{hdr}" not in seen_ok:
                seen_ok.add(f"hdr:{hdr}")
                report.record_protocol(f"{hdr} header", False, "absent on delivery")

        event = request.headers.get("X-GitHub-Event", "unknown")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        body = json.loads(raw or b"{}")

        if event == "ping":
            _validate_event("ping", body, report, seen_ok)
            report.record_live_event("ping", f"zen={body.get('zen')!r} hook_id={body.get('hook_id')}")
            return Response(json.dumps({"ok": True}), mimetype="application/json")

        _validate_event(event, body, report, seen_ok)

        action = body.get("action")
        repo = (body.get("repository") or {}).get("full_name")
        summary = f"action={action} repo={repo} delivery={delivery[:8]}"
        report.record_live_event(event, summary)
        report.count(f"event:{event}", 1)
        return Response("", status=200)
