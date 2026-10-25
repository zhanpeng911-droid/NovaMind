"""Bounded, nonblocking commit observer. Full queues sacrifice learning, not chat."""
from __future__ import annotations

import logging
import queue
import threading
import time

from .policy import can_observe, digest, features

logger = logging.getLogger(__name__)


class LearningObserver:
    def __init__(self, store, settings_reader=None):
        if settings_reader is None:
            from ...personal import settings
            settings_reader = settings
        self.store = store
        self.settings_reader = settings_reader
        self.queue: queue.Queue = queue.Queue(maxsize=128)
        self.stopped = threading.Event()
        self.dropped = 0
        self.generation = 0
        self.cached = {}
        self.refresh()
        self.thread = threading.Thread(target=self._work, name="novamind-learning-observer", daemon=True)
        self.thread.start()

    def submit(self, event):
        if self.stopped.is_set() or not can_observe(self.cached.get("learning", {}), event["context"],
                                                    self.cached.get("memory", {}).get("auto_extract", True)):
            return
        hints = features(event["text"])
        if hints is None and event["evidence"].get("skills"):
            hints = {"family": "personal", "label": "已使用的个人方法", "fingerprint": digest(event["run_id"])}
        if hints is None:
            return
        item = {key: value for key, value in event.items() if key != "text"}
        item["features"] = hints
        item["generation"] = self.generation
        item["evidence"] = {**item["evidence"], "submitted_at": time.time()}
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def refresh(self):
        settings = self.settings_reader()
        self.last_refresh = time.monotonic()
        old = (self.cached.get("learning", {}).get("enabled"), self.cached.get("memory", {}).get("auto_extract"))
        self.cached = {"learning": dict(settings.get("learning", {})), "memory": dict(settings.get("memory", {}))}
        new = (self.cached["learning"].get("enabled"), self.cached["memory"].get("auto_extract"))
        if old != new:
            self.generation += 1

    def _work(self):
        while not self.stopped.is_set():
            try:
                event = self.queue.get(timeout=.1)
            except queue.Empty:
                if time.monotonic() - self.last_refresh >= 1:
                    try:
                        self.refresh()
                    except Exception:
                        self.last_refresh = time.monotonic()
                        self.cached = {"learning": {"enabled": False}}
                        self.generation += 1
                continue
            try:
                self.refresh()
                settings = self.cached
                options = settings.get("learning", {})
                if event["generation"] == self.generation and can_observe(options, event["context"], settings.get("memory", {}).get("auto_extract", True)):
                    self.store.observe(event["run_id"], event["thread_id"], event["message_ids"],
                                       event["context"].get("scope") or "global", event["features"], event["evidence"])
                    self.store.prune(options.get("retention_days", 30), options.get("max_observations", 1000))
            except Exception:
                logger.warning("learning observation unavailable")
            finally:
                self.queue.task_done()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
        return not self.thread.is_alive()
