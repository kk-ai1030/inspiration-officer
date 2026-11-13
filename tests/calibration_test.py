#!/usr/bin/env python3
"""③ 真实结果 → 独立校准提议：离线回归（零联网）。

核心验证：**两套标签隔离** —— outcomes.jsonl 的增删不改变周报第 1–3 节的 p̂ / 权重提议；
校准提议恒未确认，只有本机 --confirm 才生效，且生效后不改 weights.json / 排序。
另外覆盖脱敏摘要、网页只读渲染、飞轮观测台、MCP 只读工具与技能契约。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK = True
DAY = date(2026, 11, 13)
SECRETS = ("理由绝密", "结论绝密", "标题绝密", "引文绝密", "ins_cal_", "9999.", "@")


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:300]) if detail and not cond else ""))
    OK &= bool(cond)


def seed(sd: Path) -> None:
    shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
    (sd / "cards" / "2026-11-13").mkdir(parents=True, exist_ok=True)
    cards = [{"id": f"ins_cal_{i}", "type": "knowledge", "subfield": "memory", "title": "标题绝密",
              "source": {"ref": f"9999.{i}", "quote": "引文绝密"}, "status": "proposed", "verdict": None}
             for i in range(1, 7)]
    (sd / "cards" / "2026-11-13" / "cards.json").write_text(
        json.dumps({"date": "2026-11-13", "cards": cards}, ensure_ascii=False), encoding="utf-8")
    verdicts = [{"ts": "2026-11-13T09:00:00+08:00", "card_id": c["id"], "type": "knowledge",
                 "subfield": "memory", "action": "adopt", "label": "采纳", "weight": 1.0,
                 "explicit": True, "reason": "理由绝密", "rubric_rule_ids": [], "weights_version": 0}
                for c in cards]
    (sd / "verdicts.jsonl").write_text("\n".join(json.dumps(v, ensure_ascii=False) for v in verdicts) + "\n",
                                       encoding="utf-8")
    outcomes = [{"ts": "2026-11-13T12:00:00+08:00", "card_id": c["id"],
                 "outcome": "confirmed" if i < 2 else "refuted", "label": "做了",
                 "conclusion": "结论绝密", "measured": "3%", "via": "cli"}
                for i, c in enumerate(cards)]
    (sd / "outcomes.jsonl").write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in outcomes) + "\n",
                                       encoding="utf-8")


def snapshot(sd: Path) -> dict:
    return {p.relative_to(sd).as_posix(): p.read_bytes() for p in sd.rglob("*") if p.is_file()}


def cli(sd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "platform/calibration.py"),
                           "--state-dir", str(sd), *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=30)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_cal_test_"))
    try:
        sd = tmp / "inspiration"
        seed(sd)
        cal = load("platform/calibration.py", "cal_test_calibration")
        wr = load("insp-review/scripts/weekly_review.py", "cal_test_weekly")
        vs = load("platform/verdict_summary.py", "cal_test_vsummary")
        fw = load("mcp/flywheel.py", "cal_test_flywheel")
        mcp = load("mcp/inspiration_mcp.py", "cal_test_mcp")

        print("== C-0 模块自测")
        check("C-0 calibration 自测", cal.self_test() == 0)

        print("== C-1 两套标签分开")
        d = cal.build(sd, None, DAY)
        check("C-1 采纳率与结论成立率分别计算", d["n"] == 6 and d["confirmed"] == 2
              and d["confirmed_rate"] == 0.333 and d["adoption_rate"] == 1.0, json.dumps(d, ensure_ascii=False)[:300])
        check("C-1 提议是写卡提示而非权重", d["proposal"] and d["proposal"]["kind"] == "prompt_hint"
              and d["proposal"]["confirmed"] is False and d["proposal"]["direction"] == "strengthen_evidence"
              and "from" not in d["proposal"] and "to" not in d["proposal"])
        check("C-1 提议不含用户原文", not any(x in d["proposal"]["hint"] for x in SECRETS))

        print("== C-2 关键隔离：outcomes 不影响 p̂ / 权重提议")
        with_o, props_o = wr.review(sd, DAY, None, None)
        raw = (sd / "outcomes.jsonl").read_bytes()
        (sd / "outcomes.jsonl").unlink()
        try:
            _, props_w = wr.review(sd, DAY, None, None)
        finally:
            (sd / "outcomes.jsonl").write_bytes(raw)
        check("C-2 有/无回填时 p̂ 完全一致", props_o["feature_stats"] == props_w["feature_stats"])
        check("C-2 有/无回填时权重提议完全一致", props_o["proposal"] == props_w["proposal"])
        prop = props_o["proposal"] or {}
        check("C-2 权重提议仍只来自评价", prop and prop["dimension"] in ("type", "subfield")
              and prop["evidence"]["n"] == 6 and "p_hat" in prop["evidence"]
              and "confirmed_rate" not in json.dumps(prop, ensure_ascii=False), json.dumps(prop, ensure_ascii=False))

        print("== C-3 周报第 10 节")
        check("C-3 第 10 节展示两套标签", "## 10. 真实结果校准" in with_o
              and "不进第 1–3 节" in with_o and "33%" in with_o and "100%" in with_o, with_o[-600:])
        obs = props_o["calibration_observation"]["knowledge"]
        check("C-3 proposals 带校准观测", obs["n"] == 6 and obs["confirmed"] == 2
              and obs["confirmed_rate"] == 0.333 and obs["gap"] == 0.667)

        print("== C-4 脱敏摘要")
        s = vs.build(sd, None, DAY)
        c = s["calibration"]
        text = json.dumps(s, ensure_ascii=False)
        check("C-4 摘要带校准聚合数值", c["n"] == 6 and c["confirmed_rate"] == 0.333
              and c["by_type"]["knowledge"]["n"] == 6)
        check("C-4 摘要里的校准提议恒未确认", c["proposal"]["confirmed"] is False
              and c["applied"] is None)
        check("C-4 自由文本 subfield 不进摘要", "by_subfield" not in c)
        check("C-4 摘要不含评价 / 结果 / 卡片原文", not any(x in text for x in SECRETS), text[:300])
        md = wr.render_from_summary(s)
        check("C-4 网页只读渲染含校准", "真实结果校准" in md and "仅写卡提示" in md
              and not any(x in md for x in SECRETS), md[-400:])

        print("== C-5 飞轮观测台")
        before = snapshot(sd)
        f = fw.build(sd, None, DAY)
        check("C-5 飞轮聚合校准信号", f["signals"]["calibration"]["n"] == 6
              and f["flywheel"]["calibration_proposal"]["confirmed"] is False
              and any("仍不参与" in x for x in f["flywheel"]["not_consumed"]))
        check("C-5 飞轮完全只读", snapshot(sd) == before)
        check("C-5 飞轮不含用户原文", not any(x in json.dumps(f, ensure_ascii=False) for x in SECRETS))

        print("== C-6 MCP 只读工具")
        ctx = mcp.Ctx(sd, None, True)
        r = mcp.call_tool("insp_calibration", {}, ctx)
        t = r["content"][0]["text"]
        check("C-6 read-only 下校准可用且不是写工具", r["isError"] is False
              and "insp-calibration/1.0" in t and "insp_calibration" not in mcp.WRITE_TOOLS, t[:250])
        ro_names = {x["name"] for x in mcp.tool_defs(ctx)}
        check("C-6 read-only 仍列出校准工具", "insp_calibration" in ro_names
              and mcp.WRITE_TOOLS.isdisjoint(ro_names))

        print("== C-7 CLI：只有 --confirm 才生效")
        weights_before = (sd / "taste/weights.json").read_bytes()
        r1 = cli(sd, "--today", DAY.isoformat(), "--write")
        pf = sd / "calibration.json"
        check("C-7 --write 生成未确认提议", r1.returncode == 0 and pf.is_file()
              and json.loads(pf.read_text(encoding="utf-8"))["proposal"]["confirmed"] is False,
              r1.stdout + r1.stderr)
        r2 = cli(sd, "--apply", str(pf))
        check("C-7 不带 --confirm 被拒", r2.returncode == 1 and "REFUSED" in r2.stdout
              and not (sd / "taste/calibration.json").exists(), r2.stdout + r2.stderr)
        r3 = cli(sd, "--apply", str(pf), "--confirm")
        check("C-7 --confirm 才生成写卡提示", r3.returncode == 0 and "APPLIED" in r3.stdout
              and (sd / "taste/calibration.json").is_file(), r3.stdout + r3.stderr)
        check("C-7 权重文件逐字节不变", (sd / "taste/weights.json").read_bytes() == weights_before)
        check("C-7 评价 / 回填 / 卡片不变", (sd / "verdicts.jsonl").read_text(encoding="utf-8").count("理由绝密") == 6
              and (sd / "outcomes.jsonl").read_text(encoding="utf-8").count("结论绝密") == 6)
        hint = cal.active_hint(sd)
        check("C-7 生效提示可读且不含原文", hint and hint["key"] == "knowledge"
              and not any(x in hint["hint"] for x in SECRETS))
        s2 = vs.build(sd, None, DAY)
        check("C-7 生效提示进脱敏摘要", s2["calibration"]["applied"]["key"] == "knowledge"
              and not any(x in json.dumps(s2, ensure_ascii=False) for x in SECRETS))
        r4 = cli(sd, "--rollback")
        check("C-7 可撤销", r4.returncode == 0 and "ROLLED_BACK" in r4.stdout and cal.active_hint(sd) is None)

        print("== C-8 契约文档")
        contract = (ROOT / "评价读写约定.md").read_text(encoding="utf-8")
        card_skill = (ROOT / "insp-card/SKILL.md").read_text(encoding="utf-8")
        dev = (ROOT / "docs/开发文档.md").read_text(encoding="utf-8")
        check("C-8 读写约定写明两套标签隔离", "## 8." in contract and "两套标签分开" in contract
              and "不写" in contract)
        check("C-8 写卡技能只当提示词证据", "结果校准提示" in card_skill
              and "calibration.applied" in card_skill and "不得" in card_skill)
        check("C-8 开发文档有 §15", "## 15." in dev and "采纳率 ≠ 结论成立率" in dev)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("CALIBRATION OK" if OK else "CALIBRATION TESTS FAILED")
    return 0 if OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
