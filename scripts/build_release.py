#!/usr/bin/env python3
"""按白名单打发布包（weak-beta 起使用）。只用标准库。

    python scripts/build_release.py                 # → release/inspiration-skills-<VERSION>.zip
    python scripts/build_release.py --out D:/tmp    # 指定输出目录

做的事（任何一步失败都不出包）：
  1. 前置检查：vendor 副本与原件一致（sync_vendor --check）、技能文件引用都在包内（skill_refs）
  2. 只复制白名单里的路径；本地专用文件（config.json、platform_ids.json、inspiration/、runs/、dist/、
     release_evidence/、__pycache__、*.bak-*、令牌文件）一律不进包
  3. 生成 MANIFEST.txt（每个文件的大小 + sha256），组员可以用它核对拿到的包没被改过
  4. 打 zip：文件名用 UTF-8 标志位（Windows 资源管理器解压中文文件名不乱码）
  5. 对 zip 跑一次 G5 扫描（selfcheck.py g5），不通过就删掉 zip
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
# 白名单：只有这些会进包（目录递归）
INCLUDE = ["VERSION", "demo_reset.bat", "demo_restore.bat", "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "useguide.md", "评价读写约定.md", ".gitignore",
           "docs", "insp-card", "insp-feedback", "insp-init", "insp-nightly", "insp-profile", "insp-review", "insp-triage",
           "platform", "scripts", "tests", "mcp"]
# 白名单目录里也不许出现的东西
EXCLUDE = [r"(^|/)__pycache__(/|$)", r"\.pyc$", r"(^|/)\.DS_Store$", r"(^|/)Thumbs\.db$", r"\.bak-\d",
           r"(^|/)config(_exp)?\.json$", r"(^|/)platform_ids\.json$", r"(^|/)(session_token|run_token|review_token)$",
           r"(^|/)probe_report\.json$", r"(^|/)\.env$", r"^library/", r"(^|/)observed\.jsonl$", r"(^|/)assets/local_state(/|$)"]


def run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run([PY, "-X", "utf8", *cmd], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONUTF8="1"))
    return r.returncode, r.stdout + r.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "release")
    a = ap.parse_args()
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    for cmd, what in ((["scripts/sync_vendor.py", "--check"], "vendor 副本一致"), (["platform/skill_refs.py"], "技能文件引用")):
        code, out = run(cmd)
        print(("OK  " if code == 0 else "FAIL ") + what)
        if code:
            print(out[-800:])
            return 1
    missing = [p for p in INCLUDE if not (ROOT / p).exists()]
    if missing:
        print("FAIL 白名单里的文件不存在：", missing)
        return 1
    files: list[Path] = []
    for item in INCLUDE:
        p = ROOT / item
        for f in ([p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file())):
            rel = f.relative_to(ROOT).as_posix()
            if not any(re.search(pat, rel) for pat in EXCLUDE):
                files.append(f)
    with tempfile.TemporaryDirectory() as t:
        stage = Path(t) / "inspiration-skills"
        for f in files:
            dst = stage / f.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
        lines = [f"# inspiration-skills {ver} · {len(files)} 个文件 · 格式：sha256  大小  路径", ""]
        for f in files:
            rel = f.relative_to(ROOT).as_posix()
            lines.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.stat().st_size:>8}  {rel}")
        (stage / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        shutil.copy2(stage / "MANIFEST.txt", ROOT / "MANIFEST.txt")   # keep the dev copy in sync (selfcheck stale reads it)
        a.out.mkdir(parents=True, exist_ok=True)
        zp = a.out / f"inspiration-skills-{ver}.zip"
        if zp.exists():
            zp.unlink()
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(stage.rglob("*")):
                if f.is_file():
                    info = zipfile.ZipInfo.from_file(f, f.relative_to(stage.parent).as_posix())
                    info.flag_bits |= 0x800  # UTF-8 file names
                    info.compress_type = zipfile.ZIP_DEFLATED
                    z.writestr(info, f.read_bytes())
    print(f"打包：{zp}（{zp.stat().st_size // 1024} KB，{len(files) + 1} 个文件含 MANIFEST.txt）")
    code, out = run(["scripts/selfcheck.py", "g5", str(zp)])
    print(out.strip())
    if code:
        zp.unlink()
        print("FAIL G5 扫描没通过，已删除 zip。按上面的提示处理后重打。")
        return 1
    print("OK  发布包已生成，并通过 G5 扫描。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
