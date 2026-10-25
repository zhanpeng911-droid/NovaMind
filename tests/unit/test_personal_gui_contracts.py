"""Offline contracts behind the preserved GUI controls; only temporary data."""
from __future__ import annotations

import io
import os
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from PIL import Image
import pytest

from novamind.core import config, images, schedules, task_store
from novamind.webui.server import app


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "TASKS_FILE", str(tmp_path / "tasks.json"))
    return TestClient(app)


def picture(format="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (24, 12), "#527cc8").save(buffer, format)
    return buffer.getvalue()


def future(hours=2):
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def test_settings_roundtrip_and_partial_desktop_contract(ui):
    assert ui.put("/personal/theme", json={"theme": "dark"}).status_code == 200
    assert ui.put("/personal/settings/style", json={"preset": "warm", "custom": "先给结论"}).status_code == 200
    assert ui.put("/personal/settings/desktop", json={"bubble_x": 240, "bubble_y": 100, "bubble_visible": False}).status_code == 200
    assert ui.put("/personal/settings/desktop", json={"size": 40, "opacity": .75, "icon": "spark"}).status_code == 200
    saved = ui.get("/personal/settings").json()
    assert saved["appearance"]["theme"] == "dark" and saved["style"]["preset"] == "warm"
    assert saved["desktop"]["bubble_x"] == 240 and not saved["desktop"]["bubble_visible"]
    assert saved["desktop"]["size"] == 40


@pytest.mark.parametrize("body", [{"hotkey": "A"}, {"size": 20}, {"opacity": .1},
                                  {"icon": "../private"}, {"unknown": True}])
def test_desktop_rejects_invalid_control_values(ui, body):
    assert ui.put("/personal/settings/desktop", json=body).status_code == 422


def test_custom_icon_upload_read_and_claim(ui):
    uploaded = ui.post("/attachments/images", content=picture(), headers={"Content-Type": "image/png"})
    assert uploaded.status_code == 200
    image_id = uploaded.json()["id"]
    assert images.image_path(image_id).with_suffix(".draft").exists()
    response = ui.get(uploaded.json()["url"])
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert ui.put("/personal/settings/desktop", json={"icon": image_id}).status_code == 200
    assert not images.image_path(image_id).with_suffix(".draft").exists()
    assert ui.put("/personal/settings/desktop", json={"icon": "a"*32}).status_code == 400


@pytest.mark.parametrize("data", [b"", b"not an image", b"<svg></svg>", picture("GIF")])
def test_invalid_images_return_safe_error(ui, data):
    assert ui.post("/attachments/images", content=data).status_code == 422


def test_image_payload_limits_and_missing_references(ui, monkeypatch):
    monkeypatch.setattr(images, "MAX_BYTES", 4)
    assert ui.post("/attachments/images", content=b"12345").status_code == 422
    assert ui.get("/attachments/images/invalid-id").status_code == 404
    assert ui.get("/attachments/images/" + "b"*32).status_code == 404
    with pytest.raises(ValueError, match="最多 3"):
        images.message_content("x", ["b"*32]*4)
    with pytest.raises(ValueError, match="不存在"):
        images.message_content("x", ["b"*32])


def test_image_resolution_is_copy_on_write_and_cleanup_keeps_claimed_images(ui):
    first = images.save_image(picture())["id"]
    abandoned = images.save_image(picture())["id"]
    content = images.message_content("", [first])
    message = HumanMessage(content=content)
    result = images.resolve_messages([HumanMessage(content="plain"), message])
    assert images.image_ids(message.content) == [first]
    assert result[1].content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert message.content[1]["image_url"]["url"] == images.PREFIX + first
    assert images.image_ids("plain") == []
    assert images.message_content("hello", []) == "hello"
    marker = images.image_path(abandoned).with_suffix(".draft")
    os.utime(marker, (1, 1))
    images.clean_stale_drafts()
    assert not images.image_path(abandoned).exists() and images.image_path(first).exists()
    images.image_path(first).unlink()
    with pytest.raises(ValueError, match="历史图片"):
        images.resolve_messages([message])


def test_schedule_actions_remain_available_after_menu_refinement(ui):
    body = {"description": "离线回归事项", "target_time": future(), "repeat": "daily", "repeat_count": 3, "remind_minutes": 15}
    task = ui.post("/personal/schedules", json=body).json()
    assert ui.post("/personal/schedules", json=body).json()["id"] == task["id"]
    changed = ui.patch("/personal/schedules/" + task["id"], json={"target_time": future(3), "description": "已修改"})
    assert changed.status_code == 200 and changed.json()["description"] == "已修改"
    completed = ui.patch("/personal/schedules/" + task["id"], json={"status": "completed"})
    assert completed.json()["status"] == "completed"
    assert ui.post("/personal/schedules/" + task["id"] + "/read").status_code == 200
    listed = ui.get("/personal/schedules").json()["tasks"]
    assert len(listed) == 1 and listed[0]["repeat_count"] == 3
    assert ui.patch("/personal/schedules/missing", json={"status": "cancelled"}).status_code == 422


@pytest.mark.parametrize("body", [{"description": "x"}, {"description": "x", "target_time": "invalid"},
    {"description": "x", "target_time": "2001-01-01 08:00:00"},
    {"description": "x", "task_date": "2030-01-01", "repeat": "daily"}])
def test_schedule_validation_never_claims_success(ui, body):
    assert ui.post("/personal/schedules", json=body).status_code == 422
    assert ui.get("/personal/schedules").json()["tasks"] == []


def test_missed_reminder_claim_is_persistent_and_read_action_is_explicit(ui):
    due = datetime.now() - timedelta(minutes=3)
    task_store.write_tasks_unlocked([{
        "id": "past", "description": "错过的事项", "target_time": due.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": "reminder", "status": "pending", "deliveries": [],
    }])
    reminders = schedules.due_reminders()
    assert len(reminders) == 1 and reminders[0]["delivery"]["missed"]
    assert schedules.due_reminders() == []
    schedules.mark_notification("past", reminders[0]["delivery"]["time"], False)
    assert not schedules.list_tasks()[0]["deliveries"][0]["read"]
    assert ui.post("/personal/schedules/past/read").status_code == 200
    assert schedules.list_tasks()[0]["deliveries"][0]["read"]


def test_date_only_todo_does_not_fire_and_monthly_repeat_clamps_day(ui):
    task = ui.post("/personal/schedules", json={"description": "待确认时刻", "task_date": "2030-01-01"}).json()
    assert task["kind"] == "todo" and schedules.due_reminders() == []
    assert schedules._next_time(datetime(2028, 1, 31, 8), "monthly") == datetime(2028, 2, 29, 8)
    assert schedules._next_time(datetime(2026, 12, 31, 8), "monthly") == datetime(2027, 1, 31, 8)
