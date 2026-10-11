from ouroboros.event_bus import CHAT_LINKS, CHAT_OUTBOUND, SKILL_LIFECYCLE, EventBus
import base64
import asyncio
import json
from types import SimpleNamespace


def test_event_bus_publishes_to_matching_topic() -> None:
    bus = EventBus()
    received = []

    bus.subscribe("skill", CHAT_OUTBOUND, received.append)
    bus.publish(CHAT_OUTBOUND, {"text": "hello"})

    assert received == [{"text": "hello", "topic": CHAT_OUTBOUND}]


def test_event_bus_supports_skill_lifecycle_topic() -> None:
    bus = EventBus()
    received = []

    bus.subscribe("skill", SKILL_LIFECYCLE, received.append)
    bus.publish(SKILL_LIFECYCLE, {"type": "skill_exec_finished", "skill": "demo"})

    assert received == [{
        "type": "skill_exec_finished",
        "skill": "demo",
        "topic": SKILL_LIFECYCLE,
    }]


def test_event_bus_supports_structured_chat_links_topic() -> None:
    bus = EventBus()
    received = []
    bus.subscribe("skill", CHAT_LINKS, received.append)

    bus.publish(CHAT_LINKS, {"actions": [{"label": "Docs", "url": "https://example.com"}]})

    assert received[0]["topic"] == CHAT_LINKS
    assert received[0]["actions"][0]["label"] == "Docs"


def test_event_bus_unsubscribe_skill_removes_handlers() -> None:
    bus = EventBus()
    received = []

    bus.subscribe("skill", CHAT_OUTBOUND, received.append)
    bus.unsubscribe_skill("skill")
    bus.publish(CHAT_OUTBOUND, {"text": "hello"})

    assert received == []


def test_event_bus_rejects_unknown_topic() -> None:
    bus = EventBus()

    try:
        bus.subscribe("skill", "unknown.topic", lambda _payload: None)
    except ValueError as exc:
        assert "unsupported event topic" in str(exc)
    else:
        raise AssertionError("expected unknown topic to be rejected")


def test_event_bus_schedules_async_handler_from_sync_publish() -> None:
    async def main():
        bus = EventBus()
        bus.set_loop(asyncio.get_running_loop())
        received = []

        async def handler(payload):
            received.append(payload["text"])

        bus.subscribe("skill", CHAT_OUTBOUND, handler)
        bus.publish(CHAT_OUTBOUND, {"text": "hello"})
        await asyncio.sleep(0.05)
        assert received == ["hello"]

    asyncio.run(main())


def test_supervisor_dispatches_skill_lifecycle_to_log_and_event_bus(tmp_path, monkeypatch) -> None:
    from supervisor.events import dispatch_event
    from ouroboros.utils import append_jsonl

    pushed = []
    published = []
    sent_video = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        append_jsonl=append_jsonl,
        bridge=SimpleNamespace(
            push_log=pushed.append,
            send_video=lambda chat_id, video_bytes, caption="", mime="", task_id="": (
                sent_video.append((chat_id, video_bytes, caption, mime)) or (True, "ok")
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.event_bus.publish_event", lambda topic, payload: published.append((topic, payload)))

    dispatch_event({"type": "skill_exec_finished", "skill": "demo", "exit_code": 0}, ctx)
    dispatch_event({
        "type": "send_video",
        "chat_id": 0,
        "video_base64": base64.b64encode(b"vid").decode("ascii"),
        "caption": "zero",
        "mime": "video/mp4",
    }, ctx)

    event_log = tmp_path / "logs" / "events.jsonl"
    records = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["type"] == "skill_exec_finished"
    assert pushed[-1]["skill"] == "demo"
    assert published[-1][0] == SKILL_LIFECYCLE
    assert published[-1][1]["skill"] == "demo"
    assert sent_video == [(0, b"vid", "zero", "video/mp4")]
