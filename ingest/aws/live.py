"""AWS live ingestion — the POLL edge (NO webhook, NO HMAC).

AWS has no inbound signed webhook: its live edge is a **poll**. New CloudTrail
activity is picked up by re-walking ``LookupEvents`` incrementally from the
high-water ``eventTime`` (the reconciler's ``has_events_since`` / warm-start
INCREMENTAL mode) — there is no HTTP callback and no signature to verify. So this
"live" mode is a polling loop, not a webhook listener:

  1. Establish the baseline high-water from the newest LookupEvents page.
  2. For ``run_seconds``, re-poll ``[high_water+1s, end]`` (exclusive floor, the
     reconciler contract) and record any new events.
  3. NEGATIVE: issue a LookupEvents whose SigV4 signature won't verify (signed with
     the wrong secret) and assert the mock rejects it with an AWS error (HTTP 403
     ``SignatureDoesNotMatch``) — the AWS analog of the other sources' webhook
     tamper test, proving the transport is genuinely authenticated.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..fidelity import FidelityReport
from .client import PAGE, AwsClient, ClientError
from .historical import _validate_event, _window


def _walk_since(client: AwsClient, *, start: datetime, end: datetime) -> list[dict]:
    """One full NextToken walk of [start, end] (newest-first)."""
    out: list[dict] = []
    token = None
    for _ in range(200):
        resp = client.lookup_events(start=start, end=end, max_results=PAGE, next_token=token)
        out.extend(resp.get("Events") or [])
        token = resp.get("NextToken")
        if not token:
            break
    return out


def run_live(report: FidelityReport, cfg, run_seconds: float | None) -> None:
    client = AwsClient(cfg, report)
    report.auth.update({"method": "IAM SigV4 via boto3/botocore (endpoint_url override)"})
    account = cfg.account_id or ""
    region = cfg.region
    win_start, end = _window(cfg)

    # 1) Baseline high-water = newest event currently visible.
    head = client.lookup_events(start=win_start, end=end, max_results=PAGE)
    events = head.get("Events") or []
    baseline = max((e["EventTime"] for e in events if isinstance(e.get("EventTime"), datetime)),
                   default=win_start)
    report.note(f"live poll baseline high-water = {baseline.isoformat()}")

    seconds = run_seconds if run_seconds is not None else 12.0
    deadline = time.time() + seconds
    seen: set = set()
    seen_ok: set = set()
    counters: dict = {}
    polls = 0
    while time.time() < deadline:
        inc_start = baseline + timedelta(seconds=1)  # exclusive floor (reconciler contract)
        try:
            fresh = _walk_since(client, start=inc_start, end=end)
        except ClientError as e:
            report.diverge("protocol", "LookupEvents(poll)", f"incremental poll error: {e}")
            break
        polls += 1
        for ev in fresh:
            eid = ev.get("EventId")
            if eid in seen:
                continue
            seen.add(eid)
            report.count("live_event")
            _validate_event(ev, report, account, region, inc_start, end, seen_ok, counters)
            name = ev.get("EventName")
            cat = "alarm_state_change" if _is_alarm(ev) else "management_event"
            report.record_live_event(f"aws.{cat}", f"{name} [{eid}]")
        time.sleep(1.0)

    report.note(f"live poll: {len(seen)} new event(s) over {polls} poll(s)")

    # 2) NEGATIVE — a tampered (bad-secret) SigV4 request must be rejected by the
    #    mock with an AWS 403, proving the transport is genuinely authenticated.
    try:
        client.lookup_events_signed_badly(start=win_start, end=end)
        report.record_protocol("SigV4 signature enforced (tamper -> 403)", False,
                               "a request signed with the WRONG secret was ACCEPTED")
    except ClientError as e:
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = e.response.get("Error", {}).get("Code")
        ok = status == 403 and code in ("SignatureDoesNotMatch", "InvalidClientTokenId",
                                        "InvalidSignatureException")
        report.record_protocol("SigV4 signature enforced (tamper -> 403)", ok,
                               "" if ok else f"unexpected rejection: status={status} code={code}")
        report.record_signature("cloudtrail:LookupEvents (SigV4)", ok,
                                "" if ok else f"tamper not rejected as 403: {status}/{code}")


def _is_alarm(ev: dict[str, Any]) -> bool:
    import json
    cte = ev.get("CloudTrailEvent")
    if not isinstance(cte, str):
        return False
    try:
        rec = json.loads(cte)
    except ValueError:
        return False
    return bool(rec.get("alarmName") or rec.get("newState"))
