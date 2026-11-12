#!/usr/bin/env python3
"""find_quotes.py — 全文模式下“找引文”的一条短命令（v1.4.3）。

为什么有它：平台的命令执行器会把过长 / 用 && 串起来的命令切碎（feedback5 · G8：
`grep ... rel_2512\n.15790.md`），连续 5 次工具失败就强制停机，cards.json 永远写不出来。
所以全文模式下，agent 只用这一条短命令在 ft_<ID>.md / rel_<ID>.md 里找可直接复制的逐字引文，
不要再自己写 grep / cat / sed / && 链。

用法（每次一条，不要用 && 串）：
    python3 /workspace/skills/insp-card/scripts/find_quotes.py 2407.04363 2512.15790
    python3 /workspace/skills/insp-card/scripts/find_quotes.py 2407.04363 --terms "memory,retrieval"
    python3 /workspace/skills/insp-card/scripts/find_quotes.py 2407.04363 --sections
    python3 /workspace/skills/insp-card/scripts/find_quotes.py 2407.04363 --section "4 Experiments"

ID 可以是入围论文（读 ft_<ID>.md，并顺带列出它的相关文献 rel_*.md 里的候选引文），
也可以直接是相关文献 ID（读 rel_<ID>.md）。

输出的每条引文都是文件里的原句（只把换行压成空格），校验器按同样的规范化比对，可以原样复制进
source.fulltext_quotes / related_evidence.quote；“§章节”一栏原样复制进 section。
标了【正文·摘要里没有】的引文满足“至少 1 条不在摘要里”的要求。

找不到文件时打印 NOT_FOUND 和搜索过的目录，退出码仍为 0（避免触发平台的连续失败熔断）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_DIRS = [Path("/workspace/inspiration_out"), Path("/workspace/_refs")]
STOP = set("""a an the of and or for to in on with by from as at is are was were be been this that these those
we our us it its their they them can may using use used via into than then such which while within without
based new approach method methods paper results show shows study towards toward between over under more most
""".split())
METHOD_HINT = re.compile(r"method|approach|model|experiment|result|evaluation|limitation|discussion|analysis|setup|"
                         r"ablation|conclusion|implement|benchmark|方法|实验|结果|局限|讨论", re.I)


# ----------------------------------------------------------------------------- text helpers (mirror validate_cards)
def norm_text(s: str) -> str:
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("...", " ").replace("…", " ")
    return " ".join(re.sub(r"[^\w%.'\-]+", " ", s.lower()).split())


def norm_id(x: str) -> str:
    return re.sub(r"v\d+$", "", re.sub(r"^arxiv:", "", str(x).strip(), flags=re.I))


def split_sections(text: str) -> list[tuple[str, str]]:
    parts, cur, buf = [], "", []
    for line in text.splitlines():
        m = re.match(r"^#{1,6} (.+)$", line)
        if m:
            parts.append((cur, "\n".join(buf)))
            cur, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    parts.append((cur, "\n".join(buf)))
    return [(h, b) for h, b in parts if b.strip() or h]


def is_abstract(h: str) -> bool:
    return h.strip().lower() in ("abstract", "摘要")


def is_refs(h: str) -> bool:
    return bool(re.match(r"^(references|bibliography|参考文献|acknowledg)", h.strip().lower()))


SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\"'])")


def sentences(body: str) -> list[str]:
    out = []
    for para in re.split(r"\n\s*\n", body):
        para = " ".join(para.split())
        if not para or para.startswith(("|", "![", "```")):
            continue
        out += [s.strip() for s in SENT_SPLIT.split(para) if s.strip()]
    return out


def clean_enough(s: str) -> bool:
    n = len(s.split())
    if n < 8 or n > 70:
        return False
    junk = sum(1 for ch in s if ch in "\\{}^_$=<>|")
    return junk <= max(2, len(s) // 60)


# ----------------------------------------------------------------------------- locating files
def find_file(name: str, dirs: list[Path]) -> Path | None:
    for d in dirs:
        p = d / name
        if p.is_file():
            return p
    for d in dirs:
        if d.is_dir():
            for p in d.rglob(name):
                if p.is_file():
                    return p
    return None


def load_json(name: str, dirs: list[Path]) -> dict:
    p = find_file(name, dirs)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p else {}
    except Exception:
        return {}


def collect_keywords(obj) -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("matched_keywords", "keywords") and isinstance(v, list):
                out += [str(x) for x in v if isinstance(x, str)]
            else:
                out += collect_keywords(v)
    elif isinstance(obj, list):
        for v in obj:
            out += collect_keywords(v)
    return out


def default_terms(ref: str, tri: dict) -> list[str]:
    terms = []
    for c in tri.get("shortlisted", []) or []:
        if norm_id(c.get("id", "")) == ref:
            terms += collect_keywords(c)
            terms += [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{4,}", c.get("title", "")) if w.lower() not in STOP]
    seen, out = set(), []
    for t in terms:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out[:12]


# ----------------------------------------------------------------------------- core
def pick_quotes(text: str, terms: list[str], abstract: str, k: int) -> tuple[list[dict], list[str]]:
    secs = split_sections(text)
    heads = [h for h, _ in secs if h and not is_refs(h)]
    abs_txt = abstract or "\n".join(b for h, b in secs if is_abstract(h))
    abs_n = norm_text(abs_txt)
    low_terms = [t.lower() for t in terms]
    rows = []
    for h, body in secs:
        if not h or is_abstract(h) or is_refs(h):
            continue
        for s in sentences(body):
            if not clean_enough(s):
                continue
            sl = s.lower()
            hit = [t for t, lt in zip(terms, low_terms) if lt and lt in sl]
            score = 2 * len(hit) + (1 if re.search(r"\d", s) else 0) + (1 if METHOD_HINT.search(h) else 0)
            if not hit and score < 2:
                continue
            ns = norm_text(s)
            rows.append({"section": h, "quote": s, "hits": hit, "score": score,
                         "in_abstract": bool(ns) and ns.strip(".-' ") in abs_n})
    rows.sort(key=lambda r: (-r["score"], r["in_abstract"]))
    picked, used_sec = [], {}
    for r in rows:  # spread over sections: at most 2 per section
        if used_sec.get(r["section"], 0) >= 2:
            continue
        used_sec[r["section"]] = used_sec.get(r["section"], 0) + 1
        picked.append(r)
        if len(picked) >= k:
            break
    return picked, heads


def show_quotes(label: str, path: Path, picked: list[dict]) -> None:
    print(f"\n[{label}] 文件 {path.name}")
    if not picked:
        print("  （没有命中关键词的句子；换 --terms 再试，或用 --sections 看章节后 --section 读一节）")
    for i, r in enumerate(picked, 1):
        tag = "摘要里也有" if r["in_abstract"] else "正文·摘要里没有"
        hit = ",".join(r["hits"]) or "-"
        print(f"  Q{i} §{r['section']} 【{tag}】 命中:{hit}")
        print(f"     \"{r['quote']}\"")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="在 ft_<ID>.md / rel_<ID>.md 里找可直接复制的逐字引文")
    ap.add_argument("ids", nargs="+", help="arXiv ID（入围论文或相关文献），可多个")
    ap.add_argument("--terms", help="逗号分隔的关键词；不给就用 triage.json 里该论文的命中关键词 + 标题词")
    ap.add_argument("--max", type=int, default=6, help="主文每篇最多几条（默认 6）")
    ap.add_argument("--max-related", type=int, default=3, help="每篇相关文献最多几条（默认 3）")
    ap.add_argument("--no-related", action="store_true", help="只看主文，不顺带看相关文献")
    ap.add_argument("--sections", action="store_true", help="只列章节标题")
    ap.add_argument("--section", help="打印某一节正文（截前 --chars 个字符），用来代替 cat / sed")
    ap.add_argument("--chars", type=int, default=4000)
    ap.add_argument("--dir", action="append", type=Path, help="文件目录（默认 /workspace/inspiration_out，再找 /workspace/_refs）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    dirs = a.dir or DEFAULT_DIRS
    tri = load_json("triage.json", dirs)
    idx = load_json("fulltext_index.json", dirs)
    user_terms = [t.strip() for t in (a.terms or "").replace("，", ",").split(",") if t.strip()]
    abstracts = {norm_id(c.get("id", "")): c.get("abstract", "") for c in tri.get("shortlisted", []) or []}
    result, found_any = {}, False

    for raw in a.ids:
        ref = norm_id(raw)
        main_p = find_file(f"ft_{ref}.md", dirs)
        rel_p = None if main_p else find_file(f"rel_{ref}.md", dirs)
        p = main_p or rel_p
        if not p:
            print(f"NOT_FOUND {ref}：没有 ft_{ref}.md / rel_{ref}.md（找过 {', '.join(str(d) for d in dirs)}）。"
                  f"只能引用 prepare 列出的入围论文及其相关文献。")
            continue
        found_any = True
        text = p.read_text(encoding="utf-8", errors="replace")
        if a.sections or a.section:
            heads = [h for h, _ in split_sections(text) if h]
            if a.sections:
                print(f"\n[{ref}] {p.name} 章节（section 字段原样复制其中一个，不能用 Abstract）：")
                for h in heads:
                    print(f"  - {h}")
            if a.section:
                want = norm_text(a.section)
                hit = [(h, b) for h, b in split_sections(text) if h and (norm_text(h) == want or want in norm_text(h))]
                if not hit:
                    print(f"[{ref}] 没有章节「{a.section}」。可用：{' | '.join(heads[:30])}")
                for h, b in hit[:1]:
                    body = " ".join(b.split())
                    print(f"\n[{ref}] §{h}（{len(body)} 字符，显示前 {a.chars}）\n{body[:a.chars]}")
            continue
        terms = user_terms or default_terms(ref, tri)
        picked, heads = pick_quotes(text, terms, abstracts.get(ref, ""), a.max)
        entry = {"file": p.name, "kind": "main" if main_p else "related", "terms": terms,
                 "sections": heads, "quotes": picked, "related": {}}
        if not a.json:
            kind = "入围论文·主文" if main_p else "相关文献"
            print(f"\n=== {ref}（{kind}）关键词：{', '.join(terms) or '（无，按数字/方法章节挑）'}")
            show_quotes(ref, p, picked)
        if main_p and not a.no_related:
            for r in (idx.get(ref) or {}).get("related", []) or []:
                rp = find_file(r.get("file") or f"rel_{r.get('ref')}.md", dirs)
                if not rp:
                    continue
                rq, _ = pick_quotes(rp.read_text(encoding="utf-8", errors="replace"), terms, "", a.max_related)
                entry["related"][r.get("ref")] = {"file": rp.name, "relation_hint": r.get("relation"), "quotes": rq}
                if not a.json:
                    show_quotes(f"相关文献 {r.get('ref')}（{r.get('relation')}）→ related_evidence.ref 写 {r.get('ref')}", rp, rq)
        result[ref] = entry

    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif found_any and not (a.sections or a.section):
        print("\n用法提示：quote 原样复制（不要改词，可用 ... 连接同一文件里的两段）；section 原样复制 § 后面的标题；"
              "每张卡至少 1 条【正文·摘要里没有】。下一步直接写 cards.json，然后跑 finalize。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
