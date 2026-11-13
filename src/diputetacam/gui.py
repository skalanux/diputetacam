from __future__ import annotations

import sys
import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .ipc import IpcClient, IpcError


STATE_LABELS = {
    "starting": "Arrancando",
    "live": "En vivo",
    "entering_loop": "Entrando en loop",
    "loop": "Loop",
    "returning": "Volviendo a vivo",
    "standby": "Cámara apagada",
    "error": "Error",
    "stopped": "Detenido",
}


def _fmt_bytes(value: float | int) -> str:
    value = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class DiputetacamWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="Diputetacam")
        self.set_default_size(540, 460)
        self._stop = threading.Event()
        self._connected = True
        self._last_status: dict[str, Any] = {}

        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Diputetacam", subtitle="Cámara virtual con loop natural")
        header.set_title_widget(title)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.set_margin_top(20)
        root.set_margin_bottom(20)
        root.set_margin_start(20)
        root.set_margin_end(20)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(root)
        self.set_content(toolbar)

        self.banner = Adw.Banner()
        self.banner.set_revealed(False)
        root.append(self.banner)

        self.state_label = Gtk.Label(label="Conectando…")
        self.state_label.add_css_class("title-1")
        self.state_label.set_xalign(0)
        root.append(self.state_label)

        self.detail_label = Gtk.Label(label="")
        self.detail_label.add_css_class("dim-label")
        self.detail_label.set_xalign(0)
        self.detail_label.set_wrap(True)
        root.append(self.detail_label)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_fraction(0.0)
        root.append(self.progress)

        self.buffer_label = Gtk.Label(label="Buffer: —")
        self.buffer_label.set_xalign(0)
        self.buffer_label.set_wrap(True)
        root.append(self.buffer_label)

        self.loop_label = Gtk.Label(label="Loop: —")
        self.loop_label.set_xalign(0)
        self.loop_label.set_wrap(True)
        root.append(self.loop_label)

        root.append(Gtk.Separator())

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        self.loop_button = Gtk.Button(label="Loop")
        self.loop_button.add_css_class("suggested-action")
        self.loop_button.connect("clicked", lambda _button: self._send("loop"))
        self.live_button = Gtk.Button(label="Volver en vivo")
        self.live_button.connect("clicked", lambda _button: self._send("live"))
        self.standby_button = Gtk.Button(label="Apagar")
        self.standby_button.add_css_class("destructive-action")
        self.standby_button.connect("clicked", lambda _button: self._send("standby"))
        buttons.append(self.loop_button)
        buttons.append(self.live_button)
        buttons.append(self.standby_button)
        root.append(buttons)

        footer = Gtk.Label(
            label="También puedes usar: diputetacam loop | live | toggle",
        )
        footer.add_css_class("dim-label")
        footer.set_xalign(0)
        footer.set_wrap(True)
        root.append(footer)

        self.connect("close-request", self._on_close)
        threading.Thread(target=self._poll_loop, name="gui-poll", daemon=True).start()

    # ------------------------------------------------------------------
    def _on_close(self, *_args: object) -> bool:
        self._stop.set()
        return False

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                response = IpcClient(timeout=2.0).request("status")
                if self._stop.is_set():
                    return
                if response.get("ok"):
                    GLib.idle_add(self._apply_status, response["status"])
                else:
                    GLib.idle_add(self._show_error, response.get("error", "error desconocido"))
            except IpcError as exc:
                if self._stop.is_set():
                    return
                GLib.idle_add(self._show_error, str(exc))
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    return
                GLib.idle_add(self._show_error, f"error inesperado: {exc}")
            self._stop.wait(0.7)

    def _apply_status(self, status: dict[str, Any]) -> bool:
        self._connected = True
        self._last_status = status
        state = status.get("state", "unknown")
        self.state_label.set_text(STATE_LABELS.get(state, state))

        output = status.get("output") or "—"
        camera = status.get("camera") or "—"
        self.detail_label.set_text(f"Salida: {output}\nCámara: {camera}")

        buffer = status.get("buffer", {})
        seconds = float(buffer.get("seconds", 0))
        target = float(buffer.get("target_seconds", 20))
        fraction = float(buffer.get("fill", 0))
        self.progress.set_fraction(max(0.0, min(1.0, fraction)))
        self.progress.set_text(f"{seconds:.1f} / {target:.0f} s")
        self.buffer_label.set_text(
            f"Buffer: {seconds:.1f} s de {target:.0f} s · "
            f"{_fmt_bytes(buffer.get('bytes', 0))}"
        )

        loop = status.get("loop", {})
        if loop:
            kind = "match cut" if loop.get("used_match") else "fallback"
            self.loop_label.set_text(
                f"Loop: {loop.get('seconds', 0):.1f} s ({kind}, score {loop.get('score')})"
            )
        else:
            self.loop_label.set_text("Loop: —")

        error = status.get("last_error")
        if error:
            self.banner.set_title(error)
            self.banner.set_revealed(True)
        elif output == "fakesink" and not status.get("test_mode"):
            self.banner.set_title(
                "Sin cámara virtual: falta /dev/video10. Ejecuta `diputetacam doctor`."
            )
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

        transitioning = state in {"starting", "entering_loop", "returning"}
        self.loop_button.set_sensitive(state == "live")
        self.live_button.set_sensitive(state in {"loop", "standby", "error"})
        self.standby_button.set_sensitive(state in {"live", "loop"} and not transitioning)
        return False

    def _show_error(self, message: str) -> bool:
        if self._connected:
            self._connected = False
        self.banner.set_title(f"Daemon no disponible: {message}")
        self.banner.set_revealed(True)
        self.state_label.set_text("Sin conexión")
        self.detail_label.set_text(
            "Arranca el servicio con:\n"
            "systemctl --user start diputetacamd"
        )
        self.loop_button.set_sensitive(False)
        self.live_button.set_sensitive(False)
        self.standby_button.set_sensitive(False)
        return False

    def _show_command_error(self, message: str) -> bool:
        self.banner.set_title(message)
        self.banner.set_revealed(True)
        return False

    def _send(self, command: str) -> None:
        threading.Thread(
            target=self._send_worker,
            args=(command,),
            name=f"gui-{command}",
            daemon=True,
        ).start()

    def _send_worker(self, command: str) -> None:
        try:
            response = IpcClient(timeout=5.0).request(command)
        except IpcError as exc:
            GLib.idle_add(self._show_error, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._show_error, f"error inesperado: {exc}")
            return
        if not response.get("ok"):
            GLib.idle_add(self._show_command_error, response.get("error", "comando fallido"))


class DiputetacamApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="io.github.skalanux.Diputetacam")
        self.window: DiputetacamWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = DiputetacamWindow(self)
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    app = DiputetacamApp()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
