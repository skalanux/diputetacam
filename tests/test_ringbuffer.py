from __future__ import annotations

import unittest

from diputetacam.ringbuffer import GST_SECOND, Frame, RingBuffer


class RingBufferTest(unittest.TestCase):
    def test_time_eviction(self) -> None:
        ring = RingBuffer(seconds=1.0, max_bytes=10_000_000, fps=10)
        step = GST_SECOND // 10
        for index in range(30):
            ring.add_frame(Frame(pts=index * step, duration=step, jpeg=b"x" * 100))
        stats = ring.stats()
        self.assertLessEqual(stats["seconds"], 1.01)
        self.assertGreater(stats["frames"], 5)
        self.assertLess(stats["frames"], 15)

    def test_byte_eviction(self) -> None:
        ring = RingBuffer(seconds=10.0, max_bytes=250, fps=10)
        for index in range(5):
            ring.add_frame(Frame(pts=index * GST_SECOND, duration=GST_SECOND // 10, jpeg=b"x" * 100))
        stats = ring.stats()
        self.assertLessEqual(stats["bytes"], 250)
        self.assertLessEqual(stats["frames"], 2)

    def test_thumb_attach_after_frame(self) -> None:
        ring = RingBuffer(seconds=5.0, max_bytes=10_000_000, fps=10)
        ring.add_frame(Frame(pts=100, duration=100, jpeg=b"jpeg"))
        ring.add_thumb(100, b"thumb")
        self.assertEqual(ring.snapshot()[0].thumb, b"thumb")

    def test_thumb_before_frame(self) -> None:
        ring = RingBuffer(seconds=5.0, max_bytes=10_000_000, fps=10)
        ring.add_thumb(100, b"thumb")
        ring.add_frame(Frame(pts=100, duration=100, jpeg=b"jpeg"))
        self.assertEqual(ring.snapshot()[0].thumb, b"thumb")

    def test_clear(self) -> None:
        ring = RingBuffer(seconds=5.0, max_bytes=10_000_000, fps=10)
        ring.add_frame(Frame(pts=100, duration=100, jpeg=b"jpeg"))
        ring.clear()
        self.assertEqual(ring.stats()["frames"], 0)
        self.assertIsNone(ring.latest)


if __name__ == "__main__":
    unittest.main()
