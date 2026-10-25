"""Provider failures expose stable categories, not arbitrary response bodies."""
import httpx
from openai import APIConnectionError, APITimeoutError
import pytest

from novamind.core.images import ImageInputError
from novamind.webui.failure_notes import failure_note


@pytest.mark.parametrize("error,code", [
    (ImageInputError("private-path"), "image_input_invalid"),
    (httpx.ConnectError("private-address"), "model_connection_failed"),
    (APIConnectionError(request=httpx.Request("POST", "https://example.invalid")), "model_connection_failed"),
    (APITimeoutError(request=httpx.Request("POST", "https://example.invalid")), "model_timeout"),
])
def test_known_local_and_transport_categories(error, code):
    kind, note = failure_note(error, has_images=True)
    assert kind == code
    assert "private-" not in note


@pytest.mark.parametrize("upstream_code", [{"invalid": "body"}, [], None, "unknown"])
def test_malformed_provider_error_codes_cannot_break_the_error_boundary(upstream_code):
    error = RuntimeError("private-upstream-text")
    error.code = upstream_code
    kind, note = failure_note(error, has_images=True)
    assert kind == "internal_error"
    assert "private-" not in note


def test_image_rejection_requires_both_an_explicit_code_and_an_image_request():
    error = RuntimeError("private-upstream-text")
    error.code = "vision_not_supported"
    assert failure_note(error, has_images=True)[0] == "image_model_rejected"
    assert failure_note(error, has_images=False, fallback_code="quick_failed")[0] == "quick_failed"
