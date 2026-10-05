from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.environ["PYTHONPATH"] = str(BACKEND)

from console import imaging  # noqa: E402


def _image_bytes(
    fmt: str,
    size: tuple[int, int] = (4, 3),
    mode: str = "RGB",
    color: tuple[int, ...] = (32, 96, 160),
    *,
    exif: Image.Exif | None = None,
) -> bytes:
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    save_kwargs = {"format": fmt}
    if exif is not None:
        save_kwargs["exif"] = exif
    image.save(buffer, **save_kwargs)
    return buffer.getvalue()


def _oriented_jpeg_bytes() -> bytes:
    exif = Image.Exif()
    exif[274] = 6
    exif[34853] = {
        1: "N",
        2: (Fraction(35, 1), Fraction(0, 1), Fraction(0, 1)),
        3: "E",
        4: (Fraction(139, 1), Fraction(0, 1), Fraction(0, 1)),
    }
    return _image_bytes("JPEG", size=(4, 2), exif=exif)


def _corrupt_png_bytes() -> bytes:
    data = bytearray(_image_bytes("PNG"))
    position = 8
    while position < len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        chunk_type = bytes(data[position + 4:position + 8])
        if chunk_type == b"IDAT":
            data[position + 8] ^= 0xFF
            return bytes(data)
        position += 12 + length
    raise AssertionError("generated PNG should contain an IDAT chunk")


def test_decode_returns_valid_metadata_for_normal_jpeg() -> None:
    properties, output = imaging.decode(_image_bytes("JPEG"))

    assert properties["decode_status"] == "valid"
    assert properties["format"] == "jpeg"
    assert properties["mime_type"] == "image/jpeg"
    assert properties["width"] == 4
    assert properties["height"] == 3
    assert output is None


def test_decode_reports_alpha_for_transparent_png() -> None:
    properties, output = imaging.decode(_image_bytes("PNG", mode="RGBA", color=(1, 2, 3, 0)))

    assert properties["decode_status"] == "valid"
    assert properties["format"] == "png"
    assert properties["mime_type"] == "image/png"
    assert properties["has_alpha"] is True
    assert output is None


def test_decode_uses_content_type_for_webp_bytes_with_fake_extension() -> None:
    properties, output = imaging.decode(_image_bytes("WEBP"))

    assert properties["decode_status"] == "valid"
    assert properties["format"] == "webp"
    assert properties["mime_type"] == "image/webp"
    assert output is None


def test_decode_marks_corrupt_png_as_corrupt() -> None:
    properties, output = imaging.decode(_corrupt_png_bytes())

    assert properties["decode_status"] == "corrupt"
    assert properties["reason"] == "decode_failed"
    assert output is None


def test_decode_rejects_images_that_exceed_pixel_budget() -> None:
    properties, output = imaging.decode(_image_bytes("PNG", size=(3, 3)), max_pixels=8)

    assert properties["decode_status"] == "resource_limited"
    assert properties["reason"] == "pixels_or_memory"
    assert output is None


def test_decode_rejects_payloads_that_exceed_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_spawned(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("decoder subprocess should not be spawned for oversized payloads")

    monkeypatch.setattr(imaging.subprocess, "run", fail_if_spawned)

    properties, output = imaging.decode(b"too large", max_bytes=4)

    assert properties["decode_status"] == "resource_limited"
    assert properties["reason"] == "file_bytes"
    assert output is None


def test_decode_reports_timeout_when_decoder_process_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=["decoder"], timeout=0.01)

    monkeypatch.setattr(imaging.subprocess, "run", raise_timeout)

    properties, output = imaging.decode(_image_bytes("JPEG"), timeout=0.01)

    assert properties["decode_status"] == "resource_limited"
    assert properties["reason"] == "decode_timeout"
    assert output is None


def test_decode_reports_decoder_process_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def return_failure(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=["decoder"], returncode=2, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(imaging.subprocess, "run", return_failure)

    properties, output = imaging.decode(_image_bytes("JPEG"))

    assert properties["decode_status"] == "resource_limited"
    assert properties["reason"] == "decoder_process"
    assert output is None


def test_decode_subprocess_imports_console_module_when_cwd_is_not_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    properties, output = imaging.decode(_image_bytes("JPEG"))

    assert properties["decode_status"] == "valid"
    assert properties["mime_type"] == "image/jpeg"
    assert output is None


def test_decode_transposes_oriented_jpeg_and_writes_preview_without_exif() -> None:
    properties, output = imaging.decode(
        _oriented_jpeg_bytes(),
        params={"width": 2, "height": 4, "fit": "fill", "format": "webp", "quality": 80},
    )

    assert properties["decode_status"] == "valid"
    assert properties["orientation"] == 6
    assert properties["width"] == 2
    assert properties["height"] == 4
    assert properties["output_width"] == 2
    assert properties["output_height"] == 4
    assert properties["output_mime"] == "image/webp"
    assert output is not None
    with Image.open(io.BytesIO(output)) as preview:
        assert preview.size == (2, 4)
        assert not preview.getexif()


def test_decode_fill_outputs_exact_requested_dimensions() -> None:
    properties, output = imaging.decode(
        _image_bytes("JPEG", size=(20, 10)),
        params={"width": 5, "height": 5, "mode": "fill", "format": "webp", "quality": 80},
    )

    assert properties["decode_status"] == "valid"
    assert properties["output_width"] == 5
    assert properties["output_height"] == 5
    assert output is not None
    with Image.open(io.BytesIO(output)) as preview:
        assert preview.size == (5, 5)


def test_decode_flattens_alpha_or_rejects_transparent_inputs_by_policy() -> None:
    transparent = _image_bytes("PNG", size=(6, 4), mode="RGBA", color=(0, 200, 0, 80))

    flattened, flattened_output = imaging.decode(
        transparent,
        params={"width": 6, "height": 4, "format": "jpeg", "alpha_policy": "flatten", "background": "#ffffff"},
    )
    rejected, rejected_output = imaging.decode(
        transparent,
        params={"width": 6, "height": 4, "format": "webp", "alpha_policy": "reject"},
    )

    assert flattened["decode_status"] == "valid"
    assert flattened["output_mime"] == "image/jpeg"
    assert flattened_output is not None
    with Image.open(io.BytesIO(flattened_output)) as preview:
        assert preview.mode == "RGB"
        assert preview.size == (6, 4)
    assert rejected == {"decode_status": "corrupt", "reason": "decode_failed"}
    assert rejected_output is None


def test_decode_keep_safe_metadata_preserves_orientation_without_gps() -> None:
    properties, output = imaging.decode(
        _oriented_jpeg_bytes(),
        params={"width": 4, "height": 2, "format": "jpeg", "metadata_policy": "keep_safe", "orientation": "keep"},
    )

    assert properties["decode_status"] == "valid"
    assert output is not None
    with Image.open(io.BytesIO(output)) as preview:
        exif = preview.getexif()
        assert exif.get(274) == 6
        assert exif.get(34853) is None


def test_decode_watermark_changes_output_bytes_and_keeps_dimensions() -> None:
    source = _image_bytes("JPEG", size=(32, 24))
    params = {"width": 32, "height": 24, "format": "png", "quality": 90}
    plain, plain_output = imaging.decode(source, params=params)
    watermarked, watermarked_output = imaging.decode(source, params=params | {"watermark": {"text": "SWC", "opacity": 0.7}})

    assert plain["decode_status"] == "valid"
    assert watermarked["decode_status"] == "valid"
    assert plain_output is not None
    assert watermarked_output is not None
    assert hashlib.sha256(plain_output).hexdigest() != hashlib.sha256(watermarked_output).hexdigest()
    with Image.open(io.BytesIO(watermarked_output)) as preview:
        assert preview.size == (32, 24)


def test_duplicate_image_bytes_have_same_checksum() -> None:
    first = _image_bytes("PNG", size=(2, 2), mode="RGBA", color=(9, 8, 7, 6))
    second = bytes(first)

    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
