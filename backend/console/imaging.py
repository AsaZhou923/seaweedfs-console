"""Bounded, isolated JPEG/PNG/WebP decoding and immutable transformations."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import warnings

PIPELINE_VERSION = "pillow-v1"
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def decode(data: bytes, *, params: dict | None = None, max_bytes: int = 32 * 1024 * 1024,
           max_pixels: int = 36_000_000, timeout: float = 15.0) -> tuple[dict, bytes | None]:
    if len(data) > max_bytes:
        return {"decode_status": "resource_limited", "reason": "file_bytes"}, None
    with tempfile.TemporaryDirectory(prefix="swc-image-") as directory:
        root = Path(directory)
        source, result, output = root / "input", root / "result.json", root / "output"
        source.write_bytes(data)
        options = json.dumps({"params": params, "max_pixels": max_pixels})
        # Absolute module source also works when callers import us from repo root.
        command = [sys.executable, str(Path(__file__).resolve()), str(source), str(result), str(output), options]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            return {"decode_status": "resource_limited", "reason": "decode_timeout"}, None
        if completed.returncode != 0 or not result.exists():
            return {"decode_status": "resource_limited", "reason": "decoder_process"}, None
        properties = json.loads(result.read_text(encoding="utf-8"))
        return properties, output.read_bytes() if output.exists() else None


def _windows_memory_limit():
    """Limit committed decoder memory using a private Windows Job Object."""
    import ctypes as c
    from ctypes import wintypes as w

    class Basic(c.Structure):
        _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong), ("flags", w.DWORD),
                    ("min_working", c.c_size_t), ("max_working", c.c_size_t), ("active", w.DWORD),
                    ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

    class Extended(c.Structure):
        _fields_ = [("basic", Basic), ("io", c.c_ulonglong * 6), ("process_memory", c.c_size_t),
                    ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.GetCurrentProcess.restype = w.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    limits.process_memory = 512 * 1024 * 1024
    if not handle or not kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise OSError(c.get_last_error(), "Unable to establish decoder memory cap")
    return handle  # Kernel releases the private handle when the decoder exits.


def _child(source: str, result: str, output: str, options: dict) -> None:
    # Linux deployment additionally enforces an address-space cap in the decoder.
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    elif os.name == "nt":
        memory_job_handle = _windows_memory_limit()
    from PIL import Image, ImageOps, ImageDraw, ImageCms, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = options["max_pixels"]
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    properties: dict = {"decode_status": "corrupt"}
    try:
        with Image.open(source, formats=list(FORMATS)) as original:
            fmt = original.format
            if original.width * original.height > options["max_pixels"]:
                raise Image.DecompressionBombError("pixel budget")
            orientation = original.getexif().get(274, 1)
            # Validate the entire stream rather than trusting dimensions alone.
            original.load()
            params = options.get("params")
            orientation_policy = (params or {}).get("orientation", "auto")
            image = original.copy() if orientation_policy in ("keep", "strip") else ImageOps.exif_transpose(original)
            alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
            properties = {"decode_status": "valid", "format": fmt.lower(),
                          "mime_type": FORMATS[fmt], "width": image.width,
                          "height": image.height, "ratio": image.width / image.height,
                          "orientation": orientation, "has_alpha": alpha,
                          "exif": {"orientation": orientation}}
            if params is not None:
                image = image.convert("RGBA" if alpha else "RGB")
                alpha_policy = params.get("alpha_policy", "preserve")
                if alpha and alpha_policy == "reject":
                    raise ValueError("transparent input rejected by preset")
                if alpha and alpha_policy == "flatten":
                    base = Image.new("RGB", image.size, params.get("background", "#ffffff"))
                    base.paste(image, mask=image.getchannel("A"))
                    image, alpha = base, False
                width = int(params.get("width", params.get("long_edge", 640)))
                height = int(params.get("height", params.get("long_edge", 640)))
                if min(width, height) < 1 or max(width, height) > 8192 or width * height > options["max_pixels"]:
                    raise ValueError("output dimensions")
                fit = params.get("fit", params.get("mode", "fit"))
                if fit in ("fill", "focal"):
                    focal = params.get("focal", [0.5, 0.5])
                    if params.get("focal_point"):
                        focal = [params["focal_point"]["x"], params["focal_point"]["y"]]
                    image = ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS,
                                         centering=tuple(focal))
                else:
                    image.thumbnail((width, height), Image.Resampling.LANCZOS)
                output_format = params.get("format", "webp").upper()
                if output_format not in FORMATS:
                    raise ValueError("output format")
                icc = original.info.get("icc_profile")
                color_policy = params.get("color", params.get("color_policy", "srgb"))
                if color_policy == "srgb" and icc:
                    profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                    image = ImageCms.profileToProfile(image, profile, ImageCms.createProfile("sRGB"),
                                                     outputMode="RGBA" if alpha else "RGB")
                    icc = None
                if output_format == "JPEG":
                    background = Image.new("RGB", image.size, params.get("background", "#ffffff"))
                    if image.mode == "RGBA":
                        background.paste(image, mask=image.getchannel("A"))
                    else:
                        background.paste(image)
                    image = background
                watermark = params.get("watermark")
                if watermark:
                    text = watermark.get("text", "") if isinstance(watermark, dict) else str(watermark)
                    opacity = watermark.get("opacity", 0.35) if isinstance(watermark, dict) else 0.35
                    layer = Image.new("RGBA", image.size)
                    ImageDraw.Draw(layer).text((12, max(0, image.height - 24)), text, fill=(255, 255, 255, int(255 * opacity)))
                    image = Image.alpha_composite(image.convert("RGBA"), layer)
                    if output_format == "JPEG":
                        image = image.convert("RGB")
                save = {"quality": int(params.get("quality", 85))}
                if color_policy == "preserve" and icc:
                    save["icc_profile"] = icc
                if params.get("metadata_policy") == "keep_safe" or orientation_policy == "keep":
                    safe_exif = Image.Exif()
                    safe_exif[274] = orientation if orientation_policy == "keep" else 1
                    save["exif"] = safe_exif
                # Never copy GPS, serial numbers or source EXIF into previews.
                image.save(output, format=output_format, **save)
                properties.update(output_width=image.width, output_height=image.height,
                                  output_format=output_format.lower(), output_mime=FORMATS[output_format])
    except (Image.DecompressionBombWarning, Image.DecompressionBombError, MemoryError):
        properties = {"decode_status": "resource_limited", "reason": "pixels_or_memory"}
    except UnidentifiedImageError:
        properties = {"decode_status": "unsupported", "reason": "not_allowed_image"}
    except (OSError, ValueError, SyntaxError):
        properties = {"decode_status": "corrupt", "reason": "decode_failed"}
    Path(result).write_text(json.dumps(properties), encoding="utf-8")


if __name__ == "__main__":
    _child(*sys.argv[1:4], json.loads(sys.argv[4]))
