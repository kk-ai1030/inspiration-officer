#!/usr/bin/env python3
"""B4 / K3：为网页周报生成「脱敏评价摘要」。

原始 verdicts.jsonl 从不上传（评价读写约定.md 第 1 节）。网页里的 insp-review 技能
拿不到它，于是在对话里说「做周报」要么失败、要么编数字。这里把 weekly_review 的
结果挑成一份**只含聚合数值**的 verdict_summary.json，随状态快照推进沙箱：

    /workspace/inspiration_state/verdict_summary.json   （收货员 push_state）
    /workspace/_refs/verdict_summary.json               （本地按钮触发时随附件挂载）

摘要里刻意不含：评价原文、卡片标题/引文、理由、屏蔽主题、记忆内容、本机路径、
邮箱、UUID。网页技能只渲染这份摘要；找不到就明确拒绝，不手算、不编造。

Usage:
    python platform/verdict_summary.py --state-dir inspiration --write
    python platform/verdict_summary.py --self-test
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_FILE = "verdict_summary.json"
SCHEMA = "insp-verdict-summary/1.0"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build(state_dir: Path, config: Path | None = None, today: date | None = None) -> dict:
    """调用本地周报逻辑，但只保留聚合字段；绝不把评价原文带出本机。"""
    wr = _load("insp-review/scripts/weekly_review.py", "vs_weekly_review")
    _, props = wr.review(Path(state_dir), today or date.today(), None, Path(config) if config else None)
    prop = props.get("proposal")
    safe_prop = None
    if isinstance(prop, dict):
        safe_prop = {k: prop.get(k) for k in ("dimension", "key", "from", "to", "evidence",
                                              "params", "weights_version_before")}
        safe_prop["confirmed"] = False   # 摘要里的提议永远未确认，网页不能拿它去改权重
    return {
        "schema": SCHEMA,
        "generated_at": now_iso(),
        "week": props.get("week"),
        "generated_for": props.get("generated_for"),
        "adoption": props.get("adoption"),
        "repeat_rate": props.get("repeat_rate"),
        "implicit_week": props.get("implicit_week"),
        "weights_version": props.get("weights_version"),
        "feature_stats": props.get("feature_stats") or {},
        "type_stats": props.get("type_stats") or {},
        "proposal": safe_prop,
        "outcome_summary": props.get("outcome_summary") or {},
        "note": "脱敏聚合：不含评价原文、卡片标题、引文、记忆内容与本机路径",
    }


def write(state_dir: Path, config: Path | None = None, today: date | None = None) -> Path:
    sd = Path(state_dir)
    summary = build(sd, config, today)
    p = sd / SUMMARY_FILE
    tmp = sd / (SUMMARY_FILE + ".tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_vsummary_"))
    sd = tmp / "inspiration"
    shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:200]) if detail and not cond else ""))
        ok &= bool(cond)

    try:
        verdict = {"ts": "2026-11-12T10:00:00+08:00", "card_id": "ins_vs_1", "source_ref": "9999.12345",
                   "type": "knowledge", "action": "adopt", "label": "采纳", "weight": 1.0,
                   "weight_source": "default", "explicit": True, "reason": "正好要补基线",
                   "rubric_rule_ids": [], "weights_version": 0}
        (sd / "verdicts.jsonl").write_text(json.dumps(verdict, ensure_ascii=False) + "\n", encoding="utf-8")
        (sd / "outcomes.jsonl").write_text(json.dumps(
            {"ts": "2026-11-12T11:00:00+08:00", "card_id": "ins_vs_1", "outcome": "confirmed",
             "label": "做了，结论成立", "conclusion": "结论成立，跑了两组", "measured": "", "via": "cli"},
            ensure_ascii=False) + "\n", encoding="utf-8")
        s = build(sd, today=date(2026, 11, 12))
        text = json.dumps(s, ensure_ascii=False)
        check("schema 正确", s["schema"] == SCHEMA and bool(s["week"]))
        check("带聚合数值", "adoption" in s and "feature_stats" in s and s["outcome_summary"]["total"] == 1)
        check("提议永远未确认", s["proposal"] is None or s["proposal"]["confirmed"] is False)
        check("不含评价原文 / 标题 / 路径 / 邮箱 / UUID",
              all(x not in text for x in ("正好要补基线", "结论成立，跑了两组", "ins_vs_1", "9999.12345",
                                          "C:\\", "@", "/workspace")), text[:200])
        p = write(sd, today=date(2026, 11, 12))
        check("原子写出且可解析", p.is_file() and json.loads(p.read_text(encoding="utf-8"))["schema"] == SCHEMA)
        wr = _load("insp-review/scripts/weekly_review.py", "vs_weekly_render")
        check("网页渲染成功", "## 1. 两个核心指标" in wr.render_from_summary(json.loads(p.read_text(encoding="utf-8"))))
        check("坏摘要被拒绝", _raises(lambda: wr.render_from_summary({"schema": "wrong"})))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST OK: verdict_summary" if ok else "SELF-TEST FAILED: verdict_summary")
    return 0 if ok else 1


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except (ValueError, LookupError, KeyError):
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", type=Path, default=Path("inspiration"))
    ap.add_argument("--config", type=Path)
    ap.add_argument("--write", action="store_true", help="写入状态目录的 verdict_summary.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    s = build(a.state_dir, a.config)
    if a.write:
        print(write(a.state_dir, a.config))
    else:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
