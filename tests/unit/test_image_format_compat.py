"""Image-library upgrade checks through the actual workspace upload helper."""
import io

from PIL import Image
import pytest

from novamind.core import config, images


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_supported_formats_still_normalize_to_workspace_jpeg(tmp_path, monkeypatch, format):
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    buffer = io.BytesIO()
    Image.new("RGB", (80, 40), (200, 80, 40)).save(buffer, format)
    saved = images.save_image(buffer.getvalue())
    assert (saved["width"], saved["height"]) == (80, 40)
    with Image.open(images.image_path(saved["id"])) as result:
        assert result.format == "JPEG"
        assert result.mode == "RGB"
        assert result.size == (80, 40)
        assert all(abs(actual - expected) < 10
                   for actual, expected in zip(result.getpixel((20, 20)), (200, 80, 40), strict=True))


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_exif_orientation_is_applied_before_saving_without_retaining_the_rotation(tmp_path, monkeypatch, format):
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    source = Image.new("RGB", (80, 40), "#527cc8")
    exif = Image.Exif()
    exif[274] = 6  # Rotate 90 degrees: the uploaded image must not appear sideways.
    buffer = io.BytesIO()
    source.save(buffer, format, exif=exif)
    saved = images.save_image(buffer.getvalue())
    assert (saved["width"], saved["height"]) == (40, 80)
    with Image.open(images.image_path(saved["id"])) as result:
        assert result.size == (40, 80)
        assert result.getexif().get(274) is None
