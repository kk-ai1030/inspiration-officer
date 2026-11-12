#!/usr/bin/env python3
"""灵感运行 · 沙箱内一键脚本（v1.4 自举分支）。由灵感官 agent 在 SciencePal 沙箱里执行，用户什么都不用上传。

    python3 /workspace/skills/insp-nightly/scripts/insp_run.py prepare  [--query Q] [--keywords "a,b"] [--fetch-mode abstract|fulltext]
    （agent 读 /workspace/inspiration_out/triage.json（fulltext 模式另读 ft_*.md / rel_*.md），按 insp-card 写 cards.json）

论文读取模式 --fetch-mode（v1.4.1；对应本地 config.json 的 "arxiv_fetch_mode"）：
  abstract（默认，不传就是它）：只用 arXiv 列表里的标题+摘要出卡，卡片附 arXiv 摘要页链接。快。
  fulltext：额外逐篇抓原文和相关文献原文，卡片必须引用正文，并附读过的全文链接。约慢 4–5 倍。
    python3 /workspace/skills/insp-nightly/scripts/insp_run.py finalize

prepare：找状态 → 拉 arXiv 列表（export.arxiv.org，429 退避）→ 增量 / 去重 / 粗打分 → triage.json
         → [仅 fulltext 模式] 逐篇抓原文 ft_<id>.md 与相关文献 rel_<id>.md → 全部落到 /workspace/inspiration_out/
finalize：校验 cards.json（字段、原文逐字引文、配额）→ 写 bundle.json → 更新本会话的状态
         → 打印 <<<INSP_BUNDLE_JSON ... INSP_BUNDLE_JSON>>> 结果块（本地“收货员”据此自动入库）

状态从哪来（按优先级）：
  1. 附件 /workspace/_refs/**/state.json（老的“附件模式”，本地兜底流程会自动上传）
  2. /workspace/inspiration_state/state.json（同一会话上次运行留下的，或收货员同步进来的）
  2b. /workspace/state.json（直接传到工作区根目录的状态；同目录的 negative.jsonl 等一并读取）
  2c. 技能自带的本地快照 assets/local_state/（v1.5.5：本地评价页 / 收货员 / demo_reset 发布；网页对话入口靠它拿到
      本地观察 observed.jsonl 与已推送清单）。比 2 新时优先于 2
  3. 技能自带模板（首次运行 = bootstrap），检索式用 --query / --keywords（来自用户画像记忆）覆盖

v1.5.9：arXiv 论文卡（B 类为主）回到 v1.4.6 的写法——读 triage.json 的 shortlisted（同内容小文件 triage_shortlisted.json），
        一次写成 /workspace/inspiration_out/cards.json；观察型卡片（A 类等）仍是 1.5.7 的 cards/NN.json 单卡文件。
        finalize 合并两处，合并结果写 cards_final.json（输出，不回读）。
退出码：0 正常（含 NO_CANDIDATES）· 2 卡片没通过校验（论文卡改 cards.json，观察型卡改 cards/NN.json，然后重跑 finalize）
        · 3 状态缺失 · 4 沙箱连不上 arXiv（FETCH_BLOCKED，收货员会自动改走本地抓取）
只用 Python 标准库（3.9+）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

RUNNER_VERSION = "insp-run/1.5.9"
BUNDLE_VERSION = "insp-bundle/1.0"
HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
VENDOR = HERE / "vendor"
TEMPLATE = SKILL / "assets" / "state_template"
# v1.5.5 (P11): the local machine publishes its latest state (incl. observed.jsonl = the A-class channel) INTO this skill,
# so a conversation started in the web UI ("主动探索灵感" without the review-page button) sees the same state
SNAPSHOT = SKILL / "assets" / "local_state"
BLOCK_START, BLOCK_END = "<<<INSP_BUNDLE_JSON", "INSP_BUNDLE_JSON>>>"
# v1.4.5 (feedback6): the platform does NOT require the complete tool; calling it with a sentence in
# attachments got the run force-stopped ("任务已中断") long after the cards were delivered.
CLOSING_HINT = ("===== 收尾：把上面的结果块原样放进最终回答后直接结束。不要调用 complete 工具，"
                "不要放附件（结果块是正文，不是附件；bundle.json 收货员会自己取） =====")
DEF_OUT = Path(os.environ.get("INSP_OUT", "/workspace/inspiration_out"))
DEF_HOME = Path(os.environ.get("INSP_STATE_HOME", "/workspace/inspiration_state"))
DEF_REFS = Path(os.environ.get("INSP_REFS", "/workspace/_refs"))
# flat attachment name -> path inside a state dir
FLAT = {"state.json": "state.json", "negative.jsonl": "negative.jsonl", "verdicts.jsonl": "verdicts.jsonl",
        "rubric.md": "taste/rubric.md", "weights.json": "taste/weights.json", "exemplars.jsonl": "taste/exemplars.jsonl",
        "interests.md": "profile/interests.md", "claims.md": "profile/claims.md", "observed.md": "profile/observed.md",
        "observed.jsonl": "observed.jsonl"}  # v1.5: findings of the local observers (code / logs / notes / usage)


def _load(name: str):
    sys.path.insert(0, str(VENDOR))
    spec = importlib.util.spec_from_file_location(name, VENDOR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- state
def find_refs_file(refs: Path, name: str) -> Path | None:
    if not refs.is_dir():
        return None
    hits = sorted(refs.rglob(name), key=lambda p: len(p.parts))
    return hits[0] if hits else None


WORKSPACE_ROOT = Path(os.environ.get("INSP_WORKSPACE_ROOT", "/workspace"))


def build_state_dir(out: Path, home: Path, refs: Path) -> tuple[Path, str]:
    """Return (state_dir, mode). The working copy always lives in OUT/_state so a failed run never touches HOME."""
    work = out / "_state"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(TEMPLATE, work)  # defaults for any optional file the source does not have
    att = find_refs_file(refs, "state.json")
    snap = snapshot_info()
    home_t = (home / "state.json").stat().st_mtime if (home / "state.json").is_file() else None
    if att is not None:
        for flat, rel in FLAT.items():
            src = find_refs_file(refs, flat)
            if src is not None:
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, work / rel)
        mode = "attached"
    elif snap and (home_t is None or float(snap.get("published_epoch", 0)) >= home_t):
        copy_snapshot(work)
        mode = "skill_snapshot"
    elif home_t is not None:
        for p in home.rglob("*"):
            if p.is_file() and "fulltext" not in p.relative_to(home).parts:
                (work / p.relative_to(home)).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, work / p.relative_to(home))
        mode = "persisted"
    elif (WORKSPACE_ROOT / "state.json").is_file():   # v1.5.2 (Q4 ④): state.json uploaded to the workspace root
        for flat, rel in FLAT.items():
            src = WORKSPACE_ROOT / flat
            if src.is_file():
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, work / rel)
        mode = "workspace_root"
    else:
        mode = "bootstrap"
    if mode != "attached" and snap and not _lines(work / "observed.jsonl") and _lines(SNAPSHOT / "observed.jsonl"):
        shutil.copyfile(SNAPSHOT / "observed.jsonl", work / "observed.jsonl")   # at least the local observations
    neg = work / "negative.jsonl"
    if neg.is_file() and not neg.read_text(encoding="utf-8").strip():
        neg.write_text("", encoding="utf-8")
    return work, mode


def snapshot_info() -> dict | None:
    """assets/local_state/snapshot.json written by the local publisher (platform/publish_state.py)."""
    try:
        info = json.loads((SNAPSHOT / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return info if isinstance(info, dict) and (SNAPSHOT / "state.json").is_file() else None


def copy_snapshot(work: Path) -> None:
    for flat, rel in FLAT.items():
        src = SNAPSHOT / rel
        if not src.is_file():
            src = SNAPSHOT / flat
        if src.is_file():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, work / rel)


def _lines(p: Path) -> int:
    try:
        return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        return 0


def apply_overrides(state_dir: Path, query: str | None, keywords: str | None, max_results: int | None,
                    mode: str, exclude: str | None = None) -> dict:
    sf = state_dir / "state.json"
    st = json.loads(sf.read_text(encoding="utf-8"))
    excluded = [x.strip() for x in (exclude or "").replace("，", ",").replace(" ", ",").split(",") if x.strip()]
    if excluded:  # ids the user was already shown (from the "[灵感状态]" memory): treat as seen, never re-card them
        st["seen_ids"] = list(dict.fromkeys(st.get("seen_ids", []) + excluded))
    src = "state" if mode != "bootstrap" else "template"
    if query:
        arx = [s for s in st.get("sources", []) if s.get("kind") == "arxiv"]
        if arx:
            arx[0]["query"] = query
        else:
            st.setdefault("sources", []).append({"kind": "arxiv", "query": query, "max_results": 60})
        src = "override"
    if keywords:
        kws = [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]
        if kws:
            st["keywords"] = kws
    if max_results:
        for s in st.get("sources", []):
            s["max_results"] = int(max_results)
    sf.write_text(jdump(st), encoding="utf-8")
    return {"query": [s.get("query") for s in st.get("sources", []) if s.get("kind") == "arxiv"],
            "keywords": st.get("keywords", []), "query_source": src, "excluded": len(excluded)}


# ----------------------------------------------------------------------------- arXiv
def arxiv_reachable(timeout: float = 15) -> tuple[bool, str]:
    try:
        req = urllib.request.Request("https://export.arxiv.org/api/query?search_query=all:test&max_results=1",
                                     headers={"User-Agent": RUNNER_VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as exc:  # 429/503 = reachable but busy
        return exc.code in (429, 500, 502, 503), f"HTTP {exc.code}"
    except Exception as exc:  # DNS / proxy / firewall
        return False, f"{type(exc).__name__}: {exc}"


def fetch_with_backoff(triage, sources: list[dict], log=print) -> list[dict]:
    waits = [0, 10, 30, 60]
    last: Exception | None = None
    for i, w in enumerate(waits):
        if w:
            log(f"arXiv 忙（{last}），{w} 秒后重试（{i}/{len(waits) - 1}）")
            time.sleep(w)
        try:
            return triage.fetch_arxiv(sources, timeout=40)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            ra = exc.headers.get("Retry-After") if exc.headers else None
            if ra and ra.isdigit() and i + 1 < len(waits):
                waits[i + 1] = max(waits[i + 1], min(int(ra), 120))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
    raise RuntimeError(f"arXiv 列表抓取失败：{last}")


# ----------------------------------------------------------------------------- bundle
TRIAGE_MIN_KEYS = ("schema_version", "generated_at", "status", "counts", "new_ids", "revival_state", "revived_ids",
                   "weights_version", "shortlisted", "observed_shortlist", "fetch_mode", "fulltext_retry")


def canon_sha(obj) -> str:
    """sha256 of canonical JSON — the receiver recomputes it to detect a damaged copy in the reply."""
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def write_bundle(out: Path, b: dict) -> Path:
    # v1.4.2: embed what the receiver needs from triage.json, so the bundle is self-contained when the
    # local side cannot read sandbox files (API key → 403 on /api/thread/{id}/files/content)
    try:
        tri = json.loads((out / "triage.json").read_text(encoding="utf-8"))
        b["triage_min"] = {k: tri[k] for k in TRIAGE_MIN_KEYS if k in tri}
        b["triage_min_sha256"] = canon_sha(b["triage_min"])
    except (OSError, json.JSONDecodeError):
        pass
    p = out / "bundle.json"
    p.write_text(jdump(b), encoding="utf-8")
    slim = dict(b)
    slim["bundle_sha256"] = sha256(p)
    block = f"{BLOCK_START}\n{json.dumps(slim, ensure_ascii=False)}\n{BLOCK_END}"
    (out / "bundle_block.txt").write_text(block, encoding="utf-8")
    print("\n===== 把下面整个结果块原样放在最终回答的最后（不要改动、不要省略） =====")
    print(block)
    print(CLOSING_HINT)
    return p


def base_bundle(meta: dict, status: str) -> dict:
    return {"bundle_version": BUNDLE_VERSION, "runner_version": RUNNER_VERSION, "run_id": meta["run_id"],
            "generated_at": now_iso(), "status": status, "state_mode": meta.get("state_mode"),
            "query": meta.get("query"), "query_source": meta.get("query_source"),
            "fetch_mode": meta.get("fetch_mode", "abstract"),
            "out_dir": meta.get("out_dir"), "files": {"triage": "triage.json", "cards": "cards.json", "bundle": "bundle.json"},
            "state_patch": {}, "triage_counts": meta.get("counts", {}), "cards": [], "rejected": [],
            "fulltext_index": {}, "sandbox_validation": {}, "notes": meta.get("notes", [])}


def slim_index(out: Path) -> dict:
    try:
        idx = json.loads((out / "fulltext_index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {ref: {"file": v.get("file"), "sha256": v.get("sha256"), "chars": v.get("chars"), "method": v.get("method"),
                  "related": [{"ref": r.get("ref"), "file": r.get("file"), "relation": r.get("relation"), "sha256": r.get("sha256")}
                              for r in v.get("related", [])]} for ref, v in idx.items()}


def persist_state(out: Path, home: Path, state_dir: Path, tri: dict, kept: list[dict]) -> dict:
    """Advance THIS conversation's state (the local receiver remains the authority and may overwrite it)."""
    st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    patch = {"add_seen_ids": list(tri.get("new_ids", [])), "revival_state": tri.get("revival_state"),
             "fulltext_retry": tri.get("fulltext_retry", st.get("fulltext_retry", {})), "last_run_at": now_iso(),
             "pushed_refs": []}
    st["seen_ids"] = list(dict.fromkeys(st.get("seen_ids", []) + patch["add_seen_ids"]))
    if patch["revival_state"]:
        st["revival"] = patch["revival_state"]
    st["fulltext_retry"] = patch["fulltext_retry"]
    today = date.today().isoformat()
    for c in kept:
        ref = str(c["source"]["ref"])
        patch["pushed_refs"].append(ref)
        st.setdefault("pushed", []).append({"id": ref, "card_id": c.get("id"), "title": c.get("title", ""), "type": c["type"],
                                            "pushed_at": patch["last_run_at"], "by": "sandbox"})
        day = st.setdefault("quota_used", {}).setdefault(today, {})
        day[c["type"]] = day.get(c["type"], 0) + 1
    st["last_run_at"], st["bootstrap"] = patch["last_run_at"], False
    (state_dir / "state.json").write_text(jdump(st), encoding="utf-8")
    home.mkdir(parents=True, exist_ok=True)
    for p in state_dir.rglob("*"):
        if p.is_file():
            dst = home / p.relative_to(state_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dst)
    return patch


# ----------------------------------------------------------------------------- commands
def install_fq(out: Path) -> str:
    """v1.4.4: put find_quotes at a SHORT path (<workspace>/fq, no extension) so the command stays far below the
    ~80-column width at which the platform chops commands (feedback5 G8). Defaults point at this run's out dir."""
    src = VENDOR / "find_quotes.py"
    dst = out.parent / "fq"
    try:
        text = src.read_text(encoding="utf-8").replace(
            'DEFAULT_DIRS = [Path("/workspace/inspiration_out"), Path("/workspace/_refs")]',
            f'DEFAULT_DIRS = [Path({str(out)!r}), Path("/workspace/_refs")]')
        dst.write_text(text, encoding="utf-8")
        dst.chmod(0o755)
        return str(dst)
    except OSError:
        return str(src)


def cmd_prepare(a) -> int:
    out, home, refs = a.out, a.state_home, a.refs
    out.mkdir(parents=True, exist_ok=True)
    for p in list(out.glob("ft_*.md")) + list(out.glob("rel_*.md")) + list(out.glob("obs_*.md")) + list(out.glob("cand_*.md")) + [
              out / n for n in ("triage.json", "cards.json", "bundle.json", "bundle_block.txt", "fulltext_index.json",
                                "run_meta.json", ATTEMPTS, SHORTLIST_FILE, FINAL_FILE)]:
        if p.exists():
            p.unlink()
    run_id = a.run_id or ("sbx-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    state_dir, mode = build_state_dir(out, home, refs)
    ft = _load("fetch_fulltext")
    fetch_mode, warn = ft.resolve_fetch_mode(a.fetch_mode)
    meta = {"run_id": run_id, "state_mode": mode, "out_dir": str(out), "notes": [], "started_at": now_iso(),
            "fetch_mode": fetch_mode}
    if warn:
        meta["notes"].append(warn)
    snap = snapshot_info()
    if snap:
        meta["snapshot"] = {k: snap.get(k) for k in ("published_at", "observed", "pushed", "package_version")}
    meta.update(apply_overrides(state_dir, a.query, a.keywords, a.max_results, mode, a.exclude))
    if mode == "bootstrap" and meta["query_source"] == "template":
        meta["notes"].append("首次运行且没有给 --query：用的是模板里的示例检索式。请先建立用户画像（insp-profile），或在指令里说明关注方向。")
    triage = _load("triage")

    # 0. legacy: a ready-made triage.json + full texts were attached (local fallback route)
    att_tri = find_refs_file(refs, "triage.json")
    if att_tri is not None:
        tri = json.loads(att_tri.read_text(encoding="utf-8"))
        for p in list(refs.rglob("ft_*.md")) + list(refs.rglob("rel_*.md")) + list(refs.rglob("fulltext_index.json")):
            shutil.copyfile(p, out / p.name)
        meta["notes"].append("使用附件里的 triage.json 与原文（本地抓取兜底流程），没有在沙箱里重新抓取。")
        meta["source_mode"] = "attached_triage"
        if tri.get("fetch_mode") in ft.FETCH_MODES:  # the local run already decided the mode; follow it
            meta["fetch_mode"] = fetch_mode = tri["fetch_mode"]
    else:
        st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        if a.observed_only:  # v1.5: only the local observed channel this time (no arXiv request at all)
            raw = []
            meta["notes"].append("--observed-only：本次只处理本地观察通道（observed.jsonl），没有抓 arXiv。")
        elif a.raw:
            raw = json.loads(a.raw.read_text(encoding="utf-8"))
        else:
            ok, why = arxiv_reachable()
            if not ok:
                meta["counts"] = {}
                b = base_bundle(meta, "FETCH_BLOCKED")
                b["notes"].append(f"沙箱连不上 export.arxiv.org（{why}）。本地收货员会自动改走“本地抓取 + 自动上传”流程。")
                write_bundle(out, b)
                print(f"FETCH_BLOCKED: {why}")
                return 4
            try:
                raw = fetch_with_backoff(triage, st.get("sources", []))
            except Exception as exc:
                meta["counts"] = {}
                b = base_bundle(meta, "FETCH_BLOCKED")
                b["notes"].append(str(exc))
                write_bundle(out, b)
                print(f"FETCH_BLOCKED: {exc}")
                return 4
        (out / "raw_items.json").write_text(jdump(raw), encoding="utf-8")
        code, tri = triage.run_triage(state_dir, raw)
        if code == 3:
            meta["counts"] = {}
            b = base_bundle(meta, "STATE_MISSING")
            b["notes"].append(str(tri.get("detail")))
            write_bundle(out, b)
            print("STATE_MISSING:", tri.get("detail"))
            return 3
        meta["source_mode"] = "sandbox_fetch"
        if tri["status"] != "NO_CANDIDATES" and fetch_mode == "fulltext":
            cfg = dict(ft.DEFAULTS)
            if a.fulltext_max is not None:
                cfg["max_candidates"] = max(1, int(a.fulltext_max))
            print(f"FULLTEXT_MAX {cfg['max_candidates']}（全文模式每次最多读几篇入围论文）")
            cache = a.cache_dir or (home / "fulltext")
            cache.mkdir(parents=True, exist_ok=True)
            rep = ft.enrich(tri, cache, out, cfg, st.get("fulltext_retry", {}))
            tri["fulltext_retry"] = rep["retry_state"]
            meta["fulltext"] = {"ok": rep["ok"], "pending": rep["pending"], "dropped": rep["dropped"],
                                "deferred": rep.get("deferred", []), "max_candidates": cfg["max_candidates"]}
    tri["fetch_mode"] = fetch_mode
    for c in tri.get("shortlisted", []):
        c["links"] = ft.arxiv_links(c.get("id", ""), c.get("fulltext"))
    obs = tri.get("observed_shortlist") or []
    for c in obs:  # one short evidence file per observed candidate: the agent reads it and copies the quote from it
        (out / f"{c['id']}.md").write_text(observed_md(c), encoding="utf-8")
    cards_dir = out / "cards"            # v1.5.5 (P9): one card = one small file; finalize assembles cards.json
    if cards_dir.exists():
        shutil.rmtree(cards_dir)
    cards_dir.mkdir(parents=True)
    tri.setdefault("counts", {})
    n_obs_rows = _lines(state_dir / "observed.jsonl")
    tri["counts"].setdefault("observed", n_obs_rows)
    tri["counts"]["observed_source"] = mode if n_obs_rows else "none"
    if not n_obs_rows:   # P11: make "no local observation channel this time" visible instead of silently 0 A cards
        tri["counts"]["observed_missing"] = True
        meta["notes"].append("这一轮没有本地观察（observed.jsonl 为空）：A 类优化卡出不来。本地运行一次评价页或 "
                             "python platform/publish_state.py 发布快照后再跑。")
    (out / "triage.json").write_text(jdump(tri), encoding="utf-8")
    meta["counts"] = tri.get("counts", {})
    (out / "run_meta.json").write_text(jdump(meta), encoding="utf-8")
    # v1.5.9: the v1.4.6 paper route reads triage.json.shortlisted in ONE go. triage.json itself is ~150 KB with 500
    # fetched items (ranked/dropped/new_ids) and gets truncated when cat'ed, so the same shortlisted entries are also
    # written to this small file (written last: if it exists, prepare has finished).
    (out / SHORTLIST_FILE).write_text(jdump(shortlist_doc(tri, run_id, state_dir)), encoding="utf-8")

    print(f"MODE {mode} · 检索式 {meta['query']}（来源 {meta['query_source']}）")
    print(f"FETCH_MODE {fetch_mode}（" + ("只读摘要；卡片附 arXiv 摘要页链接）" if fetch_mode == "abstract"
                                       else "通读全文 + 相关文献；卡片附全文链接）"))
    print("COUNTS " + json.dumps(meta["counts"], ensure_ascii=False))
    if tri.get("status") == "NO_CANDIDATES" or not (tri.get("shortlisted") or obs):
        patch = persist_state(out, home, state_dir, tri, [])
        b = base_bundle(meta, "NO_CANDIDATES")
        b["state_patch"] = patch
        write_bundle(out, b)
        print("NO_CANDIDATES：没有新的可出卡候选。直接把上面的结果块作为最终回答结束，不要写卡片。")
        return 0
    if fetch_mode == "fulltext":
        fq = install_fq(out)
        meta["find_quotes"] = fq
        (out / "run_meta.json").write_text(jdump(meta), encoding="utf-8")
        print(f"FIND_QUOTES 找引文只用这条短命令：python3 {fq} <ID>（一次 1–2 个 ID；--section \"<标题>\" 读一节）")
        print("\n入围且拿到原文的候选（逐篇读 ft_ 与 rel_ 文件后再写卡片）：")
        for c in tri["shortlisted"]:
            f = c.get("fulltext") or {}
            rel = ", ".join(r["file"] for r in f.get("related", []))
            print(f"- {c['id']} | {c.get('title', '')[:90]}\n    原文 {out / str(f.get('file'))} · 章节 {len(f.get('sections', []))} · 相关 {rel}"
                  f"\n    全文链接 {c['links'].get('fulltext_url')}")
    elif tri.get("shortlisted"):
        print(f"\n入围候选（摘要模式：只读 triage.json 里 shortlisted 的标题与摘要——一次读完 {out / SHORTLIST_FILE}，"
              "内容就是 triage.json 的 shortlisted，不要 cat 整个 triage.json，不要逐篇另找文件，不要去抓原文；quote 从摘要逐字复制）：")
        for c in tri["shortlisted"]:
            print(f"- {c['id']} | {c.get('title', '')[:90]}\n    摘要页 {c['links']['url']}")
    if obs:
        print(f"\nOBSERVED {len(obs)}")
        print(f"观察通道候选 {len(obs)} 条（来自你本地的代码 / 日志 / 笔记 / 使用记录；按 insp-card 的“观察型卡片”规则写）："
              "\n  source.kind = 下面的 kind，source.ref = obs_ 编号，quote 从对应 obs_*.md 的【证据原文】里原样复制；"
              "type 只能用 allowed 里的类型；数字来源写 observed")
        for c in obs:
            print(f"- {c['id']} | {c['candidate_type']} · kind {c['kind']} · 严重度 {c['severity']} | {c['title'][:80]}"
                  f"\n    证据 {out / (c['id'] + '.md')} · allowed {c['allowed_types']} · 位置 {c.get('evidence_ref') or '—'}")
    print(remaining_line(state_dir, tri, obs))
    print(WRITE_RULES.format(cards=out / "cards", cards_json=out / "cards.json", sl=out / SHORTLIST_FILE, run_id=run_id,
                             flow=_FLOW(), obs_note=("" if obs else "（这一轮没有观察型候选，跳过第二步）")))
    return 0


TYPE_CN = {"optimization": "A 优化", "knowledge": "B 知识", "hypothesis": "C 假说", "process": "D 流程"}


def remaining_line(state_dir: Path, tri: dict, obs: list) -> str:
    """v1.5.7: tell the agent BEFORE writing how many cards of each type can still be kept this run (same numbers
    finalize enforces), so it does not write 8 knowledge cards for 5 slots. Memory may hold stale counts
    (CARD1: memory said process 1/2, the state said 2/2) — this line wins."""
    try:
        st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        rem = _load("validate_cards").remaining_quota(st, date.today())
    except Exception as exc:  # noqa: BLE001
        return f"REMAINING 未知（{exc}）"
    show = {t: rem[t] for t in ("optimization", "knowledge", "hypothesis", "process")}
    txt = "，".join(f"{TYPE_CN[t]} {'不限' if n >= 999 else n}" for t, n in show.items())
    zero = [TYPE_CN[t] for t, n in show.items() if n == 0]
    return (f"\nREMAINING {json.dumps(show, ensure_ascii=False)}\n本轮各类型最多还能入库：{txt}"
            + (f"（{'、'.join(zero)} 已满：这类候选不要写，直接写进 rejected.json）" if zero else "")
            + "。以这一行为准，不要用记忆里的配额数字；先按它决定写哪几张，按分数从高到低写，写满就停。"
              "多写的卡 finalize 会按分数自动移进 rejected，不算错误，也不要去删文件。")


WRITE_RULES = """
===== 写卡（v1.5.9，必须照做；论文卡 = v1.4.6 写法）=====
第一步 · 论文卡（arXiv 入围候选，B 类知识卡为主）——先做这一步：
- 一次读完 {sl}（= triage.json 的 shortlisted：标题 + 摘要），读完立刻用文件写入工具（create_file）
  一次写成 {cards_json}：
  {{"flow_version": "{flow}", "run_id": "{run_id}", "cards": [ …每篇一张… ], "rejected": [ …不出卡的候选… ]}}
- 只按上面 REMAINING 的名额写，按分数从高到低；不要先在回答里打草稿，不要逐篇另找文件。
第二步 · 观察型卡片（obs_ 候选，A 类优化卡等，1.5.7 写法不变）{obs_note}：
- 一张卡 = 一个文件：{cards}/01.json、02.json …（文件内容 = 一张卡片对象）；读一条 obs_*.md 写一张；
  id 接着论文卡往后编号（重了 finalize 会自动改号）。被否决的观察候选写 {cards}/rejected.json。
第三步 · 写完立刻运行 finalize；在 finalize 打印结果块之前不得结束回答。
- finalize 合并 cards.json（论文卡）+ cards/NN.json（观察型卡），合并结果写 cards_final.json（输出，不要读、不要改）。
- 报错：论文卡改 cards.json 里那一张，观察型卡改点名的 cards/NN.json；不想要某张卡就 finalize --drop <卡片 id 或文件名>（不用删除工具）。
- 超配额的卡会被自动移进 rejected，不是错误。第 3 次仍不过会自动只交通过的卡片，不要再循环修改。
- 不用 echo / cat > / heredoc 拼 JSON。
"""

SHORTLIST_FILE = "triage_shortlisted.json"   # v1.5.9: triage.json.shortlisted only, for the one-read v1.4.6 paper route
FINAL_FILE = "cards_final.json"              # v1.5.9: finalize's merged output (never read back as input)
SL_KEYS = ("id", "title", "abstract", "authors", "published", "categories", "links", "scores", "score", "revival", "fulltext")


def shortlist_doc(tri: dict, run_id: str, state_dir: Path) -> dict:
    try:
        st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        rem = _load("validate_cards").remaining_quota(st, date.today())
        rem = {k: rem[k] for k in ("optimization", "knowledge", "hypothesis", "process")}
    except Exception:  # noqa: BLE001
        rem = None
    return {"run_id": run_id, "fetch_mode": tri.get("fetch_mode"), "flow_version": _FLOW(), "remaining": rem,
            "note": "与 triage.json 的 shortlisted 相同（只保留出卡要用的字段）。论文卡一次写成 cards.json。",
            "shortlisted": [{k: c[k] for k in SL_KEYS if k in c} for c in tri.get("shortlisted", [])]}



ASSEMBLED_MARK = "cards/"   # v1.5.7: cards.json written by finalize carries "assembled_from": "cards/" = output only
ATTEMPTS = ".finalize_attempts.json"


def _paper_doc(out: Path) -> dict:
    """v1.5.9: cards.json = the agent's v1.4.6-style paper cards (input). Our own output from 1.5.7 (marked
    assembled_from) is ignored, so a 1.5.7 sandbox left over in the same conversation cannot resurrect cards."""
    f = out / "cards.json"
    if not f.is_file():
        return {}
    doc = read_cards(f)
    if isinstance(doc, list):
        doc = {"cards": doc}
    if not isinstance(doc, dict) or doc.get("assembled_from") == ASSEMBLED_MARK:
        return {}
    return doc


def _next_id(used: set, like: str) -> str:
    day = like[4:12] if len(like) >= 12 and like.startswith("ins_") else datetime.now().strftime("%Y%m%d")
    n = 1
    while f"ins_{day}_{n:03d}" in used:
        n += 1
    return f"ins_{day}_{n:03d}"


def assemble_cards(out: Path, meta: dict) -> tuple[dict | None, str]:
    """v1.5.9: two inputs, one output.

    - cards.json      : paper cards written in ONE go, exactly the v1.4.6 contract (root flow_version / run_id /
                        cards / rejected). The v1.4.6 route the user asked to restore for B cards + arXiv.
    - cards/NN.json   : observed cards, one card per file (1.5.5–1.5.7, unchanged). Deleting a file removes that
                        card (CARD1).
    - cards_final.json: the merged result, OUTPUT only — never read back (1.5.7 wrote the merge into cards.json and
                        read it again next time; that loop resurrected deleted cards).
    A card id used in both places is renumbered on the card-file side (no card is lost)."""
    d = out / "cards"
    files = sorted(f for f in d.glob("*.json") if f.stem != "rejected") if d.is_dir() else []
    rej_file = d / "rejected.json"
    rejected: list = []
    if rej_file.is_file():
        try:
            o = read_cards(rej_file)
            rejected = o if isinstance(o, list) else list(o.get("rejected") or [])
        except (OSError, json.JSONDecodeError):
            rejected = []   # a broken rejected.json never blocks the cards
    paper = _paper_doc(out)          # may raise JSONDecodeError → finalize reports "cards.json 不是合法 JSON"
    if not files and not paper:
        return None, ""
    cards: list = []
    where: dict = {}
    for c in paper.get("cards") or []:
        if isinstance(c, dict):
            cards = [x for x in cards if x.get("id") != c.get("id")] + [c]
            where[c.get("id")] = "cards.json"
    paper_ids = set(where)
    for f in files:
        try:
            o = read_cards(f)
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"{f.name} 不是合法 JSON：{exc}（只改这一个文件后重跑 finalize）"
        items = o if isinstance(o, list) else (o.get("cards") if isinstance(o, dict) and "cards" in o else [o])
        for c in items:
            if not isinstance(c, dict):
                continue
            if c.get("id") in paper_ids:     # same id as a paper card → renumber this one, keep both
                new = _next_id(set(where) | {x.get("id") for x in cards}, str(c.get("id") or ""))
                print(f"RENUMBER {f.name} 的卡片 id {c.get('id')} 和 cards.json 里的论文卡重复，自动改成 {new}")
                c["id"] = new
            if c.get("id") in where:
                print(f"WARN: {f.name} 和 {where[c.get('id')]} 的卡片 id 相同（{c.get('id')}），以 {f.name} 为准")
            cards = [x for x in cards if x.get("id") != c.get("id")] + [c]
            where[c.get("id")] = f"cards/{f.name}"
    root = {"schema_version": paper.get("schema_version") or "1.0", "flow_version": paper.get("flow_version") or _FLOW(),
            "run_id": paper.get("run_id") or meta.get("run_id"), "generated_at": now_iso(),
            "status": "OK" if cards else "NO_CANDIDATES", "cards": cards,
            "rejected": list(paper.get("rejected") or []) + rejected, "assembled_from": ASSEMBLED_MARK}
    (out / FINAL_FILE).write_text(jdump(root), encoding="utf-8")
    root["_card_files"] = where
    n_paper = sum(1 for v in where.values() if v == "cards.json")
    return root, f"cards.json 论文卡 {n_paper} 张 + {len(files)} 个观察型单卡文件"


def drop_cards(out: Path, keys: list[str]) -> list[str]:
    """v1.5.7 finalize --drop: take cards out of this run by card id / file name (05 or 05.json) / source ref.
    The file is MOVED to cards/_dropped/ (never read again) and noted in cards/rejected.json — no delete_file tool
    needed (CARD1: one delete_file of cards.json silently did nothing)."""
    d = out / "cards"
    want = {k.strip() for k in keys if k.strip()}
    done: list[str] = []
    if not want:
        return done
    rej_file = d / "rejected.json"
    try:
        rej = read_cards(rej_file) if rej_file.is_file() else []
        rej = rej if isinstance(rej, list) else list(rej.get("rejected") or [])
    except (OSError, json.JSONDecodeError):
        rej = []
    paper_f = out / "cards.json"
    try:
        paper = _paper_doc(out)
    except (OSError, json.JSONDecodeError):
        paper = {}
    if paper.get("cards"):        # v1.5.9: paper cards live in cards.json — take the card out of the list
        keep = []
        for c in paper["cards"]:
            ref = str((c.get("source") or {}).get("ref", "")) if isinstance(c, dict) else ""
            if isinstance(c, dict) and want & {str(c.get("id", "")), ref}:
                paper.setdefault("rejected", []).append({"id": ref or c.get("id"), "card_id": c.get("id"),
                                                         "reason": "finalize --drop 手动去掉", "rule_ids": []})
                done.append(f"cards.json（{c.get('id')}）")
            else:
                keep.append(c)
        if len(keep) != len(paper["cards"]):
            paper["cards"] = keep
            paper_f.write_text(jdump(paper), encoding="utf-8")
    if not d.is_dir():
        return done
    for f in sorted(d.glob("*.json")):
        if f.stem == "rejected":
            continue
        try:
            c = read_cards(f)
        except (OSError, json.JSONDecodeError):
            c = {}
        c = c if isinstance(c, dict) else {}
        ref = str((c.get("source") or {}).get("ref", ""))
        if want & {f.stem, f.name, str(c.get("id", "")), ref}:
            (d / "_dropped").mkdir(exist_ok=True)
            shutil.move(str(f), str(d / "_dropped" / f.name))
            rej.append({"id": ref or c.get("id") or f.stem, "card_id": c.get("id"), "reason": "finalize --drop 手动去掉",
                        "rule_ids": []})
            done.append(f"{f.name}（{c.get('id')}）")
    rej_file.write_text(jdump(rej), encoding="utf-8")
    return done


def _FLOW() -> str:
    try:
        return _load("validate_cards").FLOW_VERSION
    except Exception:  # noqa: BLE001
        return "insp-card/1.1.0"


def observed_md(c: dict) -> str:
    m = c.get("metrics") or {}
    lines = [f"# {c['id']} · {c['title']}", "",
             f"- 来源通道：observed · kind = {c['kind']} · 检测器 {c.get('detector') or '—'} · 子领域 {c.get('subfield') or '—'}",
             f"- 建议卡片类型：{c['candidate_type']}（允许：{', '.join(c.get('allowed_types') or [])}）· 严重度 {c['severity']}",
             f"- 位置：{c.get('evidence_ref') or '—'} · 首次发现 {c.get('first_seen') or '—'} · 最近一次 {c.get('last_seen') or '—'}", ""]
    if c.get("summary"):
        lines += ["## 观察结论（脚本生成，可参考，不可引用）", "", c["summary"], ""]
    if m:
        lines += ["## 指标（脚本生成）", ""] + [f"- {k}: {v}" for k, v in list(m.items())[:20]] + [""]
    lines += ["## 证据原文（quote 只能从这里逐字复制）", "", "```text", c.get("abstract", ""), "```", ""]
    return "\n".join(lines)


def read_cards(p: Path) -> dict:
    txt = p.read_text(encoding="utf-8").strip()
    if "<<<INSP_CARDS_JSON" in txt:
        txt = txt.split("<<<INSP_CARDS_JSON", 1)[1].split("INSP_CARDS_JSON>>>", 1)[0]
    txt = txt.strip().strip("`")
    if txt.startswith("json"):
        txt = txt[4:]
    return json.loads(txt)


def cmd_finalize(a) -> int:
    out, home = a.out, a.state_home
    meta = json.loads((out / "run_meta.json").read_text(encoding="utf-8"))
    tri = json.loads((out / "triage.json").read_text(encoding="utf-8"))
    state_dir = out / "_state"
    if getattr(a, "drop", None):
        gone = drop_cards(out, str(a.drop).replace("，", ",").split(","))
        print("DROPPED " + ("；".join(gone) if gone else f"没有找到 {a.drop} 对应的卡片（用卡片 id、观察型卡的文件名 05 或 source.ref）"))
    if a.cards:
        try:
            doc = read_cards(a.cards)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"ERROR: {a.cards} 不是合法 JSON：{exc}")
            return 2
    else:
        try:
            doc, how = assemble_cards(out, meta)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"ERROR: cards.json 不是合法 JSON：{exc}")
            return 2
        if doc is None:
            print(f"ERROR: 还没有卡片{('：' + how) if how else ''}。论文卡一次写成 {out / 'cards.json'}"
                  f"（读 {out / SHORTLIST_FILE}），观察型卡写成 {out / 'cards'}/01.json …，写完立刻再运行 finalize。")
            return 2
        print(f"CARDS 读到 {len(doc.get('cards') or [])} 张卡（来自 {how}）")
    where = doc.pop("_card_files", {}) if isinstance(doc, dict) else {}
    vc = _load("validate_cards")
    log: list = []
    full = meta.get("fetch_mode") == "fulltext"
    root, per, kept = vc.validate(doc, tri, state_dir, vc.FLOW_VERSION, None, log, out if full else None, full, 1)
    # v1.5.7: a card that is fine but does not fit the quota is NOT an error — the selector already kept the best
    # ones by score; the rest go to rejected automatically. 1.5.6 reported them as failures and the agent tried to
    # fix them by deleting files (CARD1).
    trimmed = [r for r in log if not r.get("kept")]
    trimmed_ids = {r["card_id"] for r in trimmed}
    bad = {k: v for k, v in per.items() if v and k not in trimmed_ids}
    by_id = {c.get("id"): c for c in doc.get("cards", []) if isinstance(c, dict)} if isinstance(doc, dict) else {}
    if trimmed:
        print(f"QUOTA_TRIMMED {len(trimmed)} 张合格但超出配额的卡已自动移进 rejected（按分数保留了高的；这不是错误，"
              "不要删文件、不要改它们）：")
        for r in trimmed:
            print(f"  - {where.get(r['card_id'], '')} {r['card_id']} [{r['type']}] {r['reason']}")
    attempts_p = out / ATTEMPTS
    try:
        attempts = int(json.loads(attempts_p.read_text(encoding="utf-8")).get("failed", 0))
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        attempts = 0
    auto_partial = False
    if root or (bad and not a.accept_partial):
        if not root and attempts >= 2:
            auto_partial = True   # third failing run: stop the fix-and-retry loop, deliver what passed (CARD1 burnt the balance)
            print(f"AUTO_PARTIAL 已经连续 {attempts} 次没通过：这次自动只交通过校验的卡片，下面这些卡不入库（不用再改）：")
            for cid, errs in bad.items():
                print(f"  - {where.get(cid, '')} [{cid}] {'；'.join(errs)}")
        else:
            attempts_p.write_text(json.dumps({"failed": attempts + 1}), encoding="utf-8")
            print(f"VALIDATION_FAILED（第 {attempts + 1} 次）只改下面列出的卡：标着 cards.json 的改 cards.json 里那一张，"
                  "标着 cards/NN.json 的改那个文件；改完重跑 finalize。不想要哪张就 finalize --drop <卡片 id 或文件名>"
                  "（不要用删除工具，不要动 cards_final.json——它只是输出）。第 3 次仍不过会自动只交通过的卡片：")
            for e in root:
                print("  [整体]", e)
            for cid, errs in bad.items():
                for e in errs:
                    print(f"  {where.get(cid, '')} [{cid}] {e}")
            return 2
    patch = persist_state(out, home, state_dir, tri, kept)
    b = base_bundle(meta, "OK" if kept else "NO_CANDIDATES")
    kept_ids = {c.get("id") for c in kept}
    auto_rej = [{"id": str((by_id.get(r["card_id"], {}).get("source") or {}).get("ref") or r["card_id"]), "card_id": r["card_id"],
                 "reason": r["reason"], "rule_ids": [], "auto": "quota"} for r in trimmed]
    auto_rej += [{"id": str((by_id.get(cid, {}).get("source") or {}).get("ref") or cid), "card_id": cid,
                  "reason": "；".join(errs)[:300], "rule_ids": [], "auto": "validation"}
                 for cid, errs in bad.items()] if (bad and (auto_partial or a.accept_partial)) else []
    b.update(state_patch=patch, cards=[c for c in doc.get("cards", []) if c.get("id") in kept_ids],
             rejected=list(doc.get("rejected", [])) + auto_rej,
             fulltext_index=slim_index(out) if full else {}, flow_version=doc.get("flow_version"),
             sandbox_validation={"kept": [c.get("id") for c in kept], "errors": bad,
                                 "quota_trimmed": [r["card_id"] for r in trimmed], "auto_partial": auto_partial},
             triage_sha256=sha256(out / "triage.json"),
             cards_doc_meta={k: doc.get(k) for k in ("schema_version", "flow_version", "run_id", "generated_at", "status")})
    write_bundle(out, b)
    try:
        attempts_p.unlink()
    except OSError:
        pass
    print(f"OK: {len(kept)} 张卡片通过沙箱校验；整包 {out / 'bundle.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="灵感运行（沙箱内自举）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "finalize"):
        s = sub.add_parser(name)
        s.add_argument("--out", type=Path, default=DEF_OUT)
        s.add_argument("--state-home", type=Path, default=DEF_HOME)
        s.add_argument("--refs", type=Path, default=DEF_REFS)
    p = sub.choices["prepare"]
    p.add_argument("--query", help="arXiv 检索式，例如 'cat:quant-ph AND abs:\"surface code\"'（来自用户画像记忆）")
    p.add_argument("--keywords", help="逗号分隔的关键词")
    p.add_argument("--max-results", type=int)
    p.add_argument("--exclude", help="逗号分隔的 arXiv ID：已经推送过、不要再出卡（来自记忆里的“[灵感状态]”条目）")
    p.add_argument("--run-id")
    p.add_argument("--raw", type=Path, help="离线测试：用本地 raw_items.json 代替抓取")
    p.add_argument("--observed-only", action="store_true",
                   help="v1.5：只处理本地观察通道（observed.jsonl），不抓 arXiv（测 A 类卡、或 arXiv 不通时用）")
    p.add_argument("--fulltext-max", type=int, default=None,
                   help="全文模式每次最多读几篇入围论文（默认 4；其余留到下次）")
    p.add_argument("--cache-dir", type=Path, help="原文缓存目录（离线测试用 tests/fixtures/fulltext_cache）")
    p.add_argument("--fetch-mode", default=None,
                   help="论文读取模式：abstract（默认，只读摘要）或 fulltext（通读全文+相关文献，约慢 4–5 倍）")
    f = sub.choices["finalize"]
    f.add_argument("--cards", type=Path)
    f.add_argument("--accept-partial", action="store_true", help="只交通过校验的卡片，其余写进 sandbox_validation.errors")
    f.add_argument("--drop", help="v1.5.7：把这些卡从本轮去掉（逗号分隔；卡片 id、文件名 05 / 05.json 或 source.ref）。"
                                  "文件移到 cards/_dropped/，不用删除工具")
    a = ap.parse_args()
    return cmd_prepare(a) if a.cmd == "prepare" else cmd_finalize(a)


if __name__ == "__main__":
    raise SystemExit(main())
