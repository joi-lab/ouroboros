"""Multi-model review tool — sends code/text to multiple LLMs for consensus review.

Models are NOT hardcoded — the LLM chooses which models to use based on
prompt guidance. Budget is tracked via llm_usage events.
"""

import os
import json
import asyncio
import logging

from ouroboros.llm import LLMClient, normalize_model_name
from ouroboros.utils import utc_now_iso
from ouroboros.tools.registry import ToolEntry, ToolContext


log = logging.getLogger(__name__)

# Maximum number of models allowed per review
MAX_MODELS = 10
# Concurrency limit for parallel requests
CONCURRENCY_LIMIT = 5

def get_tools():
    """Return list of ToolEntry for registry."""
    return [
        ToolEntry(
            name="multi_model_review",
            schema={
                "name": "multi_model_review",
                "description": (
                    "Send code or text to multiple LLM models for review/consensus. "
                    "Each model reviews independently. Returns structured verdicts. "
                    "Choose diverse models yourself. Budget is tracked automatically."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The code or text to review",
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Review instructions — what to check for. "
                                "Fully specified by the LLM at call time."
                            ),
                        },
                        "models": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Model identifiers to query "
                                "(e.g. 3 diverse models for good coverage)"
                            ),
                        },
                    },
                    "required": ["content", "prompt", "models"],
                },
            },
            handler=_handle_multi_model_review,
        )
    ]


def _handle_multi_model_review(ctx: ToolContext, content: str = "", prompt: str = "", models: list = None) -> str:
    """Sync wrapper around async multi-model review. Registry calls this."""
    if models is None:
        models = []
    try:
        try:
            asyncio.get_running_loop()
            # Already in async context — run in a separate thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(asyncio.run, _multi_model_review_async(content, prompt, models, ctx)).result()
        except RuntimeError:
            # No running loop — safe to use asyncio.run directly
            result = asyncio.run(_multi_model_review_async(content, prompt, models, ctx))
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        log.error("Multi-model review failed: %s", e, exc_info=True)
        return json.dumps({"error": f"Review failed: {e}"}, ensure_ascii=False)


async def _query_model(model, messages, semaphore):
    """Query a single model with semaphore-based concurrency control."""
    async with semaphore:
        try:
            def _call() -> dict:
                llm = LLMClient()
                msg, usage = llm.chat(
                    messages=messages,
                    model=normalize_model_name(model, fallback="gpt-5-mini"),
                    tools=None,
                    reasoning_effort="low",
                    max_tokens=1400,
                )
                return {
                    "choices": [{"message": {"content": str(msg.get("content") or "")}}],
                    "usage": usage or {},
                }

            data = await asyncio.to_thread(_call)
            return model, data, None
        except asyncio.TimeoutError:
            return model, "Error: Timeout after 120s", None
        except Exception as e:
            error_msg = str(e)[:200]
            if len(str(e)) > 200:
                error_msg += " [truncated]"
            return model, f"Error: {error_msg}", None


async def _multi_model_review_async(content: str, prompt: str, models: list, ctx: ToolContext):
    """Async orchestration: validate → query → parse → emit → return."""
    # Validation
    if not content:
        return {"error": "content is required"}
    if not prompt:
        return {"error": "prompt is required"}
    if not models:
        return {"error": "models list is required (e.g. ['openai/o3', 'google/gemini-2.5-pro'])"}

    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        return {"error": "models must be a list of strings"}

    if len(models) > MAX_MODELS:
        return {"error": f"Too many models requested ({len(models)}). Maximum is {MAX_MODELS}."}

    if len(models) == 0:
        return {"error": "At least one model is required"}

    if not os.environ.get("OPENAI_API_KEY"):
        return {"error": "OPENAI_API_KEY not set"}

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": content},
    ]

    # Query all models with bounded concurrency
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    tasks = [_query_model(m, messages, semaphore) for m in models]
    results = await asyncio.gather(*tasks)

    # Parse and process results
    review_results = []
    for model, result, headers_dict in results:
        review_result = _parse_model_response(model, result, headers_dict)
        _emit_usage_event(review_result, ctx)
        review_results.append(review_result)

    return {
        "model_count": len(models),
        "results": review_results,
    }


def _parse_model_response(model: str, result, headers_dict) -> dict:
    """Parse one model's response into structured review_result dict."""
    if isinstance(result, str):
        # Error case
        return {
            "model": model,
            "verdict": "ERROR",
            "text": result,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_estimate": 0.0,
        }

    # Success case — extract response text and verdict
    try:
        choices = result.get("choices", [])
        if not choices:
            text = f"(no choices in response: {json.dumps(result)[:200]})"
            verdict = "ERROR"
        else:
            text = choices[0]["message"]["content"]
            # Robust verdict parsing: check first 3 lines for PASS/FAIL anywhere (case-insensitive)
            verdict = "UNKNOWN"
            lines = text.split("\n")[:3]  # Check only first 3 lines
            for line in lines:
                line_upper = line.upper()
                if "PASS" in line_upper:
                    verdict = "PASS"
                    break
                elif "FAIL" in line_upper:
                    verdict = "FAIL"
                    break
    except (KeyError, IndexError, TypeError):
        error_text = json.dumps(result)[:200]
        if len(json.dumps(result)) > 200:
            error_text += " [truncated]"
        text = f"(unexpected response format: {error_text})"
        verdict = "ERROR"

    # Extract usage for budget tracking
    usage = result.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    # Extract cost from response body if present
    cost = 0.0
    try:
        if "usage" in result and "cost" in result["usage"]:
            cost = float(result["usage"]["cost"])
        elif "usage" in result and "total_cost" in result["usage"]:
            cost = float(result["usage"]["total_cost"])
    except (ValueError, TypeError, KeyError):
        pass

    return {
        "model": model,
        "verdict": verdict,
        "text": text,
        "tokens_in": prompt_tokens,
        "tokens_out": completion_tokens,
        "cost_estimate": cost,
    }


def _emit_usage_event(review_result: dict, ctx: ToolContext) -> None:
    """Emit llm_usage event for budget tracking (for ALL cases, including errors)."""
    if ctx is None:
        return

    usage_event = {
        "type": "llm_usage",
        "ts": utc_now_iso(),
        "task_id": ctx.task_id if ctx.task_id else "",
        "usage": {
            "prompt_tokens": review_result["tokens_in"],
            "completion_tokens": review_result["tokens_out"],
            "cost": review_result["cost_estimate"],
        },
        "category": "review",
    }

    if ctx.event_queue is not None:
        try:
            ctx.event_queue.put_nowait(usage_event)
        except Exception:
            # Fallback to pending_events if queue fails
            if hasattr(ctx, "pending_events"):
                ctx.pending_events.append(usage_event)
    elif hasattr(ctx, "pending_events"):
        # No event_queue — use pending_events
        ctx.pending_events.append(usage_event)
