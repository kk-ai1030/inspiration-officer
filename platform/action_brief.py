#!/usr/bin/env python3
"""v1.5.1 “动手单”（action brief）：把一张卡片变成可以直接开工的任务说明。

在评价页按 6（立刻动手）之后，任务不再只拿卡片的几句话去跑，而是先整理成 7 栏的动手单：

    场景：<研究对象 + 子领域>
    参数：<N 项，带数值与单位，必须标注来源 library / observed / user>
    方法：<建立什么模型 / 用什么手段求解>
    产出：<可量化的指标或可观测现象>
    对照：<基线是什么>
    判据：<产出与基线满足什么关系才算成立>
    交付：<文件 / 脚本 / 文档>

来源有两种：
  1. 卡片自带 action_brief（灵感官出卡时写，validate_cards 已校验过参数来源）→ 原样使用；
  2. 卡片没有（旧卡片、或校验没过被剥掉）→ 按卡片字段 + 本地 observed.jsonl 的指标自动生成。
参数来源只允许三种：library = 资料与文献（本地资料库文件、卡片引用的论文原文）· observed = 本地观察 / 实测
（观察者统计出的指标、沙箱里实际执行得到的数字）· user = 由你提供（值为空 = 开跑前必须你来填）。

只用标准库。被 run_action.py（建任务、写进提示词与回执）和 review_server.py（评价页动手单 / 回执面板）共用。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

FIELDS = [("scene", "场景"), ("params", "参数"), ("method", "方法"), ("output", "产出"),
          ("baseline", "对照"), ("criterion", "判据"), ("deliverables", "交付")]
SOURCES = {"library": "资料/文献", "observed": "本地观察/实测", "user": "你提供"}
TEXT_FIELDS = [k for k, _ in FIELDS if k not in ("params", "deliverables")]

# observed.jsonl metric key → (中文名, 单位)
METRIC_NAMES = {
    "length": ("函数长度", "行"), "lines": ("行数", "行"), "complexity": ("圈复杂度", "无量纲"), "nesting": ("最大嵌套深度", "层"),
    "args": ("参数个数", "个"), "loop_depth": ("循环嵌套", "层"), "copies": ("重复份数", "份"), "span": ("重复片段长度", "行"),
    "dup_lines": ("重复行数", "行"), "commits": ("近期提交次数", "次"), "count": ("出现次数", "次"), "score": ("出现次数", "次"),
    "complexity_sum": ("文件复杂度合计", "无量纲"), "z": ("偏离程度 z", "σ"), "value": ("异常值", "—"), "line": ("行号", "行"),
    "calls": ("调用次数", "次"), "files": ("涉及文件数", "个"), "size_kb": ("文件大小", "KB"), "rows": ("行数", "行"),
}

BASELINE_BY_TYPE = {
    "optimization": "改动前的原代码：同一输入、同一环境下的输出与耗时（先跑一遍原版留底）",
    "hypothesis": "不引入该假说 / 方法时的现有结果（原流程在同一数据上的输出）",
    "knowledge": "你现在的笔记与理解（卡片 why_me 里描述的现状）",
    "process": "现在的做法：步骤数、耗时、手动操作次数（按当前流程走一遍记录）",
}
DELIVERABLES_BY_TYPE = {
    "optimization": ["patch.diff（只改副本，原文件保持不动）", "bench.py（新旧两版同输入对比 + 计时）", "result.json"],
    "hypothesis": ["check.py（最小验证脚本）", "report.md（结果与判断）", "result.json"],
    "knowledge": ["summary.md（针对卡片问题的整理文档）", "result.json"],
    "process": ["process.md（新旧流程对比）", "可复用的脚本或模板", "result.json"],
}
DEFAULT_USER_PARAM = {"optimization": ("计时重复次数", "5", "次"), "hypothesis": ("重复次数（随机种子数）", "3", "次"),
                      "knowledge": ("文档篇幅上限", "1500", "字"), "process": ("对比的流程轮数", "1", "轮")}
UNIT_RE = r"\s*(%|ms|s|秒|分钟|小时|h|min|倍|行|层|次|个|份|条|项|KB|MB|GB|K|nm|μm|um|mm|cm|m|eV|meV|K|Hz|kHz|MHz|GHz|THz|dB|W|mW|V|mV|A|mA|T|σ)?"


def _num_unit(text: str, value: str) -> tuple[str, str]:
    """Find `value` in text and return (name-ish words before it, unit after it)."""
    m = re.search(r"([\u4e00-\u9fffA-Za-z_]{1,10})?\s*[:：=为是约达到]?\s*" + re.escape(value) + UNIT_RE, text or "")
    if not m:
        return "", ""
    return (m.group(1) or "").strip(), (m.group(2) or "").strip()


def _prov_source(s: str) -> str:
    s = str(s or "")
    if s == "observed" or s.startswith("executed"):
        return "observed"
    if s.startswith(("abstract", "fulltext")):
        return "library"
    return "library"


def _observed_row(state_dir: Path | None, obs_id: str) -> dict:
    if not state_dir or not obs_id.startswith("obs_"):
        return {}
    for name in ("observed.jsonl", "library/observed_history.jsonl"):
        p = Path(state_dir) / name
        if not p.is_file():
            continue
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict) and o.get("id") == obs_id:
                return o
    return {}


def _deliverables(v) -> list[str]:
    if isinstance(v, str):
        v = [x.strip() for x in re.split(r"[;\n；、]+", v) if x.strip()]
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def normalize(brief: dict) -> dict:
    """Coerce a brief coming from the agent or the review page into the canonical shape (never raises)."""
    b = dict(brief or {})
    out = {k: str(b.get(k) or "").strip() for k in TEXT_FIELDS}
    ps = []
    for p in b.get("params") or []:
        if not isinstance(p, dict):
            continue
        ps.append({"name": str(p.get("name") or "").strip(), "value": str(p.get("value") if p.get("value") is not None else "").strip(),
                   "unit": str(p.get("unit") or "").strip(), "source": str(p.get("source") or "").strip().lower(),
                   **({"ref": str(p["ref"])} if p.get("ref") else {}), **({"suggested": True} if p.get("suggested") else {})})
    out["params"] = ps
    out["deliverables"] = _deliverables(b.get("deliverables"))
    for k in ("origin", "card_id"):
        if b.get(k):
            out[k] = b[k]
    return out


def check(brief: dict) -> tuple[list[str], list[str]]:
    """Return (problems, missing). problems = the brief is malformed; missing = user-sourced values still empty
    (the review page asks for them before the task starts)."""
    b = normalize(brief)
    problems, missing = [], []
    for k, label in FIELDS:
        if k == "params":
            if not b["params"]:
                problems.append("参数：至少 1 项（数值 + 单位 + 来源）")
            for i, p in enumerate(b["params"], 1):
                tag = p["name"] or f"第 {i} 项"
                if not p["name"]:
                    problems.append(f"参数第 {i} 项缺名称")
                if p["source"] not in SOURCES:
                    problems.append(f"参数「{tag}」来源必须是 library / observed / user（当前 {p['source'] or '空'}）")
                if not p["value"]:
                    if p["source"] == "user":
                        missing.append(tag)
                    else:
                        problems.append(f"参数「{tag}」缺数值（来源 {p['source']} 的数值必须写出来）")
                if not p["unit"] and not (p["source"] == "user" and not p["value"]):
                    problems.append(f"参数「{tag}」缺单位（没有量纲写“无量纲”）")
        elif k == "deliverables":
            if not b["deliverables"]:
                problems.append("交付：至少 1 个文件 / 脚本 / 文档")
        elif not b[k]:
            problems.append(f"{label}：不能为空")
    return problems, missing


def derive(card: dict, state_dir: Path | None = None) -> dict:
    """Build a brief from the card fields (used when the card has no valid action_brief)."""
    src = card.get("source") or {}
    ctype = card.get("type") or "optimization"
    sub = card.get("subfield") or src.get("kind") or "—"
    ev = str(src.get("evidence_ref") or "")
    obj = Path(ev.split("#", 1)[0]).name if ev.startswith("library://") else (f"arXiv:{src.get('ref')}" if src.get("kind") == "arxiv" else "")
    scene = f"{card.get('title', '')}（" + (f"对象：{obj}；" if obj else "") + f"子领域：{sub}）"
    texts = " ".join(str(card.get(k) or "") for k in ("title", "one_line", "why_me", "hypothesis", "magnitude", "expected_observable"))
    texts += " " + str(src.get("quote") or "")
    params, seen = [], set()
    row = _observed_row(state_dir, str(src.get("ref") or ""))
    for key, val in (row.get("metrics") or {}).items():
        if isinstance(val, bool) or not isinstance(val, (int, float)) or key not in METRIC_NAMES:
            continue
        name, unit = METRIC_NAMES[key]
        params.append({"name": name, "value": f"{val:g}" if isinstance(val, float) else str(val), "unit": unit,
                       "source": "observed", "ref": ev or row.get("evidence_ref") or src.get("ref")})
        seen.add(str(val))
    for p in card.get("numbers_provenance") or []:
        if not isinstance(p, dict) or str(p.get("value", "")).strip() in ("", *seen):
            continue
        v = str(p["value"]).strip()
        name, unit = _num_unit(texts, v)
        params.append({"name": name or f"数值 {len(params) + 1}", "value": v, "unit": unit or "无量纲",
                       "source": _prov_source(p.get("source")), "ref": str(p.get("source") or "")})
        seen.add(v)
    if not params:  # nothing numeric on the card: a SUGGESTED user parameter (editable on the review page)
        name, val, unit = DEFAULT_USER_PARAM.get(ctype, DEFAULT_USER_PARAM["hypothesis"])
        params.append({"name": name, "value": val, "unit": unit, "source": "user", "suggested": True})
    method = (f"假说：{card['hypothesis']}；" if str(card.get("hypothesis") or "").strip() else "") + f"最小验证：{card.get('minimal_check', '')}"
    return normalize({
        "scene": scene, "params": params, "method": method,
        "output": str(card.get("expected_observable") or ""),
        "baseline": BASELINE_BY_TYPE.get(ctype, BASELINE_BY_TYPE["hypothesis"]),
        "criterion": f"成立：{card.get('expected_observable', '')}；不成立：{card.get('falsifier', '')}",
        "deliverables": DELIVERABLES_BY_TYPE.get(ctype, DELIVERABLES_BY_TYPE["hypothesis"]),
        "origin": "derived", "card_id": card.get("id"),
    })


def build(card: dict, state_dir: Path | None = None) -> dict:
    """Card's own action_brief when it is well-formed, otherwise derived. Always returns problems / missing."""
    own = card.get("action_brief")
    if isinstance(own, dict):
        b = normalize(dict(own, origin="card", card_id=card.get("id")))
        problems, missing = check(b)
        if not problems:
            return dict(b, problems=[], missing=missing)
    b = derive(card, state_dir)
    problems, missing = check(b)
    return dict(b, problems=problems, missing=missing)


def render_md(b: dict) -> str:
    b = normalize(b)
    L = [f"- **场景**：{b['scene']}", "- **参数**："]
    for p in b["params"]:
        val = (p["value"] or "（待你填写）") + ("（建议值，可改）" if p.get("suggested") else "")
        L.append(f"  - {p['name']} = {val} {p['unit']} 〔{p['source']} · {SOURCES.get(p['source'], '?')}"
                 + (f" · {p['ref']}" if p.get("ref") else "") + "〕")
    L += [f"- **方法**：{b['method']}", f"- **产出**：{b['output']}", f"- **对照**：{b['baseline']}",
          f"- **判据**：{b['criterion']}", "- **交付**：" + "；".join(b["deliverables"])]
    return "\n".join(L)


def render_prompt(b: dict, outdir: str) -> str:
    b = normalize(b)
    ps = "\n".join(f"  - {p['name']} = {p['value']} {p['unit']}（来源 {p['source']}" + (f"：{p['ref']}" if p.get("ref") else "") + "）"
                   for p in b["params"])
    dl = "\n".join(f"  - {outdir}/{d}" if "/" not in d.split("（")[0] else f"  - {d}" for d in b["deliverables"])
    return f"""【动手单】（用户已确认；与卡片字段冲突时以动手单为准）
场景：{b['scene']}
参数（来源 library=资料/文献，observed=本地观察/实测，user=用户提供；不得改动 user 参数的值）：
{ps}
方法：{b['method']}
产出：{b['output']}
对照：{b['baseline']}（先在同一条件下跑出对照，再跑新做法）
判据：{b['criterion']}
交付：
{dl}
"""


def self_test() -> int:
    card = {"id": "ins_20261030_008", "type": "optimization", "title": "拆分 process_high_freq_power", "subfield": "code",
            "one_line": "117 行、复杂度 25", "why_me": "你的 power_dep.py 里这个函数有 117 行、复杂度 25、嵌套 5 层",
            "hypothesis": "", "minimal_check": "拆成 3 个函数，同一输入跑新旧两版比对输出并计时",
            "expected_observable": "输出逐元素一致，耗时不增加", "falsifier": "输出不一致或耗时上升 > 10%",
            "magnitude": "117 行 → 每个 ≤ 40 行", "numbers_provenance": [{"value": "117", "source": "observed"},
                                                                       {"value": "25", "source": "observed"},
                                                                       {"value": "10%", "source": "executed:bench"}],
            "source": {"kind": "workspace", "ref": "obs_3a7611581a56", "evidence_ref": "library://code/power_dep.py#L177-L293"}}
    b = build(card)
    assert b["origin"] == "derived" and not b["problems"], b
    assert {p["source"] for p in b["params"]} <= set(SOURCES) and any(p["value"] == "117" and p["unit"] == "行" for p in b["params"]), b["params"]
    assert "power_dep.py" in b["scene"] and "patch.diff" in b["deliverables"][0]
    good = dict(card, action_brief={"scene": "s", "params": [{"name": "n", "value": "1", "unit": "次", "source": "user"}],
                                    "method": "m", "output": "o", "baseline": "b", "criterion": "c", "deliverables": "a.py；b.md"})
    b2 = build(good)
    assert b2["origin"] == "card" and b2["deliverables"] == ["a.py", "b.md"], b2
    bad = dict(card, action_brief={"scene": "s", "params": [{"name": "n", "value": "1", "unit": "次", "source": "guess"}]})
    assert build(bad)["origin"] == "derived"
    p, m = check({"scene": "s", "params": [{"name": "规模", "value": "", "unit": "条", "source": "user"}], "method": "m",
                  "output": "o", "baseline": "b", "criterion": "c", "deliverables": ["x"]})
    assert not p and m == ["规模"], (p, m)
    assert "待你填写" in render_md({"params": [{"name": "规模", "value": "", "unit": "条", "source": "user"}]})
    assert "/workspace/x/bench.py" in render_prompt(b2 | {"deliverables": ["bench.py"]}, "/workspace/x")
    nocard = build({"id": "x", "type": "knowledge", "title": "t", "expected_observable": "e", "falsifier": "f", "minimal_check": "m", "source": {}})
    assert not nocard["missing"] and not nocard["problems"] and nocard["params"][0]["suggested"], nocard
    assert nocard["scene"] == "t（子领域：—）", nocard["scene"]
    print("SELF-TEST OK: action_brief")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test() if "--self-test" in sys.argv else 0)
