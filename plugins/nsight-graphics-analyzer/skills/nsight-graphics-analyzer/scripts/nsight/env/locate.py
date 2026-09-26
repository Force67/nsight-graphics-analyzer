"""Locate the NVIDIA Nsight Graphics installation directory and binaries.

Discovery priority:
    1. NSIGHT_GRAPHICS_DIR env var (must point at the host/<platform> dir)
    2. Windows: glob across all fixed drives (Program Files variants)
       Linux: glob under /opt/nvidia and ~/nvidia, plus `ngfx` on PATH
    3. Windows uninstall registry (catches non-standard install locations)

Within each source the candidates are version-sorted (newest first) and the
first that contains `ngfx-capture` (the install marker) wins.
"""
from __future__ import annotations

import ctypes
import glob
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

ENV_VAR = "NSIGHT_GRAPHICS_DIR"

IS_WINDOWS = sys.platform == "win32"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""
# Value ngfx expects for --platform.
NGFX_PLATFORM = "Windows" if IS_WINDOWS else "Linux (x86_64)"

KNOWN_BINARIES = tuple(
    name + EXE_SUFFIX
    for name in ("ngfx", "ngfx-capture", "ngfx-replay", "ngfx-ui", "ngfx-rpc")
)
INSTALL_MARKER = "ngfx-capture" + EXE_SUFFIX
HOST_SUBDIR = Path("host") / (
    "windows-desktop-nomad-x64" if IS_WINDOWS else "linux-desktop-nomad-x64"
)


def parse_version(host_dir: Path) -> tuple[int, ...]:
    """Extract numeric version tuple from `.../Nsight Graphics 2026.1.0/host/...`."""
    try:
        version_dir = host_dir.parents[1].name
    except IndexError:
        return (0,)
    match = re.search(r"(\d+(?:\.\d+)+)", version_dir)
    if not match:
        return (0,)
    return tuple(int(x) for x in match.group(1).split("."))


def parse_version_from_path(host: Path) -> Optional[str]:
    """Best-effort version-string extraction (e.g. '2026.1.0')."""
    try:
        match = re.search(r"(\d+(?:\.\d+)+)", host.parents[1].name)
        return match.group(1) if match else None
    except IndexError:
        return None


def _fixed_windows_drives() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        get_type = ctypes.windll.kernel32.GetDriveTypeW
    except Exception:
        return ["C:"]
    drive_fixed = 3
    drives: list[str] = []
    for i in range(26):
        if not (mask & (1 << i)):
            continue
        letter = chr(ord("A") + i)
        try:
            if get_type(f"{letter}:\\") == drive_fixed:
                drives.append(f"{letter}:")
        except Exception:
            continue
    return drives or ["C:"]


def _linux_patterns() -> list[str]:
    host = HOST_SUBDIR.as_posix()
    return [
        f"/opt/nvidia/nsight-graphics-for-linux/*/{host}",
        f"/opt/nvidia/nsight-graphics*/{host}",
        f"{Path.home()}/nvidia/nsight-graphics-for-linux/*/{host}",
    ]


def _path_candidates() -> list[Path]:
    """`ngfx` on PATH, if it resolves into a host dir (symlink installs)."""
    found = shutil.which("ngfx")
    if not found:
        return []
    host = Path(found).resolve().parent
    return [host] if (host / INSTALL_MARKER).exists() else []


def _glob_candidates() -> tuple[list[Path], list[str]]:
    patterns: list[str] = [] if IS_WINDOWS else _linux_patterns()
    for drive in _fixed_windows_drives():
        for pf in ("Program Files", "Program Files (x86)"):
            patterns.append(
                f"{drive}/{pf}/NVIDIA Corporation/Nsight Graphics */{HOST_SUBDIR.as_posix()}"
            )
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(Path(p) for p in glob.glob(pattern))
    return matches, patterns


def _registry_candidates() -> list[Path]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    out: list[Path] = []
    for hive, root in roots:
        try:
            with winreg.OpenKey(hive, root) as root_key:
                idx = 0
                while True:
                    try:
                        name = winreg.EnumKey(root_key, idx)
                    except OSError:
                        break
                    idx += 1
                    try:
                        with winreg.OpenKey(root_key, name) as sub:
                            try:
                                display, _ = winreg.QueryValueEx(sub, "DisplayName")
                            except OSError:
                                continue
                            if not display or "Nsight Graphics" not in display:
                                continue
                            try:
                                loc, _ = winreg.QueryValueEx(sub, "InstallLocation")
                            except OSError:
                                continue
                            if not loc:
                                continue
                            host = Path(loc) / HOST_SUBDIR
                            if host.is_dir():
                                out.append(host)
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def all_candidates() -> tuple[list[Path], list[str]]:
    """All filesystem + registry candidates, version-sorted (newest first), deduped."""
    fs_matches, patterns = _glob_candidates()
    candidates = list(fs_matches) + _registry_candidates() + _path_candidates()
    seen: set[str] = set()
    ordered: list[Path] = []
    for cand in sorted(candidates, key=parse_version, reverse=True):
        key = str(cand).lower() if IS_WINDOWS else str(cand)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cand)
    return ordered, patterns


def find_install(strict: bool = True) -> Optional[Path]:
    """Return the newest installed Nsight Graphics host dir.

    With strict=True (default), exits the process with code 3 and a clear
    message on failure. With strict=False, returns None — useful for `doctor`
    to report a missing install without aborting.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        path = Path(override)
        if (path / INSTALL_MARKER).exists():
            return path
        if strict:
            sys.stderr.write(f"{ENV_VAR}={override} does not contain {INSTALL_MARKER}\n")
            sys.exit(3)
        return None

    candidates, patterns = all_candidates()
    for cand in candidates:
        if (cand / INSTALL_MARKER).exists():
            return cand

    if strict:
        sys.stderr.write("Could not locate a Nsight Graphics install.\nTried glob patterns:\n")
        for pat in patterns:
            sys.stderr.write(f"  {pat}\n")
        sys.stderr.write(
            f"Plus {'Windows uninstall registry' if IS_WINDOWS else 'ngfx on PATH'}. "
            f"None contained {INSTALL_MARKER}.\nOverride with env "
            f"{ENV_VAR}=<install>/{HOST_SUBDIR.as_posix()}\n"
        )
        sys.exit(3)
    return None


def binary(host: Path, name: str, strict: bool = True) -> Optional[str]:
    """Resolve a Nsight binary path by base name (e.g. 'ngfx'); exit 3 if
    missing in strict mode."""
    name += EXE_SUFFIX
    path = host / name
    if path.exists():
        return str(path)
    if strict:
        sys.stderr.write(
            f"[nsight] required file {name} not found in {host}. "
            "Reinstall Nsight Graphics or point "
            f"{ENV_VAR} at a complete install.\n"
        )
        sys.exit(3)
    return None


def binary_mtimes(host: Path) -> dict[str, int]:
    """Per-binary mtime_ns for cache invalidation."""
    out: dict[str, int] = {}
    for name in KNOWN_BINARIES:
        path = host / name
        try:
            out[name] = path.stat().st_mtime_ns if path.exists() else 0
        except OSError:
            out[name] = 0
    return out


def install_inventory(host: Path) -> dict[str, dict]:
    """Per-binary presence/path/size/mtime for `doctor` output."""
    out: dict[str, dict] = {}
    for name in KNOWN_BINARIES:
        path = host / name
        info: dict = {"present": path.exists(), "path": str(path)}
        if path.exists():
            try:
                stat = path.stat()
                info["size"] = stat.st_size
                info["mtime_ns"] = stat.st_mtime_ns
            except OSError:
                pass
        out[name] = info
    return out
