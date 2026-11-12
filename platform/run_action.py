#!/usr/bin/env python3
"""“立刻动手”（start_now）的分档执行与回执（v1.3）。

一张卡片被标成“立刻动手”后：分档 → （自动执行 | 先出方案等你批准 | 只出方案由你执行）→ 回执 → 你确认回执。

  T0 只读整理    knowledge 型、风险 low、不涉及副作用           自动执行：读原文 / 整理成文档，产物放平台工作区
  T1 沙箱验证    风险与成本在自动额度内、不涉及副作用           自动执行：在沙箱里跑 minimal_check；改代码只在副本上做、交补丁
  T2 先方案后执行 超出自动额度 / 风险较高 / 涉及部署、删除、正式数据等 先只出方案（不执行），你在评价页“批准”后才执行
  T3 只出方案    涉及真实设备、湿实验、下单付款、对外发送等       只出方案，永不自动执行（设计文档 §4：不闭环控制真实设备）

用法（在包根目录）：
  python platform/run_action.py --config config.json --card-id ins_20261028_001          # 动手单 + 分档 + 建任务（自动档会在后台执行）
  python platform/run_action.py --config config.json --card-id ins_... --brief-file brief.json   # 用改好的动手单建任务
  python platform/run_action.py --config config.json --brief ins_...                     # 只看这张卡的动手单（不建任务）
  python platform/run_action.py --config config.json --sweep                             # 给 Excel/CLI 记录的 start_now 补建任务
  python platform/run_action.py --config config.json --approve act_...                   # 批准 T2 方案 → 执行
  python platform/run_action.py --config config.json --ack act_... --result accept|rerun|discard [--note "..."]
  python platform/run_action.py --config config.json --list
  python platform/run_action.py --config config.json --execute act_... [--fake-agent-output reply.txt]   # 后台工作进程 / 测试
  python platform/run_action.py --self-test

文件（全部在状态目录下，append-only 的只有 actions.jsonl）：
  inspiration/actions/<job_id>/job.json       任务与状态（原子写）；job["brief"] = 动手单（v1.5.1）
  inspiration/actions/<job_id>/brief.md       动手单（场景 / 参数 / 方法 / 产出 / 对照 / 判据 / 交付）
  inspiration/actions/<job_id>/receipt.json   回执（机器可读）    receipt.md  回执（给人看）
  inspiration/actions/<job_id>/plan.md        T2/T3 的方案        outputs/    从平台下载回来的产物
  inspiration/actions.jsonl                   事件流：created / started / plan_ready / approved / finished / failed / acked / cancelled
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
from sciencepal_client import ApiError, MultiAuth, SciencePal, collect_strings, extract_block, extract_blocks  # noqa: E402
import action_brief as AB  # noqa: E402  v1.5.1 动手单
import dryrun_template as DT  # noqa: E402  B7 最小验证 dry-run 模板与回执


def file_reader(cfg: dict, sp: SciencePal):
    """v1.4.2: thread files need the session token on the real platform (API key → 403); fall back to the key."""
    tok = os.environ.get(cfg.get("setup_token_env", "SCIENCEPAL_SETUP_TOKEN"), "")
    f = Path(os.path.expanduser(cfg.get("setup_token_file", "~/.sciencepal/session_token")))
    if not tok and f.is_file():
        tok = f.read_text(encoding="utf-8").strip()
    if not tok:
        return sp
    sess = SciencePal(cfg["base_url"], tok, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer"))
    return MultiAuth([sess, sp], ["session", "api_key"])


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


VERDICT = _load("insp-feedback/scripts/record_verdict.py", "verdict_ra")

RES_START, RES_END = "<<<INSP_ACTION_RESULT", "INSP_ACTION_RESULT>>>"
TIERS = {
    "T0": "只读整理（自动执行）",
    "T1": "沙箱验证（自动执行）",
    "T2": "先出方案，批准后执行",
    "T3": "只出方案，由你执行",
}
DEFAULTS = {
    "enabled": True, "auto_tiers": ["T0", "T1"], "allow_auto_risk": ["low"], "max_auto_tokens": 60000,
    "max_auto_hours": 4, "daily_auto_max": 3, "run_timeout_s": 1800, "plan_timeout_s": 600, "max_download_mb": 20,
    "start_now_mode": "form", "attach_local_files": True, "max_local_file_mb": 2,   # v1.5.1
    "max_attempts": 2, "cancel_grace_s": 5,                                            # v1.5.2
    "plan_keywords": ["部署", "上线", "发布", "生产环境", "正式数据", "数据库", "push", "merge", "删除", "覆盖原文件",
                      "迁移", "rm -rf", "drop table"],
    "manual_keywords": ["仪器", "设备", "实验台", "湿实验", "样品", "试剂", "动物", "患者", "受试者", "下单", "购买", "付款",
                        "发邮件", "发送邮件", "投稿", "instrument", "wet lab", "purchase", "send email"],
}
TERMINAL = {"succeeded", "failed", "timeout", "blocked", "plan_ready", "cancelled", "rejected_plan"}
ACK_RESULTS = {"accept": "结果可用", "rerun": "需要重跑", "discard": "结果作废"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ----------------------------------------------------------------------------- config / paths
def load_cfg(cfg_path: Path) -> tuple[dict, Path, dict]:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    base = cfg_path.resolve().parent
    state_dir = (base / cfg["state_dir"]).resolve()
    acfg = dict(DEFAULTS, **{k: v for k, v in (cfg.get("actions") or {}).items() if not k.startswith("_")})
    return cfg, state_dir, acfg


def job_dir(state_dir: Path, job_id: str) -> Path:
    return state_dir / "actions" / job_id


def read_job(state_dir: Path, job_id: str) -> dict:
    return json.loads((job_dir(state_dir, job_id) / "job.json").read_text(encoding="utf-8"))


def write_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def log_event(state_dir: Path, job: dict, event: str, **extra) -> None:
    row = {"ts": now_iso(), "job_id": job["job_id"], "card_id": job["card_id"], "event": event,
           "tier": job["tier"], "status": job["status"], **extra}
    with (state_dir / "actions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def save(state_dir: Path, job: dict, event: str | None = None, **extra) -> None:
    job["updated_at"] = now_iso()
    write_json(job_dir(state_dir, job["job_id"]) / "job.json", job)
    if event:
        log_event(state_dir, job, event, **extra)


def list_jobs(state_dir: Path) -> list[dict]:
    out = []
    for f in sorted((state_dir / "actions").glob("act_*/job.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda j: j.get("created_at", ""), reverse=True)


# ----------------------------------------------------------------------------- classification
def parse_tokens(s) -> int | None:
    m = re.search(r"([\d.]+)\s*([kKmM万千]?)", str(s or ""))
    if not m:
        return None
    n = float(m.group(1))
    return int(n * {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "万": 1e4, "千": 1e3, "": 1}[m.group(2)])


def parse_hours(s) -> float | None:
    """Upper bound in hours: '2h' → 2, '1-2d' / '1–2 天' → 48, '40min' → 0.67, '1 周' → 168."""
    t = str(s or "").lower().replace("–", "-").replace("~", "")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]
    if not nums:
        return None
    n = max(nums)
    if re.search(r"min|分钟", t):
        return n / 60
    if re.search(r"week|wk|周", t):
        return n * 168
    if re.search(r"d|day|天", t):
        return n * 24
    return n  # h / 小时 / bare number


def classify(card: dict, acfg: dict, auto_used_today: int) -> tuple[str, list[str]]:
    """Return (tier, reasons). Deterministic; every reason is shown on the receipt."""
    text = " ".join(str(card.get(k, "")) for k in ("title", "one_line", "minimal_check", "hypothesis", "expected_observable")).lower()
    hit_manual = [k for k in acfg["manual_keywords"] if k.lower() in text]
    if hit_manual:
        return "T3", [f"涉及真实世界 / 对外副作用：{', '.join(hit_manual)} → 只出方案，由你执行"]
    ce = card.get("cost_estimate") or {}
    risk, tok, hrs = str(ce.get("risk", "")).lower(), parse_tokens(ce.get("tokens")), parse_hours(ce.get("wall_clock"))
    why: list[str] = []
    hit_plan = [k for k in acfg["plan_keywords"] if k.lower() in text]
    if hit_plan:
        why.append(f"涉及有副作用的操作：{', '.join(hit_plan)}")
    if risk not in acfg["allow_auto_risk"]:
        why.append(f"风险 {risk or '未申报'} 不在自动执行范围 {acfg['allow_auto_risk']}")
    if tok is None or tok > int(acfg["max_auto_tokens"]):
        why.append(f"申报 token {ce.get('tokens')!r} 超出自动额度 {acfg['max_auto_tokens']}" if tok else "没有可解析的 token 申报")
    if hrs is None or hrs > float(acfg["max_auto_hours"]):
        why.append(f"申报耗时 {ce.get('wall_clock')!r} 超出自动额度 {acfg['max_auto_hours']} 小时" if hrs else "没有可解析的耗时申报")
    if why:
        return "T2", why + ["→ 先出方案，你批准后才执行"]
    tier = "T0" if card.get("type") == "knowledge" else "T1"
    if tier not in acfg["auto_tiers"]:
        return "T2", [f"{tier} 不在 auto_tiers {acfg['auto_tiers']} 中 → 先出方案"]
    if auto_used_today >= int(acfg["daily_auto_max"]):
        return "T2", [f"今天已自动执行 {auto_used_today} 次，达到 daily_auto_max={acfg['daily_auto_max']} → 改为先出方案"]
    return tier, [f"风险 {risk}、token ≈{tok}、耗时 ≤{hrs:g}h 都在自动额度内"
                  + ("；知识型：只读原文、整理成文档" if tier == "T0" else "；在沙箱里跑最小验证，改动只在副本上做")]


def auto_used_today(state_dir: Path) -> int:
    today = datetime.now().astimezone().date().isoformat()
    return sum(1 for j in list_jobs(state_dir) if j["tier"] in ("T0", "T1") and str(j.get("created_at", "")).startswith(today)
               and j["status"] != "cancelled")


# ----------------------------------------------------------------------------- job creation
def card_view(state_dir: Path, card_id: str) -> dict:
    _, card = VERDICT.find_card(state_dir, card_id)
    if card is None:
        raise LookupError(f"找不到卡片 {card_id}")
    return dict(card, **(card.get("_edited") or {}))  # act on the user's edited version


def make_brief(state_dir: Path, card_id: str, brief: dict | None = None) -> dict:
    """v1.5.1: the brief a job will run with. brief=None → the card's own action_brief or one derived from the card."""
    card = card_view(state_dir, card_id)
    if brief is None:
        return AB.build(card, state_dir)
    b = AB.normalize(dict(brief, origin=brief.get("origin") or "user_edit", card_id=card_id))
    problems, missing = AB.check(b)
    return dict(b, problems=problems, missing=missing)


def create_job(state_dir: Path, acfg: dict, card_id: str, via: str = "cli", verdict_ts: str | None = None,
               brief: dict | None = None) -> dict:
    existing = [j for j in list_jobs(state_dir) if j["card_id"] == card_id and j["status"] not in ("cancelled",)]
    if existing:
        return existing[0]
    card = card_view(state_dir, card_id)
    b = brief if brief is not None and "problems" in brief else make_brief(state_dir, card_id, brief)
    if b.get("problems"):
        raise ValueError("动手单不完整：" + "；".join(b["problems"]))
    if b.get("missing"):
        raise ValueError("动手单还缺你提供的参数：" + "、".join(b["missing"]) + "（在评价页动手单里填，或用 --brief-file）")
    btxt = " ".join([b["method"], b["output"], " ".join(b["deliverables"])])
    tier, reasons = classify(dict(card, minimal_check=f"{card.get('minimal_check', '')} {btxt}"), acfg, auto_used_today(state_dir))
    job_id = f"act_{datetime.now():%Y%m%d_%H%M%S}_{card_id[-3:]}"
    k = 1
    while job_dir(state_dir, job_id).exists():
        k += 1
        job_id = f"{job_id.rsplit('-', 1)[0]}-{k}"
    phase = "execute" if tier in ("T0", "T1") else "plan"
    job = {"job_id": job_id, "card_id": card_id, "card_title": card.get("title"), "card_type": card.get("type"),
           "tier": tier, "tier_label": TIERS[tier], "tier_reasons": reasons, "phase": phase,
           "status": "queued" if phase == "execute" else "planning", "via": via, "verdict_ts": verdict_ts,
           "created_at": now_iso(), "card_snapshot": {k: card.get(k) for k in (
               "id", "type", "title", "one_line", "why_me", "hypothesis", "minimal_check", "expected_observable",
               "falsifier", "magnitude", "cost_estimate", "source", "related_evidence", "subfield", "numbers_provenance")},
           "brief": {k: v for k, v in b.items() if k not in ("problems", "missing")},
           "runs": [], "ack": None}
    save(state_dir, job, "created", reasons=reasons, brief_origin=b.get("origin"))
    (job_dir(state_dir, job_id) / "brief.md").write_text(f"# 动手单 · {card.get('title')}\n\n" + AB.render_md(job["brief"]) + "\n",
                                                         encoding="utf-8")
    return job


def sweep(state_dir: Path, acfg: dict) -> list[dict]:
    """start_now verdicts recorded via Excel / CLI / platform chat have no job yet: create them."""
    stacks = VERDICT.verdict_stacks(VERDICT.read_verdicts(state_dir))
    have = {j["card_id"] for j in list_jobs(state_dir)}
    made = []
    for cid, st in stacks.items():
        if st and st[-1]["action"] == "start_now" and cid not in have:
            made.append(create_job(state_dir, acfg, cid, via="sweep", verdict_ts=st[-1]["ts"]))
    return made


CANCELLABLE = ("queued", "planning", "approved", "needs_approval", "running")


CANCEL_WHY = {"undo": "撤销「立刻动手」", "reject": "评价改为否决", "manual": "手动取消"}   # v1.5.3: shown in the receipt


def cancel_for_card(state_dir: Path, card_id: str, cfg: dict | None = None, cfg_path: Path | None = None,
                    why: str = "undo") -> list[str]:
    """User undid 'start_now' in the card UI (v1.5.2, Q4): cancel every job of this card that has not finished —
    including one that is already running (plan or execute phase). A running job is marked cancelled at once;
    the worker notices it (grace period before start, and again when the platform run returns), discards the
    result and does not notify. Stopping the platform run itself is best effort (endpoint not verified)."""
    out = []
    for j in list_jobs(state_dir):
        if j["card_id"] == card_id and j["status"] in CANCELLABLE:
            was = j["status"]
            j["status"] = "cancelled"
            j["cancelled_at"] = now_iso()
            j["cancelled_from"] = was
            j["cancel_why"] = CANCEL_WHY.get(why, why)
            save(state_dir, j, "cancelled", why=j["cancel_why"], was=was)
            out.append(j["job_id"])
            if was == "running" and cfg is not None and cfg_path is not None:
                stop_platform_run(cfg, cfg_path, j)
    return out


def persist_runs(state_dir: Path, job: dict, cfg: dict | None = None, cfg_path: Path | None = None) -> None:
    """v1.5.4 (S5): write the platform run to job.json the moment it starts (before only at the end), so a Z while it
    runs knows the run (receipt text, stop request). Only `runs` is merged — the on-disk status (maybe cancelled) wins."""
    try:
        cur = read_job(state_dir, job["job_id"])
    except (OSError, json.JSONDecodeError):
        return
    cur["runs"] = job["runs"]
    save(state_dir, cur)
    if cur.get("status") == "cancelled" and cfg is not None and cfg_path is not None:   # cancelled while it was starting
        stop_platform_run(cfg, cfg_path, cur)


def cancel_text(job: dict) -> str | None:
    """v1.5.3: one readable line for a cancelled job — what happened and why (R6 feedback)."""
    if job.get("status") != "cancelled":
        return None
    why = job.get("cancel_why") or CANCEL_WHY["undo"]
    # v1.5.4 (S5): decide by the status it was cancelled FROM, not by `runs` (which used to be written only at the end)
    was = job.get("cancelled_from")
    started = was == "running" or (was is None and any(r.get("agent_run_id") for r in job.get("runs") or []))
    if job.get("discarded_result"):
        head = f"已完成 · 结果已丢弃（取消于 {_hm(job.get('cancelled_at'))}）"
    elif started:
        head = "已取消（执行中被打断）" + ("" if job.get("finished_at") else " · 平台那边正在收尾")
    elif was in ("needs_approval", "approved") and any(r.get("agent_run_id") for r in job.get("runs") or []):
        head = "已取消（方案已出，未执行）"
    else:
        head = "已取消（未开始）"
    return f"{head} · 因{why}"


def is_cancelled(state_dir: Path, job_id: str) -> bool:
    try:
        return read_job(state_dir, job_id).get("status") == "cancelled"
    except (OSError, json.JSONDecodeError):
        return False


def stop_platform_run(cfg: dict, cfg_path: Path, job: dict) -> bool:
    """Best effort: ask the platform to stop the run. The endpoint is a guess (not in the verified list);
    failure is fine — the worker discards the result anyway."""
    rid = ((job.get("runs") or [{}])[-1]).get("agent_run_id")
    if not rid or str(rid).startswith("fake"):
        return False
    try:
        rn = _load("platform/run_local.py", "rn_stop")
        tok = rn.run_token(cfg)
        sp = file_reader(cfg, SciencePal(cfg["base_url"], tok, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer")))
        for path in (f"/api/agent-run/{rid}/stop", f"/api/agent-run/{rid}/cancel"):
            try:
                sp.request("POST", path, json_body={})
                return True
            except ApiError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


# ----------------------------------------------------------------------------- prompts
def build_prompt(job: dict, plan: str | None = None) -> str:
    c, jid = job["card_snapshot"], job["job_id"]
    out = f"/workspace/inspiration_actions/{jid}"
    common = f"""【仅本次任务·灵感“立刻动手”】job_id = {jid}，分档 = {job['tier']}（{job['tier_label']}）。
附件在 /workspace/_refs/ 下（用 ls -R /workspace/_refs 查找）：card.json（用户已批准的卡片，含用户改写）{'、plan.md（用户已批准的方案）' if plan else ''}{'、ft_*.md / rel_*.md（原文）' if job.get('has_fulltext') else ''}。
卡片：{c.get('title')}
最小验证：{c.get('minimal_check')}
成立判据：{c.get('expected_observable')}
失败判据：{c.get('falsifier')}
{('附件里的 local_* 是用户本地的原文件（卡片证据所在文件），只能在副本上改。' + chr(10)) if job.get('local_files') else ''}
{AB.render_prompt(job['brief'], out) if job.get('brief') else ''}
硬性边界：
- 禁止调用 ask（询问用户）：这是后台任务，没有人能回答，问了任务就会卡死。拿不准时自己选最保守的做法继续；确实做不下去就在结果块里写 status=blocked 并说明缺什么。
- 禁止调用 complete 交付工具、不要放附件：结果只靠最终回答里的结果块交付。
- 只在沙箱里工作，所有产物写到 {out}/；不得修改这个目录以外的任何文件，不得发送任何外部消息，不得联网下单 / 提交 / 部署。
- 所有数字必须来自你实际执行的命令输出，并在 steps 里给出命令和退出码。
"""
    tail = f"""【收尾，必须做】无论成功、失败还是被卡住，最终回答的最后一段都必须是下面这个结果块（把尖括号和“|”选项换成真实内容，不要原样照抄模板）。
没有结果块 = 任务判失败。先把同一份 JSON 写到 {out}/result.json（写这个文件在“只写方案”阶段也允许），再在回答里原样输出：
{RES_START}
{{"job_id": "{jid}", "status": "{'plan' if job['phase'] == 'plan' else 'done | blocked | failed'}", "summary": "≤120 字",
  "steps": [{{"cmd": "实际执行的命令", "exit_code": 0}}],
  "outputs": [{{"path": "{out}/<文件名>", "what": "这是什么"}}],
  {'"plan": {"steps": ["..."], "files_touched": ["..."], "risks": ["..."], "rollback": "...", "cost": "...", "you_do": ["需要你亲自做的步骤（T3 必填）"]},' if job['phase'] == 'plan' else '"minimal_check_result": {"observed": "实际观察到的结果（带数字）", "criterion": "成立判据原文", "verdict": "supported | refuted | inconclusive", "evidence": "支撑这个判断的命令或文件"},' + (chr(10) + '  "brief_result": {"output_measured": "动手单“产出”的实测值（带数字与单位）", "baseline_measured": "动手单“对照”的实测值", "criterion_met": true, "deliverables": ["' + out + '/<交付文件>"]},' if job.get('brief') else '')}
  "next_step": "一句话建议"}}
{RES_END}
输出结果块之后立刻结束，不要再调用任何工具。"""
    if job["phase"] == "plan":
        body = ("本次只写方案，不执行任何会改变状态的操作（可以只读查看文件 / 文档；唯一允许写的是 result.json）。方案要具体到可以照着做：步骤、要动的文件、"
                "风险、回滚方式、预计成本；" + ("这张卡片涉及真实世界的操作，方案的 you_do 里写清楚需要用户亲自完成的每一步。" if job["tier"] == "T3" else
                                         "用户批准后会让你按这个方案执行。"))
    elif job["tier"] == "T0":
        body = "这是只读整理任务：阅读原文和相关材料，写成一份可直接阅读的 Markdown 文档放进产物目录，不需要运行实验。minimal_check_result 填写文档是否回答了卡片的问题。"
    else:
        body = ("这是沙箱验证任务：按最小验证动作实际运行，记录命令、输出和判断。需要改用户代码时，只在副本上改，产物给出补丁（.diff）和前后对比，"
                "不要覆盖原文件。" + (f"\n按已批准的方案执行，不得超出方案范围：\n{plan}" if plan else ""))
    return common + "\n" + body + "\n\n" + tail


# ----------------------------------------------------------------------------- execution
def stream_evidence(events: list) -> dict:
    tool, codes = 0, []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        typ = str(e.get("type", "")).lower()
        if typ in {"tool", "tool_call", "tool_use", "tool_result", "function_call"} or "tool_name" in e:
            tool += 1
            blob = json.dumps(e, ensure_ascii=False, default=str)
            codes += [int(x) for x in re.findall(r'"exit_code"\s*:\s*(-?\d+)', blob)]
    return {"tool_events": tool, "exit_codes": codes[:200], "nonzero_exits": sum(1 for x in codes if x != 0)}


TEMPLATE_MARKS = ("≤120 字", "done | blocked | failed", "实际执行的命令", "<文件名>", "supported | refuted | inconclusive")


def _loads_lenient(blk: str):
    s = blk.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    for cand in (s, re.sub(r",\s*([}\]])", r"\1", s)):          # tolerate trailing commas
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", s[i:j + 1]))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def is_template(obj: dict) -> bool:
    txt = json.dumps(obj, ensure_ascii=False)
    return sum(m in txt for m in TEMPLATE_MARKS) >= 2


def parse_result(texts: list[str], job_id: str) -> dict | None:
    """v1.5.2 (Q4: 3/4 tasks 'no result block', and a receipt summary showing prompt text): the stream also
    contains OUR prompt, whose template block is valid JSON. Take the LAST block that parses, belongs to this
    job (job_id matches or is absent) and is not the unfilled template."""
    for blk in reversed(extract_blocks(texts, RES_START, RES_END)):
        obj = _loads_lenient(blk)
        if not isinstance(obj, dict) or is_template(obj):
            continue
        if obj.get("job_id") not in (None, "", job_id):
            continue
        if "status" not in obj and "summary" not in obj:
            continue
        return obj
    return None


def _fake_part(fake: Path, job: dict, attempt: int) -> tuple[str, str]:
    """Offline fake reply. Parts separated by a line '===RETRY===' are used for successive attempts;
    a part starting with 'STATUS:<s>' sets the run status (e.g. asked)."""
    parts = fake.read_text(encoding="utf-8").replace("__JOB_ID__", job["job_id"]).split("\n===RETRY===\n")
    txt = parts[min(attempt, len(parts) - 1)]
    status = "completed"
    if txt.startswith("STATUS:"):
        first, _, txt = txt.partition("\n")
        status = first[7:].strip()
    return txt, status


RETRY_MSG = ("上一轮没有收到 INSP_ACTION_RESULT 结果块（或你停下来询问了）。这是后台任务，没有人能回答问题。"
             "不要调用 ask，不要调用 complete。请基于已经完成的工作，立刻按原要求输出结果块"
             "（job_id = {jid}）；做不完就写 status=blocked 并说明原因。输出后立刻结束。")


def _one_run(sp, cfg: dict, job: dict, timeout_s: int, *, prompt: str | None = None, files: list[Path] | None = None,
             thread_id: str | None = None, agent_id: str | None = None, state_dir: Path | None = None,
             cfg_path: Path | None = None) -> dict:
    """Start one platform run: a new thread (prompt) or a follow-up message in an existing thread."""
    created = None
    if thread_id:
        fr = file_reader(cfg, sp)
        mid = fr.add_message(thread_id, RETRY_MSG.format(jid=job["job_id"]))
        resp = fr.start_thread_agent(thread_id, agent_id, mid)
        run_id = (resp or {}).get("agent_run_id") or (resp or {}).get("id") if isinstance(resp, dict) else None
    else:
        resp = sp.initiate(prompt, agent_id=agent_id, files=files or [], model_name=cfg.get("model_name"),
                           web_search_on=False, workspace_context=cfg.get("workspace_context"))
        thread_id, run_id = resp.get("thread_id"), resp.get("agent_run_id")
        created = resp
    prev_pid = next((r.get("project_id") for r in reversed(job["runs"]) if r.get("thread_id") == thread_id and r.get("project_id")), None)
    pid = prev_pid
    if thread_id and not pid:   # v1.5.4 (S6): conversation link needs /projects/<project_id>/
        try:
            pid = sp.project_of_thread(thread_id, created)
        except Exception:  # noqa: BLE001
            pid = None
        if not pid:   # v1.5.5 (P5): API keys cannot read the conversation list; retry with the session token
            try:
                pid = file_reader(cfg, sp).project_of_thread(thread_id)
            except Exception:  # noqa: BLE001
                pid = None
    job["runs"].append({"phase": job["phase"], "thread_id": thread_id, "project_id": pid, "agent_run_id": run_id,
                        "started_at": now_iso(), "kind": "followup" if prompt is None else ("retry" if job["runs"] else "first")})
    if state_dir is not None:
        persist_runs(state_dir, job, cfg, cfg_path)
    events = sp.stream_run(run_id, timeout_s=timeout_s) if run_id else []
    final = sp.wait_run(run_id, timeout_s=120, poll_s=5) if run_id else None
    status = str((final or {}).get("status", "")) if isinstance(final, dict) else ""
    if any(isinstance(e, dict) and e.get("_orchestrator") == "timeout" for e in events):
        status = "timeout"
    return {"thread_id": thread_id, "run_id": run_id, "events": events, "final": final, "status": status}


def rescue_result(cfg: dict, sp, job: dict, thread_id: str | None) -> dict | None:
    """No block in the reply: read result.json the task was told to write (thread file → sandbox file)."""
    if not thread_id:
        return None
    path = f"/workspace/inspiration_actions/{job['job_id']}/result.json"
    fr = file_reader(cfg, sp)
    raws = []
    try:
        raws.append(fr.read_thread_file(thread_id, path))
    except ApiError:
        pass
    if not raws:
        try:
            sbx = fr.thread_sandbox(thread_id)
            sid = sbx.get("sandbox_id") or sbx.get("id")
            if sid:
                raws.append(fr.read_sandbox_file(sid, path, thread_id))
        except ApiError:
            pass
    for raw in raws:
        obj = _loads_lenient(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw))
        if isinstance(obj, dict) and not is_template(obj):
            return obj
    return None


def run_on_platform(cfg: dict, cfg_path: Path, job: dict, prompt: str, files: list[Path], timeout_s: int,
                    jd: Path, fake: Path | None, max_attempts: int = 2, state_dir: Path | None = None) -> dict:
    """v1.5.2: up to max_attempts runs. A run that ends without a usable result block (or stops at 'asked')
    is rescued from result.json first, then retried once: a follow-up message in the same thread (keeps the
    sandbox work), falling back to a fresh run when the platform refuses the follow-up."""
    attempts_log = []
    if fake:
        result, status, text = None, "", ""
        for a in range(max(1, max_attempts)):
            text, status = _fake_part(fake, job, a)
            job["runs"].append({"phase": job["phase"], "thread_id": "fake-thread", "agent_run_id": f"fake-run-{a + 1}",
                                "started_at": now_iso(), "kind": "first" if a == 0 else "followup"})
            job["runs"][-1].update(finished_at=now_iso(), run_status=status or "completed")   # v1.5.3: no half rows
            result = parse_result([text], job["job_id"])
            attempts_log.append({"attempt": a + 1, "status": status, "result": bool(result)})
            if result or status == "timeout" or (state_dir and is_cancelled(state_dir, job["job_id"])):
                break
        return {"result": result, "text": text, "thread_id": "fake-thread", "agent_run_id": job["runs"][-1]["agent_run_id"],
                "status": status, "events": [], "sp": None, "attempts": attempts_log}
    rn = _load("platform/run_local.py", "rn_ra")
    token = rn.run_token(cfg)
    if not token:
        raise RuntimeError("没有 API Key（环境变量或 ~/.sciencepal/run_token）")
    sp = SciencePal(cfg["base_url"], token, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer"))
    ids = json.loads((cfg_path.resolve().parent / cfg.get("platform_ids_file", "platform_ids.json")).read_text(encoding="utf-8"))
    agent_id = ids.get("agent_id")
    all_events: list = []
    result, rep, texts = None, None, []
    for a in range(max(1, max_attempts)):
        if a == 0:
            rep = _one_run(sp, cfg, job, timeout_s, prompt=prompt, files=files, agent_id=agent_id, state_dir=state_dir, cfg_path=cfg_path)
        else:
            try:
                rep = _one_run(sp, cfg, job, timeout_s, thread_id=rep["thread_id"], agent_id=agent_id, state_dir=state_dir,
                               cfg_path=cfg_path)
            except ApiError as exc:   # follow-up refused → fresh run with a reminder on top
                attempts_log.append({"attempt": a + 1, "followup_error": f"{exc.status}"})
                rep = _one_run(sp, cfg, job, timeout_s, prompt="【重试：上一轮没有输出结果块，这一轮务必在最后输出】\n" + prompt,
                               files=files, agent_id=agent_id, state_dir=state_dir, cfg_path=cfg_path)
        all_events += rep["events"]
        texts = collect_strings(rep["events"]) + collect_strings(rep["final"])
        result = parse_result(texts, job["job_id"]) or rescue_result(cfg, sp, job, rep["thread_id"])
        attempts_log.append({"attempt": a + 1, "run_id": rep["run_id"], "status": rep["status"], "result": bool(result)})
        if job["runs"]:   # v1.5.3: every attempt row gets its own end time / status (no run_status=None rows)
            job["runs"][-1].update(finished_at=now_iso(), run_status=rep["status"] or "finished")
        (jd / f"stream_{job['phase']}{'' if a == 0 else '_retry' + str(a)}.json").write_text(
            json.dumps(rep["events"], ensure_ascii=False)[:5_000_000], encoding="utf-8")
        if result or rep["status"] == "timeout" or (state_dir and is_cancelled(state_dir, job["job_id"])):
            break
    text = f"{RES_START}\n{json.dumps(result, ensure_ascii=False)}\n{RES_END}" if result else ""
    return {"result": result, "text": text, "thread_id": rep["thread_id"], "agent_run_id": rep["run_id"], "status": rep["status"],
            "events": all_events, "sp": file_reader(cfg, sp), "attempts": attempts_log}


def download_outputs(sp, thread_id: str | None, outputs: list, jd: Path, max_mb: float) -> list[dict]:
    """Independent check of the agent's claimed outputs: fetch each file from the platform, hash it, keep a copy."""
    res = []
    od = jd / "outputs"
    for o in outputs or []:
        path = str((o or {}).get("path", "")) if isinstance(o, dict) else str(o)
        row = {"path": path, "what": (o or {}).get("what") if isinstance(o, dict) else None, "verified": False}
        if sp is None or not thread_id or not path:
            row["note"] = "离线测试：未下载" if sp is None else "缺少路径"
            res.append(row)
            continue
        try:
            data = sp.read_thread_file(thread_id, path)
            row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), verified=True)
            if len(data) <= max_mb * 1024 * 1024:
                od.mkdir(exist_ok=True)
                (od / Path(path).name).write_bytes(data)
                row["local"] = f"outputs/{Path(path).name}"
        except ApiError as exc:
            row["note"] = f"平台上读不到这个文件（{exc.status}）"
        res.append(row)
    return res


def attach_local_evidence(cfg: dict, cfg_path: Path, job: dict, att: Path) -> list[Path]:
    """v1.5.1: an observed card (A 类优化 etc.) is about a file on YOUR computer, which the sandbox does not have.
    Attach that file (≤ actions.max_local_file_mb, secrets redacted) so the task can really run the old/new version.
    Off when observe.upload_code_snippets=false or actions.attach_local_files=false."""
    src = job["card_snapshot"].get("source") or {}
    ev = str(src.get("evidence_ref") or "")
    obs = cfg.get("observe") or {}
    acfg = cfg.get("actions") or {}
    if not ev.startswith("library://") or obs.get("upload_code_snippets", True) is False or acfg.get("attach_local_files", True) is False:
        return []
    try:
        from observers.common import Env, redact
        env = Env(cfg_path, cfg=cfg, base=cfg_path.resolve().parent)
        label, _, rel = ev[len("library://"):].split("#", 1)[0].partition("/")
        root = next((r["path"] for r in env.roots() if r["label"] == label), None)
        p = (root / rel).resolve() if root else None
        if p is None or not p.is_file() or root not in p.parents:
            return []
        if p.stat().st_size > float(acfg.get("max_local_file_mb", 2)) * 1024 * 1024:
            job.setdefault("notes", []).append(f"{p.name} 超过 max_local_file_mb，没有附上")
            return []
        dst = att / f"local_{p.name}"
        dst.write_text(redact(p.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        job["local_files"] = [f"library://{label}/{rel}"]
        return [dst]
    except Exception as exc:  # noqa: BLE001 - never block the task on this
        job.setdefault("notes", []).append(f"附本地文件失败：{type(exc).__name__}")
        return []


def stage_files(state_dir: Path, cfg: dict, cfg_path: Path, job: dict, jd: Path, plan: str | None) -> list[Path]:
    att = jd / "attach"
    att.mkdir(parents=True, exist_ok=True)
    (att / "card.json").write_text(json.dumps(job["card_snapshot"], ensure_ascii=False, indent=2), encoding="utf-8")
    files = [att / "card.json"]
    if job.get("brief"):
        (att / "brief.json").write_text(json.dumps(job["brief"], ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(att / "brief.json")
    files += attach_local_evidence(cfg, cfg_path, job, att)
    if plan:
        (att / "plan.md").write_text(plan, encoding="utf-8")
        files.append(att / "plan.md")
    ref = str((job["card_snapshot"].get("source") or {}).get("ref", ""))
    ref = re.sub(r"v\d+$", "", ref)
    ftc = cfg.get("fulltext") or {}
    cache = Path(ftc.get("cache_dir") or (state_dir / "fulltext"))
    cache = cache if cache.is_absolute() else (cfg_path.resolve().parent / cache).resolve()
    rel = [re.sub(r"v\d+$", "", str(r.get("ref", ""))) for r in job["card_snapshot"].get("related_evidence") or [] if isinstance(r, dict)]
    for prefix, r in [("ft", ref)] + [("rel", x) for x in rel]:
        p = cache / f"{r}.md"
        if r and p.is_file():
            (att / f"{prefix}_{r}.md").write_text(p.read_text(encoding="utf-8")[:120000], encoding="utf-8")
            files.append(att / f"{prefix}_{r}.md")
            job["has_fulltext"] = True
    return files


def execute(cfg_path: Path, job_id: str, fake: Path | None = None, grace: bool = False) -> dict:
    cfg, state_dir, acfg = load_cfg(cfg_path)
    if grace and float(acfg.get("cancel_grace_s") or 0) > 0:
        time.sleep(float(acfg["cancel_grace_s"]))   # v1.5.2: a Z right after pressing 6 must win the race
    job = read_job(state_dir, job_id)
    jd = job_dir(state_dir, job_id)
    if job["status"] not in ("queued", "planning", "approved"):
        print(f"SKIP: {job_id} 当前状态 {job['status']}，不执行")
        return job
    if job["tier"] == "T3" and job["phase"] == "execute":
        raise SystemExit("T3 永不自动执行")
    plan = (jd / "plan.md").read_text(encoding="utf-8") if job["phase"] == "execute" and (jd / "plan.md").is_file() else None
    files = stage_files(state_dir, cfg, cfg_path, job, jd, plan)
    prompt = build_prompt(job, plan)
    (jd / f"prompt_{job['phase']}.txt").write_text(prompt, encoding="utf-8")
    job["status"] = "running"
    job.setdefault("started_at", now_iso())
    save(state_dir, job, "started", phase=job["phase"])
    t0 = time.time()
    timeout = int(acfg["run_timeout_s"] if job["phase"] == "execute" else acfg["plan_timeout_s"])
    try:
        rep = run_on_platform(cfg, cfg_path, job, prompt, files, timeout, jd, fake,
                              max_attempts=int(acfg.get("max_attempts") or 2), state_dir=state_dir)
    except (ApiError, RuntimeError, OSError) as exc:
        if is_cancelled(state_dir, job_id):
            return read_job(state_dir, job_id)
        job["status"] = "failed"
        job["error"] = str(exc)[:500]
        write_receipt(state_dir, job, None, {}, [], time.time() - t0)
        save(state_dir, job, "failed", error=job["error"])
        notify(cfg, cfg_path, state_dir, job)
        return job
    (jd / f"reply_{job['phase']}.txt").write_text(rep["text"] or "", encoding="utf-8")
    result = rep.get("result")
    if is_cancelled(state_dir, job_id):   # v1.5.2: user pressed Z while it ran → discard, no notification
        cur = read_job(state_dir, job_id)
        cur["runs"] = job["runs"]
        if cur["runs"]:   # v1.5.3: no half rows (run_status=None) — the platform run did finish
            cur["runs"][-1].update(finished_at=now_iso(), run_status=rep.get("status") or "finished")
        cur["discarded_result"] = bool(result)
        cur["finished_at"] = now_iso()
        save(state_dir, cur, "discarded", why="任务运行中被撤销，结果已丢弃")
        return cur
    job["attempts"] = rep.get("attempts")
    ev = stream_evidence(rep["events"])
    outs = download_outputs(rep["sp"], rep["thread_id"], (result or {}).get("outputs"), jd,
                            float(acfg["max_download_mb"])) if result else []
    if job["runs"]:
        job["runs"][-1].update(finished_at=now_iso(), run_status=rep["status"])
    else:
        job["runs"].append({"phase": job["phase"], "thread_id": rep["thread_id"], "agent_run_id": rep["agent_run_id"],
                            "finished_at": now_iso(), "run_status": rep["status"]})
    if rep["status"] == "timeout":
        job["status"] = "timeout"
    elif result is None:
        job["status"] = "failed"
        n = len(rep.get("attempts") or [])
        job["error"] = ("智能体停下来询问（asked），后台任务无法回答" if str(rep["status"]).lower() == "asked"
                        else "回复里没有结果块") + (f"；已自动补问 / 重试 {n - 1} 次仍没有" if n > 1 else "")
    elif job["phase"] == "plan":
        (jd / "plan.md").write_text(render_plan(job, result), encoding="utf-8")
        job["status"] = "needs_approval" if job["tier"] == "T2" else "plan_ready"
    else:
        st = str(result.get("status", "")).lower()
        job["status"] = {"done": "succeeded", "blocked": "blocked"}.get(st, "failed")
        if job["status"] == "failed" and st not in ("failed",):
            job["error"] = f"结果块的 status 不认识：{result.get('status')!r}"
    job["finished_at"] = now_iso()
    write_receipt(state_dir, job, result, ev, outs, time.time() - t0)
    save(state_dir, job, "plan_ready" if job["phase"] == "plan" and result else ("finished" if job["status"] == "succeeded" else job["status"]))
    notify(cfg, cfg_path, state_dir, job)
    return job


def render_plan(job: dict, result: dict) -> str:
    p = result.get("plan") or {}
    L = [f"# 方案 · {job['card_title']}", "", f"job：{job['job_id']} · 分档 {job['tier']}（{job['tier_label']}）", "",
         f"为什么不自动执行：{'；'.join(job['tier_reasons'])}", "", f"摘要：{result.get('summary', '')}", "", "## 步骤"]
    L += [f"{i}. {s}" for i, s in enumerate(p.get("steps") or [], 1)] or ["（未给出）"]
    L += ["", "## 会动到的文件", *[f"- {x}" for x in p.get("files_touched") or ["（无）"]],
          "", "## 风险", *[f"- {x}" for x in p.get("risks") or ["（未列出）"]],
          "", f"## 回滚\n{p.get('rollback') or '（未给出）'}", "", f"## 预计成本\n{p.get('cost') or '（未给出）'}"]
    if p.get("you_do"):
        L += ["", "## 需要你亲自做", *[f"- [ ] {x}" for x in p["you_do"]]]
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------------------- receipt
def write_receipt(state_dir: Path, job: dict, result: dict | None, ev: dict, outs: list[dict], wall_s: float) -> dict:
    jd = job_dir(state_dir, job["job_id"])
    checks = []
    def chk(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    last = job["runs"][-1] if job["runs"] else {}
    chk("平台运行已完成", str(last.get("run_status", "")).lower() in ("completed", "succeeded", "done", ""),
        f"run_status={last.get('run_status')}")
    chk("有结构化结果块", result is not None, "" if result else job.get("error", ""))
    if result and job["phase"] == "execute":
        chk("每一步都有退出码", bool(result.get("steps")) and all(isinstance(s, dict) and "exit_code" in s for s in result["steps"]),
            f"{len(result.get('steps') or [])} 步")
        mcr = result.get("minimal_check_result") or {}
        chk("最小验证给出了判断", mcr.get("verdict") in ("supported", "refuted", "inconclusive"), str(mcr.get("verdict")))
        chk("产物能从平台读到", outs and all(o.get("verified") for o in outs) or (outs and all("离线" in str(o.get("note")) for o in outs)),
            f"{sum(1 for o in outs if o.get('verified'))}/{len(outs)} 个文件已下载并计算 sha256")
        if job.get("brief"):
            want = [re.split(r"[（(\s]", d.strip(), maxsplit=1)[0] for d in job["brief"].get("deliverables") or []]
            want = [w for w in want if re.search(r"\.\w{1,6}$", w) and w != "result.json"]  # file-like names; result.json is always written
            have = {Path(str(o.get("path", ""))).name for o in (result.get("outputs") or []) if isinstance(o, dict)}
            have |= {Path(str(x)).name for x in ((result.get("brief_result") or {}).get("deliverables") or [])}
            miss = [w for w in want if Path(w).name not in have]
            chk("动手单的交付物都在", not miss, "缺 " + "、".join(miss) if miss else f"{len(want)} 个文件都已列出")
            br = result.get("brief_result") or {}
            chk("动手单判据给出了是 / 否", isinstance(br.get("criterion_met"), bool), f"criterion_met={br.get('criterion_met')}")
        if ev:
            chk("平台事件流里有工具调用", ev.get("tool_events", 0) > 0 or last.get("thread_id") == "fake-thread",
                f"tool_events={ev.get('tool_events')} nonzero_exits={ev.get('nonzero_exits')}")
    mcr = (result or {}).get("minimal_check_result") or {}
    conclusion = ("plan_only" if job["phase"] == "plan" and result else
                  mcr.get("verdict") if job["status"] == "succeeded" else job["status"])
    started = job.get("started_at")
    latency = verdict_gap = None
    try:   # v1.5.2: latency = this task's creation → start (not the possibly days-old verdict)
        if started:
            latency = int((datetime.fromisoformat(started) - datetime.fromisoformat(job["created_at"])).total_seconds())
        if job.get("verdict_ts"):
            verdict_gap = int((datetime.fromisoformat(job["created_at"]) - datetime.fromisoformat(job["verdict_ts"])).total_seconds())
    except (ValueError, TypeError):
        pass
    dry = DT.receipt(job.get("card_snapshot") or {}, job_id=job["job_id"])  # B7: 独立字段，不进 checks / 结论
    rec = {"job_id": job["job_id"], "card_id": job["card_id"], "card_title": job["card_title"], "tier": job["tier"],
           "tier_label": job["tier_label"], "tier_reasons": job["tier_reasons"], "phase": job["phase"],
           "status": job["status"], "conclusion": conclusion,
           "timeline": {"verdict_at": job.get("verdict_ts"), "created_at": job["created_at"], "started_at": started,
                        "finished_at": job.get("finished_at"), "start_latency_s": latency, "verdict_to_create_s": verdict_gap,
                        "wall_clock_s": round(wall_s, 1)},
           "attempts": job.get("attempts"),
           "platform": {"thread_id": last.get("thread_id"), "agent_run_id": last.get("agent_run_id"), "run_status": last.get("run_status")},
           "budget": {"declared": (job["card_snapshot"].get("cost_estimate") or {})},
           "execution_evidence": {"stream": ev, "outputs": outs},
           "claims": {k: (result or {}).get(k) for k in ("summary", "steps", "minimal_check_result", "brief_result", "plan", "next_step")},
           "brief": job.get("brief"), "local_files": job.get("local_files"),
           "checks": checks, "all_checks_ok": all(c["ok"] for c in checks), "error": job.get("error"),
           "dry_run": dry, "ack": job.get("ack"), "written_at": now_iso()}
    write_json(jd / "receipt.json", rec)
    (jd / "receipt.md").write_text(render_receipt(rec), encoding="utf-8")
    job["receipt"] = {"conclusion": conclusion, "all_checks_ok": rec["all_checks_ok"], "summary": (result or {}).get("summary"),
                      "brief_result": (result or {}).get("brief_result"), "dry_run": dry,
                      "outputs": [{"path": o.get("path"), "local": o.get("local"), "verified": o.get("verified")} for o in outs][:20]}
    return rec


def _hm(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M:%S")
    except ValueError:
        return str(iso)


def _dur(s: int | None) -> str:
    if s is None:
        return ""
    s = int(s)
    return f"{s} 秒" if s < 120 else f"{s // 60} 分钟" if s < 7200 else f"{s // 3600} 小时" if s < 172800 else f"{s // 86400} 天"


def timeline_line(tl: dict) -> str:
    """v1.5.2 (Q4): separate THIS run (task created → started → finished) from the first time 6 was pressed."""
    lat = tl.get("start_latency_s")
    s = (f"- 时间：本次建任务 {_hm(tl.get('created_at'))} → 开始 {_hm(tl.get('started_at'))}"
         + (f"（{_dur(lat)}后）" if lat is not None else "") + f" → 结束 {_hm(tl.get('finished_at'))}，耗时 {tl.get('wall_clock_s')} 秒")
    gap = tl.get("verdict_to_create_s")
    if tl.get("verdict_at"):
        s += (f"；按“立刻动手”在 {_hm(tl['verdict_at'])}" + (f"（{_dur(gap)}后才建任务：当时动手单待补，或由 --sweep 补建）" if gap and gap > 600 else ""))
    return s


VERDICT_ZH = {"supported": "成立", "refuted": "不成立", "inconclusive": "无定论", "plan_only": "只出了方案",
              "failed": "失败", "timeout": "超时", "blocked": "被卡住"}


def render_receipt(r: dict) -> str:
    c = r["claims"] or {}
    mcr = c.get("minimal_check_result") or {}
    L = [f"# 回执 · {r['card_title']}", "",
         f"- 任务：`{r['job_id']}` · 卡片 `{r['card_id']}`",
         f"- 分档：**{r['tier']} {r['tier_label']}** —— {'；'.join(r['tier_reasons'])}",
         f"- 状态：**{r['status']}** · 结论：**{VERDICT_ZH.get(str(r['conclusion']), r['conclusion'])}**",
         timeline_line(r["timeline"]),
         f"- 回放：thread `{r['platform']['thread_id']}` · run `{r['platform']['agent_run_id']}`", ""]
    if r.get("error"):
        L += [f"> 错误：{r['error']}", ""]
    if r.get("brief"):
        L += ["## 动手单（本次任务按它执行）", "", AB.render_md(r["brief"]), ""]
    L += ["## 执行层证据（平台与编排器核对过的事实）", "", "| 检查 | 结果 | 说明 |", "|---|---|---|"]
    L += [f"| {x['name']} | {'通过' if x['ok'] else '**未通过**'} | {x['detail']} |" for x in r["checks"]]
    outs = r["execution_evidence"].get("outputs") or []
    if outs:
        L += ["", "产物：", *[f"- `{o['path']}` {o.get('what') or ''} —— "
                             + (f"{o['bytes']} 字节，sha256 {o['sha256'][:12]}…，本地副本 {o.get('local', '（过大未下载）')}" if o.get("verified") else o.get("note", "未核对"))
                             for o in outs]]
    L += ["", "## 解释层（模型自述，需要你核对）", "", f"摘要：{c.get('summary') or '—'}", ""]
    if r["phase"] == "execute":
        L += [f"- 观察到：{mcr.get('observed') or '—'}", f"- 判据：{mcr.get('criterion') or '—'}",
              f"- 判断：{VERDICT_ZH.get(str(mcr.get('verdict')), mcr.get('verdict'))}（依据：{mcr.get('evidence') or '—'}）", ""]
        br = c.get("brief_result") or {}
        if br:
            met = br.get("criterion_met")
            L += [f"- 动手单 · 产出实测：{br.get('output_measured') or '—'}", f"- 动手单 · 对照实测：{br.get('baseline_measured') or '—'}",
                  f"- 动手单 · 判据：{'满足' if met is True else '不满足' if met is False else '未给出'}", ""]
        steps = c.get("steps") or []
        if steps:
            L += ["执行的命令：", "", "| # | 命令 | 退出码 |", "|---|---|---|"]
            L += [f"| {i} | `{str(s.get('cmd', ''))[:120]}` | {s.get('exit_code')} |" for i, s in enumerate(steps, 1) if isinstance(s, dict)]
    else:
        L += ["方案见同目录 `plan.md`。" + (" 在评价页“回执”里点“批准执行”，或运行 `run_action.py --approve " + r["job_id"] + "`。" if r["tier"] == "T2" else " 这一档不会自动执行，请按方案里“需要你亲自做”的步骤操作。")]
    L += ["", f"下一步建议：{c.get('next_step') or '—'}", "",
          "## 你的确认", "", "在评价页“回执”里点 结果可用 / 需要重跑 / 结果作废，或运行：",
          f"`python platform/run_action.py --config <config> --ack {r['job_id']} --result accept|rerun|discard --note \"...\"`"]
    if r.get("ack"):
        L += ["", f"已确认：{ACK_RESULTS.get(r['ack']['result'])}（{r['ack']['ts']}）{r['ack'].get('note') or ''}"]
    if r.get("dry_run"):   # B7: 独立段落，不动上面的执行层证据与解释层
        L += ["", "## 本地 dry-run（未执行真实实验）", "", DT.render_receipt_md(r["dry_run"])]
    return "\n".join(L) + "\n"


def notify(cfg: dict, cfg_path: Path, state_dir: Path, job: dict) -> None:
    try:
        rn = _load("platform/run_local.py", "rn_notify")
        rn.RUNTIME.update(cfg=cfg, cfg_path=cfg_path.resolve())
        head = {"succeeded": "已完成", "needs_approval": "方案待批准", "plan_ready": "方案已出（需你执行）",
                "failed": "失败", "timeout": "超时", "blocked": "被卡住"}.get(job["status"], job["status"])
        concl = VERDICT_ZH.get(str((job.get("receipt") or {}).get("conclusion")), "")
        body = f"{job['tier']} · {job['card_title'][:40]}" + (f" · 结论：{concl}" if concl else "")
        # v1.5.2: clicking the toast opens the receipts view and highlights THIS job (not the card queue)
        rn.review_notify(state_dir, f"立刻动手 · {head}", body, query={"act": job["job_id"]}, button="查看回执")
        rn.notify(cfg, f"立刻动手 · {head}", f"{body}\n回执：{state_dir.name}/actions/{job['job_id']}/receipt.md")
    except Exception as exc:  # notifications never break the job
        print("WARN notify:", exc)


# ----------------------------------------------------------------------------- user actions
def approve(state_dir: Path, job_id: str, note: str = "") -> dict:
    job = read_job(state_dir, job_id)
    if job["tier"] == "T3":
        raise ValueError("T3（涉及真实世界操作）永不自动执行：请按 plan.md 亲自操作")
    if job["status"] != "needs_approval":
        raise ValueError(f"{job_id} 当前状态 {job['status']}，只有 needs_approval 可以批准")
    job.update(status="approved", phase="execute", approved_at=now_iso(), approve_note=note)
    save(state_dir, job, "approved", note=note)
    return job


def ack(state_dir: Path, job_id: str, result: str, note: str = "") -> dict:
    if result not in ACK_RESULTS:
        raise ValueError(f"result 只能是 {sorted(ACK_RESULTS)}")
    job = read_job(state_dir, job_id)
    if job["status"] not in TERMINAL | {"needs_approval"}:
        raise ValueError(f"{job_id} 还在 {job['status']}，等回执出来再确认")
    job["ack"] = {"result": result, "note": note, "ts": now_iso()}
    if result == "rerun" and job["status"] != "needs_approval" and job["tier"] != "T3":
        job["status"] = "approved" if job["tier"] == "T2" else "queued"
        job["phase"] = "execute"
        job["ack"]["rerun"] = True
    save(state_dir, job, "acked", result=result, note=note)
    rp = job_dir(state_dir, job_id) / "receipt.json"
    if rp.is_file():
        rec = json.loads(rp.read_text(encoding="utf-8"))
        rec["ack"] = job["ack"]
        write_json(rp, rec)
        (job_dir(state_dir, job_id) / "receipt.md").write_text(render_receipt(rec), encoding="utf-8")
    return job


PASS_ENV = ("SCIENCEPAL_RUN_TOKEN", "SCIENCEPAL_TOKEN", "SCIENCEPAL_SETUP_TOKEN", "INSP_FEISHU_WEBHOOK", "INSP_FAKE_AGENT_OUTPUT")


def spawn(cfg_path: Path, job_id: str, state_dir: Path, fake: Path | None = None, grace: bool = False) -> int | None:
    """Run --execute in a detached background process (the review page must answer immediately).
    BUG-5: the offline fake reply (--fake-agent-output or env INSP_FAKE_AGENT_OUTPUT) is passed through, and the
    worker's argv is written to worker.log first, so a stuck worker can always be diagnosed."""
    fake = fake or (Path(os.environ["INSP_FAKE_AGENT_OUTPUT"]) if os.environ.get("INSP_FAKE_AGENT_OUTPUT") else None)
    log = (job_dir(state_dir, job_id) / "worker.log").open("a", encoding="utf-8")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--config", str(cfg_path.resolve()), "--execute", job_id]
    if fake:
        cmd += ["--fake-agent-output", str(Path(fake).resolve())]
    if grace:
        cmd += ["--grace"]
    log.write(f"[{now_iso()}] spawn argv={cmd[1:]} env={[k for k in PASS_ENV if os.environ.get(k)]}\n")
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
             fake: Path | None = None, brief: dict | None = None) -> dict:
    """Entry point for the review page: brief → classify + create the job, and start the background worker when needed.
    v1.5.1: when the brief still misses user-provided values, NO job is created; the brief comes back with
    needs_input=True and the review page asks for those values first."""
    cfg, state_dir, acfg = load_cfg(cfg_path)
    if not acfg.get("enabled", True):
        return {"job_id": None, "message": "actions.enabled=false：只记录了评价，没有建任务"}
    existing = [j for j in list_jobs(state_dir) if j["card_id"] == card_id and j["status"] not in ("cancelled",)]
    if existing:
        j = existing[0]
        return {"job_id": j["job_id"], "tier": j["tier"], "tier_label": j["tier_label"], "status": j["status"],
                "reasons": j["tier_reasons"], "message": "这张卡已经有任务了", "brief": j.get("brief"), "worker_pid": None}
    b = make_brief(state_dir, card_id, brief)
    if b.get("problems") or b.get("missing"):
        return {"job_id": None, "needs_input": True, "brief": b,
                "message": "动手单还需要你补充：" + "；".join(b.get("problems", []) + [f"填写 {m}" for m in b.get("missing", [])])}
    job = create_job(state_dir, acfg, card_id, via, verdict_ts, brief=b)
    pid = spawn(cfg_path, job["job_id"], state_dir, fake, grace=(via == "card_ui")) if run and job["status"] in ("queued", "planning") else None
    msg = {"T0": "自动执行：只读整理，完成后发回执", "T1": "自动执行：沙箱验证，完成后发回执",
           "T2": "先出方案，方案出来后请在“回执”里批准", "T3": "只出方案，由你亲自执行"}[job["tier"]]
    return {"job_id": job["job_id"], "tier": job["tier"], "tier_label": job["tier_label"], "status": job["status"],
            "reasons": job["tier_reasons"], "message": msg, "worker_pid": pid, "brief": job.get("brief")}


# ----------------------------------------------------------------------------- self-test
def self_test() -> int:
    acfg = dict(DEFAULTS)
    base = {"type": "hypothesis", "title": "召回门控", "minimal_check": "在 20 个任务上 A/B 对比", "expected_observable": "解决率不降",
            "cost_estimate": {"tokens": "~40k", "wall_clock": "2h", "risk": "low"}}
    assert classify(base, acfg, 0)[0] == "T1"
    assert classify(dict(base, type="knowledge"), acfg, 0)[0] == "T0"
    assert classify(dict(base, cost_estimate={"tokens": "~40k", "wall_clock": "1-2d", "risk": "low"}), acfg, 0)[0] == "T2"
    assert classify(dict(base, cost_estimate={"tokens": "200k", "wall_clock": "1h", "risk": "low"}), acfg, 0)[0] == "T2"
    assert classify(dict(base, cost_estimate={"tokens": "~40k", "wall_clock": "2h", "risk": "medium"}), acfg, 0)[0] == "T2"
    assert classify(dict(base, minimal_check="改完后 push 到主分支并部署"), acfg, 0)[0] == "T2"
    assert classify(dict(base, minimal_check="用实验室的仪器重新测一组样品"), acfg, 0)[0] == "T3"
    assert classify(base, acfg, 3)[0] == "T2"
    assert parse_hours("40min") < 1 and parse_hours("1–2 天") == 48 and parse_tokens("~1.2M") == 1_200_000
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        import shutil
        shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", t / "inspiration")
        cfg = json.loads((PKG / "platform/config.example.json").read_text(encoding="utf-8"))
        cfg["review"]["enabled"] = False
        cfg["state_dir"] = "./inspiration"
        (t / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        sd = t / "inspiration"
        (sd / "cards/2026-10-28").mkdir(parents=True)
        cards = [dict(base, id="ins_20261028_001", source={"ref": "9999.1"}, status="proposed", verdict=None),
                 dict(base, id="ins_20261028_002", source={"ref": "9999.2"}, minimal_check="上线到生产环境", status="proposed", verdict=None),
                 dict(base, id="ins_20261028_003", source={"ref": "9999.3"}, minimal_check="去湿实验台跑样品", status="proposed", verdict=None)]
        (sd / "cards/2026-10-28/cards.json").write_text(json.dumps({"date": "2026-10-28", "cards": cards}), encoding="utf-8")
        VERDICT.set_config(t / "config.json")
        for c in cards:
            VERDICT.record(sd, c["id"], "start_now")
        made = sweep(sd, dict(DEFAULTS))
        assert sorted(j["tier"] for j in made) == ["T1", "T2", "T3"], [j["tier"] for j in made]
        assert not sweep(sd, dict(DEFAULTS)), "sweep 不应重复建任务"
        j1 = next(j for j in made if j["tier"] == "T1")
        ok = t / "ok.txt"
        ok.write_text(f"""{RES_START}
{{"job_id": "{j1['job_id']}", "status": "done", "summary": "A/B 跑完，门控组解决率 0.41 vs 0.38",
  "steps": [{{"cmd": "python ab.py --n 20", "exit_code": 0}}],
  "outputs": [{{"path": "/workspace/inspiration_actions/{j1['job_id']}/ab.csv", "what": "逐任务结果"}},
              {{"path": "/workspace/inspiration_actions/{j1['job_id']}/check.py", "what": "最小验证脚本"}},
              {{"path": "/workspace/inspiration_actions/{j1['job_id']}/report.md", "what": "报告"}}],
  "minimal_check_result": {{"observed": "0.41 vs 0.38", "criterion": "解决率不降", "verdict": "supported", "evidence": "ab.csv"}},
  "brief_result": {{"output_measured": "解决率 0.41", "baseline_measured": "解决率 0.38", "criterion_met": true, "deliverables": []}},
  "next_step": "扩大到 100 个任务"}}
{RES_END}""", encoding="utf-8")
        r1 = execute(t / "config.json", j1["job_id"], ok)
        rec = json.loads((job_dir(sd, j1["job_id"]) / "receipt.json").read_text(encoding="utf-8"))
        assert r1["status"] == "succeeded" and rec["conclusion"] == "supported" and rec["all_checks_ok"], rec["checks"]
        assert "执行层证据" in (job_dir(sd, j1["job_id"]) / "receipt.md").read_text(encoding="utf-8")
        rmd = (job_dir(sd, j1["job_id"]) / "receipt.md").read_text(encoding="utf-8")
        assert "动手单（本次任务按它执行）" in rmd and "判据：满足" in rmd, rmd
        assert (job_dir(sd, j1["job_id"]) / "brief.md").is_file() and "【动手单】" in (job_dir(sd, j1["job_id"]) / "prompt_execute.txt").read_text(encoding="utf-8")
        j2 = next(j for j in made if j["tier"] == "T2")
        plan = t / "plan.txt"
        plan.write_text(f"""{RES_START}
{{"job_id": "{j2['job_id']}", "status": "plan", "summary": "先在预发环境验证再上线", "steps": [],
  "outputs": [], "plan": {{"steps": ["打补丁", "预发验证", "上线"], "files_touched": ["svc.py"], "risks": ["回滚窗口"],
  "rollback": "git revert", "cost": "1h"}}, "next_step": "批准后执行"}}
{RES_END}""", encoding="utf-8")
        r2 = execute(t / "config.json", j2["job_id"], plan)
        assert r2["status"] == "needs_approval" and (job_dir(sd, j2["job_id"]) / "plan.md").is_file()
        try:
            ack(sd, j1["job_id"], "maybe")
            raise AssertionError("非法 ack 应报错")
        except ValueError:
            pass
        approve(sd, j2["job_id"], "可以")
        assert read_job(sd, j2["job_id"])["phase"] == "execute"
        j3 = next(j for j in made if j["tier"] == "T3")
        plan3 = t / "plan3.txt"   # v1.5.2: a block for ANOTHER job_id is no longer accepted
        plan3.write_text(plan.read_text(encoding="utf-8").replace(j2["job_id"], j3["job_id"]), encoding="utf-8")
        r3 = execute(t / "config.json", j3["job_id"], plan3)
        assert r3["status"] == "plan_ready"
        try:
            approve(sd, j3["job_id"])
            raise AssertionError("T3 不能批准执行")
        except ValueError:
            pass
        # v1.5.1: observed card → its local evidence file is attached (secrets redacted); off when snippets are off
        (t / "library/code").mkdir(parents=True)
        (t / "library/code/a.py").write_text("API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\nx = 1\n", encoding="utf-8")
        fj = {"card_snapshot": {"source": {"evidence_ref": "library://code/a.py#L1"}}}
        (t / "att").mkdir()
        got = attach_local_evidence(cfg, t / "config.json", fj, t / "att")
        assert got and got[0].name == "local_a.py" and "abcdefghijklmnop" not in got[0].read_text(encoding="utf-8"), got
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["observe"]["upload_code_snippets"] = False
        assert not attach_local_evidence(cfg2, t / "config.json", {"card_snapshot": fj["card_snapshot"]}, t / "att")
        assert not attach_local_evidence(cfg, t / "config.json", {"card_snapshot": {"source": {"evidence_ref": "library://code/../../etc/passwd"}}}, t / "att")
        ack(sd, j1["job_id"], "accept", "数字我核对过")
        assert "已确认：结果可用" in (job_dir(sd, j1["job_id"]) / "receipt.md").read_text(encoding="utf-8")
        bad = t / "bad.txt"
        bad.write_text("我做完了，一切顺利。", encoding="utf-8")
        ack(sd, j1["job_id"], "rerun")
        r4 = execute(t / "config.json", j1["job_id"], bad)
        assert r4["status"] == "failed" and "结果块" in r4["error"]
        # ---- v1.5.2 (Q4) ----
        jid = j1["job_id"]
        # the prompt's own template block (echoed in the stream) is NOT a result
        prm = (job_dir(sd, jid) / "prompt_execute.txt").read_text(encoding="utf-8")
        assert parse_result([prm], jid) is None, "模板不能当结果"
        assert "禁止调用 ask" in prm and "禁止调用 complete" in prm and "必须" in prm
        good = ok.read_text(encoding="utf-8")
        assert parse_result([prm, good, prm], jid)["summary"].startswith("A/B 跑完")
        assert parse_result([good.replace(jid, "act_other")], jid) is None
        assert _loads_lenient('```json\n{"status": "done", "summary": "x",}\n```') == {"status": "done", "summary": "x"}
        # no block on the first run → automatic follow-up → second answer has the block
        retry = t / "retry.txt"
        retry.write_text("我做完了。\n===RETRY===\n" + good, encoding="utf-8")
        ack(sd, jid, "rerun")
        r5 = execute(t / "config.json", jid, retry)
        assert r5["status"] == "succeeded" and len(r5["attempts"]) == 2, r5.get("attempts")
        assert r5["runs"][-1]["kind"] == "followup"
        # asked twice → failed with a clear reason
        asked = t / "asked.txt"
        asked.write_text("STATUS:asked\n是否继续生成交付文件?\n===RETRY===\nSTATUS:asked\n还要继续吗?", encoding="utf-8")
        ack(sd, jid, "rerun")
        r6 = execute(t / "config.json", jid, asked)
        assert r6["status"] == "failed" and "asked" in r6["error"] and "重试 1 次" in r6["error"], r6["error"]
        # Z while RUNNING: the job is cancelled at once; the worker discards the result and does not notify
        ack(sd, jid, "rerun")
        real_rop = globals()["run_on_platform"]
        def rop_cancel(*a, **kw):
            assert cancel_for_card(sd, j1["card_id"]) == [jid]
            return real_rop(*a, **kw)
        globals()["run_on_platform"] = rop_cancel
        try:
            r7 = execute(t / "config.json", jid, ok)
        finally:
            globals()["run_on_platform"] = real_rop
        assert r7["status"] == "cancelled" and r7.get("discarded_result") is True and read_job(sd, jid)["status"] == "cancelled"
        # v1.5.3: readable cancel text + no half run rows
        j7 = read_job(sd, jid)
        assert all(r.get("run_status") for r in j7["runs"] if r.get("agent_run_id")), j7["runs"]
        assert cancel_text(j7).startswith("已完成 · 结果已丢弃") and cancel_text(j7).endswith("因撤销「立刻动手」"), cancel_text(j7)
        assert cancel_text({"status": "cancelled", "runs": [], "cancel_why": "评价改为否决"}) == "已取消（未开始） · 因评价改为否决"
        assert cancel_text({"status": "cancelled", "runs": [{"agent_run_id": "r"}]}).startswith("已取消（执行中被打断）")
        assert cancel_text({"status": "succeeded"}) is None
        # timeline: this run vs the (old) first press of 6
        tl = timeline_line({"verdict_at": "2026-10-29T17:40:00+08:00", "created_at": "2026-10-31T01:32:00+08:00",
                            "started_at": "2026-10-31T01:32:01+08:00", "finished_at": "2026-10-31T01:38:00+08:00",
                            "start_latency_s": 1, "verdict_to_create_s": 114720, "wall_clock_s": 359})
        assert "本次建任务 10-31 01:32:00" in tl and "1 秒后" in tl and "31 小时后才建任务" in tl, tl
        ev = [json.loads(l)["event"] for l in (sd / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
        assert ev.count("created") == 3 and "approved" in ev and "acked" in ev, ev
        VERDICT.record(sd, "ins_20261028_001", "adopt")
    print("SELF-TEST OK: run_action")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="“立刻动手”分档执行与回执")
    ap.add_argument("--config", type=Path)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--card-id")
    g.add_argument("--brief", metavar="CARD_ID", help="只打印这张卡的动手单（不建任务）")
    g.add_argument("--sweep", action="store_true")
    g.add_argument("--approve")
    g.add_argument("--ack")
    g.add_argument("--execute")
    ap.add_argument("--grace", action="store_true", help="执行前先等 actions.cancel_grace_s 秒（评价页按 6 后可以按 Z 撤销）")
    g.add_argument("--list", action="store_true")
    g.add_argument("--self-test", action="store_true")
    ap.add_argument("--result", choices=sorted(ACK_RESULTS))
    ap.add_argument("--note", default="")
    ap.add_argument("--no-run", action="store_true", help="只分档建任务，不启动后台执行")
    ap.add_argument("--fake-agent-output", type=Path)
    ap.add_argument("--brief-file", type=Path, help="v1.5.1：用这个 JSON 动手单建任务（--card-id 时）")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.config:
        ap.error("需要 --config")
    cfg, state_dir, acfg = load_cfg(a.config)
    VERDICT.set_config(a.config.resolve())
    try:
        if a.execute:
            j = execute(a.config, a.execute, a.fake_agent_output, grace=a.grace)
            print(f"{j['job_id']} → {j['status']}；回执 {job_dir(state_dir, j['job_id']) / 'receipt.md'}")
            return 0 if j["status"] in ("succeeded", "needs_approval", "plan_ready") else 1
        if a.brief:
            b = make_brief(state_dir, a.brief)
            print(AB.render_md(b))
            for x in b.get("problems", []):
                print("  问题：", x)
            for x in b.get("missing", []):
                print("  待你填写：", x)
            print(json.dumps({k: v for k, v in b.items() if k not in ("problems", "missing")}, ensure_ascii=False, indent=1))
            return 0
        if a.card_id:
            bf = json.loads(a.brief_file.read_text(encoding="utf-8")) if a.brief_file else None
            d = dispatch(a.config, a.card_id, "cli", None, run=not a.no_run, fake=a.fake_agent_output, brief=bf)
            print(json.dumps(d, ensure_ascii=False, indent=2))
            return 0 if d.get("job_id") or not d.get("needs_input") else 3
        if a.sweep:
            made = sweep(state_dir, acfg)
            for j in made:
                pid = None if a.no_run else spawn(a.config, j["job_id"], state_dir, a.fake_agent_output)
                print(f"{j['job_id']} {j['card_id']} → {j['tier']} {j['tier_label']}" + (f"（后台 pid {pid}）" if pid else ""))
            print(f"OK: 新建 {len(made)} 个任务")
            return 0
        if a.approve:
            j = approve(state_dir, a.approve, a.note)
            pid = None if a.no_run else spawn(a.config, j["job_id"], state_dir, a.fake_agent_output)
            print(f"APPROVED {j['job_id']}" + (f"，后台执行 pid {pid}" if pid else ""))
            return 0
        if a.ack:
            if not a.result:
                ap.error("--ack 需要 --result accept|rerun|discard")
            j = ack(state_dir, a.ack, a.result, a.note)
            pid = spawn(a.config, j["job_id"], state_dir, a.fake_agent_output) if a.result == "rerun" and j["status"] in ("queued", "approved") and not a.no_run else None
            print(f"ACK {j['job_id']} ← {ACK_RESULTS[a.result]}" + (f"；已重新排队（pid {pid}）" if pid else ""))
            return 0
        for j in list_jobs(state_dir):
            r = j.get("receipt") or {}
            print(f"{j['job_id']}  {j['tier']}  {j['status']:<15} {VERDICT_ZH.get(str(r.get('conclusion')), '-'):<6} "
                  f"{'ack=' + j['ack']['result'] if j.get('ack') else '待确认' if j['status'] in TERMINAL else ''}  {j['card_title']}")
        return 0
    except (ValueError, LookupError) as exc:
        print("ERROR:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
