#!/usr/bin/env python3
"""把网页访谈的回答整理成用户画像 profile.json（insp-profile/1.0），在 SciencePal 沙箱里运行。

    python3 build_profile.py --answers /workspace/inspiration_out/profile_answers.json --preview
    python3 build_profile.py --answers /workspace/inspiration_out/profile_answers.json --confirmed
    python3 build_profile.py --self-test

--preview    只校验并打印给用户看的预览（AGENT.md 风格），不写 profile.json
--confirmed  用户已在对话里确认：写 /workspace/inspiration_out/profile.json，并打印
             <<<INSP_PROFILE_JSON ... INSP_PROFILE_JSON>>> 结果块（本地收货员据此落盘、同步记忆）

answers 的格式（缺省字段可以不写）：
{
  "identity": "物理系博三，做中性原子量子计算实验；项目是 Rydberg 门保真度优化",
  "interests": [{"topic": "量子纠错", "why": "下一步要做"}, "中性原子阵列"],
  "strengths": [{"item": "Python 数据分析", "evidence": "写过整套采集-拟合脚本"}],
  "gaps": ["量子纠错编码理论", {"item": "深度学习训练细节", "need": "先给直觉再给公式"}],
  "knowledge_levels": [{"area": "表面码", "level": "入门"}],
  "preferences": "中文；结论先行；能表格就表格",
  "arxiv_categories": ["quant-ph", "physics.atom-ph"],
  "keywords": ["Rydberg", "neutral atom"],
  "do_not_assume": ["不要假设我会 C++"]
}

校验规则与本地 platform/profile_tool.py 的 check() 一致；没有举例（evidence）的“擅长”会被自动降级为
knowledge_levels 里的“了解”，并打印 WARN —— 画像里没有证据的能力不能算“会”。
只用 Python 标准库。退出码：0 成功 · 2 回答不合格（按提示补问用户后重跑）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "insp-profile/1.0"
LEVELS = ("熟练", "能用", "了解", "入门", "不会")
BLOCK_START, BLOCK_END = "<<<INSP_PROFILE_JSON", "INSP_PROFILE_JSON>>>"
DEF_OUT = Path("/workspace/inspiration_out")
CAT_RE = re.compile(r"^[a-z\-]+(\.[A-Za-z\-]+)?$")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def as_list(x) -> list:
    if x is None:
        return []
    if isinstance(x, str):
        return [s.strip() for s in re.split(r"[;；\n]", x) if s.strip()]
    return list(x)


def make_query(cats: list[str], kws: list[str]) -> str:
    c = " OR ".join(f"cat:{x}" for x in cats if x)
    k = " OR ".join((f'abs:"{x}"' if " " in x else f"abs:{x}") for x in kws if x)
    if c and k:
        return f"({c}) AND ({k})"
    return c or k


def build(ans: dict) -> tuple[dict, list[str], list[str]]:
    """Return (profile, errors, warnings)."""
    errs, warns = [], []
    ident = ans.get("identity")
    ident = ident.get("summary", "") if isinstance(ident, dict) else str(ident or "").strip()
    if not ident:
        errs.append("identity 为空：请补问用户“你的身份、研究领域和当前项目”")
    interests = []
    for i in as_list(ans.get("interests")):
        if isinstance(i, dict) and i.get("topic"):
            interests.append({k: v for k, v in {"topic": str(i["topic"]).strip(), "why": str(i.get("why") or "").strip()}.items() if v})
        elif isinstance(i, str):
            interests.append({"topic": i})
    if not interests:
        errs.append("interests 为空：请补问用户最近最关注的 2–4 个方向")
    strengths, levels = [], []
    for s in as_list(ans.get("strengths")):
        item = s.get("item") if isinstance(s, dict) else s
        ev = (s.get("evidence") if isinstance(s, dict) else "") or ""
        if not item:
            continue
        if str(ev).strip():
            strengths.append({"item": str(item).strip(), "evidence": str(ev).strip()})
        else:
            levels.append({"area": str(item).strip(), "level": "了解", "note": "自述擅长但没有举例，按“了解”处理"})
            warns.append(f"“{item}”没有举例，已降级为“了解”。想算“擅长”，请补问一个具体例子后重跑。")
    gaps = []
    for g in as_list(ans.get("gaps")):
        if isinstance(g, dict) and g.get("item"):
            gaps.append({"item": str(g["item"]).strip(), "need": str(g.get("need") or "先给定义再给结论").strip()})
        elif isinstance(g, str):
            gaps.append({"item": g, "need": "先给定义再给结论"})
    for k in as_list(ans.get("knowledge_levels")):
        if not isinstance(k, dict) or not k.get("area"):
            errs.append(f"knowledge_levels 每条都要有 area 与 level：{k}")
        elif k.get("level") not in LEVELS:
            errs.append(f"knowledge_levels.level 只能是 {'/'.join(LEVELS)}：{k}")
        else:
            levels.append({"area": str(k["area"]).strip(), "level": k["level"]})
    cats = [str(c).strip() for c in as_list(ans.get("arxiv_categories")) if str(c).strip()]
    bad = [c for c in cats if not CAT_RE.match(c)]
    if bad:
        errs.append(f"arXiv 分类格式不对：{bad}（例：quant-ph、cs.LG、physics.atom-ph）")
    kws = [str(k).strip() for k in as_list(ans.get("keywords")) if str(k).strip()]
    if not cats and not kws:
        errs.append("arxiv_categories 和 keywords 都为空：请根据用户领域提议 1–3 个 arXiv 分类和 3–6 个关键词，让用户确认")
    prefs = ans.get("preferences")
    prefs = prefs.get("summary", "") if isinstance(prefs, dict) else "；".join(as_list(prefs))
    p = {
        "schema": SCHEMA, "updated_at": now_iso(),
        "identity": {"summary": ident},
        "interests": interests, "strengths": strengths, "gaps": gaps,
        "preferences": {"summary": prefs},
        "sources": {"arxiv_categories": cats, "keywords": kws, "arxiv_query": make_query(cats, kws)},
        "do_not_assume": [str(x) for x in as_list(ans.get("do_not_assume"))],
        "provenance": {"via": "insp-profile", "confirmed_in_chat": False},
    }
    if levels:
        p["knowledge_levels"] = levels
    return p, errs, warns


def preview(p: dict) -> str:
    L = [f"【我是谁】{p['identity']['summary']}"]
    if p["strengths"]:
        L.append("【擅长（有例子的才算）】" + "；".join(f"{s['item']}（{s['evidence']}）" for s in p["strengths"]))
    if p.get("knowledge_levels"):
        L.append("【知识深度】" + "；".join(f"{k['area']}：{k['level']}" for k in p["knowledge_levels"]))
    if p["gaps"]:
        L.append("【不熟，要先给定义】" + "；".join(g["item"] for g in p["gaps"]))
    L.append("【正在关注（兴趣≠会）】" + "；".join(i["topic"] for i in p["interests"]))
    if p["preferences"]["summary"]:
        L.append("【表达偏好】" + p["preferences"]["summary"])
    if p["do_not_assume"]:
        L.append("【不要假设我会 / 不想看】" + "；".join(p["do_not_assume"]))
    L.append(f"【每次跑灵感用的检索式】{p['sources']['arxiv_query']}")
    return "\n".join(L)


def run(answers: Path, out: Path, confirmed: bool) -> int:
    try:
        ans = json.loads(answers.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: 读不了 {answers}：{exc}")
        return 2
    p, errs, warns = build(ans)
    for w in warns:
        print("WARN:", w)
    if errs:
        print("PROFILE_INVALID（补问用户后修改 answers 再运行）：")
        for e in errs:
            print("  -", e)
        return 2
    print("PREVIEW（原样给用户看，并请用户回复“确认”或指出要改的地方）：")
    print(preview(p))
    if not confirmed:
        return 0
    p["provenance"]["confirmed_in_chat"] = True
    out.mkdir(parents=True, exist_ok=True)
    (out / "profile.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: 已写 {out / 'profile.json'}。把下面的结果块原样放在最终回答最后：")
    print(BLOCK_START)
    print(json.dumps(p, ensure_ascii=False))
    print(BLOCK_END)
    print("===== 收尾：把上面的结果块原样放进最终回答后直接结束。不要调用 complete 工具，不要放附件 =====")
    return 0


def self_test() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond
    good = {"identity": "博三，做中性原子", "interests": ["量子纠错", {"topic": "Rydberg", "why": "项目"}],
            "strengths": [{"item": "Python", "evidence": "写过拟合脚本"}, "光学对准"],
            "gaps": "纠错理论；深度学习", "preferences": "中文；结论先行",
            "arxiv_categories": ["quant-ph"], "keywords": ["Rydberg", "neutral atom"]}
    p, e, w = build(good)
    check("good answers valid", not e)
    check("no-evidence strength demoted", [s["item"] for s in p["strengths"]] == ["Python"]
          and p["knowledge_levels"][0]["area"] == "光学对准" and len(w) == 1)
    check("query built", p["sources"]["arxiv_query"] == '(cat:quant-ph) AND (abs:Rydberg OR abs:"neutral atom")')
    check("gaps split", [g["item"] for g in p["gaps"]] == ["纠错理论", "深度学习"])
    _, e, _ = build({"identity": "", "interests": [], "arxiv_categories": ["Quant Ph"]})
    check("missing fields rejected", len(e) == 3)
    with tempfile.TemporaryDirectory() as t:
        a = Path(t) / "a.json"
        a.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
        check("preview does not write", run(a, Path(t) / "o", False) == 0 and not (Path(t) / "o" / "profile.json").exists())
        check("confirmed writes", run(a, Path(t) / "o", True) == 0 and
              json.loads((Path(t) / "o" / "profile.json").read_text(encoding="utf-8"))["provenance"]["confirmed_in_chat"])
    print("SELF-TEST OK: build_profile" if ok else "SELF-TEST FAILED: build_profile")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="网页访谈 → 用户画像 profile.json")
    ap.add_argument("--answers", type=Path, default=DEF_OUT / "profile_answers.json")
    ap.add_argument("--out", type=Path, default=DEF_OUT)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--preview", action="store_true")
    g.add_argument("--confirmed", action="store_true")
    g.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    return run(a.answers, a.out, a.confirmed)


if __name__ == "__main__":
    sys.exit(main())
