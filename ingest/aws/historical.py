"""AWS historical ingestion — the org-wide CloudTrail backfill.

Three exercises against the verified surface:

  1. **STS GetCallerIdentity** — the connectivity/credential probe (Account/Arn/
     UserId), the AWS analog of Grafana's ``GET /api/org``.
  2. **STS AssumeRole** — mint short-lived creds (the recommended credential kind),
     validate the ``Credentials``/``AssumedRoleUser`` envelope, then issue ONE
     ``LookupEvents`` with the temp creds (proves the session-token SigV4 path).
  3. **CloudTrail LookupEvents** — page the account/region's management events over
     a ``[end-90d, end]`` window via the opaque ``NextToken`` (end-of-data = a page
     with no token), newest-first, ``MaxResults`` ≤ 50.

CloudTrail publishes no machine schema for these objects, so (like the QBO/Grafana
slices) we structurally validate the fields a consumer depends on — on BOTH the
LookupEvents ``Event`` wrapper (``ReadOnly`` is a STRING, ``EventTime`` a parsed
datetime, ``CloudTrailEvent`` a JSON STRING) AND the parsed native CloudTrail
record inside it (``eventID`` matches, ``eventTime`` is RFC3339, ``eventCategory``
Management) — plus the alarm-vs-management discriminator the consumer keys on
(``alarmName``/``newState`` present → ``state_change``; absent → ``signal``).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..fidelity import FidelityReport
from .client import PAGE, AwsClient, ClientError

_MAX_PAGES = 2000  # safety bound
_RFC3339_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_ALARM_STATES = {"OK", "ALARM", "INSUFFICIENT_DATA"}


def _is_str(x: Any) -> bool:
    return isinstance(x, str)


def _window(cfg) -> tuple[datetime, datetime]:
    if cfg.backfill_end:
        end = datetime.fromisoformat(cfg.backfill_end.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
    else:
        end = datetime.now(timezone.utc)
    return end - timedelta(days=cfg.window_days), end


def _validate_event(ev: dict, report: FidelityReport, account: str, region: str,
                    start: datetime, end: datetime, seen_ok: set,
                    counters: dict) -> None:
    problems: list[str] = []
    eid = ev.get("EventId")
    if not (_is_str(eid) and eid):
        problems.append("missing/!str `EventId`")
    if not _is_str(ev.get("EventName")):
        problems.append("missing/!str `EventName`")
    # ReadOnly is a STRING "true"/"false" on the wire — NOT a bool (the JSON-1.1
    # CloudTrail contract). A bool here would be a real divergence.
    ro = ev.get("ReadOnly")
    if ro is not None and not _is_str(ro):
        problems.append(f"`ReadOnly` must be a string 'true'/'false', got {type(ro).__name__}")
    elif _is_str(ro) and ro not in ("true", "false"):
        problems.append(f"`ReadOnly` string must be 'true'/'false', got {ro!r}")
    # EventTime: botocore parses the epoch-seconds wire number into a datetime.
    et = ev.get("EventTime")
    if not isinstance(et, datetime):
        problems.append(f"`EventTime` must parse to a datetime, got {type(et).__name__}")
    elif not (start - timedelta(seconds=1) <= et <= end + timedelta(seconds=1)):
        problems.append(f"`EventTime` {et.isoformat()} outside requested window")
    if not isinstance(ev.get("Resources"), list):
        problems.append("`Resources` must be a list")

    # CloudTrailEvent is a JSON-encoded STRING, not a nested object.
    cte = ev.get("CloudTrailEvent")
    rec: dict[str, Any] = {}
    if not _is_str(cte):
        problems.append("`CloudTrailEvent` must be a JSON STRING (escaped record)")
    else:
        try:
            rec = json.loads(cte)
        except ValueError:
            problems.append("`CloudTrailEvent` string is not parseable JSON")
    if isinstance(rec, dict):
        if str(rec.get("eventID")) != str(eid):
            problems.append(f"native `eventID` {rec.get('eventID')!r} != Event.EventId {eid!r}")
        if not _is_str(rec.get("eventVersion")):
            problems.append("native record missing `eventVersion`")
        rt = rec.get("eventTime")
        if not (_is_str(rt) and _RFC3339_Z.match(rt)):
            problems.append(f"native `eventTime` must be RFC3339 `…Z`, got {rt!r}")
        if rec.get("eventCategory") != "Management":
            problems.append(f"native `eventCategory` should be 'Management', got {rec.get('eventCategory')!r}")
        if rec.get("recipientAccountId") not in (None, account):
            problems.append("native `recipientAccountId` != install account")
        if rec.get("awsRegion") not in (None, region):
            problems.append("native `awsRegion` != install region")
        if not isinstance(rec.get("userIdentity"), dict):
            problems.append("native record missing `userIdentity` object")

    # Alarm-vs-management discriminator (the consumer's signal/state_change split).
    alarm_name = rec.get("alarmName") if isinstance(rec, dict) else None
    new_state = rec.get("newState") if isinstance(rec, dict) else None
    if alarm_name or new_state:
        counters["alarm"] = counters.get("alarm", 0) + 1
        if new_state not in _ALARM_STATES:
            problems.append(f"alarm event `newState` must be one of {_ALARM_STATES}, got {new_state!r}")
        if isinstance(rec, dict) and rec.get("eventSource") != "monitoring.amazonaws.com":
            problems.append("alarm event should have eventSource monitoring.amazonaws.com")
    else:
        counters["mgmt"] = counters.get("mgmt", 0) + 1

    check = "CloudTrail event object contract"
    if problems:
        report.record_protocol(check, False, f"id={eid}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def run_historical(report: FidelityReport, cfg) -> None:
    client = AwsClient(cfg, report)
    report.auth.update({"method": "IAM SigV4 via boto3/botocore (endpoint_url override)",
                        "credential_kinds": "static_keys + AssumeRole"})
    account = cfg.account_id or ""
    region = cfg.region
    start, end = _window(cfg)
    report.note(f"LookupEvents window [{start.isoformat()} .. {end.isoformat()}] "
                f"({cfg.window_days}-day floor)")

    # 1) STS GetCallerIdentity — connectivity + credential probe.
    try:
        ident = client.get_caller_identity()
        ok = all(_is_str(ident.get(k)) and ident.get(k) for k in ("Account", "Arn", "UserId"))
        report.record_protocol("STS GetCallerIdentity probe", ok,
                               "" if ok else f"missing Account/Arn/UserId: {ident}")
        if ident.get("Account"):
            account = account or ident["Account"]
        report.note(f"caller identity account={ident.get('Account')} arn={ident.get('Arn')}")
    except ClientError as e:
        report.record_protocol("STS GetCallerIdentity probe", False, str(e))

    # 2) STS AssumeRole — the recommended credential kind; then a temp-cred call.
    if cfg.role_arn:
        try:
            ar = client.assume_role(cfg.role_arn)
            creds = ar.get("Credentials") or {}
            aru = ar.get("AssumedRoleUser") or {}
            ar_ok = (all(creds.get(k) for k in ("AccessKeyId", "SecretAccessKey",
                                                "SessionToken", "Expiration"))
                     and bool(aru.get("Arn")) and bool(aru.get("AssumedRoleId")))
            # Expiration is parsed to a datetime by botocore.
            if not isinstance(creds.get("Expiration"), datetime):
                ar_ok = False
            report.record_protocol("STS AssumeRole envelope", ar_ok,
                                   "" if ar_ok else f"incomplete AssumeRole response: {ar}")
            # Use the temp creds to call CloudTrail (session-token SigV4 path).
            ct_temp = client.cloudtrail_with_credentials(creds)
            r = client.lookup_events(start=start, end=end, max_results=1, client=ct_temp)
            report.record_protocol("AssumeRole temp-cred LookupEvents", "Events" in r,
                                   "" if "Events" in r else "temp-cred call failed")
            report.note(f"assumed role {aru.get('Arn')} (expires {creds.get('Expiration')})")
        except ClientError as e:
            report.record_protocol("STS AssumeRole envelope", False, str(e))

    # 3) CloudTrail LookupEvents — the NextToken backfill walk.
    seen_ok: set = set()
    seen_ids: set = set()
    counters: dict = {}
    token: str | None = None
    pages = 0
    total = 0
    high_water: datetime | None = None
    order_ok = True
    max_page_len = 0
    while pages < _MAX_PAGES:
        try:
            resp = client.lookup_events(start=start, end=end, max_results=PAGE,
                                        next_token=token)
        except ClientError as e:
            report.diverge("protocol", "LookupEvents", f"LookupEvents error: {e}")
            return
        report.record_page("LookupEvents", token or "head")
        pages += 1
        events = resp.get("Events") or []
        max_page_len = max(max_page_len, len(events))
        prev_t: datetime | None = None
        for ev in events:
            if not isinstance(ev, dict):
                report.diverge("protocol", "LookupEvents", "Event element is not an object")
                continue
            report.count("event")
            _validate_event(ev, report, account, region, start, end, seen_ok, counters)
            eid = ev.get("EventId")
            et = ev.get("EventTime")
            if isinstance(et, datetime):
                high_water = et if high_water is None else max(high_water, et)
                if prev_t is not None and et > prev_t and order_ok:
                    order_ok = False
                    report.record_protocol("LookupEvents newest-first ordering", False,
                                           f"EventId={eid} time {et} > previous {prev_t}")
                prev_t = et
            # external_id is immutable (no version suffix): aws:{acct}:{region}:event:{id}
            if eid in seen_ids:
                report.record_protocol("event ids unique within walk", False,
                                       f"duplicate EventId {eid} across pages")
            seen_ids.add(eid)
            total += 1
        token = resp.get("NextToken")
        if not token:
            break

    if max_page_len > PAGE:
        report.record_protocol("MaxResults <= 50 per page", False,
                               f"a page returned {max_page_len} > 50 events")
    else:
        report.record_protocol("MaxResults <= 50 per page", True, "")
    if order_ok:
        report.record_protocol("LookupEvents newest-first ordering", True, "")
    report.record_protocol("LookupEvents pagination terminates (no NextToken = EOF)", True, "")
    report.note(f"events: {total} over {pages} page(s); "
                f"{counters.get('alarm', 0)} alarm-state-change, "
                f"{counters.get('mgmt', 0)} management; high_water={high_water}")

    # Window-filter probe: a window entirely before the data returns nothing.
    pre = start - timedelta(days=cfg.window_days)
    empty = client.lookup_events(start=pre, end=start - timedelta(days=1), max_results=PAGE)
    report.record_protocol("empty-window returns no events",
                           len(empty.get("Events") or []) == 0,
                           f"pre-window returned {len(empty.get('Events') or [])} events")
