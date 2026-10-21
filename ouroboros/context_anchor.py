"""This conversation's own window facts: its size anchor, its accepted floor and its refused sizes.

Main measures every candidate on the conversation it belongs to. The last request a model
accepted in this task gives the provider-reported input U and the host's raw estimate B of
that same candidate; the next candidate is ``U + (B' - B) * U / B``. Another task's request
says nothing reliable about this one: its composition and size differ (17.10.2026: a 97k
helper request's density, borrowed by a 900k release task, hid a full window). The largest
input a model accepted here is a floor under its window: a stated window below it is no
window. The smallest estimate a model refused here bounds it from above and clears the
free room the facts state. All live in the task's usage, so a working checkpoint keeps
them and a new execution starts cold. Data and arithmetic only; the loop records them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

#: This conversation's own size anchors (model key -> its last accepted request) and accepted-input floors in
#: the task's usage (both survive a working checkpoint; a new execution starts without them). Each model keeps
#: its own anchor: a fallback's acceptance never erases the primary's, so a return to it stays measured.
INPUT_ANCHOR_KEY = "_context_input_anchor"
ACCEPTED_INPUTS_KEY = "_context_accepted_inputs"
REFUSED_INPUTS_KEY = "_context_refused_inputs"  # model key -> the smallest estimate it refused in this task


@dataclass(frozen=True)
class InputAnchor:
    """The last request this model accepted in this conversation.

    ``input_tokens`` (U) is the provider-reported input of that request, ``estimate_tokens`` (B) the
    host's raw estimate of the same candidate. The next candidate measures ``U + (B' - B) * U / B``:
    nothing is borrowed from another conversation, whose composition and size differ. ``rewritten``
    says the transcript was rewritten since (a fold, a recovery pass): the same arithmetic, less sure.
    """

    input_tokens: int
    estimate_tokens: int
    round: int
    rewritten: bool = False

    @property
    def density(self) -> float:
        return self.input_tokens / self.estimate_tokens


def _anchor_model_key(plan: Any) -> str:
    from ouroboros.provider_models import normalize_model_identity

    return (f"{str(getattr(plan, 'provider', '') or '').strip().lower()}|"
            f"{normalize_model_identity(str(getattr(plan, 'model', '') or '').strip())}")


def record_input_anchor(usage: Dict[str, Any], plan: Any, *, round_idx: int, rewrite_epoch: int = 0) -> None:
    """Make the Main request just accepted this conversation's size anchor and raise its accepted floor.

    U is the adapter-normalized input of the settled response (``_last_round_cache_usage``), B the raw
    estimate measured for that candidate (``_context_raw_input_tokens``). A response another model
    served, an input total whose cache semantics are unknown, a gateway total that counts the cache read
    twice, a count an order of magnitude away from the raw estimate of the same bytes, or a missing count
    leaves the previous anchor and floor as they were: unknown stays unknown.
    """
    from ouroboros.capability_evidence import _CACHE_INCLUSIVE_PROMPT_TOKEN_PROVIDERS, gateway_double_counted_cache

    if plan is None or usage.get("_model_substitutions"):
        return
    last = usage.get("_last_round_cache_usage") or {}
    real, estimate = last.get("prompt_tokens"), usage.get("_context_raw_input_tokens")
    if not isinstance(real, int) or not isinstance(estimate, int) or real <= 0 or estimate <= 0:
        return
    if not estimate / 10 <= real <= estimate * 10:
        return  # an order of magnitude off the same bytes is no input total of this request (a delta, a stub)
    provider = str(last.get("provider") or getattr(plan, "provider", "") or "").strip().lower()
    cache_bearing = bool(last.get("cached_tokens") or last.get("cache_write_tokens"))
    if cache_bearing and provider not in _CACHE_INCLUSIVE_PROMPT_TOKEN_PROVIDERS | {"claudexor"}:
        return
    if gateway_double_counted_cache(real, int(last.get("cached_tokens") or 0)):
        return  # twice the real input would double every estimate and lift the floor above the window for good
    key = _anchor_model_key(plan)
    anchors = usage.get(INPUT_ANCHOR_KEY)
    anchors = dict(anchors) if isinstance(anchors, dict) and "model" not in anchors else {}
    anchors[key] = {"input_tokens": real, "estimate_tokens": estimate, "round": int(round_idx),
                    "rewrite_epoch": int(rewrite_epoch)}
    usage[INPUT_ANCHOR_KEY] = anchors
    floors = usage.setdefault(ACCEPTED_INPUTS_KEY, {})
    floors[key] = max(int(floors.get(key) or 0), real)


def main_input_anchor(usage: Optional[Mapping[str, Any]], plan: Any, *,
                      rewrite_epoch: Optional[int] = None) -> Tuple[Optional[InputAnchor], Optional[int]]:
    """``(anchor, accepted floor)`` of ``plan``'s model in this conversation, or ``(None, None)``.

    A different model or provider (a fallback, an owner switch) has no anchor here; a Codex account
    rotation keeps the model and therefore the anchor. ``rewrite_epoch`` is the slot's current count of
    sanctioned rewrites (``transcript_prefix.rewrite_epoch``); None treats the view as rewritten.
    """
    if not isinstance(usage, Mapping) or plan is None:
        return None, None
    key = _anchor_model_key(plan)
    floor = (usage.get(ACCEPTED_INPUTS_KEY) or {}).get(key)
    anchors = usage.get(INPUT_ANCHOR_KEY)
    row = anchors.get(key) if isinstance(anchors, Mapping) else None
    anchor = None
    if isinstance(row, Mapping):
        anchor = InputAnchor(input_tokens=int(row["input_tokens"]), estimate_tokens=int(row["estimate_tokens"]),
                             round=int(row.get("round") or 0),
                             rewritten=rewrite_epoch is None or int(row.get("rewrite_epoch") or 0) != rewrite_epoch)
    return anchor, (int(floor) if floor else None)


def floored_window(window_tokens: Optional[int], known: bool, accepted: Optional[int]) -> Tuple[int, bool]:
    """``(window, known)`` with this conversation's floor applied: a stated window below an input this model
    already accepted here is no window (``known`` False; the number stays, so the facts can name the reading
    the floor disproved), whatever reads it — the Main fit, a route rebind, the books step of a refusal
    pass. Otherwise the reading passes unchanged."""
    if accepted and window_tokens and int(accepted) > int(window_tokens):
        return int(window_tokens), False
    return int(window_tokens or 0), bool(known)


def record_refused_input(usage: Dict[str, Any], plan: Any, estimate_tokens: int) -> None:
    """Remember the smallest input estimate ``plan``'s model refused by size in this task."""
    if plan is None or int(estimate_tokens or 0) <= 0:
        return
    refused = usage.setdefault(REFUSED_INPUTS_KEY, {})
    key = _anchor_model_key(plan)
    refused[key] = min(int(refused.get(key) or estimate_tokens), int(estimate_tokens))


def refused_input(usage: Optional[Mapping[str, Any]], plan: Any) -> Optional[int]:
    """The smallest estimate ``plan``'s model refused here, or None."""
    if not isinstance(usage, Mapping) or plan is None:
        return None
    value = (usage.get(REFUSED_INPUTS_KEY) or {}).get(_anchor_model_key(plan))
    return int(value) if value else None
