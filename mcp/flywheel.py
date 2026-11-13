#!/usr/bin/env python3
"""飞轮观测台：只读聚合现有反馈信号，让「越用越好用」看得见。

刻意做成**纯只读**：不改权重、不改画像、不写任何文件、不上传原文。
同时诚实列出「目前还没被消费的信号」——真实结果回填、隐式信号、画像
在现有闭环里都还没回流，不假装飞轮已经转起来。

    python mcp/flywheel.py --state-dir inspiration --self-test
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "insp-flywheel/1.0"
MIN_N = 5


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _jsonl(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except json.JSONDecodeError:
            continue
    return out


def _week(ts: str) -> str:
    try:
        d = date.fromisoformat(str(ts)[:10])
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    except (ValueError, TypeError):
        return ""


def _cards(sd: Path) -> int:
    n = 0
    for f in (sd / "cards").glob("*/cards.json"):
        try:
            n += len(json.loads(f.read_text(encoding="utf-8")).get("cards", []))
        except (OSError, json.JSONDecodeError):
            continue
    return n


def build(state_dir: Path, config: Path | None = None, today: date | None = None) -> dict:
    sd = Path(state_dir)
    today = today or date.today()
    VERDICT = _load("insp-feedback/scripts/record_verdict.py", "flywheel_verdict")
    OUTCOME = _load("insp-feedback/scripts/record_outcome.py", "flywheel_outcome")
    WR = _load("insp-review/scripts/weekly_review.py", "flywheel_weekly")

    verdicts = VERDICT.read_verdicts(sd)
    explicit = [v for v in verdicts if v.get("explicit") and v.get("action") != "retract"]
    by_action = defaultdict(int)
    for v in verdicts:
        by_action[str(v.get("action"))] += 1
    adopted = sum(1 for v in explicit if v.get("action") in ("adopt", "start_now", "done"))
    recent_cut = today - timedelta(days=14)
    recent = [v for v in explicit if _week(v.get("ts", "")) and
              date.fromisoformat(str(v["ts"])[:10]) >= recent_cut]
    weekly = defaultdict(lambda: {"explicit": 0, "adopted": 0})
    for v in explicit:
        w = _week(v.get("ts", ""))
        if not w:
            continue
        weekly[w]["explicit"] += 1
        if v.get("action") in ("adopt", "start_now", "done"):
            weekly[w]["adopted"] += 1

    outcomes = OUTCOME.summarize(sd)
    implicit = _jsonl(sd / "implicit.jsonl")
    negative = _jsonl(sd / "negative.jsonl")
    weights = json.loads((sd / "taste/weights.json").read_text(encoding="utf-8")) \
        if (sd / "taste/weights.json").is_file() else {}
    history = weights.get("history") or []
    _, props = WR.review(sd, today, None, Path(config) if config else None)

    feature_stats = props.get("feature_stats") or {}
    proposal = props.get("proposal")
    missing = [st["missing"] for vals in feature_stats.values() for st in vals.values() if st.get("missing")]
    nearest = min(missing) if missing else 0

    learned = []
    if explicit:
        learned.append(f"累计 {len(explicit)} 条显式评价，其中 {adopted} 条采纳类；最近 14 天 {len(recent)} 条。")
    if outcomes["total"]:
        rate = "暂不可算" if outcomes["confirmed_rate"] is None else f"{outcomes['confirmed_rate']:.0%}"
        learned.append(f"采纳后结果回填 {outcomes['total']} 张，结论成立率 {rate}。")
    if history:
        learned.append(f"权重已确认调整 {len(history)} 次，当前版本 {weights.get('version')}。")
    if not learned:
        learned.append("还没有任何反馈信号，飞轮尚未开始转动。")

    gaps = [
        "采纳后结果回填（outcomes.jsonl）目前只进周报第 9 节展示，不参与 p̂ / 权重 / 去重 / 排序（B1 的隔离约定）。",
        "隐式信号（implicit.jsonl 的停留 / 翻面）只被记录，不参与权重（项目约定「只问不改」）。",
        "profile/interests.md 建立画像后不会自动更新，画像只用于出卡提示词。",
    ]
    if nearest:
        gaps.append(f"距下一次可提议的权重调整，最近的维度还差 {nearest} 条有效评价（门槛 n≥{MIN_N} 且 |Δ|≥0.01）。")
    if proposal:
        gaps.append("已有满足门槛的权重提议，但仍需你在本机 --confirm 才会生效；MCP 不会代你确认。")

    status = "stalled" if explicit and not recent else ("turning" if proposal or history else "collecting")
    return {
        "schema": SCHEMA, "generated_at": now_iso(), "state_dir": sd.name,
        "status": status,
        "signals": {
            "cards": {"total": _cards(sd), "pending": max(0, _cards(sd) - len(VERDICT.judged_ids(sd)))},
            "verdicts": {"total": len(verdicts), "explicit": len(explicit), "adopted": adopted,
                         "adoption_rate": round(adopted / len(explicit), 3) if explicit else None,
                         "by_action": dict(by_action), "last_14d": len(recent)},
            "outcomes": outcomes,
            "implicit": {"total": len(implicit), "flipped": sum(1 for r in implicit if r.get("flipped")),
                         "avg_dwell_ms": round(sum(int(r.get("dwell_ms") or 0) for r in implicit) / len(implicit))
                         if implicit else None},
            "negative": {"total": len(negative), "topics": sum(1 for r in negative if r.get("kind") == "topic")},
            "weights": {"version": weights.get("version"), "updated_at": weights.get("updated_at"),
                        "history_len": len(history),
                        "non_default": {k: v for k, v in (weights.get("type") or {}).items() if v != 1.0}},
            "trend": [{"week": w, **weekly[w]} for w in sorted(weekly)[-4:]],
        },
        "flywheel": {
            "p_hat_by_feature": feature_stats,
            "next_weight_proposal": proposal,
            "samples_to_next_proposal": nearest,
            "learned": learned,
            "not_consumed": gaps,
        },
        "note": "纯只读观测：不改权重、不改画像、不写文件、不上传评价原文。权重与画像的变更仍需你在本机 --confirm。",
    }


def self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_flywheel_"))
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:250]) if detail and not cond else ""))
        ok &= bool(cond)

    try:
        sd = tmp / "inspiration"
        shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
        before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
                  for p in sd.rglob("*") if p.is_file()}
        d = build(sd, today=date(2026, 11, 13))
        check("schema 与状态", d["schema"] == SCHEMA and d["status"] in ("collecting", "turning", "stalled"))
        check("信号齐全", all(k in d["signals"] for k in ("cards", "verdicts", "outcomes", "implicit",
                                                        "negative", "weights", "trend")))
        check("诚实标注未消费信号", len(d["flywheel"]["not_consumed"]) >= 3 and
              any("outcomes.jsonl" in x for x in d["flywheel"]["not_consumed"]))
        check("空状态不假装已经学到东西", any("尚未开始" in x for x in d["flywheel"]["learned"]))
        after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in sd.rglob("*") if p.is_file()}
        check("完全只读", before == after, str(set(before) ^ set(after)))

        (sd / "cards" / "2026-11-13").mkdir(parents=True, exist_ok=True)
        (sd / "cards" / "2026-11-13" / "cards.json").write_text(json.dumps(
            {"date": "2026-11-13", "cards": [{"id": "ins_fw_1", "type": "knowledge", "title": "t",
                                              "source": {"ref": "9999.1", "kind": "arxiv"}}]}, ensure_ascii=False),
            encoding="utf-8")
        (sd / "verdicts.jsonl").write_text(json.dumps(
            {"ts": "2026-11-13T10:00:00+08:00", "card_id": "ins_fw_1", "action": "adopt", "label": "采纳",
             "weight": 1.0, "explicit": True, "type": "knowledge", "reason": "原文不该出现"},
            ensure_ascii=False) + "\n", encoding="utf-8")
        (sd / "outcomes.jsonl").write_text(json.dumps(
            {"ts": "2026-11-13T11:00:00+08:00", "card_id": "ins_fw_1", "outcome": "confirmed",
             "conclusion": "绝密结论", "measured": "3%", "via": "mcp"}, ensure_ascii=False) + "\n", encoding="utf-8")
        (sd / "implicit.jsonl").write_text(json.dumps(
            {"ts": "2026-11-13T11:00:00+08:00", "card_id": "ins_fw_1", "event": "flip",
             "flipped": True, "dwell_ms": 4200}, ensure_ascii=False) + "\n", encoding="utf-8")
        d2 = build(sd, today=date(2026, 11, 13))
        text = json.dumps(d2, ensure_ascii=False)
        check("聚合出新信号", d2["signals"]["verdicts"]["explicit"] == 1 and
              d2["signals"]["outcomes"]["total"] == 1 and d2["signals"]["implicit"]["flipped"] == 1)
        check("趋势按周", d2["signals"]["trend"] and d2["signals"]["trend"][-1]["week"] == "2026-W46")
        check("不含用户原文", "原文不该出现" not in text and "绝密结论" not in text and "C:\\" not in text)
        check("样本缺口可算", d2["flywheel"]["samples_to_next_proposal"] >= 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST OK: flywheel" if ok else "SELF-TEST FAILED: flywheel")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", type=Path, default=ROOT / "mcp_demo_state")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    print(json.dumps(build(a.state_dir, a.config), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
