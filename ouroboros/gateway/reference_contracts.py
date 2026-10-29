"""Additive stored-row identity shared by live chat and attachment frames."""
from typing import Any, TypedDict


class ChatReferenceFields(TypedDict, total=False):
    row_address: str
    row_client_message_id: str  # an outgoing row's own saved name; never the owner's submission id
    reply_to: dict[str, Any]
    relay: dict[str, Any]
    reply_to_relay: str
    kind: str
