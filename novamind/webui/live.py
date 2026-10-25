"""Bounded process-local fanout for workbench/assistant conversation mirroring."""
from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import OrderedDict


MAX_REPLAY_FRAMES = 256
MAX_REPLAY_BYTES = 128 * 1024
MAX_RECENT_TURNS = 8
RECENT_TTL_SECONDS = 60


def _size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _tail(text, byte_limit):
    return text.encode("utf-8")[-byte_limit:].decode("utf-8", errors="ignore")


class LiveConversations:
    def __init__(self):
        self.listeners: set[asyncio.Queue] = set()
        self.active: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._sizes: dict[str, list[int]] = {}
        self._recent: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._expiry: dict[str, asyncio.TimerHandle] = {}

    def _forget(self, turn):
        self._recent.pop(turn, None)
        handle = self._expiry.pop(turn, None)
        if handle is not None:
            handle.cancel()

    def _snapshot(self, finished=None):
        now = time.monotonic()
        for turn, (expires, _) in list(self._recent.items()):
            if expires <= now:
                self._forget(turn)
        # Older terminal turns must replay before current turns in the same thread.
        turns = [item[1] for item in self._recent.values()]
        if finished is not None:
            turns.append(finished)
        return copy.deepcopy(turns + list(self.active.values()))

    def _append(self, turn, frame):
        state = self.active[turn]
        # Keep a suffix, never an unbounded aggregate of the complete answer.
        if _size(frame) > MAX_REPLAY_BYTES - 256:
            state["truncated"] = True
            if frame.get("type") != "text":
                return False
            frame = {"type": "text", "content": _tail(frame["content"], MAX_REPLAY_BYTES - 512)}
        frame = copy.deepcopy(frame)
        frames, sizes = state["frames"], self._sizes[turn]
        frames.append(frame)
        sizes.append(_size(frame) + 1)
        while len(frames) > MAX_REPLAY_FRAMES or sum(sizes) > MAX_REPLAY_BYTES - 2:
            frames.pop(0)
            sizes.pop(0)
            state["truncated"] = True
        return True

    def publish(self, event):
        turn = event.get("turn_id")
        bounded = True
        finished = None
        if event["kind"] == "start":
            self._forget(turn)
            state = {**copy.deepcopy(event), "frames": []}
            if _size(state.get("message", "")) > 16 * 1024:
                state["message"] = _tail(state["message"], 16 * 1024)
                state["truncated"] = True
                bounded = False
            self.active[turn] = state
            self._sizes[turn] = []
        elif event["kind"] == "frame" and turn in self.active:
            bounded = self._append(turn, event["frame"]) and _size(event["frame"]) <= MAX_REPLAY_BYTES
        elif event["kind"] == "finish":
            state = self.active.pop(turn, None)
            self._sizes.pop(turn, None)
            self.tasks.pop(turn, None)
            if state is not None:
                state["finish"] = {"cancelled": bool(event.get("cancelled")), "error": bool(event.get("error"))}
                finished = state
            if state is not None and (event.get("cancelled") or event.get("error")):
                self._recent[turn] = (time.monotonic() + RECENT_TTL_SECONDS, state)
                while len(self._recent) > MAX_RECENT_TURNS:
                    self._forget(next(iter(self._recent)))
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass  # Synchronous callers also expire records on snapshot access.
                else:
                    self._expiry[turn] = loop.call_later(RECENT_TTL_SECONDS, self._forget, turn)
                finished = None  # Already included in the recent terminal replay.
        for queue in tuple(self.listeners):
            if queue.full() or not bounded:
                # One replacement per full queue, with no duplicate current frame:
                # the snapshot already includes the event being published.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"kind": "snapshot", "turns": self._snapshot(finished)})
                if event["kind"] == "history":
                    queue.put_nowait(event)
            else:
                queue.put_nowait(event)

    def subscribe(self):
        queue = asyncio.Queue(maxsize=MAX_REPLAY_FRAMES)
        self.listeners.add(queue)
        return queue, self._snapshot()

    def unsubscribe(self, queue):
        self.listeners.discard(queue)
