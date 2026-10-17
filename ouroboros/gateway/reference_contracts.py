"""Additive stored-row identity shared by live chat and attachment frames."""
from typing import Any, TypedDict


class ChatReferenceFields(TypedDict, total=False):
    row_address: str
    reply_to: dict[str, Any]
    relay: dict[str, Any]
    reply_to_relay: str
    kind: str
