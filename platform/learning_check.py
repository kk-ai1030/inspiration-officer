#!/usr/bin/env python3
"""Learning-loop acceptance check + daily progress dashboard (independent of weekly_review.py's own math).

Usage (from the package root):
  python platform/learning_check.py --config config.json                       # progress + all checks
  python platform/learning_check.py --config config.json --expected expected.json
  python platform/learning_check.py --config config.json --replay runs/run-XXXX --weights inspiration/taste/weights.v0.json

Input : inspiration/{state.json, verdicts.jsonl, taste/weights.json, taste/weights.v*.json, cards/*/cards.json,
        runs.jsonl, audit.jsonl, reports/proposals_*.json}, runs/<run_id>/{triage.json, selection.json}
        expected.json (optional) = the preference you committed to BEFORE judging, e.g.
        {"type.hypothesis": "up", "type.process": "down"}
Output: inspiration/reports/learning_check_<ts>.md ; last line of stdout = OVERALL: PROGRESS | PASS | FAIL
Exit  : 0 PASS · 1 FAIL · 2 PROGRESS (not enough data yet, nothing wrong)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


V_CARDS = _load("insp-card/scripts/validate_cards.py", "v_cards_lc")
WCFG = _load("insp-feedback/scripts/weights_config.py", "wcfg_lc")


def jl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def replay_selection(run_dir: Path, weights: dict, state: dict) -> tuple[list[str], list[str]]:
    """Re-run the deterministic selection of one run with other weights. Returns (actual_kept, replay_kept)."""
    sel = json.loads((run_dir / "selection.json").read_text(encoding="utf-8"))
    cards = [{"id": r["card_id"], "type": r["type"], "subfield": r["subfield"], "source": {"ref": ""},
              "rubric_scores": [{"score": r["avg_rubric"]}]} for r in sel["log"]]
    st = json.loads(json.dumps(state))
    st["quota_used"] = {}  # same (empty) quota context for both arms -> fair comparison
    actual = sorted(r["card_id"] for r in sel["log"] if r["kept"])
    kept, _ = V_CARDS.select(cards, st, weights, {}, date.today())
    return actual, sorted(c["id"] for c in kept)


def replay_shortlist(run_dir: Path, weights: dict, max_short: int) -> tuple[list[str], list[str]]:
    tri = json.loads((run_dir / "triage.json").read_text(encoding="utf-8"))
    ranked = tri.get("ranked") or []
    actual = sorted(r["id"] for r in ranked if r.get("shortlisted"))
    ws = weights.get("subfield", {}) or {}
    alt = sorted(ranked, key=lambda r: (-(r["total"] * float(ws.get(r["subfield"], 1.0))), r["id"]))[:max_short]
    return actual, sorted(r["id"] for r in alt)


def main() -> int:
    ap = argparse.ArgumentParser(description="学习闭环验收 + 进度看板")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--expected", type=Path)
    ap.add_argument("--replay", type=Path, help="单次运行目录（runs/run-...）")
    ap.add_argument("--weights", type=Path, help="与 --replay 搭配：用这份权重重放")
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    base = a.config.resolve().parent
    sd = (base / cfg["state_dir"]).resolve()
    wd = (base / cfg.get("work_dir", "runs")).resolve()
    state = json.loads((sd / "state.json").read_text(encoding="utf-8"))
    weights = json.loads((sd / "taste" / "weights.json").read_text(encoding="utf-8"))

    if a.replay:
        alt_w = json.loads(a.weights.read_text(encoding="utf-8")) if a.weights else weights
        act, rep = replay_selection(a.replay, alt_w, state)
        print("实际入选:", act)
        print("重放入选:", rep)
        print("差异:", sorted(set(act) ^ set(rep)) or "无")
        return 0

    try:
        block, wsrc = WCFG.load(a.config.resolve(), state)
    except WCFG.WeightConfigError as exc:
        print(f"ERROR: {exc}")
        return 1
    learn, events = block["learning"], block["events"]
    alpha, beta = float(learn["alpha"]), float(learn["beta"])
    lam, min_n = float(learn["lambda"]), int(learn["min_n"])
    min_delta = float(learn["min_delta"])
    dims = learn["dimensions"]

    cards = {}
    for cf in sorted((sd / "cards").glob("*/cards.json")):
        for c in json.loads(cf.read_text(encoding="utf-8")).get("cards", []):
            cards[c["id"]] = c
    verdicts = jl(sd / "verdicts.jsonl")
    stacks = {}
    for v in verdicts:  # one vote per card: its latest explicit verdict (e.g. ask_detail then adopt -> adopt)
        if v.get("action") == "retract":  # card-UI undo pops the latest explicit verdict
            if stacks.get(v["card_id"]):
                stacks[v["card_id"]].pop()
        elif v.get("explicit"):
            stacks.setdefault(v["card_id"], []).append(v)
    latest = {k: st[-1] for k, st in stacks.items() if st}
    runs = jl(sd / "runs.jsonl")
    audit = {r["run_id"]: r for r in jl(sd / "audit.jsonl")}
    hist = weights.get("history", [])
    applied = [h for h in hist if not h.get("rollback")]
    first_apply = ts(applied[0]["applied_at"]) if applied and applied[0].get("applied_at") else None

    checks: list[tuple[str, str, str]] = []  # (id, PASS/FAIL/WAIT/INFO, detail)
    L = [f"# 学习闭环验收报告 · {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
         f"参数：α={alpha:g} β={beta:g} λ={lam:g} 样本门槛 n≥{min_n}（来源 {wsrc['learning']}；事件权重 {wsrc['events']}） · 配置档 {state.get('quota_profile', 'prod')} · 当前权重 v{weights.get('version', 0)}", ""]

    # ---------- progress ----------
    stats: dict[str, dict[str, dict]] = {}
    for dim in dims:
        g: dict[str, list[dict]] = defaultdict(list)
        for v in latest.values():
            val = v.get(dim) or (cards.get(v["card_id"], {}).get(dim) if dim == "subfield" else None)
            if val:
                g[str(val)].append(v)
        stats[dim] = {}
        for val, vs in sorted(g.items()):
            n = len(vs)
            s = sum(min(1.0, max(0.0, WCFG.effective_weight(x, events))) for x in vs)
            p = (s + alpha) / (n + alpha + beta)
            stats[dim][val] = {"n": n, "s": round(s, 2), "p_hat": round(p, 3), "delta": round(lam * (p - 0.5), 3)}
    pending = [cid for cid in cards if cid not in latest]
    L += ["## 1. 进度：每个特征的显式评价数", "", f"卡片总数 {len(cards)} · 已评价 {len(latest)} · **待评价 {len(pending)}**"
          + ("（打开 inspiration/inbox/verdict_inbox.csv 填 action 列）" if pending else ""), "",
          "| 维度 | 取值 | 已推送卡片 | 已评价 n | 还差 | p̂ | Δw（独立重算） |", "|---|---|---|---|---|---|---|"]
    pushed_by = {d: Counter(str(c.get(d) or "-") for c in cards.values()) for d in dims}
    for dim in dims:
        vals = sorted(set(pushed_by[dim]) | set(stats[dim]))
        for val in vals:
            s = stats[dim].get(val, {"n": 0, "p_hat": "-", "delta": "-"})
            L.append(f"| {dim} | {val} | {pushed_by[dim].get(val, 0)} | {s['n']} | {max(0, min_n - s['n'])} | {s['p_hat']} | {s['delta']} |")
    eligible = {(d, v) for d in dims for v, s in stats[d].items() if s["n"] >= min_n}
    type_vals = set(state.get("quota_test", {}).get("target_types", ["knowledge", "hypothesis", "process"]))
    type_ready = all(stats.get("type", {}).get(t, {}).get("n", 0) >= min_n for t in type_vals)
    checks.append(("P1", "PASS" if type_ready else "WAIT",
                   f"目标类型 {sorted(type_vals)} 是否都达到 n≥{min_n}" + ("" if type_ready else "；不够就补跑一次 run_local 并评价")))

    # ---------- L1 formula ----------
    props = sorted((sd / "reports").glob("proposals_*.json"), key=lambda p: p.stat().st_mtime)
    last_prop = json.loads(props[-1].read_text(encoding="utf-8")) if props else None
    if last_prop and last_prop.get("feature_stats"):
        bad = []
        for dim, vals in last_prop["feature_stats"].items():
            for val, s in vals.items():
                mine = stats.get(dim, {}).get(val)
                # proposals may be older than the latest verdicts: only compare when n matches
                if mine and mine["n"] == s["n"] and (abs(mine["p_hat"] - s["p_hat"]) > 1e-3 or abs(mine["delta"] - s["delta"]) > 1e-3):
                    bad.append(f"{dim}.{val}")
        checks.append(("L1", "FAIL" if bad else "PASS", f"周报的 p̂/Δ 与独立重算一致（{props[-1].name}）" + (f"；不一致：{bad}" if bad else "")))
    else:
        checks.append(("L1", "WAIT", "还没有 proposals_*.json：样本够了以后运行 run_weekly.py"))

    # ---------- L2 discipline ----------
    if last_prop:
        pr = last_prop.get("proposal")
        fs = last_prop.get("feature_stats", {})
        elig = [(abs(s["delta"]), d, v) for d, vals in fs.items() for v, s in vals.items() if s["n"] >= min_n and abs(s["delta"]) >= min_delta]
        if pr is None:
            checks.append(("L2", "PASS" if not elig else "FAIL", "没有提议" + ("（正确：没有够样本的特征）" if not elig else "，但存在够样本的特征")))
        else:
            ok_n = fs.get(pr["dimension"], {}).get(pr["key"], {}).get("n", 0) >= min_n
            ok_max = bool(elig) and max(elig)[0] <= abs(pr["to"] - pr["from"]) + 1e-3
            checks.append(("L2", "PASS" if ok_n and ok_max else "FAIL",
                           f"只提议 1 个维度、样本够（n≥{min_n}）、且是 |Δ| 最大的：{pr['dimension']}.{pr['key']} {pr['from']}→{pr['to']}"))

    # ---------- L3 apply ----------
    if applied:
        probs = []
        for h in applied:
            if h.get("confirmed") is not True:
                probs.append(f"{h.get('review_id')} 未确认却被应用")
        live = {}
        for h in hist:
            live[(h["dimension"], h["key"])] = h["to"]
        for (d, k), to in live.items():
            if abs(float(weights.get(d, {}).get(k, 1.0)) - float(to)) > 1e-9:
                probs.append(f"{d}.{k} 当前值与历史记录不符")
        if int(weights.get("version", 0)) != len(hist):
            probs.append(f"version={weights.get('version')} 与历史条数 {len(hist)} 不符")
        checks.append(("L3", "FAIL" if probs else "PASS", "调整须经确认、写入历史、版本号连续" + (f"；问题：{probs}" if probs else "")))
    else:
        checks.append(("L3", "WAIT", "还没有应用过任何调整"))

    # ---------- L4 effect ----------
    after = [r for r in runs if r.get("status") == "OK" and first_apply and ts(r["started_at"]) > first_apply]
    post = [r for r in runs if r.get("status") == "OK" and int(r.get("weights_version", 0) or 0) >= 1]
    if first_apply and post:
        wiring = [r["run_id"] for r in after if int(r.get("weights_version", -1) or -1) < 1]
        prev_w_file = sd / "taste" / f"weights.v{int(applied[0].get('version', 1)) - 1}.json"
        prev_w = json.loads(prev_w_file.read_text(encoding="utf-8")) if prev_w_file.exists() else None
        diffs, lines = 0, []
        max_short = int(state.get("triage", {}).get("max_shortlist", 8))
        for r in post:
            rd = wd / r["run_id"]
            if prev_w is None or not (rd / "selection.json").exists():
                continue
            act, rep = replay_selection(rd, prev_w, state)
            sh_a, sh_r = replay_shortlist(rd, prev_w, max_short) if (rd / "triage.json").exists() else ([], [])
            d = sorted(set(act) ^ set(rep))
            ds = sorted(set(sh_a) ^ set(sh_r))
            diffs += bool(d or ds)
            lines.append(f"| {r['run_id']} | v{r.get('weights_version')} | {len(d)} | {len(ds)} | {', '.join(d) or '—'} |")
        checks.append(("L4a", "FAIL" if wiring else "PASS", "调整之后的运行都用了新权重（runs.jsonl.weights_version）" + (f"；未用：{wiring}" if wiring else "")))
        checks.append(("L4b", "PASS" if diffs else "WAIT",
                       f"反事实重放：同样的候选用旧权重 {prev_w_file.name} 重选，{diffs}/{len(lines)} 次运行的结果不同"
                       + ("" if diffs else "（还没观测到效果：被调的特征可能没有参与名额竞争，再跑几次）")))
        L += ["", "## 反事实重放明细（新权重实际入选 vs 旧权重重放）", "",
              "| 运行 | 权重版本 | 卡片入选差异数 | 预筛名单差异数 | 差异卡片 |", "|---|---|---|---|---|", *lines]
        # floor: down-weighted value still appears
        down = [h for h in applied if h["to"] < h["from"]]
        for h in down:
            seen_after = sum(1 for c in cards.values() if c.get(h["dimension"]) == h["key"]
                             and int((c.get("_trace") or {}).get("weights_version", 0)) >= int(h.get("version", 1)))
            checks.append(("L5", "PASS" if seen_after else "WAIT", f"被调低的 {h['dimension']}.{h['key']} 调低后仍出现 {seen_after} 次（不被判死刑）"))
        # descriptive shift
        before = Counter(c["type"] for c in cards.values() if int((c.get("_trace") or {}).get("weights_version", 0)) == 0)
        after = Counter(c["type"] for c in cards.values() if int((c.get("_trace") or {}).get("weights_version", 0)) >= 1)
        L += ["", "## 类型分布（描述性，样本小，不做显著性推断）", "", "| 类型 | 调整前 | 调整后 |", "|---|---|---|"]
        for t in sorted(set(before) | set(after)):
            L.append(f"| {t} | {before.get(t, 0)}（{before.get(t, 0) / max(1, sum(before.values())):.0%}） | "
                     f"{after.get(t, 0)}（{after.get(t, 0) / max(1, sum(after.values())):.0%}） |")
    elif first_apply:
        checks.append(("L4", "WAIT", "已应用调整，等待之后的运行"))
    else:
        checks.append(("L4", "WAIT", "还没有应用过调整"))

    # ---------- L6 expected direction ----------
    if a.expected and a.expected.exists():
        exp = json.loads(a.expected.read_text(encoding="utf-8"))
        for key, direction in exp.items():
            d, v = key.split(".", 1)
            s = stats.get(d, {}).get(v)
            if not s or s["n"] < min_n:
                checks.append(("L6", "WAIT", f"{key} 期望 {direction}：样本不足（n={s['n'] if s else 0}）"))
                continue
            got = "up" if s["delta"] > 0 else ("down" if s["delta"] < 0 else "flat")
            checks.append(("L6", "PASS" if got == direction else "FAIL", f"{key} 期望 {direction}，系统学到 {got}（Δ={s['delta']:+}）"))

    # ---------- L7 skill evidence ----------
    ok_runs = [r for r in runs if r.get("status") == "OK"]
    dist = Counter((audit.get(r["run_id"], {}).get("skill_loaded") or r.get("skill_loaded") or "?") for r in ok_runs)
    checks.append(("L7", "FAIL" if dist.get("false") else "INFO",
                   f"技能加载证据（含 run_audit 回填）：{dict(dist)}；unverified 用 run_audit.py 回填"))

    L += ["", "## 2. 检查项", "", "| 编号 | 结果 | 说明 |", "|---|---|---|"] + [f"| {c} | **{r}** | {d} |" for c, r, d in checks]
    overall = "FAIL" if any(r == "FAIL" for _, r, _ in checks) else ("PASS" if all(r in ("PASS", "INFO") for _, r, _ in checks) else "PROGRESS")
    L += ["", f"## 结论：**{overall}**", "",
          "- PROGRESS：数据还不够，没有发现错误，按计划继续。", "- PASS：闭环全部验证通过。", "- FAIL：有检查失败，看上表说明。"]
    out = sd / "reports"
    out.mkdir(exist_ok=True)
    f = out / f"learning_check_{datetime.now().strftime('%m%d-%H%M%S')}.md"
    f.write_text("\n".join(L) + "\n", encoding="utf-8")
    for c, r, d in checks:
        print(f"{c:4s} {r:8s} {d}")
    print(f"报告：{f}")
    print(f"OVERALL: {overall}")
    return {"PASS": 0, "FAIL": 1}.get(overall, 2)


if __name__ == "__main__":
    sys.exit(main())
