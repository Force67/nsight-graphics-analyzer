"""Process management: admin detection, process-tree kills (taskkill on
Windows, /proc + SIGKILL on Linux), residual-ngfx detection so wrappers can
warn before launching duplicates."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional


def _proc_pids() -> list[int]:
    return [int(d.name) for d in Path("/proc").iterdir() if d.name.isdigit()]


def _proc_name(pid: int) -> Optional[str]:
    """Executable basename; /proc/<pid>/comm truncates at 15 chars, so read
    argv[0] instead."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    argv0 = raw.split(b"\0", 1)[0].decode(errors="replace")
    return os.path.basename(argv0) if argv0 else None


def _proc_start_epoch(pid: int) -> Optional[float]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        btime = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
    except (OSError, StopIteration, ValueError):
        return None
    # Field 22 (starttime); split after the ')' because comm may contain spaces.
    fields = stat.rsplit(")", 1)[1].split()
    return btime + int(fields[19]) / os.sysconf("SC_CLK_TCK")


def _proc_descendants(pid: int) -> list[int]:
    out: list[int] = []
    stack = [pid]
    while stack:
        cur = stack.pop()
        try:
            tasks = list(Path(f"/proc/{cur}/task").iterdir())
        except OSError:
            continue
        for task in tasks:
            try:
                children = (task / "children").read_text().split()
            except OSError:
                continue
            for child in map(int, children):
                out.append(child)
                stack.append(child)
    return out


def is_admin() -> Optional[bool]:
    """Return True/False: Windows elevation, or euid 0 elsewhere."""
    if sys.platform != "win32":
        return os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def kill_process_tree(pid: int, *, force: bool = True) -> tuple[int, str, str]:
    """Kill the process and all descendants.

    Returns (returncode, stdout, stderr) from taskkill on Windows. Elsewhere
    sends SIGKILL (SIGTERM if not `force`) to the tree, children first;
    returncode is 1 if any signal failed.
    """
    if sys.platform != "win32":
        sig = signal.SIGKILL if force else signal.SIGTERM
        errors: list[str] = []
        for target in reversed([pid, *_proc_descendants(pid)]):
            try:
                os.kill(target, sig)
            except ProcessLookupError:
                pass
            except OSError as exc:
                errors.append(f"{target}: {exc}")
        return (1 if errors else 0), "", "\n".join(errors)
    args = ["taskkill", "/T"]
    if force:
        args.append("/F")
    args.extend(["/PID", str(pid)])
    proc = subprocess.run(
        args, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def list_residual_ngfx() -> list[dict]:
    """List currently-running ngfx* processes."""
    if sys.platform != "win32":
        out = []
        for pid in _proc_pids():
            name = _proc_name(pid)
            if name and name.lower().startswith("ngfx"):
                out.append({"name": name, "pid": pid})
        return out
    try:
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    out: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        cells = [c.strip().strip('"') for c in line.split(",")]
        if len(cells) < 2:
            continue
        name = cells[0].lower()
        if not name.startswith("ngfx"):
            continue
        try:
            pid = int(cells[1])
        except ValueError:
            continue
        out.append({"name": cells[0], "pid": pid})
    return out


def kill_all_ngfx() -> list[dict]:
    """Kill every ngfx* process tree we can find."""
    out: list[dict] = []
    for proc in list_residual_ngfx():
        rc, stdout, stderr = kill_process_tree(proc["pid"])
        out.append({
            "pid": proc["pid"],
            "name": proc["name"],
            "returncode": rc,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
        })
    return out


def kill_target_started_after(
    exe_basename: str,
    since_epoch: float,
    *,
    grace_seconds: float = 1.0,
) -> list[dict]:
    """Kill processes whose Image Name == `exe_basename` and CreationDate >=
    (since_epoch - grace_seconds). Used by capture commands to clean up the
    game ngfx launched on our behalf.

    Returns a list of `{pid, name, returncode, stdout, stderr}` for each
    killed process. If no matches, returns `[]`. We
    deliberately use exe basename + start time (not PID) so concurrent
    independent runs of the same game are NOT touched.

    Implementation: filter and compare in PowerShell using `[DateTime]`
    objects (CIM `CreationDate` is `Kind=Local`; we convert our cutoff
    via `[DateTimeOffset]::FromUnixTimeSeconds(...).LocalDateTime` so .NET
    handles the timezone correctly). Returning epochs from PowerShell and
    comparing in Python was unreliable (Get-Date timezone quirks gave an
    8-hour offset on UTC+8 hosts).
    """
    if sys.platform != "win32":
        cutoff = since_epoch - grace_seconds
        out = []
        for pid in _proc_pids():
            if _proc_name(pid) != exe_basename:
                continue
            started = _proc_start_epoch(pid)
            if started is None or started < cutoff:
                continue
            rc, stdout, stderr = kill_process_tree(pid)
            out.append({"pid": pid, "name": exe_basename, "returncode": rc,
                        "stdout": stdout, "stderr": stderr.strip()})
        return out
    cutoff_epoch = int(since_epoch - grace_seconds)
    name_pattern = exe_basename.replace("'", "''")
    ps = (
        f"$cutoff = [DateTimeOffset]::FromUnixTimeSeconds({cutoff_epoch}).LocalDateTime; "
        f"Get-CimInstance Win32_Process -Filter \"Name='{name_pattern}'\" "
        "| Where-Object { $_.CreationDate -ge $cutoff } "
        "| ForEach-Object { Write-Output $_.ProcessId.ToString() }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    out: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        rc, stdout, stderr = kill_process_tree(pid)
        out.append({
            "pid": pid,
            "name": exe_basename,
            "returncode": rc,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
        })
    return out
