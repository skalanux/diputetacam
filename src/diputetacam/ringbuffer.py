from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

GST_SECOND = 1_000_000_000


@dataclass
class Frame:
    pts: int
    duration: int
    jpeg: bytes
    thumb: bytes | None = None

    @property
    def seconds(self) -> float:
        return self.duration / GST_SECOND


class RingBuffer:
    """A time- and memory-bounded ring of JPEG frames.

    The compositor output is tapped twice: once through jpegenc (the frame)
    and once through a tiny grayscale scaler (the thumbnail used for match
    cuts). The two branches can deliver in either order, so thumbnails are
    correlated by PTS and may be attached after the frame is already stored.
    """

    def __init__(
        self,
        seconds: float = 20.0,
        max_bytes: int = 256 * 1024 * 1024,
        fps: int = 30,
    ) -> None:
        self.seconds = float(seconds)
        self.max_bytes = int(max_bytes)
        self.fps = max(1, int(fps))
        self._frames: deque[Frame] = deque()
        self._by_pts: dict[int, Frame] = {}
        self._pending_thumbs: OrderedDict[int, bytes] = OrderedDict()
        self._bytes = 0
        self._last_pts: int | None = None
        self._lock = threading.Lock()

    def _effective_pts(self, frame: Frame) -> Frame:
        if frame.pts is None or frame.pts < 0:
            base = self._last_pts if self._last_pts is not None else 0
            frame.pts = base + max(frame.duration, GST_SECOND // self.fps)
        if frame.duration <= 0:
            frame.duration = GST_SECOND // self.fps
        return frame

    def add_frame(self, frame: Frame) -> None:
        with self._lock:
            frame = self._effective_pts(frame)
            thumb = self._pending_thumbs.pop(frame.pts, None)
            if thumb is not None:
                frame.thumb = thumb
            self._frames.append(frame)
            self._by_pts[frame.pts] = frame
            self._bytes += len(frame.jpeg)
            self._last_pts = frame.pts
            self._evict_locked()

    def add_thumb(self, pts: int, thumb: bytes) -> None:
        if pts is None or pts < 0 or not thumb:
            return
        with self._lock:
            frame = self._by_pts.get(pts)
            if frame is not None:
                frame.thumb = thumb
                return
            self._pending_thumbs[pts] = thumb
            self._pending_thumbs.move_to_end(pts)
            while len(self._pending_thumbs) > 120:
                self._pending_thumbs.popitem(last=False)

    def _evict_locked(self) -> None:
        if not self._frames:
            return
        latest = self._frames[-1].pts
        cutoff = latest - int(self.seconds * GST_SECOND)
        while self._frames and self._frames[0].pts < cutoff:
            old = self._frames.popleft()
            self._bytes -= len(old.jpeg)
            if self._by_pts.get(old.pts) is old:
                self._by_pts.pop(old.pts, None)
        while self._bytes > self.max_bytes and len(self._frames) > 1:
            old = self._frames.popleft()
            self._bytes -= len(old.jpeg)
            if self._by_pts.get(old.pts) is old:
                self._by_pts.pop(old.pts, None)
        # Pending thumbnails that were never paired with a frame are useless.
        oldest = self._frames[0].pts if self._frames else latest
        while self._pending_thumbs:
            pts = next(iter(self._pending_thumbs))
            if pts >= oldest:
                break
            self._pending_thumbs.popitem(last=False)

    def snapshot(self) -> list[Frame]:
        with self._lock:
            return list(self._frames)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._by_pts.clear()
            self._pending_thumbs.clear()
            self._bytes = 0
            self._last_pts = None

    @property
    def latest(self) -> Frame | None:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            if not self._frames:
                return {
                    "frames": 0,
                    "seconds": 0.0,
                    "bytes": 0,
                    "fill": 0.0,
                    "target_seconds": self.seconds,
                }
            span = (self._frames[-1].pts - self._frames[0].pts) / GST_SECOND
            return {
                "frames": len(self._frames),
                "seconds": round(span, 3),
                "bytes": self._bytes,
                "fill": round(min(1.0, span / self.seconds), 3),
                "target_seconds": self.seconds,
            }


def now_ns() -> int:
    return time.monotonic_ns()
