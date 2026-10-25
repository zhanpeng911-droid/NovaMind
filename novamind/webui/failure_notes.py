"""Stable user-facing failure notes; never echo a provider's exception text."""
from __future__ import annotations

import httpx
from openai import APIConnectionError, APITimeoutError

from novamind.core.images import ImageInputError


def failure_note(error: Exception, *, has_images: bool = False,
                 fallback_code: str = "internal_error") -> tuple[str, str]:
    if isinstance(error, ImageInputError):
        return "image_input_invalid", "图片引用无效或已失效，请重新添加图片后发送。"
    if isinstance(error, (TimeoutError, httpx.TimeoutException, APITimeoutError)):
        return "model_timeout", "模型请求超时，请稍后重试；当前输入与图片可继续使用。"
    if isinstance(error, (httpx.NetworkError, APIConnectionError)):
        return "model_connection_failed", "无法连接模型服务，请检查网络和服务地址后重试。"
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if status in (401, 403):
        return "model_auth_failed", "模型服务拒绝了访问，请检查凭据和模型访问权限。"
    if status == 429:
        return "model_rate_limited", "模型请求过于频繁或额度不足，请稍后重试或检查账户额度。"
    if status in (408, 504):
        return "model_timeout", "模型请求超时，请稍后重试。"
    if isinstance(status, int) and status >= 500:
        return "model_unavailable", "模型服务暂时不可用，请稍后重试。"
    # Only explicit machine-readable image codes justify a vision-specific note.
    upstream_code = getattr(error, "code", None)
    if has_images and isinstance(upstream_code, str) and upstream_code in {
        "unsupported_image", "vision_not_supported", "invalid_image", "image_parse_error",
    }:
        return "image_model_rejected", "模型拒绝了图片输入，请确认模型支持图片，或重新添加图片。"
    return fallback_code, "请求未能完成，请稍后重试；详细原因已记录在本机日志中。"
