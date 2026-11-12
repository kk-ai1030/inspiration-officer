#!/usr/bin/env python3
"""setup_platform.py against a local mock SciencePal (no network, no real account).

    python tests/mock_setup_test.py
S1 安装：导入 7 个技能（zip）→ 建灵感官 → 绑定 7 个 → 安装后检查通过
S2 升级：已有 agent_id → 更新而不是重建；--replace 透传
S3 清理旧定时任务：列出只认灵感相关的任务，--yes 才删除，无关任务不动
S4 账号不一致（BUG-3）：API Key 看不到灵感官 → 安装检查失败并给出原因
S5 zip 导入失败 → 退回文本方式；insp-nightly / insp-profile 文本方式 → 安装检查判失败
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
PY = sys.executable


class Mock:
    def __init__(self):
        self.reset()

    def reset(self, visible_agents_for_run=None, zip_fail=False, keep_skills=False):
        old = self.skills if keep_skills and hasattr(self, "skills") else {}
        self.skills, self.agents, self.bound, self.calls = dict(old), {}, {}, []
        self.missing_file_for: set[str] = set()
        self.tasks = {"t1": {"id": "t1", "name": "inspiration-nightly-test", "cron_expression": "40 2 * * *"},
                      "t2": {"id": "t2", "name": "组会提醒", "prompt": "每周提醒我组会"},
                      "t3": {"id": "t3", "name": "daily", "prompt": "执行 skill: insp-nightly"}}
        self.visible = visible_agents_for_run
        self.zip_fail = zip_fail


M = Mock()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_POST(self):
        body = self._body()
        M.calls.append(("POST", self.path, self.headers.get("Authorization")))
        if self.path == "/api/skills/import":
            if M.zip_fail:
                return self._send(415, {"error": "zip not supported"})
            name = re.search(rb'name="name"\r\n\r\n([^\r]+)', body).group(1).decode()
            if name in M.missing_file_for:  # feedback6 H1: platform's new file-reference check
                return self._send(400, {"detail": {"code": "missing_file",
                                                   "guidance": "The instructions reference 'scripts/find_quotes.py', but it is missing."}})
            replace = b'name="replace_existing"\r\n\r\ntrue' in body
            sid = f"sk_{name}"
            M.skills[sid] = {"name": name, "mode": "zip", "replace": replace}
            return self._send(200, {"skill": {"skill_id": sid}})
        if self.path == "/api/skills/items":
            j = json.loads(body)
            if any(v["name"] == j["name"] for v in M.skills.values()):
                return self._send(409, {"detail": "user_skill_name_conflict"})
            sid = f"sk_{j['name']}_text"
            M.skills[sid] = {"name": j["name"], "mode": "text"}
            return self._send(200, {"id": sid})
        if self.path == "/api/agents":
            aid = f"ag_{len(M.agents) + 1}"
            M.agents[aid] = json.loads(body)
            return self._send(200, {"agent_id": aid})
        self._send(404, {"error": self.path})

    def do_PUT(self):
        body = json.loads(self._body())
        M.calls.append(("PUT", self.path, self.headers.get("Authorization")))
        m = re.match(r"/api/agents/([^/]+)/skills$", self.path)
        if m:
            M.bound[m.group(1)] = body["skill_ids"]
            return self._send(200, {"ok": True})
        m = re.match(r"/api/agents/([^/]+)$", self.path)
        if m:
            M.agents.setdefault(m.group(1), {}).update(body)
            return self._send(200, {"ok": True})
        self._send(404, {})

    def do_GET(self):
        auth = self.headers.get("Authorization") or ""
        M.calls.append(("GET", self.path, auth))
        m = re.match(r"/api/agents/([^/]+)/skills$", self.path)
        if m:
            return self._send(200, {"skills": [{"skill_id": s} for s in M.bound.get(m.group(1), [])]})
        if self.path == "/api/agents":
            ids = list(M.agents)
            if "run-key" in auth and M.visible is not None:
                ids = M.visible
            return self._send(200, {"agents": [{"agent_id": a} for a in ids]})
        if self.path == "/api/scheduled-tasks":
            return self._send(200, {"tasks": list(M.tasks.values())})
        self._send(404, {})

    def do_DELETE(self):
        M.calls.append(("DELETE", self.path, self.headers.get("Authorization")))
        m = re.match(r"/api/scheduled-tasks/([^/]+)$", self.path)
        if m and m.group(1) in M.tasks:
            del M.tasks[m.group(1)]
            return self._send(200, {"ok": True})
        self._send(404, {})


def check(name, cond, log=""):
    if not cond:
        print(log[-2500:])
        raise SystemExit(f"FAIL  {name}")
    print(f"PASS  {name}")


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / "session").write_text("session-key", encoding="utf-8")
        (t / "run").write_text("run-key", encoding="utf-8")
        cfg = json.loads((PKG / "platform/config.example.json").read_text(encoding="utf-8"))
        cfg.update(base_url=base, setup_token_file=str(t / "session"), token_file=str(t / "run"),
                   platform_ids_file="./platform_ids.json")
        (t / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("SCIENCEPAL_")}
        env["PYTHONUTF8"] = "1"

        def setup(*extra):
            r = subprocess.run([PY, "-X", "utf8", str(PKG / "platform/setup_platform.py"), "--config", "config.json", *extra],
                               cwd=t, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            return r.returncode, r.stdout + r.stderr

        M.reset()
        code, log = setup()
        ids = json.loads((t / "platform_ids.json").read_text(encoding="utf-8"))
        check("S1 安装 7 个技能 + 绑定 + 检查通过", code == 0 and len(ids["skills"]) == 7
              and all(v["mode"] == "zip" for v in ids["skills"].values())
              and len(M.bound[ids["agent_id"]]) == 7 and "安装检查通过" in log
              and any(c[0] == "POST" and c[1] == "/api/skills/import" and "session-key" in c[2] for c in M.calls), log)

        code, log = setup("--replace")
        check("S2 升级：更新而不是重建，--replace 透传", code == 0 and "agent updated" in log and len(M.agents) == 1
              and all(s["replace"] for s in M.skills.values() if s["mode"] == "zip"), log)

        code, log = setup("--cleanup-schedules")
        check("S3a 只列出、不删除", code == 0 and len(M.tasks) == 3 and "t1" in log and "t3" in log and "组会提醒" not in log, log)
        code, log = setup("--cleanup-schedules", "--yes")
        check("S3b --yes 删除灵感任务，无关任务保留", code == 0 and set(M.tasks) == {"t2"}, log)

        M.visible = ["ag_other"]
        code, log = setup("--check")
        check("S4 API Key 看不到灵感官 → 检查失败并说明", code == 1 and "不在同一账号" in log, log)

        check("S1b 安装前做文件引用检查（平台 missing_file 的本地版）", "文件引用检查通过" in log, log)
        print("== S6 复现 feedback6 H1：zip 400 missing_file → 文本回退 409 → 不崩，保留旧版本，其余照常更新")
        M.reset(keep_skills=True)
        M.agents = {ids["agent_id"]: {"name": "x"}}
        M.missing_file_for = {"insp-nightly"}
        code, log = setup("--replace")
        ids6 = json.loads((t / "platform_ids.json").read_text(encoding="utf-8"))
        check("S6", code == 1 and "Traceback" not in log and "missing_file" in log and "skill_refs.py" in log
              and "平台上仍是旧版本" in log and ids6["skills"]["insp-nightly"].get("stale")
              and ids6["skills"]["insp-nightly"]["skill_id"] == ids["skills"]["insp-nightly"]["skill_id"]
              and all(ids6["skills"][s]["mode"] == "zip" and not ids6["skills"][s].get("stale") for s in ids6["skills"] if s != "insp-nightly")
              and "agent updated" in log and "1 个技能没有更新成功" in log, log)
        M.missing_file_for = set()
        code, log = setup("--replace")
        ids7 = json.loads((t / "platform_ids.json").read_text(encoding="utf-8"))
        check("S6b 修好后重跑 --replace → 恢复正常，stale 标记清除", code == 0 and not ids7["skills"]["insp-nightly"].get("stale"), log)

        M.reset(zip_fail=True)
        (t / "platform_ids.json").unlink()
        code, log = setup()
        check("S5 zip 失败 → 文本方式；关键技能文本方式 → 检查失败", code == 1 and "改用文本方式" in log
              and "insp-nightly" in log and "沙箱里没有脚本" in log, log)
    srv.shutdown()
    print("ALL MOCK SETUP TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
