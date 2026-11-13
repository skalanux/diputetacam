from __future__ import annotations

from dataclasses import dataclass

from .ringbuffer import Frame, GST_SECOND


@dataclass
class LoopMatch:
    start_index: int
    loop_seconds: float
    score: float | None
    used_match: bool
    reason: str


def thumbnail_distance(a: bytes, b: bytes) -> float:
    """Mean absolute difference between two 32x18 grayscale thumbnails."""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def choose_loop_start(
    frames: list[Frame],
    min_seconds: float = 15.0,
    max_seconds: float = 20.0,
    threshold: float = 18.0,
) -> LoopMatch:
    """Pick the start frame for a natural loop.

    The loop must end at the newest frame, so the start is chosen among frames
    between ``min_seconds`` and ``max_seconds`` old. The frame most similar to
    the newest one wins. This makes both the entry freeze-cut and the loop
    wrap cut land between nearly identical images.
    """

    if len(frames) < 2:
        return LoopMatch(0, 0.0, None, False, "not enough frames")

    newest = frames[-1]
    fallback_seconds = max(0.0, (newest.pts - frames[0].pts) / GST_SECOND)
    fallback = LoopMatch(0, fallback_seconds, None, False, "fallback to full buffer")

    if newest.thumb is None:
        fallback.reason = "no thumbnail for newest frame; fallback to full buffer"
        return fallback

    candidates: list[tuple[float, int, float]] = []
    for index, frame in enumerate(frames[:-1]):
        if frame.thumb is None:
            continue
        age = (newest.pts - frame.pts) / GST_SECOND
        if age < min_seconds or age > max_seconds:
            continue
        candidates.append((thumbnail_distance(newest.thumb, frame.thumb), index, age))

    if not candidates:
        fallback.reason = "no candidates in loop window; fallback to full buffer"
        return fallback

    # Prefer the best match; on a tie prefer the oldest (longer loop).
    best_score, best_index, best_age = min(candidates, key=lambda item: (item[0], -item[2]))
    if best_score > threshold:
        fallback.score = round(best_score, 2)
        fallback.reason = f"best match {best_score:.1f} > {threshold:.1f}; fallback"
        return fallback

    return LoopMatch(
        start_index=best_index,
        loop_seconds=best_age,
        score=round(best_score, 2),
        used_match=True,
        reason="matched start frame",
    )
