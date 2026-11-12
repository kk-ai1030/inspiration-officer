#!/usr/bin/env python3
"""Weekly review for the inspiration mechanism.

Computes adoption rate, repeat rate, Bayesian per-type acceptance, ONE proposed weight change,
rubric-rule diagnostics, an audit list of auto-extracted platform memories, and 90-day follow-ups.
Never changes weights by itself: --apply only works on a proposals file the user marked confirmed.

Usage:
    python3 weekly_review.py --state-dir inspiration/ [--memory memory.json] [--today 2026-11-02] --out-dir inspiration/reports
    python3 weekly_review.py --state-dir inspiration/ --apply inspiration/reports/proposals_2026-W45.json
    python3 weekly_review.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# Since v1.3 the learning parameters (alpha/beta/lambda/min_n/min_delta/floor/dimensions) and the event weights
# live in config.json -> "weights" (see insp-feedback/scripts/weights_config.py). Nothing numeric is hard-coded here.
_WC_DIR = Path(__file__).resolve().parents[2] / "insp-feedback" / "scripts"
if (_WC_DIR / "weights_config.py").is_file():
    sys.path.insert(0, str(_WC_DIR))
    import weights_config as WCFG  # noqa: E402
else:  # isolated skill copy (e.g. on the platform sandbox): minimal stand-in with the same defaults
    class WCFG:  # type: ignore[no-redef]
        DEFAULT_EVENTS = {"adopt": 1.0, "start_now": 1.0, "reject": -1.0, "done": -1.0, "not_important": -0.5,
                          "edited": 0.8, "ask_detail": 0.5, "read_no_action": -0.2, "unread": -0.1}
        DEFAULT_LEARNING = {"profile": "prod", "alpha": 2.0, "beta": 2.0, "lambda": 0.3, "min_n": 5, "min_delta": 0.01,
                            "floor": 0.1, "dimensions": ["type", "subfield"], "review_files": "weekly"}
        WeightConfigError = ValueError

        @staticmethod
        def load(p=None, state=None, quiet=False):
            cfg = json.loads(Path(p).read_text(encoding="utf-8-sig")) if p else {}
            w = cfg.get("weights") or {}
            le = w.get("learning") or (state or {}).get("learning") or {}
            return ({"events": dict(WCFG.DEFAULT_EVENTS, **w.get("events", {})), "learning": dict(WCFG.DEFAULT_LEARNING, **le)},
                    {"events": "config" if w else "default", "learning": "config" if w else "fallback", "config": p})

        @staticmethod
        def effective_weight(v, events):
            return float(events.get(v.get("action"), v.get("weight", 0.0)))
POSITIVE = {"adopt", "start_now", "edited"}


def d_of(ts: str) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()


def tokens(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", t.lower()) if len(w) > 1 or "\u4e00" <= w <= "\u9fff"}


def jacc(a: str, b: str) -> float:
    x, y = tokens(a), tokens(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def norm_id(x: str) -> str:
    return re.sub(r"v\d+$", "", str(x))


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.is_file() else []


def is_auto_memory(m: dict) -> bool:
    src = m.get("source")
    kind = src.get("kind") if isinstance(src, dict) else m.get("source_kind") or src
    return str(kind) == "auto_extraction"


def review(state_dir: Path, today: date, memory: list[dict] | None, config: Path | None = None) -> tuple[str, dict]:
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    weights = json.loads((state_dir / "taste" / "weights.json").read_text(encoding="utf-8"))
    verdicts = load_jsonl(state_dir / "verdicts.jsonl")
    week0 = today - timedelta(days=6)
    pushed = state.get("pushed", [])
    pushed_week = [p for p in pushed if p.get("pushed_at") and week0 <= d_of(p["pushed_at"]) <= today]
    v_week = [v for v in verdicts if week0 <= d_of(v["ts"]) <= today]
    # weak-beta G4: count a card as adopted only if its CURRENT verdict (after retracts) is positive.
    # v1.4.4 counted every positive row in the week, so "采纳 → Z 撤销 → 否决" still raised the adoption rate.
    cur: dict[str, list] = {}
    for v in verdicts:
        if v.get("action") == "retract":
            if cur.get(v["card_id"]):
                cur[v["card_id"]].pop()
        elif v.get("explicit"):
            cur.setdefault(v["card_id"], []).append(v)
    current = {k: st[-1] for k, st in cur.items() if st}
    adopted_week = {cid for cid, v in current.items() if v["action"] in POSITIVE and week0 <= d_of(v["ts"]) <= today}
    adoption = len(adopted_week) / len(pushed_week) if pushed_week else None

    two_weeks = [p for p in pushed if p.get("pushed_at") and today - timedelta(days=13) <= d_of(p["pushed_at"]) <= today]
    dup = 0
    for i, p in enumerate(two_weeks):
        if any(norm_id(p["id"]) == norm_id(q["id"]) or jacc(p.get("title", ""), q.get("title", "")) >= 0.6 for q in two_weeks[:i]):
            dup += 1
    repeat = dup / len(two_weeks) if two_weeks else None

    # Bayesian acceptance per feature value, on explicit verdicts (all time). Dimensions: type, subfield.
    block, wsrc = WCFG.load(config, state)
    learn, events = block["learning"], block["events"]
    alpha, beta = float(learn["alpha"]), float(learn["beta"])
    lam, min_n = float(learn["lambda"]), int(learn["min_n"])
    min_delta, floor = float(learn["min_delta"]), float(learn["floor"])
    dims = learn["dimensions"]
    card_sub = {}
    for cf in (state_dir / "cards").glob("*/cards.json"):
        for c in json.loads(cf.read_text(encoding="utf-8")).get("cards", []):
            card_sub[c["id"]] = c.get("subfield")
    stacks: dict[str, list] = {}
    for v in verdicts:  # one vote per card: its latest explicit verdict (e.g. ask_detail then adopt -> adopt)
        if v.get("action") == "retract":  # card-UI undo pops the latest explicit verdict
            if stacks.get(v["card_id"]):
                stacks[v["card_id"]].pop()
        elif v.get("explicit"):
            stacks.setdefault(v["card_id"], []).append(v)
    latest: dict[str, dict] = {k: st[-1] for k, st in stacks.items() if st}
    explicit = list(latest.values())
    feature_stats: dict[str, dict] = {}
    candidates = []
    for dim in dims:
        groups: dict[str, list[dict]] = defaultdict(list)
        for v in explicit:
            val = v.get(dim) if dim != "subfield" else (v.get("subfield") or card_sub.get(v["card_id"]))
            if val:
                groups[str(val)].append(v)
        feature_stats[dim] = {}
        for val, vs in sorted(groups.items()):
            n = len(vs)
            s_ = sum(min(1.0, max(0.0, WCFG.effective_weight(v, events))) for v in vs)
            p_hat = (s_ + alpha) / (n + alpha + beta)
            delta = round(lam * (p_hat - 0.5), 3)
            feature_stats[dim][val] = {"n": n, "s": round(s_, 2), "p_hat": round(p_hat, 3), "delta": delta,
                                       "eligible": n >= min_n, "missing": max(0, min_n - n)}
            if n >= min_n and abs(delta) >= min_delta:
                candidates.append((abs(delta), dim, val, delta))
    stats = feature_stats.get("type", {})
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
    proposal = None
    if candidates:
        _, dim, t, delta = candidates[0]
        cur = float(weights.get(dim, {}).get(t, 1.0))
        proposal = {"dimension": dim, "key": t, "from": cur, "to": round(max(floor, cur + delta), 3),
                    "evidence": feature_stats[dim][t], "params": {"alpha": alpha, "beta": beta, "lambda": lam, "min_n": min_n, "min_delta": min_delta,
                               "floor": floor, "source": wsrc["learning"], "event_weights_source": wsrc["events"]},
                    "weights_version_before": int(weights.get("version", 0)), "confirmed": False}

    # rubric diagnostics: which rules co-occur with rejection
    rule_rej: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for v in explicit:  # latest non-retracted explicit verdict per card
        if not v.get("explicit"):
            continue
        for r in v.get("rubric_rule_ids", []) or []:
            rule_rej[r][1] += 1
            if v["action"] in ("reject", "not_important"):
                rule_rej[r][0] += 1
    rule_diag = sorted(((r, a, b) for r, (a, b) in rule_rej.items() if b >= 3), key=lambda x: -x[1] / x[2])

    # implicit pattern (only to ask, never to apply)
    implicit = [v for v in v_week if not v.get("explicit") and v.get("action") != "retract"]

    mem_audit = [m for m in (memory or []) if is_auto_memory(m)]
    follow = [v for v in current.values() if v["action"] in POSITIVE and 85 <= (today - d_of(v["ts"])).days <= 95]

    iso = today.isocalendar()
    tag = f"{iso[0]}-W{iso[1]:02d}"
    # In the accelerated experiment several reviews happen in one ISO week: give each its own file.
    review_id = f"{tag}_{datetime.now().strftime('%m%d-%H%M')}" if learn.get("review_files") == "per_review" else tag
    L = [f"# 灵感机制周报 {tag}", "", f"统计区间：{week0} ～ {today}", ""]
    L += ["## 1. 两个核心指标", "", "| 指标 | 本周 | 健康区间 | 判断 |", "|---|---|---|---|"]
    if adoption is None:
        L.append("| 采纳率 | 本周无推送 | 20%–60% | — |")
    else:
        judge = "筛选太松" if adoption < 0.2 else ("推得太少，可放宽配额" if adoption > 0.6 else "正常")
        L.append(f"| 采纳率 | {adoption:.0%}（{len(adopted_week)}/{len(pushed_week)}） | 20%–60% | {judge} |")
    L.append(f"| 两周重复率 | {'—' if repeat is None else f'{repeat:.0%}（{dup}/{len(two_weeks)}）'} | ≈ 0 | {'需排查去重' if repeat else '正常'} |")
    L += ["", f"## 2. 各特征采纳率（贝叶斯平均，α={alpha:g}, β={beta:g}, λ={lam:g}, 样本门槛 n≥{min_n}）", "",
          f"参数来源：学习参数 {wsrc['learning']} · 事件权重 {wsrc['events']}（采纳率按当前配置里的事件权重重算）", "",
          "| 维度 | 取值 | n | p̂ | 建议 Δw | 当前权重 | 是否够样本 |", "|---|---|---|---|---|---|---|"]
    for dim, vals in feature_stats.items():
        for t, s in vals.items():
            cur = weights.get(dim, {}).get(t, 1.0)
            enough = "是" if s["eligible"] else f"否（还差 {s['missing']} 条，只记录）"
            L.append(f"| {dim} | {t} | {s['n']} | {s['p_hat']} | {s['delta']:+} | {cur} | {enough} |")
    L += ["", "## 3. 本周权重调整提议（一次只调一个维度）", ""]
    if proposal:
        L.append(f"- 提议：`{proposal['dimension']}={proposal['key']}` 权重 {proposal['from']} → **{proposal['to']}**（p̂={proposal['evidence']['p_hat']}, n={proposal['evidence']['n']}）")
        L.append(f"- 确认方式：把 `proposals_{review_id}.json` 里的 `confirmed` 改成 `true`，再运行 `weekly_review.py --apply`。不确认就不生效。")
    else:
        L.append(f"- 本周没有满足条件（n≥{min_n} 且 |Δ|≥{min_delta:g}）的调整。")
    L += ["", "## 4. 准则诊断（出现 ≥3 次的准则与否决的共现）", ""]
    L += [f"- {r}：{a}/{b} 次与否决/不重要同时出现" for r, a, b in rule_diag[:5]] or ["- 样本不足。"]
    L += ["", "## 5. 需要你确认的模式（隐式信号，只问不改）", ""]
    L.append(f"- 本周有 {len(implicit)} 条隐式信号（未读 / 已读无动作）。" + (" 是推送时间不合适，还是内容不相关？" if implicit else ""))
    L += ["", "## 6. 平台记忆审计（自动抽取的条目）", ""]
    if memory is None:
        L.append("- 本次未提供记忆导出（`--memory`），跳过。")
    elif not mem_audit:
        L.append("- 没有自动抽取的条目。")
    else:
        L.append("以下条目由平台自动抽取，可能把某次提示词里的临时约束固化成了长期偏好。请逐条决定 保留 / 改写为“条件→动作” / 删除：")
        L.append("")
        for m in mem_audit:
            L.append(f"- `{m.get('id', '?')}`：{str(m.get('content', ''))[:120]}")
    L += ["", "## 7. 三个月回访（验证“品味”而不是“偏好”）", ""]
    L += [f"- {v['card_id']}（{v['ts'][:10]} 采纳）：后来真的用上了吗？" for v in follow] or ["- 本周没有到期的回访。"]
    L += ["", f"## 8. 复活机制", "", f"- 当前计数 {state.get('revival', {}).get('counter', 0)}，每 {state.get('revival', {}).get('every', 20)} 次放行 1 次。"]
    # B1: 采纳后结果回填。与 verdict 完全隔离：只读 outcomes.jsonl 做聚合，不进第 1–3 节的 p̂ / 权重。
    # 这里不 import record_outcome：平台只挂载实际加载的技能，review 技能必须自包含。
    outcome_latest = {}
    for o in load_jsonl(state_dir / "outcomes.jsonl"):
        if o.get("card_id"):
            outcome_latest[o["card_id"]] = o
    oc = {k: 0 for k in ("confirmed", "refuted", "abandoned", "unknown")}
    for o in outcome_latest.values():
        if o.get("outcome") in oc:
            oc[o["outcome"]] += 1
    done_outcomes = oc["confirmed"] + oc["refuted"]
    outcome_summary = {"total": len(outcome_latest), "by_outcome": oc, "done": done_outcomes,
                       "confirmed_rate": round(oc["confirmed"] / done_outcomes, 3) if done_outcomes else None}
    L += ["", "## 9. 采纳后结果回填（B1）", "",
          f"- 已回填 {outcome_summary['total']} 张：结论成立 {oc['confirmed']} · 结论不成立 {oc['refuted']} · "
          f"没做 {oc['abandoned']} · 待定 {oc['unknown']}"]
    L.append(f"- 做了的里面，结论成立率：{outcome_summary['confirmed_rate']:.0%}（{oc['confirmed']}/{done_outcomes}）"
             if done_outcomes else "- 还没有做完的卡片，结论成立率暂不可算。")
    L += ["", "实测摘录（最多 3 条，来自你自己填写的结论）：", ""]
    excerpts = [o for o in outcome_latest.values() if o.get("conclusion")][:3]
    L += [f"- `{o['card_id']}`：{str(o['conclusion'])[:80]}" for o in excerpts] or ["- 暂无。"]
    proposals = {"week": tag, "review_id": review_id, "generated_for": today.isoformat(), "proposal": proposal,
                 "type_stats": stats, "feature_stats": feature_stats, "adopted_week_ids": sorted(adopted_week),
                 "memory_audit_ids": [m.get("id") for m in mem_audit], "outcome_summary": outcome_summary,
                 # B4: 供 verdict_summary.py 生成脱敏摘要（只取数值，不取 adopted_week_ids / memory_audit_ids）
                 "adoption": adoption, "repeat_rate": repeat, "implicit_week": len(implicit),
                 "weights_version": int(weights.get("version", 0))}
    return "\n".join(L) + "\n", proposals


SUMMARY_SCHEMA = "insp-verdict-summary/1.0"


def render_from_summary(s: dict) -> str:
    """B4 / K3：网页里只依据本机推送的脱敏摘要渲染周报。

    不读 verdicts.jsonl、不写文件、不提议应用权重；摘要缺失或格式不对时直接抛错，
    让技能据实回答「周报需要在本地运行」，而不是手算或编造数字。
    """
    if not isinstance(s, dict) or s.get("schema") != SUMMARY_SCHEMA:
        raise ValueError(f"不是有效的脱敏评价摘要（需要 {SUMMARY_SCHEMA}）")
    ad, rp = s.get("adoption"), s.get("repeat_rate")
    ad_text = "—（本周无推送）" if ad is None else f"{ad:.0%}"
    rp_text = "—" if rp is None else f"{rp:.0%}"
    prop = s.get("proposal") or {}
    L = ["# 灵感机制周报（网页版 · 脱敏摘要）", "",
         f"周次：{s.get('week')} · 生成于 {s.get('generated_at')}", "",
         "> 本页只依据本机推送的脱敏聚合摘要渲染；没有评价原文、卡片标题、引文和记忆内容。",
         "> 权重调整必须在本地确认后执行，网页里不会改任何权重或记忆。", "",
         "## 1. 两个核心指标", "", "| 指标 | 数值 | 健康区间 |", "|---|---|---|",
         f"| 采纳率 | {ad_text} | 20%–60% |",
         f"| 两周重复率 | {rp_text} | ≈ 0 |", "",
         "## 2. 各特征采纳率（贝叶斯平均）", "",
         "| 维度 | 取值 | n | p̂ | 建议 Δw | 够样本 |", "|---|---|---|---|---|---|"]
    for dim, vals in (s.get("feature_stats") or {}).items():
        for t, st in vals.items():
            L.append(f"| {dim} | {t} | {st.get('n')} | {st.get('p_hat')} | {st.get('delta')} | "
                     f"{'是' if st.get('eligible') else '否'} |")
    L += ["", "## 3. 本周权重调整提议（只展示，不生效）", ""]
    if prop:
        ev = prop.get("evidence") or {}
        L.append(f"- 提议：`{prop.get('dimension')}={prop.get('key')}` {prop.get('from')} → "
                 f"**{prop.get('to')}**（p̂={ev.get('p_hat')}, n={ev.get('n')}）。未确认，网页不会应用。")
    else:
        L.append("- 本周没有满足门槛的调整。")
    L += ["", "## 4. 隐式信号与采纳后结果", "",
          f"- 本周隐式信号 {s.get('implicit_week', 0)} 条（只问不改）。"]
    oc = s.get("outcome_summary") or {}
    if oc.get("total"):
        by = oc.get("by_outcome") or {}
        L.append(f"- 已采纳建议回填 {oc['total']} 张：成立 {by.get('confirmed', 0)} · 不成立 {by.get('refuted', 0)} · "
                 f"没做 {by.get('abandoned', 0)} · 待定 {by.get('unknown', 0)}；结论成立率 {oc.get('confirmed_rate')}。")
    else:
        L.append("- 还没有采纳后结果回填。")
    L += ["", "## 5. 边界", "",
          "- 没有摘要时应直接说明「周报需要在本地运行」，不得手算、不得编造数字。",
          "- 记忆审计、权重应用、记忆删除都不在网页里做。"]
    return "\n".join(L) + "\n"


def apply(state_dir: Path, proposals_file: Path) -> str:
    p = json.loads(proposals_file.read_text(encoding="utf-8"))
    prop = p.get("proposal")
    if not prop:
        return "NOTHING: 提议为空"
    if prop.get("confirmed") is not True:
        return "REFUSED: 提议未被确认（confirmed 不是 true），不修改权重"
    wf = state_dir / "taste" / "weights.json"
    w = json.loads(wf.read_text(encoding="utf-8"))
    rid = p.get("review_id", p["week"])
    if any(h.get("review_id", h.get("week")) == rid and not h.get("rollback") for h in w.get("history", [])):
        return f"REFUSED: 提议 {rid} 已经应用过"
    cur = float(w.get(prop["dimension"], {}).get(prop["key"], 1.0))
    if abs(cur - float(prop["from"])) > 1e-9:
        return f"REFUSED: 当前权重 {cur} 与提议的起点 {prop['from']} 不一致（权重在提议之后被改过），请重新生成提议"
    shutil.copy(wf, wf.with_name(f"weights.v{int(w.get('version', 0))}.json"))  # snapshot for replay / rollback
    w.setdefault(prop["dimension"], {})[prop["key"]] = prop["to"]
    w["version"] = int(w.get("version", 0)) + 1
    w["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    w.setdefault("history", []).append({"week": p["week"], "review_id": rid, "applied_at": w["updated_at"],
                                        "version": w["version"], **prop})
    wf.write_text(json.dumps(w, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"APPLIED: {prop['dimension']}.{prop['key']} {prop['from']} → {prop['to']}（weights v{w['version']}）"


def rollback(state_dir: Path) -> str:
    """Undo the latest applied change (the user can always overrule the system)."""
    wf = state_dir / "taste" / "weights.json"
    w = json.loads(wf.read_text(encoding="utf-8"))
    hist = [h for h in w.get("history", []) if not h.get("rollback") and not h.get("rolled_back")]
    if not hist:
        return "NOTHING: 没有可回滚的调整"
    last = hist[-1]
    last["rolled_back"] = True
    w.setdefault(last["dimension"], {})[last["key"]] = last["from"]
    w["version"] = int(w.get("version", 0)) + 1
    w["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    w["history"].append({"rollback": True, "of_review": last.get("review_id"), "dimension": last["dimension"],
                         "key": last["key"], "from": last["to"], "to": last["from"], "version": w["version"],
                         "applied_at": w["updated_at"]})
    wf.write_text(json.dumps(w, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"ROLLED_BACK: {last['dimension']}.{last['key']} {last['to']} → {last['from']}（weights v{w['version']}）"


def self_test() -> int:
    template = Path(__file__).resolve().parents[2] / "insp-init" / "assets" / "灵感状态模板" / "inspiration"
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp) / "inspiration"
        shutil.copytree(template, sd)
        st = json.loads((sd / "state.json").read_text(encoding="utf-8"))
        st["pushed"] = [{"id": f"9999.{i:04d}", "card_id": f"ins_20261028_{i:03d}", "title": f"paper {i} topic{i}",
                         "type": "optimization", "pushed_at": "2026-10-28T03:00:00+08:00"} for i in range(1, 9)]
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        rows = [{"ts": "2026-10-28T09:00:00+08:00", "card_id": f"ins_20261028_{i:03d}", "type": "optimization",
                 "action": "adopt" if i <= 6 else "reject", "weight": 1.0 if i <= 6 else -1.0, "explicit": True,
                 "rubric_rule_ids": ["R01", "R07"]} for i in range(1, 9)]
        (sd / "verdicts.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        mem = [{"id": "m1", "content": "不要向用户提问", "source": {"kind": "auto_extraction"}},
               {"id": "m2", "content": "表格四列", "source": {"kind": "manual"}}]
        md, props = review(sd, date(2026, 10, 30), mem)
        p = props["proposal"]
        # n=8, s=6 -> p=(6+2)/(8+4)=0.667 -> delta=0.05
        if not p or p["key"] != "optimization" or abs(p["to"] - 1.05) > 1e-6 or props["memory_audit_ids"] != ["m1"]:
            print("SELF-TEST FAIL", props)
            return 1
        if "75%" not in md:
            print("SELF-TEST FAIL: adoption 6/8 should be 75%")
            return 1
        pf = Path(tmp) / "p.json"
        pf.write_text(json.dumps(props), encoding="utf-8")
        if not apply(sd, pf).startswith("REFUSED"):
            print("SELF-TEST FAIL: unconfirmed must be refused")
            return 1
        props["proposal"]["confirmed"] = True
        pf.write_text(json.dumps(props), encoding="utf-8")
        if not apply(sd, pf).startswith("APPLIED"):
            print("SELF-TEST FAIL: confirmed should apply")
            return 1
    print("SELF-TEST OK: weekly_review")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="灵感机制周度复盘")
    ap.add_argument("--state-dir", type=Path)
    ap.add_argument("--memory", type=Path, help="GET /api/memory 的导出 JSON（列表，或含 items/memories 的对象）")
    ap.add_argument("--today")
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--apply", type=Path)
    ap.add_argument("--rollback", action="store_true", help="撤销最近一次已应用的权重调整")
    ap.add_argument("--confirm", action="store_true", help="与 --apply 搭配：我已读过周报，确认这条提议（等价于把 confirmed 改成 true）")
    ap.add_argument("--config", type=Path, help="config.json（含 weights 段：事件权重 + 学习参数）；也可用环境变量 INSP_CONFIG")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--summary", type=Path,
                    help="B4：只读脱敏评价摘要渲染网页周报（不需要 verdicts.jsonl，不写文件、不改权重）")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.summary:
        try:
            print(render_from_summary(json.loads(a.summary.read_text(encoding="utf-8"))), end="")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return 0
    if not a.state_dir:
        ap.error("需要 --state-dir")
    if a.rollback:
        msg = rollback(a.state_dir)
        print(msg)
        return 0
    if a.apply:
        if a.confirm:
            pj = json.loads(a.apply.read_text(encoding="utf-8"))
            if pj.get("proposal"):
                pj["proposal"]["confirmed"] = True
                pj["proposal"]["confirmed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                a.apply.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
        msg = apply(a.state_dir, a.apply)
        print(msg)
        return 0 if msg.startswith(("APPLIED", "NOTHING")) else 1
    mem = None
    if a.memory:
        raw = json.loads(a.memory.read_text(encoding="utf-8"))
        mem = raw if isinstance(raw, list) else raw.get("items") or raw.get("memories") or raw.get("data") or []
    today = date.fromisoformat(a.today) if a.today else date.today()
    try:
        md, props = review(a.state_dir, today, mem, a.config)
    except WCFG.WeightConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    out = a.out_dir or (a.state_dir / "reports")
    out.mkdir(parents=True, exist_ok=True)
    rid = props.get("review_id", props["week"])
    (out / f"weekly_{rid}.md").write_text(md, encoding="utf-8")
    (out / f"proposals_{rid}.json").write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {out}/weekly_{rid}.md")
    print(f"PROPOSAL: {json.dumps(props['proposal'], ensure_ascii=False) if props['proposal'] else '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
