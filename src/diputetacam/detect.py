from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


@dataclass(frozen=True)
class Camera:
    path: str
    name: str


@dataclass(frozen=True)
class CameraFormat:
    pixelformat: str
    width: int
    height: int
    fps: float

    @property
    def label(self) -> str:
        return f"{self.pixelformat} {self.width}x{self.height}@{self.fps:g}"


def _props_string(props, key: str) -> str | None:
    if props is None:
        return None
    try:
        value = props.get_string(key)
    except Exception:
        value = None
    return value or None


def list_cameras() -> list[Camera]:
    """List V4L2 capture cameras, excluding virtual/loopback devices."""
    if not Gst.is_initialized():
        Gst.init(None)

    cameras: dict[str, Camera] = {}
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", Gst.Caps.new_empty_simple("video/x-raw"))
    monitor.start()
    try:
        for device in monitor.get_devices():
            props = device.get_properties()
            if _props_string(props, "device.api") != "v4l2":
                continue
            path = _props_string(props, "api.v4l2.path")
            if not path or path in cameras:
                continue
            name = (
                _props_string(props, "api.v4l2.cap.card")
                or _props_string(props, "node.nick")
                or device.get_display_name()
                or path
            )
            if "virtual" in name.lower() or "loopback" in name.lower():
                continue
            cameras[path] = Camera(path=path, name=name)
    finally:
        monitor.stop()

    if cameras:
        return sorted(cameras.values(), key=lambda camera: camera.path)

    # Fallback for systems where the PipeWire device provider is unavailable.
    for sysdir in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            index = int((sysdir / "index").read_text().strip())
        except (OSError, ValueError):
            index = 0
        if index != 0:
            continue
        try:
            name = (sysdir / "name").read_text().strip()
        except OSError:
            name = sysdir.name
        path = f"/dev/{sysdir.name}"
        if "virtual" in name.lower() or "loopback" in name.lower():
            continue
        cameras[path] = Camera(path=path, name=name)
    return sorted(cameras.values(), key=lambda camera: camera.path)


_FORMAT_RE = re.compile(r"^\s*\[\d+\]:\s*'([^']+)'")
_SIZE_RE = re.compile(r"^\s*Size:\s*Discrete\s+(\d+)x(\d+)")
_INTERVAL_RE = re.compile(r"^\s*Interval:\s*Discrete\s+[^()]*\(([\d.]+)\s*fps\)")


def list_formats(device: str) -> list[CameraFormat]:
    """Parse `v4l2-ctl --list-formats-ext` output."""
    tool = shutil.which("v4l2-ctl")
    if not tool:
        return []
    try:
        result = subprocess.run(
            [tool, "--list-formats-ext", "-d", device],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    formats: list[CameraFormat] = []
    pixelformat: str | None = None
    width = height = 0
    for line in result.stdout.splitlines():
        if match := _FORMAT_RE.match(line):
            pixelformat = match.group(1)
            width = height = 0
            continue
        if match := _SIZE_RE.match(line):
            width, height = int(match.group(1)), int(match.group(2))
            continue
        if match := _INTERVAL_RE.match(line):
            if pixelformat and width and height:
                formats.append(
                    CameraFormat(pixelformat, width, height, float(match.group(1)))
                )
    return formats


def choose_format(
    formats: list[CameraFormat],
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
) -> CameraFormat | None:
    if not formats:
        return None

    preference = {"MJPG": 0, "JPEG": 0, "YUYV": 1, "NV12": 2, "H264": 3}

    def score(fmt: CameraFormat) -> tuple[int, int, float, int]:
        exact = fmt.width == width and fmt.height == height
        enough = fmt.width >= width and fmt.height >= height
        if exact and fmt.fps + 0.01 >= fps:
            size_score = 0
        elif enough and fmt.fps + 0.01 >= fps:
            size_score = 1
        elif exact:
            size_score = 2
        elif fmt.width >= 640 and fmt.height >= 480:
            size_score = 3
        else:
            size_score = 4
        return (size_score, preference.get(fmt.pixelformat, 9), -fmt.fps, -(fmt.width * fmt.height))

    return min(formats, key=score)


def fps_fraction(fps: float) -> str:
    if fps <= 0:
        return "30/1"
    fraction = Fraction(fps).limit_denominator(1001)
    return f"{fraction.numerator}/{fraction.denominator}"


def format_caps(fmt: CameraFormat) -> tuple[str, str | None] | None:
    """Return (caps, decoder) for GStreamer, or None for decodebin fallback."""
    rate = fps_fraction(fmt.fps)
    pixelformat = fmt.pixelformat.upper()
    if pixelformat in {"MJPG", "JPEG"}:
        return (f"image/jpeg,width={fmt.width},height={fmt.height},framerate={rate}", "jpegdec")
    if pixelformat == "YUYV":
        return (
            f"video/x-raw,format=YUY2,width={fmt.width},height={fmt.height},framerate={rate}",
            None,
        )
    if pixelformat == "NV12":
        return (
            f"video/x-raw,format=NV12,width={fmt.width},height={fmt.height},framerate={rate}",
            None,
        )
    if pixelformat in {"H264", "H265", "HEVC"}:
        return (
            f"video/x-{'h264' if pixelformat == 'H264' else 'h265'},"
            f"width={fmt.width},height={fmt.height},framerate={rate}",
            "decodebin",
        )
    if pixelformat in {"GREY", "Y8"}:
        return (
            f"video/x-raw,format=GRAY8,width={fmt.width},height={fmt.height},framerate={rate}",
            None,
        )
    return None
