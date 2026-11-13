#!/usr/bin/env python3
"""MCP 使用画像：默认关闭的本地 append-only 埋点。

只记「哪类操作」，不记任何用户原文（reason / conclusion / measured / 卡片标题 / 引文
一律不落盘）。目的是让 MCP 记得「上次做到哪一步」「你常用什么」，从而越用越顺手：
    - insp_help 给个性化引导
    - insp_status 带最近一次有意义的操作
    - insp_usage 看统计；insp_forget 一键清除

开关：启动参数 --usage 或环境变量 INSP_MCP_USAGE=1；缺省完全不写任何文件。
多进程安全：os.open(O_APPEND) + 单次 os.write，不做 read-modify-write。

    python mcp/usage_log.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

USAGE_FILE = "mcp_usage.jsonl"
# 观测类工具不埋点：否则「上次调用」永远会是 insp_status 自己
SKIP_TOOLS = {"insp_status", "insp_help", "insp_usage", "insp_forget", "insp_flywheel"}
# 只允许这些类别值进入日志；card_id 单独放行（标识符，不是原文）
SAFE_KEYS = ("scope", "action", "outcome", "write")
FLOW = ("insp_list_cards", "insp_get_card", "insp_record_verdict", "insp_record_outcome",
        "insp_weekly_report", "insp_dryrun_script", "insp_flywheel")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def path_of(state_dir: Path) -> Path:
    return Path(state_dir) / USAGE_FILE


def append(state_dir: Path, tool: str, args: dict, ok: bool, ms: float) -> dict | None:
    """单行追加。任何失败都静默跳过：埋点绝不能影响工具本身。"""
    if tool in SKIP_TOOLS:
        return None
    row = {"ts": now_iso(), "tool": str(tool), "ok": bool(ok), "ms": round(float(ms), 1), "via": "mcp"}
    for k in SAFE_KEYS:
        v = (args or {}).get(k)
        if isinstance(v, (str, bool, int)) and v != "":
            row[k] = v if not isinstance(v, str) else v[:40]
    cid = (args or {}).get("card_id")
    if isinstance(cid, str) and cid:
        row["card_id"] = cid[:120]
    line = json.dumps(row, ensure_ascii=False) + "\n"
    try:
        fd = os.open(str(path_of(state_dir)), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return None
    return row


def read(state_dir: Path) -> list[dict]:
    p = path_of(state_dir)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except json.JSONDecodeError:
            continue
    return out


def summary(state_dir: Path) -> dict:
    rows = read(state_dir)
    by_tool = Counter(r.get("tool") for r in rows if r.get("tool"))
    actions = Counter(r.get("action") for r in rows if r.get("action"))
    outcomes = Counter(r.get("outcome") for r in rows if r.get("outcome"))
    last = rows[-1] if rows else None
    return {"total": len(rows), "by_tool": dict(by_tool.most_common()), "actions": dict(actions.most_common()),
            "outcomes": dict(outcomes.most_common()), "first_at": rows[0].get("ts") if rows else None,
            "last": last, "failed": sum(1 for r in rows if not r.get("ok"))}


def guidance(state_dir: Path) -> list[str]:
    """给 insp_help 的个性化提示；没有数据时返回空。"""
    s = summary(state_dir)
    if not s["total"]:
        return []
    if not s["by_tool"]:
        return []
    top, n = next(iter(s["by_tool"].items()))
    out = [f"你在本机已调用 {s['total']} 次工具，最常用的是 {top}（{n} 次）。"]
    if s["last"]:
        out.append(f"上一次有意义的操作：{s['last'].get('tool')}（{s['last'].get('ts')}）"
                   + (f"，卡片 {s['last']['card_id']}" if s["last"].get("card_id") else "") + "。")
    untried = [t for t in FLOW if t not in s["by_tool"]]
    if untried:
        out.append("还没试过：" + "、".join(untried[:4]) + "。")
    return out


def forget(state_dir: Path) -> dict:
    p = path_of(state_dir)
    existed = p.is_file()
    n = len(read(state_dir)) if existed else 0
    if existed:
        p.unlink()
    return {"removed": existed, "rows_removed": n}


def self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_usage_"))
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:200]) if detail and not cond else ""))
        ok &= bool(cond)

    try:
        append(tmp, "insp_record_verdict", {"card_id": "ins_1", "action": "adopt", "reason": "绝密原文"}, True, 1.2)
        append(tmp, "insp_record_outcome", {"card_id": "ins_1", "outcome": "confirmed", "conclusion": "绝密结论"}, True, 2.0)
        append(tmp, "insp_get_card", {"card_id": "ins_1"}, True, 0.5)
        append(tmp, "insp_status", {}, True, 0.1)   # 观测类应被跳过
        text = path_of(tmp).read_text(encoding="utf-8")
        check("只记 3 条、观测类跳过", len(read(tmp)) == 3, text)
        check("不记任何用户原文", "绝密" not in text and "reason" not in text and "conclusion" not in text, text)
        check("类别字段齐备", '"action": "adopt"' in text and '"outcome": "confirmed"' in text)
        s = summary(tmp)
        check("统计正确", s["total"] == 3 and s["by_tool"]["insp_record_verdict"] == 1 and s["failed"] == 0)
        check("个性化引导可用", any("最常用" in x for x in guidance(tmp)))
        fd = os.open(str(path_of(tmp)), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, b"{broken json\n")
        os.close(fd)
        check("坏行不炸", summary(tmp)["total"] == 3)
        check("一键清除", forget(tmp)["removed"] is True and not path_of(tmp).exists())
        check("重复清除安全", forget(tmp)["removed"] is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST OK: usage_log" if ok else "SELF-TEST FAILED: usage_log")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    return self_test() if a.self_test else 0


if __name__ == "__main__":
    raise SystemExit(main())
