from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "diputetacam" / "config.toml"


DEFAULT_CONFIG_TOML = """\
# Diputetacam configuration.
# Changes are read when the daemon starts. Restart the daemon after editing.

[output]
# The v4l2loopback device created by the kernel module.
device = "/dev/video10"
width = 1280
height = 720
fps = 30
format = "YUY2"

[camera]
# Empty means: choose the first V4L2 camera found.
device = ""
width = 1280
height = 720
fps = 30

[buffer]
# How much of the composed output to keep in memory.
seconds = 20
min_loop_seconds = 15
max_loop_seconds = 20
jpeg_quality = 80
max_memory_mb = 256
# Mean absolute difference (0-255) between 32x18 luma thumbnails.
# Lower is stricter. If no frame is below this, use the full buffer.
match_threshold = 24.0

[transition]
# How long the current frame stays frozen before the cut.
freeze_ms = 150

[startup]
# false: start in standby (camera released). Run `diputetacam live` to start.
# true: start the camera and ring buffer as soon as the daemon starts.
live = false
"""


@dataclass
class OutputConfig:
    device: str = "/dev/video10"
    width: int = 1280
    height: int = 720
    fps: int = 30
    format: str = "YUY2"


@dataclass
class CameraConfig:
    device: str = ""
    width: int = 1280
    height: int = 720
    fps: int = 30


@dataclass
class BufferConfig:
    seconds: float = 20.0
    min_loop_seconds: float = 15.0
    max_loop_seconds: float = 20.0
    jpeg_quality: int = 80
    max_memory_mb: int = 256
    match_threshold: float = 24.0


@dataclass
class TransitionConfig:
    freeze_ms: int = 150


@dataclass
class StartupConfig:
    live: bool = False


@dataclass
class Config:
    output: OutputConfig = field(default_factory=OutputConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    buffer: BufferConfig = field(default_factory=BufferConfig)
    transition: TransitionConfig = field(default_factory=TransitionConfig)
    startup: StartupConfig = field(default_factory=StartupConfig)

    def validate(self) -> None:
        if self.output.width < 320 or self.output.height < 240:
            raise ConfigError("output width/height must be at least 320x240")
        if not 1 <= self.output.fps <= 60:
            raise ConfigError("output fps must be between 1 and 60")
        if self.output.format not in {"YUY2", "UYVY", "NV12", "I420"}:
            raise ConfigError("output format must be one of YUY2, UYVY, NV12, I420")
        if self.buffer.seconds < 2:
            raise ConfigError("buffer.seconds must be at least 2")
        if self.buffer.min_loop_seconds < 1:
            raise ConfigError("buffer.min_loop_seconds must be at least 1")
        if self.buffer.max_loop_seconds < self.buffer.min_loop_seconds:
            raise ConfigError("buffer.max_loop_seconds must be >= min_loop_seconds")
        if self.buffer.max_loop_seconds > self.buffer.seconds:
            raise ConfigError("buffer.max_loop_seconds must be <= buffer.seconds")
        if not 1 <= self.buffer.jpeg_quality <= 100:
            raise ConfigError("buffer.jpeg_quality must be between 1 and 100")
        if self.buffer.max_memory_mb < 16:
            raise ConfigError("buffer.max_memory_mb must be at least 16")
        if not 0 <= self.transition.freeze_ms <= 2000:
            raise ConfigError("transition.freeze_ms must be between 0 and 2000")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _get(section: dict[str, Any], key: str, default: Any, typ: type) -> Any:
    value = section.get(key, default)
    if typ is float and isinstance(value, int):
        return float(value)
    if not isinstance(value, typ):
        raise ConfigError(f"{key!r} must be {typ.__name__}")
    return value


def config_from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    out = _section(data, "output")
    cfg.output = OutputConfig(
        device=_get(out, "device", cfg.output.device, str),
        width=_get(out, "width", cfg.output.width, int),
        height=_get(out, "height", cfg.output.height, int),
        fps=_get(out, "fps", cfg.output.fps, int),
        format=_get(out, "format", cfg.output.format, str),
    )
    cam = _section(data, "camera")
    cfg.camera = CameraConfig(
        device=_get(cam, "device", cfg.camera.device, str),
        width=_get(cam, "width", cfg.camera.width, int),
        height=_get(cam, "height", cfg.camera.height, int),
        fps=_get(cam, "fps", cfg.camera.fps, int),
    )
    buf = _section(data, "buffer")
    cfg.buffer = BufferConfig(
        seconds=_get(buf, "seconds", cfg.buffer.seconds, float),
        min_loop_seconds=_get(buf, "min_loop_seconds", cfg.buffer.min_loop_seconds, float),
        max_loop_seconds=_get(buf, "max_loop_seconds", cfg.buffer.max_loop_seconds, float),
        jpeg_quality=_get(buf, "jpeg_quality", cfg.buffer.jpeg_quality, int),
        max_memory_mb=_get(buf, "max_memory_mb", cfg.buffer.max_memory_mb, int),
        match_threshold=_get(buf, "match_threshold", cfg.buffer.match_threshold, float),
    )
    trans = _section(data, "transition")
    cfg.transition = TransitionConfig(
        freeze_ms=_get(trans, "freeze_ms", cfg.transition.freeze_ms, int),
    )
    startup = _section(data, "startup")
    cfg.startup = StartupConfig(
        live=_get(startup, "live", cfg.startup.live, bool),
    )
    cfg.validate()
    return cfg


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("DIPUTETACAM_CONFIG") or config_path())
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a TOML table")
    return config_from_dict(data)


def ensure_config(path: str | Path | None = None) -> Path:
    path = Path(path or os.environ.get("DIPUTETACAM_CONFIG") or config_path())
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return path
