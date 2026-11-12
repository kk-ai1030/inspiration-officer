#!/usr/bin/env python3
"""“追问细节”（ask_detail，评价页按 5）的后台对话（v1.5.3）。

按 5 之后：照常记录“追问细节”评价（权重 +0.5，接口不变）→ 用固定模板向灵感官提问（新开一个平台对话）→
灵感官回答后，Windows 通知“来聊聊新的idea吧”，并在浏览器里打开这个对话，你可以直接接着聊。
流程和“立刻动手”（run_action.py）一样走后台进程，但它不分档、不出回执，产物就是这个对话本身。

固定模板（灵感官必须逐行填写）：
    <研究对象 + 子领域> <建立什么模型 / 用什么手段求解>
    产出：<可量化的指标或可观测现象>
    对照：<基线是什么>
    判据：<产出与基线满足什么关系才算成立>

用法（在包根目录）：
  python platform/ask_detail.py --config config.json --card-id ins_20261031_002     # 建追问并在后台提问
  python platform/ask_detail.py --config config.json --list
  python platform/ask_detail.py --config config.json --execute ask_...  [--fake-agent-output reply.txt]   # 后台工作进程 / 测试
  python platform/ask_detail.py --self-test

config.json（可选，不写就用默认值）：
  "ask": {"enabled": true, "open_thread": true, "timeout_s": 900, "cancel_grace_s": 5, "web_search_on": true}
    open_thread=true：回答出来后自动在浏览器打开这个对话（通知本身也能点开）
    web_search_on=true（v1.5.4）：追问时允许灵感官联网读原文（摘要页 / PDF），不再反过来向你要论文

文件（都在状态目录下）：
  inspiration/asks/<ask_id>/ask.json     追问与状态（原子写）    prompt.md  发出去的问题    answer.md  认出来的模板回答
  inspiration/asks/<ask_id>/worker.log   后台进程日志
  inspiration/asks.jsonl                 事件流：created / started / answered / failed / cancelled / discarded
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
from sciencepal_client import SciencePal, collect_strings  # noqa: E402


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


RA = _load("platform/run_action.py", "run_action_ask")
now_iso = RA.now_iso

TEMPLATE = ("<研究对象 + 子领域> <建立什么模型 / 用什么手段求解>\n"
            "产出：<可量化的指标或可观测现象>\n"
            "对照：<基线是什么>\n"
            "判据：<产出与基线满足什么关系才算成立>")
TEMPLATE_MARKS = ("<可量化的指标或可观测现象>", "<基线是什么>", "<产出与基线满足什么关系才算成立>", "【追问细节】")
DEFAULTS = {"enabled": True, "open_thread": True, "timeout_s": 900, "cancel_grace_s": 5, "web_search_on": True,
            "attach_local": True, "auto_retry": 1}   # v1.5.6: A 类卡附本地证据；平台“重复调用被中止”时同会话自动重试一次
OPEN = ("queued", "running")
STATUS_ZH = {"queued": "排队中", "running": "灵感官思考中", "answered": "已回答", "failed": "没有完成", "timeout": "超时",
             "cancelled": "已取消"}
TOAST_TITLE = "来聊聊新的idea吧"


# ----------------------------------------------------------------------------- config / files
def load(cfg_path: Path) -> tuple[dict, Path, dict]:
    cfg, state_dir, _ = RA.load_cfg(cfg_path)
    return cfg, state_dir, dict(DEFAULTS, **(cfg.get("ask") or {}))


def ask_dir(state_dir: Path, ask_id: str) -> Path:
    return state_dir / "asks" / ask_id


def read_ask(state_dir: Path, ask_id: str) -> dict:
    return json.loads((ask_dir(state_dir, ask_id) / "ask.json").read_text(encoding="utf-8"))


def save(state_dir: Path, a: dict, event: str | None = None, **extra) -> None:
    a["updated_at"] = now_iso()
    RA.write_json(ask_dir(state_dir, a["ask_id"]) / "ask.json", a)
    if event:
        row = {"ts": now_iso(), "ask_id": a["ask_id"], "card_id": a["card_id"], "event": event, **extra}
        with (state_dir / "asks.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def list_asks(state_dir: Path) -> list[dict]:
    out = []
    for p in sorted((state_dir / "asks").glob("*/ask.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def thread_url(cfg: dict, thread_id: str | None, project_id: str | None = None) -> str | None:
    """v1.5.4 (S6): /projects/<project_id>/thread/<id>; without the project id → review server /go/thread/<id>."""
    from sciencepal_client import web_thread_url
    return web_thread_url(cfg, thread_id, project_id)


def paper_abstract(state_dir: Path, card: dict) -> str:
    """v1.5.4 (S3b): the abstract the run already fetched (runs/<run>/triage.json shortlist), so the agent never has
    to ask the user for the paper. Looks in the card's own run first, then in every run (newest first)."""
    if str((card.get("source") or {}).get("abstract") or "").strip():   # v1.5.5: stored with the card at collection time
        return str(card["source"]["abstract"]).strip()[:3000]
    ref = str((card.get("source") or {}).get("ref") or "")
    key = re.sub(r"v\d+$", "", ref.split("/")[-1].replace("arXiv:", "").strip())
    if not key:
        return ""
    runs = state_dir.parent / "runs"
    own = (card.get("_trace") or {}).get("run_id")
    files = ([runs / own / "triage.json"] if own else []) + sorted(runs.glob("*/triage.json"), reverse=True)[:60]
    for f in files:
        try:
            tri = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for c in (tri.get("shortlist") or []) + (tri.get("shortlisted") or []):
            cid = re.sub(r"v\d+$", "", str(c.get("id") or c.get("raw_id") or "").split("/")[-1])
            if cid == key and c.get("origin") != "observed" and str(c.get("abstract") or "").strip():
                return str(c["abstract"]).strip()[:3000]
    return ""


# ----------------------------------------------------------------------------- prompt / answer
CARD_FIELDS = [("title", "标题"), ("one_line", "一句话"), ("type", "类型"), ("subfield", "子领域"), ("why_me", "为什么和我有关"),
               ("hypothesis", "假说"), ("minimal_check", "最小验证"), ("expected_observable", "预期判据"),
               ("falsifier", "推翻条件"), ("magnitude", "量级")]


LOCAL_KINDS = ("workspace", "log", "observed", "code", "notes", "data", "usage")


def is_local_card(card: dict) -> bool:
    """v1.5.6: a card whose evidence is on YOUR computer (本地观察 / A 类), not a paper with a link."""
    src = card.get("source") or {}
    return str(src.get("kind") or "") in LOCAL_KINDS or str(src.get("evidence_ref") or "").startswith("library://")


def build_prompt(card: dict, abstract: str = "", local_files: list[str] | None = None) -> str:
    lines = []
    for k, zh in CARD_FIELDS:
        v = card.get(k)
        if v:
            lines.append(f"- {zh}：{v}")
    src = card.get("source") or {}
    if src.get("ref") or src.get("quote"):
        lines.append(f"- 来源：{src.get('kind', '')} {src.get('ref', '')}" + (f"；原文：“{src['quote']}”" if src.get("quote") else ""))
    local = is_local_card(card)
    links = [(zh, src.get(k)) for k, zh in (("url", "摘要页"), ("pdf_url", "PDF 全文"), ("fulltext_url", "读过的全文"))]
    links = [f"{zh} {u}" for zh, u in links if u]
    if not links and src.get("kind") == "arxiv" and src.get("ref"):   # older cards: derive the arXiv links
        r = str(src["ref"]).replace("arXiv:", "").strip()
        links = [f"摘要页 https://arxiv.org/abs/{r}", f"PDF 全文 https://arxiv.org/pdf/{r}"]
    if links:
        lines.append("- 原文链接：" + "；".join(links))
    if abstract and not local:
        lines.append(f"- 摘要全文（运行时已抓取）：{abstract}")
    if local:   # v1.5.6 (ASK1): local evidence — say exactly what exists, so the agent never goes hunting for it
        ev = str(src.get("evidence_ref") or src.get("ref") or "")
        lines.append(f"- 本地证据：{ev}" + (f"（检测项 {src['detector']}）" if src.get("detector") else ""))
        if local_files:
            lines.append("- 附件：" + "、".join(local_files) + "（证据所在文件的打码副本，在 /workspace/_refs/ 下；用一次 ls -R /workspace/_refs 就能找到）")
    if local and local_files:
        rule3 = ("3. 这张卡的证据是用户本机的文件，已作为附件放在 /workspace/_refs/ 下（" + "、".join(local_files) + "）。"
                 "直接读这个附件回答；不要用 find / search_memory / search_history 去别处找原文件或补丁，工作区和记忆里都没有。\n"
                 "   附件里也读不到的数字写“待测”，并写出你根据现有材料最接近的推断，不要编。")
    elif local:
        rule3 = ("3. 这张卡的证据是用户本机的文件，沙箱里没有原文件，上面的“原文”片段就是全部可见内容。"
                 "不要用 find / ls / search_memory / search_history 去找原文件、补丁或路径——找不到，也不需要。\n"
                 "   只依据卡片和这段片段回答；读不到的数字写“待测”，并写出最接近的推断；确实需要更多上下文，就在最后问我一个具体问题。")
    elif links:
        rule3 = ("3. 依据这张卡片和它的来源。需要更多细节（基线、数据集、具体数字）时，自己用工具打开上面的原文链接去读（先摘要页，不够再读 PDF），\n"
                 "   不要向我索要论文、正文或摘要——这正是我请你来做的事。确实读不到时，把该数字写“待测”，并写出你根据现有材料最接近的推断，不要编。")
    else:
        rule3 = ("3. 只依据这张卡片回答，不要去工作区或记忆里找别的文件。读不到的数字写“待测”，并写出最接近的推断，不要编。")
    return f"""【追问细节】我想把下面这张灵感卡片聊具体。请严格按这个固定模板回答，四行都要填具体内容，不要保留尖括号：

{TEMPLATE}

卡片：
{chr(10).join(lines)}

要求：
1. 第一行写“研究对象 + 子领域”，空一格，再写“建立什么模型 / 用什么手段求解”。
2. 产出必须是可量化的指标或可观测现象；对照写清基线是什么；判据写清产出与基线满足什么关系才算成立（例如“比基线高 2% 以上”）。
{rule3}
4. 模板之后，用不超过 3 句话说出你最不确定的一点，再问我一个问题，方便我们接着聊。
5. 这是一次对话，不是后台任务：不要调用 ask（弹表单）工具，不要调用 complete（交付）工具，不要生成文件。
6. 工具最多用几次就够了：同一条命令或同一个查询不要重复执行；一次没找到就停止查找，直接按模板回答。"""


RETRY_MSG = ("【自动重试】上一轮在反复调用同样的工具，被平台中止了。这一次不要调用任何工具，"
             "直接依据上面卡片里已有的内容，按四行模板（第一行 + 产出 / 对照 / 判据）回答；读不到的数字写“待测”。"
             "模板后用不超过 3 句话说出最不确定的一点，再问我一个问题。")


def failure_reason(*objs) -> str | None:
    """v1.5.6: the platform's own reason (stream_repetition, …) from the final run record / stream events."""
    blob = json.dumps(objs, ensure_ascii=False, default=str)
    m = re.search(r'failure_reason\\?"\s*:\s*\\?"([A-Za-z_]+)', blob) or re.search(r'timeout_phase\\?"\s*:\s*\\?"([A-Za-z_]+)', blob)
    if m:
        return m.group(1)
    if "became repetitive" in blob or "stream_repetition" in blob:
        return "stream_repetition"
    return None


REASON_ZH = {"stream_repetition": "模型反复调用同样的工具，被平台中止"}


def is_template_echo(s: str) -> bool:
    return any(m in s for m in TEMPLATE_MARKS)


def pick_answer(texts: list[str]) -> str:
    """The agent's filled-in template: the LAST string that has 产出/对照/判据 and is not the prompt echoed back."""
    best = ""
    for s in texts:
        if not isinstance(s, str) or len(s) < 12:
            continue
        if "产出" in s and "对照" in s and "判据" in s and not is_template_echo(s):
            best = s
    return best.strip()[:6000]


# ----------------------------------------------------------------------------- create / cancel
def create_ask(state_dir: Path, card_id: str, via: str = "card_ui", verdict_ts: str | None = None,
               cfg: dict | None = None, cfg_path: Path | None = None, qc: dict | None = None) -> dict:
    card = RA.card_view(state_dir, card_id)
    ask_id = f"ask_{datetime.now():%Y%m%d_%H%M%S}_{card_id[-3:]}"
    k = 1
    while ask_dir(state_dir, ask_id).exists():
        k += 1
        ask_id = f"{ask_id.rsplit('-', 1)[0]}-{k}"
    a = {"ask_id": ask_id, "card_id": card_id, "card_title": card.get("title"), "card_type": card.get("type"),
         "status": "queued", "via": via, "verdict_ts": verdict_ts, "created_at": now_iso(),
         "thread_id": None, "agent_run_id": None, "answer": None}
    ask_dir(state_dir, ask_id).mkdir(parents=True, exist_ok=True)
    local_names: list[str] = []
    if is_local_card(card) and cfg is not None and cfg_path is not None and (qc or {}).get("attach_local", True):
        # v1.5.6 (ASK1): the same redacted copy “立刻动手” attaches, so the agent can really read the code
        att = ask_dir(state_dir, ask_id) / "attach"
        att.mkdir(exist_ok=True)
        job = {"card_snapshot": card}
        got = RA.attach_local_evidence(cfg, cfg_path, job, att)
        local_names = [g.name for g in got]
        a["attach"] = local_names
        if job.get("notes"):
            a["attach_notes"] = job["notes"]
    prompt = build_prompt(card, "" if is_local_card(card) else paper_abstract(state_dir, card), local_names)
    (ask_dir(state_dir, ask_id) / "prompt.md").write_text(prompt + "\n", encoding="utf-8")
    a.update(prompt_file="prompt.md", prompt=prompt[:8000])   # v1.5.4: what was actually asked, for later diagnosis
    save(state_dir, a, "created")
    return a


def cancel_for_card(state_dir: Path, card_id: str, why: str = "撤销「追问细节」") -> list[str]:
    out = []
    for a in list_asks(state_dir):
        if a["card_id"] == card_id and a["status"] in OPEN:
            a.update(status="cancelled", cancelled_at=now_iso(), cancelled_from=a["status"], cancel_why=why)
            save(state_dir, a, "cancelled", why=why)
            out.append(a["ask_id"])
    return out


def is_cancelled(state_dir: Path, ask_id: str) -> bool:
    try:
        return read_ask(state_dir, ask_id).get("status") == "cancelled"
    except (OSError, json.JSONDecodeError):
        return False


# ----------------------------------------------------------------------------- worker
def execute(cfg_path: Path, ask_id: str, fake: Path | None = None, grace: bool = False) -> dict:
    cfg, state_dir, qc = load(cfg_path)
    if grace and float(qc.get("cancel_grace_s") or 0) > 0:
        time.sleep(float(qc["cancel_grace_s"]))   # a Z right after pressing 5 must win the race
    a = read_ask(state_dir, ask_id)
    if a["status"] != "queued":
        print(f"SKIP: {ask_id} 当前状态 {a['status']}")
        return a
    prompt = (ask_dir(state_dir, ask_id) / "prompt.md").read_text(encoding="utf-8")
    a.update(status="running", started_at=now_iso())
    save(state_dir, a, "started")
    t0 = time.time()
    try:
        if fake:   # tests: "STATUS:failed stream_repetition" first line; "@@RETRY@@" separates the auto-retry reply
            parts = fake.read_text(encoding="utf-8").split("\n@@RETRY@@\n")

            def fake_part(s: str) -> tuple[str, list, str | None]:
                st, why = "completed", None
                if s.startswith("STATUS:"):
                    first, _, s = s.partition("\n")
                    st, _, why = first[7:].strip().partition(" ")
                return st, [s], (why or None)
            status, texts, reason = fake_part(parts[0])
            texts = [prompt] + texts
            a.update(thread_id=f"fake-thread-{ask_id}", agent_run_id="fake-run")
            if str(status).lower() == "failed" and reason == "stream_repetition" and not pick_answer(texts) \
                    and int(qc.get("auto_retry", 1)) > 0 and len(parts) > 1:
                a["retry"] = {"at": now_iso(), "reason": reason, "from_run": "fake-run", "run": "fake-run-2"}
                status, t2, reason2 = fake_part(parts[1])
                texts += [RETRY_MSG] + t2
                reason = reason2
            a["failure_reason"] = reason
        else:
            rn = _load("platform/run_local.py", "rn_ask")
            token = rn.run_token(cfg)
            if not token:
                raise RuntimeError("没有 API Key（环境变量或 ~/.sciencepal/run_token）")
            sp = SciencePal(cfg["base_url"], token, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer"))
            ids = json.loads((cfg_path.resolve().parent / cfg.get("platform_ids_file", "platform_ids.json")).read_text(encoding="utf-8"))
            att = ask_dir(state_dir, ask_id) / "attach"
            files = [att / n for n in (a.get("attach") or []) if (att / n).is_file()]
            resp = sp.initiate(prompt, agent_id=ids.get("agent_id"), files=files, model_name=cfg.get("model_name"),
                               web_search_on=bool(qc.get("web_search_on", True)), workspace_context=cfg.get("workspace_context"))
            a.update(thread_id=resp.get("thread_id"), agent_run_id=resp.get("agent_run_id"))
            if a.get("thread_id"):   # v1.5.4 (S6): needed for the conversation link
                a["project_id"] = sp.project_of_thread(a["thread_id"], resp)
                if not a["project_id"]:
                    try:
                        ss = _load("platform/profile_tool.py", "pt_ask").session_client(cfg)
                        a["project_id"] = ss.project_of_thread(a["thread_id"]) if ss else None
                    except Exception:  # noqa: BLE001
                        a["project_id"] = None
            cur = read_ask(state_dir, ask_id)
            if cur.get("status") != "cancelled":
                save(state_dir, a)   # the link is visible on the review page while the agent is still thinking
            rid = a["agent_run_id"]
            events = sp.stream_run(rid, timeout_s=int(qc["timeout_s"])) if rid else []
            final = sp.wait_run(rid, timeout_s=120, poll_s=5) if rid else None
            status = str((final or {}).get("status", "")) if isinstance(final, dict) else ""
            if any(isinstance(e, dict) and e.get("_orchestrator") == "timeout" for e in events):
                status = "timeout"
            texts = collect_strings(events) + collect_strings(final)
            reason = failure_reason(final, events[-8:]) if str(status).lower() in ("failed", "error", "stopped") else None
            # v1.5.6 (ASK1): the platform stopped a looping run (recoverable) → one retry in the SAME conversation, no tools
            if reason == "stream_repetition" and not pick_answer(texts) and int(qc.get("auto_retry", 1)) > 0 \
                    and a.get("thread_id") and not is_cancelled(state_dir, ask_id):
                mid = sp.add_message(a["thread_id"], RETRY_MSG)
                resp2 = sp.start_thread_agent(a["thread_id"], ids.get("agent_id"), mid)
                rid2 = ((resp2 or {}).get("agent_run_id") or (resp2 or {}).get("id")) if isinstance(resp2, dict) else None
                a["retry"] = {"at": now_iso(), "reason": reason, "from_run": rid, "run": rid2}
                save(state_dir, a, "retry", reason=reason)
                if rid2:
                    a["agent_run_id"] = rid2
                    ev2 = sp.stream_run(rid2, timeout_s=int(qc["timeout_s"]))
                    fin2 = sp.wait_run(rid2, timeout_s=120, poll_s=5)
                    status = str((fin2 or {}).get("status", "")) if isinstance(fin2, dict) else ""
                    if any(isinstance(e, dict) and e.get("_orchestrator") == "timeout" for e in ev2):
                        status = "timeout"
                    texts += collect_strings(ev2) + collect_strings(fin2)
                    reason = failure_reason(fin2, ev2[-8:]) if str(status).lower() in ("failed", "error", "stopped") else None
            a["failure_reason"] = reason
    except Exception as exc:  # noqa: BLE001
        if is_cancelled(state_dir, ask_id):
            return read_ask(state_dir, ask_id)
        a.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}", finished_at=now_iso())
        save(state_dir, a, "failed", error=a["error"])
        notify(cfg, cfg_path, state_dir, a, qc, fake is not None)
        return a
    if is_cancelled(state_dir, ask_id):   # Z / 否决 while it ran → keep the record, no notification
        cur = read_ask(state_dir, ask_id)
        cur.update(thread_id=a.get("thread_id"), agent_run_id=a.get("agent_run_id"), finished_at=now_iso(), discarded_result=True)
        save(state_dir, cur, "discarded")
        return cur
    answer = pick_answer(texts)
    a["run_status"] = status
    a["wall_s"] = round(time.time() - t0, 1)
    a["finished_at"] = now_iso()
    if status == "timeout":
        a["status"], a["error"] = "timeout", f"超过 {qc['timeout_s']} 秒还没有回答完（对话仍然可以打开接着看）"
    elif str(status).lower() in ("failed", "error", "stopped") or not a.get("thread_id"):
        why = a.get("failure_reason")
        if why in REASON_ZH:
            a["error"] = REASON_ZH[why] + ("（已在同一会话自动重试 1 次，仍没有完成）" if a.get("retry") else "") + \
                "。可以点“重试追问”，或打开对话说“继续”"
        else:
            a["error"] = f"平台运行状态：{status or '未知'}" + (f"（{why}）" if why else "") + "。可以点“重试追问”，或打开对话看原因"
        a["status"] = "failed"
    else:
        # asked / completed: either way the conversation is now waiting for you
        a["status"] = "answered"
        a["answer"] = answer or None
        if not answer:
            a["note"] = "没在事件流里认出按模板填写的回答，打开对话看原文"
        elif a.get("retry"):
            a["note"] = "第一次被平台中止（模型反复调用同样的工具），已在同一会话自动重试一次后回答"
    if a.get("answer"):
        (ask_dir(state_dir, ask_id) / "answer.md").write_text(f"# 追问回答 · {a['card_title']}\n\n{a['answer']}\n", encoding="utf-8")
    save(state_dir, a, a["status"])
    notify(cfg, cfg_path, state_dir, a, qc, fake is not None)
    return a


def notify(cfg: dict, cfg_path: Path, state_dir: Path, a: dict, qc: dict, offline: bool = False) -> None:
    """Answered → toast “来聊聊新的idea吧” that opens THIS conversation, and (open_thread) open it in the browser."""
    try:
        rn = _load("platform/run_local.py", "rn_ask_notify")
        rn.RUNTIME.update(cfg=cfg, cfg_path=cfg_path.resolve())
        url = thread_url(cfg, a.get("thread_id"), a.get("project_id"))
        title = (a.get("card_title") or "")[:40]
        if a["status"] == "answered" and url:
            body = f"灵感官回答了你对「{title}」的追问" + ("，已按模板写好产出 / 对照 / 判据" if a.get("answer") else "")
            rv = cfg.get("review") or {}
            d = {"called": False, "why": "offline"}
            if not offline and rv.get("enabled", os.name == "nt") and rv.get("windows_toast", True):
                rs = _load("platform/review_server.py", "review_server_ask")
                d = rs.toast(TOAST_TITLE, body, url, "打开对话")
                try:   # v1.5.4: same diagnostics file as every other notification
                    (state_dir / "inbox").mkdir(exist_ok=True)
                    (state_dir / "inbox" / "toast_last.json").write_text(json.dumps(
                        {"at": now_iso(), "title": TOAST_TITLE, "ask_id": a["ask_id"], "url": url, **d}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                except OSError:
                    pass
            opened = False
            if not offline and qc.get("open_thread", True) and (os.name == "nt" or os.environ.get("INSP_ASK_OPEN_BROWSER")):
                opened = bool(webbrowser.open(url))
            a.update(toast={k: d.get(k) for k in ("called", "visible", "why")}, opened_thread=opened)
            save(state_dir, a)
            rn.notify(cfg, TOAST_TITLE, f"{body}\n对话：{url}")
        elif a["status"] != "answered":
            head = STATUS_ZH.get(a["status"], a["status"])
            if not offline:
                rn.review_notify(state_dir, f"追问细节 · {head}", f"{title} · {a.get('error') or ''}"[:120], query={"ask": a["ask_id"]})
    except Exception as exc:  # notifications never break the ask
        print("WARN notify:", exc)


# ----------------------------------------------------------------------------- spawn / dispatch / view
def spawn(cfg_path: Path, ask_id: str, state_dir: Path, fake: Path | None = None, grace: bool = False) -> int | None:
    fake = fake or (Path(os.environ["INSP_FAKE_ASK_OUTPUT"]) if os.environ.get("INSP_FAKE_ASK_OUTPUT") else None)
    log = (ask_dir(state_dir, ask_id) / "worker.log").open("a", encoding="utf-8")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--config", str(cfg_path.resolve()), "--execute", ask_id]
    if fake:
        cmd += ["--fake-agent-output", str(Path(fake).resolve())]
    if grace:
        cmd += ["--grace"]
    log.write(f"[{now_iso()}] spawn argv={cmd[1:]}\n")
    log.flush()
    kw: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": str(PKG),
                "env": dict(os.environ, PYTHONUTF8="1")}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kw).pid
    except OSError as exc:
        print("WARN spawn:", exc)
        return None


def dispatch(cfg_path: Path, card_id: str, via: str = "card_ui", verdict_ts: str | None = None, run: bool = True,
             fake: Path | None = None) -> dict:
    cfg, state_dir, qc = load(cfg_path)
    if not qc.get("enabled", True):
        return {"ask_id": None, "message": "ask.enabled=false：只记录了“追问细节”，没有向灵感官提问"}
    busy = [a for a in list_asks(state_dir) if a["card_id"] == card_id and a["status"] in OPEN]
    if busy:
        return {"ask_id": busy[0]["ask_id"], "status": busy[0]["status"], "message": "这张卡的追问还在进行中"}
    a = create_ask(state_dir, card_id, via, verdict_ts, cfg=cfg, cfg_path=cfg_path, qc=qc)
    pid = spawn(cfg_path, a["ask_id"], state_dir, fake, grace=(via == "card_ui")) if run else None
    return {"ask_id": a["ask_id"], "status": a["status"], "worker_pid": pid,
            "message": "已按模板向灵感官追问，回答好了会通知你“来聊聊新的idea吧”并打开对话"}


def asks_view(cfg: dict, state_dir: Path, limit: int = 20) -> list[dict]:
    out = []
    for a in sorted(list_asks(state_dir), key=lambda x: (x.get("created_at", ""), x.get("ask_id", "")), reverse=True)[:limit]:
        out.append({k: a.get(k) for k in ("ask_id", "card_id", "card_title", "status", "created_at", "finished_at",
                                          "answer", "note", "error", "cancel_why", "failure_reason")}
                   | {"retryable": a.get("status") in ("failed", "timeout")}
                   | {"status_text": STATUS_ZH.get(a.get("status"), a.get("status")),
                      "thread_url": thread_url(cfg, a.get("thread_id"), a.get("project_id"))})
    return out


# ----------------------------------------------------------------------------- self-test
def self_test() -> int:
    import shutil
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        sd = t / "inspiration"
        shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", sd)
        (sd / "cards/2026-10-31").mkdir(parents=True, exist_ok=True)
        card = {"id": "ins_20261031_002", "type": "knowledge", "title": "Rydberg 阵列", "one_line": "一句话", "why_me": "w",
                "hypothesis": "h", "minimal_check": "m", "expected_observable": "e", "falsifier": "f", "magnitude": "x",
                "source": {"kind": "arxiv", "ref": "2610.00001v1", "quote": "q"}, "status": "proposed"}
        (sd / "cards/2026-10-31/cards.json").write_text(json.dumps({"date": "2026-10-31", "cards": [card]}, ensure_ascii=False), encoding="utf-8")
        (t / "config.json").write_text(json.dumps({"base_url": "https://sciencepal.ai", "state_dir": "./inspiration",
                                                    "review": {"enabled": False}}), encoding="utf-8")
        cfgp = t / "config.json"
        # prompt carries the fixed template + the card, and is recognised as an echo
        d = dispatch(cfgp, card["id"], run=False)
        a = read_ask(sd, d["ask_id"])
        pr = (ask_dir(sd, a["ask_id"]) / "prompt.md").read_text(encoding="utf-8")
        assert TEMPLATE in pr and "Rydberg 阵列" in pr and "2610.00001v1" in pr and is_template_echo(pr)
        assert "不要调用 complete" in pr and "不要调用 ask（弹表单）工具" in pr
        # a second press while it is open does not create a second conversation
        assert dispatch(cfgp, card["id"], run=False)["ask_id"] == a["ask_id"]
        # answered: the filled template is picked, the prompt echo is not
        reply = t / "reply.txt"
        reply.write_text("256 原子 Rydberg 阵列 · 量子模拟 用张量网络拟合关联函数\n产出：关联长度 ξ\n对照：平均场基线\n判据：ξ 比基线大 20% 以上\n"
                         "我最不确定的是数据能否公开。你手上有原始快照吗？", encoding="utf-8")
        r = execute(cfgp, a["ask_id"], reply)
        assert r["status"] == "answered" and r["answer"].startswith("256 原子") and "<基线是什么>" not in r["answer"], r
        assert (ask_dir(sd, a["ask_id"]) / "answer.md").is_file()
        # template-only reply → still answered (conversation waits for you) but no answer recognised
        d2 = dispatch(cfgp, card["id"], run=False)
        echo = t / "echo.txt"
        echo.write_text("STATUS:asked\n" + TEMPLATE, encoding="utf-8")
        r2 = execute(cfgp, d2["ask_id"], echo)
        assert r2["status"] == "answered" and r2["answer"] is None and r2.get("note"), r2
        # Z before it starts → cancelled, the worker skips it
        d3 = dispatch(cfgp, card["id"], run=False)
        assert cancel_for_card(sd, card["id"]) == [d3["ask_id"]]
        assert execute(cfgp, d3["ask_id"], reply)["status"] == "cancelled"
        # cancelled while running → discarded, status stays cancelled
        d4 = dispatch(cfgp, card["id"], run=False)
        real = Path.read_text
        def rt(self, *aa, **kw):
            if self == reply:
                cancel_for_card(sd, card["id"], "评价改为否决")
            return real(self, *aa, **kw)
        Path.read_text = rt
        try:
            r4 = execute(cfgp, d4["ask_id"], reply)
        finally:
            Path.read_text = real
        assert r4["status"] == "cancelled" and r4.get("discarded_result") and r4["cancel_why"] == "评价改为否决", r4
        v = asks_view({"base_url": "https://x.ai"}, sd)
        assert len(v) == 4 and [x["status_text"] for x in v].count("已回答") == 2 and all(x["thread_url"] is None for x in v)  # fake → no link
        assert thread_url({"base_url": "https://x.ai/"}, "abc", "p1") == "https://x.ai/projects/p1/thread/abc"
        assert thread_url({"base_url": "https://x.ai/", "review": {"port": 9}}, "abc") == "http://127.0.0.1:9/go/thread/abc"
        pr = build_prompt({"title": "T", "source": {"kind": "arxiv", "ref": "2510.20798", "quote": "q",
                                                   "url": "https://arxiv.org/abs/2510.20798", "pdf_url": "https://arxiv.org/pdf/2510.20798"}},
                          "ABSTRACT-TEXT")
        assert "https://arxiv.org/abs/2510.20798" in pr and "https://arxiv.org/pdf/2510.20798" in pr and "ABSTRACT-TEXT" in pr
        assert "不要向我索要论文" in pr
        assert "arxiv.org/pdf/2501.1" in build_prompt({"source": {"kind": "arxiv", "ref": "2501.1"}})
        assert pick_answer(["产出 对照 判据 但是 <基线是什么>", "x 产出：a 对照：b 判据：c 好的"]) == "x 产出：a 对照：b 判据：c 好的"
        ev = [json.loads(l)["event"] for l in (sd / "asks.jsonl").read_text(encoding="utf-8").splitlines()]
        assert ev.count("created") == 4 and "answered" in ev and "discarded" in ev, ev
    print("SELF-TEST OK: ask_detail")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="追问细节：按固定模板向灵感官提问")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--card-id")
    ap.add_argument("--execute")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fake-agent-output", type=Path)
    ap.add_argument("--grace", action="store_true")
    ap.add_argument("--no-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.config:
        ap.error("需要 --config")
    if a.execute:
        r = execute(a.config, a.execute, a.fake_agent_output, a.grace)
        print(json.dumps({k: r.get(k) for k in ("ask_id", "status", "thread_id", "error", "note")}, ensure_ascii=False))
        return 0 if r["status"] in ("answered", "cancelled") else 1
    if a.card_id:
        print(json.dumps(dispatch(a.config, a.card_id, "cli", run=not a.no_run, fake=a.fake_agent_output), ensure_ascii=False, indent=2))
        return 0
    cfg, state_dir, _ = load(a.config)
    for x in asks_view(cfg, state_dir, 50):
        print(f"{x['ask_id']}  {x['status_text']:<8} {x['card_title']}  {x['thread_url'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
