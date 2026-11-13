#!/usr/bin/env python3
"""B1 → ③：把「采纳后结果回填」变成独立的**校准提议**（calibration）。

采纳率（verdict）和结果成立率（outcome）是两套标签，混用会把「我爱点采纳」误当成
「建议真的成立」。本模块只读 outcomes.jsonl / verdicts.jsonl / cards，产出一份
insp-calibration/1.0 提议：

    state_dir/calibration.json          提议（confirmed 永远为 false，除非你显式 --confirm）
    state_dir/taste/calibration.json   确认后才写入的生效提示（供写卡提示词消费）

刻意保持的边界：
  - 不进 p̂ / Δw / weights.json / negative.jsonl / 去重 / 排序；周报第 1–3 节的数字不变；
  - 提议 kind 恒为 prompt_hint，不是权重提议；apply() 会拒绝任何伪装成权重调整的提议；
  - 只写聚合数值与系统生成的固定提示，不含评价理由、回填结论、卡片标题、引文、路径；
  - 只有本机 --confirm 才会生成生效提示，网页与 MCP 都不能代你确认。

Usage:
    python platform/calibration.py --state-dir inspiration
    python platform/calibration.py --state-dir inspiration --write
    python platform/calibration.py --state-dir inspiration --apply inspiration/calibration.json --confirm
    python platform/calibration.py --state-dir inspiration --rollback
    python platform/calibration.py --self-test
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "insp-calibration/1.0"
APPLIED_SCHEMA = "insp-calibration-applied/1.0"
PROPOSAL_FILE = "calibration.json"
APPLIED_REL = Path("taste") / "calibration.json"
MIN_N = 3          # 少于 3 条做完的回填只记录、不提议
MIN_GAP = 0.2      # 采纳率与结果成立率的差距门槛，避免小样本噪音
DONE = ("confirmed", "refuted")
POSITIVE = {"adopt", "start_now", "edited"}
DIMS = ("type", "subfield")
DIM_FOR_PROPOSAL = "type"   # type 是受控枚举；subfield 是自由文本，只观测不进提议文本

HINT_STRENGTHEN = (
    "实测校准：{key} 类建议的采纳率明显高于结论成立率（{adoption_rate:.0%} vs {confirmed_rate:.0%}，"
    "有效结果 {n} 条）。写这类卡时把 expected_observable / falsifier 写成可量化的判据，"
    "minimal_check 写清两天内能做完的步骤；证据不足的候选宁可放进 rejected。"
    "这只是写卡提示，不改 rubric、权重、配额或排序。"
)
HINT_BROADEN = (
    "实测校准：{key} 类建议的结论成立率高于采纳率（{confirmed_rate:.0%} vs {adoption_rate:.0%}，"
    "有效结果 {n} 条）。这类建议的实测价值可能被低估，写卡时不必额外收紧，"
    "但每张卡仍要给出可验证的判据。这只是写卡提示，不改 rubric、权重、配额或排序。"
)

ISOLATION = {
    "writes": ["calibration.json（提议）", "taste/calibration.json（仅 --confirm 后）"],
    "never_writes": ["taste/weights.json", "verdicts.jsonl", "outcomes.jsonl", "negative.jsonl",
                     "cards/", "taste/rubric.md", "profile/"],
    "not_in": ["p_hat", "delta_w", "weights", "triage_ranking", "dedup", "negative_list"],
    "labels": {"verdict": "采纳率（用户是否点了采纳）", "outcome": "结论成立率（建议做完是否真的成立）"},
}


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


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def latest_outcomes(sd: Path) -> dict[str, dict]:
    """与 record_outcome.latest_by_card 同语义：append-only，一卡取最后一条。"""
    out: dict[str, dict] = {}
    for row in _jsonl(sd / "outcomes.jsonl"):
        cid = row.get("card_id")
        if cid:
            out[cid] = row
    return out


def verdict_stacks(sd: Path) -> dict[str, dict]:
    """显式评价栈：retract 弹栈顶，只取最后一条显式评价。与 weekly_review 同语义。"""
    stacks: dict[str, list[dict]] = defaultdict(list)
    for v in _jsonl(sd / "verdicts.jsonl"):
        cid = v.get("card_id")
        if not cid:
            continue
        if v.get("action") == "retract":
            if stacks.get(cid):
                stacks[cid].pop()
        elif v.get("explicit"):
            stacks[cid].append(v)
    return {k: st[-1] for k, st in stacks.items() if st}


def cards_meta(sd: Path) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for f in sorted((sd / "cards").glob("*/cards.json")):
        try:
            rows = json.loads(f.read_text(encoding="utf-8")).get("cards", [])
        except (OSError, json.JSONDecodeError):
            continue
        for c in rows:
            if isinstance(c, dict) and c.get("id"):
                meta[c["id"]] = {"type": c.get("type"), "subfield": c.get("subfield")}
    return meta


def _dim_value(cid: str, dim: str, meta: dict, stacks: dict) -> str | None:
    val = (meta.get(cid) or {}).get(dim) or (stacks.get(cid) or {}).get(dim)
    return str(val) if val else None


def _aggregate(dim: str, sd: Path, latest: dict, stacks: dict, meta: dict) -> dict:
    """按维度给出两套标签：评价侧的采纳率 vs 结果侧结论成立率，并算出差距。"""
    evaluated: dict[str, int] = defaultdict(int)
    adopted: dict[str, int] = defaultdict(int)
    for cid, v in stacks.items():
        val = _dim_value(cid, dim, meta, stacks)
        if not val:
            continue
        evaluated[val] += 1
        if v.get("action") in POSITIVE:
            adopted[val] += 1

    done: dict[str, list[str]] = defaultdict(list)
    for cid, o in latest.items():
        if o.get("outcome") not in DONE:
            continue
        val = _dim_value(cid, dim, meta, stacks)
        if val:
            done[val].append(o["outcome"])

    out: dict[str, dict] = {}
    for val in sorted(set(evaluated) | set(done)):
        labels = done.get(val, [])
        n = len(labels)
        conf = labels.count("confirmed")
        cr = _rate(conf, n)
        ar = _rate(adopted.get(val, 0), evaluated.get(val, 0))
        gap = round(ar - cr, 3) if ar is not None and cr is not None else None
        out[val] = {"n": n, "confirmed": conf, "refuted": n - conf, "confirmed_rate": cr,
                    "evaluated": evaluated.get(val, 0), "adopted": adopted.get(val, 0),
                    "adoption_rate": ar, "gap": gap,
                    "eligible": n >= MIN_N and gap is not None and abs(gap) >= MIN_GAP}
    return out


def read_applied(sd: Path) -> dict | None:
    p = sd / APPLIED_REL
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("schema") == APPLIED_SCHEMA else None
    except (OSError, json.JSONDecodeError):
        return None


def active_hint(sd: Path) -> dict | None:
    """给脱敏摘要用：只带生效提示的结构化字段，不带历史与本机路径。"""
    d = read_applied(sd)
    a = (d or {}).get("active")
    if not isinstance(a, dict):
        return None
    return {k: a.get(k) for k in ("calibration_id", "dimension", "key", "direction", "hint", "evidence")}


def build(state_dir: Path, config: Path | None = None, today: date | None = None) -> dict:
    """纯只读聚合。config 保留参数位以便未来接学习参数，当前不读配置、不联网。"""
    sd = Path(state_dir)
    today = today or date.today()
    latest = latest_outcomes(sd)
    stacks = verdict_stacks(sd)
    meta = cards_meta(sd)
    by_type = _aggregate("type", sd, latest, stacks, meta)
    by_subfield = _aggregate("subfield", sd, latest, stacks, meta)

    done = [o for o in latest.values() if o.get("outcome") in DONE]
    conf = sum(1 for o in done if o["outcome"] == "confirmed")
    adopted = sum(1 for v in stacks.values() if v.get("action") in POSITIVE)
    confirmed_rate = _rate(conf, len(done))
    adoption_rate = _rate(adopted, len(stacks))

    candidates = [(abs(v["gap"]), -v["n"], k, v) for k, v in by_type.items() if v["eligible"]]
    proposal = None
    if candidates:
        candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
        _, _, key, st = candidates[0]
        strengthen = st["gap"] >= MIN_GAP
        proposal = {
            "kind": "prompt_hint", "target": "insp-card 写卡提示", "dimension": DIM_FOR_PROPOSAL,
            "key": key, "direction": "strengthen_evidence" if strengthen else "broaden_coverage",
            "hint": (HINT_STRENGTHEN if strengthen else HINT_BROADEN).format(
                key=key, n=st["n"], adoption_rate=st["adoption_rate"], confirmed_rate=st["confirmed_rate"]),
            "evidence": {"n": st["n"], "confirmed_rate": st["confirmed_rate"],
                         "adoption_rate": st["adoption_rate"], "gap": st["gap"],
                         "min_n": MIN_N, "min_gap": MIN_GAP},
            "confirmed": False,
        }

    iso = today.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    return {
        "schema": SCHEMA, "generated_at": now_iso(), "generated_for": today.isoformat(), "week": week,
        "calibration_id": f"cal-{today.isoformat()}",
        "n": len(done), "confirmed": conf, "refuted": len(done) - conf,
        "confirmed_rate": confirmed_rate, "adoption_rate": adoption_rate,
        "by_type": by_type, "by_subfield": by_subfield,
        "proposal": proposal, "applied": active_hint(sd), "isolation": ISOLATION,
        "note": ("采纳率 ≠ 结论成立率：by_type / by_subfield 里两列分别来自 verdicts.jsonl 与 outcomes.jsonl。"
                 "提议只是写卡提示，不改权重、画像、配额或排序；subfield 为自由文本，只观测不进提议文本；"
                 "只有本机 --confirm 才会生效。"),
    }


def write(state_dir: Path, config: Path | None = None, today: date | None = None) -> Path:
    sd = Path(state_dir)
    d = build(sd, config, today)
    p = sd / PROPOSAL_FILE
    tmp = sd / (PROPOSAL_FILE + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def _write_applied(sd: Path, d: dict) -> None:
    p = sd / APPLIED_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def apply(state_dir: Path, proposal_file: Path) -> str:
    sd = Path(state_dir)
    try:
        p = json.loads(Path(proposal_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"REFUSED: 提议文件读不了（{type(exc).__name__}）"
    if not isinstance(p, dict) or p.get("schema") != SCHEMA:
        return f"REFUSED: 不是 {SCHEMA} 提议"
    prop = p.get("proposal")
    if not prop:
        return "NOTHING: 本周没有满足门槛的校准提议"
    if prop.get("confirmed") is not True:
        return "REFUSED: 校准提议未被确认（confirmed 不是 true），不写生效提示"
    if prop.get("kind") != "prompt_hint" or prop.get("dimension") != DIM_FOR_PROPOSAL:
        return "REFUSED: 只接受 prompt_hint / type 校准提议，不接受权重或其他维度的调整"
    if not isinstance(prop.get("hint"), str) or not prop["hint"]:
        return "REFUSED: 提议缺少提示文本"

    cur = read_applied(sd) or {"schema": APPLIED_SCHEMA, "version": 0, "active": None, "history": []}
    rid = p.get("calibration_id")
    if any(h.get("calibration_id") == rid and not h.get("rolled_back") for h in cur.get("history", [])):
        return f"REFUSED: 校准提议 {rid} 已经应用过"
    entry = {"calibration_id": rid, "applied_at": now_iso(), "dimension": prop["dimension"],
             "key": prop["key"], "direction": prop["direction"], "hint": prop["hint"],
             "evidence": prop.get("evidence") or {}}
    cur["active"] = entry
    cur.setdefault("history", []).append(dict(entry))
    cur["version"] = int(cur.get("version", 0)) + 1
    _write_applied(sd, cur)
    return f"APPLIED: {prop['key']} 校准提示已生效（v{cur['version']}），未改任何权重"


def rollback(state_dir: Path) -> str:
    sd = Path(state_dir)
    cur = read_applied(sd)
    if not cur or not cur.get("active"):
        return "NOTHING: 没有生效中的校准提示"
    last = cur["active"]
    for h in reversed(cur.get("history", [])):
        if h.get("calibration_id") == last.get("calibration_id"):
            h["rolled_back"] = True
            break
    cur["active"] = None
    cur["version"] = int(cur.get("version", 0)) + 1
    _write_applied(sd, cur)
    return f"ROLLED_BACK: 已撤销 {last.get('key')} 的校准提示（v{cur['version']}）"


def self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_calibration_"))
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:250]) if detail and not cond else ""))
        ok &= bool(cond)

    try:
        sd = tmp / "inspiration"
        shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
        d0 = build(sd, today=date(2026, 11, 13))
        check("空状态不假装学到东西", d0["n"] == 0 and d0["confirmed_rate"] is None and d0["proposal"] is None)

        (sd / "cards" / "2026-11-13").mkdir(parents=True, exist_ok=True)
        cards = [{"id": f"ins_cal_{i}", "type": "knowledge", "subfield": "memory", "title": "标题绝密",
                  "source": {"ref": f"9999.{i}", "quote": "引文绝密"}} for i in range(1, 7)]
        (sd / "cards" / "2026-11-13" / "cards.json").write_text(
            json.dumps({"date": "2026-11-13", "cards": cards}, ensure_ascii=False), encoding="utf-8")
        verdicts = [{"ts": "2026-11-13T09:00:00+08:00", "card_id": c["id"], "type": "knowledge",
                     "action": "adopt", "explicit": True, "reason": "理由绝密"} for c in cards]
        (sd / "verdicts.jsonl").write_text(
            "\n".join(json.dumps(v, ensure_ascii=False) for v in verdicts) + "\n", encoding="utf-8")
        outcomes = [{"ts": "2026-11-13T12:00:00+08:00", "card_id": c["id"],
                     "outcome": "confirmed" if i < 2 else "refuted", "conclusion": "结论绝密",
                     "measured": "3%", "via": "cli"} for i, c in enumerate(cards)]
        (sd / "outcomes.jsonl").write_text(
            "\n".join(json.dumps(o, ensure_ascii=False) for o in outcomes) + "\n", encoding="utf-8")

        watched = {p.relative_to(sd).as_posix(): p.read_bytes() for p in sd.rglob("*") if p.is_file()}
        d = build(sd, today=date(2026, 11, 13))
        check("两套标签分开算", d["n"] == 6 and d["confirmed"] == 2 and d["confirmed_rate"] == 0.333
              and d["adoption_rate"] == 1.0 and d["by_type"]["knowledge"]["gap"] == 0.667)
        check("提议是提示词而非权重", d["proposal"] and d["proposal"]["kind"] == "prompt_hint"
              and d["proposal"]["confirmed"] is False and "from" not in d["proposal"]
              and "to" not in d["proposal"])
        text = json.dumps(d, ensure_ascii=False)
        check("不含用户原文 / 标题 / 引文 / 路径",
              all(x not in text for x in ("理由绝密", "结论绝密", "标题绝密", "引文绝密", "ins_cal_", "9999.")))
        check("build 完全只读", watched == {p.relative_to(sd).as_posix(): p.read_bytes()
                                           for p in sd.rglob("*") if p.is_file()})

        pf = write(sd, today=date(2026, 11, 13))
        check("提议落盘且仍未确认", json.loads(pf.read_text(encoding="utf-8"))["proposal"]["confirmed"] is False)
        check("未确认被拒", apply(sd, pf).startswith("REFUSED"))
        check("坏 schema 被拒", apply(sd, sd / "taste/weights.json").startswith("REFUSED"))

        pj = json.loads(pf.read_text(encoding="utf-8"))
        pj["proposal"]["confirmed"] = True
        pf.write_text(json.dumps(pj, ensure_ascii=False), encoding="utf-8")
        before = {q.relative_to(sd).as_posix(): q.read_bytes() for q in sd.rglob("*") if q.is_file()
                  and q.name not in (PROPOSAL_FILE, APPLIED_REL.name)}
        check("确认后应用成功", apply(sd, pf).startswith("APPLIED"))
        after = {q.relative_to(sd).as_posix(): q.read_bytes() for q in sd.rglob("*") if q.is_file()
                 and q.name not in (PROPOSAL_FILE, APPLIED_REL.name)}
        check("绝不改权重 / 评价 / 回填 / 卡片", before == after, str(set(before) ^ set(after)))
        check("生效提示可读", (active_hint(sd) or {}).get("key") == "knowledge")
        check("重复应用被拒", apply(sd, pf).startswith("REFUSED"))
        fake = json.loads(pf.read_text(encoding="utf-8"))
        fake["proposal"]["kind"] = "weight"
        fake["calibration_id"] = "cal-fake"
        fake_path = tmp / "fake.json"
        fake_path.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")
        check("伪装成权重调整被拒", apply(sd, fake_path).startswith("REFUSED"))
        check("可回滚", rollback(sd).startswith("ROLLED_BACK") and active_hint(sd) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST OK: calibration" if ok else "SELF-TEST FAILED: calibration")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", type=Path, default=ROOT / "inspiration")
    ap.add_argument("--config", type=Path, help="保留参数位：当前不读配置，聚合门槛固定")
    ap.add_argument("--today", help="YYYY-MM-DD，缺省今天")
    ap.add_argument("--write", action="store_true", help="把提议写入状态目录的 calibration.json")
    ap.add_argument("--apply", type=Path, help="应用提议文件；必须同时给 --confirm")
    ap.add_argument("--confirm", action="store_true", help="我已读过提议，显式确认（把 confirmed 改成 true）")
    ap.add_argument("--rollback", action="store_true", help="撤销当前生效的校准提示")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.rollback:
        print(rollback(a.state_dir))
        return 0
    if a.apply:
        if a.confirm:
            pj = json.loads(a.apply.read_text(encoding="utf-8"))
            if pj.get("proposal"):
                pj["proposal"]["confirmed"] = True
                pj["proposal"]["confirmed_at"] = now_iso()
                a.apply.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
        msg = apply(a.state_dir, a.apply)
        print(msg)
        return 0 if msg.startswith(("APPLIED", "NOTHING")) else 1
    today = date.fromisoformat(a.today) if a.today else None
    if a.write:
        print(write(a.state_dir, a.config, today))
    else:
        print(json.dumps(build(a.state_dir, a.config, today), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
