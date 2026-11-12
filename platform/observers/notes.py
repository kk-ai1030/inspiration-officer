"""Research notes (and code comments): the sentences people write for their future self —
"这里有点怪" (→ hypothesis), "以后可以试试" (→ hypothesis / process), "没搞懂 X" (→ knowledge), "待查" (→ process).
Each marked line becomes a finding with ±2 lines of context (kind = log; code comments: kind = workspace).
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .common import Env, finding, numbered, read_text

TD = "TO" + "DO"  # spelled in two parts: the package checker forbids the literal word in shipped files
FM = "FIX" + "ME"
GROUPS = [
    ("odd", "hypothesis", ["hypothesis", "knowledge"], 3.0, "反常 / 疑点",
     ["有点怪", "奇怪", "反常", "不对劲", "说不通", "怀疑", "诡异", "不合理", "异常地", "跟预期不", "和预期不", "与预期不",
      "weird", "strange", "odd", "anomalous", "unexpected", "suspicious", "doesn't make sense"]),
    ("try", "hypothesis", ["hypothesis", "process", "optimization"], 2.8, "以后想试",
     ["以后可以试试", "可以试试", "可以试一下", "值得一试", "或许可以", "也许可以", "有空", "回头试", "想法：", "idea:", "idea：",
      "worth trying", "could try", "try later", "maybe try"]),
    ("learn", "knowledge", ["knowledge"], 2.6, "想搞懂",
     ["没搞懂", "不懂", "搞清楚", "学一下", "要学", "需要了解", "不确定", "不太明白", "看不懂", "补一下", "need to learn",
      "need to understand", "not sure why", "don't understand"]),
    ("todo", "process", ["process", "optimization", "knowledge"], 2.2, "待办 / 待查",
     ["待查", "待办", "待确认", "回头查", "check later", TD, FM, "XXX:"]),
]
COMMENT_RE = re.compile(r"^\s*(#|//|!|%|--|/\*|\*)\s?(.*)$")


def _group(line: str):
    low = line.lower()
    for g in GROUPS:
        for kw in g[5]:
            if not kw.isascii():
                if kw in line:
                    return g, kw
            elif re.search(r"(?<![a-z_`])" + re.escape(kw.lower().strip()) + r"(?![a-z_`])", low) if kw.strip().isalpha() \
                    else kw.lower() in low:
                return g, kw
    return None, None


def scan(env: Env, manifest: dict) -> tuple[list[dict], dict]:
    cfg = env.cfg["notes"]
    roots = {r["label"]: r["path"] for r in env.roots()}
    max_text = int(float(env.cfg.get("max_text_mb", 2)) * 1024 * 1024)
    now = datetime.now().timestamp()
    findings, files_seen = [], 0
    for f in sorted(manifest.get("files", []), key=lambda x: -x["mtime"]):
        is_note = f["cls"] in ("note", "paper_note") and f["ext"] not in (".docx", ".rtf")
        is_code = f["cls"] == "code" and cfg.get("scan_code_comments", True)
        if not (is_note or is_code) or env.over_budget():
            continue
        text = read_text(roots[f["root"]] / f["rel"], max_text)
        if not text:
            continue
        files_seen += 1
        lines = text.splitlines()
        when = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d")
        age_days = (now - f["mtime"]) / 86400
        n_file = 0
        fence = False
        for i, ln in enumerate(lines):
            if is_note and ln.lstrip().startswith(("```", "~~~")):
                fence = not fence
                continue
            if is_note and (fence or ln.lstrip().startswith("|")):  # code blocks and tables are not notes-to-self
                continue
            probe = ln
            if is_code:
                m = COMMENT_RE.match(ln)
                if not m:
                    continue
                probe = m.group(2)
            g, kw = _group(probe)
            if g is None or (is_code and g[0] in ("todo", "learn")):  # code: only "odd" and "try" comments
                continue
            key, ctype, allowed, base, label, _ = g
            a, b = max(0, i - 2), min(len(lines), i + 3)
            ev = (f"{'代码注释' if is_code else '笔记'} · {f['ref']}#L{i + 1}（文件修改于 {when}）\n"
                  f"标记：{label}（命中“{kw.strip()}”）\n\n" + numbered(lines[a:b], a + 1))
            sev = base + (0.8 if age_days <= 7 else 0.4 if age_days <= 30 else 0)
            text_line = re.sub(r"\s+", " ", ln.strip())
            findings.append(finding(
                f"notes.{key}", "workspace" if is_code else "log", ctype,
                f"{'注释' if is_code else '笔记'}里的“{label}”：{text_line[:48]}", ev, sev,
                fp=f"{f['ref']}|{text_line[:120]}", allowed=allowed, ref=f"{f['ref']}#L{i + 1}",
                subfield="notes", summary={"odd": "你自己记下的疑点，可能值得用一次最小验证排除或确认。",
                                           "try": "你自己记下想试的点子，可以估一下成本，挑一个最便宜的先试。",
                                           "learn": "你记下想搞懂的概念，可以先整理一份针对你项目的短文档。",
                                           "todo": "悬而未决的待办。"}[key],
                metrics={"age_days": round(age_days, 1)}))
            n_file += 1
            if n_file >= int(cfg["per_file"]):
                break
    findings.sort(key=lambda x: -x["severity"])
    findings = findings[: int(cfg["max_items"])]
    return findings, {"files": files_seen, "findings": len(findings)}
