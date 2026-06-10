"""AWS slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import AwsConfig
from ..fidelity import FidelityReport
from . import historical, live


def _new_report(cfg: AwsConfig) -> FidelityReport:
    report = FidelityReport("aws", cfg.base_url or "(real AWS endpoints)")
    report.note("AWS (CloudTrail). The ONLY source that speaks the genuine AWS wire "
                "protocols via real boto3/botocore — CloudTrail JSON 1.1 (LookupEvents, "
                "opaque NextToken, MaxResults<=50, newest-first) + STS Query "
                "(GetCallerIdentity/AssumeRole, XML), all SigV4-signed. The mock is "
                "reached purely through boto3's endpoint_url (the moto/localstack "
                "endpoint_override seam). One account/region shard; immutable external_id "
                "aws:{account}:{region}:event:{eventId}; the live edge is a POLL (no "
                "webhook/HMAC).")
    return report


def run_historical() -> FidelityReport:
    cfg = AwsConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = AwsConfig.from_env()
    report = _new_report(cfg)
    report.note("AWS live = the POLL edge (no inbound webhook, no HMAC): new "
                "CloudTrail activity is picked up by re-walking LookupEvents "
                "incrementally from high-water+1s (the reconciler contract). A "
                "tampered (wrong-secret) SigV4 request is asserted to be rejected 403.")
    live.run_live(report, cfg, run_seconds)
    return report
