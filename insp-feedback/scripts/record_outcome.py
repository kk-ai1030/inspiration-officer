#!/usr/bin/env python3
"""采纳后结果回填：记录「这条建议最后做没做、结论是否成立」。

与评价（verdict）严格分开：只追加到状态目录的 outcomes.jsonl，不写 verdicts.jsonl、
不改卡片 status、不进负面清单、不参与 p̂ / 权重计算，也不影响 retract 的评价栈。
这样记录的是真实科研结果，而不是对卡片的又一次打分。

Usage:
    python3 record_outcome.py --state-dir inspiration --card-id ins_x --outcome confirmed \
        --conclusion "复现了" --measured "A 比 B 高 3.2%"
    python3 record_outcome.py --state-dir inspiration --list
    python3 record_outcome.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

OUTCOME_FILE = "outcomes.jsonl"
OUTCOMES = {
    "confirmed": "做了，结论成立",
    "refuted": "做了，结论不成立",
    "abandoned": "没做 / 放弃了",
    "unknown": "还没做，待定",
}
MAX_TEXT = 2000


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_outcomes(state_dir: Path) -> list[dict]:
    p = state_dir / OUTCOME_FILE
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def latest_by_card(state_dir: Path) -> dict[str, dict]:
    """一卡取最后一条回填；append-only 文件，重复回填以最新为准。"""
    out: dict[str, dict] = {}
    for row in read_outcomes(state_dir):
        cid = row.get("card_id")
        if cid:
            out[cid] = row
    return out


def find_card(state_dir: Path, card_id: str) -> dict | None:
    for f in sorted((state_dir / "cards").glob("*/cards.json")):
        for c in json.loads(f.read_text(encoding="utf-8")).get("cards", []):
            if c.get("id") == card_id:
                return c
    return None


def explicit_stacks(state_dir: Path) -> dict[str, list[dict]]:
    """与 record_verdict.verdict_stacks 同语义：retract 弹出栈顶，只看显式评价。"""
    p = state_dir / "verdicts.jsonl"
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.is_file() else []
    stacks: dict[str, list[dict]] = {}
    for v in rows:
        cid = v.get("card_id")
        if v.get("action") == "retract":
            if stacks.get(cid):
                stacks[cid].pop()
        elif v.get("explicit"):
            stacks.setdefault(cid, []).append(v)
    return {k: st for k, st in stacks.items() if st}


def record(state_dir: Path, card_id: str, outcome: str, conclusion: str = "", measured: str = "",
           ts: str | None = None, via: str | None = None) -> dict:
    if outcome not in OUTCOMES:
        raise ValueError(f"未知结果：{outcome}，可选 {sorted(OUTCOMES)}")
    if not card_id:
        raise ValueError("缺少 card_id")
    if find_card(state_dir, card_id) is None:
        raise LookupError(f"在 {state_dir}/cards 下找不到卡片 {card_id}")
    if card_id not in explicit_stacks(state_dir):
        raise ValueError("这张卡还没有显式评价，先评价再回填结果")
    entry = {
        "ts": ts or now_iso(),
        "card_id": card_id,
        "outcome": outcome,
        "label": OUTCOMES[outcome],
        "conclusion": str(conclusion or "").strip()[:MAX_TEXT],
        "measured": str(measured or "").strip()[:MAX_TEXT],
        "via": via or "cli",
    }
    with (state_dir / OUTCOME_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def summarize(state_dir: Path) -> dict:
    """只做聚合统计：给周报与后续学习闭环用，不含卡片标题、引文等原文。"""
    latest = latest_by_card(state_dir)
    counts = {k: 0 for k in OUTCOMES}
    for row in latest.values():
        if row.get("outcome") in counts:
            counts[row["outcome"]] += 1
    done = counts["confirmed"] + counts["refuted"]
    return {
        "total": len(latest),
        "by_outcome": counts,
        "done": done,
        "confirmed_rate": round(counts["confirmed"] / done, 3) if done else None,
        "with_measured": sum(1 for r in latest.values() if r.get("measured")),
    }


def render_section(state_dir: Path) -> list[str]:
    """周报第 9 节。只输出聚合值 + 用户自己写的结论，避免把卡片原文塞进报告。"""
    s = summarize(state_dir)
    c = s["by_outcome"]
    lines = ["", "## 9. 采纳后结果回填（B1）", "",
             f"- 已回填 {s['total']} 张：结论成立 {c['confirmed']} · 结论不成立 {c['refuted']} · "
             f"没做 {c['abandoned']} · 待定 {c['unknown']}"]
    lines.append(f"- 做了的里面，结论成立率：{s['confirmed_rate']:.0%}（{c['confirmed']}/{s['done']}）"
                 if s["confirmed_rate"] is not None else "- 还没有做完的卡片，结论成立率暂不可算。")
    lines.append(f"- 带实测数字的记录：{s['with_measured']} 条。")
    excerpts = [r for r in latest_by_card(state_dir).values() if r.get("conclusion")][:3]
    lines += ["", "实测摘录（最多 3 条，来自你自己填写的结论）：", ""]
    lines += [f"- `{r['card_id']}`（{OUTCOMES.get(r['outcome'], r['outcome'])}）：{r['conclusion'][:80]}"
              for r in excerpts] or ["- 暂无。"]
    return lines


def self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_outcome_"))
    sd = tmp / "inspiration"
    (sd / "cards" / "2026-01-01").mkdir(parents=True)
    card = {"id": "ins_outcome_1", "title": "卡片", "source": {"ref": "9999.00001"}, "type": "knowledge",
            "status": "proposed", "verdict": None}
    (sd / "cards" / "2026-01-01" / "cards.json").write_text(
        json.dumps({"date": "2026-01-01", "cards": [card]}, ensure_ascii=False), encoding="utf-8")
    verdict = {"ts": now_iso(), "card_id": "ins_outcome_1", "action": "adopt", "explicit": True, "weight": 1.0}
    (sd / "verdicts.jsonl").write_text(json.dumps(verdict, ensure_ascii=False) + "\n", encoding="utf-8")

    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok &= bool(cond)

    try:
        check("无评价时拒绝回填", _raises(lambda: record(sd, "ins_missing", "confirmed")))
        check("非法结果被拒", _raises(lambda: record(sd, "ins_outcome_1", "made_up")))
        check("卡片不存在被拒", _raises(lambda: record(sd, "ins_nope", "unknown")))
        record(sd, "ins_outcome_1", "unknown", conclusion="先记下", ts="2026-01-02T10:00:00+00:00")
        e = record(sd, "ins_outcome_1", "confirmed", conclusion="跑通了", measured="提升 3.2%",
                   ts="2026-01-03T10:00:00+00:00")
        check("返回条目完整", e["outcome"] == "confirmed" and e["label"] == OUTCOMES["confirmed"])
        check("只写 outcomes.jsonl", len(read_outcomes(sd)) == 2 and
              len((sd / "verdicts.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 1)
        check("取最后一条", latest_by_card(sd)["ins_outcome_1"]["outcome"] == "confirmed")
        check("卡片状态未被改", find_card(sd, "ins_outcome_1")["status"] == "proposed")
        s = summarize(sd)
        check("聚合正确", s["total"] == 1 and s["by_outcome"]["confirmed"] == 1 and s["confirmed_rate"] == 1.0)
        text = "\n".join(render_section(sd))
        check("周报章节含结论成立率", "## 9. 采纳后结果回填" in text and "100%" in text and "跑通了" in text)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST OK: record_outcome" if ok else "SELF-TEST FAILED: record_outcome")
    return 0 if ok else 1


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except (ValueError, LookupError):
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", type=Path, default=Path("inspiration"))
    ap.add_argument("--card-id")
    ap.add_argument("--outcome", choices=sorted(OUTCOMES))
    ap.add_argument("--conclusion", default="")
    ap.add_argument("--measured", default="")
    ap.add_argument("--list", action="store_true", help="列出所有卡片的最新回填")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.list:
        print(json.dumps(latest_by_card(a.state_dir), ensure_ascii=False, indent=2))
        return 0
    if not a.card_id or not a.outcome:
        ap.error("需要 --card-id 与 --outcome（或用 --list / --self-test）")
    try:
        print(json.dumps(record(a.state_dir, a.card_id, a.outcome, a.conclusion, a.measured), ensure_ascii=False))
    except (ValueError, LookupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
