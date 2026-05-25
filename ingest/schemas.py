"""Strict response-schema validation against each provider's official spec.

Three dialects are in play, so we pick the right validator per spec:
  - Slack   -> Swagger 2.0  -> jsonschema Draft4Validator (refs: #/definitions/*)
  - GitHub  -> OpenAPI 3.0  -> OAS30Validator             (refs: #/components/schemas/*)
  - Discord -> OpenAPI 3.1  -> OAS31Validator             (refs: #/components/schemas/*)

We register the *entire* spec document in a `referencing` registry under a named
URI, and validate every payload through a `$ref` JSON-pointer that points *into*
that document. That keeps the spec doc as the active resource, so the nested
fragment refs the schemas use (`#/definitions/...`, `#/components/schemas/...`)
resolve correctly. Validation never raises into the ingestion loop: errors are
collected onto the FidelityReport.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft4Validator
from jsonschema.exceptions import best_match
from openapi_schema_validator import OAS30Validator, OAS31Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT4, DRAFT202012

from .fidelity import FidelityReport

SPECS_DIR = Path(__file__).resolve().parent.parent / "specs"

_SPEC_FILES = {
    "slack": "slack.openapi.json",
    "github": "github.openapi.json",
    "discord": "discord.openapi.json",
    # Google APIs ship discovery documents (not OpenAPI); handled as a 4th dialect.
    "gmail": "gmail.discovery.json",
    "calendar": "calendar.discovery.json",
    "admin_directory": "admin_directory.discovery.json",
    # Notion: hand-authored from the official API reference (no official OpenAPI exists).
    "notion": "notion.openapi.json",
}

_MAX_ERRORS = 3


def _esc(token: str) -> str:
    """Escape a single JSON Pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _normalize_discovery(node: Any) -> None:
    """In-place: make a Google discovery doc validatable with a JSON-Schema validator.

    Discovery `$ref`s are bare schema names ({"$ref": "Event"}) and it uses the
    non-standard `type: "any"`. Rewrite name refs into `#/schemas/<name>` JSON pointers
    and drop `type: "any"` so a Draft4 validator resolves and runs cleanly.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#") and "/" not in ref:
            node["$ref"] = f"#/schemas/{ref}"
        if node.get("type") == "any":
            node.pop("type")
        for v in node.values():
            _normalize_discovery(v)
    elif isinstance(node, list):
        for v in node:
            _normalize_discovery(v)


class SpecValidator:
    """Loads one provider's official spec and validates payloads against it."""

    def __init__(self, provider: str, lenient_additional_properties: bool | None = None):
        self.provider = provider
        # Slack's official spec marks objects `additionalProperties: false`, yet the
        # real Slack API (and its own examples) return more fields than the spec lists
        # — including response_metadata. So for Slack, extra fields are recorded as
        # informational "undocumented fields", not divergences. GitHub/Discord specs
        # are authoritative, so unexpected fields there ARE genuine findings.
        self.lenient = (provider == "slack") if lenient_additional_properties is None \
            else lenient_additional_properties
        path = SPECS_DIR / _SPEC_FILES[provider]
        if not path.exists():
            raise FileNotFoundError(
                f"missing spec {path}; run `python scripts/fetch_specs.py` first."
            )
        self.doc: dict[str, Any] = json.loads(path.read_text())
        self.doc_uri = f"urn:spec:{provider}"

        version = self.doc.get("openapi") or self.doc.get("swagger") or ""
        if self.doc.get("kind") == "discovery#restDescription" or "discoveryVersion" in self.doc:
            # Google discovery format: schemas under "schemas", $refs are bare schema
            # names, and the dialect is JSON-Schema Draft-4-ish. Normalize name refs to
            # JSON pointers and drop the non-standard `type: "any"` so Draft4 can run.
            self.dialect = "google_discovery"
            _normalize_discovery(self.doc)
            self._validator_cls = Draft4Validator
            specification = DRAFT4
            self._schema_root = ("schemas",)
        elif version.startswith("2"):
            self.dialect = "swagger2"
            self._validator_cls = Draft4Validator
            specification = DRAFT4
            self._schema_root = ("definitions",)
        elif version.startswith("3.0"):
            self.dialect = "oas30"
            self._validator_cls = OAS30Validator
            specification = DRAFT202012  # used only for pointer resolution
            self._schema_root = ("components", "schemas")
        else:
            self.dialect = "oas31"
            self._validator_cls = OAS31Validator
            specification = DRAFT202012
            self._schema_root = ("components", "schemas")

        resource = Resource.from_contents(self.doc, default_specification=specification)
        self.registry: Registry = Registry().with_resource(self.doc_uri, resource)

    # ---- existence checks + pointers --------------------------------------

    def _node(self, *parts: str) -> Any:
        node: Any = self.doc
        for p in parts:
            if not isinstance(node, dict) or p not in node:
                return None
            node = node[p]
        return node

    def _deref(self, obj: Any) -> Any:
        """Follow a single intra-document `$ref` (JSON pointer into self.doc).

        OAS 3.x specs (GitHub, Discord) frequently express response examples as a
        `$ref` into `#/components/examples/*` rather than inlining them. Resolving
        that ref lets the self-consistency check (does the spec's own example uphold
        its own schema?) run for those specs too, so an authoritative spec is held to
        exactly the same fairness standard as Slack's inline-example one.
        """
        if isinstance(obj, dict) and "$ref" in obj and isinstance(obj["$ref"], str):
            ref = obj["$ref"]
            if ref.startswith("#/"):
                parts = [p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/")]
                return self._node(*parts)
        return obj

    def component(self, name: str) -> dict[str, Any] | None:
        return self._node(*self._schema_root, name)

    def _component_ref(self, name: str) -> dict[str, Any] | None:
        if self.component(name) is None:
            return None
        pointer = "/".join(_esc(p) for p in (*self._schema_root, name))
        return {"$ref": f"{self.doc_uri}#/{pointer}"}

    def _resolve_op(self, path: str, method: str) -> tuple[str, dict] | None:
        path_item = self._node("paths", path)
        if not isinstance(path_item, dict):
            return None
        if method.lower() in path_item:
            return method.lower(), path_item[method.lower()]
        # Slack documents methods as GET though slack_sdk POSTs; fall back to any.
        for m in ("get", "post", "put", "patch", "delete"):
            if m in path_item:
                return m, path_item[m]
        return None

    def _response_ref(self, path: str, method: str, status: str) -> dict[str, Any] | None:
        resolved = self._resolve_op(path, method)
        if not resolved:
            return None
        m, op = resolved
        resp = self._node("paths", path, m, "responses", status)
        if not isinstance(resp, dict):
            return None
        if self.dialect == "swagger2":
            if "schema" not in resp:
                return None
            ptr_tail = ("paths", path, m, "responses", status, "schema")
        else:
            content = resp.get("content", {})
            media = "application/json" if "application/json" in content else next(iter(content), None)
            if media is None or "schema" not in content[media]:
                return None
            ptr_tail = ("paths", path, m, "responses", status, "content", media, "schema")
        pointer = "/".join(_esc(p) for p in ptr_tail)
        return {"$ref": f"{self.doc_uri}#/{pointer}"}

    def _response_example(self, path: str, method: str, status: str) -> Any:
        resolved = self._resolve_op(path, method)
        if not resolved:
            return None
        m, _ = resolved
        resp = self._node("paths", path, m, "responses", status)
        if not isinstance(resp, dict):
            return None
        if self.dialect == "swagger2":
            examples = resp.get("examples")
            if isinstance(examples, dict) and examples:
                return self._deref(next(iter(examples.values())))
            return None
        content = resp.get("content", {})
        media = content.get("application/json") or next(iter(content.values()), {})
        if "example" in media:
            return self._deref(media["example"])
        examples = media.get("examples")
        if isinstance(examples, dict) and examples:
            first = self._deref(next(iter(examples.values())))
            if isinstance(first, dict) and "value" in first:
                return self._deref(first["value"])
            return first
        return None

    def response_authoritative(self, path: str, method: str = "get",
                               status: str = "200") -> bool:
        """Is this response's schema reliable enough to count failures as divergences?

        We can only hold the target to a contract the spec upholds for its own
        documented example. If the spec's example fails its own schema (the Slack
        spec does this for several methods), the schema is self-inconsistent and we
        downgrade failures against it to informational notes.
        """
        cache = getattr(self, "_authoritative_cache", None)
        if cache is None:
            cache = self._authoritative_cache = {}
        key = (path, method, status)
        if key in cache:
            return cache[key]
        example = self._response_example(path, method, status)
        ref = self._response_ref(path, method, status)
        if example is None or ref is None:
            cache[key] = True  # can't disprove -> treat as authoritative
            return True
        hard, _ = self._errors(ref, example)
        cache[key] = not hard
        return cache[key]

    # introspection helper (returns the inline schema dict, e.g. for tests/reports)
    def response_schema(self, path: str, method: str = "get",
                        status: str = "200") -> dict[str, Any] | None:
        resolved = self._resolve_op(path, method)
        if not resolved:
            return None
        m, _ = resolved
        resp = self._node("paths", path, m, "responses", status)
        if not isinstance(resp, dict):
            return None
        if self.dialect == "swagger2":
            return resp.get("schema")
        content = resp.get("content", {})
        media = content.get("application/json") or next(iter(content.values()), {})
        return media.get("schema")

    # ---- validation --------------------------------------------------------

    def _errors(self, ref_schema: dict[str, Any], payload: Any) -> tuple[list[str], list[str]]:
        """Return (hard_errors, undocumented_field_paths).

        In lenient mode, `additionalProperties` violations are pulled out of the hard
        errors and reported as undocumented fields instead.
        """
        validator = self._validator_cls(ref_schema, registry=self.registry)
        hard: list[str] = []
        undocumented: list[str] = []
        for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.path)):
            loc = "/".join(str(p) for p in err.path) or "<root>"
            if err.validator == "additionalProperties":
                fields = sorted(err.instance.keys() - set(err.schema.get("properties", {})))
                undocumented.extend(f"{loc}/{f}" if loc != "<root>" else f for f in fields)
                if self.lenient:
                    continue
            if len(hard) < _MAX_ERRORS:
                hard.append(f"{loc}: {self._message(err)}")
        return hard, undocumented

    @staticmethod
    def _message(err) -> str:
        """A useful one-line message, drilling into anyOf/oneOf branches.

        A bare anyOf/oneOf failure reports the unhelpful "X is not valid under any of
        the given schemas"; the real cause (e.g. a missing required field) lives in the
        sub-errors. We surface the best-matching sub-error so findings are actionable.
        """
        if err.context:
            deep = best_match(err.context)
            if deep is not None:
                tail = "/".join(str(p) for p in deep.relative_path)
                return f"{deep.message}" + (f" (at {tail})" if tail else "")
        return err.message

    def _validate(self, payload: Any, ref_schema: dict[str, Any] | None,
                  label: str, report: FidelityReport, authoritative: bool = True) -> bool:
        if ref_schema is None:
            report.note(f"no official schema found for `{label}`; left unvalidated")
            return True
        errors, undocumented = self._errors(ref_schema, payload)
        report.record_schema(label, errors, undocumented, authoritative=authoritative)
        return not errors

    def validate_against_component(self, payload: Any, component_name: str,
                                   report: FidelityReport) -> bool:
        return self._validate(payload, self._component_ref(component_name),
                              component_name, report)

    def validate_response(self, payload: Any, path: str, report: FidelityReport,
                          method: str = "get", status: str = "200",
                          label: str | None = None) -> bool:
        return self._validate(payload, self._response_ref(path, method, status),
                              label or path, report,
                              authoritative=self.response_authoritative(path, method, status))
