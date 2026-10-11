"""Cross-stream integration for the chat.links host event.

This seam only exists on the merged tree: the media stream registers
``chat.links`` in the event-bus topic allowlist and publishes the host event,
while the Telegram skill subscribes to it and addresses the target chat. The
per-stream suites mock these halves independently; this test wires the real
``EventBus`` producer payload to the real Telegram consumer addressing.
"""

from ouroboros.event_bus import (
    CHAT_LINKS,
    EventBus,
    VALID_TOPICS,
    publish_event,
    get_global_event_bus,
)
from supervisor import message_bus
from skills.telegram import plugin as telegram_plugin


def _canonical_links_payload(chat_id, transport):
    """The exact payload shape supervisor.message_bus publishes for send_links."""
    return {
        "chat_id": int(chat_id),
        "transport": dict(transport),
        "title": "Docs",
        "actions": [{"label": "Spec", "url": "https://example.com/spec"}],
        "ts": "2026-08-30T00:00:00Z",
    }


def test_chat_links_is_a_valid_event_topic():
    # The media stream added CHAT_LINKS to the allowlist; a C-only tree would
    # raise on subscribe/publish. Both halves coexist only on the merged tree.
    assert CHAT_LINKS == "chat.links"
    assert CHAT_LINKS in VALID_TOPICS


_CONTRACT_KEYS = {"chat_id", "transport", "title", "actions", "ts"}


def test_real_event_bus_delivers_the_canonical_links_payload():
    bus = EventBus()
    received = []
    bus.subscribe("telegram", CHAT_LINKS, lambda data: received.append(data))
    payload = _canonical_links_payload(42, {"kind": "telegram", "conversation_id": 777})
    bus.publish(CHAT_LINKS, payload)
    assert len(received) == 1
    # The bus augments the payload with a routing "topic" key; every contract
    # field the consumer relies on must survive delivery.
    assert _CONTRACT_KEYS <= set(received[0])
    assert received[0]["topic"] == CHAT_LINKS
    assert received[0]["actions"] == [{"label": "Spec", "url": "https://example.com/spec"}]


def test_unknown_topic_still_rejected():
    bus = EventBus()
    for op in (lambda: bus.subscribe("x", "chat.links.bogus", lambda _d: None),
               lambda: bus.publish("chat.links.bogus", {})):
        try:
            op()
            raise AssertionError("expected ValueError for an unsupported topic")
        except ValueError:
            pass


def test_message_bus_send_links_publishes_to_chat_links():
    # The producer publishes on the shared global bus that the plugin subscribes
    # through; subscribe there and drive the real send_links path.
    bus = get_global_event_bus()
    received = []
    sub_id = bus.subscribe("telegram-producer-probe", CHAT_LINKS, lambda d: received.append(d))
    try:
        broadcasts = []
        bridge = message_bus.LocalChatBridge({})
        bridge._broadcast_fn = broadcasts.append
        if hasattr(bridge, "_chat_transports"):
            bridge._chat_transports[7] = {"kind": "telegram", "conversation_id": 777}
        ok, _msg = bridge.send_links(
            7, [{"label": "Spec", "url": "https://example.com/spec"}], title="Docs",
        )
        assert ok is True, _msg
        assert received, "send_links must publish a chat.links host event"
        data = received[-1]
        assert data.get("topic") == CHAT_LINKS
        assert _CONTRACT_KEYS <= set(data)
        assert data["actions"] == [{"label": "Spec", "url": "https://example.com/spec"}]
    finally:
        bus.unsubscribe(sub_id)


def test_producer_event_reaches_telegram_target_end_to_end():
    bus = get_global_event_bus()
    received = []
    sub_id = bus.subscribe("telegram-e2e-probe", CHAT_LINKS, lambda data: received.append(data))
    try:
        bridge = message_bus.LocalChatBridge({})
        bridge._chat_transports[7] = {"kind": "telegram", "conversation_id": 777}
        ok, error = bridge.send_links(
            7, [{"label": "Spec", "url": "https://example.com/spec"}], title="Docs",
        )
        assert (ok, error) == (True, "ok")
        assert received, "send_links must publish a chat.links host event"

        captured_event = received[-1]
        assert telegram_plugin._target_chat(
            {"TELEGRAM_CHAT_ID": ""}, captured_event,
        ) == 777
    finally:
        bus.unsubscribe(sub_id)


def test_telegram_consumer_addresses_the_producer_payload():
    # The Telegram skill's addressing must consume the producer's transport dict.
    payload = _canonical_links_payload(7, {"kind": "telegram", "conversation_id": 777})
    settings = {"TELEGRAM_CHAT_ID": ""}
    chat_id = telegram_plugin._target_chat(settings, payload)
    assert int(chat_id) == 777


def test_global_event_bus_accepts_chat_links_subscription():
    # The real singleton the plugin's PluginAPIImpl.subscribe_event routes through.
    bus = get_global_event_bus()
    seen = []
    sub_id = bus.subscribe("telegram-integration-probe", CHAT_LINKS, lambda d: seen.append(d))
    try:
        publish_event(CHAT_LINKS, _canonical_links_payload(1, {"kind": "telegram", "conversation_id": 1}))
        assert len(seen) == 1
        assert _CONTRACT_KEYS <= set(seen[0])
    finally:
        bus.unsubscribe(sub_id)
