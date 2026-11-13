#!/usr/bin/env python3
"""MCP stdio 服务器：用真实子进程跑 JSON-RPC 协议回归（零联网）。

验证：stdout 每行都是协议 JSON（日志不混入）、notification 不回复、
tools/list schema、完整读写闭环、错误码、--read-only 隔离、返回内容不含本机路径。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp/inspiration_mcp.py"
OK = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:300]) if detail and not cond else ""))
    OK &= bool(cond)


class Client:
    def __init__(self, sd: Path, extra: list[str] | None = None):
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, "-X", "utf8", str(SERVER), "--state-dir", str(sd), *(extra or [])],
                                  cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            self.q.put(line.rstrip("\r\n"))

    def send(self, raw: str) -> None:
        self.p.stdin.write(raw + "\n")
        self.p.stdin.flush()

    def rpc(self, method: str, params: dict | None = None, mid: int = 1) -> dict:
        self.send(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
                             ensure_ascii=False))
        return json.loads(self.q.get(timeout=20))

    def notify(self, method: str) -> None:
        self.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": {}}, ensure_ascii=False))

    def tool(self, name: str, args: dict | None = None, mid: int = 1) -> tuple[dict, str]:
        r = self.rpc("tools/call", {"name": name, "arguments": args or {}}, mid)
        return r.get("result", {}), r["result"]["content"][0]["text"]

    def close(self) -> str:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        self.p.wait(timeout=10)
        return self.p.stderr.read()


def seed(sd: Path) -> None:
    shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
    (sd / "cards" / "2026-11-12").mkdir(parents=True, exist_ok=True)
    c = {"id": "ins_mcp_1", "type": "knowledge", "title": 'MCP 测试卡 "引号"', "one_line": "一句话",
         "minimal_check": "跑 A / B 两组", "expected_observable": "A 比 B 高 3%", "falsifier": "A 不比 B 高",
         "source": {"kind": "arxiv", "ref": "9999.12345", "quote": "q", "verified": True},
         "status": "proposed", "verdict": None, "cost_estimate": {"tokens": "~1k"}}
    (sd / "cards" / "2026-11-12" / "cards.json").write_text(
        json.dumps({"date": "2026-11-12", "cards": [c]}, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_mcp_stdio_"))
    sd = tmp / "inspiration"
    seed(sd)
    try:
        print("== MCP-1 握手与协议")
        c = Client(sd)
        r = c.rpc("initialize", {"protocolVersion": "2025-06-18",
                                 "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}, 1)
        res = r["result"]
        check("MCP-1 initialize", r["id"] == 1 and res["protocolVersion"] == "2025-06-18" and
              res["serverInfo"]["name"] == "inspiration-officer" and "tools" in res["capabilities"], str(r))
        c.notify("notifications/initialized")
        t = c.rpc("tools/list", {}, 2)
        tools = t["result"]["tools"]
        names = {x["name"] for x in tools}
        check("MCP-1 notification 不产生响应", t["id"] == 2, str(t)[:200])
        check("MCP-1 18 个工具且 schema 合法", len(names) == 18 and
              all(x["inputSchema"]["type"] == "object" and x["description"] for x in tools), str(sorted(names)))
        check("MCP-1 ping / 空资源与提示", c.rpc("ping", {}, 3)["result"] == {} and
              c.rpc("resources/list", {}, 4)["result"] == {"resources": []} and
              c.rpc("prompts/list", {}, 5)["result"] == {"prompts": []})
        check("MCP-1 未知方法 -32601", c.rpc("nope", {}, 6)["error"]["code"] == -32601)
        c.send("{not json")
        check("MCP-1 坏 JSON -32700", json.loads(c.q.get(timeout=10))["error"]["code"] == -32700)

        print("== MCP-2 完整闭环")
        _, text = c.tool("insp_status", {}, 10)
        check("MCP-2 status", json.loads(text)["cards_total"] == 1 and json.loads(text)["pending"] == 1, text[:200])
        _, text = c.tool("insp_list_cards", {"scope": "pending"}, 11)
        check("MCP-2 列出待评卡片", json.loads(text)["cards"][0]["id"] == "ins_mcp_1", text[:200])
        _, text = c.tool("insp_get_card", {"card_id": "ins_mcp_1"}, 12)
        check("MCP-2 卡片详情", "引号" in text)
        _, text = c.tool("insp_record_verdict", {"card_id": "ins_mcp_1", "action": "adopt", "reason": "mcp 测试"}, 13)
        check("MCP-2 写评价", json.loads(text)["written"] is True, text[:200])
        _, text = c.tool("insp_record_outcome", {"card_id": "ins_mcp_1", "outcome": "confirmed",
                                                  "conclusion": "成立", "measured": "3%"}, 14)
        check("MCP-2 写结果回填", json.loads(text)["written"] is True, text[:200])
        check("MCP-2 落盘", len((sd / "verdicts.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 1 and
              len((sd / "outcomes.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 1)
        _, text = c.tool("insp_outcome_summary", {}, 15)
        check("MCP-2 结果聚合", json.loads(text)["summary"]["total"] == 1)
        _, text = c.tool("insp_weekly_report", {"date": "2026-11-12"}, 16)
        check("MCP-2 周报", "两个核心指标" in text or "采纳率" in text, text[:200])
        _, text = c.tool("insp_verdict_summary", {}, 17)
        check("MCP-2 脱敏摘要", json.loads(text)["schema"] == "insp-verdict-summary/1.0")
        _, text = c.tool("insp_dryrun_script", {"card_id": "ins_mcp_1"}, 18)
        check("MCP-2 dry-run 脚本", "DRY-RUN" in text and "subprocess" not in text)
        _, text = c.tool("insp_dryrun_receipt", {"card_id": "ins_mcp_1"}, 19)
        check("MCP-2 dry-run 回执", "不能当证据" in text)
        _, text = c.tool("insp_list_jobs", {}, 20)
        check("MCP-2 任务列表可读", "jobs" in json.loads(text))
        _, text = c.tool("insp_sources_plan", {}, 21)
        check("MCP-2 来源开关默认关闭", json.loads(text)["plan"]["enabled"] is False)
        _, text = c.tool("insp_help", {}, 22)
        check("MCP-2 help", "insp_status" in text)

        print("== MCP-6 飞轮观测台")
        r, text = c.tool("insp_flywheel", {}, 30)
        fw = json.loads(text)
        check("MCP-6 schema 与诚实标注", r["isError"] is False and
              fw["schema"] == "insp-flywheel/1.0" and len(fw["flywheel"]["not_consumed"]) >= 3, text[:200])
        check("MCP-6 聚合到刚才的写操作", fw["signals"]["verdicts"]["explicit"] >= 1 and
              fw["signals"]["outcomes"]["total"] == 1 and fw["signals"]["weights"]["history_len"] == 0)
        before = {p.name: p.stat().st_mtime_ns for p in sd.rglob("*") if p.is_file()}
        c.tool("insp_flywheel", {}, 31)
        after = {p.name: p.stat().st_mtime_ns for p in sd.rglob("*") if p.is_file()}
        check("MCP-6 完全只读", before == after)

        print("== MCP-8 独立校准提议（③）")
        before = {p.name: p.stat().st_mtime_ns for p in sd.rglob("*") if p.is_file()}
        r, text = c.tool("insp_calibration", {}, 32)
        cal = json.loads(text)["calibration"]
        check("MCP-8 两套标签分开且样本不足不提议", r["isError"] is False and
              cal["n"] == 1 and cal["confirmed_rate"] == 1.0 and cal["adoption_rate"] == 1.0
              and cal["proposal"] is None, text[:200])
        check("MCP-8 校准不进 p̂ / 权重 / 排序", "p_hat" in cal["isolation"]["not_in"]
              and "weights" in cal["isolation"]["not_in"], text[:200])
        check("MCP-8 完全只读", before == {p.name: p.stat().st_mtime_ns for p in sd.rglob("*") if p.is_file()})

        print("== MCP-3 错误与隐私")
        r, text = c.tool("no_such_tool", {}, 23)
        check("MCP-3 未知工具 isError", r["isError"] is True)
        r, text = c.tool("insp_record_verdict", {"card_id": "ins_mcp_1", "action": "nope"}, 24)
        check("MCP-3 非法 action isError", r["isError"] is True and "ValueError" in text)
        r, text = c.tool("insp_get_card", {}, 25)
        check("MCP-3 缺参数 isError", r["isError"] is True)
        r, text = c.tool("insp_get_card", {"card_id": "../state.json"}, 26)
        check("MCP-3 路径穿越被拒", r["isError"] is True and "ValueError" in text)
        _, text = c.tool("insp_status", {}, 27)
        check("MCP-3 不含本机绝对路径", "C:\\" not in text and "/Users/" not in text and "/home/" not in text)
        err = c.close()
        check("MCP-3 日志只走 stderr", "ready" in err and "PASS" not in err)

        print("== MCP-4 read-only 隔离")
        ro = Client(sd, ["--read-only"])
        ro.rpc("initialize", {"protocolVersion": "2024-11-05"}, 1)
        names = {x["name"] for x in ro.rpc("tools/list", {}, 2)["result"]["tools"]}
        check("MCP-4 写工具被隐藏", not ({"insp_record_verdict", "insp_record_outcome", "insp_forget"} & names),
              str(sorted(names)))
        check("MCP-4 清除画像也被拒绝", ro.tool("insp_forget", {}, 6)[0]["isError"] is True)
        r, text = ro.rpc("tools/call", {"name": "insp_record_verdict",
                                        "arguments": {"card_id": "ins_mcp_1", "action": "adopt"}}, 3)["result"], ""
        check("MCP-4 写调用被拒绝", r["isError"] is True)
        _, text = ro.tool("insp_status", {}, 4)
        check("MCP-4 只读工具仍可用", json.loads(text)["read_only"] is True)
        check("MCP-4 拒绝写入摘要", ro.tool("insp_verdict_summary", {"write": True}, 5)[0]["isError"] is True)
        ro.close()

        print("== MCP-5 演示状态开箱可测")
        sys.path.insert(0, str(ROOT / "mcp"))
        import seed_demo
        demo = tmp / "demo_state"
        seed_demo.seed(demo)
        cards_file = next((demo / "cards").glob("*/cards.json"))
        check("MCP-5 生成 3 张演示卡", len(json.loads(cards_file.read_text(encoding="utf-8"))["cards"]) == 3)
        d = Client(demo)
        d.rpc("initialize", {"protocolVersion": "2024-11-05"}, 1)
        r, text = d.tool("insp_list_cards", {"scope": "all"}, 2)
        check("MCP-5 能列出演示卡", r["isError"] is False and json.loads(text)["total"] == 3, text[:200])
        r, text = d.tool("insp_record_verdict", {"card_id": "ins_demo_1", "action": "adopt"}, 3)
        check("MCP-5 演示卡可评价", r["isError"] is False, text[:200])
        r, text = d.tool("insp_record_outcome", {"card_id": "ins_demo_1", "outcome": "unknown"}, 4)
        check("MCP-5 演示卡可回填", r["isError"] is False, text[:200])
        r, text = d.tool("insp_weekly_report", {}, 5)
        check("MCP-5 演示数据能出周报", r["isError"] is False, text[:200])
        d.close()

        print("== MCP-7 使用画像（默认关闭）")
        log_file = sd / "mcp_usage.jsonl"
        u0 = Client(sd)
        u0.rpc("initialize", {"protocolVersion": "2024-11-05"}, 1)
        r, text = u0.tool("insp_usage", {}, 40)
        check("MCP-7 默认关闭且不写文件", json.loads(text)["enabled"] is False and not log_file.exists())
        u0.close()

        u = Client(sd, ["--usage"])
        u.rpc("initialize", {"protocolVersion": "2024-11-05"}, 1)
        u.tool("insp_record_verdict", {"card_id": "ins_mcp_1", "action": "adopt", "reason": "绝密原文"}, 41)
        log_text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        check("MCP-7 只记类别不记原文", '"action": "adopt"' in log_text and "绝密原文" not in log_text
              and "reason" not in log_text, log_text[:200])
        r, text = u.tool("insp_help", {}, 42)
        check("MCP-7 个性化引导", "your_pattern" in text)
        r, text = u.tool("insp_status", {}, 43)
        check("MCP-7 状态带上次操作", json.loads(text)["last_tool_call"] is not None)
        r, text = u.tool("insp_usage", {}, 44)
        check("MCP-7 统计", json.loads(text)["enabled"] is True and json.loads(text)["total"] >= 1)
        r, text = u.tool("insp_forget", {}, 45)
        check("MCP-7 一键清除", json.loads(text)["forgotten"]["removed"] is True and not log_file.exists())
        u.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("MCP OK" if OK else "MCP TESTS FAILED")
    return 0 if OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
