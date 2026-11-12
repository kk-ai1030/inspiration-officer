#!/usr/bin/env python3
"""用户画像（v1.4）：像系统级 AGENT.md 一样，让灵感官“不用提醒也知道你是谁、会什么、不会什么”。

双写：文件是权威，记忆是投影（feedback3 §9 的实验结论）
  inspiration/profile/profile.json   结构化权威副本（可 diff / 回滚）
  inspiration/profile/AGENT.md       给人看、也给 agent 读的系统级画像
  inspiration/profile/interests.md   兴趣（只代表“在关注”，不代表“会”）
  inspiration/profile/claims.md      能力自述 + 不熟领域（每条带证据与日期）
  平台长期记忆 category=profile      5–8 条一句话投影，每次运行自动召回（tags: profile, confirmed, insp-profile）

    python platform/profile_tool.py --config config.json --interview          # 终端里回答 7 个问题
    python platform/profile_tool.py --config config.json --from-json p.json   # 用 insp-profile 技能产出的 JSON
    python platform/profile_tool.py --config config.json --show
    python platform/profile_tool.py --config config.json --sync-memory        # 以文件为准重写记忆投影
    python platform/profile_tool.py --config config.json --audit-memory       # 列出平台自动抽取的“画像类”记忆
    python platform/profile_tool.py --config config.json --promote <记忆ID> | --drop <记忆ID>
    python platform/profile_tool.py --self-test

写记忆需要会话令牌（API Key 不能写记忆）：环境变量 SCIENCEPAL_SETUP_TOKEN 或 ~/.sciencepal/session_token。
没有令牌时文件照常写，记忆投影存进 profile/memory_pending.json，下次有令牌时 --sync-memory 补上。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from sciencepal_client import ApiError, SciencePal  # noqa: E402

SCHEMA = "insp-profile/1.0"
TAG = "insp-profile"
LEVELS = ("熟练", "能用", "了解", "入门", "不会")
QUESTIONS = [
    ("identity", "1/7 你的身份与研究领域？当前在做的项目用一句话说。\n   例：物理系博士三年级，做中性原子量子计算实验；项目是 Rydberg 门保真度优化"),
    ("interests", "2/7 最近最关注的 2–4 个方向（只代表兴趣，不代表你会），用分号隔开"),
    ("strengths", "3/7 你擅长的技术/方法，写到能举出一个例子的程度，用分号隔开\n   例：Python 数据分析（写过整套采集-拟合脚本）；光学对准"),
    ("gaps", "4/7 你不熟、需要先给定义再给结论的领域，用分号隔开\n   例：量子纠错编码理论；深度学习训练细节"),
    ("preferences", "5/7 表达偏好（语言、结论先行、表格、详略）\n   例：中文；结论先行；能表格就表格；不要客套"),
    ("sources", "6/7 arXiv 分类与关键词。分类用逗号（如 quant-ph,physics.atom-ph），关键词用分号\n   格式：分类 | 关键词1; 关键词2"),
    ("do_not_assume", "7/7 有什么“不要假设我会”的东西，或明确不想看的方向？（可留空）"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def split(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[;；\n]", s or "") if x.strip()]


def make_query(cats: list[str], kws: list[str]) -> str:
    """arXiv API query from categories + keywords, e.g. (cat:quant-ph) AND (abs:"surface code" OR abs:decoder)."""
    c = " OR ".join(f"cat:{x}" for x in cats if x)
    k = " OR ".join((f'abs:"{x}"' if " " in x else f"abs:{x}") for x in kws if x)
    if c and k:
        return f"({c}) AND ({k})"
    return c or k


def from_answers(ans: dict) -> dict:
    ident = ans.get("identity", "")
    src = ans.get("sources", "")
    cats, kws = [], []
    if "|" in src:
        a, b = src.split("|", 1)
        cats, kws = [x.strip() for x in re.split(r"[,，\s]+", a) if x.strip()], split(b)
    else:
        kws = split(src)
    return {
        "schema": SCHEMA, "updated_at": now_iso(),
        "identity": {"summary": ident},
        "interests": [{"topic": t} for t in split(ans.get("interests", ""))],
        "strengths": [{"item": t, "evidence": f"自述 {now_iso()[:10]}"} for t in split(ans.get("strengths", ""))],
        "gaps": [{"item": t, "need": "先给定义再给结论"} for t in split(ans.get("gaps", ""))],
        "preferences": {"summary": ans.get("preferences", "")},
        "sources": {"arxiv_categories": cats, "keywords": kws, "arxiv_query": make_query(cats, kws)},
        "do_not_assume": split(ans.get("do_not_assume", "")),
    }


def check(p: dict) -> list[str]:
    errs = []
    if p.get("schema") != SCHEMA:
        errs.append(f"schema 必须是 {SCHEMA}")
    if not (p.get("identity") or {}).get("summary"):
        errs.append("identity.summary 不能为空")
    for k in ("interests", "strengths", "gaps"):
        if not isinstance(p.get(k), list):
            errs.append(f"{k} 必须是数组")
    for s in p.get("strengths") or []:
        if not isinstance(s, dict) or not s.get("item") or not s.get("evidence"):
            errs.append(f"strengths 每条都要有 item 与 evidence（没有证据的能力不能写进画像）：{s}")
    for s in p.get("knowledge_levels") or []:
        if s.get("level") not in LEVELS:
            errs.append(f"knowledge_levels.level 只能是 {LEVELS}：{s}")
    return errs


# ----------------------------------------------------------------------------- renderers
def render_agent_md(p: dict) -> str:
    L = ["# 用户画像（AGENT.md · 系统级）", "",
         f"> 由 profile_tool.py 生成于 {p.get('updated_at')}。权威副本是 profile.json；改画像请改它或重新访谈，不要只改这里。", "",
         "## 我是谁", p["identity"].get("summary", ""), ""]
    if p.get("strengths"):
        L += ["## 擅长（有证据的才算）"] + [f"- {s['item']}（{s.get('evidence')}）" for s in p["strengths"]] + [""]
    if p.get("gaps"):
        L += ["## 不熟 / 需要先给定义"] + [f"- {g['item']}：{g.get('need', '先给定义再给结论')}" for g in p["gaps"]] + [""]
    if p.get("knowledge_levels"):
        L += ["## 知识深度"] + [f"- {k['area']}：{k['level']}" for k in p["knowledge_levels"]] + [""]
    if p.get("interests"):
        L += ["## 正在关注（兴趣 ≠ 能力）"] + [f"- {i['topic']}" + (f"：{i['why']}" if i.get("why") else "") for i in p["interests"]] + [""]
    if (p.get("preferences") or {}).get("summary"):
        L += ["## 表达偏好", p["preferences"]["summary"], ""]
    if p.get("do_not_assume"):
        L += ["## 不要假设我会 / 不想看"] + [f"- {x}" for x in p["do_not_assume"]] + [""]
    if p.get("confirmed_notes"):
        L += ["## 已确认的补充（来自平台自动抽取，经用户确认）"] + [f"- {x['content']}" for x in p["confirmed_notes"]] + [""]
    L += ["## 给灵感官的纪律",
          "- 画像里没有证据的能力，一律按“不会”处理：先给定义、再给结论。",
          "- “正在关注”只用来判断相关性，绝不能推断成“擅长”。",
          "- 平台自动抽取的记忆只是候选；涉及能力和偏好的，必须经用户确认才算画像。", ""]
    return "\n".join(L)


def render_interests(p: dict) -> str:
    s = p.get("sources") or {}
    L = ["# 兴趣（interests.md）", "", "> 只代表“在关注”，**不代表会**。灵感官用它判断相关性，不得用它推断能力。", ""]
    L += [f"- {i['topic']}" + (f"：{i['why']}" if i.get("why") else "") for i in p.get("interests", [])]
    L += ["", "## 检索来源", f"- arXiv 分类：{', '.join(s.get('arxiv_categories') or []) or '-'}",
          f"- 关键词：{'; '.join(s.get('keywords') or []) or '-'}", f"- 检索式：`{s.get('arxiv_query') or '-'}`", ""]
    return "\n".join(L)


def render_claims(p: dict) -> str:
    L = ["# 能力自述（claims.md）", "", "> 每条都带证据。没有写在这里的能力 = 不要假设用户会。", "", "## 擅长"]
    L += [f"- {s['item']}　〔证据：{s.get('evidence')}〕" for s in p.get("strengths", [])] or ["- （空）"]
    L += ["", "## 不熟 / 需要先给定义"]
    L += [f"- {g['item']}　〔{g.get('need', '先给定义再给结论')}〕" for g in p.get("gaps", [])] or ["- （空）"]
    if p.get("knowledge_levels"):
        L += ["", "## 知识深度"] + [f"- {k['area']}：{k['level']}" for k in p["knowledge_levels"]]
    if p.get("do_not_assume"):
        L += ["", "## 不要假设我会"] + [f"- {x}" for x in p["do_not_assume"]]
    return "\n".join(L) + "\n"


def memory_items(p: dict) -> list[dict]:
    """5–8 one-sentence projections (≤ 400 chars each; the platform limit is 1000)."""
    def j(xs, n=6):
        return "；".join(xs[:n])
    items = [("fact", f"[灵感画像] 身份与领域：{p['identity'].get('summary', '')}")]
    if p.get("strengths"):
        items.append(("fact", "[灵感画像] 擅长（有证据）：" + j([s["item"] for s in p["strengths"]])))
    if p.get("gaps"):
        items.append(("fact", "[灵感画像] 不熟、需要先给定义再给结论：" + j([g["item"] for g in p["gaps"]])))
    if p.get("interests"):
        items.append(("fact", "[灵感画像] 正在关注（只代表兴趣，不代表会）：" + j([i["topic"] for i in p["interests"]])))
    if (p.get("preferences") or {}).get("summary"):
        items.append(("preference", "[灵感画像] 表达偏好：" + p["preferences"]["summary"]))
    if p.get("do_not_assume"):
        items.append(("fact", "[灵感画像] 不要假设我会 / 不想看：" + j(p["do_not_assume"])))
    s = p.get("sources") or {}
    if s.get("arxiv_query") or s.get("keywords"):
        items.append(("fact", f"[灵感配置] 跑灵感时传给 insp_run.py 的 --query：{s.get('arxiv_query') or '-'}；"
                              f"--keywords：{','.join(s.get('keywords') or [])}"))
    items.append(("preference", "[灵感画像] 纪律：画像里没有证据的能力一律按“不会”处理；兴趣不能推断成能力。"))
    return [{"memory_type": t, "content": c[:400], "category": "profile", "tags": ["profile", "confirmed", TAG]} for t, c in items[:8]]


# ----------------------------------------------------------------------------- io
def load_cfg(cfg_path: Path) -> tuple[dict, Path]:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    sd = Path(cfg.get("state_dir", "./inspiration"))
    return cfg, (sd if sd.is_absolute() else cfg_path.resolve().parent / sd).resolve()


def session_client(cfg: dict) -> SciencePal | None:
    tok = os.environ.get(cfg.get("setup_token_env", "SCIENCEPAL_SETUP_TOKEN"), "")
    p = Path(os.path.expanduser(cfg.get("setup_token_file", "~/.sciencepal/session_token")))
    if not tok and p.is_file():
        tok = p.read_text(encoding="utf-8").strip()
    if not tok:
        return None
    return SciencePal(cfg["base_url"], tok, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer"))


def _mem_list(sp: SciencePal) -> list[dict]:
    r = sp.list_memory()
    items = r.get("memories", r.get("items", r.get("data", []))) if isinstance(r, dict) else r
    return [m for m in items or [] if isinstance(m, dict)]


def sync_memory(cfg: dict, prof_dir: Path, p: dict, client: SciencePal | None = None) -> str:
    items = memory_items(p)
    (prof_dir / "memory_projection.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    sp = client or session_client(cfg)
    if sp is None:
        (prof_dir / "memory_pending.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"skipped（没有会话令牌；{len(items)} 条投影已存 memory_pending.json，之后运行 --sync-memory）"
    removed = 0
    for m in _mem_list(sp):
        if TAG in (m.get("tags") or []):
            sp.delete_memory(m.get("id") or m.get("memory_id"))
            removed += 1
    for it in items:
        sp.create_memory(it["content"], category=it["category"], tags=it["tags"], memory_type=it["memory_type"])
    (prof_dir / "memory_pending.json").unlink(missing_ok=True)
    return f"synced {len(items)}（替换旧投影 {removed} 条）"


def apply_profile(cfg_path: Path, p: dict, sync_memory: bool = True, client: SciencePal | None = None) -> dict:
    errs = check(p)
    if errs:
        raise ValueError("画像不合格：" + "；".join(errs))
    cfg, state_dir = load_cfg(Path(cfg_path))
    prof = state_dir / "profile"
    prof.mkdir(parents=True, exist_ok=True)
    p = dict(p, updated_at=p.get("updated_at") or now_iso())
    files = {"profile.json": json.dumps(p, ensure_ascii=False, indent=2), "AGENT.md": render_agent_md(p),
             "interests.md": render_interests(p), "claims.md": render_claims(p)}
    for name, txt in files.items():
        (prof / name).write_text(txt, encoding="utf-8")
    # v1.5.1: every applied version (including the very first one) is snapshotted, so history/ always has v1..vN
    hist = prof / "history"
    hist.mkdir(exist_ok=True)
    n = len(list(hist.glob("profile_*.json"))) + 1
    shutil.copy(prof / "profile.json", hist / f"profile_{datetime.now().strftime('%Y%m%d-%H%M%S')}_v{n}.json")
    s = p.get("sources") or {}
    sf = state_dir / "state.json"
    if (s.get("arxiv_query") or s.get("keywords")) and sf.is_file():
        st = json.loads(sf.read_text(encoding="utf-8"))
        changed = False
        if s.get("arxiv_query"):
            arx = [x for x in st.get("sources", []) if x.get("kind") == "arxiv"]
            if arx and arx[0].get("query") != s["arxiv_query"]:
                arx[0]["query"], changed = s["arxiv_query"], True
            elif not arx:
                st.setdefault("sources", []).append({"kind": "arxiv", "query": s["arxiv_query"], "max_results": 60})
                changed = True
        if s.get("keywords") and st.get("keywords") != s["keywords"]:
            st["keywords"], changed = list(s["keywords"]), True
        if changed:
            shutil.copy(sf, sf.with_name(f"state.json.bak-profile-{datetime.now().strftime('%Y%m%d-%H%M%S')}"))
            sf.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    mem = sync_memory_fn(cfg, prof, p, client) if sync_memory else "not requested"
    return {"files": sorted(files), "memory": mem, "profile_dir": str(prof)}


sync_memory_fn = sync_memory


def audit_memory(cfg: dict) -> list[dict]:
    """Auto-extracted memories that talk about the user's abilities / preferences are only CANDIDATES."""
    sp = session_client(cfg)
    if sp is None:
        raise SystemExit("需要会话令牌（SCIENCEPAL_SETUP_TOKEN 或 ~/.sciencepal/session_token）")
    pat = re.compile(r"用户(擅长|熟悉|会|不会|不熟|背景|偏好|喜欢|要求|知识)|prefers?|familiar|expert|background", re.I)
    out = []
    for m in _mem_list(sp):
        src = json.dumps(m.get("source") or m.get("metadata") or {}, ensure_ascii=False)
        if "auto_extraction" in src and TAG not in (m.get("tags") or []) and pat.search(str(m.get("content", ""))):
            out.append({"id": m.get("id") or m.get("memory_id"), "content": m.get("content"), "category": m.get("category")})
    return out


def interview() -> dict:
    print("用户画像访谈（7 个问题，回车跳过；随时 Ctrl+C 放弃，不会写任何东西）\n")
    ans = {}
    for key, q in QUESTIONS:
        print(q)
        ans[key] = input("> ").strip()
        print()
    return from_answers(ans)


def self_test() -> int:
    ans = {"identity": "物理系博士，做中性原子实验", "interests": "QEC 布局；Rydberg 门", "strengths": "Python 数据分析；光学对准",
           "gaps": "量子纠错编码理论", "preferences": "中文；结论先行；表格", "sources": "quant-ph, physics.atom-ph | surface code; Rydberg gate",
           "do_not_assume": "深度学习训练细节"}
    p = from_answers(ans)
    assert not check(p), check(p)
    assert p["sources"]["arxiv_query"] == '(cat:quant-ph OR cat:physics.atom-ph) AND (abs:"surface code" OR abs:"Rydberg gate")', p["sources"]
    bad = dict(p, strengths=[{"item": "会 QEC"}])
    assert any("evidence" in e for e in check(bad))
    items = memory_items(p)
    assert 5 <= len(items) <= 8 and all(len(i["content"]) <= 400 and TAG in i["tags"] for i in items)
    assert any("只代表兴趣" in i["content"] for i in items)

    class FakeSP:
        def __init__(self):
            self.mem = [{"id": "old1", "content": "[灵感画像] 旧", "tags": ["profile", TAG]},
                        {"id": "keep", "content": "用户偏好表格", "tags": [], "source": {"kind": "auto_extraction"}}]

        def list_memory(self):
            return {"memories": self.mem}

        def delete_memory(self, i):
            self.mem = [m for m in self.mem if m["id"] != i]

        def create_memory(self, content, category=None, tags=None, memory_type="fact", confidence=1.0):
            self.mem.append({"id": f"n{len(self.mem)}", "content": content, "tags": tags})

    with tempfile.TemporaryDirectory() as d:
        t = Path(d)
        shutil.copytree(HERE.parent / "insp-init/assets/灵感状态模板/inspiration", t / "inspiration")
        (t / "config.json").write_text(json.dumps({"base_url": "http://x", "state_dir": "./inspiration"}), encoding="utf-8")
        fake = FakeSP()
        r = apply_profile(t / "config.json", p, True, fake)
        st = json.loads((t / "inspiration/state.json").read_text(encoding="utf-8"))
        assert st["sources"][0]["query"] == p["sources"]["arxiv_query"] and st["keywords"] == ["surface code", "Rydberg gate"]
        assert (t / "inspiration/profile/AGENT.md").read_text(encoding="utf-8").count("兴趣 ≠ 能力") == 1
        assert "old1" not in {m["id"] for m in fake.mem} and "keep" in {m["id"] for m in fake.mem}, fake.mem
        assert r["memory"].startswith("synced"), r
        # first apply already leaves v1 in history (does not depend on whether this machine has a session token)
        assert len(list((t / "inspiration/profile/history").glob("profile_*_v1.json"))) == 1, "history v1 kept"
        r2 = apply_profile(t / "config.json", p, False, None)          # sync_memory=False: never touches a real account
        assert r2["memory"] == "not requested", r2
        assert len(list((t / "inspiration/profile/history").glob("profile_*.json"))) == 2, "history v2 kept"
        if not session_client({"base_url": "x"}):
            r3 = apply_profile(t / "config.json", p, True, None)
            assert r3["memory"].startswith("skipped") and (t / "inspiration/profile/memory_pending.json").is_file()
    print("SELF-TEST OK: profile_tool")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="灵感机制 · 用户画像")
    ap.add_argument("--config", type=Path)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--interview", action="store_true")
    g.add_argument("--from-json", type=Path)
    g.add_argument("--show", action="store_true")
    g.add_argument("--sync-memory", action="store_true")
    g.add_argument("--audit-memory", action="store_true")
    g.add_argument("--promote")
    g.add_argument("--drop")
    g.add_argument("--self-test", action="store_true")
    ap.add_argument("--no-memory", action="store_true", help="只写文件，不碰平台记忆")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.config:
        ap.error("需要 --config")
    cfg, state_dir = load_cfg(a.config)
    prof = state_dir / "profile"
    if a.show:
        f = prof / "AGENT.md"
        print(f.read_text(encoding="utf-8") if f.is_file() else "还没有画像：python platform/profile_tool.py --config <config> --interview")
        return 0
    if a.interview or a.from_json:
        p = interview() if a.interview else json.loads(a.from_json.read_text(encoding="utf-8"))
        errs = check(p)
        if errs:
            print("ERROR:", "；".join(errs))
            return 2
        print(render_agent_md(p))
        if a.interview and input("\n按回车确认写入，输入 n 放弃：").strip().lower() == "n":
            print("已放弃，没有写任何东西")
            return 0
        r = apply_profile(a.config, p, not a.no_memory)
        print(f"OK: 画像文件 {r['files']} → {r['profile_dir']}；记忆 {r['memory']}")
        return 0
    if a.sync_memory:
        p = json.loads((prof / "profile.json").read_text(encoding="utf-8"))
        print("记忆：", sync_memory(cfg, prof, p))
        return 0
    if a.audit_memory:
        rows = audit_memory(cfg)
        if not rows:
            print("没有需要确认的自动抽取画像记忆")
        for r in rows:
            print(f"{r['id']}  [{r.get('category')}]  {r['content']}")
        if rows:
            print("\n逐条处理：--promote <ID>（确认为画像）或 --drop <ID>（删除）。不处理 = 继续只当候选，不会写进画像文件。")
        return 0
    sp = session_client(cfg)
    if sp is None:
        print("需要会话令牌")
        return 2
    mid = a.promote or a.drop
    m = next((x for x in _mem_list(sp) if str(x.get("id") or x.get("memory_id")) == mid), None)
    if not m:
        print("找不到记忆", mid)
        return 1
    if a.drop:
        sp.delete_memory(mid)
        print("已删除", mid)
        return 0
    p = json.loads((prof / "profile.json").read_text(encoding="utf-8"))
    p.setdefault("confirmed_notes", []).append({"content": m.get("content"), "from_memory": mid, "confirmed_at": now_iso()})
    (prof / "profile.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    sp.patch_memory(mid, {"tags": sorted(set((m.get("tags") or []) + ["profile", "confirmed"])), "category": "profile"})
    print("已确认为画像：", m.get("content"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
