from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from . import __version__
from .config import config_path, load_config
from .detect import list_cameras
from .ipc import IpcClient, IpcError, socket_path


def _fmt_bytes(value: float | int) -> str:
    value = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _print_status(status: dict[str, Any]) -> None:
    buffer = status.get("buffer", {})
    loop = status.get("loop", {})
    print(f"Estado:       {status.get('state')}")
    print(f"Salida:       {status.get('output')}")
    print(f"Cámara:       {status.get('camera') or '—'}")
    print(f"Fuente activa:{status.get('active_source') or '—'}")
    print(
        "Buffer:       "
        f"{buffer.get('seconds', 0):.1f} / {buffer.get('target_seconds', 0):.1f} s "
        f"({_fmt_bytes(buffer.get('bytes', 0))})"
    )
    if loop:
        match = "match" if loop.get("used_match") else "fallback"
        print(f"Loop:         {loop.get('seconds', 0):.1f} s ({match}, score {loop.get('score')})")
    print(f"Grabando:     {'sí' if status.get('recording') else 'no'}")
    print(f"Freeze:       {status.get('freeze_ms')} ms")
    if status.get("last_error"):
        print(f"Error:        {status['last_error']}")
    if status.get("test_mode"):
        print("Modo:         test (fakesink)")


def _client_path(args: argparse.Namespace) -> Path:
    if args.socket:
        return args.socket
    path = socket_path()
    if args.test and not os.environ.get("DIPUTETACAM_SOCKET"):
        return path.with_name("diputetacam-test.sock")
    return path


def _request(args: argparse.Namespace, command: str) -> dict[str, Any]:
    client = IpcClient(_client_path(args))
    return client.request(command)


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        response = _request(args, "status")
    except IpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("Arranca el daemon con: systemctl --user start diputetacamd", file=sys.stderr)
        return 1
    if not response.get("ok"):
        print(f"error: {response.get('error')}", file=sys.stderr)
        return 1
    status = response["status"]
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        _print_status(status)
    return 0


def _cmd_simple(args: argparse.Namespace, command: str) -> int:
    try:
        response = _request(args, command)
    except IpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(response, indent=2, ensure_ascii=False))
    elif not response.get("ok"):
        print(f"error: {response.get('error')}", file=sys.stderr)
    else:
        print(response.get("state", "ok"))
    return 0 if response.get("ok") else 1


def _cmd_devices(args: argparse.Namespace) -> int:
    if not Gst.is_initialized():
        Gst.init(None)
    cameras = list_cameras()
    if args.json:
        print(json.dumps([{"path": c.path, "name": c.name} for c in cameras], indent=2))
    else:
        if not cameras:
            print("No se encontraron cámaras V4L2.")
        for camera in cameras:
            print(f"{camera.path}\t{camera.name}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    if not Gst.is_initialized():
        Gst.init(None)
    cfg_path = Path(os.environ.get("DIPUTETACAM_CONFIG") or config_path())
    print(f"Config:       {cfg_path}{'' if cfg_path.exists() else ' (no existe; se usarán defaults)'}")
    try:
        config = load_config(cfg_path)
        print("Config load:  ok")
    except Exception as exc:  # noqa: BLE001
        print(f"Config load:  ERROR: {exc}")
        return 1

    checks: list[tuple[str, bool, str]] = []
    module = Path("/sys/module/v4l2loopback").exists()
    checks.append(("v4l2loopback module", module, "sudo modprobe v4l2loopback"))
    device = Path(config.output.device)
    checks.append(("output device", device.exists(), f"missing {device}"))
    checks.append(
        (
            "output writable",
            device.exists() and os.access(device, os.W_OK),
            f"check permissions/ACL on {device}",
        )
    )
    checks.append(("v4l2-ctl", shutil.which("v4l2-ctl") is not None, "install v4l2-utils"))
    for element in ("v4l2sink", "compositor", "jpegenc", "jpegdec", "imagefreeze", "appsrc", "appsink"):
        checks.append((f"gst {element}", Gst.ElementFactory.find(element) is not None, "install gst-plugins-good/bad"))
    cameras = list_cameras()
    checks.append(("camera", bool(cameras), "connect a V4L2 camera"))

    for name, ok, hint in checks:
        mark = "ok" if ok else "FALTA"
        print(f"{name:22} {mark}" + ("" if ok else f"  ({hint})"))
    if cameras:
        for camera in cameras:
            print(f"  camera: {camera.path}  {camera.name}")

    if not module or not device.exists():
        print("\nPara crear la cámara virtual en Arch/Omarchy:")
        print("  sudo pacman -S --needed v4l2loopback-dkms v4l2loopback-utils")
        print('  echo \'options v4l2loopback devices=1 video_nr=10 card_label="Virtual Camera" exclusive_caps=1 max_buffers=4\' | sudo tee /etc/modprobe.d/99-diputetacam.conf')
        print("  echo v4l2loopback | sudo tee /etc/modules-load.d/diputetacam.conf")
        print("  sudo modprobe v4l2loopback")
        print("  # si ya estaba cargado: sudo modprobe -r v4l2loopback && sudo modprobe v4l2loopback")
    return 0 if all(ok for _name, ok, _hint in checks) else 1


def _cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import main as daemon_main

    argv: list[str] = []
    if args.config:
        argv += ["--config", str(args.config)]
    if args.test:
        argv.append("--test")
    if args.debug:
        argv.append("--debug")
    if args.socket:
        argv += ["--socket", str(args.socket)]
    return daemon_main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diputetacam", description="Diputetacam control CLI")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--socket", type=Path, default=None, help="Unix socket path")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--config", type=Path, default=None, help="config TOML path")
    parser.add_argument("--test", action="store_true", help="use test mode (daemon only)")
    parser.add_argument("--debug", action="store_true", help="debug logging")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="show daemon status")
    sub.add_parser("loop", help="enter the natural loop")
    sub.add_parser("live", help="return to the live camera")
    sub.add_parser("toggle", help="toggle live/loop")
    sub.add_parser("standby", help="show the camera-off card")
    sub.add_parser("devices", help="list V4L2 cameras")
    sub.add_parser("doctor", help="check dependencies and devices")
    sub.add_parser("daemon", help="run the daemon in the foreground")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "status"):
        return _cmd_status(args)
    if args.command in {"loop", "live", "toggle", "standby"}:
        return _cmd_simple(args, args.command)
    if args.command == "devices":
        return _cmd_devices(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "daemon":
        return _cmd_daemon(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
