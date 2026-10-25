"""Personal reminders on the existing tasks.json store; no LLM needed for delivery."""
from __future__ import annotations

import calendar
import hashlib
from datetime import datetime, timedelta

from .task_store import TASKS_LOCK, load_tasks_unlocked, write_tasks_unlocked


def create(target_time: str, description: str, repeat=None, repeat_count=None,
           remind_minutes: int = 0, source_id: str = "", task_date: str | None = None) -> dict:
    if target_time:
        target = datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S")
        if target <= datetime.now():
            raise ValueError("日程时间必须晚于当前时间")
    elif task_date:
        datetime.strptime(task_date, "%Y-%m-%d")
        if repeat or remind_minutes:
            raise ValueError("待办尚无具体时间，不能设置重复或提前提醒")
    else:
        raise ValueError("请提供发生时间，或提供日期以先保存待办")
    if repeat not in (None, "", "hourly", "daily", "weekly", "monthly"):
        raise ValueError("不支持的重复频率")
    if not description.strip() or not 0 <= remind_minutes <= 525600:
        raise ValueError("事项或提前提醒时间无效")
    if repeat_count is not None and repeat_count < 1:
        raise ValueError("重复次数须为正整数")
    key = hashlib.sha256(f"{source_id}\0{description}\0{target_time}\0{task_date}\0{repeat}".encode()).hexdigest()[:16]
    with TASKS_LOCK:
        tasks = load_tasks_unlocked()
        existing = next((t for t in tasks if t["id"] == key or (
            t.get("status", "pending") == "pending" and t.get("description") == description.strip()
            and t.get("target_time") == target_time and t.get("task_date") == task_date
            and t.get("repeat") == (repeat or None))), None)
        if existing:
            return existing
        item = {"id": key, "description": description.strip(), "target_time": target_time,
                "repeat": repeat or None, "repeat_count": repeat_count,
                "remind_minutes": remind_minutes, "status": "pending", "kind": "reminder" if target_time else "todo",
                "task_date": task_date,
                "source_id": source_id, "deliveries": []}
        tasks.append(item)
        write_tasks_unlocked(tasks)
        return item


def list_tasks() -> list[dict]:
    with TASKS_LOCK:
        return sorted(load_tasks_unlocked(), key=lambda t: t.get("target_time", ""))


def change(task_id: str, changes: dict) -> dict:
    with TASKS_LOCK:
        tasks = load_tasks_unlocked()
        item = next((t for t in tasks if t["id"] == task_id), None)
        if item is None:
            raise ValueError("找不到该日程")
        if "task_date" in changes:
            datetime.strptime(changes["task_date"], "%Y-%m-%d")
        if "target_time" in changes:
            if changes["target_time"]:
                target = datetime.strptime(changes["target_time"], "%Y-%m-%d %H:%M:%S")
                if target <= datetime.now():
                    raise ValueError("日程时间必须晚于当前时间")
                changes = {**changes, "status": "pending", "kind": "reminder"}
            else:
                datetime.strptime(changes.get("task_date") or item.get("task_date") or "", "%Y-%m-%d")
                changes = {**changes, "status": "pending", "kind": "todo", "repeat": None, "remind_minutes": 0}
        item.update(changes)
        write_tasks_unlocked(tasks)
        return dict(item)


def delete(task_id: str) -> None:
    """Idempotently remove the item and its reminder deliveries under one lock."""
    with TASKS_LOCK:
        tasks = load_tasks_unlocked()
        remaining = [item for item in tasks if item["id"] != task_id]
        if len(remaining) != len(tasks):
            write_tasks_unlocked(remaining)


def _next_time(value: datetime, repeat: str) -> datetime:
    if repeat == "monthly":
        month = value.month % 12 + 1
        year = value.year + (value.month == 12)
        return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))
    return value + {"hourly": timedelta(hours=1), "daily": timedelta(days=1),
                    "weekly": timedelta(days=7)}[repeat]


def due_reminders(*, include_legacy: bool = True) -> list[dict]:
    """Persist claim before delivery. Unread cards survive restart even if a toast fails."""
    now = datetime.now()
    due = []
    changed = False
    with TASKS_LOCK:
        tasks = load_tasks_unlocked()
        for item in tasks:
            if not include_legacy and item.get("kind") != "reminder":
                continue
            if item.get("kind") == "todo":
                continue
            if item.get("status", "pending") in {"cancelled", "completed", "notified"}:
                continue
            try:
                target = datetime.strptime(item["target_time"], "%Y-%m-%d %H:%M:%S")
                remind_at = target - timedelta(minutes=item.get("remind_minutes", 0))
                if now < remind_at:
                    continue
                delivery = {"time": item["target_time"], "read": False,
                            "missed": now > target + timedelta(minutes=1), "shown": False}
                deliveries = item.setdefault("deliveries", [])
                if not any(d["time"] == delivery["time"] for d in deliveries):
                    deliveries.append(delivery)
                    due.append({**item, "delivery": delivery})
                changed = True
                repeat = item.get("repeat")
                count = item.get("repeat_count")
                if repeat and (count is None or count > 1):
                    next_time = _next_time(target, repeat)
                    # Skip missed occurrences without a notification storm.
                    consumed = 1
                    while next_time - timedelta(minutes=item.get("remind_minutes", 0)) <= now:
                        next_time = _next_time(next_time, repeat)
                        consumed += 1
                    item["target_time"] = next_time.strftime("%Y-%m-%d %H:%M:%S")
                    if count is not None:
                        item["repeat_count"] = max(0, count - consumed)
                        if item["repeat_count"] == 0:
                            item["status"] = "notified"
                else:
                    item["status"] = "notified"
            except (ValueError, KeyError, TypeError):
                continue
        if changed:
            write_tasks_unlocked(tasks)
    return due


def mark_notification(task_id: str, occurrence: str, accepted: bool) -> None:
    """Native acceptance is not a read receipt; unread remains until user action."""
    with TASKS_LOCK:
        tasks = load_tasks_unlocked()
        for task in tasks:
            if task["id"] == task_id:
                for delivery in task.get("deliveries", []):
                    if delivery["time"] == occurrence:
                        delivery["shown"] = accepted
        write_tasks_unlocked(tasks)


def acknowledge(task_id: str) -> None:
    with TASKS_LOCK:
        tasks = load_tasks_unlocked()
        for item in tasks:
            if item["id"] == task_id:
                for delivery in item.get("deliveries", []):
                    delivery["read"] = True
        write_tasks_unlocked(tasks)
