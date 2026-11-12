#!/usr/bin/env python3
"""Copy the scripts the agent needs inside the SciencePal sandbox into insp-nightly/ (v1.4 "self-bootstrap").

Why: the platform only mounts the skills that were actually loaded in a run (feedback3 §6). insp-triage /
insp-init are usually NOT loaded, so insp-nightly must carry its own copies of triage.py, fetch_fulltext.py,
validate_cards.py, verify_arxiv.py and the state template. The originals stay the single source of truth;
selfcheck.py package fails if a vendored copy drifts.

    python scripts/sync_vendor.py          # copy
    python scripts/sync_vendor.py --check  # exit 1 if any copy differs (used by selfcheck.py package)
"""
from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
VENDOR = PKG / "insp-nightly" / "scripts" / "vendor"
FILES = {
    "insp-triage/scripts/triage.py": "triage.py",
    "insp-card/scripts/fetch_fulltext.py": "fetch_fulltext.py",
    "insp-card/scripts/validate_cards.py": "validate_cards.py",
    "insp-card/scripts/verify_arxiv.py": "verify_arxiv.py",
    "insp-card/scripts/find_quotes.py": "find_quotes.py",  # v1.4.4: nightly must not reference files of another skill
}
TEMPLATE_SRC = PKG / "insp-init" / "assets" / "灵感状态模板" / "inspiration"
TEMPLATE_DST = PKG / "insp-nightly" / "assets" / "state_template"


def pairs():
    for src, dst in FILES.items():
        yield PKG / src, VENDOR / dst
    for p in sorted(TEMPLATE_SRC.rglob("*")):
        if p.is_file():
            yield p, TEMPLATE_DST / p.relative_to(TEMPLATE_SRC)


def main() -> int:
    check = "--check" in sys.argv
    bad = []
    for src, dst in pairs():
        if check:
            if not dst.is_file() or not filecmp.cmp(src, dst, shallow=False):
                bad.append(str(dst.relative_to(PKG)))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    if check:
        if bad:
            print("VENDOR DRIFT（请运行 python scripts/sync_vendor.py）：\n  " + "\n  ".join(bad))
            return 1
        print("OK: insp-nightly 内置脚本与源文件一致")
        return 0
    print(f"OK: 已同步到 {VENDOR.relative_to(PKG)} 与 {TEMPLATE_DST.relative_to(PKG)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
