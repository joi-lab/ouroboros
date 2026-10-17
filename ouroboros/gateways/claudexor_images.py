"""Image facet of the engine's existing model-operation transport.

Mirrors schema/model-image.ts: model-purpose uploads, requestKind=image, and the
ordinary status/result/whole-result ACK/cancel verbs. No image endpoint or scheduler.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any

from ouroboros.gateways.claudexor import (
    _model_idempotency_key, _model_operation, _model_payload_ref, operation_query_supported,
)
from ouroboros.gateways.image_api import ImageBackendError, read_result_image


def image_operation_supported(operations: list[dict]) -> bool:
    return operation_query_supported(operations, method="POST", path="/v2/model-operations",
                                     name="requestKind", value="image")


def image_request(route: str, prompt: str, images: list[tuple[bytes, str]],
                  options: dict, credential_profile_id: str = "") -> dict:
    source, separator, model = route.partition("=")
    if not separator or not source.strip() or not model.strip():
        raise ValueError("Subscription image route must be claudexor::<source>=<image-model>.")
    if set(options) - {"size", "quality", "background", "n"}:
        raise ValueError("Subscription image options are size, quality, background and n.")
    if "size" in options and (not isinstance(options["size"], str) or not options["size"].strip()):
        raise ValueError("size must be a nonempty vendor size token.")
    if "quality" in options and options["quality"] not in {"low", "medium", "high", "auto"}:
        raise ValueError("Subscription quality must be low, medium, high or auto.")
    if "background" in options and options["background"] not in {"transparent", "opaque", "auto"}:
        raise ValueError("background must be transparent, opaque or auto.")
    if "n" in options and (type(options["n"]) is not int or not 1 <= options["n"] <= 10):
        raise ValueError("n must be an integer from 1 to 10 in one provider request.")
    return {"source": source, "model": model, "prompt": prompt,
            "account": ({"mode": "pin", "profileId": credential_profile_id}
                        if credential_profile_id else {"mode": "auto"}),
            "images": [{"mime": mime, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "dataBase64": base64.b64encode(data).decode("ascii")} for data, mime in images],
            "options": dict(options)}


def create_image_operation(gateway: Any, request_ref: dict, *, idempotency_key: str) -> dict:
    return _model_operation(gateway._request(
        "POST", "/v2/model-operations?requestKind=image",
        json_body={"request": _model_payload_ref(request_ref)},
        headers={"Idempotency-Key": _model_idempotency_key(idempotency_key)}))


def image_result_data(result: dict) -> tuple[list[dict], list[dict]]:
    """Keep valid siblings and report rejected rows against their original index."""
    images = result.get("images")
    if not isinstance(images, list):
        return [], [{"stage": "result", "code": "IMAGE_RESULT_INVALID", "error": "Engine images is not an array."}]
    rows, failures = [], []
    for index, item in enumerate(images):
        try:
            if not isinstance(item, dict):
                raise ImageBackendError("IMAGE_RESULT_INVALID", "Invalid engine image row.")
            row = {"b64_json": item.get("dataBase64"), "media_type": item.get("mime")}
            data, mime = read_result_image(row)
            if (mime != item.get("mime") or len(data) != item.get("sizeBytes")
                    or "sha256:" + hashlib.sha256(data).hexdigest() != item.get("sha256")):
                raise ImageBackendError("IMAGE_RESULT_INVALID", "Engine image differs from its declared MIME, size or SHA-256.")
            rows.append(row)
        except ImageBackendError as exc:
            failures.append({"index": index, "stage": "verify_image", "code": exc.code, "error": str(exc)})
    return rows, failures
