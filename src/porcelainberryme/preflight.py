"""Read-only, low-cost host capacity and existing voice installation inspection.

No model imports, provider calls, deep directory scans, installation, or
downloads. All paths must be explicit; never list environment secrets.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


def disk_info(directory: str) -> dict:
    path = Path(directory)
    if not path.is_absolute() or not path.exists():
        return {"available": False}
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"available": False}
    return {
        "available": True,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def memory_info() -> dict:
    try:
        with open("/proc/meminfo", encoding="ascii") as source:
            lines = source.readlines()
    except OSError:
        return {"available": False}
    values = {}
    for line in lines:
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            try:
                values[key] = int(raw.strip().split()[0]) * 1024
            except (ValueError, IndexError):
                continue
    return {"available": bool(values), **{key + "_bytes": val for key, val in values.items()}}


def voice_info(executable: str | None, model: str | None) -> dict:
    if not executable and not model:
        return {"configured": False}
    if not executable or not model:
        return {"configured": False, "reason": "both_paths_required"}
    exe = Path(executable)
    voice = Path(model)
    config = Path(str(voice) + ".json")
    if not exe.is_absolute() or not voice.is_absolute():
        return {"configured": False, "reason": "absolute_paths_required"}
    return {
        "configured": True,
        "executable_exists": exe.is_file(),
        "executable_usable": exe.is_file() and os.access(exe, os.X_OK),
        "model_exists": voice.is_file(),
        "model_config_exists": config.is_file(),
        "model_bytes": voice.stat().st_size if voice.is_file() else None,
    }


def unit_info(unit: str) -> dict:
    """Query one explicit systemd unit; read only operational states."""
    if not unit or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@.-" for c in unit):
        raise ValueError("invalid_systemd_unit")
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState"],
            text=True, capture_output=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    if result.returncode != 0:
        return {"available": False}
    valid = {}
    for line in result.stdout.splitlines():
        k, _, v = line.partition("=")
        if k in {"LoadState", "ActiveState"} and v:
            valid[k] = v
    return {"available": bool(valid), **valid}


def collect(*, disk_paths: tuple[str, ...] = ("/",),
            executable: str | None = None, model: str | None = None,
            unit: str | None = None) -> dict:
    result = {
        "mode": "read_only",
        "disk": {path: disk_info(path) for path in disk_paths},
        "memory": memory_info(),
        "voice": voice_info(executable, model),
    }
    if unit:
        result["service"] = unit_info(unit)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="PorcelainberryMe read-only host preflight")
    parser.add_argument("--disk", action="append", dest="disks", help="Absolute mount/path; repeated")
    parser.add_argument("--piper-executable", help="Existing Piper CLI executable path")
    parser.add_argument("--piper-model", help="Existing Piper ONNX voice path")
    parser.add_argument("--service", help="Specific voice systemd unit to inspect")
    args = parser.parse_args()
    report = collect(
        disk_paths=tuple(args.disks or ["/"]),
        executable=args.piper_executable, model=args.piper_model, unit=args.service,
    )
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
