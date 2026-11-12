"""Local paper library → interest profile.

Reads .bib / .ris entries (title, keywords, abstract, eprint), PDF titles (pypdf when installed: metadata + first
page; otherwise the /Title field in the file and the file name), and Markdown / txt reading notes in the papers
folder. Produces:
  inspiration/library/papers.json   papers found, arXiv IDs, top terms, suggested keywords / arXiv query
  state.json → library_arxiv_ids    papers you already have are never pushed as "new" (observe.papers.mark_owned_as_seen)
  one "process" finding when frequent topics of your library are missing from the arXiv keywords (interest gap)
Keywords are merged into state.json only on request: python platform/observe.py papers --apply-keywords
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .common import Env, finding, read_text

ARXIV_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d])")
OLD_ARXIV_RE = re.compile(r"\b((?:hep-th|hep-ph|hep-lat|hep-ex|quant-ph|cond-mat(?:\.[a-z-]+)?|gr-qc|astro-ph(?:\.[A-Z]{2})?|"
                          r"math-ph|nucl-th|physics(?:\.[a-z-]+)?)/\d{7})\b")
STOP = set("""a an the of for and or to in on with by from is are was were be been being we our this that these those via
as at into using use used based study studies approach approaches method methods analysis new towards toward its their
it can not no than then also via within without between under over through about across more most less very high low
paper papers results result effect effects case cases first one two three non type types different large small show
shows shown give gives given general simple novel recent role nature science physical review letters journal vol
arxiv preprint doi http https www org pdf et al proceedings conference supplementary material version""".split())
CJK_STOP = set("我们 一种 研究 基于 方法 分析 及其 以及 问题 进行 应用 一个 论文 文献 中的 的研 究的 关于 通过 对于 利用 系统 结果".split())


def _bib_entries(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"@(\w+)\s*\{", text):
        start, depth, i = m.end(), 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        body = text[start:i - 1]
        fields = {}
        for fm in re.finditer(r"(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\"|\w+)", body):
            fields[fm.group(1).lower()] = re.sub(r"[{}\"]", "", fm.group(2)).strip()
        if fields:
            out.append(fields)
    return out


def _ris_entries(text: str) -> list[dict]:
    out, cur = [], {}
    for ln in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])  - (.*)$", ln)
        if not m:
            continue
        tag, val = m.groups()
        if tag == "ER":
            out.append(cur)
            cur = {}
        elif tag in ("TI", "T1"):
            cur["title"] = val
        elif tag == "KW":
            cur["keywords"] = (cur.get("keywords", "") + ", " + val).strip(", ")
        elif tag == "AB":
            cur["abstract"] = val
        elif tag in ("UR", "DO", "M3"):
            cur.setdefault("url", val)
    return out


def _pdf_info(p: Path) -> tuple[str, str]:
    """(title, first-page text). pypdf if available; otherwise the raw /Title entry."""
    try:
        import pypdf  # type: ignore
        r = pypdf.PdfReader(str(p))
        title = str((r.metadata or {}).get("/Title") or "").strip()
        first = (r.pages[0].extract_text() or "")[:2500] if r.pages else ""
        return title, first
    except Exception:  # noqa: BLE001 - not installed / damaged / encrypted
        pass
    try:
        with p.open("rb") as f:
            raw = f.read(400_000)
        m = re.search(rb"/Title\s*\(((?:\\\)|[^)]){3,300})\)", raw)
        if m:
            t = m.group(1).decode("latin-1", errors="ignore")
            if not t.startswith("\xfe\xff"):
                return t.strip(), ""
    except OSError:
        pass
    return "", ""


def _terms(text: str) -> set[str]:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text.lower()) if w not in STOP and len(w) > 2]
    out = set(words)
    out |= {f"{a} {b}" for a, b in zip(words, words[1:]) if a != b}
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        out |= {seg[i:i + 2] for i in range(len(seg) - 1)} - CJK_STOP
    return out


def scan(env: Env, manifest: dict) -> tuple[list[dict], dict]:
    cfg = env.cfg["papers"]
    roots = {r["label"]: r["path"] for r in env.roots()}
    papers: list[dict] = []
    n_pdf = 0
    for f in manifest.get("files", []):
        if env.over_budget():
            break
        p = roots[f["root"]] / f["rel"]
        if f["cls"] == "paper" and f["ext"] in (".bib", ".ris"):
            text = read_text(p, 5_000_000) or ""
            for e in (_bib_entries(text) if f["ext"] == ".bib" else _ris_entries(text)):
                blob = " ".join(e.get(k, "") for k in ("eprint", "url", "arxiv", "archiveprefix", "doi", "note", "journal"))
                ids = ARXIV_RE.findall(blob) + OLD_ARXIV_RE.findall(blob)
                if e.get("title"):
                    papers.append({"ref": f["ref"], "title": e["title"], "keywords": e.get("keywords", ""),
                                   "abstract": e.get("abstract", "")[:1500], "arxiv": ids, "via": f["ext"][1:]})
        elif f["cls"] == "paper" and f["ext"] == ".pdf":
            n_pdf += 1
            if n_pdf > int(cfg["max_pdf"]):
                continue
            title, first = _pdf_info(p)
            stem = re.sub(r"[_\-]+", " ", Path(f["rel"]).stem)
            ids = ARXIV_RE.findall(f["rel"]) + OLD_ARXIV_RE.findall(f["rel"].replace("_", "/")) + ARXIV_RE.findall(first[:600])
            if not title or len(title) < 8 or re.fullmatch(r"(?i)(untitled|microsoft word.*|document\d*|main|paper)", title):
                title = stem
            papers.append({"ref": f["ref"], "title": title.strip()[:300], "keywords": "", "abstract": first[:1500],
                           "arxiv": sorted(set(ids)), "via": "pdf"})
        elif f["cls"] == "paper_note":
            text = read_text(p, 400_000) or ""
            head = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.strip()), Path(f["rel"]).stem)
            papers.append({"ref": f["ref"], "title": head[:300], "keywords": "", "abstract": text[:1500],
                           "arxiv": sorted(set(ARXIV_RE.findall(text))), "via": "note"})
    df: dict[str, int] = {}
    for pp in papers:
        for t in _terms(" ".join([pp["title"], pp["title"], pp["keywords"], pp["abstract"][:600]])):
            df[t] = df.get(t, 0) + 1
    min_df = 2 if len(papers) >= 4 else 1
    ranked = sorted(((t, n) for t, n in df.items() if n >= min_df), key=lambda kv: (-kv[1] * (1.6 if " " in kv[0] else 1), kv[0]))
    top: list[tuple[str, int]] = []
    for t, n in ranked:  # prefer "spin liquid" over its parts when both are equally frequent
        if any(t in u and u != t and nu >= n for u, nu in top):
            continue
        top = [(u, nu) for u, nu in top if not (u in t and nu <= n)] + [(t, n)]
        if len(top) >= int(cfg["top_terms"]):
            break
    ids = sorted({i for pp in papers for i in pp["arxiv"]})
    sugg = [t for t, _ in top[:8]]
    query = " OR ".join(f'abs:"{t}"' for t in sugg[:5]) if sugg else None
    rep = {"generated_at": datetime.now().isoformat(timespec="seconds"), "n_papers": len(papers),
           "top_terms": [{"term": t, "papers": n} for t, n in top], "arxiv_ids": ids, "suggested_keywords": sugg,
           "suggested_query": query, "papers": [{k: v for k, v in pp.items() if k != "abstract"} for pp in papers[:2000]]}
    env.write_json("papers.json", rep)

    findings: list[dict] = []
    st_path = env.state_dir / "state.json"
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        st = None
    if st is not None and ids and cfg.get("mark_owned_as_seen", True) and sorted(st.get("library_arxiv_ids", [])) != ids:
        st["library_arxiv_ids"] = ids
        tmp = st_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(st_path)
    if st is not None and len(papers) >= 3:
        have = " ".join([*map(str, st.get("keywords", [])), *[str(s.get("query", "")) for s in st.get("sources", [])]]).lower()
        gap = [(t, n) for t, n in top if n >= max(3, len(papers) // 5) and t.lower() not in have][:3]
        if gap:
            lines = []
            for t, n in gap:
                titles = [pp["title"] for pp in papers if t in " ".join([pp["title"], pp["keywords"], pp["abstract"][:600]]).lower()
                          or t in pp["title"]][:3]
                lines.append(f"主题“{t}”：本地 {n} 篇，例如\n" + "\n".join(f"  - {x[:110]}" for x in titles))
            ev = (f"论文库扫描 · 共 {len(papers)} 篇（bib / ris / pdf / 阅读笔记）\n当前 arXiv 关键词：{', '.join(map(str, st.get('keywords', []))) or '（空）'}\n"
                  f"本地高频但检索式里没有的主题：\n" + "\n".join(lines))
            findings.append(finding(
                "papers.interest_gap", "workspace", "process", f"论文库里常见的“{gap[0][0]}”等主题不在 arXiv 检索式里",
                ev, 2.6 + 0.3 * len(gap), fp="|".join(sorted(t for t, _ in gap)), allowed=["process", "knowledge"],
                ref="library://papers", subfield="papers",
                summary="你本地收藏的论文主题和灵感机制的 arXiv 检索方向不一致：可能漏掉你真正关注方向的新进展。",
                metrics={"papers": len(papers), "gap_terms": [t for t, _ in gap]}))
    return findings, {"papers": len(papers), "arxiv_ids": len(ids), "top": sugg[:5], "findings": len(findings)}


def apply_keywords(env: Env, max_keywords: int = 12) -> list[str]:
    rep = env.read_json("papers.json", {}) or {}
    st_path = env.state_dir / "state.json"
    st = json.loads(st_path.read_text(encoding="utf-8"))
    old = [str(k) for k in st.get("keywords", [])]
    merged = list(dict.fromkeys(old + [t for t in rep.get("suggested_keywords", []) if t.lower() not in {o.lower() for o in old}]))
    st["keywords"] = merged[:max(max_keywords, len(old))]
    st_path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return [k for k in st["keywords"] if k not in old]
