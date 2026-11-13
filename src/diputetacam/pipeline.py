from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .config import Config
from .ringbuffer import Frame, RingBuffer

log = logging.getLogger(__name__)


@dataclass
class Slot:
    index: int
    pad: Gst.Pad
    source: object | None = None


class VirtualCamera:
    """The always-running compositor that feeds v4l2loopback."""

    def __init__(self, config: Config, test_mode: bool = False) -> None:
        self.config = config
        self.test_mode = test_mode
        self.width = config.output.width
        self.height = config.output.height
        self.fps = config.output.fps
        self.frame_duration = Gst.SECOND // self.fps
        self.ring = RingBuffer(
            seconds=config.buffer.seconds,
            max_bytes=config.buffer.max_memory_mb * 1024 * 1024,
            fps=self.fps,
        )
        self._recording = False
        self._active_slot: Slot | None = None
        self._slots: list[Slot] = []
        self._last_error: str | None = None
        self._error_callback: Callable[[str], None] | None = None
        self._jpeg_samples = 0
        self._thumb_samples = 0

        self.output_device: str | None = None
        if test_mode:
            sink = "fakesink name=output sync=false async=false"
        elif Path(config.output.device).exists():
            self.output_device = config.output.device
            sink = (
                f"v4l2sink device={config.output.device} "
                f"sync=false async=false"
            )
        else:
            log.warning(
                "output device %s does not exist; using fakesink. "
                "Load v4l2loopback to expose a virtual camera.",
                config.output.device,
            )
            sink = "fakesink name=output sync=false async=false"

        pipeline_desc = (
            f"compositor name=mix background=black ignore-inactive-pads=true latency=0 ! "
            f"video/x-raw,width={self.width},height={self.height},framerate={self.fps}/1 ! "
            f"videoconvert ! "
            f"video/x-raw,format={config.output.format} ! "
            f"tee name=out "
            f"out. ! queue max-size-buffers=4 leaky=downstream ! {sink} "
            f"out. ! queue max-size-buffers=2 leaky=downstream ! "
            f"jpegenc quality={config.buffer.jpeg_quality} ! "
            f"appsink name=ring emit-signals=true sync=false max-buffers=2 drop=true "
            f"out. ! queue max-size-buffers=2 leaky=downstream ! "
            f"videoscale ! videoconvert ! "
            f"video/x-raw,format=GRAY8,width=32,height=18 ! "
            f"appsink name=thumbs emit-signals=true sync=false max-buffers=2 drop=true"
        )
        try:
            self.pipeline = Gst.parse_launch(pipeline_desc)
        except Exception as exc:  # GLib.Error is not a subclass of RuntimeError
            raise RuntimeError(f"cannot build GStreamer pipeline: {exc}") from exc
        self.mix = self.pipeline.get_by_name("mix")
        if self.mix is None:
            raise RuntimeError("compositor missing from pipeline")

        for index in range(3):
            pad = self.mix.request_pad_simple("sink_%u")
            if pad is None:
                raise RuntimeError("cannot request compositor pad")
            pad.set_property("xpos", 0)
            pad.set_property("ypos", 0)
            pad.set_property("width", self.width)
            pad.set_property("height", self.height)
            pad.set_property("alpha", 0.0)
            pad.set_property("zorder", index)
            self._slots.append(Slot(index=index, pad=pad))

        self._ring_sink = self.pipeline.get_by_name("ring")
        self._thumb_sink = self.pipeline.get_by_name("thumbs")
        if self._ring_sink is None or self._thumb_sink is None:
            raise RuntimeError("ring buffer taps missing from pipeline")
        self._ring_sink.connect("new-sample", self._on_jpeg_sample)
        self._thumb_sink.connect("new-sample", self._on_thumb_sample)

        self._bus = self.pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message", self._on_bus_message)

    @property
    def active_slot(self) -> Slot | None:
        return self._active_slot

    @property
    def slots(self) -> list[Slot]:
        return list(self._slots)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        self._error_callback = callback

    def start(self) -> None:
        log.info("starting output pipeline (%s)", self.output_device or "fakesink")
        # Start in READY. The first source is attached before we go to
        # PLAYING; otherwise the pipeline's initial async state change would
        # leave newly-added bins in READY forever.
        result = self.pipeline.set_state(Gst.State.READY)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("cannot prepare the GStreamer pipeline")

    def stop(self) -> None:
        for slot in self._slots:
            self.detach(slot)
        self.pipeline.set_state(Gst.State.NULL)
        self._bus.remove_signal_watch()

    def set_recording(self, enabled: bool) -> None:
        self._recording = enabled
        log.debug("ring recording=%s", enabled)

    @property
    def recording(self) -> bool:
        return self._recording

    def attach(self, source: object, alpha: float = 0.0) -> Slot:
        slot = next((candidate for candidate in self._slots if candidate.source is None), None)
        if slot is None:
            raise RuntimeError("no free compositor slot")
        pad = slot.pad
        try:
            # Both pads must belong to the same pipeline hierarchy before they
            # can be linked.
            source.prepare(self.pipeline)
            if source.src_pad.link(pad) != Gst.PadLinkReturn.OK:
                raise RuntimeError("cannot link source to compositor")
            slot.source = source
            pad.set_property("alpha", float(alpha))
            source.start(self.pipeline)
            if self.pipeline.get_state(0)[1] != Gst.State.PLAYING:
                self.pipeline.set_state(Gst.State.PLAYING)
        except Exception:
            slot.source = None
            try:
                source.src_pad.unlink(pad)
            except Exception:
                pass
            try:
                source.stop()
            except Exception:
                pass
            raise
        return slot

    def cut_to(self, slot: Slot) -> None:
        if slot.source is None:
            raise RuntimeError("cannot cut to an empty slot")
        if self._active_slot is not None and self._active_slot is not slot:
            self._active_slot.pad.set_property("alpha", 0.0)
        slot.pad.set_property("alpha", 1.0)
        self._active_slot = slot

    def detach(self, slot: Slot) -> None:
        source = slot.source
        if source is None:
            slot.pad.set_property("alpha", 0.0)
            return
        try:
            source.stop()
        finally:
            try:
                if source.src_pad.get_peer() is not None:
                    source.src_pad.unlink(slot.pad)
            except Exception:
                pass
            slot.source = None
            slot.pad.set_property("alpha", 0.0)
            if self._active_slot is slot:
                self._active_slot = None

    def slot_for_source(self, source: object) -> Slot | None:
        return next((slot for slot in self._slots if slot.source is source), None)

    def ring_stats(self) -> dict[str, float | int]:
        return self.ring.stats()

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            text = f"{error.message}"
            if debug:
                text = f"{text} ({debug})"
            self._last_error = text
            log.error("GStreamer error: %s", text)
            if self._error_callback is not None:
                self._error_callback(text)
        elif message.type == Gst.MessageType.WARNING:
            warning, _debug = message.parse_warning()
            log.warning("GStreamer warning: %s", warning.message)
        elif message.type == Gst.MessageType.EOS:
            source = message.src.name if message.src is not None else "?"
            log.info("output pipeline reached EOS (from %s)", source)

    def _pull_buffer(self, sink: Gst.AppSink, name: str) -> tuple[bytes, int, int] | None:
        sample = sink.emit("pull-sample")
        if sample is None:
            return None
        buf = sample.get_buffer()
        if buf is None:
            return None
        pts = buf.pts
        if pts is None or pts < 0 or pts == Gst.CLOCK_TIME_NONE:
            pts = time.monotonic_ns()
        duration = buf.duration if buf.duration and buf.duration > 0 else self.frame_duration
        success, info = buf.map(Gst.MapFlags.READ)
        if not success:
            log.debug("%s buffer map failed", name)
            return None
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        return data, int(pts), int(duration)

    def _on_jpeg_sample(self, sink: Gst.AppSink) -> Gst.FlowReturn:
        try:
            pulled = self._pull_buffer(sink, "ring")
            if pulled is not None:
                self._jpeg_samples += 1
                if self._recording:
                    data, pts, duration = pulled
                    self.ring.add_frame(Frame(pts=pts, duration=duration, jpeg=data))
        except Exception:
            log.exception("error while storing a ring-buffer frame")
        return Gst.FlowReturn.OK

    def _on_thumb_sample(self, sink: Gst.AppSink) -> Gst.FlowReturn:
        try:
            pulled = self._pull_buffer(sink, "thumbs")
            if pulled is not None:
                self._thumb_samples += 1
                if self._recording:
                    data, pts, _duration = pulled
                    self.ring.add_thumb(pts, data)
        except Exception:
            log.exception("error while storing a thumbnail")
        return Gst.FlowReturn.OK

    def sample_counts(self) -> tuple[int, int]:
        return self._jpeg_samples, self._thumb_samples
