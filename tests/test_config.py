from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from diputetacam.config import Config, ConfigError, config_from_dict, load_config


class ConfigTest(unittest.TestCase):
    def test_defaults_validate(self) -> None:
        Config().validate()

    def test_missing_file_returns_defaults(self) -> None:
        config = load_config(Path("/nonexistent/diputetacam/config.toml"))
        self.assertEqual(config.output.device, "/dev/video10")
        self.assertEqual(config.buffer.seconds, 20.0)

    def test_merge_sections(self) -> None:
        config = config_from_dict(
            {
                "output": {"device": "/dev/video42", "fps": 25},
                "buffer": {"seconds": 30, "min_loop_seconds": 20, "max_loop_seconds": 30},
                "transition": {"freeze_ms": 250},
            }
        )
        self.assertEqual(config.output.device, "/dev/video42")
        self.assertEqual(config.output.fps, 25)
        self.assertEqual(config.buffer.seconds, 30.0)
        self.assertEqual(config.transition.freeze_ms, 250)

    def test_invalid_fps(self) -> None:
        with self.assertRaises(ConfigError):
            config_from_dict({"output": {"fps": 0}})

    def test_invalid_loop_window(self) -> None:
        with self.assertRaises(ConfigError):
            config_from_dict(
                {
                    "buffer": {
                        "seconds": 20,
                        "min_loop_seconds": 25,
                        "max_loop_seconds": 20,
                    }
                }
            )

    def test_load_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[output]\ndevice = \"/dev/video11\"\n\n[buffer]\nseconds = 25\n",
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.output.device, "/dev/video11")
            self.assertEqual(config.buffer.seconds, 25.0)


if __name__ == "__main__":
    unittest.main()
