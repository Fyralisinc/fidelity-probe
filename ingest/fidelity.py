"""Fidelity report: a structured record of everything observed during a run.

This is the whole point of the client. As we authenticate, paginate, hit rate
limits, receive live events, and validate payloads against the official spec, we
record each observation here. At the end we render a human report (Markdown) and a
machine report (JSON), and the process exits non-zero if *any* divergence was seen
— so the run both ingests everything and tells you every place the target deviated
from the real API contract.
"""
from __future__ import annotations

import dataclasses
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Divergence:
    """A single observed deviation from the official spec / expected protocol."""

    category: str  # "schema" | "signature" | "pagination" | "rate_limit" | "protocol"
    where: str  # operation / object type / endpoint
    detail: str

    def line(self) -> str:
        return f"[{self.category}] {self.where}: {self.detail}"


@dataclass
class SchemaCheck:
    label: str  # object type or operation validated
    count: int = 0
    passed: int = 0
    failed: int = 0
    first_error: str | None = None
    # Fields present in the payload but absent from the official spec. For Slack these
    # are expected (its spec is incomplete) and informational; recorded for transparency.
    undocumented_fields: list[str] = field(default_factory=list)


@dataclass
class FidelityReport:
    provider: str
    base_url: str
    started_at: float = field(default_factory=time.time)

    auth: dict[str, Any] = field(default_factory=dict)
    object_counts: Counter = field(default_factory=Counter)
    pages: dict[str, int] = field(default_factory=dict)  # operation -> pages traversed
    cursors_seen: list[str] = field(default_factory=list)
    rate_limit_events: list[dict[str, Any]] = field(default_factory=list)
    live_events: list[dict[str, Any]] = field(default_factory=list)
    signature_checks: list[dict[str, Any]] = field(default_factory=list)
    schema_checks: dict[str, SchemaCheck] = field(default_factory=dict)
    divergences: list[Divergence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ---- recording helpers -------------------------------------------------

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def diverge(self, category: str, where: str, detail: str) -> None:
        self.divergences.append(Divergence(category, where, detail))

    def count(self, obj_type: str, n: int = 1) -> None:
        self.object_counts[obj_type] += n

    def record_page(self, operation: str, cursor: str | None) -> None:
        self.pages[operation] = self.pages.get(operation, 0) + 1
        if cursor:
            self.cursors_seen.append(f"{operation}:{cursor[:24]}")

    def record_rate_limit(self, operation: str, status: int, retry_after: float | None,
                          honored: bool) -> None:
        self.rate_limit_events.append({
            "operation": operation, "status": status,
            "retry_after": retry_after, "backoff_honored": honored,
        })
        if not honored:
            self.diverge("rate_limit", operation,
                         f"status {status} but no usable Retry-After / backoff signal")

    def record_signature(self, endpoint: str, valid: bool, detail: str = "") -> None:
        self.signature_checks.append({"endpoint": endpoint, "valid": valid, "detail": detail})
        if not valid:
            self.diverge("signature", endpoint, detail or "signature verification failed")

    def record_live_event(self, kind: str, summary: str) -> None:
        self.live_events.append({"kind": kind, "summary": summary, "at": time.time()})

    def record_schema(self, label: str, errors: list[str],
                      undocumented: list[str] | None = None,
                      authoritative: bool = True) -> None:
        chk = self.schema_checks.setdefault(label, SchemaCheck(label=label))
        chk.count += 1
        for f in undocumented or []:
            if f not in chk.undocumented_fields:
                chk.undocumented_fields.append(f)
        if errors:
            chk.failed += 1
            if chk.first_error is None:
                chk.first_error = errors[0]
            # Only count as a real divergence when the spec is authoritative for this
            # label (its own example upholds the schema). Otherwise it's a spec bug, not
            # a target bug — record it but don't fail the run on it.
            if chk.failed == 1:
                if authoritative:
                    self.diverge("schema", label, errors[0])
                else:
                    self.note(f"`{label}` failed validation but the official spec is "
                              f"self-inconsistent here (its own example also fails); "
                              f"not counted as a divergence. First error: {errors[0]}")
        else:
            chk.passed += 1

    # ---- output ------------------------------------------------------------

    @property
    def ok(self) -> bool:
        return not self.divergences

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "started_at": self.started_at,
            "duration_sec": round(time.time() - self.started_at, 2),
            "ok": self.ok,
            "auth": self.auth,
            "object_counts": dict(self.object_counts),
            "pages": self.pages,
            "cursors_seen": self.cursors_seen,
            "rate_limit_events": self.rate_limit_events,
            "live_events": self.live_events,
            "signature_checks": self.signature_checks,
            "schema_checks": {k: dataclasses.asdict(v) for k, v in self.schema_checks.items()},
            "divergences": [dataclasses.asdict(d) for d in self.divergences],
            "notes": self.notes,
        }

    def to_markdown(self) -> str:
        d = self.to_dict()
        out: list[str] = []
        verdict = "✅ NO DIVERGENCES" if self.ok else f"❌ {len(self.divergences)} DIVERGENCE(S)"
        out.append(f"# Fidelity report — {self.provider}")
        out.append("")
        out.append(f"- **Target base URL:** `{self.base_url}`")
        out.append(f"- **Duration:** {d['duration_sec']}s")
        out.append(f"- **Verdict:** {verdict}")
        out.append("")
        if self.auth:
            out.append("## Auth")
            for k, v in self.auth.items():
                out.append(f"- {k}: `{v}`")
            out.append("")
        if self.object_counts:
            out.append("## Objects ingested")
            for k, v in sorted(self.object_counts.items()):
                out.append(f"- {k}: {v}")
            out.append("")
        if self.pages:
            out.append("## Pagination")
            for op, n in sorted(self.pages.items()):
                out.append(f"- {op}: {n} page(s)")
            out.append(f"- distinct cursors observed: {len(self.cursors_seen)}")
            out.append("")
        if self.rate_limit_events:
            out.append("## Rate limiting")
            for e in self.rate_limit_events:
                out.append(f"- {e['operation']}: status {e['status']}, "
                           f"retry_after={e['retry_after']}, honored={e['backoff_honored']}")
            out.append("")
        if self.signature_checks:
            out.append("## Signature verification")
            for e in self.signature_checks:
                mark = "ok" if e["valid"] else "FAIL"
                out.append(f"- {e['endpoint']}: {mark} {e['detail']}".rstrip())
            out.append("")
        if self.live_events:
            out.append("## Live events")
            for e in self.live_events:
                out.append(f"- {e['kind']}: {e['summary']}")
            out.append("")
        if self.schema_checks:
            out.append("## Schema validation (vs official spec)")
            for chk in self.schema_checks.values():
                line = f"- {chk.label}: {chk.passed}/{chk.count} passed"
                if chk.failed:
                    line += f" — {chk.failed} failed; first error: {chk.first_error}"
                out.append(line)
                if chk.undocumented_fields:
                    out.append(f"    - undocumented fields observed: "
                               f"{', '.join(sorted(chk.undocumented_fields))}")
            out.append("")
        if self.divergences:
            out.append("## Divergences")
            for dv in self.divergences:
                out.append(f"- {dv.line()}")
            out.append("")
        if self.notes:
            out.append("## Notes")
            for n in self.notes:
                out.append(f"- {n}")
            out.append("")
        return "\n".join(out)

    def write(self, out_dir: str | Path = "reports") -> tuple[Path, Path]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        json_path = out / f"{self.provider}.report.json"
        md_path = out / f"{self.provider}.report.md"
        json_path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        md_path.write_text(self.to_markdown())
        return json_path, md_path
