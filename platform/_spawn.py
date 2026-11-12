"""Never let a console child pop a window (v1.5.6 local patch).

Background entry points (`review_server.py`, `receiver.py`, scheduled runs) are started with
`pythonw.exe`, which owns **no console**. Windows then allocates a brand-new console window for
every console-subsystem child we spawn (`git.exe`, `schtasks.exe`, `taskkill.exe`,
`powershell.exe`, `cmd.exe`, …). That is what flashed cmd / PowerShell windows during the
30-minute snapshot publish, and on every `observe.py scan` while the review server was running.

`CREATE_NO_WINDOW` (0x08000000) suppresses only the window: stdout / stderr pipes keep working,
so `capture_output=True`, `text=True` and friends behave exactly as before.

GUI programs you *want* the user to see (`explorer.exe`, `chrome.exe`, `webbrowser.open`) must
**not** be hidden, so this module only injects the flag for console tools (or when
`shell=True`), and never overrides an explicit `creationflags` — it ORs into it.

Usage::

    from _spawn import run as run_hidden, popen as popen_hidden
    r = run_hidden(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                   capture_output=True, text=True, timeout=15)
"""
from __future__ import annotations

import os
import subprocess

CREATE_NO_WINDOW = 0x08000000
NO_WIN = CREATE_NO_WINDOW if os.name == "nt" else 0

# Console-subsystem tools: spawning them from pythonw pops a window unless we pass the flag.
CONSOLE_TOOLS = {
    "git", "git.exe",
    "schtasks", "schtasks.exe",
    "taskkill", "taskkill.exe",
    "powershell", "powershell.exe",
    "pwsh", "pwsh.exe",
    "cmd", "cmd.exe",
    "bash", "bash.exe",
    "sh", "sh.exe",
    "python", "python.exe", "python3", "python3.exe",
    "pythonw", "pythonw.exe",
    "py", "py.exe",
}


def is_console_tool(cmd) -> bool:
    """True when `cmd` (list or str, no shell) names a console-subsystem executable."""
    if isinstance(cmd, (list, tuple)):
        first = str(cmd[0]) if cmd else ""
    else:
        first = str(cmd or "")
    name = os.path.basename(first).lower()
    if name.endswith(".exe") or name.endswith(".cmd") or name.endswith(".bat"):
        return name in CONSOLE_TOOLS
    return name in CONSOLE_TOOLS


def kw(cmd=None, **extra) -> dict:
    """Add CREATE_NO_WINDOW on Windows, then return kwargs for subprocess.run/Popen.

    - `shell=True` always gets the flag (the shell itself is a console program);
    - a list/str first element in CONSOLE_TOOLS gets the flag;
    - anything else (explorer, chrome, xdg-open …) is left alone;
    - an existing `creationflags` is preserved and ORed.
    """
    if os.name == "nt" and (extra.get("shell") or cmd is None or is_console_tool(cmd)):
        extra["creationflags"] = int(extra.get("creationflags") or 0) | CREATE_NO_WINDOW
    return extra


def run(cmd, **extra):
    """subprocess.run with the console window suppressed when appropriate."""
    return subprocess.run(cmd, **kw(cmd, **extra))


def popen(cmd, **extra):
    """subprocess.Popen with the console window suppressed when appropriate."""
    return subprocess.Popen(cmd, **kw(cmd, **extra))
