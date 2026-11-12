#!/usr/bin/env python3
"""v1.5.6 local patch: make sure console children never pop a window.

Background entry points run under pythonw.exe (no console). Windows allocates a NEW console
window for every console-subsystem child (git / schtasks / taskkill / powershell / cmd) unless
CREATE_NO_WINDOW is passed. This test checks the helper, and checks that the known call sites
actually go through it.

    python tests/no_window_test.py

Exit 0 and print NO_WINDOW OK: N checks when everything holds.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PLATFORM = PKG / "platform"
for p in (str(PLATFORM), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import _spawn  # noqa: E402

fails: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  ← {detail}"))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- helper behaviour
check("NO_WIN is 0x08000000 on Windows, 0 elsewhere",
      _spawn.NO_WIN == (0x08000000 if os.name == "nt" else 0), f"NO_WIN={_spawn.NO_WIN!r}")

check("is_console_tool knows git / schtasks / powershell / cmd / python",
      all(_spawn.is_console_tool(x) for x in
          (["git", "-C", "."], ["schtasks"], ["powershell.exe"], ["cmd.exe"], [sys.executable])),
      "one of the console tools was not recognised")

check("is_console_tool leaves GUI programs alone",
      not any(_spawn.is_console_tool(x) for x in (["explorer.exe", "/select,", "x"], ["chrome.exe"], ["msedge.exe"])),
      "a GUI program was classified as a console tool")

kw = _spawn.kw(["git", "status"])
check("kw() adds CREATE_NO_WINDOW for a console tool",
      kw.get("creationflags") == (0x08000000 if os.name == "nt" else None) if os.name == "nt" else "creationflags" not in kw,
      f"kw={kw!r}")

kw_gui = _spawn.kw(["explorer.exe", "/select,", "x"])
check("kw() does not touch GUI programs", "creationflags" not in kw_gui, f"kw={kw_gui!r}")

kw_shell = _spawn.kw("dir", shell=True)
check("kw(shell=True) always hides the shell window",
      ("creationflags" in kw_shell) == (os.name == "nt"), f"kw={kw_shell!r}")

if os.name == "nt":
    kw_or = _spawn.kw(["git", "status"], creationflags=0x00000008)   # DETACHED_PROCESS already set
    check("kw() ORs into an existing creationflags",
          int(kw_or["creationflags"]) == (0x00000008 | 0x08000000), f"kw={kw_or!r}")


# ---------------------------------------------------------------- run() really passes it
real_run = _spawn.subprocess.run
seen: list[dict] = []


def fake_run(cmd, **kw):   # noqa: ANN001, ANN003 - test double
    seen.append({"cmd": cmd, **kw})
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()  # noqa: SLF001


try:
    _spawn.subprocess.run = fake_run   # type: ignore[assignment]
    _spawn.run(["git", "--version"], capture_output=True, text=True)
    _spawn.run("echo hi", shell=True, capture_output=True)
    _spawn.run(["explorer.exe", "/select,", "x"], capture_output=True)
finally:
    _spawn.subprocess.run = real_run   # type: ignore[assignment]

if os.name == "nt":
    check("run(git) forwards CREATE_NO_WINDOW", int(seen[0].get("creationflags") or 0) & 0x08000000 == 0x08000000, f"{seen[0]!r}")
    check("run(shell=True) forwards CREATE_NO_WINDOW", int(seen[1].get("creationflags") or 0) & 0x08000000 == 0x08000000, f"{seen[1]!r}")
    check("run(explorer) stays visible", "creationflags" not in seen[2], f"{seen[2]!r}")
    check("run() keeps caller kwargs (capture_output/text)",
          seen[0].get("capture_output") is True and seen[0].get("text") is True, f"{seen[0]!r}")
else:
    check("run() is a pass-through off Windows", "creationflags" not in seen[0], f"{seen[0]!r}")


# ---------------------------------------------------------------- call sites are wired
def src(rel: str) -> str:
    return (PKG / rel).read_text(encoding="utf-8")


codescan = src("platform/observers/codescan.py")
check("codescan imports the hidden runner", "_run_hidden" in codescan, "no _run_hidden import")
check("codescan no longer spawns git through plain subprocess.run",
      "subprocess.run([git" not in codescan and "subprocess.run([git," not in codescan,
      "git is still spawned with subprocess.run")

feedback = src("platform/collect_feedback.py")
check("collect_feedback uses the hidden runner for git",
      "_run_hidden([\"git\"" in feedback, "git call not wired")
check("collect_feedback uses the hidden runner for shell commands",
      "shell=True, cwd=ROOT" in feedback and "_run_hidden(cmd, shell=True" in feedback, "shell call not wired")
check("collect_feedback uses the hidden runner for schtasks",
      '_run_hidden(["schtasks"' in feedback, "schtasks call not wired")

reset = src("platform/demo_reset.py")
check("demo_reset uses the hidden runner for taskkill",
      '_run_hidden(["taskkill"' in reset, "taskkill call not wired")

# the helper itself must stay dependency-light (stdlib only)
mod_src = inspect.getsource(_spawn)
check("_spawn.py imports only os + subprocess",
      "import os" in mod_src and "import subprocess" in mod_src and "import requests" not in mod_src,
      "unexpected import in _spawn.py")

if fails:
    print(f"\n{len(fails)} check(s) failed")
    sys.exit(1)
print(f"\nNO_WINDOW OK: {checks} checks")
