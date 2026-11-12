#!/usr/bin/env python3
"""Full-text evidence for L2 — used ONLY when config "arxiv_fetch_mode" = "fulltext" (default is "abstract").

For every shortlisted arXiv candidate:
  1. fetch the paper's FULL TEXT  (arXiv HTML  https://arxiv.org/html/<id>  → fallback PDF via pypdf / pdftotext)
  2. find RELATED literature      (a) arXiv IDs cited in the paper's own reference list
                                  (b) arXiv search on the paper's key terms
     and fetch THEIR full text too
  3. write a bundle the agent reads (ft_<id>.md, rel_<id>.md, fulltext_index.json) and annotate the candidate with
     cand["fulltext"] so validate_cards.py can check every quote against the text it came from.

Candidates whose full text (or the minimum number of related full texts) cannot be obtained are NOT turned into
cards. They are left unseen and retried next run, up to fulltext.max_retries times, then dropped and reported.

Standard library only; PDF fallback uses `pypdf` if installed (pip install pypdf), else the `pdftotext` binary.
Everything is cached in <cache_dir>/ (text + meta + related list), so re-runs and tests need no network.

    python3 fetch_fulltext.py --ref 2401.04088 --cache-dir inspiration/fulltext          # fetch one paper
    python3 fetch_fulltext.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

UA = "insp-fulltext/1.3 (+inspiration mechanism; polite: 1 request / 3 s)"
ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ID_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d])")
STOP = set("a an the of for and or to in on with by from is are we our this that via as at be its it can not "
           "using towards toward based new approach method methods model models learning".split())
# ----------------------------------------------------------------------------- read mode (v1.4.1)
# One switch for the whole pipeline: config.json → "arxiv_fetch_mode".
#   "abstract" (DEFAULT) : list + abstract only, cards quote the abstract, each card links to the arXiv page. Fast.
#   "fulltext"           : additionally fetch the paper's full text + related papers before writing cards
#                          (≈4–5× longer); cards must quote the body and link to the full text they were read from.
# Anything missing / unknown → "abstract". The legacy key fulltext.required is ignored on purpose.
FETCH_MODES = ("abstract", "fulltext")
DEFAULT_FETCH_MODE = "abstract"


def resolve_fetch_mode(value=None) -> tuple[str, str | None]:
    """Return (mode, warning). Only the exact value "fulltext" (case/space-insensitive) switches full-text on."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_FETCH_MODE, None
    v = str(value).strip().lower()
    if v in FETCH_MODES:
        return v, None
    return DEFAULT_FETCH_MODE, f"arxiv_fetch_mode={value!r} 不认识（只能是 abstract 或 fulltext），已按默认 abstract 运行"


def cfg_fetch_mode(cfg: dict | None) -> tuple[str, str | None]:
    return resolve_fetch_mode((cfg or {}).get("arxiv_fetch_mode"))


def arxiv_links(ref: str, fulltext: dict | None = None) -> dict:
    """Deterministic links for a card (never trust links written by the model)."""
    r = re.sub(r"^(arxiv:|https?://arxiv\.org/(abs|pdf|html)/)", "", str(ref).strip(), flags=re.I).removesuffix(".pdf")
    out = {"url": f"https://arxiv.org/abs/{r}", "pdf_url": f"https://arxiv.org/pdf/{r}"}
    ft = fulltext or {}
    if ft.get("status") == "ok":
        out["read_mode"] = "fulltext"
        out["fulltext_url"] = ft.get("url") or f"https://arxiv.org/html/{norm_id(r)}"
    else:
        out["read_mode"] = "abstract"
    return out


def is_arxiv_ref(ref) -> bool:
    return bool(re.fullmatch(r"(arxiv:)?\d{4}\.\d{4,5}(v\d+)?", str(ref).strip(), flags=re.I))


DEFAULTS = {
    # v1.4.3: full-text mode reads at most this many shortlisted papers per run (top by score); the rest stay
    # unseen and compete again next run. 8 papers + 23 related texts took 48 min and hit TOOL_FAILURE_LIMIT (G8).
    "max_candidates": 4,
    "min_related": 1, "related_cited": 1, "related_search": 2,
    "max_chars_main": 120000, "max_chars_related": 60000, "max_retries": 3,
    "request_interval_s": 3.0, "timeout_s": 40, "min_body_chars": 3000,
}
_last_request = [0.0]


def norm_id(x: str) -> str:
    s = re.sub(r"^(arxiv:|https?://arxiv\.org/(abs|pdf|html)/)", "", str(x).strip(), flags=re.I)
    return re.sub(r"v\d+$", "", s.removesuffix(".pdf"))


# ----------------------------------------------------------------------------- HTTP
def http_get(url: str, cfg: dict, binary: bool = False) -> tuple[int, bytes | str | None]:
    """Polite GET with spacing + backoff on 429/5xx. Returns (status, body); status 0 = network error."""
    for attempt, wait in enumerate((0, 5, 15)):
        if wait:
            time.sleep(wait)
        gap = float(cfg.get("request_interval_s", 3.0)) - (time.time() - _last_request[0])
        if gap > 0:
            time.sleep(gap)
        _last_request[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=float(cfg.get("timeout_s", 40))) as r:
                data = r.read()
                return r.status, data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                continue
            return exc.code, None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 2:
                continue
            return 0, None
    return 0, None


# ----------------------------------------------------------------------------- HTML (arXiv / LaTeXML)
class _LtxText(HTMLParser):
    """LaTeXML HTML → markdown-ish text: headings as '#', paragraphs, list items, captions, math as $alttext$.
    Navigation, scripts, styles and the arXiv chrome are skipped."""
    SKIP = {"script", "style", "nav", "header", "footer", "button", "noscript", "svg"}
    BLOCK = {"p", "div", "li", "figcaption", "caption", "tr", "section", "article", "blockquote", "td", "dd", "dt"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.math = 0
        self.in_doc = False
        self.depth_doc = 0
        self.heading: str | None = None
        self.headings: list[str] = []
        self._hbuf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "article" and "ltx_document" in cls:
            self.in_doc = True
        if not self.in_doc:
            return
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "math":
            alt = a.get("alttext")
            if alt:
                self.out.append(f" ${alt}$ ")
            self.math += 1
            return
        if self.math:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = tag
            self._hbuf = []
            self.out.append("\n\n")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "br":
            self.out.append("\n")

    def handle_endtag(self, tag):
        if not self.in_doc:
            return
        if tag == "article":
            self.in_doc = False
            return
        if tag in self.SKIP and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "math" and self.math:
            self.math -= 1
            return
        if self.math:
            return
        if self.heading and tag == self.heading:
            title = " ".join("".join(self._hbuf).split())
            level = int(tag[1])
            if title:
                self.out.append("#" * max(1, level) + " " + title + "\n")
                self.headings.append(title)
            self.heading = None
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.in_doc or self.skip or self.math:
            return
        if self.heading:
            self._hbuf.append(data)
        else:
            self.out.append(data)


def html_to_text(page: str) -> tuple[str, list[str]]:
    p = _LtxText()
    p.feed(page)
    text = "".join(p.out)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, p.headings


# ----------------------------------------------------------------------------- PDF
def pdf_to_text(data: bytes) -> tuple[str | None, str]:
    try:
        import io

        import pypdf  # type: ignore
        r = pypdf.PdfReader(io.BytesIO(data))
        return "\n\n".join((pg.extract_text() or "") for pg in r.pages), "pdf:pypdf"
    except ImportError:
        pass
    except Exception as exc:  # broken PDF
        return None, f"pdf:pypdf_error:{type(exc).__name__}"
    exe = shutil.which("pdftotext")
    if exe:
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "p.pdf"
            src.write_bytes(data)
            r = subprocess.run([exe, "-layout", str(src), "-"], capture_output=True, timeout=120)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "replace"), "pdf:pdftotext"
    return None, "no_pdf_parser（pip install pypdf）"


HEAD_RE = re.compile(r"^\s*((?:\d+(?:\.\d+)*|[IVX]+)\.?\s+[A-Z][A-Za-z][^\n]{1,80}|Abstract|References|Bibliography|"
                     r"Acknowledg(?:e)?ments?|Appendix[^\n]{0,60}|Conclusions?)\s*$", re.M)


def pdf_headings(text: str) -> tuple[str, list[str]]:
    heads = []
    def mark(m):
        h = " ".join(m.group(1).split())
        heads.append(h)
        return f"\n## {h}\n"
    return HEAD_RE.sub(mark, text), heads


# ----------------------------------------------------------------------------- sections
def split_sections(text: str) -> list[tuple[str, str]]:
    """[(heading, body)], heading '' for text before the first heading."""
    parts: list[tuple[str, str]] = []
    cur, buf = "", []
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
    return bool(re.match(r"^(references|bibliography|参考文献)\b", h.strip().lower()))


def body_chars(text: str) -> int:
    return sum(len(b) for h, b in split_sections(text) if h and not is_abstract(h) and not is_refs(h))


def cited_ids(text: str, self_id: str) -> list[str]:
    refs = "\n".join(b for h, b in split_sections(text) if is_refs(h)) or text[-40000:]
    out: list[str] = []
    for m in ID_RE.finditer(refs):
        i = m.group(1)
        if i != self_id and i not in out:
            out.append(i)
    return out


# ----------------------------------------------------------------------------- fetch one paper
def get_paper(ref: str, cache_dir: Path, cfg: dict) -> dict:
    """Return meta {ref, status, method, url, chars, body_chars, sections, sha256, title?}; text cached in <ref>.md."""
    ref = norm_id(ref)
    cache_dir.mkdir(parents=True, exist_ok=True)
    mf, tf = cache_dir / f"{ref}.json", cache_dir / f"{ref}.md"
    if mf.is_file() and tf.is_file():
        meta = json.loads(mf.read_text(encoding="utf-8"))
        if meta.get("status") == "ok":
            return meta
    meta = {"ref": ref, "status": "unavailable", "method": None, "url": None}
    code, page = http_get(f"https://arxiv.org/html/{ref}", cfg)
    text, heads = None, []
    if code == 200 and isinstance(page, str) and "ltx_document" in page:
        text, heads = html_to_text(page)
        m = re.search(r"<title>([^<]*)</title>", page)
        meta.update(method="html", url=f"https://arxiv.org/html/{ref}", title=html.unescape(m.group(1)).strip() if m else None)
    else:
        meta["html_status"] = code
        code2, data = http_get(f"https://arxiv.org/pdf/{ref}", cfg, binary=True)
        if code2 == 200 and isinstance(data, bytes) and data[:4] == b"%PDF":
            raw, how = pdf_to_text(data)
            meta["method"], meta["url"] = how, f"https://arxiv.org/pdf/{ref}"
            if raw:
                text, heads = pdf_headings(raw)
        else:
            meta["pdf_status"] = code2
    if text:
        bc = body_chars(text)
        meta.update(chars=len(text), body_chars=bc, sections=heads[:80],
                    sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
        if bc >= int(cfg.get("min_body_chars", 3000)):
            meta["status"] = "ok"
            tf.write_text(text, encoding="utf-8")
        else:
            meta["status"] = "too_short"
    mf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def search_related(cand: dict, cfg: dict, cache_dir: Path, k: int) -> list[dict]:
    """arXiv search on the candidate's most distinctive title terms (cached per candidate)."""
    ref = norm_id(cand["id"])
    cf = cache_dir / f"{ref}.related.json"
    if cf.is_file():
        return json.loads(cf.read_text(encoding="utf-8"))
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", cand.get("title", "")) if w.lower() not in STOP]
    words = sorted(dict.fromkeys(words), key=lambda w: -len(w))[:4]
    if not words:
        return []
    q = " AND ".join(f"all:{w}" for w in words[:3])
    url = ARXIV_API + "?" + urllib.parse.urlencode({"search_query": q, "start": 0, "max_results": k + 4, "sortBy": "relevance"})
    code, body = http_get(url, cfg)
    if code != 200 or not isinstance(body, str):
        return []  # not cached: retry next time
    out = []
    for e in ET.fromstring(body).findall(f"{ATOM}entry"):
        rid = norm_id((e.findtext(f"{ATOM}id") or "").rsplit("/", 1)[-1])
        if rid and rid != ref:
            out.append({"ref": rid, "title": " ".join((e.findtext(f"{ATOM}title") or "").split())})
    cf.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ----------------------------------------------------------------------------- shortlist enrichment
def enrich(tri: dict, cache_dir: Path, bundle_dir: Path, cfg: dict, retry_state: dict | None = None,
           log=print) -> dict:
    """Annotate tri['shortlisted'] with full text; move candidates without enough evidence out of the shortlist.
    Returns a report {ok, pending, dropped, retry_state, files}. Mutates tri in place."""
    cfg = dict(DEFAULTS, **(cfg or {}))
    retry = dict(retry_state or {})
    bundle_dir.mkdir(parents=True, exist_ok=True)
    keep, pending, dropped, files, index = [], [], [], [], {}
    cands = list(tri.get("shortlisted", []))
    cap = int(cfg.get("max_candidates") or 0)
    deferred = []
    if cap > 0 and len(cands) > cap:
        cands, deferred = cands[:cap], cands[cap:]
        log(f"FULLTEXT_CAP 全文模式本次只读前 {cap} 篇（按分数），其余 {len(deferred)} 篇留到下次：" +
            ", ".join(norm_id(c["id"]) for c in deferred))
    for cand in cands:
        ref = norm_id(cand["id"])
        main = get_paper(ref, cache_dir, cfg)
        problem = None if main["status"] == "ok" else f"原文不可得（{main['status']}，html={main.get('html_status')} pdf={main.get('pdf_status')}，{main.get('method')}）"
        related: list[dict] = []
        if not problem:
            text = (cache_dir / f"{ref}.md").read_text(encoding="utf-8")
            pool = [{"ref": r, "relation": "cited", "title": None} for r in cited_ids(text, ref)[: int(cfg["related_cited"]) + 3]]
            pool += [dict(x, relation="search") for x in search_related(cand, cfg, cache_dir, int(cfg["related_search"]))]
            want = {"cited": int(cfg["related_cited"]), "search": int(cfg["related_search"])}
            got = {"cited": 0, "search": 0}
            seen = {ref}
            for r in pool:
                if r["ref"] in seen or got[r["relation"]] >= want[r["relation"]]:
                    continue
                seen.add(r["ref"])
                m = get_paper(r["ref"], cache_dir, cfg)
                if m["status"] == "ok":
                    got[r["relation"]] += 1
                    related.append({"ref": r["ref"], "title": r.get("title") or m.get("title"), "relation": r["relation"],
                                    "method": m["method"], "url": m["url"], "chars": m["chars"], "sha256": m["sha256"],
                                    "file": f"rel_{r['ref']}.md"})
            if len(related) < int(cfg["min_related"]):
                problem = f"相关文献原文不足（拿到 {len(related)} 篇，至少需要 {cfg['min_related']} 篇）"
        if problem:
            n = int(retry.get(ref, 0)) + 1
            retry[ref] = n
            row = {"ref": ref, "title": cand.get("title"), "reason": problem, "tries": n}
            if n >= int(cfg["max_retries"]):
                dropped.append(row)
                log(f"WARN 原文核验：放弃 {ref}（已尝试 {n} 次）：{problem}")
            else:
                pending.append(row)
                log(f"WARN 原文核验：{ref} 暂不出卡，下次重试（{n}/{cfg['max_retries']}）：{problem}")
            continue
        retry.pop(ref, None)
        lim_m, lim_r = int(cfg["max_chars_main"]), int(cfg["max_chars_related"])
        mt = (cache_dir / f"{ref}.md").read_text(encoding="utf-8")
        (bundle_dir / f"ft_{ref}.md").write_text(mt[:lim_m], encoding="utf-8")
        files.append(bundle_dir / f"ft_{ref}.md")
        for r in related:
            rt = (cache_dir / f"{r['ref']}.md").read_text(encoding="utf-8")
            (bundle_dir / r["file"]).write_text(rt[:lim_r], encoding="utf-8")
            if bundle_dir / r["file"] not in files:
                files.append(bundle_dir / r["file"])
        cand["fulltext"] = {"status": "ok", "file": f"ft_{ref}.md", "method": main["method"], "url": main["url"],
                            "chars": min(main["chars"], lim_m), "truncated": main["chars"] > lim_m,
                            "sections": main.get("sections", []), "sha256": main["sha256"], "related": related}
        index[ref] = cand["fulltext"]
        keep.append(cand)
    tri["shortlisted"] = keep
    gone = {r["ref"] for r in pending} | {norm_id(c["id"]) for c in deferred}
    tri["new_ids"] = [i for i in tri.get("new_ids", []) if norm_id(i) not in gone]  # pending → stay unseen, retried
    c = tri.setdefault("counts", {})
    c.update(fulltext_ok=len(keep), fulltext_pending=len(pending), fulltext_dropped=len(dropped),
             fulltext_deferred=len(deferred))
    c["shortlisted"] = len(keep)
    if not keep and not tri.get("observed_shortlist"):  # v1.5: observed candidates do not need any full text
        tri["status"] = "NO_CANDIDATES"
    (bundle_dir / "fulltext_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    files.append(bundle_dir / "fulltext_index.json")
    return {"ok": [norm_id(c["id"]) for c in keep], "pending": pending, "dropped": dropped,
            "deferred": [norm_id(c["id"]) for c in deferred], "retry_state": retry, "files": files}


def load_texts(tri: dict, bundle_dir: Path) -> dict:
    """{ref: {"main": text, "related": {rref: text}, "abstract": str}} for validate_cards."""
    out = {}
    for cand in tri.get("shortlisted", []):
        ft = cand.get("fulltext") or {}
        if ft.get("status") != "ok":
            continue
        ref = norm_id(cand["id"])
        rel = {}
        for r in ft.get("related", []):
            p = bundle_dir / r["file"]
            if p.is_file():
                rel[r["ref"]] = p.read_text(encoding="utf-8")
        p = bundle_dir / ft["file"]
        if p.is_file():
            out[ref] = {"main": p.read_text(encoding="utf-8"), "related": rel}
    return out


# ----------------------------------------------------------------------------- self-test
SAMPLE_HTML = """<html><head><title>Mixture Gating for Agents</title><script>var x=1;</script></head><body>
<nav>arXiv chrome</nav><article class="ltx_document"><h1 class="ltx_title">Mixture Gating for Agents</h1>
<div class="ltx_abstract"><h6 class="ltx_title">Abstract</h6><p class="ltx_p">We gate memories.</p></div>
<section class="ltx_section"><h2 class="ltx_title">1 Introduction</h2><p class="ltx_p">Recall gating reduces cost by
<math alttext="\\Lambda=3" class="ltx_Math"><mi>Λ</mi></math> tokens on average.</p></section>
<section class="ltx_section"><h2 class="ltx_title">4 Experiments</h2><p class="ltx_p">""" + ("Gated recall keeps the solve rate while cutting the injected context. " * 60) + """</p></section>
<section class="ltx_bibliography"><h2 class="ltx_title">References</h2><ul><li class="ltx_bibitem">A. Author. Naive RAG. arXiv:2610.30755, 2026.</li>
<li class="ltx_bibitem">B. Author. Old. arXiv preprint arXiv:2401.04088v2.</li></ul></section></article><footer>x</footer></body></html>"""


def self_test() -> int:
    text, heads = html_to_text(SAMPLE_HTML)
    assert "arXiv chrome" not in text and "var x" not in text, text[:200]
    assert "$\\Lambda=3$" in text and "## 1 Introduction" in text, text[:400]
    assert heads[:2] == ["Mixture Gating for Agents", "Abstract"], heads
    secs = dict(split_sections(text))
    assert "4 Experiments" in secs and is_refs("References") and is_abstract("Abstract")
    assert cited_ids(text, "2610.99999") == ["2610.30755", "2401.04088"], cited_ids(text, "x")
    assert body_chars(text) > 3000
    pdf_t, heads2 = pdf_headings("Title\nAbstract\nwe do x\n1 Introduction\nbody body\nReferences\n[1] arXiv:2401.04088\n")
    assert heads2 == ["Abstract", "1 Introduction", "References"], heads2
    with tempfile.TemporaryDirectory() as tmp:
        cache, bundle = Path(tmp) / "cache", Path(tmp) / "bundle"
        cache.mkdir()
        for r, n in (("2610.11111", 6000), ("2610.30755", 5000), ("2401.04088", 5000)):
            body = f"# T {r}\n## Abstract\nshort\n## 2 Method\n" + ("word " * (n // 5)) + "\n## References\narXiv:2610.30755\narXiv:2401.04088\n"
            (cache / f"{r}.md").write_text(body, encoding="utf-8")
            (cache / f"{r}.json").write_text(json.dumps({"ref": r, "status": "ok", "method": "html", "url": "u", "chars": len(body),
                                                         "sha256": "x", "sections": ["Abstract", "2 Method"], "title": r}), encoding="utf-8")
        (cache / "2610.11111.related.json").write_text(json.dumps([{"ref": "2401.04088", "title": "Mixtral"}]), encoding="utf-8")
        (cache / "2610.22222.json").write_text(json.dumps({"ref": "2610.22222", "status": "unavailable"}), encoding="utf-8")
        tri = {"status": "OK", "counts": {"shortlisted": 2}, "new_ids": ["2610.11111", "2610.22222"],
               "shortlisted": [{"id": "2610.11111v1", "title": "Gating"}, {"id": "2610.22222", "title": "Missing"}]}
        global http_get
        real = http_get
        http_get = lambda url, cfg, binary=False: (404, None)  # noqa: E731  (no network in self-test)
        try:
            rep = enrich(tri, cache, bundle, {"related_cited": 1, "related_search": 1, "max_retries": 2}, {}, log=lambda *a: None)
        finally:
            http_get = real
        ft = tri["shortlisted"][0]["fulltext"]
        assert [c["id"] for c in tri["shortlisted"]] == ["2610.11111v1"], tri
        assert [r["relation"] for r in ft["related"]] == ["cited", "search"], ft["related"]
        assert rep["pending"][0]["ref"] == "2610.22222" and tri["new_ids"] == ["2610.11111"], rep
        assert rep["retry_state"] == {"2610.22222": 1}
        texts = load_texts(tri, bundle)
        assert set(texts["2610.11111"]["related"]) == {"2610.30755", "2401.04088"}
        tri2 = {"new_ids": ["2610.22222"], "shortlisted": [{"id": "2610.22222", "title": "Missing"}]}
        http_get = lambda url, cfg, binary=False: (404, None)  # noqa: E731
        try:
            rep2 = enrich(tri2, cache, bundle, {"max_retries": 2}, rep["retry_state"], log=lambda *a: None)
        finally:
            http_get = real
        assert rep2["dropped"] and tri2["new_ids"] == ["2610.22222"] and tri2["status"] == "NO_CANDIDATES", rep2
    print("SELF-TEST OK: fetch_fulltext")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取 arXiv 论文原文（HTML 优先，PDF 兜底）")
    ap.add_argument("--ref")
    ap.add_argument("--cache-dir", type=Path, default=Path("inspiration/fulltext"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.ref:
        ap.error("需要 --ref 或 --self-test")
    m = get_paper(a.ref, a.cache_dir, DEFAULTS)
    print(json.dumps({k: v for k, v in m.items() if k != "sections"}, ensure_ascii=False, indent=2))
    print("sections:", m.get("sections"))
    if m.get("status") == "ok":
        print("cited arXiv:", cited_ids((a.cache_dir / f"{norm_id(a.ref)}.md").read_text(encoding="utf-8"), norm_id(a.ref))[:10])
    return 0 if m.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
