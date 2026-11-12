"""Merge the findings of one scan into inspiration/observed.jsonl (the input of the observed channel).

Rules
- id = obs_<sha1(detector|fingerprint)[:12]>: the same finding keeps the same id across scans, so the dedup lists
  (state.pushed, negative.jsonl) keep working after it was pushed or rejected once.
- A finding of a detector that ran this time but was not found again is "resolved": it leaves observed.jsonl and is
  appended to library/observed_history.jsonl. Findings of detectors that did not run are kept untouched.
- Evidence is redacted (keys, tokens, e-mails, home folders) and cut to evidence_max_chars at a line boundary.
- At most max_items findings (highest severity first).
- profile/observed.md gets an automatic section (between the two markers); text outside it is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path

from .common import Env, now_iso, redact

AUTO_START, AUTO_END = "<!-- observe:auto:start -->", "<!-- observe:auto:end -->"


def _cut(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n]
    k = cut.rfind("\n")
    return (cut[:k] if k > n * 0.6 else cut) + "\n…（证据过长已截断）"


def read_observed(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(ln)
            if isinstance(o, dict):
                out.append(o)
        except json.JSONDecodeError:
            continue
    return out


def merge(env: Env, findings: list[dict], detectors_run: set[str]) -> dict:
    path = env.state_dir / "observed.jsonl"
    prev = {o.get("id"): o for o in read_observed(path)}
    now = now_iso()
    nmax = int(env.cfg.get("evidence_max_chars", 1800))
    cur: dict[str, dict] = {}
    for f in findings:
        f = dict(f)
        f["title"] = redact(f["title"])[:120]
        f["summary"] = redact(f.get("summary", ""))[:400]
        f["evidence"] = _cut(redact(f["evidence"]), nmax)
        old = prev.get(f["id"])
        f["first_seen"] = (old or {}).get("first_seen") or now
        f["last_seen"] = now
        f["times_seen"] = int((old or {}).get("times_seen", 0)) + 1
        if f["id"] in cur:  # same fingerprint twice in one scan: keep the stronger one
            if cur[f["id"]]["severity"] >= f["severity"]:
                continue
        cur[f["id"]] = f
    resolved = []
    for oid, o in prev.items():
        if oid in cur:
            continue
        group = str(o.get("detector", "")).split(".", 1)[0]
        if group in detectors_run:
            resolved.append(dict(o, status="resolved", resolved_at=now))
        else:
            cur[oid] = o
    rows = sorted(cur.values(), key=lambda o: (-float(o.get("severity", 0)), o["id"]))
    rows = rows[: int(env.cfg.get("max_items", 80))]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in rows), encoding="utf-8")
    tmp.replace(path)
    if resolved:
        env.out.mkdir(parents=True, exist_ok=True)
        with (env.out / "observed_history.jsonl").open("a", encoding="utf-8") as fh:
            for o in resolved:
                fh.write(json.dumps(o, ensure_ascii=False) + "\n")
    write_profile(env, rows)
    by_type: dict[str, int] = {}
    for o in rows:
        by_type[o["candidate_type"]] = by_type.get(o["candidate_type"], 0) + 1
    return {"total": len(rows), "new": sum(1 for o in rows if o["id"] not in prev), "resolved": len(resolved),
            "by_type": by_type, "path": str(path)}


def write_profile(env: Env, rows: list[dict]) -> None:
    """profile/observed.md: facts observed in the user's files, each with a path and a date (三源画像规范)."""
    p = env.state_dir / "profile" / "observed.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    old = p.read_text(encoding="utf-8") if p.is_file() else "# observed：从你的代码、数据、日志中观察到的事实\n"
    L = [AUTO_START, f"## 本地观察（自动生成 · {now_iso()[:16]} · platform/observe.py）", "",
         "> 只是观察到的事实，不代表偏好或能力；删掉某一条：在评价页否决对应卡片，或把它的 obs_ 编号加进 negative.jsonl。", ""]
    for o in rows[:20]:
        L.append(f"- {str(o.get('last_seen', ''))[:10]} {o.get('evidence_ref') or '—'} · {o['title']}"
                 f"（{o.get('detector')}，严重度 {o.get('severity')}，{o['id']}）")
    if not rows:
        L.append("- （本次扫描没有发现）")
    L.append(AUTO_END)
    block = "\n".join(L)
    if AUTO_START in old and AUTO_END in old:
        a, b = old.index(AUTO_START), old.index(AUTO_END) + len(AUTO_END)
        new = old[:a] + block + old[b:]
    else:
        new = old.rstrip() + "\n\n" + block + "\n"
    p.write_text(new, encoding="utf-8")
