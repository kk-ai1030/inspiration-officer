#!/usr/bin/env python3
"""inspiration-officer 的 MCP stdio 服务器（零第三方依赖，只用标准库）。

把本机灵感状态的读写能力暴露成 MCP tools，方便在别的 agent 里测试完整闭环：
    看状态 → 列卡片 → 评价 → 采纳后结果回填 → 周报 / 脱敏摘要 → 最小验证脚本

设计约束（与仓库一致）：
  - 纯本地、不联网、不需要 SciencePal 令牌；stdout 只输出协议 JSON，日志走 stderr；
  - 所有数据都在 --state-dir 指定的目录里，工具不接受任意路径，返回内容不含本机绝对路径；
  - 写操作（record_verdict / record_outcome / verdict_summary.write）是显式的，
    --read-only 下连工具都不列出；
  - dry-run 脚本只打印清单，不执行实验。

协议：MCP over stdio（每行一个 JSON-RPC 2.0 消息）。实现 initialize / ping /
tools/list / tools/call / resources/list / prompts/list，兼容 2024-11-05 及之后版本。

Usage:
    python mcp/inspiration_mcp.py --state-dir inspiration
    python mcp/inspiration_mcp.py --state-dir inspiration --read-only
    python mcp/inspiration_mcp.py --state-dir inspiration --usage   # 使用画像，默认关闭
    python mcp/inspiration_mcp.py --self-test
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
DEFAULT_STATE = ROOT / "inspiration"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL = "2024-11-05"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").is_file() else "dev"
CARD_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
ABS_PATH_RE = re.compile(r"([A-Za-z]:[\\/]|/Users/|/home/)")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


VERDICT = _load("insp-feedback/scripts/record_verdict.py", "mcp_verdict")
OUTCOME = _load("insp-feedback/scripts/record_outcome.py", "mcp_outcome")
RS = _load("platform/review_server.py", "mcp_review_server")
VS = _load("platform/verdict_summary.py", "mcp_vsummary")
DT = _load("platform/dryrun_template.py", "mcp_dryrun")
WR = _load("insp-review/scripts/weekly_review.py", "mcp_weekly")
XS = _load("insp-nightly/scripts/extra_sources.py", "mcp_extra")
UL = _load("mcp/usage_log.py", "mcp_usage_log")
FW = _load("mcp/flywheel.py", "mcp_flywheel")

WRITE_TOOLS = {"insp_record_verdict", "insp_record_outcome", "insp_forget"}


class Ctx:
    def __init__(self, state_dir: Path, config: Path | None, read_only: bool, usage_enabled: bool = False):
        self.state_dir = state_dir
        self.config = config
        self.read_only = read_only
        self.usage_enabled = usage_enabled


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def scrub(obj):
    """递归去掉本机绝对路径，避免把用户目录回传给第三方 agent。"""
    if isinstance(obj, str):
        return "<本地路径已省略>" if ABS_PATH_RE.search(obj) else obj
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    return obj


def card_id(args: dict) -> str:
    cid = str(args.get("card_id") or "").strip()
    if not CARD_ID_RE.match(cid):
        raise ValueError("card_id 格式不对（只允许字母数字和 _ . : -，最长 120）")
    return cid


def get_card(ctx: Ctx, cid: str) -> dict:
    card = OUTCOME.find_card(ctx.state_dir, cid)
    if card is None:
        raise LookupError(f"找不到卡片 {cid}")
    return card


def limit_of(args: dict, default: int = 20) -> int:
    try:
        return max(1, min(200, int(args.get("limit", default))))
    except (TypeError, ValueError):
        raise ValueError("limit 必须是 1–200 的整数")


# ----------------------------------------------------------------------------- tool handlers
def t_status(args: dict, ctx: Ctx):
    sd = ctx.state_dir
    state = json.loads((sd / "state.json").read_text(encoding="utf-8")) if (sd / "state.json").is_file() else {}
    cards = RS.load_cards(sd, "all")
    weights = json.loads((sd / "taste/weights.json").read_text(encoding="utf-8")) if (sd / "taste/weights.json").is_file() else {}
    last = UL.summary(sd)["last"] if ctx.usage_enabled else None
    return {"ok": True, "state_dir": sd.name, "state_json": (sd / "state.json").is_file(),
            "usage_enabled": ctx.usage_enabled, "last_tool_call": scrub(last) if last else None,
            "cards_total": cards["total"], "pending": cards["pending"],
            "verdicts": len(VERDICT.read_verdicts(sd)), "outcomes": len(OUTCOME.read_outcomes(sd)),
            "weights_version": weights.get("version"), "read_only": ctx.read_only,
            "available_actions": sorted(VERDICT.ACTIONS), "available_outcomes": sorted(OUTCOME.OUTCOMES),
            "candidate_sources": XS.plan(state), "server_version": VERSION,
            "hint": "状态目录不存在或为空时，先按仓库 README 初始化 inspiration/。"}


def t_list_cards(args: dict, ctx: Ctx):
    scope = str(args.get("scope") or "pending")
    if scope not in ("pending", "all"):
        raise ValueError("scope 只能是 pending 或 all")
    n = limit_of(args, 20)
    d = RS.load_cards(ctx.state_dir, scope)
    return {"scope": scope, "total": d["total"], "pending": d["pending"], "returned": min(n, len(d["cards"])),
            "cards": scrub(d["cards"][:n]), "actions": d["actions"]}


def t_get_card(args: dict, ctx: Ctx):
    return scrub(get_card(ctx, card_id(args)))


def t_list_verdicts(args: dict, ctx: Ctx):
    rows = VERDICT.read_verdicts(ctx.state_dir)
    n = limit_of(args, 20)
    stacks = VERDICT.verdict_stacks(rows)
    return {"total": len(rows), "returned": min(n, len(rows)), "rows": scrub(rows[-n:]),
            "stacks": {k: scrub(v[-3:]) for k, v in stacks.items()}}


def t_record_verdict(args: dict, ctx: Ctx):
    cid = card_id(args)
    action = str(args.get("action") or "")
    if action not in VERDICT.ACTIONS:
        raise ValueError(f"未知 action：{action}，可选 {sorted(VERDICT.ACTIONS)}")
    row = VERDICT.record(ctx.state_dir, cid, action, reason=str(args.get("reason") or ""), via="mcp")
    return {"written": True, "entry": scrub(row)}


def t_record_outcome(args: dict, ctx: Ctx):
    cid = card_id(args)
    outcome = str(args.get("outcome") or "")
    if outcome not in OUTCOME.OUTCOMES:
        raise ValueError(f"未知 outcome：{outcome}，可选 {sorted(OUTCOME.OUTCOMES)}")
    row = OUTCOME.record(ctx.state_dir, cid, outcome,
                         conclusion=str(args.get("conclusion") or ""),
                         measured=str(args.get("measured") or ""), via="mcp")
    return {"written": True, "entry": scrub(row)}


def t_outcome_summary(args: dict, ctx: Ctx):
    latest = OUTCOME.latest_by_card(ctx.state_dir)
    return {"summary": OUTCOME.summarize(ctx.state_dir),
            "by_card": {k: scrub(v) for k, v in list(latest.items())[:limit_of(args, 20)]}}


def t_weekly_report(args: dict, ctx: Ctx):
    today = date.fromisoformat(args["date"]) if args.get("date") else date.today()
    text, _ = WR.review(ctx.state_dir, today, None, ctx.config)
    return scrub(text)


def t_verdict_summary(args: dict, ctx: Ctx):
    if args.get("write") and not ctx.read_only:
        VS.write(ctx.state_dir, ctx.config)
        return {"written": True, "summary": VS.build(ctx.state_dir, ctx.config)}
    if args.get("write"):
        raise PermissionError("服务器以 --read-only 启动，不能写 verdict_summary.json")
    return VS.build(ctx.state_dir, ctx.config)


def t_dryrun_script(args: dict, ctx: Ctx):
    return DT.build(get_card(ctx, card_id(args)))["content"]


def t_dryrun_receipt(args: dict, ctx: Ctx):
    dry = DT.receipt(get_card(ctx, card_id(args)))
    return DT.render_receipt_md(dry) + "\n\n" + json.dumps(scrub(dry), ensure_ascii=False, indent=2)


def t_list_jobs(args: dict, ctx: Ctx):
    n = limit_of(args, 20)
    jobs = []
    for p in sorted((ctx.state_dir / "actions").glob("*/job.json"), reverse=True)[:n]:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        r = j.get("receipt") or {}
        jobs.append({k: j.get(k) for k in ("job_id", "card_id", "card_title", "tier", "phase", "status", "created_at", "ack")}
                    | {"conclusion": r.get("conclusion"), "dry_run": scrub(r.get("dry_run"))})
    return {"returned": len(jobs), "jobs": scrub(jobs)}


def t_sources_plan(args: dict, ctx: Ctx):
    state = json.loads((ctx.state_dir / "state.json").read_text(encoding="utf-8")) \
        if (ctx.state_dir / "state.json").is_file() else {}
    return {"plan": XS.plan(state),
            "note": "这里只读开关状态，不联网抓取。要启用请按 insp-triage/references/候选来源与增量规范.md 配置。"}


def t_help(args: dict, ctx: Ctx):
    d = {"typical_flow": ["insp_status", "insp_list_cards", "insp_get_card", "insp_record_verdict",
                          "insp_record_outcome", "insp_weekly_report", "insp_dryrun_script"],
         "writes": sorted(WRITE_TOOLS), "read_only": ctx.read_only, "usage_enabled": ctx.usage_enabled,
         "note": "所有工具都只访问 --state-dir；卡片生成仍由 SciencePal 云端智能体负责，MCP 不联网。"}
    if ctx.usage_enabled:
        d["your_pattern"] = UL.guidance(ctx.state_dir)
        d["note"] += " 使用画像已开启：insp_usage 看统计，insp_forget 一键清除。"
    else:
        d["note"] += " 使用画像默认关闭，加 --usage 或 INSP_MCP_USAGE=1 可开启。"
    return d


def t_flywheel(args: dict, ctx: Ctx):
    return FW.build(ctx.state_dir, ctx.config)


def t_usage(args: dict, ctx: Ctx):
    return {"enabled": ctx.usage_enabled, **UL.summary(ctx.state_dir),
            "note": "使用画像只记操作类别，不记 reason / conclusion / 卡片标题等原文；默认关闭，insp_forget 可清除。"}


def t_forget(args: dict, ctx: Ctx):
    return {"forgotten": UL.forget(ctx.state_dir),
            "note": "已清除本机 MCP 使用画像；评价、卡片、权重与结果回填都不受影响。"}


HANDLERS = {
    "insp_status": t_status, "insp_list_cards": t_list_cards, "insp_get_card": t_get_card,
    "insp_list_verdicts": t_list_verdicts, "insp_record_verdict": t_record_verdict,
    "insp_record_outcome": t_record_outcome, "insp_outcome_summary": t_outcome_summary,
    "insp_weekly_report": t_weekly_report, "insp_verdict_summary": t_verdict_summary,
    "insp_dryrun_script": t_dryrun_script, "insp_dryrun_receipt": t_dryrun_receipt,
    "insp_list_jobs": t_list_jobs, "insp_sources_plan": t_sources_plan, "insp_help": t_help,
    "insp_flywheel": t_flywheel, "insp_usage": t_usage, "insp_forget": t_forget,
}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


CID = {"type": "string", "description": "卡片 ID，例如 ins_20261112_001；非 arXiv 来源可能带 oa: / rss: 前缀"}
LIM = {"type": "integer", "description": "最多返回几条（1–200，默认 20）", "minimum": 1, "maximum": 200}


def tool_defs(ctx: Ctx) -> list[dict]:
    defs = [
        ("insp_status", "只读。查看状态目录概览：卡片数、待评数、评价与回填条数、权重版本、候选来源开关。", _schema({})),
        ("insp_list_cards", "只读。列出卡片（scope=pending 未评价 / all 全部），含评价栈与结果回填摘要。",
         _schema({"scope": {"type": "string", "enum": ["pending", "all"]}, "limit": LIM})),
        ("insp_get_card", "只读。按 card_id 返回一张完整卡片。", _schema({"card_id": CID}, ["card_id"])),
        ("insp_list_verdicts", "只读。列出评价历史与每卡的评价栈。", _schema({"limit": LIM})),
        ("insp_record_verdict", "**写本地文件**。给卡片记一条评价（adopt/reject/done/not_important/ask_detail/start_now 等）。",
         _schema({"card_id": CID, "action": {"type": "string", "enum": sorted(VERDICT.ACTIONS)},
                  "reason": {"type": "string", "description": "理由，可空"}}, ["card_id", "action"])),
        ("insp_record_outcome", "**写本地文件**。回填「这条建议最后做没做、结论是否成立」（B1），不影响权重与评价栈。",
         _schema({"card_id": CID, "outcome": {"type": "string", "enum": sorted(OUTCOME.OUTCOMES)},
                  "conclusion": {"type": "string"}, "measured": {"type": "string"}}, ["card_id", "outcome"])),
        ("insp_outcome_summary", "只读。采纳后结果回填的聚合统计（B1）。", _schema({"limit": LIM})),
        ("insp_weekly_report", "只读。按本机完整数据生成周报 Markdown；不写文件、不改权重。",
         _schema({"date": {"type": "string", "description": "YYYY-MM-DD，缺省今天"}})),
        ("insp_verdict_summary", "只读（除非 write=true）。生成 B4 脱敏评价摘要；不含评价原文/卡片标题/记忆/路径。",
         _schema({"write": {"type": "boolean", "description": "true 时把摘要写入状态目录（--read-only 下拒绝）"}})),
        ("insp_dryrun_script", "只读。返回某张卡的最小验证 dry-run 脚本源码（只打印清单，不执行、不联网）。",
         _schema({"card_id": CID}, ["card_id"])),
        ("insp_dryrun_receipt", "只读。B7 dry-run 回执（Markdown + 结构化 JSON），恒为未执行 / 无定论。",
         _schema({"card_id": CID}, ["card_id"])),
        ("insp_list_jobs", "只读。列出「立刻动手」任务与回执概要，含独立的 dry_run 字段。", _schema({"limit": LIM})),
        ("insp_sources_plan", "只读。查看 B2 额外候选来源的开关配置（不联网、不抓取）。", _schema({})),
        ("insp_help", "只读。返回推荐调用顺序、读写工具清单，以及开启使用画像后的个性化引导。", _schema({})),
        ("insp_flywheel", "只读。飞轮观测台：采纳率趋势、p̂、权重版本与提议、结果成立率、隐式信号，"
                          "并诚实列出目前还没回流的信号。不改权重、不写文件。", _schema({})),
        ("insp_usage", "只读。查看 MCP 使用画像统计（默认关闭时不产生任何日志）。", _schema({})),
        ("insp_forget", "**写本地文件**。一键清除 MCP 使用画像；不影响评价 / 卡片 / 权重 / 结果回填。", _schema({})),
    ]
    return [{"name": n, "description": d, "inputSchema": s} for n, d, s in defs
            if not (ctx.read_only and n in WRITE_TOOLS)]


# ----------------------------------------------------------------------------- JSON-RPC / MCP
def _ok(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def call_tool(name: str, arguments: dict, ctx: Ctx) -> dict:
    if ctx.read_only and name in WRITE_TOOLS:
        return {"content": [{"type": "text", "text": f"服务器以 --read-only 启动，{name} 不可用"}], "isError": True}
    fn = HANDLERS.get(name)
    if fn is None:
        return {"content": [{"type": "text", "text": f"未知工具 {name}，用 tools/list 查看可用工具"}], "isError": True}
    args = arguments or {}
    t0 = time.perf_counter()
    ok = False
    try:
        result = fn(args, ctx)
        text = result if isinstance(result, str) else json.dumps(scrub(result), ensure_ascii=False, indent=2)
        ok = True
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as exc:  # noqa: BLE001 - 工具错误按 MCP 约定走 isError，不打断会话
        log(f"TOOL ERROR {name}: {type(exc).__name__}: {exc}")
        return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
    finally:
        if ctx.usage_enabled:   # 埋点失败静默跳过；观测类工具在 usage_log 内部不记
            UL.append(ctx.state_dir, name, args, ok, (time.perf_counter() - t0) * 1000)


def handle(msg: dict, ctx: Ctx) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "Invalid Request")
    mid = msg.get("id")
    method = msg.get("method") or ""
    if mid is None:   # notification：initialize 后的 notifications/initialized 等，不回复
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        ver = params.get("protocolVersion")
        if ver not in SUPPORTED_PROTOCOLS:
            ver = DEFAULT_PROTOCOL
        return _ok(mid, {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "inspiration-officer", "version": VERSION},
                         "instructions": "先调用 insp_help / insp_status；写评价用 insp_record_verdict，采纳后结果用 insp_record_outcome。"})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": tool_defs(ctx)})
    if method == "tools/call":
        return _ok(mid, call_tool(str(params.get("name") or ""), params.get("arguments") or {}, ctx))
    if method in ("resources/list", "resources/templates/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        return _ok(mid, {key: []})
    return _err(mid, -32601, f"Method not found: {method}")


def send(obj: dict) -> None:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def serve(ctx: Ctx) -> int:
    log(f"inspiration-officer MCP {VERSION} ready · state_dir={ctx.state_dir.name} · "
        f"read_only={ctx.read_only} · usage={ctx.usage_enabled}")
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send(_err(None, -32700, "Parse error"))
            continue
        try:
            resp = handle(msg, ctx)
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
            resp = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, "Internal error")
        if resp is not None:
            send(resp)
    return 0


def self_test() -> int:
    """不启动进程、不起客户端，直接跑一轮协议 + 主要工具（用临时状态目录）。"""
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="insp_mcp_"))
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:200]) if detail and not cond else ""))
        ok &= bool(cond)

    try:
        sd = tmp / "inspiration"
        shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
        (sd / "cards" / "2026-11-12").mkdir(parents=True, exist_ok=True)
        c = {"id": "ins_mcp_1", "type": "knowledge", "title": "MCP 自测卡", "one_line": "一句话",
             "minimal_check": "跑 A / B 两组", "expected_observable": "A 比 B 高", "falsifier": "A 不比 B 高",
             "source": {"kind": "arxiv", "ref": "9999.12345", "quote": "q", "verified": True},
             "status": "proposed", "verdict": None}
        (sd / "cards" / "2026-11-12" / "cards.json").write_text(
            json.dumps({"date": "2026-11-12", "cards": [c]}, ensure_ascii=False), encoding="utf-8")
        ctx = Ctx(sd, None, False)
        r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}}, ctx)
        check("initialize 回显协议版本", r["result"]["protocolVersion"] == "2025-06-18")
        check("notification 不回复", handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, ctx) is None)
        tl = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ctx)["result"]["tools"]
        names = {t["name"] for t in tl}
        check("工具齐全且 schema 合法", len(names) == len(HANDLERS) and
              all(t["inputSchema"]["type"] == "object" for t in tl))
        status = call_tool("insp_status", {}, ctx)
        check("insp_status", status["isError"] is False and '"cards_total": 1' in status["content"][0]["text"])
        v = call_tool("insp_record_verdict", {"card_id": "ins_mcp_1", "action": "adopt", "reason": "mcp"}, ctx)
        o = call_tool("insp_record_outcome", {"card_id": "ins_mcp_1", "outcome": "confirmed", "conclusion": "成立"}, ctx)
        check("写评价 / 回填成功", v["isError"] is False and o["isError"] is False)
        check("verdicts / outcomes 已落盘", len(VERDICT.read_verdicts(sd)) == 1 and len(OUTCOME.read_outcomes(sd)) == 1)
        check("周报可读", call_tool("insp_weekly_report", {}, ctx)["isError"] is False)
        check("脱敏摘要可读", '"schema"' in call_tool("insp_verdict_summary", {}, ctx)["content"][0]["text"])
        script = call_tool("insp_dryrun_script", {"card_id": "ins_mcp_1"}, ctx)["content"][0]["text"]
        check("dry-run 脚本安全", "DRY-RUN" in script and all(x not in script for x in DT.DANGEROUS))
        fw = call_tool("insp_flywheel", {}, ctx)
        check("飞轮观测台只读可用", fw["isError"] is False and
              "insp-flywheel/1.0" in fw["content"][0]["text"] and
              "not_consumed" in fw["content"][0]["text"], fw["content"][0]["text"][:200])
        check("使用画像默认关闭", '"enabled": false' in call_tool("insp_usage", {}, ctx)["content"][0]["text"]
              and not UL.path_of(sd).exists())
        uctx = Ctx(sd, None, False, True)
        call_tool("insp_record_verdict", {"card_id": "ins_mcp_1", "action": "adopt", "reason": "绝密理由"}, uctx)
        usage_text = UL.path_of(sd).read_text(encoding="utf-8")
        check("埋点只记类别不记原文", "绝密理由" not in usage_text and "reason" not in usage_text and
              '"action": "adopt"' in usage_text, usage_text[:200])
        check("个性化引导", "your_pattern" in call_tool("insp_help", {}, uctx)["content"][0]["text"])
        check("状态带上次操作", '"last_tool_call"' in call_tool("insp_status", {}, uctx)["content"][0]["text"])
        check("一键清除画像", '"removed": true' in call_tool("insp_forget", {}, uctx)["content"][0]["text"]
              and not UL.path_of(sd).exists())
        check("未知工具 isError", call_tool("nope", {}, ctx)["isError"] is True)
        check("未知方法 -32601", handle({"jsonrpc": "2.0", "id": 3, "method": "nope"}, ctx)["error"]["code"] == -32601)
        ro = Ctx(sd, None, True)
        check("read-only 隐藏写工具", WRITE_TOOLS.isdisjoint({t["name"] for t in tool_defs(ro)}))
        check("read-only 拒绝写调用", call_tool("insp_record_verdict", {"card_id": "ins_mcp_1", "action": "adopt"}, ro)["isError"] is True)
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST OK: inspiration_mcp" if ok else "SELF-TEST FAILED: inspiration_mcp")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", type=Path, default=Path(os.environ.get("INSP_STATE_DIR") or DEFAULT_STATE),
                    help="本机灵感状态目录（默认 ./inspiration，或环境变量 INSP_STATE_DIR）")
    ap.add_argument("--config", type=Path, help="可选的 config.json，用于周报 / 摘要的权重配置")
    ap.add_argument("--read-only", action="store_true", help="只暴露只读工具，屏蔽所有写入")
    ap.add_argument("--usage", action="store_true",
                    help="开启 MCP 使用画像（默认关闭；也可设 INSP_MCP_USAGE=1）。只记操作类别，不记用户原文")
    ap.add_argument("--self-test", action="store_true", help="离线跑一轮协议与工具自测后退出")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    cfg = a.config
    if cfg is None and (ROOT / "config.json").is_file():
        cfg = ROOT / "config.json"
    usage = a.usage or os.environ.get("INSP_MCP_USAGE") == "1"
    return serve(Ctx(a.state_dir.resolve(), cfg, a.read_only, usage))


if __name__ == "__main__":
    raise SystemExit(main())
