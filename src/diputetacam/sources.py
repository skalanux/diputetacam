from __future__ import annotations

import itertools
import logging
from collections.abc import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from .config import Config
from .detect import (
    CameraFormat,
    choose_format,
    format_caps,
    list_cameras,
    list_formats,
)

log = logging.getLogger(__name__)

_ids = itertools.count(1)


class SourceError(RuntimeError):
    pass


def _parse_bin(description: str, name: str) -> Gst.Bin:
    try:
        element = Gst.parse_bin_from_description(description, True)
    except GLib.Error as exc:
        raise SourceError(f"cannot build {name}: {exc.message}") from exc
    if element is None:
        raise SourceError(f"cannot build {name}")
    bin_ = element  # Gst.Bin
    bin_.set_name(f"{name}_{next(_ids)}")
    return bin_


class Source:
    """A source bin with a single 'src' ghost pad."""

    def __init__(self, bin_: Gst.Bin, name: str) -> None:
        self.bin = bin_
        self.name = name
        self._pipeline: Gst.Pipeline | None = None
        self._probe_id: int | None = None
        self._ready = False
        self._stopped = False
        self._on_ready: Callable[[Source], None] | None = None

    @property
    def src_pad(self) -> Gst.Pad:
        pad = self.bin.get_static_pad("src")
        if pad is None:
            raise SourceError(f"{self.name} has no src pad")
        return pad

    @property
    def ready(self) -> bool:
        return self._ready

    def on_ready(self, callback: Callable[[Source], None]) -> None:
        self._on_ready = callback
        if self._ready:
            GLib.idle_add(callback, self)

    def prepare(self, pipeline: Gst.Pipeline) -> None:
        """Add the bin to the pipeline so pads can be linked across the hierarchy."""
        if self._pipeline is None:
            self._pipeline = pipeline
            pipeline.add(self.bin)
            log.debug("prepared source %s", self.name)

    def start(self, pipeline: Gst.Pipeline) -> None:
        if self._pipeline is None:
            self.prepare(pipeline)
        if self._ready or self._stopped:
            return
        self._stopped = False
        self._probe_id = self.src_pad.add_probe(Gst.PadProbeType.BUFFER, self._probe, None)
        self.bin.sync_state_with_parent()
        log.debug("started source %s", self.name)

    def _probe(self, pad: Gst.Pad, info: Gst.PadProbeInfo, data: object) -> Gst.PadProbeReturn:
        if self._stopped or self._ready:
            return Gst.PadProbeReturn.REMOVE
        self._ready = True
        self._probe_id = None
        if self._on_ready is not None:
            GLib.idle_add(self._on_ready, self)
        return Gst.PadProbeReturn.REMOVE

    def stop(self) -> None:
        self._stopped = True
        if self._probe_id is not None:
            try:
                self.src_pad.remove_probe(self._probe_id)
            except Exception:
                pass
            self._probe_id = None
        if self._pipeline is not None:
            try:
                self.bin.set_state(Gst.State.NULL)
                self._pipeline.remove(self.bin)
            finally:
                self._pipeline = None
        log.debug("stopped source %s", self.name)

    def unlink(self) -> None:
        try:
            self.src_pad.unlink(self.src_pad.get_peer())
        except Exception:
            pass


class SimpleSource(Source):
    def __init__(self, description: str, name: str) -> None:
        super().__init__(_parse_bin(description, name), name)


class TestSource(SimpleSource):
    def __init__(self, width: int = 1280, height: int = 720, fps: int = 30) -> None:
        description = (
            f"videotestsrc is-live=true pattern=ball do-timestamp=true ! "
            f"video/x-raw,width={width},height={height},framerate={fps}/1 ! "
            f"queue max-size-buffers=2 leaky=downstream"
        )
        super().__init__(description, "test-source")


class StandbySource(SimpleSource):
    def __init__(self, width: int = 1280, height: int = 720, fps: int = 30) -> None:
        description = (
            f"videotestsrc is-live=true pattern=black do-timestamp=true ! "
            f"video/x-raw,width={width},height={height},framerate={fps}/1 ! "
            f'textoverlay text="Cámara apagada" font-desc="Sans 42" '
            f"halignment=center valignment=center shaded-background=true "
            f"wait-text=false ! "
            f"videoconvert ! videoscale ! videorate ! "
            f"video/x-raw,width={width},height={height},framerate={fps}/1 ! "
            f"queue max-size-buffers=2 leaky=downstream"
        )
        try:
            super().__init__(description, "standby-source")
        except SourceError:
            # Some GStreamer builds lack textoverlay/pango. A black card is
            # still better than no output at all.
            log.warning("textoverlay unavailable; using a plain black standby card")
            super().__init__(
                f"videotestsrc is-live=true pattern=black do-timestamp=true ! "
                f"video/x-raw,width={width},height={height},framerate={fps}/1 ! "
                f"queue max-size-buffers=2 leaky=downstream",
                "standby-source",
            )


class CameraSource(Source):
    def __init__(self, config: Config) -> None:
        self.path: str
        self.camera_name: str
        self.format: CameraFormat | None = None

        if config.camera.device:
            self.path = config.camera.device
            matches = [c for c in list_cameras() if c.path == self.path]
            self.camera_name = matches[0].name if matches else self.path
        else:
            cameras = list_cameras()
            if not cameras:
                raise SourceError("no V4L2 camera found (is it connected?)")
            camera = cameras[0]
            self.path = camera.path
            self.camera_name = camera.name

        formats = list_formats(self.path)
        self.format = choose_format(
            formats,
            width=config.camera.width,
            height=config.camera.height,
            fps=config.camera.fps,
        )

        width = config.output.width
        height = config.output.height
        fps = config.output.fps
        tail = (
            f"videoconvert ! videoscale ! videorate ! "
            f"video/x-raw,width={width},height={height},framerate={fps}/1 ! "
            f"queue max-size-buffers=2 leaky=downstream"
        )

        bin_: Gst.Bin | None = None
        if self.format is not None:
            mapped = format_caps(self.format)
            if mapped is not None and mapped[1] != "decodebin":
                caps, decoder = mapped
                decode = f"{decoder} ! " if decoder else ""
                description = (
                    f"v4l2src device={self.path} do-timestamp=true ! "
                    f"{caps} ! {decode}{tail}"
                )
                try:
                    bin_ = _parse_bin(description, "camera-source")
                except SourceError:
                    log.warning("preferred camera format failed; falling back to decodebin")

        if bin_ is None:
            bin_ = self._build_decodebin(config.output.width, config.output.height, config.output.fps)

        super().__init__(bin_, "camera-source")

    def _build_decodebin(self, width: int, height: int, fps: int) -> Gst.Bin:
        bin_ = Gst.Bin.new(f"camera-source_{next(_ids)}")
        src = Gst.ElementFactory.make("v4l2src", "camera-src")
        dec = Gst.ElementFactory.make("decodebin", "camera-decode")
        convert = Gst.ElementFactory.make("videoconvert", "camera-convert")
        scale = Gst.ElementFactory.make("videoscale", "camera-scale")
        rate = Gst.ElementFactory.make("videorate", "camera-rate")
        capsfilter = Gst.ElementFactory.make("capsfilter", "camera-caps")
        queue = Gst.ElementFactory.make("queue", "camera-queue")
        elements = [src, dec, convert, scale, rate, capsfilter, queue]
        if any(element is None for element in elements):
            raise SourceError("cannot create the camera pipeline elements")
        for element in elements:
            bin_.add(element)
        src.set_property("device", self.path)
        src.set_property("do-timestamp", True)
        capsfilter.set_property(
            "caps",
            Gst.Caps.from_string(
                f"video/x-raw,width={width},height={height},framerate={fps}/1"
            ),
        )
        queue.set_property("max-size-buffers", 2)
        queue.set_property("leaky", 2)  # downstream
        if src.link(dec) is not True:
            raise SourceError("cannot link v4l2src to decodebin")

        def pad_added(_dec: Gst.Element, pad: Gst.Pad) -> None:
            caps = pad.get_current_caps() or pad.query_caps(None)
            if caps is None or caps.get_size() == 0:
                return
            name = caps.get_structure(0).get_name()
            if name.startswith("video/") and not convert.get_static_pad("sink").is_linked():
                pad.link(convert.get_static_pad("sink"))

        dec.connect("pad-added", pad_added)
        for left, right in ((convert, scale), (scale, rate), (rate, capsfilter), (capsfilter, queue)):
            if left.link(right) is not True:
                raise SourceError(f"cannot link {left.name} to {right.name}")
        bin_.add_pad(Gst.GhostPad.new("src", queue.get_static_pad("src")))
        return bin_

    def describe(self) -> str:
        fmt = self.format.label if self.format else "decodebin"
        return f"{self.camera_name} ({self.path}, {fmt})"


class StillSource(Source):
    def __init__(self, jpeg: bytes, width: int, height: int, fps: int) -> None:
        self.jpeg = jpeg
        description = (
            f"appsrc name=src is-live=true format=time do-timestamp=true "
            f"caps=image/jpeg,width={width},height={height},framerate={fps}/1 ! "
            f"jpegparse ! imagefreeze is-live=true ! jpegdec ! "
            f"videoconvert ! videoscale ! videorate ! "
            f"video/x-raw,width={width},height={height},framerate={fps}/1 ! "
            f"queue max-size-buffers=2 leaky=downstream"
        )
        super().__init__(_parse_bin(description, "still-source"), "still-source")
        self._appsrc = self.bin.get_by_name("src")
        if self._appsrc is None:
            raise SourceError("still source has no appsrc")

    def start(self, pipeline: Gst.Pipeline) -> None:
        super().start(pipeline)
        self._push()

    def _push(self) -> None:
        if not self.jpeg:
            return
        buf = Gst.Buffer.new_wrapped(self.jpeg)
        buf.pts = 0
        buf.duration = Gst.SECOND // 30
        result = self._appsrc.emit("push-buffer", buf)
        if result not in (Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING):
            log.warning("still source push failed: %s", result)


class LoopSource(Source):
    def __init__(
        self,
        frames: list[bytes],
        width: int,
        height: int,
        fps: int,
    ) -> None:
        if not frames:
            raise SourceError("cannot create an empty loop")
        self.frames = frames
        self.fps = max(1, fps)
        self.index = 0
        self.last_jpeg: bytes | None = frames[-1]
        self._timer: int | None = None
        description = (
            f"appsrc name=src is-live=true format=time do-timestamp=true "
            f"max-buffers=2 block=false "
            f"caps=image/jpeg,width={width},height={height},framerate={fps}/1 ! "
            f"jpegparse ! jpegdec ! videoconvert ! videoscale ! videorate ! "
            f"video/x-raw,width={width},height={height},framerate={fps}/1 ! "
            f"queue max-size-buffers=2 leaky=downstream"
        )
        super().__init__(_parse_bin(description, "loop-source"), "loop-source")
        self._appsrc = self.bin.get_by_name("src")
        if self._appsrc is None:
            raise SourceError("loop source has no appsrc")

    def start(self, pipeline: Gst.Pipeline) -> None:
        super().start(pipeline)
        self._push_current()
        interval = max(1, round(1000 / self.fps))
        self._timer = GLib.timeout_add(interval, self._tick)

    def _tick(self) -> bool:
        if self._timer is None:
            return False
        self.index = (self.index + 1) % len(self.frames)
        self._push_current()
        return True

    def _push_current(self) -> None:
        jpeg = self.frames[self.index]
        self.last_jpeg = jpeg
        buf = Gst.Buffer.new_wrapped(jpeg)
        buf.duration = Gst.SECOND // self.fps
        result = self._appsrc.emit("push-buffer", buf)
        if result == Gst.FlowReturn.FLUSHING:
            log.debug("loop source is flushing")

    def stop(self) -> None:
        if self._timer is not None:
            try:
                GLib.source_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        super().stop()
