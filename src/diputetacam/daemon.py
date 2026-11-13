from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import GLib, GLibUnix, Gst  # noqa: E402

from . import __version__
from .config import Config, ConfigError, ensure_config, load_config
from .detect import list_cameras
from .ipc import IpcClient, IpcServer, socket_path
from .match import LoopMatch, choose_loop_start
from .pipeline import Slot, VirtualCamera
from .sources import (
    CameraSource,
    LoopSource,
    Source,
    SourceError,
    StandbySource,
    StillSource,
    TestSource,
)

log = logging.getLogger(__name__)

LIVE = "live"
STARTING = "starting"
ENTERING_LOOP = "entering_loop"
LOOP = "loop"
RETURNING = "returning"
STANDBY = "standby"
ERROR = "error"
STOPPED = "stopped"


class CommandError(RuntimeError):
    pass


class Controller:
    def __init__(self, config: Config, test_mode: bool = False) -> None:
        self.config = config
        self.test_mode = test_mode
        self.pipeline = VirtualCamera(config, test_mode=test_mode)
        self.pipeline.set_error_callback(self._on_pipeline_error)
        self.state = STARTING
        self._closing = False
        self._started_at = time.monotonic()
        self._last_error: str | None = None
        self._camera_source: CameraSource | TestSource | None = None
        self._loop_source: LoopSource | None = None
        self._still_source: StillSource | None = None
        self._standby_source: StandbySource | None = None
        self._loop_info: dict[str, Any] = {}
        self._entry: dict[str, Any] = {}
        self._return: dict[str, Any] = {}
        self._timers: set[int] = set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        try:
            self.pipeline.start()
        except Exception as exc:  # noqa: BLE001
            self.state = ERROR
            self._set_error(f"cannot start output pipeline: {exc}")
            return
        self._start_standby(initial=True)

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        for timer in list(self._timers):
            try:
                GLib.source_remove(timer)
            except Exception:
                pass
        self._timers.clear()
        for slot in self.pipeline.slots:
            if slot.source is not None:
                try:
                    self.pipeline.detach(slot)
                except Exception:
                    log.exception("error detaching source during shutdown")
        try:
            self.pipeline.stop()
        except Exception:
            log.exception("error stopping pipeline")
        self.state = STOPPED

    # ------------------------------------------------------------------
    # startup
    # ------------------------------------------------------------------
    def _start_standby(self, initial: bool = False) -> None:
        try:
            source = StandbySource(
                width=self.config.output.width,
                height=self.config.output.height,
                fps=self.config.output.fps,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_error(f"cannot create standby source: {exc}")
            if initial:
                self._start_camera_initial()
            return
        self._standby_source = source
        self.state = STARTING
        self._start_and_wait(
            source,
            timeout_ms=2000,
            on_ready=lambda src: self._on_standby_ready(src, initial),
            on_timeout=self._on_standby_timeout,
        )

    def _on_standby_ready(self, source: Source, initial: bool) -> None:
        if self._closing:
            return
        self._cut(source)
        self.state = STANDBY
        if initial:
            if self.config.startup.live:
                self._start_camera_initial()
            else:
                log.info("standby: waiting for `diputetacam live`")
                self._notify("Diputetacam", "En espera; usa `diputetacam live`")

    def _on_standby_timeout(self, source: Source, reason: str) -> None:
        self._detach_source(source)
        self._standby_source = None
        self._set_error(f"standby source did not start: {reason}")
        if self.state == STARTING:
            if self.config.startup.live:
                self._start_camera_initial()
            else:
                self.state = ERROR

    def _make_camera(self) -> CameraSource | TestSource:
        if self.test_mode:
            return TestSource(
                width=self.config.output.width,
                height=self.config.output.height,
                fps=self.config.output.fps,
            )
        return CameraSource(self.config)

    def _start_camera_initial(self) -> None:
        if self._closing:
            return
        if self._camera_source is not None:
            self._detach_source(self._camera_source)
        try:
            source = self._make_camera()
        except Exception as exc:  # noqa: BLE001
            self._set_error(f"camera unavailable: {exc}")
            self.state = STANDBY if self.pipeline.active_slot else ERROR
            return
        self._camera_source = source
        self.state = STARTING
        self._start_and_wait(
            source,
            timeout_ms=4000,
            on_ready=self._on_camera_initial_ready,
            on_timeout=self._on_camera_initial_timeout,
        )

    def _on_camera_initial_ready(self, source: Source) -> None:
        if self._closing:
            return
        self._cut(source)
        self._detach_others(source)
        self._camera_source = source
        self.state = LIVE
        self._last_error = None
        self.pipeline.set_recording(True)
        log.info("live: %s", self._camera_description(source))
        self._notify("Diputetacam", "Cámara en vivo")

    def _on_camera_initial_timeout(self, source: Source, reason: str) -> None:
        self._detach_source(source)
        self._camera_source = None
        self._set_error(f"camera did not produce frames: {reason}")
        self.state = STANDBY if self.pipeline.active_slot else ERROR

    # ------------------------------------------------------------------
    # transitions
    # ------------------------------------------------------------------
    def enter_loop(self) -> None:
        if self.state != LIVE:
            raise CommandError("loop is only available while live")
        frames = self.pipeline.ring.snapshot()
        if len(frames) < 2:
            raise CommandError("the ring buffer is still empty; wait a few seconds")
        match = choose_loop_start(
            frames,
            min_seconds=self.config.buffer.min_loop_seconds,
            max_seconds=self.config.buffer.max_loop_seconds,
            threshold=self.config.buffer.match_threshold,
        )
        loop_frames = [frame.jpeg for frame in frames[match.start_index :]]
        if len(loop_frames) < 2:
            raise CommandError("not enough frames for a loop")

        self.pipeline.set_recording(False)
        self.state = ENTERING_LOOP
        self._entry = {
            "match": match,
            "frames": loop_frames,
            "still": None,
            "loop": None,
            "freeze_done": False,
            "loop_ready": False,
        }
        log.info(
            "entering loop: %.2fs, match=%s, score=%s",
            match.loop_seconds,
            match.used_match,
            match.score,
        )
        try:
            still = StillSource(
                frames[-1].jpeg,
                width=self.config.output.width,
                height=self.config.output.height,
                fps=self.config.output.fps,
            )
        except Exception as exc:  # noqa: BLE001
            self.pipeline.set_recording(True)
            self.state = LIVE
            self._set_error(f"cannot create freeze frame: {exc}")
            raise CommandError(f"cannot create freeze frame: {exc}") from exc

        self._entry["still"] = still
        self._start_and_wait(
            still,
            timeout_ms=1500,
            on_ready=self._on_entry_still_ready,
            on_timeout=self._on_entry_still_timeout,
        )

    def _on_entry_still_ready(self, source: Source) -> None:
        if self._closing or self.state != ENTERING_LOOP:
            return
        self._still_source = source  # type: ignore[assignment]
        self._cut(source)
        # The freeze is now visible. The real camera can be released.
        if self._camera_source is not None:
            self._detach_source(self._camera_source)
            self._camera_source = None

        loop_source: LoopSource | None = None
        try:
            loop_source = LoopSource(
                self._entry["frames"],
                width=self.config.output.width,
                height=self.config.output.height,
                fps=self.config.output.fps,
            )
        except Exception as exc:  # noqa: BLE001
            self._on_entry_loop_timeout(None, str(exc))
            return
        self._entry["loop"] = loop_source
        self._start_and_wait(
            loop_source,
            timeout_ms=2500,
            on_ready=self._on_entry_loop_ready,
            on_timeout=self._on_entry_loop_timeout,
        )

        freeze_ms = max(0, self.config.transition.freeze_ms)
        if freeze_ms == 0:
            self._entry["freeze_done"] = True
            self._maybe_finish_entry()
        else:
            self._after(freeze_ms, self._on_entry_freeze_done)

    def _on_entry_still_timeout(self, source: Source | None, reason: str) -> None:
        if source is not None:
            self._detach_source(source)
        self._entry = {}
        self.pipeline.set_recording(True)
        self.state = LIVE
        self._set_error(f"freeze source did not start: {reason}")

    def _on_entry_loop_ready(self, source: Source) -> None:
        if self._closing or self.state != ENTERING_LOOP:
            return
        self._entry["loop_ready"] = True
        self._maybe_finish_entry()

    def _on_entry_loop_timeout(self, source: Source | None, reason: str) -> None:
        if source is not None:
            self._detach_source(source)
        self._entry = {}
        self.state = ERROR
        self._set_error(f"loop source did not start: {reason}")

    def _on_entry_freeze_done(self) -> None:
        if self._closing or self.state != ENTERING_LOOP:
            return
        self._entry["freeze_done"] = True
        self._maybe_finish_entry()

    def _maybe_finish_entry(self) -> None:
        if self.state != ENTERING_LOOP:
            return
        if not self._entry.get("freeze_done") or not self._entry.get("loop_ready"):
            return
        loop_source = self._entry.get("loop")
        if loop_source is None:
            return
        self._cut(loop_source)
        if self._still_source is not None:
            self._detach_source(self._still_source)
            self._still_source = None
        self._loop_source = loop_source
        self._loop_info = self._loop_status(self._entry["match"])
        self._entry = {}
        self.state = LOOP
        self._last_error = None
        log.info("loop active: %.2fs", self._loop_info.get("seconds", 0))
        self._notify(
            "Diputetacam",
            f"Loop activo: {self._loop_info.get('seconds', 0):.1f} s",
        )

    def go_live(self) -> None:
        if self.state == LIVE:
            return
        if self.state == LOOP:
            self._begin_return()
            return
        if self.state in (STANDBY, ERROR):
            self._start_camera_for_live()
            return
        raise CommandError("a transition is already in progress")

    def _begin_return(self) -> None:
        if self._loop_source is None:
            self._start_camera_for_live()
            return
        self.state = RETURNING
        self.pipeline.set_recording(False)
        self._return = {
            "loop": self._loop_source,
            "camera": None,
            "still": None,
            "freeze_done": False,
        }
        try:
            camera = self._make_camera()
        except Exception as exc:  # noqa: BLE001
            self.state = LOOP
            self._set_error(f"camera unavailable: {exc}")
            return
        self._return["camera"] = camera
        self._start_and_wait(
            camera,
            timeout_ms=4000,
            on_ready=self._on_return_camera_ready,
            on_timeout=self._on_return_camera_timeout,
        )

    def _on_return_camera_ready(self, source: Source) -> None:
        if self._closing or self.state != RETURNING:
            return
        self._camera_source = source  # type: ignore[assignment]
        loop_source = self._return.get("loop")
        jpeg = getattr(loop_source, "last_jpeg", None)
        if jpeg is None and self._loop_source is not None:
            jpeg = self._loop_source.frames[-1]
        if jpeg is None:
            self._cut(source)
            self._finish_return(source)
            return
        try:
            still = StillSource(
                jpeg,
                width=self.config.output.width,
                height=self.config.output.height,
                fps=self.config.output.fps,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_error(f"cannot create return freeze: {exc}")
            self._cut(source)
            self._finish_return(source)
            return
        self._still_source = still
        self._return["still"] = still
        self._start_and_wait(
            still,
            timeout_ms=1500,
            on_ready=self._on_return_still_ready,
            on_timeout=self._on_return_still_timeout,
        )

    def _on_return_camera_timeout(self, source: Source, reason: str) -> None:
        self._detach_source(source)
        self._camera_source = None
        self.state = LOOP
        self._set_error(f"camera did not start: {reason}")

    def _on_return_still_ready(self, source: Source) -> None:
        if self._closing or self.state != RETURNING:
            return
        self._cut(source)
        if self._loop_source is not None:
            self._detach_source(self._loop_source)
            self._loop_source = None
        self._after(max(0, self.config.transition.freeze_ms), self._finish_return_after_freeze)

    def _on_return_still_timeout(self, source: Source | None, reason: str) -> None:
        if source is not None:
            self._detach_source(source)
        camera = self._return.get("camera")
        if camera is None:
            self.state = LOOP
            self._set_error(f"return freeze failed: {reason}")
            return
        self._cut(camera)
        self._finish_return(camera)
        self._set_error(f"return freeze failed; direct cut: {reason}")

    def _finish_return_after_freeze(self) -> None:
        if self._closing or self.state != RETURNING:
            return
        camera = self._return.get("camera")
        if camera is None:
            return
        self._cut(camera)
        self._finish_return(camera)

    def _finish_return(self, camera: Source) -> None:
        if self._still_source is not None:
            self._detach_source(self._still_source)
            self._still_source = None
        if self._loop_source is not None:
            self._detach_source(self._loop_source)
            self._loop_source = None
        self._camera_source = camera  # type: ignore[assignment]
        self._return = {}
        self._loop_info = {}
        self.pipeline.ring.clear()
        self.pipeline.set_recording(True)
        self.state = LIVE
        self._last_error = None
        log.info("back live: %s", self._camera_description(camera))
        self._notify("Diputetacam", "Cámara en vivo")

    def _start_camera_for_live(self) -> None:
        if self.state in (STARTING, ENTERING_LOOP, RETURNING):
            raise CommandError("a transition is already in progress")
        previous = self.state
        self.state = RETURNING
        self.pipeline.set_recording(False)
        try:
            camera = self._make_camera()
        except Exception as exc:  # noqa: BLE001
            self.state = previous
            self._set_error(f"camera unavailable: {exc}")
            return
        self._start_and_wait(
            camera,
            timeout_ms=4000,
            on_ready=self._on_live_camera_ready,
            on_timeout=self._on_live_camera_timeout,
        )

    def _on_live_camera_ready(self, source: Source) -> None:
        if self._closing:
            return
        self._cut(source)
        self._detach_others(source)
        self._camera_source = source  # type: ignore[assignment]
        self._loop_source = None
        self._still_source = None
        self._standby_source = None
        self._loop_info = {}
        self.pipeline.ring.clear()
        self.pipeline.set_recording(True)
        self.state = LIVE
        self._last_error = None
        log.info("live: %s", self._camera_description(source))
        self._notify("Diputetacam", "Cámara en vivo")

    def _on_live_camera_timeout(self, source: Source, reason: str) -> None:
        self._detach_source(source)
        self._set_error(f"camera did not start: {reason}")
        self.state = STANDBY if self.pipeline.active_slot else ERROR

    def standby(self) -> None:
        if self.state in (STARTING, ENTERING_LOOP, RETURNING):
            raise CommandError("a transition is already in progress")
        if self.state == STANDBY:
            return
        previous = self.state
        self.state = STARTING
        self.pipeline.set_recording(False)
        try:
            source = StandbySource(
                width=self.config.output.width,
                height=self.config.output.height,
                fps=self.config.output.fps,
            )
        except Exception as exc:  # noqa: BLE001
            self.state = previous
            self._set_error(f"cannot create standby source: {exc}")
            return
        self._standby_source = source
        self._start_and_wait(
            source,
            timeout_ms=2000,
            on_ready=self._on_standby_command_ready,
            on_timeout=self._on_standby_command_timeout,
        )

    def _on_standby_command_ready(self, source: Source) -> None:
        if self._closing:
            return
        self._cut(source)
        self._detach_others(source)
        self._camera_source = None
        self._loop_source = None
        self._still_source = None
        self._loop_info = {}
        self.pipeline.ring.clear()
        self.state = STANDBY
        self._last_error = None
        self._notify("Diputetacam", "Cámara apagada")

    def _on_standby_command_timeout(self, source: Source, reason: str) -> None:
        self._detach_source(source)
        self._standby_source = None
        self.state = ERROR
        self._set_error(f"standby source did not start: {reason}")

    # ------------------------------------------------------------------
    # commands / status
    # ------------------------------------------------------------------
    def toggle(self) -> None:
        if self.state == LIVE:
            self.enter_loop()
        elif self.state == LOOP:
            self.go_live()
        elif self.state in (STANDBY, ERROR):
            self.go_live()
        else:
            raise CommandError("a transition is already in progress")

    def handle_command(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("cmd")
        if not isinstance(command, str):
            return {"ok": False, "error": "missing cmd"}
        try:
            if command == "ping":
                return {"ok": True, "version": __version__}
            if command == "status":
                return {"ok": True, "status": self.status()}
            if command == "loop":
                self.enter_loop()
                return {"ok": True, "state": self.state}
            if command == "live":
                self.go_live()
                return {"ok": True, "state": self.state}
            if command == "toggle":
                self.toggle()
                return {"ok": True, "state": self.state}
            if command == "standby":
                self.standby()
                return {"ok": True, "state": self.state}
            if command == "devices":
                return {
                    "ok": True,
                    "devices": [{"path": c.path, "name": c.name} for c in list_cameras()],
                }
            return {"ok": False, "error": f"unknown command: {command}"}
        except CommandError as exc:
            return {"ok": False, "error": str(exc), "status": self.status()}
        except Exception as exc:  # noqa: BLE001
            log.exception("command failed: %s", command)
            return {"ok": False, "error": str(exc), "status": self.status()}

    def status(self) -> dict[str, Any]:
        buffer = self.pipeline.ring_stats()
        active_slot = self.pipeline.active_slot
        active = active_slot.source if active_slot is not None else None
        return {
            "version": __version__,
            "state": self.state,
            "test_mode": self.test_mode,
            "output": self.pipeline.output_device or "fakesink",
            "camera": self._camera_description(self._camera_source) if self._camera_source else None,
            "active_source": getattr(active, "name", None),
            "recording": self.pipeline.recording,
            "buffer": buffer,
            "loop": self._loop_info,
            "last_error": self._last_error,
            "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            "freeze_ms": self.config.transition.freeze_ms,
            "output_size": [self.config.output.width, self.config.output.height],
            "output_fps": self.config.output.fps,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _camera_description(self, source: object | None) -> str | None:
        if source is None:
            return None
        describe = getattr(source, "describe", None)
        if callable(describe):
            return describe()
        return getattr(source, "name", None)

    def _loop_status(self, match: LoopMatch) -> dict[str, Any]:
        return {
            "seconds": round(match.loop_seconds, 2),
            "score": match.score,
            "used_match": match.used_match,
            "reason": match.reason,
        }

    def _cut(self, source: Source) -> None:
        slot = self.pipeline.slot_for_source(source)
        if slot is None:
            raise RuntimeError("source is not attached")
        self.pipeline.cut_to(slot)

    def _detach_source(self, source: Source | None) -> None:
        if source is None:
            return
        slot = self.pipeline.slot_for_source(source)
        if slot is not None:
            self.pipeline.detach(slot)
        if self._camera_source is source:
            self._camera_source = None
        if self._loop_source is source:
            self._loop_source = None
        if self._still_source is source:
            self._still_source = None
        if self._standby_source is source:
            self._standby_source = None

    def _detach_others(self, keep: Source) -> None:
        for slot in self.pipeline.slots:
            if slot.source is not None and slot.source is not keep:
                self._detach_source(slot.source)  # type: ignore[arg-type]

    def _start_and_wait(
        self,
        source: Source,
        timeout_ms: int,
        on_ready: Callable[[Source], None],
        on_timeout: Callable[[Source, str], None],
    ) -> Slot | None:
        done = {"finished": False, "timer": None}

        def ready(src: Source) -> None:
            if done["finished"] or self._closing:
                return
            done["finished"] = True
            if done["timer"] is not None:
                try:
                    GLib.source_remove(done["timer"])
                except Exception:
                    pass
                self._timers.discard(done["timer"])
                done["timer"] = None
            try:
                on_ready(src)
            except Exception:  # noqa: BLE001
                log.exception("ready callback failed")

        def timeout() -> bool:
            if done["timer"] is not None:
                self._timers.discard(done["timer"])
            if done["finished"] or self._closing:
                return False
            done["finished"] = True
            try:
                on_timeout(source, "timeout")
            except Exception:  # noqa: BLE001
                log.exception("timeout callback failed")
            return False

        source.on_ready(ready)
        try:
            slot = self.pipeline.attach(source)
        except Exception as exc:  # noqa: BLE001
            done["finished"] = True
            try:
                on_timeout(source, str(exc))
            except Exception:  # noqa: BLE001
                log.exception("attach failure callback failed")
            return None

        done["timer"] = GLib.timeout_add(max(1, timeout_ms), timeout)
        self._timers.add(done["timer"])
        return slot

    def _after(self, ms: int, callback: Callable[[], None]) -> int:
        def run() -> bool:
            self._timers.discard(timer)
            if self._closing:
                return False
            try:
                callback()
            except Exception:  # noqa: BLE001
                log.exception("timer callback failed")
            return False

        timer = GLib.timeout_add(max(1, ms), run)
        self._timers.add(timer)
        return timer

    def _set_error(self, message: str) -> None:
        self._last_error = message
        log.error("%s", message)
        self._notify("Diputetacam", message, urgent=True)

    def _notify(self, title: str, message: str, urgent: bool = False) -> None:
        if not shutil.which("notify-send"):
            return
        args = ["notify-send", "-a", "Diputetacam", "-t", "3000"]
        if urgent:
            args += ["-u", "critical"]
        args += [title, message]
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def _on_pipeline_error(self, message: str) -> None:
        self._set_error(f"pipeline error: {message}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="diputetacamd", description="Diputetacam daemon")
    parser.add_argument("--config", type=Path, default=None, help="config TOML path")
    parser.add_argument("--test", action="store_true", help="use a test pattern and fakesink")
    parser.add_argument("--socket", type=Path, default=None, help="Unix socket path")
    parser.add_argument("--debug", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def _daemon_socket(test_mode: bool, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("DIPUTETACAM_SOCKET")
    if env:
        return Path(env)
    path = socket_path()
    if test_mode:
        return path.with_name("diputetacam-test.sock")
    return path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    Gst.init(None)

    try:
        config_path = ensure_config(args.config)
        config = load_config(config_path)
    except (ConfigError, OSError) as exc:
        log.error("configuration error: %s", exc)
        return 2

    sock = _daemon_socket(args.test, args.socket)
    # Refuse to steal the socket from a running daemon.
    try:
        response = IpcClient(sock, timeout=0.5).request("ping")
        if response.get("ok"):
            log.error("diputetacamd is already running at %s", sock)
            return 1
    except Exception:
        pass

    controller = Controller(config, test_mode=args.test)
    server = IpcServer(controller, sock)
    server.start()
    controller.start()

    loop = GLib.MainLoop()

    def quit_loop(*_args: object) -> bool:
        loop.quit()
        return False

    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, quit_loop)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, quit_loop)

    log.info("diputetacamd %s listening on %s", __version__, sock)
    try:
        loop.run()
    finally:
        controller.shutdown()
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
