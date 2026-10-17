"""Delivery participates in the reviewer roster identity (the ask's reconciliation roster hash)."""
import dataclasses

from ouroboros.review_records import ReviewSlot
from ouroboros.tools.plan_review_runtime import plan_reviewer_config_fingerprint


def test_delivery_fingerprint_preserves_legacy_and_separates_direct_native():
    slot = ReviewSlot(slot_id="one", model="m/a", effort="high")
    assert plan_reviewer_config_fingerprint([slot]) == plan_reviewer_config_fingerprint([
        dataclasses.replace(slot, native_retrieval_override=False)])
    assert plan_reviewer_config_fingerprint([slot]) != plan_reviewer_config_fingerprint([
        dataclasses.replace(slot, native_retrieval_override=True)])
