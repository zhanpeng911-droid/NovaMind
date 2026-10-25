"""Workspace-owned images. Stored references stay small; only model input gets data URLs."""
from __future__ import annotations

import base64
import copy
import io
import re
import uuid
import warnings
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

from novamind.core import config

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
PREFIX = "novamind-image:"
_lock = threading.RLock()


class ImageInputError(ValueError):
    """A local image reference or supported-input constraint was not satisfied."""


def root() -> Path:
    path = Path(config.WORKSPACE_DIR) / "attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def image_path(image_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", image_id):
        raise ImageInputError("图片标识无效")
    return root() / (image_id + ".jpg")


def save_image(data: bytes) -> dict:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("每张图片上限 10 MiB")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
                raise ValueError("支持 PNG、JPEG 和静态 WebP 图片")
            if source.width * source.height > MAX_PIXELS:
                raise ValueError("图片超过 2000 万像素，请缩小后再添加")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((2560, 2560))
            image_id = uuid.uuid4().hex
            image.save(image_path(image_id), "JPEG", quality=92)
            image_path(image_id).with_suffix(".draft").touch()
    return {"id": image_id, "url": f"/attachments/images/{image_id}",
            "width": image.width, "height": image.height}


def message_content(text: str, images: list[str]):
    if not images:
        return text
    if len(images) > 3:
        raise ImageInputError("每条消息最多 3 张图片")
    for image_id in images:
        if not image_path(image_id).is_file():
            raise ImageInputError("图片不存在，请重新添加")
    claim_images(images)
    return [{"type": "text", "text": text or "请描述这张图片，并结合对话回答。"}] + [
        {"type": "image_url", "image_url": {"url": PREFIX + image_id}} for image_id in images
    ]


def claim_images(images: list[str]) -> None:
    with _lock:
        for image_id in images:
            image_path(image_id).with_suffix(".draft").unlink(missing_ok=True)


def clean_stale_drafts() -> None:
    """Only abandoned uploads expire. Sent images and saved icons are never swept."""
    with _lock:
        cutoff = time.time() - 7 * 86400
        for marker in root().glob("*.draft"):
            if re.fullmatch(r"[a-f0-9]{32}", marker.stem) and marker.stat().st_mtime < cutoff:
                image_path(marker.stem).unlink(missing_ok=True)
                marker.unlink(missing_ok=True)


def resolve_messages(messages):
    resolved = []
    for message in messages:
        if not isinstance(message.content, list):
            resolved.append(message)
            continue
        msg = copy.deepcopy(message)
        for block in msg.content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            url = block.get("image_url", {}).get("url", "")
            if url.startswith(PREFIX):
                path = image_path(url[len(PREFIX):])
                if not path.exists():
                    raise ImageInputError("历史图片已清理，请重新添加")
                block["image_url"]["url"] = "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()
        resolved.append(msg)
    return resolved


def image_ids(content) -> list[str]:
    if not isinstance(content, list):
        return []
    return [b["image_url"]["url"][len(PREFIX):] for b in content
            if isinstance(b, dict) and b.get("type") == "image_url"
            and b.get("image_url", {}).get("url", "").startswith(PREFIX)]
