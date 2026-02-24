"""OCR preparation with manual-text priority and VLM fallback."""

from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Tuple

from ouroboros.llm import LLMClient
from vfriday.schemas import OCRPrepResult


def _hash_image(image_base64: str | None) -> str:
    raw = str(image_base64 or "")
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _vlm_ocr(image_base64: str, model: str) -> Tuple[str, Dict[str, float]]:
    if not image_base64 or not os.environ.get("OPENAI_API_KEY"):
        return "", {"cost": 0.0}
    prompt = (
        "Transcribe the math/physics content in this image.\n"
        "Return plain text first, then a section 'LATEX:' with formulas in LaTeX."
    )
    llm = LLMClient()
    text, usage = llm.vision_query(
        prompt=prompt,
        images=[{"base64": image_base64, "mime": "image/png"}],
        model=model,
        max_tokens=800,
        reasoning_effort="low",
    )
    return (text or "").strip(), usage or {}


def prepare_ocr_payload(
    *,
    problem_text: str | None,
    ocr_text: str | None,
    latex_text: str | None,
    user_message: str | None,
    image_base64: str | None,
    ocr_model: str,
) -> OCRPrepResult:
    """Build normalized text payload for solver stage."""
    notes: List[str] = []
    usage: Dict[str, float] = {"cost": 0.0}

    normalized_problem = (problem_text or "").strip()
    normalized_working = (user_message or "").strip()
    source = "manual_problem_text"

    if not normalized_problem and (ocr_text or "").strip():
        normalized_problem = (ocr_text or "").strip()
        source = "provided_ocr_text"
    if not normalized_problem and (latex_text or "").strip():
        normalized_problem = (latex_text or "").strip()
        source = "provided_latex_text"
    if not normalized_problem and image_base64:
        try:
            vlm_text, vlm_usage = _vlm_ocr(image_base64, ocr_model)
            if vlm_text:
                normalized_problem = vlm_text
                usage = vlm_usage
                source = "vlm_ocr"
                notes.append(f"image_hash={_hash_image(image_base64)}")
            else:
                notes.append("vlm_ocr_empty")
        except Exception as exc:
            notes.append(f"vlm_ocr_error:{type(exc).__name__}")

    if not normalized_working:
        if source in {"provided_ocr_text", "provided_latex_text", "vlm_ocr"}:
            normalized_working = normalized_problem
            notes.append("working_text_derived_from_problem")
        else:
            normalized_working = "(no explicit student working provided)"

    if not normalized_problem:
        normalized_problem = "(problem text unavailable; ask student to provide statement)"
        source = "missing_problem"

    return OCRPrepResult(
        normalized_problem=normalized_problem,
        normalized_working=normalized_working,
        source=source,
        usage=usage,
        notes=notes,
    )
