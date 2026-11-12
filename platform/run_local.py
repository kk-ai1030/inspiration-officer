#!/usr/bin/env python3
"""本地兜底编排器（v1.4 起只在手动触发时使用；原 run_nightly.py，已取消一切定时）。

什么时候用它：沙箱连不上 arXiv（收货员收到 FETCH_BLOCKED 会自动调用它），或者你想在本机抓取、只让平台写卡片。
Flow:  validate state → L0/L1 triage (local script) → full texts → POST /api/agent/initiate with everything attached
       → wait → check skill really loaded → extract cards (INSP_CARDS_JSON or INSP_BUNDLE_JSON) → validate (+ arXiv)
       → write cards / update state ONLY on success → digest + notify.

Usage:
    python platform/run_local.py --config config.json --fetch-arxiv
    python platform/run_local.py --config config.json --raw raw.json --fake-agent-output reply.txt   # offline test
    python platform/run_local.py --config config.json --import-verdicts reply_with_verdict_block.txt

Exit codes: 0 ok / NO_CANDIDATES · 3 STATE_MISSING · 4 fetch failed · 5 agent/validation failed
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import time
import os
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
from sciencepal_client import MultiAuth, ApiError, SciencePal, collect_strings, extract_block, skill_loaded  # noqa: E402


def load_mod(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


V_STATE = load_mod("insp-init/scripts/validate_insp_state.py", "v_state")
TRIAGE = load_mod("insp-triage/scripts/triage.py", "triage")
V_CARDS = load_mod("insp-card/scripts/validate_cards.py", "v_cards")
ARXIV = load_mod("insp-card/scripts/verify_arxiv.py", "v_arxiv")
VERDICT = load_mod("insp-feedback/scripts/record_verdict.py", "verdict")
FT = load_mod("insp-card/scripts/fetch_fulltext.py", "fulltext")

WC = load_mod("insp-feedback/scripts/weights_config.py", "weights_config_rl")
CARD_START, CARD_END = "<<<INSP_CARDS_JSON", "INSP_CARDS_JSON>>>"
BUNDLE_START, BUNDLE_END = "<<<INSP_BUNDLE_JSON", "INSP_BUNDLE_JSON>>>"
TEST_MARK = ".test_state"


def cards_from_bundle(texts: list[str]) -> str | None:
    """v1.4 agents finish with an INSP_BUNDLE_JSON block; turn it back into a cards document."""
    bb = extract_block(texts, BUNDLE_START, BUNDLE_END)
    if not bb:
        return None
    try:
        b = json.loads(bb)
    except json.JSONDecodeError:
        return None
    meta = b.get("cards_doc_meta") or {}
    doc = {"schema_version": meta.get("schema_version") or "1.0", "flow_version": meta.get("flow_version") or b.get("flow_version"),
           "run_id": meta.get("run_id") or b.get("run_id"), "generated_at": meta.get("generated_at") or b.get("generated_at"),
           "status": "OK" if b.get("cards") else "NO_CANDIDATES", "cards": b.get("cards", []), "rejected": b.get("rejected", [])}
    return json.dumps(doc, ensure_ascii=False)
VERD_START, VERD_END = "<<<INSP_VERDICTS_JSONL", "INSP_VERDICTS_JSONL>>>"


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


RUNTIME: dict = {}  # cfg / cfg_path for notifications (set in main)
FAIL_STATUSES = {"VALIDATION_FAILED", "FETCH_FAILED", "STATE_MISSING", "AGENT_ERROR", "ASKED"}


def log_run(state_dir: Path, row: dict) -> None:
    with (state_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if row.get("status") in FAIL_STATUSES or str(row.get("status", "")).startswith("api_error"):
        review_notify(state_dir, "灵感运行失败：" + str(row.get("status")), "详情见 runs.jsonl / scheduled.log；状态没有被修改", link=False)


def review_notify(state_dir: Path, title: str, body: str, link: bool = True, query: dict | None = None,
                  button: str | None = None) -> None:
    """Card UI: make sure the local review server runs, then pop a Windows toast that opens it."""
    rv = (RUNTIME.get("cfg") or {}).get("review") or {}
    if not rv.get("enabled", os.name == "nt"):  # default: on for Windows, off elsewhere
        return
    try:
        rs = load_mod("platform/review_server.py", "review_server")
        url = rs.ensure_server(RUNTIME.get("cfg_path"), state_dir.resolve(), int(rv.get("port", rs.DEFAULT_PORT))) if link else None
        if url and query:   # v1.5.2: e.g. act=<job_id> → the page opens the receipts view and highlights that job
            from urllib.parse import urlencode
            url += ("&" if "?" in url else "?") + urlencode(query)
        d = rs.toast(title, body, url, button) if rv.get("windows_toast", True) else {"called": False, "visible": False, "why": "windows_toast=false"}
        try:
            (state_dir / "inbox").mkdir(parents=True, exist_ok=True)
            (state_dir / "inbox" / "toast_last.json").write_text(json.dumps({"at": now().isoformat(timespec="seconds"), "title": title, **d},
                                                                            ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        if d.get("called"):
            vis = {True: "已进通知中心", False: "未显示", None: "已调用，未能确认是否显示"}[d.get("visible")]
            print(f"TOAST {vis}（{d.get('why')}；setting={d.get('setting')} history={d.get('history')} session={d.get('session_id')}）")
        if url:
            print("评价页：" + url.split("?")[0] + "（或运行 python platform/review_server.py --config <config> --open）")
            if d.get("visible") is False and rv.get("fallback_open_browser", True) and d.get("session_id") not in (0, None):
                import webbrowser
                webbrowser.open(url)
                print("通知不可见，已改为直接在浏览器打开评价页")
    except Exception as exc:  # notification failure must never break the run
        print("WARN review notify:", exc)


ABSTRACT_RULES = """【摘要模式（arxiv_fetch_mode=abstract，默认）】本次只提供标题和摘要，没有原文附件：
- 只依据 triage.json 里每个候选的 title / abstract 写卡片；不要抓取或推测原文内容；
- source.quote 从摘要逐字复制；不写 fulltext_quotes / related_evidence（写了也会被忽略）；
- 机制、最小验证、判据只能建立在摘要说出的内容上；摘要没有说的细节写进 hypothesis 作为待验证的假设，不得当作事实；
- 链接由脚本自动补到卡片上（source.url = arXiv 摘要页），你不需要也不要自己写链接。"""

FULLTEXT_RULES = """【全文模式（arxiv_fetch_mode=fulltext）】附件里有原文：ft_<arXiv ID>.md（候选论文全文）、rel_<arXiv ID>.md（相关文献全文）、fulltext_index.json（对应关系）。
每张卡片必须先读候选论文的全文和 fulltext_index.json 列出的相关文献全文，再写卡片：
- source.fulltext_quotes：1–4 条从 ft_<ID>.md 正文逐字复制的引文 {{quote, section}}，section 写原文里的章节标题，至少 1 条不在摘要里；
- related_evidence：至少 1 条 {{ref, relation, quote, why}}，ref 只能是附件给出的相关文献，quote 从 rel_<ID>.md 逐字复制；
- 只读过摘要就写出的卡片会被外部校验器整张丢弃；
- 找引文只用 prepare 打印的短命令：python3 /workspace/fq <ID>（一次 1–2 个 ID，会自动在 /workspace/_refs/ 里找附件）；禁止用 && 串多个 grep，一次一条命令，不要 cat 整个原文文件；连续失败 2 次就用已有引文出卡；
- 链接由脚本自动补到卡片上（source.url 摘要页 + source.fulltext_url 读过的全文），你不需要自己写链接。"""


OBSERVED_RULES = """【观察通道 v1.5】triage.json 里还有 observed_shortlist（来自用户本地代码 / 日志 / 笔记 / 使用记录的发现，不是论文）。
对其中每个合格候选按 insp-card 的“观察型卡片”规则写卡：source.kind = 候选的 kind，source.ref = 候选 id（obs_ 开头），
quote 从候选 abstract（证据原文）里逐字复制 6–400 个字符，type 只能取 allowed_types 里的值，数字来源写 observed。
"""


def build_prompt(run_id: str, today: str, counts: dict, state: dict | None = None,  # noqa: C901
                 fetch_mode: str = "abstract") -> str:
    # NOTE: deliberately does NOT contain the flow_version canary; only the insp-card skill has it.
    extra = ""
    if (state or {}).get("quota_profile") == "test":
        qt = state.get("quota_test", {})
        types = qt.get("target_types", ["knowledge", "hypothesis", "process"])
        extra = (f"\n【测试模式·学习闭环实验】配额改由外部脚本按“每次运行名额”截断，你不需要按周配额自行截断。"
                 f"请对 shortlisted 中每个合格候选都写卡片（最多 {int(qt.get('agent_max_cards', 8))} 张），"
                 f"类型尽量覆盖 {' / '.join(types)}，每种至少 1 张（如果候选允许）。类型必须如实反映卡片内容，不得为了凑类型而改写含义。\n")
    full = fetch_mode == "fulltext"
    rules = FULLTEXT_RULES if full else ABSTRACT_RULES
    what = "预筛结果和原文" if full else "预筛结果（含摘要）"
    return f"""【仅本次任务】主动探索灵感（本地兜底模式：{what}已经作为附件提供；论文读取模式 = {fetch_mode}）。run_id = {run_id}，日期 = {today}。

第一步：调用技能加载工具加载 insp-nightly，并复述其中 3 条硬纪律。
第二步：按 insp-nightly 的路由（附件里已有 triage.json → 不要重新抓取），加载 insp-card（必须实际加载，不得凭记忆模拟），把附件 triage.json 中 shortlisted 的候选整理成卡片。

附件在 /workspace/_refs/ 下（可能带子目录，用 ls -R /workspace/_refs 查找）：
state.json、negative.jsonl、rubric.md、weights.json、interests.md、claims.md、triage.json{"、ft_*.md、rel_*.md、fulltext_index.json" if full else ""}。

{rules}
预筛计数由脚本生成，不得修改：{json.dumps(counts, ensure_ascii=False)}
{OBSERVED_RULES if counts.get("observed_shortlisted") else ""}{extra}
要求：
- 禁止调用 ask；信息不足时输出 NEED_INPUT: <原因> 后结束。
- 缺少 state.json / negative.jsonl / rubric.md 时只输出 STATE_MISSING: <缺失文件>。
- 不要联网搜索，不要生成 docx/pdf。
- 最终回答包含且只包含一个结果块：
{CARD_START}
{{...JSON...}}
{CARD_END}
- 同时把同一份 JSON 写到 /workspace/inspiration_out/cards.json。
- 输出结果块后直接结束回答：不要调用 complete 工具，不要放附件（结果块是正文，不是附件）。"""


def stage_attachments(state_dir: Path, triage_path: Path, run_dir: Path, extra: list[Path] | None = None) -> list[Path]:
    att = run_dir / "attach"
    att.mkdir(parents=True, exist_ok=True)
    pairs = [("state.json", "state.json"), ("negative.jsonl", "negative.jsonl"),
             ("taste/rubric.md", "rubric.md"), ("taste/weights.json", "weights.json"),
             ("profile/interests.md", "interests.md"), ("profile/claims.md", "claims.md")]
    files = []
    for src, dst in pairs:
        shutil.copy(state_dir / src, att / dst)
        files.append(att / dst)
    for src, dst in (("profile/observed.md", "observed.md"), ("observed.jsonl", "observed.jsonl")):  # v1.5, optional
        if (state_dir / src).is_file() and (state_dir / src).stat().st_size:
            shutil.copy(state_dir / src, att / dst)
            files.append(att / dst)
    shutil.copy(triage_path, att / "triage.json")
    files.append(att / "triage.json")
    for p in extra or []:  # full-text bundle: ft_<id>.md, rel_<id>.md, fulltext_index.json
        if p.parent != att:
            shutil.copy(p, att / p.name)
        files.append(att / p.name)
    # An empty negative.jsonl is legal, but zero-byte uploads may be dropped: send a single newline instead.
    if (att / "negative.jsonl").stat().st_size == 0:
        (att / "negative.jsonl").write_text("\n", encoding="utf-8")
    return files


def setup_client(cfg: dict, run_client: SciencePal | None) -> SciencePal | None:
    """Session-token client for endpoints an API key cannot call (agent-run/skills, skills, memory)."""
    tok = os.environ.get(cfg.get("setup_token_env", "SCIENCEPAL_SETUP_TOKEN"), "")
    f = Path(os.path.expanduser(cfg.get("setup_token_file", "~/.sciencepal/session_token")))
    if not tok and f.is_file():  # v1.4.2: same lookup as receiver.py / setup_platform.py
        tok = f.read_text(encoding="utf-8").strip()
    if tok:
        return SciencePal(cfg["base_url"], tok, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer"))
    return run_client


def file_reader(cfg: dict, run_client: SciencePal):
    """Thread files need the session token on the real platform (API key → 403); fall back to the key."""
    sess = setup_client(cfg, None)
    return MultiAuth([sess, run_client], ["session", "api_key"]) if sess is not None else run_client


def run_token(cfg: dict) -> str:
    """API key for unattended runs: env var first, then a token file (scheduled tasks on Windows often
    do not see environment variables set after logon)."""
    for name in (cfg.get("token_env"), "SCIENCEPAL_RUN_TOKEN", "SCIENCEPAL_TOKEN"):
        if name and os.environ.get(name):
            return os.environ[name]
    p = Path(os.path.expanduser(cfg.get("token_file", "~/.sciencepal/run_token")))
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return ""


TOOL_TYPES = {"tool", "tool_call", "tool_use", "tool_result", "function_call"}


def stream_skill_evidence(events: list, skill: str) -> dict:
    """Look for a platform tool event that loads `skill` (e.g. load_skill(insp-card)). Assistant text is ignored,
    so the model merely *saying* it loaded the skill does not count."""
    names: list[str] = []
    hits: list[str] = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        typ = str(e.get("type", "")).lower()
        if typ not in TOOL_TYPES and not any(k in e for k in ("tool_name", "tool_call", "function")):
            continue
        blob = json.dumps(e, ensure_ascii=False, default=str)
        m = re.findall(r'"(?:tool_name|name|function_name|tool)"\s*:\s*"([A-Za-z0-9_.:-]{2,60})"', blob)
        names.extend(m)
        if re.search(r"load[_-]?skill", blob, flags=re.I) and skill in blob:
            hits.append(blob[:240])
    uniq = list(dict.fromkeys(names))
    return {"loaded": bool(hits), "tool_events": sum(1 for e in events or [] if isinstance(e, dict) and str(e.get("type", "")).lower() in TOOL_TYPES),
            "tool_names": uniq[:40], "hits": hits[:3]}


def get_agent_reply(cfg: dict, sp: SciencePal | None, prompt: str, files: list[Path], agent_id: str | None,
                    fake: Path | None, run_dir: Path) -> dict:
    if fake:
        return {"text": fake.read_text(encoding="utf-8"), "thread_id": None, "agent_run_id": None,
                "skill_loaded": "true", "status": "completed(fake)"}
    assert sp is not None
    resp = sp.initiate(prompt, agent_id=agent_id, files=files, model_name=cfg.get("model_name"),
                       web_search_on=bool(cfg.get("web_search_on")), workspace_context=cfg.get("workspace_context"))
    thread_id, run_id = resp.get("thread_id"), resp.get("agent_run_id")
    events = sp.stream_run(run_id, timeout_s=int(cfg.get("run_timeout_s", 1500))) if run_id else []
    final = sp.wait_run(run_id, timeout_s=120, poll_s=5) if run_id else None
    (run_dir / "stream.json").write_text(json.dumps(events, ensure_ascii=False)[:5_000_000], encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps(final, ensure_ascii=False, default=str), encoding="utf-8")
    texts = collect_strings(events) + collect_strings(final)
    block = extract_block(texts, CARD_START, CARD_END) or cards_from_bundle(texts)
    text = f"{CARD_START}\n{block}\n{CARD_END}" if block else "\n".join(t for t in texts if "STATE_MISSING" in t or "NEED_INPUT" in t)[:2000]
    if not block and thread_id:
        rd = file_reader(cfg, sp)
        for p in cfg.get("output_file_candidates", []):
            try:
                raw = rd.read_thread_file(thread_id, p).decode("utf-8", "replace")
                json.loads(raw)
                text = f"{CARD_START}\n{raw}\n{CARD_END}"
                break
            except (ApiError, json.JSONDecodeError, UnicodeError):
                continue
    skill = cfg.get("skill_to_check", "insp-card")
    sev = stream_skill_evidence(events, skill)  # platform-emitted tool events; readable with an API key
    try:
        ev = setup_client(cfg, sp).run_skills(run_id) if run_id else None
        (run_dir / "skills_evidence.json").write_text(json.dumps({"api": ev, "stream": sev}, ensure_ascii=False, default=str), encoding="utf-8")
        loaded = skill_loaded(ev, skill)
        if loaded == "unknown" and sev["loaded"]:
            loaded = "true(stream)"
    except ApiError as exc:
        # An API key (sp_live_) gets 403 on agent-run/{id}/skills. Fall back to the stream's tool events
        # (load_skill + skill name, emitted by the platform, not by the model); flow_version stays a hard gate.
        (run_dir / "skills_evidence.json").write_text(json.dumps({"api_error": str(exc)[:300], "stream": sev}, ensure_ascii=False), encoding="utf-8")
        loaded = "true(stream)" if sev["loaded"] else ("unverified" if cfg.get("require_skill_loaded", True) in ("auto", "evidence") else "unknown")
    status = str((final or {}).get("status", "")) if isinstance(final, dict) else ""
    return {"text": text, "thread_id": thread_id, "agent_run_id": run_id, "skill_loaded": loaded, "status": status}


def weights_version(state_dir: Path) -> int:
    try:
        return int(json.loads((state_dir / "taste" / "weights.json").read_text(encoding="utf-8")).get("version", 0))
    except (OSError, ValueError):
        return 0


def notify(cfg: dict, title: str, body: str) -> None:
    hook = os.environ.get(cfg.get("notify", {}).get("feishu_webhook_env", ""), "")
    if not hook:
        return
    data = json.dumps({"msg_type": "text", "content": {"text": f"{title}\n{body}"[:3500]}}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(hook, data=data, headers={"Content-Type": "application/json"}), timeout=15)
    except Exception as exc:  # notification failure must not break the run
        print("WARN notify failed:", exc)


def write_digest(state_dir: Path, today: str, cards: list[dict], counts: dict, row: dict | None = None) -> Path:
    d = state_dir / "digest"
    d.mkdir(exist_ok=True)
    row = row or {}
    mode_label = "通读全文" if row.get("fetch_mode") == "fulltext" else "只读摘要（默认）"
    L = [f"# 今日灵感 {today}", "", f"预筛：raw {counts['raw']} / new {counts['new']} / shortlisted {counts['shortlisted']} → 卡片 {len(cards)}",
         f"论文读取模式：{mode_label}"]
    if counts.get("observed"):
        L.append(f"观察通道：observed {counts['observed']} 条 / 入围 {counts.get('observed_shortlisted', 0)} 条"
                 f"（本地代码、日志、笔记、使用记录）")
    L.append("")
    notes = []
    for x in row.get("arxiv_rejected", []):
        notes.append(f"- 丢弃 {x['ref']}（{x.get('reason')}）：页面标题「{x.get('page_title')}」vs 预筛标题「{x.get('expected_title')}」")
    for x in row.get("arxiv_unverified", []):
        notes.append(f"- 未完成核验但保留 {x['ref']}（{x['status']}），卡片上标为 verified=false")
    if row.get("state_healed"):
        notes.append(f"- state.json 落后于卡片库，已把 {len(row['state_healed'])} 篇已推送论文补回去重清单")
    ftr = row.get("fulltext") if isinstance(row.get("fulltext"), dict) else {}
    for x in ftr.get("pending", []):
        notes.append(f"- 暂不出卡 {x['ref']}「{x.get('title')}」：{x['reason']}；下次运行重试（第 {x['tries']} 次）")
    for x in ftr.get("dropped", []):
        notes.append(f"- 放弃 {x['ref']}「{x.get('title')}」：{x['reason']}（已重试 {x['tries']} 次）")
    if row.get("fetch_mode_warning"):
        notes.append(f"- {row['fetch_mode_warning']}")
    if notes:
        L += ["## 本次需要你知道的", *notes, ""]
    for c in cards:
        L += [f"## {c['id']} · {c['type']} · {c.get('subfield', '-')} · {c['cost_estimate'].get('wall_clock')} · risk={c['cost_estimate'].get('risk')}",
              f"**{c['title']}**", "", c["one_line"], "", f"- 为什么是我：{c['why_me']}",
              f"- 最小验证：{c['minimal_check']}", f"- 失败判据：{c['falsifier']}",
              f"- 来源：{c['source']['ref']}（verified={c['source']['verified']}；"
              + {"fulltext": "通读全文", "observed": f"本地观察 · {c['source'].get('evidence_ref') or c['source'].get('kind')}"}
              .get(c['source'].get('read_mode'), "只读摘要") + "）",
              *([f"- 链接：{c['source']['url']}"] if c['source'].get('url') else []),
              *([f"- 全文：{c['source']['fulltext_url']}"] if c['source'].get('fulltext_url') else []),
              *[f"- 原文 §{q.get('section')}：“{q.get('quote')}”" for q in (c['source'].get('fulltext_quotes') or [])],
              *[f"- 相关文献 {r.get('ref')}（{r.get('relation')}）：“{r.get('quote')}” —— {r.get('why')}" for r in (c.get('related_evidence') or [])],
              f"- 回放：thread={c.get('_trace', {}).get('thread_id')} run={c.get('_trace', {}).get('agent_run_id')}", "",
              "```bash", f"python3 insp-feedback/scripts/record_verdict.py --state-dir {state_dir} --card-id {c['id']} --action adopt --reason \"\"",
              f"python3 insp-feedback/scripts/record_verdict.py --state-dir {state_dir} --card-id {c['id']} --action reject --reason \"\"", "```", ""]
    p = d / f"{today}.md"
    p.write_text("\n".join(L), encoding="utf-8")
    return p


def commit_success(state_dir: Path, tri: dict, cards: list[dict], today: str, run_id: str) -> None:
    st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    seen = list(dict.fromkeys(st.get("seen_ids", []) + tri.get("new_ids", [])))
    st["seen_ids"] = seen
    st["revival"] = tri.get("revival_state", st.get("revival"))
    if "fulltext_retry" in tri:  # candidates waiting for their full text (retried next run, then dropped)
        st["fulltext_retry"] = tri["fulltext_retry"]
    ts = now().isoformat(timespec="seconds")
    titles = {c["id"]: c["title"] for c in tri.get("shortlisted", [])}
    for c in cards:
        ref = V_CARDS.norm_id(c["source"]["ref"])
        st["pushed"].append({"id": ref, "card_id": c["id"], "title": titles.get(ref, c["title"]), "type": c["type"],
                          "subfield": c.get("subfield"), "pushed_at": ts})
        day = st["quota_used"].setdefault(today, {})
        day[c["type"]] = day.get(c["type"], 0) + 1
    st["last_run_at"], st["last_run_id"], st["bootstrap"] = ts, run_id, False
    tmp = state_dir / "state.json.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_dir / "state.json")


def heal_pushed_from_archive(state_dir: Path) -> list[str]:
    """The card archive (cards/<date>/cards.json) is the ground truth of what was pushed. If state.json is behind it
    (restored from a backup / re-initialised), re-add the missing refs to state.pushed so they are never pushed again.
    T3: state.json had been reset, and 2608.17189 was pushed twice on the same day."""
    sf = state_dir / "state.json"
    st = json.loads(sf.read_text(encoding="utf-8"))
    have = {V_CARDS.norm_id(p.get("id", "")) for p in st.get("pushed", [])}
    missing: list[dict] = []
    for f in sorted((state_dir / "cards").glob("*/cards.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for c in doc.get("cards", []):
            ref = V_CARDS.norm_id((c.get("source") or {}).get("ref", ""))
            if ref and ref not in have:
                have.add(ref)
                missing.append({"id": ref, "card_id": c.get("id"), "title": c.get("title", ""), "type": c.get("type", "knowledge"),
                                "subfield": c.get("subfield"), "pushed_at": c.get("created_at") or doc.get("date"), "healed": True})
    if missing:
        ts = now().strftime("%Y%m%d-%H%M%S")
        shutil.copy(sf, sf.with_name(f"state.json.bak-heal-{ts}"))
        st.setdefault("pushed", []).extend(missing)
        tmp = state_dir / "state.json.tmp"
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(sf)
        print(f"WARN state.json 落后于卡片库：{len(missing)} 篇已推送论文不在 state.pushed 里（状态可能被还原过）。"
              f"已补回去重清单，备份 state.json.bak-heal-{ts}：" + ", ".join(m["id"] for m in missing))
    return [m["id"] for m in missing]


def import_verdicts(state_dir: Path, path: Path) -> int:
    block = extract_block([path.read_text(encoding="utf-8")], VERD_START, VERD_END)
    if not block:
        print("ERROR: 找不到 INSP_VERDICTS_JSONL 结果块")
        return 1
    for line in block.splitlines():
        if line.strip():
            v = json.loads(line)
            e = VERDICT.record(state_dir, v["card_id"], v["action"], v.get("reason", ""), v.get("block_topic"))
            print(f"OK: {e['card_id']} ← {e['label']}")
    return 0



def finalize_cards(cfg: dict, state_dir: Path, tri: dict, kept: list[dict], today: str, run_id: str, row: dict,
                   reply: dict, no_arxiv_check: bool = False) -> int:
    """Shared tail of every run (local orchestrator AND receiver): arXiv check → archive cards with orchestrator ids
    → commit state → digest → notifications. Only called after the cards passed validation."""
    # 4. arXiv existence check
    if kept and cfg.get("verify_arxiv_online", True) and not no_arxiv_check:
        titles = {V_CARDS.norm_id(c["id"]): c["title"] for c in tri["shortlisted"]}
        survivors = []
        for c in kept:
            if c["source"].get("kind") != "arxiv":
                survivors.append(c)
                continue
            ref = V_CARDS.norm_id(c["source"]["ref"])
            status, page_title = ARXIV.fetch(ref)
            for wait in (5, 15):  # 429 / timeouts: back off and retry before deciding anything
                if status == "ok" or status == "not_found":
                    break
                time.sleep(wait)
                status, page_title = ARXIV.fetch(ref)
            ok, ratio = ARXIV.title_match(page_title, titles.get(ref, ""), float(cfg.get("arxiv_title_fuzzy", 0.9)))
            ok = ok and status == "ok"
            info = {"agent_card_id": c["id"], "ref": ref, "status": status, "ratio": ratio,
                    "page_title": page_title, "expected_title": titles.get(ref)}
            if ok:
                c["source"]["verified"] = True
                if ratio < 1.0:
                    row.setdefault("arxiv_fuzzy", []).append(info)
                survivors.append(c)
            elif status not in ("ok", "not_found") and cfg.get("arxiv_network_policy", "keep_unverified") == "keep_unverified":
                # Network trouble is "not checked", not "fake": the ref already came from the arXiv API via triage.
                c["source"]["verified"] = False
                c["source"]["verify_status"] = status
                row.setdefault("arxiv_unverified", []).append(info)
                print(f"WARN arXiv 核验未完成（{status}），保留为未核验：{ref}")
                survivors.append(c)
            else:
                info["reason"] = "not_found" if status == "not_found" else f"title_mismatch(ratio={ratio})"
                row.setdefault("arxiv_rejected", []).append(info)
                print(f"WARN arXiv 核验未通过，丢弃卡片 {c['id']}（{ref}）：{info['reason']}\n  页面标题：{page_title}\n  预筛标题：{titles.get(ref)}")
        kept = survivors

    out_dir = state_dir / "cards" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "cards.json"
    prev = json.loads(out_file.read_text(encoding="utf-8"))["cards"] if out_file.exists() else []
    for n, c in enumerate(kept, start=len(prev) + 1):
        # IDs are assigned by the orchestrator so they stay unique across several runs on the same day.
        c["_agent_card_id"] = c["id"]
        # memory canary (memory_seeds.json): record it, then strip it so the user never sees the marker
        t = str(c.get("title", "")).rstrip()
        c["memory_canary"] = t.endswith("◆")
        c["title"] = t.rstrip("◆").rstrip()
        c["id"] = f"ins_{today.replace('-', '')}_{n:03d}"
        c["_trace"] = {"run_id": run_id, "thread_id": reply.get("thread_id"), "agent_run_id": reply.get("agent_run_id"),
                       "bundle_run_id": reply.get("bundle_run_id"),
                       "skill_loaded": reply.get("skill_loaded"), "weights_version": weights_version(state_dir)}
    out_file.write_text(json.dumps({"date": today, "cards": prev + kept}, ensure_ascii=False, indent=2), encoding="utf-8")
    commit_success(state_dir, tri, kept, today, run_id)
    digest = write_digest(state_dir, today, kept, tri["counts"], row)
    canary = sum(1 for c in kept if c.get("memory_canary"))
    row.update(status="OK" if kept else "NO_CANDIDATES", cards=[c["id"] for c in kept], thread_id=reply.get("thread_id"),
               agent_run_id=reply.get("agent_run_id"), skill_loaded=reply.get("skill_loaded"),
               memory_canary=f"{canary}/{len(kept)}", weights_version=weights_version(state_dir),
               quota_profile=json.loads((state_dir / "state.json").read_text(encoding="utf-8")).get("quota_profile", "prod"),
               types={t: sum(1 for c in kept if c["type"] == t) for t in sorted({c["type"] for c in kept})},
               finished_at=now().isoformat(timespec="seconds"))
    if kept:
        try:
            VERDICT.rebuild_inbox(state_dir)
        except OSError as exc:
            print("WARN inbox:", exc)
    log_run(state_dir, row)
    print(f"OK: {len(kept)} 张卡片 → {out_file}；摘要 {digest}")
    if not kept and row.get("arxiv_rejected"):
        review_notify(state_dir, "灵感机制：本次卡片全部未通过 arXiv 核验", f"{len(row['arxiv_rejected'])} 张被丢弃，详见 runs.jsonl / 摘要", link=False)
    if kept:
        by_type: dict = {}
        for c in kept:
            by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        label = {"knowledge": "知识", "hypothesis": "假说", "process": "流程", "optimization": "优化"}
        extra = f"（另有 {len(row['arxiv_rejected'])} 张因 arXiv 核验被丢弃，见摘要）" if row.get("arxiv_rejected") else ""
        review_notify(state_dir, f"新灵感 {len(kept)} 张，点击开始评价",
                      " · ".join(f"{label.get(t, t)} {n}" for t, n in sorted(by_type.items())) + "　|　" + kept[0]["title"][:40] + extra)
        notify(cfg, f"今日灵感 {len(kept)} 条", "\n".join(f"{c['id']} [{c['type']}] {c['one_line']}" for c in kept))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="灵感机制夜间编排器")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--raw", type=Path)
    ap.add_argument("--fetch-arxiv", action="store_true")
    ap.add_argument("--fake-agent-output", type=Path, help="离线测试：用文件内容代替 agent 回复")
    ap.add_argument("--import-verdicts", type=Path)
    ap.add_argument("--no-arxiv-check", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    RUNTIME.update(cfg=cfg, cfg_path=a.config.resolve())
    base = a.config.resolve().parent
    state_dir = (base / cfg["state_dir"]).resolve()
    VERDICT.set_config(a.config.resolve())  # event weights (layer 1) come from config.json -> weights.events
    try:
        WC.load(a.config.resolve())  # BUG-7: a broken weights block fails here, not silently later
    except Exception as exc:
        print(f"CONFIG_INVALID: {exc}")
        return 2
    if a.import_verdicts:
        return import_verdicts(state_dir, a.import_verdicts)

    t0 = now()
    today = t0.date().isoformat()
    run_id = "run-" + t0.strftime("%Y%m%d-%H%M%S")
    run_dir = (base / cfg.get("work_dir", "runs") / run_id).resolve()
    k = 1
    while run_dir.exists():  # two runs in the same second must not share (and overwrite) one evidence folder
        k += 1
        run_dir = run_dir.with_name(f"{run_id.split('_')[0]}_{k}")
    run_id = run_dir.name
    run_dir.mkdir(parents=True, exist_ok=True)
    fetch_mode, mode_warn = FT.cfg_fetch_mode(cfg)
    row = {"run_id": run_id, "started_at": t0.isoformat(timespec="seconds"), "fetch_mode": fetch_mode}
    if mode_warn:
        row["fetch_mode_warning"] = mode_warn
        print("WARN", mode_warn)
    print(f"FETCH_MODE {fetch_mode}（config.json → arxiv_fetch_mode；默认 abstract）")

    # 1. state gate
    errs = V_STATE.validate_dir(state_dir)
    if errs:
        row.update(status="STATE_MISSING", errors=errs)
        log_run(state_dir, row) if state_dir.is_dir() and (state_dir / "runs.jsonl").exists() else None
        print("STATE_MISSING:", errs)
        notify(cfg, "灵感机制：STATE_MISSING", "\n".join(errs))
        return 3

    if cfg.get("heal_pushed_from_archive", True):
        healed = heal_pushed_from_archive(state_dir)
        if healed:
            row["state_healed"] = healed

    # 2. L0/L1
    if a.fetch_arxiv:
        st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        try:
            raw = TRIAGE.fetch_arxiv(st.get("sources", []))
        except Exception as exc:
            row.update(status="FETCH_FAILED", error=str(exc))
            log_run(state_dir, row)
            print("FETCH_FAILED:", exc)
            return 4
        (run_dir / "raw_items.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    elif a.raw:
        raw = json.loads(a.raw.read_text(encoding="utf-8"))
    else:
        ap.error("需要 --raw 或 --fetch-arxiv")
    if any(str(x.get("id", "")).startswith("9999.") for x in raw) and not (state_dir / TEST_MARK).exists():
        # BUG-6: fixtures must never be written into a real state dir
        print(f"REFUSED: 输入里有测试夹具 ID（9999.*），但 {state_dir} 不是测试状态目录（缺少 {TEST_MARK}）。"
              "请复制一份临时状态目录再测，真实状态没有被修改。")
        return 3
    code, tri = TRIAGE.run_triage(state_dir, raw)
    if code == 3:
        row.update(status="STATE_MISSING", errors=[tri.get("detail")])
        log_run(state_dir, row)
        print("STATE_MISSING:", tri.get("detail"))
        return 3
    tri_path = run_dir / "triage.json"
    tri_path.write_text(json.dumps(tri, ensure_ascii=False, indent=2), encoding="utf-8")
    row["counts"] = tri["counts"]
    print("TRIAGE", tri["status"], tri["counts"])

    # 2b. full text of every shortlisted paper + related literature — ONLY when arxiv_fetch_mode = "fulltext"
    ftc = dict(FT.DEFAULTS, **(cfg.get("fulltext") or {}))
    bundle_dir, ft_files = run_dir / "fulltext", []
    if tri["status"] != "NO_CANDIDATES":
        if fetch_mode == "fulltext":
            cache_dir = Path(ftc.get("cache_dir") or (state_dir / "fulltext"))
            cache_dir = cache_dir if cache_dir.is_absolute() else (base / cache_dir).resolve()
            st0 = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
            rep = FT.enrich(tri, cache_dir, bundle_dir, ftc, st0.get("fulltext_retry", {}))
            tri["fulltext_retry"] = rep["retry_state"]
            ft_files = rep["files"]
            row["fulltext"] = {"ok": rep["ok"], "pending": rep["pending"], "dropped": rep["dropped"]}
            tri_path.write_text(json.dumps(tri, ensure_ascii=False, indent=2), encoding="utf-8")
            row["counts"] = tri["counts"]
            print("FULLTEXT", {k: tri["counts"].get(k) for k in ("fulltext_ok", "fulltext_pending", "fulltext_dropped")})
        else:
            print("摘要模式：不抓原文（要通读全文，把 config.json 里的 arxiv_fetch_mode 改成 \"fulltext\"）")
            row["fulltext"] = "skipped(abstract mode)"
    tri["fetch_mode"] = fetch_mode
    for c in tri.get("shortlisted", []):
        c["links"] = FT.arxiv_links(c.get("id", ""), c.get("fulltext"))
    tri_path.write_text(json.dumps(tri, ensure_ascii=False, indent=2), encoding="utf-8")
    if tri["status"] == "NO_CANDIDATES":
        commit_success(state_dir, tri, [], today, run_id)
        row.update(status="NO_CANDIDATES", finished_at=now().isoformat(timespec="seconds"))
        log_run(state_dir, row)
        return 0

    # 3. L2 on platform
    sp, agent_id = None, None
    if not a.fake_agent_output:
        token = run_token(cfg)
        if not token:
            print("ERROR: token 环境变量未设置")
            return 5
        sp = SciencePal(cfg["base_url"], token, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer"))
        ids = json.loads((base / cfg.get("platform_ids_file", "platform_ids.json")).read_text(encoding="utf-8"))
        agent_id = ids.get("agent_id")
        try:  # BUG-3: token and agent must belong to the same account — say so now, not as a bare 403 later
            visible = {str(x.get("agent_id") or x.get("id")) for x in sp.whoami_agents()}
            if visible and agent_id not in visible:
                msg = (f"token 与灵感官不属于同一个账号（agent {str(agent_id)[:8]}… 不在该 token 可见的 {len(visible)} 个智能体中）。"
                       "请在灵感官所在账号重新生成 API Key 写入 ~/.sciencepal/run_token，或在当前账号重新运行 setup_platform.py")
                row.update(status="AGENT_ERROR", problem=msg)
                log_run(state_dir, row)
                print("AGENT_ERROR:", msg)
                return 5
        except ApiError as exc:
            print(f"WARN 无法列出智能体（HTTP {exc.status}），跳过账号一致性检查")
    files = stage_attachments(state_dir, tri_path, run_dir, ft_files)
    prompt = build_prompt(run_id, today, tri["counts"], json.loads((state_dir / "state.json").read_text(encoding="utf-8")),
                          fetch_mode)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    expect_flow = V_CARDS.FLOW_VERSION
    if cfg.get("expect_flow_version") and cfg["expect_flow_version"] != expect_flow:
        print(f"WARN config 里的 expect_flow_version={cfg['expect_flow_version']} 已过时，按包内版本 {expect_flow} 校验"
              f"（平台上的 insp-card 技能需要重新导入，否则会被判定为“没有加载技能”）")
    attempts, kept, last_problem, reply = int(cfg.get("max_attempts", 2)), [], None, {}
    for attempt in range(1, attempts + 1):
        try:
            reply = get_agent_reply(cfg, sp, prompt, files, agent_id, a.fake_agent_output, run_dir)
        except ApiError as exc:
            last_problem = f"api_error: {exc}"
            continue
        (run_dir / f"reply_{attempt}.txt").write_text(reply["text"], encoding="utf-8")
        if reply.get("thread_id"):  # the receiver must not ingest this conversation a second time
            (state_dir / "inbox").mkdir(exist_ok=True)
            with (state_dir / "inbox" / "collected.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": f"thread-owned:{reply['thread_id']}", "run_id": run_id, "status": "LOCAL_RUN"},
                                   ensure_ascii=False) + "\n")
        if "asked" in reply.get("status", "").lower():
            last_problem = "agent 进入 asked 状态（调用了 ask），API 无法回答"
            continue
        if "STATE_MISSING" in reply["text"] and CARD_START not in reply["text"]:
            last_problem = "agent 报告 STATE_MISSING（附件未挂载？）"
            break
        req = cfg.get("require_skill_loaded", True)
        sl = reply["skill_loaded"]
        if ((req is True and sl != "true") or (req == "auto" and sl == "false")
                or (req == "evidence" and sl not in ("true", "true(stream)"))):
            last_problem = f"skill {cfg.get('skill_to_check')} loaded={reply['skill_loaded']}"
            continue
        block = extract_block([reply["text"]], CARD_START, CARD_END)
        if not block:
            last_problem = "回复中没有结果块"
            continue
        try:
            doc = json.loads(block.strip().strip("`").removeprefix("json").strip())
        except json.JSONDecodeError as exc:
            last_problem = f"结果块不是合法 JSON：{exc}"
            continue
        (run_dir / "cards_raw.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        sel_log: list = []
        root_errs, per, kept = V_CARDS.validate(doc, tri, state_dir, expect_flow, None, sel_log,
                                                bundle_dir if (ft_files and fetch_mode == "fulltext") else None,
                                                fetch_mode == "fulltext",
                                                int(ftc.get("min_related", 1)))
        (run_dir / "selection.json").write_text(json.dumps({"weights_version": weights_version(state_dir), "log": sel_log},
                                                           ensure_ascii=False, indent=2), encoding="utf-8")
        row["validation"] = {"root": root_errs, "cards": per}
        if root_errs:
            last_problem = "; ".join(root_errs)
            kept = []
            continue
        last_problem = None
        break

    if last_problem:
        row.update(status="VALIDATION_FAILED", problem=last_problem, thread_id=reply.get("thread_id"),
                   agent_run_id=reply.get("agent_run_id"), finished_at=now().isoformat(timespec="seconds"))
        log_run(state_dir, row)
        print("VALIDATION_FAILED:", last_problem)
        notify(cfg, "灵感机制：本次运行失败（未推送、未更新状态）", last_problem)
        return 5

    return finalize_cards(cfg, state_dir, tri, kept, today, run_id, row, reply, a.no_arxiv_check)

if __name__ == "__main__":
    raise SystemExit(main())
