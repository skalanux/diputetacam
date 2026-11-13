from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable

from gi.repository import GLib


class IpcError(RuntimeError):
    pass


def socket_path() -> Path:
    override = os.environ.get("DIPUTETACAM_SOCKET")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path("/tmp")
    if runtime:
        return base / "diputetacam.sock"
    return base / f"diputetacam-{os.getuid()}.sock"


class IpcClient:
    def __init__(self, path: str | Path | None = None, timeout: float = 3.0) -> None:
        self.path = Path(path) if path else socket_path()
        self.timeout = timeout

    def request(self, command: str, **kwargs: Any) -> dict[str, Any]:
        payload = {"cmd": command, **kwargs}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(self.path))
                sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
                data = bytearray()
                while b"\n" not in data:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
        except (OSError, socket.timeout) as exc:
            raise IpcError(f"cannot talk to diputetacamd at {self.path}: {exc}") from exc
        if not data:
            raise IpcError("empty response from diputetacamd")
        try:
            response = json.loads(bytes(data).split(b"\n", 1)[0])
        except json.JSONDecodeError as exc:
            raise IpcError(f"invalid response: {exc}") from exc
        if not isinstance(response, dict):
            raise IpcError("invalid response")
        return response


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, handler: type[socketserver.BaseRequestHandler], controller: Any):
        self.controller = controller
        super().__init__(path, handler)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: _ThreadingUnixServer = self.server  # type: ignore[assignment]
        raw = bytearray()
        self.request.settimeout(5.0)
        while b"\n" not in raw:
            chunk = self.request.recv(65536)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 1_000_000:
                self._send({"ok": False, "error": "request too large"})
                return
        if not raw:
            return
        try:
            request = json.loads(bytes(raw).split(b"\n", 1)[0])
        except json.JSONDecodeError as exc:
            self._send({"ok": False, "error": f"invalid JSON: {exc}"})
            return
        if not isinstance(request, dict):
            self._send({"ok": False, "error": "request must be a JSON object"})
            return

        future: Future[dict[str, Any]] = Future()

        def dispatch() -> bool:
            try:
                result = server.controller.handle_command(request)
            except Exception as exc:  # noqa: BLE001 - report to the client
                result = {"ok": False, "error": str(exc)}
            if not future.done():
                future.set_result(result)
            return False

        GLib.idle_add(dispatch)
        try:
            response = future.result(timeout=8.0)
        except FutureTimeout:
            response = {"ok": False, "error": "daemon command timed out"}
        self._send(response)

    def _send(self, response: dict[str, Any]) -> None:
        try:
            self.request.sendall(json.dumps(response).encode("utf-8") + b"\n")
        except OSError:
            pass


class IpcServer:
    def __init__(self, controller: Any, path: str | Path | None = None) -> None:
        self.controller = controller
        self.path = Path(path) if path else socket_path()
        self._server: _ThreadingUnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._server = _ThreadingUnixServer(str(self.path), _Handler, self.controller)
        os.chmod(self.path, 0o600)
        self._thread = threading.Thread(target=self._server.serve_forever, name="ipc", daemon=True)
        self._thread.start()
        # Give accept() a moment so an immediately-following CLI call works.
        time.sleep(0.02)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
