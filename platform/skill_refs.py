#!/usr/bin/env python3
"""Local copy of the platform's skill-import "file reference" check (v1.4.4, feedback6 H1).

The SciencePal skill importer (since 2026-10-29) rejects a zip with HTTP 400 `missing_file` when any
instruction file mentions a relative path such as `scripts/x.py` that is not inside THAT skill package.
It matches the substring even inside an absolute path, so `/workspace/skills/insp-card/scripts/find_quotes.py`
written in insp-nightly/SKILL.md was read as `scripts/find_quotes.py` and the whole zip was refused.

Rule enforced here (stricter than needed, so we fail locally before uploading):
every `scripts/…`, `references/…`, `assets/…` path with a file extension that appears in any .md file of a
skill must exist inside that skill directory. Paths containing placeholders (<ID>, *, {x}) are ignored.

    python platform/skill_refs.py            # check all 7 skills, exit 1 on problems
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
REF_RE = re.compile(r"(?<![\w.-])((?:scripts|references|assets)/[^\s`'\"()（）<>，。；;:：、\]\[|]*?\.[A-Za-z0-9]{1,5})(?![\w/])")


def refs_in(text: str) -> list[str]:
    out = []
    for m in REF_RE.finditer(text):
        ref = m.group(1)
        if any(c in ref for c in "*{}$"):
            continue
        out.append(ref)
    return out


def problems(skill_dir: Path) -> list[str]:
    bad = []
    for md in sorted(skill_dir.rglob("*.md")):
        for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
            for ref in refs_in(line):
                if not (skill_dir / ref).is_file():
                    bad.append(f"{skill_dir.name}/{md.relative_to(skill_dir).as_posix()}:{n} 引用了 {ref}，但本技能包里没有这个文件"
                               f"（平台导入会报 missing_file）")
    return bad


def main() -> int:
    skills = [p for p in sorted(PKG.glob("insp-*")) if (p / "SKILL.md").is_file()]
    bad = [b for s in skills for b in problems(s)]
    for b in bad:
        print("FAIL", b)
    if not bad:
        print(f"OK: {len(skills)} 个技能的文件引用都在各自包内")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
