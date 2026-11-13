from __future__ import annotations

import unittest

from diputetacam.match import choose_loop_start, thumbnail_distance
from diputetacam.ringbuffer import GST_SECOND, Frame


def thumb(value: int) -> bytes:
    return bytes([value]) * (32 * 18)


class MatchTest(unittest.TestCase):
    def test_prefers_similar_frame(self) -> None:
        fps = 30
        step = GST_SECOND // fps
        frames = [
            Frame(pts=index * step, duration=step, jpeg=b"jpeg", thumb=thumb(index % 7))
            for index in range(600)
        ]
        # The newest frame looks like the frame 18 seconds back.
        frames[-1].thumb = thumb(200)
        frames[59].thumb = thumb(200)
        match = choose_loop_start(frames, min_seconds=15, max_seconds=20, threshold=5)
        self.assertTrue(match.used_match)
        self.assertEqual(match.start_index, 59)
        self.assertAlmostEqual(match.loop_seconds, 18.0, places=1)
        self.assertEqual(match.score, 0.0)

    def test_falls_back_without_thumbnails(self) -> None:
        frames = [
            Frame(pts=index * GST_SECOND, duration=GST_SECOND, jpeg=b"jpeg")
            for index in range(20)
        ]
        match = choose_loop_start(frames, min_seconds=5, max_seconds=10)
        self.assertFalse(match.used_match)
        self.assertEqual(match.start_index, 0)

    def test_falls_back_when_threshold_is_exceeded(self) -> None:
        frames = [
            Frame(pts=index * GST_SECOND, duration=GST_SECOND, jpeg=b"jpeg", thumb=thumb(index))
            for index in range(20)
        ]
        frames[-1].thumb = thumb(255)
        match = choose_loop_start(frames, min_seconds=5, max_seconds=10, threshold=0.1)
        self.assertFalse(match.used_match)
        self.assertEqual(match.start_index, 0)
        self.assertIsNotNone(match.score)

    def test_distance_is_zero_for_identical(self) -> None:
        self.assertEqual(thumbnail_distance(b"\x10" * 10, b"\x10" * 10), 0.0)

    def test_distance_is_255_for_different_sizes(self) -> None:
        self.assertEqual(thumbnail_distance(b"a", b"ab"), 255.0)


if __name__ == "__main__":
    unittest.main()
