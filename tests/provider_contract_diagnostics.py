"""Opt-in public projections of synthetic canaries, never a second wire recorder.

Private CAS is read through the runtime's verified readers. Only selected request,
tool-field and response views leave it; neither SSE nor native opaque material is
exported. Observer/export failures are diagnostic gaps, not new canary verdicts.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import os
import uuid
from pathlib import Path


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _unavailable(reason, error=None):
    return {"status": "unavailable", "reason": reason,
            **({"error_type": type(error).__name__} if error is not None else {})}


def _pick(value, keys):
    return {key: value[key] for key in keys if key in value} if isinstance(value, dict) else {}


def _message(value):
    """Select useful public fields, excluding reasoning/signatures/replay receipts."""
    if not isinstance(value, dict):
        return {"unsupported_type": type(value).__name__}
    result = _pick(value, ("role", "tool_call_id", "name", "response_id", "finish_reason", "stop_reason", "refusal"))
    content = value.get("content")
    if isinstance(content, str) or content is None:
        result["content"] = content
    elif isinstance(content, list):
        result["content"] = [
            _pick(block, ("type", "text")) if block.get("type") == "text"
            else _pick(block, ("type", "id", "name", "input"))
            for block in content if isinstance(block, dict) and block.get("type") in {"text", "tool_use"}
        ]
    if "tool_calls" in value:
        calls = value["tool_calls"]
        result["tool_calls"] = [
            {**_pick(call, ("id", "type", "index")),
             **{kind: _pick(call[kind], ("name", "arguments", "input"))
                for kind in ("function", "custom") if kind in call}}
            if isinstance(call, dict) else {"unsupported_type": type(call).__name__}
            for call in calls
        ] if isinstance(calls, list) else {"unsupported_type": type(calls).__name__}
    result["omitted_fields"] = sorted(set(value) - set(result) - {"content"})
    if isinstance(content, list):
        result["content_projection"] = "text_and_tool_use_only; native_opaque_omitted"
    return result


def _request(value):
    result = _pick(value, ("model", "system", "tools", "functions", "tool_choice", "max_tokens",
                           "max_completion_tokens", "reasoning_effort", "temperature", "stream",
                           "stream_options", "thinking", "output_config", "bypass_response_cache"))
    if isinstance(value.get("messages"), list):
        result["messages"] = [_message(message) for message in value["messages"]]
    # No arbitrary extensions, headers, URL, or environment serialization.
    result["omitted_fields"] = sorted(set(value) - set(result))
    return result


def _assembly(value):
    if not isinstance(value, dict):
        return {"unsupported_type": type(value).__name__}
    if isinstance(value.get("choices"), list):
        return {"choices": [{**_pick(choice, ("index", "finish_reason")),
                              "message": _message(choice.get("message", {}))}
                             for choice in value["choices"] if isinstance(choice, dict)]}
    return _message(value)


class ProviderEvidence:
    def __init__(self, *, directory, root, task_id, nodeid, canary, secrets=()):
        self.directory, self.root = Path(directory), Path(root)
        self.task_id, self.nodeid, self.canary = task_id, nodeid, canary
        self._secrets = tuple(sorted(set(filter(None, secrets)), key=len, reverse=True))
        self.expected, self.attempts, self.errors = {}, [], []

    def safe(self, value):
        """Redact decoded JSON strings too, retaining original text when unchanged."""
        from ouroboros.observability import redact_projection
        from ouroboros.secret_masking import redact_known_values

        def visit(item):
            if isinstance(item, dict):
                return {visit(str(key)): visit(child) for key, child in item.items()}
            if isinstance(item, (list, tuple)):
                return [visit(child) for child in item]
            if not isinstance(item, str):
                return item
            try:
                parsed = json.loads(item)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                safe_parsed = redact_projection(visit(parsed)).value
                if safe_parsed != parsed:
                    item = json.dumps(safe_parsed, ensure_ascii=False, sort_keys=True)
            return redact_known_values(item, self._secrets)

        result = redact_projection(visit(value)).value
        # A final exact-value pass covers secret echoes in labels/keys as well.
        result = visit(result)
        return result, result != value

    def view(self, value, **facts):
        public, changed = self.safe(value)
        raw = _bytes(public)
        return {"status": "available", "value": public, "redacted": changed,
                "projection_bytes": len(raw), "projection_sha256": hashlib.sha256(raw).hexdigest(), **facts}

    def observe(self, event, **facts):
        try:
            if event == "expected":
                self.expected = self.view(facts)
            elif event == "attempt":
                usage = facts.get("usage") or {}
                error = facts.get("error")
                ids = usage.get("ledger_attempt_ids", []) if isinstance(usage, dict) else []
                ids = list(ids) + list(getattr(error, "ledger_attempt_ids", []) or [])
                capture = getattr(error, "physical_attempt_capture", None)
                if capture is not None and capture.attempt_id not in ids:
                    ids.append(capture.attempt_id)
                self.attempts.append({
                    "turn": facts["turn"], "ordinal": facts["ordinal"],
                    "status": "exception" if error else "semantic_empty" if facts.get("semantic_empty") else "response_received",
                    "attempt_ids": list(dict.fromkeys(ids)),
                    "logical_request": self.view(_request(facts["request"])),
                    "normalized_response": self.view(_message(facts["message"])) if "message" in facts else _unavailable("no_normalized_response"),
                    "usage": self.safe(_pick(usage, ("provider", "resolved_model", "response_provider", "response_finish_reason",
                                                    "prompt_tokens", "completion_tokens", "cost", "request_wire")))[0],
                    "error": self.error_facts(error) if error else None,
                })
        except Exception as exc:
            self.errors.append({"stage": event, "error_type": type(exc).__name__})

    def error_facts(self, error):
        from tests.provider_contract_ci import ProviderFailureClassification, classify_provider_failure
        classification = getattr(error, "provider_failure_classification", None)
        preserved_classification = isinstance(classification, ProviderFailureClassification)
        if not preserved_classification:
            classification = classify_provider_failure(self.canary.canary_id, error)
        result = {"exception_type": type(error).__name__, "classification": classification.kind.value,
                  "reason": classification.reason, "status_code": classification.status_code}
        if preserved_classification:
            result["originating_exception_type"] = error.provider_failure_exception_type
        # Only host-authored typed violations, never arbitrary assertion/HTTP text.
        if isinstance(error, AssertionError) and error.args and isinstance(error.args[0], dict):
            for key in ("provider_contract_violation", "semantic_empty_provider_response"):
                if key in error.args[0]:
                    result[key] = self.safe(error.args[0][key])[0]
        return result

    def fragments(self, evidence):
        """Project supported tool fields using the production SSE framer.

        Reassembled field values are checked before any fragments are published.
        Unfinished wire is omitted because a credential may span its missing tail.
        This projection is explicitly not whole-wire or assembler equivalence proof.
        """
        from ouroboros.llm_stream import _SSEFrames
        if not evidence.get("complete"):
            return _unavailable("incomplete_stream_fragment_view_omitted")
        frames = _SSEFrames()
        raw = base64.b64decode(evidence["wire_base64"], validate=True)
        selected, joined = [], {}

        def add(identity, fields, ordinal):
            if not fields:
                return
            selected.append({"frame": ordinal, "identity": list(identity), "fields": fields})
            for field, value in fields.items():
                if isinstance(value, str):
                    joined.setdefault((*identity, field), []).append(value)
                elif field not in {"index"} and not isinstance(value, (dict, list, int)):
                    raise ValueError("unsupported tool field")

        for ordinal, (event, data) in enumerate(frames.feed(raw, final=True)):
            if data == "[DONE]":
                continue
            chunk = json.loads(data)
            if not isinstance(chunk, dict):
                return _unavailable("unsupported_stream_shape")
            if "choices" in chunk:
                for choice in chunk["choices"]:
                    delta = choice.get("delta") or {}
                    for call in delta.get("tool_calls") or []:
                        ci, ti = choice.get("index"), call.get("index")
                        if type(ci) is not int or type(ti) is not int:
                            return _unavailable("unsupported_tool_identity")
                        add((ci, ti), _pick(call, ("id", "type")), ordinal)
                        for kind, argument in (("function", "arguments"), ("custom", "input")):
                            if kind in call:
                                add((ci, ti, kind), _pick(call[kind], ("name", argument)), ordinal)
            elif chunk.get("type") == "content_block_start":
                block = chunk.get("content_block") or {}
                if block.get("type") == "tool_use":
                    add(("native", chunk["index"]), _pick(block, ("id", "name", "input")), ordinal)
            elif chunk.get("type") == "content_block_delta":
                delta = chunk.get("delta") or {}
                if delta.get("type") == "input_json_delta":
                    add(("native", chunk["index"]), _pick(delta, ("partial_json",)), ordinal)
            elif not event and not chunk.get("type"):
                return _unavailable("unsupported_stream_shape")
        if frames.buffer or frames.data:
            return _unavailable("unframed_stream_tail")
        complete_fields = ["".join(parts) for parts in joined.values()]
        if self.safe(complete_fields)[1] or self.safe(selected)[1]:
            return _unavailable("tool_fields_require_redaction; fragments_omitted")
        return self.view(selected, basis="received_tool_fields_only; native_opaque_omitted",
                         source_bytes=len(raw), source_sha256=hashlib.sha256(raw).hexdigest())

    def physical(self, attempt_id, row):
        from ouroboros.observability import read_blob_ref, read_call_manifest_ref, read_call_payload
        result = {"attempt_id": attempt_id, "state": row.get("state", "unknown")}
        try:
            ref = row.get("candidate_manifest_ref")
            if ref:
                manifest = read_call_manifest_ref(self.root, ref, task_id=self.task_id)
                payload = read_blob_ref(self.root, manifest["redacted_projection_ref"])
            else:
                manifest, payload, _ = read_call_payload(self.root, task_id=self.task_id, call_id=attempt_id)
            result["physical_request"] = self.view(_request(payload),
                source_bytes=manifest["redacted_projection_ref"]["size"],
                source_sha256=manifest["redacted_projection_ref"]["sha256"],
                basis="persisted_post_transform_public_projection")
        except Exception as exc:
            result["physical_request"] = _unavailable("physical_request_unavailable", exc)
            self.errors.append({"stage": "physical_request", "attempt_id": attempt_id,
                                "error_type": type(exc).__name__})
        try:
            manifest, payload, _ = read_call_payload(self.root, task_id=self.task_id,
                                                    call_id=f"physical_{attempt_id}_stream")
            evidence = read_blob_ref(self.root, payload["private_wire_ref"])
            if evidence.get("attempt_id") != attempt_id:
                raise ValueError("stream attempt identity mismatch")
            result["stream_complete"] = evidence.get("complete")
            result["partial_assembly"] = self.view(_assembly(evidence.get("partial_assembly")),
                                                    basis="retained_after_assembly; not_original_wire")
            result["received_tool_fields"] = self.fragments(evidence)
        except FileNotFoundError:
            result["received_tool_fields"] = _unavailable("original_body_unavailable")
            result["partial_assembly"] = _unavailable("retained_stream_unavailable")
        except Exception as exc:
            result["received_tool_fields"] = _unavailable("stream_projection_unavailable", exc)
            result.setdefault("partial_assembly", _unavailable("retained_stream_unavailable", exc))
            self.errors.append({"stage": "stream_projection", "attempt_id": attempt_id,
                                "error_type": type(exc).__name__})
        return result

    def finish(self, *, outcome, error=None):
        from ouroboros.usage_accounting import read_usage_records
        from tests.ci_evidence import write_json
        try:
            rows = {row["attempt_id"]: row for row in read_usage_records(self.root, final_only=True)
                    if row.get("task_id") == self.task_id}
        except Exception as exc:
            rows = {}
            self.errors.append({"stage": "ledger", "error_type": type(exc).__name__})
        attempts = []
        for observed in self.attempts:
            item = copy.deepcopy(observed)
            ids = item["attempt_ids"]
            # Multiple sends in one chat call: retain the earlier physical attempt
            # even when the last one returned a usable response.
            detailed = outcome != "passed" or item["status"] != "response_received" or len(ids) > 1
            if detailed:
                item["physical_attempts"] = [self.physical(aid, rows.get(aid, {})) for aid in ids]
            else:
                for key in ("logical_request", "normalized_response"):
                    item.pop(key, None)
                item["physical_states"] = [{"attempt_id": aid, "state": rows.get(aid, {}).get("state", "unknown")}
                                           for aid in ids]
            if not ids:
                item["physical_evidence"] = _unavailable("no_physical_attempt_identity")
            item["detail_retained"] = detailed
            attempts.append(item)
        record = {"schema_version": 1, "nodeid": self.nodeid, "canary_id": self.canary.canary_id,
                  "requested_model": self.canary.model, "expected_provider": self.canary.expected_provider,
                  "outcome": outcome, "attempts": attempts, "diagnostics_errors": self.errors,
                  "stage": attempts[-1]["turn"] if attempts else "before_dispatch",
                  "error": self.error_facts(error) if error else None}
        if record["error"]:
            failure = record["error"]
            record["classification"] = failure["reason"]
            record["violation"] = failure.get("provider_contract_violation", {}).get("violation")
            if "semantic_empty_provider_response" in failure:
                record["violation"] = "semantic_empty_provider_response"
        if outcome != "passed" or any(item["detail_retained"] for item in attempts):
            record["logical_expected"] = self.expected or _unavailable("canary_not_dispatched")
        if not attempts:
            record["physical_evidence"] = _unavailable("canary_not_dispatched")
        if hasattr(self, "credential_present"):
            record["credential_status"] = "configured" if self.credential_present else "missing"
            if not attempts and not self.credential_present:
                record["classification"] = "missing_credential"
        record = self.safe(record)[0]
        path = self.directory / f"provider-{hashlib.sha256(self.nodeid.encode()).hexdigest()}.json"
        write_json(path, record)
        return path


@contextlib.contextmanager
def provider_evidence(request, canary):
    """Bind only the opt-in isolated CI lifecycle, including credential failures."""
    from tests.ci_evidence import output_dir
    try:
        directory = output_dir(request.config)
    except Exception as exc:
        print(f"provider diagnostics_incomplete: {type(exc).__name__}")
        directory = None
    if directory is None:
        yield None
        return
    from ouroboros import config
    from ouroboros.usage_accounting import UsageScope, usage_scope
    from tests.provider_contract_ci import provider_canary_matrix
    observer = ProviderEvidence(directory=directory, root=config.DATA_DIR,
        task_id=f"ci-provider-{uuid.uuid4().hex}", nodeid=request.node.nodeid, canary=canary,
        secrets=[os.environ.get(row.credential_env, "") for row in provider_canary_matrix()])
    observer.credential_present = bool(os.environ.get(canary.credential_env, "").strip())
    outcome, error = "passed", None
    try:
        with usage_scope(UsageScope(drive_root=observer.root, task_id=observer.task_id,
                                   source="test.provider_contract")):
            yield observer
    except BaseException as exc:
        import pytest
        error = exc
        outcome = "skipped" if isinstance(exc, pytest.skip.Exception) else "failed"
        raise
    finally:
        try:
            observer.finish(outcome=outcome, error=error)
        except Exception as exc:
            print(f"provider diagnostics_incomplete: {type(exc).__name__}")
