#!/usr/bin/env python3
"""
Agent Row Notifier

Small Windows utility that Claude Code and Codex CLI hooks can call when an
agent turn finishes or when a CLI is waiting for user input/approval.

The Dell capacitive function row is exposed as HID/input devices on current XPS
machines, but Dell does not publish a stable per-key LED API. This tool keeps
the hook integration stable and makes the hardware bytes configurable once the
right HID report is known for a specific firmware/driver build.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import threading
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from datetime import datetime
from typing import Any


APP_NAME = "kbd-agent-notify"
CONFIG_NAME = "kbd-agent-notify.config.json"
DEFAULT_CONFIG_VERSION = 1

EVENT_DONE = "done"
EVENT_WAITING = "waiting"
EVENT_TEST = "test"


def script_path() -> Path:
    return Path(__file__).resolve()


def default_config_path() -> Path:
    return script_path().with_name(CONFIG_NAME)


def default_config() -> dict[str, Any]:
    return {
        "version": DEFAULT_CONFIG_VERSION,
        "backend": "auto",
        "hid": {
            "targets": [],
            "patterns": {
                EVENT_DONE: [],
                EVENT_WAITING: [],
                EVENT_TEST: [],
            },
        },
        "kbd": {
            # Keyboard-backlight pulse notifier.
            #
            # Windows (Dell): requires the elevated Scheduled Task installed via
            # `install-kbd-task` (the BFn WMI interface is admin-only; CLI hooks
            # run non-elevated). Each pulse reads + restores the prior state.
            #
            # macOS (Apple Silicon / Intel): pulses the built-in keyboard backlight
            # via the bundled `macos/kbdflash` helper (CoreBrightness, no root).
            # Build it once with `macos/build.sh`. No calibration needed.
            "enabled": False,
            "taskName": "KbdAgentNotify",
            "requestPath": str(script_path().parent / "captures" / "kbd-request.json"),
            # --- macOS-only settings ---
            "macHelper": str(script_path().parent / "macos" / "kbdflash"),
            "macKeyboardID": None,   # None = auto-detect the built-in keyboard
            "macLevel": 1.0,         # lit brightness for a flash, 0.0-1.0
            "macFadeMs": 50,         # hardware fade speed in ms (50 = snappy)
            "patterns": {
                # 2 flashes, ~0.3 second each. level 0 = keep the saved level
                # (this firmware drives brightness by mode bit, not a numeric level).
                # The pulse inverts the resting state: dark->flash on, lit->flash off.
                EVENT_DONE: {"count": 2, "onMs": 300, "offMs": 300, "level": 0},
                EVENT_WAITING: {"count": 2, "onMs": 300, "offMs": 300, "level": 0},
                EVENT_TEST: {"count": 2, "onMs": 300, "offMs": 300, "level": 0},
            },
        },
    }


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or default_config_path()
    cfg = default_config()
    if not path.exists():
        return cfg

    with path.open("r", encoding="utf-8") as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a JSON object")
    deep_merge(cfg, loaded)
    return cfg


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value


def write_default_config(path: Path | None = None, force: bool = False) -> Path:
    path = path or default_config_path()
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(default_config(), indent=2) + "\n", encoding="utf-8")
    return path


def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def kbd_pulse_script() -> Path:
    return script_path().parent / "kbd_pulse.ps1"


def kbdflash_path(kbd: dict[str, Any]) -> Path:
    """Path to the compiled macOS kbdflash helper (macos/kbdflash)."""
    p = kbd.get("macHelper")
    if p:
        return Path(str(p)).expanduser()
    return script_path().parent / "macos" / "kbdflash"


def emit_kbd_backlight_macos(kbd: dict[str, Any], event: str, verbose: bool = False) -> bool:
    """Flash the built-in keyboard backlight on macOS via the kbdflash helper.

    Uses CoreBrightness' KeyboardBrightnessClient (no root needed for the built-in
    keyboard). kbdflash reads the prior brightness + auto-brightness state, pulses,
    and restores both. Returns True if the helper ran successfully.
    """
    helper = kbdflash_path(kbd)
    if not helper.exists():
        if verbose:
            print(f"{APP_NAME}: kbdflash helper not found at {helper}", file=sys.stderr)
        return False
    pat = (kbd.get("patterns") or {}).get(event) or {}
    count = int(pat.get("count", 2))
    on_ms = int(pat.get("onMs", 300))
    off_ms = int(pat.get("offMs", 300))
    # In a pattern, level 0/None means "use the configured macOS lit level".
    level = pat.get("level")
    if not level:
        level = kbd.get("macLevel", 1.0)
    fade = int(kbd.get("macFadeMs", 50))
    cmd = [
        str(helper), "flash",
        "--count", str(count),
        "--on-ms", str(on_ms),
        "--off-ms", str(off_ms),
        "--level", str(level),
        "--fade", str(fade),
        "--quiet",
    ]
    kid = kbd.get("macKeyboardID")
    if kid not in (None, "", "auto"):
        cmd += ["--id", str(kid)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            if verbose:
                print(f"{APP_NAME}: kbdflash failed: {(result.stderr or '').strip()}", file=sys.stderr)
            return False
        if verbose:
            print(f"{APP_NAME}: kbdflash flashed for event={event}", file=sys.stderr)
        return True
    except Exception as exc:
        if verbose:
            print(f"{APP_NAME}: kbdflash error: {exc}", file=sys.stderr)
        return False


def emit_kbd_backlight(cfg: dict[str, Any], event: str, verbose: bool = False) -> bool:
    """Trigger the elevated Dell keyboard-backlight pulse for this event.

    Non-elevated path: write the request JSON, then `schtasks /run` the task
    registered by `install-kbd-task`. The elevated task runs kbd_pulse.ps1,
    which reads + restores the prior backlight state. Returns True if the task
    was triggered (not whether the light physically pulsed; that is logged by
    the worker to captures/kbd-pulse.jsonl).
    """
    kbd = cfg.get("kbd") or {}
    if not kbd.get("enabled", False):
        return False
    if is_macos():
        return emit_kbd_backlight_macos(kbd, event, verbose=verbose)
    if not is_windows():
        return False
    task_name = str(kbd.get("taskName", "KbdAgentNotify"))
    request_path = Path(str(kbd.get("requestPath", script_path().parent / "captures" / "kbd-request.json")))
    try:
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request = {"event": event, "kbd": {"patterns": kbd.get("patterns", {})}}
        request_path.write_text(json.dumps(request), encoding="utf-8")
    except Exception as exc:
        if verbose:
            print(f"{APP_NAME}: kbd request write failed: {exc}", file=sys.stderr)
        return False
    try:
        result = subprocess.run(
            ["schtasks", "/run", "/tn", task_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            if verbose:
                msg = (result.stderr or result.stdout or "").strip()
                print(f"{APP_NAME}: kbd task '{task_name}' not triggered: {msg}", file=sys.stderr)
            return False
        if verbose:
            print(f"{APP_NAME}: kbd task '{task_name}' triggered for event={event}", file=sys.stderr)
        return True
    except Exception as exc:
        if verbose:
            print(f"{APP_NAME}: kbd task trigger failed: {exc}", file=sys.stderr)
        return False


def signal(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    event = args.event
    if event not in {EVENT_DONE, EVENT_WAITING, EVENT_TEST}:
        raise ValueError(f"Unsupported event: {event}")

    emitted = False
    backend = str(cfg.get("backend", "auto")).lower()

    if backend in {"auto", "hid"}:
        emitted = emit_hid_pattern(cfg, event, verbose=args.verbose)
        if backend == "hid" and not emitted:
            return 2

    # Keyboard-backlight pulse (elevated via Scheduled Task). This is the only
    # real transport now that fallbacks (screen/console/beep) have been removed.
    if backend in {"auto", "kbd"}:
        kbd_emitted = emit_kbd_backlight(cfg, event, verbose=args.verbose)
        emitted = kbd_emitted or emitted
        if backend == "kbd" and not kbd_emitted:
            return 2

    if args.verbose:
        print(f"{APP_NAME}: source={args.source} event={event} emitted={emitted}")
    return 0 if emitted else 1


def normalize_hex(data: str | list[int]) -> bytes:
    if isinstance(data, list):
        return bytes(int(x) & 0xFF for x in data)
    clean = data.replace("0x", "").replace(",", " ").replace("-", " ")
    parts = [p for p in clean.split() if p]
    if not parts:
        return b""
    return bytes(int(p, 16) & 0xFF for p in parts)


def emit_hid_pattern(cfg: dict[str, Any], event: str, verbose: bool = False) -> bool:
    hid_cfg = cfg.get("hid") or {}
    targets = hid_cfg.get("targets") or []
    frames = (hid_cfg.get("patterns") or {}).get(event) or []
    if not targets or not frames:
        return False
    if not is_windows():
        return False

    devices = list(hid_enumerate())
    emitted = False

    for target in targets:
        if not isinstance(target, dict):
            continue
        matching = [dev for dev in devices if hid_device_matches(dev, target)]
        if verbose:
            print(f"{APP_NAME}: target {target!r} matched {len(matching)} HID interface(s)")
        for dev in matching:
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                data = normalize_hex(frame.get("data", ""))
                mode = str(frame.get("mode", target.get("mode", "output"))).lower()
                ok = hid_send(dev["path"], data, mode=mode, verbose=verbose)
                emitted = emitted or ok
                delay = int(frame.get("delayMs", 0))
                if delay > 0:
                    time.sleep(delay / 1000.0)

    return emitted


def hid_device_matches(device: dict[str, Any], target: dict[str, Any]) -> bool:
    text = "\n".join(
        str(device.get(k, ""))
        for k in ("path", "instance_id", "manufacturer", "product")
    ).lower()
    match = target.get("match")
    if match and str(match).lower() not in text:
        return False

    for key in ("vendor_id", "product_id", "usage_page", "usage"):
        if key in target and target[key] is not None:
            wanted = int(str(target[key]), 0) if isinstance(target[key], str) else int(target[key])
            if int(device.get(key, -1)) != wanted:
                return False
    return True


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __str__(self) -> str:
        tail = bytes(self.Data4)
        return (
            f"{self.Data1:08x}-{self.Data2:04x}-{self.Data3:04x}-"
            f"{tail[0]:02x}{tail[1]:02x}-{tail[2:].hex()}"
        )


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.ULONG),
        ("VendorID", wintypes.USHORT),
        ("ProductID", wintypes.USHORT),
        ("VersionNumber", wintypes.USHORT),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


class HIDP_CAPS_RANGE(ctypes.Structure):
    _fields_ = [
        ("UsageMin", wintypes.USHORT),
        ("UsageMax", wintypes.USHORT),
        ("StringMin", wintypes.USHORT),
        ("StringMax", wintypes.USHORT),
        ("DesignatorMin", wintypes.USHORT),
        ("DesignatorMax", wintypes.USHORT),
        ("DataIndexMin", wintypes.USHORT),
        ("DataIndexMax", wintypes.USHORT),
    ]


class HIDP_CAPS_NOT_RANGE(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("Reserved1", wintypes.USHORT),
        ("StringIndex", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT),
        ("DesignatorIndex", wintypes.USHORT),
        ("Reserved3", wintypes.USHORT),
        ("DataIndex", wintypes.USHORT),
        ("Reserved4", wintypes.USHORT),
    ]


class HIDP_CAPS_UNION(ctypes.Union):
    _fields_ = [
        ("Range", HIDP_CAPS_RANGE),
        ("NotRange", HIDP_CAPS_NOT_RANGE),
    ]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("UsagePage", wintypes.USHORT),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", wintypes.USHORT),
        ("LinkCollection", wintypes.USHORT),
        ("LinkUsage", wintypes.USHORT),
        ("LinkUsagePage", wintypes.USHORT),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", wintypes.USHORT),
        ("ReportCount", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT * 5),
        ("UnitsExp", wintypes.ULONG),
        ("Units", wintypes.ULONG),
        ("LogicalMin", ctypes.c_long),
        ("LogicalMax", ctypes.c_long),
        ("PhysicalMin", ctypes.c_long),
        ("PhysicalMax", ctypes.c_long),
        ("u", HIDP_CAPS_UNION),
    ]


def hid_enumerate() -> list[dict[str, Any]]:
    if not is_windows():
        return []

    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    hid_guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(hid_guid))

    DIGCF_PRESENT = 0x00000002
    DIGCF_DEVICEINTERFACE = 0x00000010
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.LPCWSTR,
        wintypes.HWND,
        wintypes.DWORD,
    ]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(SP_DEVINFO_DATA),
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(SP_DEVINFO_DATA),
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL

    devs = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(hid_guid),
        None,
        None,
        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
    )
    if devs == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    devices: list[dict[str, Any]] = []
    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            ok = setupapi.SetupDiEnumDeviceInterfaces(
                devs,
                None,
                ctypes.byref(hid_guid),
                index,
                ctypes.byref(iface),
            )
            if not ok:
                break

            required = wintypes.DWORD()
            devinfo = SP_DEVINFO_DATA()
            devinfo.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                devs,
                ctypes.byref(iface),
                None,
                0,
                ctypes.byref(required),
                None,
            )

            buf = ctypes.create_string_buffer(required.value)
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            )
            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                devs,
                ctypes.byref(iface),
                buf,
                required.value,
                None,
                ctypes.byref(devinfo),
            )
            if ok:
                path_offset = 4
                path = ctypes.wstring_at(ctypes.addressof(buf) + path_offset)
                instance_id = _device_instance_id(setupapi, devs, devinfo)
                devices.append(_hid_describe_path(path, instance_id, kernel32, hid))
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)

    return devices


def _device_instance_id(setupapi: Any, devs: Any, devinfo: SP_DEVINFO_DATA) -> str:
    required = wintypes.DWORD()
    setupapi.SetupDiGetDeviceInstanceIdW(
        devs,
        ctypes.byref(devinfo),
        None,
        0,
        ctypes.byref(required),
    )
    if required.value <= 1:
        return ""
    buf = ctypes.create_unicode_buffer(required.value)
    if not setupapi.SetupDiGetDeviceInstanceIdW(
        devs,
        ctypes.byref(devinfo),
        buf,
        required.value,
        None,
    ):
        return ""
    return buf.value


def _open_hid(kernel32: Any, path: str, access: int) -> Any:
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    return kernel32.CreateFileW(
        path,
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )


def _hid_describe_path(path: str, instance_id: str, kernel32: Any, hid: Any) -> dict[str, Any]:
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    handle = _open_hid(kernel32, path, 0)
    device: dict[str, Any] = {
        "path": path,
        "instance_id": instance_id,
        "vendor_id": None,
        "product_id": None,
        "version": None,
        "usage_page": None,
        "usage": None,
        "input_report_len": None,
        "output_report_len": None,
        "feature_report_len": None,
        "open_error": None,
    }
    if handle == INVALID_HANDLE_VALUE:
        device["open_error"] = ctypes.get_last_error()
        return device

    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
            device["vendor_id"] = attrs.VendorID
            device["product_id"] = attrs.ProductID
            device["version"] = attrs.VersionNumber

        preparsed = ctypes.c_void_p()
        if hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            try:
                caps = HIDP_CAPS()
                hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
                device.update(
                    {
                        "usage_page": caps.UsagePage,
                        "usage": caps.Usage,
                        "input_report_len": caps.InputReportByteLength,
                        "output_report_len": caps.OutputReportByteLength,
                        "feature_report_len": caps.FeatureReportByteLength,
                        "output_button_caps": caps.NumberOutputButtonCaps,
                        "output_value_caps": caps.NumberOutputValueCaps,
                        "feature_button_caps": caps.NumberFeatureButtonCaps,
                        "feature_value_caps": caps.NumberFeatureValueCaps,
                    }
                )
            finally:
                hid.HidD_FreePreparsedData(preparsed)
    finally:
        kernel32.CloseHandle(handle)

    return device


def hid_send(path: str, data: bytes, mode: str = "output", verbose: bool = False) -> bool:
    if not data:
        return False
    if not is_windows():
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)

    GENERIC_WRITE = 0x40000000
    GENERIC_READ = 0x80000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    access = GENERIC_WRITE if mode in {"output", "set-output"} else (GENERIC_READ | GENERIC_WRITE)
    handle = _open_hid(kernel32, path, access)
    if handle == INVALID_HANDLE_VALUE:
        if verbose:
            print(f"{APP_NAME}: open failed for {path}: {ctypes.get_last_error()}")
        return False

    try:
        buf = ctypes.create_string_buffer(data, len(data))
        if mode == "feature":
            ok = bool(hid.HidD_SetFeature(handle, buf, len(data)))
            if verbose and not ok:
                print(f"{APP_NAME}: HidD_SetFeature failed: {ctypes.get_last_error()}")
            return ok
        if mode == "set-output":
            ok = bool(hid.HidD_SetOutputReport(handle, buf, len(data)))
            if verbose and not ok:
                print(f"{APP_NAME}: HidD_SetOutputReport failed: {ctypes.get_last_error()}")
            return ok

        written = wintypes.DWORD()
        ok = bool(kernel32.WriteFile(handle, buf, len(data), ctypes.byref(written), None))
        if verbose and not ok:
            print(f"{APP_NAME}: WriteFile failed: {ctypes.get_last_error()}")
        return ok and written.value == len(data)
    finally:
        kernel32.CloseHandle(handle)


def hid_list(args: argparse.Namespace) -> int:
    devices = hid_enumerate()
    filtered = []
    for dev in devices:
        if args.match:
            text = json.dumps(dev, sort_keys=True).lower()
            if args.match.lower() not in text:
                continue
        filtered.append(dev)

    if args.json:
        print(json.dumps(filtered, indent=2))
        return 0

    for idx, dev in enumerate(filtered, 1):
        vid = _hex_or_blank(dev.get("vendor_id"), 4)
        pid = _hex_or_blank(dev.get("product_id"), 4)
        up = _hex_or_blank(dev.get("usage_page"), 4)
        usage = _hex_or_blank(dev.get("usage"), 4)
        print(f"{idx}. vid={vid} pid={pid} usage_page={up} usage={usage}")
        print(f"   instance: {dev.get('instance_id')}")
        print(f"   path:     {dev.get('path')}")
        print(
            "   reports:  "
            f"in={dev.get('input_report_len')} "
            f"out={dev.get('output_report_len')} "
            f"feature={dev.get('feature_report_len')}"
        )
        if dev.get("open_error"):
            print(f"   open_error: {dev.get('open_error')}")
    return 0


def hid_send_command(args: argparse.Namespace) -> int:
    target: dict[str, Any] = {"match": args.match}
    if args.vendor_id is not None:
        target["vendor_id"] = args.vendor_id
    if args.product_id is not None:
        target["product_id"] = args.product_id
    if args.usage_page is not None:
        target["usage_page"] = args.usage_page
    if args.usage is not None:
        target["usage"] = args.usage

    data = normalize_hex(args.data)
    devices = [dev for dev in hid_enumerate() if hid_device_matches(dev, target)]
    if not devices:
        print(f"{APP_NAME}: no matching HID device", file=sys.stderr)
        return 1

    ok_count = 0
    for dev in devices:
        ok = hid_send(dev["path"], data, mode=args.mode, verbose=args.verbose)
        ok_count += int(ok)
        if args.verbose:
            print(f"{APP_NAME}: sent={ok} instance={dev.get('instance_id')}")
    return 0 if ok_count else 2


def hid_open_probe_command(args: argparse.Namespace) -> int:
    devices = [dev for dev in hid_enumerate() if hid_device_matches(dev, {"match": args.match})]
    if not devices:
        print(f"{APP_NAME}: no matching HID device", file=sys.stderr)
        return 1

    if not is_windows():
        return 1

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    modes = [
        ("none", 0),
        ("read", GENERIC_READ),
        ("write", GENERIC_WRITE),
        ("readwrite", GENERIC_READ | GENERIC_WRITE),
    ]

    for dev in devices:
        print(f"instance: {dev.get('instance_id')}")
        print(f"path:     {dev.get('path')}")
        print(
            "reports:  "
            f"in={dev.get('input_report_len')} "
            f"out={dev.get('output_report_len')} "
            f"feature={dev.get('feature_report_len')}"
        )
        for name, access in modes:
            handle = _open_hid(kernel32, dev["path"], access)
            if handle == INVALID_HANDLE_VALUE:
                print(f"open {name}: failed error={ctypes.get_last_error()}")
                continue
            kernel32.CloseHandle(handle)
            print(f"open {name}: ok")
        print()
    return 0


def hid_caps_command(args: argparse.Namespace) -> int:
    devices = [dev for dev in hid_enumerate() if not args.match or hid_device_matches(dev, {"match": args.match})]
    if args.json:
        print(json.dumps([hid_caps(dev["path"], dev) for dev in devices], indent=2))
        return 0

    for dev in devices:
        caps = hid_caps(dev["path"], dev)
        print(f"instance: {caps.get('instance_id')}")
        print(f"path:     {caps.get('path')}")
        print(
            "reports:  "
            f"in={caps.get('input_report_len')} "
            f"out={caps.get('output_report_len')} "
            f"feature={caps.get('feature_report_len')}"
        )
        for kind in ("input", "output", "feature"):
            values = caps.get(f"{kind}_values") or []
            if values:
                print(f"{kind} value caps:")
                for value in values:
                    print(
                        "  "
                        f"report_id={value['report_id']} "
                        f"usage_page=0x{value['usage_page']:04x} "
                        f"usage={value['usage']} "
                        f"bit_size={value['bit_size']} "
                        f"report_count={value['report_count']} "
                        f"logical={value['logical_min']}..{value['logical_max']}"
                    )
        print()
    return 0


def hid_caps(path: str, base: dict[str, Any] | None = None) -> dict[str, Any]:
    if not is_windows():
        return {}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    handle = _open_hid(kernel32, path, 0)
    result = dict(base or {})
    if handle == INVALID_HANDLE_VALUE:
        result["open_error"] = ctypes.get_last_error()
        return result

    try:
        preparsed = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            result["preparsed_error"] = ctypes.get_last_error()
            return result
        try:
            caps = HIDP_CAPS()
            hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
            result.update(
                {
                    "input_report_len": caps.InputReportByteLength,
                    "output_report_len": caps.OutputReportByteLength,
                    "feature_report_len": caps.FeatureReportByteLength,
                    "input_values": _hid_value_caps(hid, preparsed, report_type=0, count=caps.NumberInputValueCaps),
                    "output_values": _hid_value_caps(hid, preparsed, report_type=1, count=caps.NumberOutputValueCaps),
                    "feature_values": _hid_value_caps(hid, preparsed, report_type=2, count=caps.NumberFeatureValueCaps),
                }
            )
        finally:
            hid.HidD_FreePreparsedData(preparsed)
    finally:
        kernel32.CloseHandle(handle)
    return result


def _hid_value_caps(hid: Any, preparsed: Any, report_type: int, count: int) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    length = wintypes.USHORT(count)
    arr = (HIDP_VALUE_CAPS * count)()
    hid.HidP_GetValueCaps(report_type, arr, ctypes.byref(length), preparsed)
    values = []
    for item in arr[: length.value]:
        if item.IsRange:
            usage = f"0x{item.Range.UsageMin:04x}-0x{item.Range.UsageMax:04x}"
        else:
            usage = f"0x{item.NotRange.Usage:04x}"
        values.append(
            {
                "report_id": int(item.ReportID),
                "usage_page": int(item.UsagePage),
                "usage": usage,
                "bit_size": int(item.BitSize),
                "report_count": int(item.ReportCount),
                "logical_min": int(item.LogicalMin),
                "logical_max": int(item.LogicalMax),
                "physical_min": int(item.PhysicalMin),
                "physical_max": int(item.PhysicalMax),
                "is_range": bool(item.IsRange),
            }
        )
    return values


def hid_get_feature_command(args: argparse.Namespace) -> int:
    devices = [dev for dev in hid_enumerate() if hid_device_matches(dev, {"match": args.match})]
    if not devices:
        print(f"{APP_NAME}: no matching HID device", file=sys.stderr)
        return 1
    ok_count = 0
    for dev in devices:
        data = hid_get_feature(dev["path"], args.report_id, args.length, verbose=args.verbose)
        if data is None:
            continue
        ok_count += 1
        print(f"{dev.get('instance_id')}: {data.hex(' ')}")
    return 0 if ok_count else 2


def hid_feature_snapshot_command(args: argparse.Namespace) -> int:
    devices = [
        dev
        for dev in hid_enumerate()
        if hid_device_matches(dev, {"match": args.match}) and int(dev.get("feature_report_len") or 0) > 0
    ]
    if not devices:
        print(f"{APP_NAME}: no matching HID feature device", file=sys.stderr)
        return 1

    snapshot: dict[str, Any] = {
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "match": args.match,
        "devices": [],
    }
    ok_count = 0
    for dev in devices:
        caps = hid_caps(dev["path"], dev)
        report_ids = sorted({int(item["report_id"]) for item in caps.get("feature_values") or []})
        if args.report_id is not None:
            report_ids = [args.report_id]
        if not report_ids and int(dev.get("feature_report_len") or 0) > 0:
            report_ids = [0]

        item: dict[str, Any] = {
            "instance_id": dev.get("instance_id"),
            "path": dev.get("path"),
            "vendor_id": dev.get("vendor_id"),
            "product_id": dev.get("product_id"),
            "usage_page": dev.get("usage_page"),
            "usage": dev.get("usage"),
            "feature_report_len": dev.get("feature_report_len"),
            "reports": [],
        }
        for report_id in report_ids:
            length = args.length or int(dev.get("feature_report_len") or 0)
            data = hid_get_feature(dev["path"], report_id, length, verbose=args.verbose)
            report: dict[str, Any] = {
                "report_id": report_id,
                "length": length,
                "data": None if data is None else data.hex(" "),
            }
            if data is not None:
                ok_count += 1
            item["reports"].append(report)
        snapshot["devices"].append(item)

    text = json.dumps(snapshot, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(args.output)
    else:
        print(text, end="")
    return 0 if ok_count else 2


def hid_get_feature(path: str, report_id: int, length: int, verbose: bool = False) -> bytes | None:
    if not is_windows():
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    handle = _open_hid(kernel32, path, GENERIC_READ | GENERIC_WRITE)
    if handle == INVALID_HANDLE_VALUE:
        if verbose:
            print(f"{APP_NAME}: open failed for {path}: {ctypes.get_last_error()}")
        return None
    try:
        buf = ctypes.create_string_buffer(length)
        buf[0] = int(report_id) & 0xFF
        if not hid.HidD_GetFeature(handle, buf, length):
            if verbose:
                print(f"{APP_NAME}: HidD_GetFeature failed: {ctypes.get_last_error()}")
            return None
        return bytes(buf.raw)
    finally:
        kernel32.CloseHandle(handle)


def dell_ctp_dll_path() -> Path | None:
    candidates = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env_name)
        if root:
            candidates.append(
                Path(root)
                / "Dell"
                / "DellOptimizer"
                / "Plugins"
                / "Bootstrap"
                / "CollaborationTouchpadPlugin.CTPAgent.dll"
            )
    for path in candidates:
        if path.exists():
            return path
    return None


def dell_ctp_probe_command(args: argparse.Namespace) -> int:
    path = args.dll or dell_ctp_dll_path()
    if path is None or not path.exists():
        print(f"{APP_NAME}: Dell CTPAgent DLL was not found", file=sys.stderr)
        return 1
    if not is_windows():
        return 1

    dll = ctypes.CDLL(str(path))
    init = getattr(dll, "Init")
    uninit = getattr(dll, "UnInit")
    init.restype = ctypes.c_int
    uninit.restype = ctypes.c_int

    result_names = {
        0: "OK",
        1: "UNKNOWN",
        2: "NO_CTP",
    }

    print(f"dll: {path}")
    init_result = init()
    print(f"Init: {init_result} ({result_names.get(init_result, 'unmapped')})")
    try:
        for name in ("IsCTPPresent", "IsFeatureEnabled", "IsActivated", "GetCTPBrightness"):
            try:
                func = getattr(dll, name)
            except AttributeError:
                print(f"{name}: missing")
                continue
            func.restype = ctypes.c_int
            try:
                print(f"{name}: {func()}")
            except Exception as exc:
                print(f"{name}: failed: {exc}")

        if args.test_lights:
            for name in ("SetButtonVisible", "SetButtonLight"):
                func = getattr(dll, name)
                func.argtypes = [ctypes.c_int, ctypes.c_int]
                func.restype = ctypes.c_int
            for button in range(args.buttons):
                visible = dll.SetButtonVisible(button, 1)
                on = dll.SetButtonLight(button, 1)
                print(f"button {button}: visible={visible} on={on}")
                time.sleep(args.delay_ms / 1000.0)
            time.sleep(args.hold_ms / 1000.0)
            for button in range(args.buttons):
                off = dll.SetButtonLight(button, 0)
                print(f"button {button}: off={off}")
    finally:
        print(f"UnInit: {uninit()}")
    return 0 if init_result == 0 else 2


def hid_read_command(args: argparse.Namespace) -> int:
    devices = [dev for dev in hid_enumerate() if hid_device_matches(dev, {"match": args.match})]
    devices = [dev for dev in devices if int(dev.get("input_report_len") or 0) > 0]
    if not devices:
        print(f"{APP_NAME}: no matching HID input device", file=sys.stderr)
        return 1

    stop_event = threading.Event()
    lock = threading.Lock()
    seen = {"count": 0}

    def on_report(dev: dict[str, Any], data: bytes) -> None:
        seen["count"] += 1
        now = time.time()
        report_id = data[0] if data else None
        with lock:
            print(
                f"{now:.3f} {dev.get('instance_id')} "
                f"rid=0x{report_id:02x} len={len(data)} data={data.hex(' ')}",
                flush=True,
            )

    threads = []
    for dev in devices:
        length = args.length or int(dev.get("input_report_len") or 0)
        t = threading.Thread(
            target=_hid_read_loop,
            args=(dev, length, args.seconds, stop_event, on_report, args.verbose),
            daemon=True,
        )
        threads.append(t)
        t.start()

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(0.05)
    except KeyboardInterrupt:
        stop_event.set()
        raise
    finally:
        stop_event.set()

    for t in threads:
        t.join(timeout=1)

    if args.verbose:
        print(f"{APP_NAME}: captured {seen['count']} report(s)")
    return 0


def hid_record_command(args: argparse.Namespace) -> int:
    matches = args.match or []
    all_devices = hid_enumerate()
    devices: list[dict[str, Any]] = []
    for match in matches:
        devices.extend(dev for dev in all_devices if hid_device_matches(dev, {"match": match}))

    unique: dict[str, dict[str, Any]] = {}
    for dev in devices:
        if int(dev.get("input_report_len") or 0) > 0:
            unique[str(dev["path"])] = dev
    devices = list(unique.values())

    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as f:
        f.write(f"# {APP_NAME} hid-record started {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"# matches: {', '.join(matches)}\n")
        if not devices:
            f.write("# no matching HID input devices\n")
            return 1
        for dev in devices:
            f.write(
                "# device "
                f"len={dev.get('input_report_len')} "
                f"instance={dev.get('instance_id')} "
                f"path={dev.get('path')}\n"
            )
        f.flush()

        stop_event = threading.Event()
        lock = threading.Lock()
        seen = {"count": 0}

        def on_report(dev: dict[str, Any], data: bytes) -> None:
            now = time.time()
            report_id = data[0] if data else 0
            line = (
                f"{now:.3f} {dev.get('instance_id')} "
                f"rid=0x{report_id:02x} len={len(data)} data={data.hex(' ')}\n"
            )
            with lock:
                seen["count"] += 1
                f.write(line)
                f.flush()

        threads = []
        for dev in devices:
            length = int(dev.get("input_report_len") or 0)
            t = threading.Thread(
                target=_hid_read_loop,
                args=(dev, length, args.seconds, stop_event, on_report, args.verbose),
                daemon=True,
            )
            threads.append(t)
            t.start()

        deadline = time.time() + args.seconds
        try:
            while time.time() < deadline:
                time.sleep(0.05)
        finally:
            stop_event.set()
            for t in threads:
                t.join(timeout=1)
            f.write(
                f"# {APP_NAME} hid-record stopped {datetime.now().isoformat(timespec='seconds')} "
                f"reports={seen['count']}\n"
            )
    return 0


def _hid_read_loop(
    dev: dict[str, Any],
    length: int,
    seconds: float,
    stop_event: threading.Event,
    on_report: Any,
    verbose: bool,
) -> None:
    try:
        import pywintypes
        import win32event
        import win32file
    except ImportError as exc:
        if verbose:
            print(f"{APP_NAME}: pywin32 is required for hid-read: {exc}", file=sys.stderr)
        return

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_FLAG_OVERLAPPED = 0x40000000
    ERROR_IO_PENDING = 997
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258

    try:
        handle = win32file.CreateFile(
            dev["path"],
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
            None,
        )
    except pywintypes.error as exc:
        if verbose:
            print(f"{APP_NAME}: read open failed for {dev.get('instance_id')}: {exc}", file=sys.stderr)
        return

    deadline = time.time() + seconds
    try:
        while not stop_event.is_set() and time.time() < deadline:
            event = win32event.CreateEvent(None, True, False, None)
            ov = pywintypes.OVERLAPPED()
            ov.hEvent = event
            buf = win32file.AllocateReadBuffer(length)
            try:
                rc, data = win32file.ReadFile(handle, buf, ov)
                if rc == ERROR_IO_PENDING:
                    remaining_ms = max(1, int((deadline - time.time()) * 1000))
                    wait_ms = min(250, remaining_ms)
                    wait_rc = win32event.WaitForSingleObject(event, wait_ms)
                    if wait_rc == WAIT_TIMEOUT:
                        try:
                            win32file.CancelIo(handle)
                        except Exception:
                            pass
                        continue
                    if wait_rc != WAIT_OBJECT_0:
                        continue
                    try:
                        read_len = win32file.GetOverlappedResult(handle, ov, False)
                    except pywintypes.error:
                        continue
                    data = bytes(buf[:read_len])
                else:
                    data = bytes(data)
                if data:
                    on_report(dev, data)
            except pywintypes.error as exc:
                if verbose:
                    print(f"{APP_NAME}: read failed for {dev.get('instance_id')}: {exc}", file=sys.stderr)
                time.sleep(0.1)
    finally:
        handle.Close()


def _hex_or_blank(value: Any, width: int) -> str:
    if value is None:
        return "-" * width
    return f"0x{int(value):0{width}x}"


def install_hooks(args: argparse.Namespace) -> int:
    cfg_path = write_default_config(args.config, force=False)
    installed: list[str] = []
    if args.target in {"all", "claude"}:
        install_claude_hooks(cfg_path)
        installed.append("Claude Code")
    if args.target in {"all", "codex"}:
        install_codex_hooks(cfg_path)
        installed.append("Codex CLI")

    print(f"Installed {APP_NAME} hooks for: {', '.join(installed)}")
    print(f"Config: {cfg_path}")
    print("Codex: run /hooks in a new Codex session to review and trust the new hooks.")
    print("Claude: restart Claude Code sessions so settings are reloaded.")
    return 0


def install_claude_hooks(cfg_path: Path) -> None:
    settings_path = Path.home() / ".claude" / "settings.json"
    data = read_json_object(settings_path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{settings_path}: hooks must be an object")

    add_claude_event(
        hooks,
        "Stop",
        matcher=None,
        event=EVENT_DONE,
        cfg_path=cfg_path,
    )
    add_claude_event(
        hooks,
        "Notification",
        matcher="permission_prompt|idle_prompt|elicitation_dialog",
        event=EVENT_WAITING,
        cfg_path=cfg_path,
    )
    write_json_object(settings_path, data)


def add_claude_event(
    hooks: dict[str, Any],
    event_name: str,
    matcher: str | None,
    event: str,
    cfg_path: Path,
) -> None:
    groups = hooks.setdefault(event_name, [])
    if not isinstance(groups, list):
        raise ValueError(f"Claude hook event {event_name} must be a list")

    handler = {
        "type": "command",
        "command": sys.executable,
        "args": [
            str(script_path()),
            "--config",
            str(cfg_path),
            "signal",
            "--source",
            "claude",
            "--event",
            event,
        ],
        "timeout": 5,
    }

    for group in groups:
        if not isinstance(group, dict):
            continue
        if matcher is None and "matcher" in group:
            continue
        if matcher is not None and group.get("matcher") != matcher:
            continue
        group_hooks = group.setdefault("hooks", [])
        if not isinstance(group_hooks, list):
            raise ValueError(f"Claude hook group for {event_name} has non-list hooks")
        prune_own_handlers(group_hooks)
        group_hooks.append(handler)
        return

    group: dict[str, Any] = {"hooks": [handler]}
    if matcher is not None:
        group["matcher"] = matcher
    groups.append(group)


def install_codex_hooks(cfg_path: Path) -> None:
    hooks_path = Path.home() / ".codex" / "hooks.json"
    data = read_json_object(hooks_path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{hooks_path}: hooks must be an object")

    add_codex_event(hooks, "Stop", matcher=None, event=EVENT_DONE, cfg_path=cfg_path)
    add_codex_event(
        hooks,
        "PermissionRequest",
        matcher="*",
        event=EVENT_WAITING,
        cfg_path=cfg_path,
    )
    write_json_object(hooks_path, data)


def add_codex_event(
    hooks: dict[str, Any],
    event_name: str,
    matcher: str | None,
    event: str,
    cfg_path: Path,
) -> None:
    groups = hooks.setdefault(event_name, [])
    if not isinstance(groups, list):
        raise ValueError(f"Codex hook event {event_name} must be a list")

    cmd = subprocess.list2cmdline(
        [
            sys.executable,
            str(script_path()),
            "--config",
            str(cfg_path),
            "signal",
            "--source",
            "codex",
            "--event",
            event,
        ]
    )
    handler = {
        "type": "command",
        "command": cmd,
        "commandWindows": cmd,
        "timeout": 5,
    }

    for group in groups:
        if not isinstance(group, dict):
            continue
        if matcher is None and "matcher" in group:
            continue
        if matcher is not None and group.get("matcher") != matcher:
            continue
        group_hooks = group.setdefault("hooks", [])
        if not isinstance(group_hooks, list):
            raise ValueError(f"Codex hook group for {event_name} has non-list hooks")
        prune_own_handlers(group_hooks)
        group_hooks.append(handler)
        return

    group = {"hooks": [handler]}
    if matcher is not None:
        group["matcher"] = matcher
    groups.append(group)


def prune_own_handlers(existing: list[Any]) -> None:
    existing[:] = [item for item in existing if not is_own_handler(item)]


def is_own_handler(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    haystack = json.dumps(item, sort_keys=True).replace("\\\\", "\\").lower()
    return str(script_path()).lower() in haystack


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json_object(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"{path.name}.{stamp}.bak")
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def show_status(args: argparse.Namespace) -> int:
    cfg_path = args.config or default_config_path()
    print(f"script: {script_path()}")
    print(f"config: {cfg_path} exists={cfg_path.exists()}")
    print(f"python: {sys.executable}")
    print(f"windows: {is_windows()}")
    print(f"macos: {is_macos()}")
    if is_macos():
        cfg = load_config(args.config)
        helper = kbdflash_path(cfg.get("kbd") or {})
        print(f"kbdflash: {helper} exists={helper.exists()}")
        print(f"kbd enabled: {(cfg.get('kbd') or {}).get('enabled', False)}")
    print(f"claude settings: {(Path.home() / '.claude' / 'settings.json')}")
    print(f"codex hooks: {(Path.home() / '.codex' / 'hooks.json')}")
    return 0


def _run_powershell(ps_args: list[str], elevated: bool = False, timeout: int = 60) -> subprocess.CompletedProcess:
    base = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    return subprocess.run(base + ps_args, capture_output=True, text=True, timeout=timeout)


def kbd_test_macos(args: argparse.Namespace) -> int:
    """macOS kbd-test: run the kbdflash helper directly (read or flash)."""
    cfg = load_config(args.config)
    kbd = cfg.get("kbd") or {}
    helper = kbdflash_path(kbd)
    if not helper.exists():
        print(f"{APP_NAME}: kbdflash helper not found at {helper}", file=sys.stderr)
        print("Build it first:  bash macos/build.sh", file=sys.stderr)
        return 1
    if args.read_only:
        result = subprocess.run([str(helper), "read"], capture_output=True, text=True, timeout=15)
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        return result.returncode
    # Temporarily force-enable so a fresh/default config can be tested, and apply
    # any timing overrides without mutating the loaded config's nested dicts.
    kbd = json.loads(json.dumps(kbd))
    kbd["enabled"] = True
    pat = kbd.setdefault("patterns", {}).setdefault(args.event, {})
    if args.count:
        pat["count"] = args.count
    if args.on_ms:
        pat["onMs"] = args.on_ms
    if args.off_ms:
        pat["offMs"] = args.off_ms
    ok = emit_kbd_backlight_macos(kbd, args.event, verbose=True)
    return 0 if ok else 2


def kbd_test_command(args: argparse.Namespace) -> int:
    """Test the keyboard-backlight pulse. --read-only just dumps current state."""
    if is_macos():
        return kbd_test_macos(args)
    if not is_windows():
        print(f"{APP_NAME}: kbd-test requires Windows or macOS", file=sys.stderr)
        return 1
    script = kbd_pulse_script()
    if not script.exists():
        print(f"{APP_NAME}: missing {script}", file=sys.stderr)
        return 1
    ps_args = ["-File", str(script)]
    if args.read_only:
        ps_args.append("-ReadOnly")
    else:
        ps_args += ["-Event", args.event]
        if args.count:
            ps_args += ["-CountOverride", str(args.count)]
        if args.on_ms:
            ps_args += ["-OnMsOverride", str(args.on_ms)]
        if args.off_ms:
            ps_args += ["-OffMsOverride", str(args.off_ms)]
    result = _run_powershell(ps_args)
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        msg = (result.stderr or "").strip()
        if "Access denied" in msg or "not found (is this elevated" in (result.stdout or ""):
            print(f"{APP_NAME}: kbd-test must be run from an ELEVATED shell.", file=sys.stderr)
        elif msg:
            print(msg, file=sys.stderr)
    return result.returncode


def install_kbd_task_command(args: argparse.Namespace) -> int:
    """Register the elevated Scheduled Task that runs kbd_pulse.ps1 with no UAC prompt.

    Must be run from an elevated shell (schtasks /create /rl HIGHEST needs admin).
    """
    if not is_windows():
        print(f"{APP_NAME}: install-kbd-task requires Windows", file=sys.stderr)
        return 1
    cfg = load_config(args.config)
    kbd = cfg.get("kbd") or {}
    task_name = str(kbd.get("taskName", "KbdAgentNotify"))
    request_path = str(kbd.get("requestPath", script_path().parent / "captures" / "kbd-request.json"))
    script = kbd_pulse_script()
    if not script.exists():
        print(f"{APP_NAME}: missing {script}", file=sys.stderr)
        return 1
    # The action runs the worker against the request file the hook drops.
    tr = (
        f'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
        f'-File "{script}" -RequestPath "{request_path}"'
    )

    # A task created with the default InteractiveToken logon runs with the
    # caller's *filtered* (non-elevated) token when triggered by schtasks /run,
    # so the admin-only Dell WMI call fails. Two ways to get a full elevated
    # token non-interactively:
    #   - S4U (default): runs elevated for the logged-on user, NO stored
    #     password. Created via Register-ScheduledTask (schtasks can't do S4U).
    #   - Password: stores account credentials via schtasks /ru /rp.
    user = args.user or os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not user:
        print(f"{APP_NAME}: could not determine username; pass --user <name>", file=sys.stderr)
        return 1
    password = args.password
    if args.prompt_password and not password:
        import getpass
        password = getpass.getpass(f"Windows password for {user} (not echoed, not stored by {APP_NAME}): ")

    if password:
        create = [
            "schtasks", "/create", "/tn", task_name, "/tr", tr,
            "/sc", "ONCE", "/st", "00:00", "/rl", "HIGHEST", "/f",
            "/ru", user, "/rp", password,
        ]
        result = subprocess.run(create, capture_output=True, text=True, timeout=30)
        cred_note = f"stored credentials for {user} (elevated, no prompt)"
    else:
        # S4U registration via PowerShell. RunLevel Highest = elevated token.
        ps = (
            "$ErrorActionPreference='Stop';"
            "$a = New-ScheduledTaskAction -Execute 'powershell' "
            "-Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden "
            f"-File \"{script}\" -RequestPath \"{request_path}\"';"
            f"$p = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType S4U -RunLevel Highest;"
            "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 1) "
            "-MultipleInstances Parallel;"
            f"Register-ScheduledTask -TaskName '{task_name}' -Action $a -Principal $p "
            "-Settings $s -Force | Out-Null;"
            f"Write-Output 'registered S4U task {task_name}'"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        cred_note = f"S4U for {user} (elevated, no prompt, no stored password)"

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        if "Access is denied" in err or "denied" in err.lower():
            print(f"{APP_NAME}: install-kbd-task must be run from an ELEVATED shell.", file=sys.stderr)
        else:
            print(err or out, file=sys.stderr)
        return result.returncode
    print(out or f"{APP_NAME}: task '{task_name}' registered")
    print(f"{APP_NAME}: logon = {cred_note}")
    print(f"{APP_NAME}: test (non-elevated) with: python kbd_agent_notify.py --config <cfg> signal --source manual --event test")
    return 0


def uninstall_kbd_task_command(args: argparse.Namespace) -> int:
    if not is_windows():
        return 1
    cfg = load_config(args.config)
    task_name = str((cfg.get("kbd") or {}).get("taskName", "KbdAgentNotify"))
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True, text=True, timeout=30,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        print(err or out, file=sys.stderr)
        return result.returncode
    print(out or f"{APP_NAME}: task '{task_name}' removed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flash/notify from Claude Code and Codex hooks.")
    parser.add_argument("--config", type=Path, default=None, help="Path to JSON config.")

    sub = parser.add_subparsers(dest="command", required=True)

    p_signal = sub.add_parser("signal", help="Emit a notification pattern.")
    p_signal.add_argument("--source", choices=["claude", "codex", "manual"], default="manual")
    p_signal.add_argument("--event", choices=[EVENT_DONE, EVENT_WAITING, EVENT_TEST], required=True)
    p_signal.add_argument("--verbose", action="store_true")
    p_signal.set_defaults(func=signal)

    p_install = sub.add_parser("install-hooks", help="Install user-level Claude/Codex hooks.")
    p_install.add_argument("--target", choices=["all", "claude", "codex"], default="all")
    p_install.set_defaults(func=install_hooks)

    p_init = sub.add_parser("init-config", help="Write the default config file.")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=lambda a: print(write_default_config(a.config, a.force)) or 0)

    p_hid = sub.add_parser("hid-list", help="List present HID interfaces and report lengths.")
    p_hid.add_argument("--match", default=None, help="Substring filter.")
    p_hid.add_argument("--json", action="store_true")
    p_hid.set_defaults(func=hid_list)

    p_open = sub.add_parser("hid-open-probe", help="Open matching HID interfaces without sending reports.")
    p_open.add_argument("--match", required=True, help="Substring filter for path/instance.")
    p_open.set_defaults(func=hid_open_probe_command)

    p_send = sub.add_parser("hid-send", help="Send one raw HID output or feature report.")
    p_send.add_argument("--match", required=True, help="Substring filter for path/instance.")
    p_send.add_argument("--data", required=True, help="Hex bytes, including report ID.")
    p_send.add_argument("--mode", choices=["output", "set-output", "feature"], default="output")
    p_send.add_argument("--vendor-id", type=lambda v: int(v, 0), default=None)
    p_send.add_argument("--product-id", type=lambda v: int(v, 0), default=None)
    p_send.add_argument("--usage-page", type=lambda v: int(v, 0), default=None)
    p_send.add_argument("--usage", type=lambda v: int(v, 0), default=None)
    p_send.add_argument("--verbose", action="store_true")
    p_send.set_defaults(func=hid_send_command)

    p_caps = sub.add_parser("hid-caps", help="Print HID value caps/report IDs.")
    p_caps.add_argument("--match", default=None, help="Substring filter for path/instance.")
    p_caps.add_argument("--json", action="store_true")
    p_caps.set_defaults(func=hid_caps_command)

    p_get_feature = sub.add_parser("hid-get-feature", help="Read one HID feature report.")
    p_get_feature.add_argument("--match", required=True, help="Substring filter for path/instance.")
    p_get_feature.add_argument("--report-id", type=lambda v: int(v, 0), required=True)
    p_get_feature.add_argument("--length", type=int, required=True, help="Total report length including report ID byte.")
    p_get_feature.add_argument("--verbose", action="store_true")
    p_get_feature.set_defaults(func=hid_get_feature_command)

    p_snapshot = sub.add_parser("hid-feature-snapshot", help="Read feature reports from matching HID interfaces.")
    p_snapshot.add_argument("--match", required=True, help="Substring filter for path/instance.")
    p_snapshot.add_argument("--report-id", type=lambda v: int(v, 0), default=None)
    p_snapshot.add_argument("--length", type=int, default=None, help="Override total report length including report ID byte.")
    p_snapshot.add_argument("--output", type=Path, default=None)
    p_snapshot.add_argument("--verbose", action="store_true")
    p_snapshot.set_defaults(func=hid_feature_snapshot_command)

    p_ctp = sub.add_parser("dell-ctp-probe", help="Probe Dell Optimizer's Collaboration Touchpad native DLL.")
    p_ctp.add_argument("--dll", type=Path, default=None, help="Override path to CollaborationTouchpadPlugin.CTPAgent.dll.")
    p_ctp.add_argument("--test-lights", action="store_true", help="Run the DLL's SetButtonLight test and restore off.")
    p_ctp.add_argument("--buttons", type=int, default=6)
    p_ctp.add_argument("--delay-ms", type=int, default=150)
    p_ctp.add_argument("--hold-ms", type=int, default=600)
    p_ctp.set_defaults(func=dell_ctp_probe_command)

    p_read = sub.add_parser("hid-read", help="Capture raw HID input reports for a short window.")
    p_read.add_argument("--match", required=True, help="Substring filter for path/instance.")
    p_read.add_argument("--seconds", type=float, default=10.0)
    p_read.add_argument("--length", type=int, default=None, help="Input report length; defaults to descriptor length.")
    p_read.add_argument("--verbose", action="store_true")
    p_read.set_defaults(func=hid_read_command)

    p_record = sub.add_parser("hid-record", help="Capture multiple raw HID input streams into a file.")
    p_record.add_argument("--match", action="append", required=True, help="Substring filter; repeat for multiple devices.")
    p_record.add_argument("--seconds", type=float, default=30.0)
    p_record.add_argument("--output", type=Path, required=True)
    p_record.add_argument("--verbose", action="store_true")
    p_record.set_defaults(func=hid_record_command)

    p_kbd_test = sub.add_parser("kbd-test", help="Test the Dell keyboard-backlight pulse (run ELEVATED).")
    p_kbd_test.add_argument("--event", choices=[EVENT_DONE, EVENT_WAITING, EVENT_TEST], default=EVENT_TEST)
    p_kbd_test.add_argument("--read-only", action="store_true", help="Just read and print current backlight state.")
    p_kbd_test.add_argument("--count", type=int, default=0, help="Override number of flashes.")
    p_kbd_test.add_argument("--on-ms", type=int, default=0, help="Override flash on duration (ms).")
    p_kbd_test.add_argument("--off-ms", type=int, default=0, help="Override gap after each flash (ms).")
    p_kbd_test.set_defaults(func=kbd_test_command)

    p_kbd_install = sub.add_parser(
        "install-kbd-task",
        help="Register the elevated Scheduled Task for keyboard-backlight pulses (run ELEVATED, once).",
    )
    p_kbd_install.add_argument("--user", default=None, help="Account to run the task as (default: current user).")
    p_kbd_install.add_argument("--password", default=None, help="Password for --user. Prefer --prompt-password.")
    p_kbd_install.add_argument(
        "--prompt-password", action="store_true",
        help="Prompt for the password interactively (not echoed, not stored by kbd-agent-notify).",
    )
    p_kbd_install.set_defaults(func=install_kbd_task_command)

    p_kbd_uninstall = sub.add_parser(
        "uninstall-kbd-task",
        help="Remove the keyboard-backlight Scheduled Task.",
    )
    p_kbd_uninstall.set_defaults(func=uninstall_kbd_task_command)

    p_status = sub.add_parser("status", help="Print paths and environment status.")
    p_status.set_defaults(func=show_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
