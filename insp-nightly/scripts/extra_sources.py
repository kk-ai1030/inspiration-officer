#!/usr/bin/env python3
"""B2 可插拔的 L0 候选来源（arXiv 之外），默认关闭。

所有来源产出与 arXiv 完全相同的 raw item：
    {id, title, abstract, url, published, source}
再由 triage.run_triage 统一做 norm_id 去重 / 打分 / 配额，不新增 counts 键、不改 COUNTS 格式。

开关放在状态目录的 state.json（沙箱 prepare 只读 state，不读本机 config.json）：

    "candidate_sources": {
      "enabled": true,
      "arxiv": true,
      "providers": [
        {"kind": "rss", "url": "https://example.org/feed.xml", "limit": 20},
        {"kind": "openalex", "query": "surface code decoder", "limit": 20},
        {"kind": "semantic_scholar", "query": "neutral atom", "limit": 20},
        {"kind": "fixture", "path": "extra_sources.json"}
      ]
    }

- enabled=false 或缺省该段：完全不联网、不产出，arXiv 路径逐字不变（E10）。
- arxiv=false：本轮只走额外来源，跳过 arXiv 可达性检查与抓取。
- --extra-raw <file>：离线/本地路线直接读一份 raw 条目文件，隐含只走额外来源。
- 所有 ID 带来源前缀，url 必填；跨源同 DOI/URL 在合入前先去掉。

Usage:
    python3 extra_sources.py --state inspiration/state.json --list
    python3 extra_sources.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

PREFIX = {"fixture": "fx", "rss": "rss", "openalex": "oa", "semantic_scholar": "s2"}
DEFAULT = {"enabled": False, "arxiv": True, "providers": []}
TIMEOUT = 15


def plan(state: dict) -> dict:
    cfg = state.get("candidate_sources") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    providers = cfg.get("providers") or []
    return {"enabled": bool(cfg.get("enabled", DEFAULT["enabled"])),
            "arxiv": bool(cfg.get("arxiv", DEFAULT["arxiv"])),
            "providers": [p for p in providers if isinstance(p, dict)]}


def extra_only(state: dict, fixture=None) -> bool:
    """True = 本轮不抓 arXiv，只走额外来源（避免 arxiv_reachable 误判 FETCH_BLOCKED）。"""
    if fixture is not None:
        return True
    cfg = plan(state)
    return cfg["enabled"] and not cfg["arxiv"] and bool(cfg["providers"])


def _clean(text) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    return re.sub(r"\s+", " ", s).strip()


def _date(value) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else (f"{s}-01-01" if re.fullmatch(r"\d{4}", s) else "")


def _url_key(url: str) -> str:
    """跨源去重键：DOI 用 DOI 本身，普通链接去掉协议 / www / 查询串 / 末尾斜杠。"""
    u = str(url or "").strip().lower()
    m = re.search(r"doi\.org/(10\.\S+)", u)
    if m:
        return "doi:" + m.group(1).rstrip("/")
    u = re.sub(r"^https?://(www\.)?", "", u).split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def _item(raw_id, title, abstract, url, published, source: str) -> dict | None:
    prefix = PREFIX.get(source)
    if not prefix or not raw_id or not url:
        return None
    rid = str(raw_id).strip()
    if not rid.startswith(prefix + ":"):
        rid = f"{prefix}:{rid}"
    title, abstract = _clean(title), _clean(abstract)
    if not title or not abstract:
        return None
    return {"id": rid, "title": title, "abstract": abstract, "url": str(url).strip(),
            "published": _date(published), "source": source}


def _default_fetch(url: str, timeout: int = TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "inspiration-officer/1.5.9 (+local research assistant)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _children(el, name: str):
    return [c for c in el if _local_name(c.tag) == name.lower()]


def _text_of(el, name: str) -> str:
    for c in _children(el, name):
        return "".join(c.itertext()).strip()
    return ""


def parse_rss(xml_text: str, limit: int = 20) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for el in root.iter():
        if _local_name(el.tag) not in ("item", "entry"):
            continue
        title = _text_of(el, "title")
        abstract = _text_of(el, "description") or _text_of(el, "summary") or _text_of(el, "content")
        url = _text_of(el, "link")
        if not url:
            for c in _children(el, "link"):
                url = c.get("href") or c.get("url") or ""
                if url:
                    break
        published = _text_of(el, "pubDate") or _text_of(el, "published") or _text_of(el, "updated")
        key = url or title
        it = _item("rss_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], title, abstract,
                   url, published, "rss")
        if it:
            out.append(it)
        if len(out) >= limit:
            break
    return out


def _inverted_abstract(inv) -> str:
    if not isinstance(inv, dict):
        return ""
    pairs = []
    for word, positions in inv.items():
        for p in positions or []:
            try:
                pairs.append((int(p), str(word)))
            except (TypeError, ValueError):
                continue
    return " ".join(w for _, w in sorted(pairs)[:300])


def parse_openalex(obj: dict, limit: int = 20) -> list[dict]:
    out = []
    for w in (obj.get("results") or [])[:limit]:
        wid = str(w.get("id") or "").rsplit("/", 1)[-1]
        url = w.get("doi") or w.get("id") or ""
        abstract = _inverted_abstract(w.get("abstract_inverted_index"))
        it = _item(wid, w.get("title") or w.get("display_name"), abstract, url,
                   w.get("publication_date"), "openalex")
        if it:
            out.append(it)
    return out


def parse_semantic_scholar(obj: dict, limit: int = 20) -> list[dict]:
    out = []
    for p in (obj.get("data") or [])[:limit]:
        pid = p.get("paperId") or ""
        url = p.get("url") or ""
        if not url and (p.get("externalIds") or {}).get("DOI"):
            url = "https://doi.org/" + p["externalIds"]["DOI"]
        it = _item(pid, p.get("title"), p.get("abstract"), url, p.get("publicationDate") or p.get("year"),
                   "semantic_scholar")
        if it:
            out.append(it)
    return out


def _read_items(path: Path, prefix: str = "fixture") -> list[dict]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    rows = obj.get("items", []) if isinstance(obj, dict) else obj
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        source = "fixture" if prefix == "fixture" else prefix
        it = _item(r.get("id"), r.get("title"), r.get("abstract"), r.get("url"),
                   r.get("published"), r.get("source") if r.get("source") in PREFIX else source)
        if it:
            out.append(it)
    return out


def _resolve(path: str, base_dirs) -> Path:
    p = Path(path)
    if p.is_file():
        return p
    for base in base_dirs:
        q = Path(base) / p
        if q.is_file():
            return q
    return p


def collect(state: dict, *, fixture=None, fetcher=None, base_dirs=(), notes: list | None = None) -> list[dict]:
    """返回规范化的额外候选；失败只跳过该来源，绝不中断整轮。"""
    notes = notes if notes is not None else []
    fetch = fetcher or _default_fetch
    if fixture is not None:
        try:
            return _dedup(_read_items(Path(fixture)), notes)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            notes.append(f"extra-raw 读取失败：{exc}")
            return []
    cfg = plan(state)
    if not cfg["enabled"]:
        return []
    items: list[dict] = []
    for p in cfg["providers"]:
        kind = str(p.get("kind") or "")
        limit = max(1, int(p.get("limit") or 20))
        try:
            if kind == "fixture":
                items += _read_items(_resolve(str(p.get("path") or ""), base_dirs))
            elif kind == "rss":
                items += parse_rss(fetch(str(p.get("url") or "")), limit)
            elif kind == "openalex":
                q = urllib.parse.quote(str(p.get("query") or ""))
                url = f"https://api.openalex.org/works?search={q}&per-page={limit}"
                items += parse_openalex(json.loads(fetch(url)), limit)
            elif kind == "semantic_scholar":
                q = urllib.parse.quote(str(p.get("query") or ""))
                url = ("https://api.semanticscholar.org/graph/v1/paper/search?"
                       f"query={q}&limit={limit}&fields=paperId,title,abstract,url,year,publicationDate,externalIds")
                items += parse_semantic_scholar(json.loads(fetch(url)), limit)
            else:
                notes.append(f"未知来源类型 {kind}，已跳过")
        except Exception as exc:  # noqa: BLE001 - best-effort: one broken provider must not kill the run
            notes.append(f"来源 {kind} 抓取失败，已跳过：{type(exc).__name__}: {str(exc)[:120]}")
    return _dedup(items, notes)


def _dedup(items: list[dict], notes: list) -> list[dict]:
    out, seen, dropped = [], set(), 0
    for it in items:
        key = _url_key(it["url"])
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(it)
    if dropped:
        notes.append(f"额外来源内部去掉 {dropped} 条同 DOI / URL 的重复条目。")
    return out


def self_test() -> int:
    abstract = " ".join(["measurement"] * 35)
    rss = f"""<rss version="2.0"><channel><item><title>Neutral atom array</title>
      <description>{abstract}</description><link>https://example.org/a</link>
      <pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    oa = {"results": [{"id": "https://openalex.org/W1", "title": "Decoder study", "doi": "https://doi.org/10.1/xyz",
                       "publication_date": "2026-11-10", "abstract_inverted_index": {"word": list(range(35))}}]}
    s2 = {"data": [{"paperId": "abc", "title": "Rydberg gates", "abstract": abstract,
                    "url": "https://example.org/b", "year": 2026}]}

    def fake(url):
        if "openalex" in url:
            return json.dumps(oa)
        if "semanticscholar" in url:
            return json.dumps(s2)
        return rss

    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:200]) if detail and not cond else ""))
        ok &= bool(cond)

    check("缺省关闭且不联网", collect({"sources": []}, fetcher=lambda u: (_ for _ in ()).throw(AssertionError("联网了"))) == [])
    check("arxiv 默认仍开", plan({})["arxiv"] is True)
    state = {"candidate_sources": {"enabled": True, "providers": [
        {"kind": "rss", "url": "https://feed"}, {"kind": "openalex", "query": "x"},
        {"kind": "semantic_scholar", "query": "y"}, {"kind": "mystery"}]}}
    notes: list = []
    items = collect(state, fetcher=fake, notes=notes)
    check("三种适配器都产出", len(items) == 3 and {i["source"] for i in items} ==
          {"rss", "openalex", "semantic_scholar"}, str(items))
    check("ID 带来源前缀", all(i["id"].startswith(PREFIX[i["source"]] + ":") for i in items))
    check("url 非空、published 归一", all(i["url"] and i["published"].startswith("2026") for i in items), str(items))
    check("未知来源只记提示", any("未知来源类型" in n for n in notes))
    check("OpenAlex 摘要还原", any(len(i["abstract"].split()) >= 30 for i in items if i["source"] == "openalex"))
    check("extra_only 只在关掉 arxiv 时为真", not extra_only(state) and
          extra_only({"candidate_sources": {"enabled": True, "arxiv": False, "providers": [{"kind": "rss"}]}}))
    dup = {"candidate_sources": {"enabled": True, "providers": [{"kind": "rss", "url": "https://feed"},
                                                                   {"kind": "rss", "url": "https://feed"}]}}
    check("同 URL 跨来源去重", len(collect(dup, fetcher=fake)) == 1)
    bad = {"candidate_sources": {"enabled": True, "providers": [{"kind": "rss", "url": "https://boom"}]}}
    broken = collect(bad, fetcher=lambda u: (_ for _ in ()).throw(OSError("offline")))
    check("单个来源失败不中断", broken == [])
    print("SELF-TEST OK: extra_sources" if ok else "SELF-TEST FAILED: extra_sources")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", type=Path, default=Path("inspiration/state.json"))
    ap.add_argument("--fixture", type=Path)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    state = json.loads(a.state.read_text(encoding="utf-8")) if a.state.is_file() else {}
    notes: list = []
    print(json.dumps({"plan": plan(state), "items": collect(state, fixture=a.fixture, notes=notes),
                      "notes": notes}, ensure_ascii=False, indent=2) if a.list else
          json.dumps(collect(state, fixture=a.fixture, notes=notes), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
