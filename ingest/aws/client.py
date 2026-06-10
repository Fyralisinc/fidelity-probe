"""AWS access via REAL boto3/botocore — the genuine SigV4 transport.

This is the ONE source that does not hand-roll HTTP: botocore owns SigV4 signing,
endpoint resolution, the CloudTrail JSON 1.1 + STS Query protocols, and the
``ClientError`` taxonomy. We point it at the mock purely through ``endpoint_url``
(``AWS_API_BASE_URL``) — the same ``endpoint_override`` seam a real
moto/localstack integration test uses — so the bytes on the wire are exactly what
real AWS would receive. If this slice passes against the mock, a boto3 consumer
works against real AWS.

Surface (the verified Fyralis read set, nothing more):
  * ``sts:GetCallerIdentity`` — zero-permission connectivity/credential probe.
  * ``sts:AssumeRole`` — mint short-lived creds (the recommended credential kind).
  * ``cloudtrail:LookupEvents`` — management events in a time window, opaque
    ``NextToken``, ``MaxResults`` ≤ 50, newest-first.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from ..config import AwsConfig
from ..fidelity import FidelityReport

# botocore caps MaxResults at the CloudTrail model's max (50). We page at the max.
PAGE = 50
_RETRY = BotoConfig(retries={"max_attempts": 4, "mode": "standard"})


class AwsClient:
    def __init__(self, cfg: AwsConfig, report: FidelityReport):
        ak, sk = cfg.require_auth()
        self.cfg = cfg
        self.report = report
        self._session = boto3.session.Session(
            aws_access_key_id=ak, aws_secret_access_key=sk, region_name=cfg.region)
        kw: dict[str, Any] = {"config": _RETRY}
        if cfg.base_url:
            kw["endpoint_url"] = cfg.base_url
        self._sts = self._session.client("sts", **kw)
        self._ct = self._session.client("cloudtrail", **kw)
        self._kw = kw

    # ---- STS -------------------------------------------------------------

    def get_caller_identity(self) -> dict[str, Any]:
        return self._sts.get_caller_identity()

    def assume_role(self, role_arn: str, *, session_name: str = "fyralis-ingest",
                    external_id: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"RoleArn": role_arn, "RoleSessionName": session_name}
        if external_id:
            kwargs["ExternalId"] = external_id
        return self._sts.assume_role(**kwargs)

    def cloudtrail_with_credentials(self, creds: dict[str, Any]):
        """A CloudTrail client signed with temp creds (the AssumeRole path —
        exercises x-amz-security-token signing + the mock's session-token verify)."""
        return self._session.client(
            "cloudtrail",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            **self._kw,
        )

    # ---- CloudTrail ------------------------------------------------------

    def lookup_events(self, *, start: datetime, end: datetime, max_results: int = PAGE,
                      next_token: str | None = None, client=None) -> dict[str, Any]:
        ct = client or self._ct
        kwargs: dict[str, Any] = {
            "StartTime": start, "EndTime": end,
            "MaxResults": min(max_results, PAGE),
        }
        if next_token:
            kwargs["NextToken"] = next_token
        return ct.lookup_events(**kwargs)

    def lookup_events_signed_badly(self, *, start: datetime, end: datetime) -> None:
        """Issue a LookupEvents whose SigV4 signature WON'T verify (signed with the
        wrong secret), to prove the mock genuinely rejects a tampered request with
        an AWS error. Raises botocore ``ClientError``."""
        bad = self._session.client(
            "cloudtrail",
            aws_access_key_id=self.cfg.access_key_id,
            aws_secret_access_key="wrong-secret-the-mock-will-not-match",
            region_name=self.cfg.region, **self._kw,
        )
        bad.lookup_events(StartTime=start, EndTime=end, MaxResults=1)


__all__ = ["AwsClient", "ClientError", "PAGE"]
