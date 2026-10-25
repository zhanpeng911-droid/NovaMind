"""Offline fanout replay checks; no model, network or persistent conversation store."""
from __future__ import annotations

import json
from unittest.mock import patch

from novamind.webui.live import LiveConversations


def start(live, turn="turn"):
    live.publish({"kind": "start", "turn_id": turn, "thread_id": "offline", "message": "问题"})


def text(live, number, turn="turn"):
    live.publish({"kind": "frame", "turn_id": turn,
                  "frame": {"type": "text", "content": f"[{number}]"}})


def test_reconnect_after_257_frames_exposes_gap_and_keeps_latest_text():
    live = LiveConversations()
    start(live)
    for number in range(257):
        text(live, number)
    _, turns = live.subscribe()
    replay = turns[0]
    assert replay.get("truncated") is True
    assert len(replay["frames"]) <= 256
    assert replay["frames"][-1]["content"] == "[256]"
    assert len({frame["content"] for frame in replay["frames"]}) == 256


def test_slow_listener_gets_resync_instead_of_silently_losing_start():
    live = LiveConversations()
    queue, _ = live.subscribe()
    start(live)
    for number in range(600):
        text(live, number)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events[0]["kind"] == "snapshot"
    replay = events[0]["turns"][0]
    assert replay["truncated"] is True
    replay_text = [frame["content"] for frame in replay["frames"]]
    replay_text += [event["frame"]["content"] for event in events[1:]]
    assert replay_text[-1] == "[599]"
    assert len(replay_text) == len(set(replay_text))


def test_failed_and_cancelled_turns_have_bounded_short_lived_replay():
    live = LiveConversations()
    with patch("time.monotonic", return_value=100):
        for number in range(20):
            turn = str(number)
            start(live, turn)
            text(live, number, turn)
            live.publish({"kind": "finish", "turn_id": turn, "error": True})
        start(live, "cancelled")
        live.publish({"kind": "finish", "turn_id": "cancelled", "cancelled": True})
        _, turns = live.subscribe()
    assert len(turns) <= 8
    assert turns[-1]["finish"]["cancelled"] is True
    assert turns[-2]["finish"]["error"] is True
    with patch("time.monotonic", return_value=161):
        _, turns = live.subscribe()
    assert turns == []


def test_large_text_replay_has_byte_budget_and_honest_truncation():
    live = LiveConversations()
    start(live)
    for number in range(4):
        live.publish({"kind": "frame", "turn_id": "turn",
                      "frame": {"type": "text", "content": "离线" * 100000 + str(number)}})
    _, turns = live.subscribe()
    replay = turns[0]
    assert replay.get("truncated") is True
    assert len(json.dumps(replay["frames"], ensure_ascii=False).encode()) <= 128 * 1024
    assert replay["frames"][-1]["content"].endswith("3")


def test_normal_completion_uses_history_instead_of_retaining_private_text():
    live = LiveConversations()
    start(live)
    text(live, 0)
    live.publish({"kind": "finish", "turn_id": "turn", "error": False})
    _, turns = live.subscribe()
    assert turns == []


def test_queue_full_at_successful_finish_still_replays_a_known_terminal_state():
    live = LiveConversations()
    queue, _ = live.subscribe()
    start(live)
    for number in range(255):
        text(live, number)
    assert queue.full()
    live.publish({"kind": "finish", "turn_id": "turn", "error": False})
    snapshot = queue.get_nowait()
    assert snapshot["kind"] == "snapshot"
    assert snapshot["turns"][0]["finish"] == {"error": False, "cancelled": False}
    assert queue.empty()
    _, turns = live.subscribe()
    assert turns == []  # Successful private text is not retained for future subscribers.


def test_terminal_replay_precedes_new_active_turn_in_the_same_thread():
    live = LiveConversations()
    start(live, "old")
    live.publish({"kind": "finish", "turn_id": "old", "cancelled": True})
    start(live, "current")
    _, turns = live.subscribe()
    assert [turn["turn_id"] for turn in turns] == ["old", "current"]
