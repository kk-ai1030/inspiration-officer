#!/usr/bin/env python3
"""Exercise the ONLINE code path of run_local.py against a local mock of the SciencePal API.

The mock's response shapes are ASSUMPTIONS (the OpenAPI leaves them untyped). This test proves the
orchestrator's plumbing (multipart upload, SSE parsing, nested-JSON extraction, skills evidence check,
loaded=false rejection); it does not prove the real platform answers in exactly this shape —
that is what platform/probe.py is for.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
REPLY = (PKG / "tests/fixtures/fake_reply_ok.txt").read_text(encoding="utf-8")
STATE = {"loaded": True, "uploads": [], "key_forbidden": False, "tool_event": False, "text_claims": False}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        if self.headers.get("Authorization") != "Bearer TESTTOKEN":
            return self._json({"detail": "unauthorized"}, 401)
        if self.path == "/api/agent/initiate":
            STATE["uploads"] = [x for x in (b"state.json", b"negative.jsonl", b"rubric.md", b"triage.json") if b'filename="' + x in body]
            STATE["ft_uploads"] = sorted(x.decode() for x in __import__("re").findall(rb'filename="((?:ft|rel)_[^"]+|fulltext_index\.json)"', body))
            STATE["prompt_mode"] = ("fulltext" if "全文模式".encode() in body else "abstract" if "摘要模式".encode() in body else None)
            return self._json({"thread_id": "th-1", "agent_run_id": "run-1", "sandbox_id": "sb-1"})
        self._json({"detail": "not found"}, 404)

    def do_GET(self):
        if self.path.startswith("/api/agent-run/run-1/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            msg = {"type": "assistant", "content": json.dumps({"role": "assistant", "content": REPLY})}
            evs = [{"type": "status", "status": "running"}]
            if STATE["text_claims"]:  # the model merely SAYING it loaded the skill must not count
                evs.append({"type": "assistant", "content": "我已调用 load_skill 加载 insp-card"})
            if STATE["tool_event"]:  # ASSUMED shape of a platform tool event
                evs.append({"type": "tool", "name": "load_skill", "arguments": {"skill_name": "insp-card"}, "exit_code": 0})
            evs += [msg, {"type": "status", "status": "completed"}]
            for ev in evs:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            return
        if self.path == "/api/agent-run/run-1":
            return self._json({"id": "run-1", "status": "completed"})
        if self.path == "/api/agent-run/run-1/skills":
            # real platform: an API key gets 403 here, only a session token may read skill lifecycle fields
            if STATE["key_forbidden"] and self.headers.get("Authorization") != "Bearer SETUPTOKEN":
                return self._json({"detail": "forbidden"}, 403)
            return self._json({"skills": [{"name": "insp-nightly", "bound": True, "loaded": True, "decision": "selected"},
                                          {"name": "insp-card", "bound": True, "loaded": STATE["loaded"],
                                           "decision": "selected" if STATE["loaded"] else "not_recorded"}]})
        self._json({"detail": "not found"}, 404)


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", t / "inspiration")
        (t / "inspiration" / ".test_state").write_text("", encoding="utf-8")  # fixtures allowed here (BUG-6 guard)
        st = json.loads((t / "inspiration/state.json").read_text(encoding="utf-8"))
        st["bootstrap"] = False
        st["seen_ids"] = json.loads((PKG / "tests/fixtures/seen_ids.json").read_text())["seen_ids"]
        (t / "inspiration/state.json").write_text(json.dumps(st), encoding="utf-8")
        cfg = json.loads((PKG / "platform/config.example.json").read_text(encoding="utf-8"))
        cfg["review"]["enabled"] = False  # no toast / review server during tests
        cfg["fulltext"]["cache_dir"] = str(PKG / "tests/fixtures/fulltext_cache")  # offline full texts
        cfg["arxiv_fetch_mode"] = "fulltext"  # M1–M8 exercise the full-text route; M9 covers the default (abstract)
        cfg.update(base_url=f"http://127.0.0.1:{port}", run_timeout_s=30, max_attempts=1,
                   token_file=str(t / "no_run_token"), setup_token_file=str(t / "no_session_token"))  # never read real tokens
        (t / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        (t / "platform_ids.json").write_text(json.dumps({"agent_id": "ag-1", "skills": {}}), encoding="utf-8")
        env = dict(os.environ, SCIENCEPAL_TOKEN="TESTTOKEN")
        cmd = [sys.executable, str(PKG / "platform/run_local.py"), "--config", str(t / "config.json"),
               "--raw", str(PKG / "tests/fixtures/raw_items.json"), "--no-arxiv-check"]
        STATE["loaded"] = False
        r0 = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if r0.returncode != 5 or "loaded=false" not in r0.stdout:
            print("FAIL M1: loaded=false 应被拒绝\n", r0.stdout, r0.stderr)
            return 1
        print("PASS  M1 平台字段 loaded=false → 整批拒绝")
        STATE["loaded"] = True
        r1 = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if r1.returncode != 0 or "OK: 2 张卡片" not in r1.stdout:
            print("FAIL M2\n", r1.stdout, r1.stderr)
            return 1
        if len(STATE["uploads"]) != 4:
            print("FAIL M2: 附件未全部上传", STATE["uploads"])
            return 1
        if STATE.get("ft_uploads") != ["ft_9999.20001.md", "ft_9999.20002.md", "fulltext_index.json", "rel_9999.30001.md", "rel_9999.30002.md"]:
            print("FAIL M2b: 原文附件未上传", STATE.get("ft_uploads"))
            return 1
        print("PASS  M2b 原文附件（2 篇候选全文 + 2 篇相关文献全文 + 索引）随任务上传")
        print("PASS  M2 multipart 附件上传 + SSE 嵌套 JSON 解析 + 技能字段校验 → 2 张卡片")

        def reset_state() -> None:
            shutil.rmtree(t / "inspiration")
            shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", t / "inspiration")
            (t / "inspiration" / ".test_state").write_text("", encoding="utf-8")  # fixtures allowed here (BUG-6 guard)
            (t / "inspiration/state.json").write_text(json.dumps(st), encoding="utf-8")

        STATE["key_forbidden"] = True
        reset_state()
        env3 = {k: v for k, v in env.items() if k != "SCIENCEPAL_SETUP_TOKEN"}
        r3 = subprocess.run(cmd, env=env3, capture_output=True, text=True)
        log = [json.loads(l) for l in (t / "inspiration/runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        if r3.returncode != 0 or log[-1].get("skill_loaded") != "unverified":
            print("FAIL M3: 只有 API Key 时应以 flow_version 为门槛放行，并标记 unverified\n", r3.stdout, r3.stderr, log[-1])
            return 1
        cards = [c for f in (t / "inspiration/cards").glob("*/cards.json")
                 for c in json.loads(f.read_text(encoding="utf-8"))["cards"]]
        if log[-1].get("memory_canary") != "1/2" or any("◆" in c["title"] for c in cards) \
                or [c.get("memory_canary") for c in cards] != [True, False]:
            print("FAIL M3b: 金丝雀应计数并从标题中去掉", log[-1].get("memory_canary"), [c["title"] for c in cards])
            return 1
        print("PASS  M3b 记忆金丝雀 ◆ 已计数（1/2）并从卡片标题中去掉")
        print("PASS  M3 只有 API Key（技能字段 403）→ flow_version 兜底放行，标记 skill_loaded=unverified")
        audit = subprocess.run([sys.executable, str(PKG / "platform/run_audit.py"), "--config", str(t / "config.json")],
                               env=dict(env3, SCIENCEPAL_SETUP_TOKEN="SETUPTOKEN"), capture_output=True, text=True)
        rows = [json.loads(l) for l in (t / "inspiration/audit.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        if audit.returncode != 0 or rows[-1]["skill_loaded"] != "true":
            print("FAIL M4: run_audit 应用会话令牌回填 loaded=true\n", audit.stdout, audit.stderr)
            return 1
        print("PASS  M4 run_audit 用会话令牌回填 → loaded=true")
        reset_state()
        r5 = subprocess.run(cmd, env=dict(env3, SCIENCEPAL_SETUP_TOKEN="SETUPTOKEN"), capture_output=True, text=True)
        log = [json.loads(l) for l in (t / "inspiration/runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        if r5.returncode != 0 or log[-1].get("skill_loaded") != "true":
            print("FAIL M5: 同时有两种令牌时应直接查到 loaded=true\n", r5.stdout, r5.stderr)
            return 1
        print("PASS  M5 API Key 运行 + 会话令牌查技能字段 → loaded=true")

        # M6: API key only, but the stream carries a load_skill tool event -> true(stream); text claims don't count
        STATE["text_claims"] = True
        reset_state()
        r6a = subprocess.run(cmd, env=env3, capture_output=True, text=True)
        log = [json.loads(l) for l in (t / "inspiration/runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        if log[-1].get("skill_loaded") != "unverified":
            print("FAIL M6a: 模型文字里说加载了技能不应算证据", log[-1].get("skill_loaded"))
            return 1
        STATE["tool_event"] = True
        reset_state()
        r6 = subprocess.run(cmd, env=env3, capture_output=True, text=True)
        log = [json.loads(l) for l in (t / "inspiration/runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        if r6.returncode != 0 or log[-1].get("skill_loaded") != "true(stream)":
            print("FAIL M6: 事件流里有 load_skill(insp-card) 工具事件时应记 true(stream)\n", r6.stdout, r6.stderr, log[-1])
            return 1
        print("PASS  M6 只有 API Key：事件流里的 load_skill 工具事件 → skill_loaded=true(stream)；模型文字自述不算")

        # M7: require_skill_loaded="evidence" rejects a run that has neither field nor stream evidence
        STATE["tool_event"] = STATE["text_claims"] = False
        cfg7 = dict(cfg, require_skill_loaded="evidence")
        (t / "config.json").write_text(json.dumps(cfg7), encoding="utf-8")
        reset_state()
        r7 = subprocess.run(cmd, env=env3, capture_output=True, text=True)
        if r7.returncode != 5 or "loaded=unverified" not in r7.stdout:
            print("FAIL M7: evidence 模式下没有证据应拒绝\n", r7.stdout, r7.stderr)
            return 1
        (t / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        print("PASS  M7 require_skill_loaded=evidence：无平台证据 → 整批拒绝")

        # M8: state.json restored to an old copy while the card archive keeps the pushed cards -> heal, no re-push
        reset_state()
        r8a = subprocess.run(cmd, env=env3, capture_output=True, text=True)
        first = [c["source"]["ref"] for f in (t / "inspiration/cards").glob("*/cards.json")
                 for c in json.loads(f.read_text(encoding="utf-8"))["cards"]]
        (t / "inspiration/state.json").write_text(json.dumps(st), encoding="utf-8")  # "restore" the pre-run state
        r8 = subprocess.run(cmd, env=env3, capture_output=True, text=True)
        allrefs = [c["source"]["ref"] for f in (t / "inspiration/cards").glob("*/cards.json")
                   for c in json.loads(f.read_text(encoding="utf-8"))["cards"]]
        log = [json.loads(l) for l in (t / "inspiration/runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        healed = [r for r in log if r.get("state_healed")]
        if not first or len(allrefs) != len(set(allrefs)) or not healed or "落后于卡片库" not in r8.stdout:
            print("FAIL M8: 状态被还原后不应重复推送同一篇论文\n", first, allrefs, r8.stdout[-800:], r8.stderr[-800:])
            return 1
        print(f"PASS  M8 state.json 被还原 → 用卡片库补回去重清单（补 {len(healed[-1]['state_healed'])} 篇），同一论文未被重复推送")
        # M9: default config (no arxiv_fetch_mode at all) -> abstract: no full text fetched or uploaded, abstract prompt
        reset_state()
        cfg9 = dict(cfg)
        cfg9.pop("arxiv_fetch_mode", None)
        (t / "config.json").write_text(json.dumps(cfg9), encoding="utf-8")
        STATE.update(loaded=True, key_forbidden=False, tool_event=False, text_claims=False, ft_uploads=None, prompt_mode=None)
        r9 = subprocess.run(cmd, env=env, capture_output=True, text=True)
        cards9 = [c for f in sorted((t / "inspiration/cards").glob("*/cards.json"))[-1:]
                  for c in json.loads(f.read_text(encoding="utf-8"))["cards"]]
        if r9.returncode != 0 or STATE.get("ft_uploads") != [] or STATE.get("prompt_mode") != "abstract" \
                or "FETCH_MODE abstract" not in r9.stdout or not cards9 \
                or not all(c["source"].get("read_mode") == "abstract" and c["source"].get("url") for c in cards9):
            print("FAIL M9: 默认应为摘要模式（不抓不传原文）\n", r9.stdout[-1500:], r9.stderr[-800:], STATE.get("ft_uploads"), STATE.get("prompt_mode"))
            return 1
        print("PASS  M9 不写 arxiv_fetch_mode → 摘要模式：不抓、不上传原文，指令是摘要版，卡片带摘要页链接")
    srv.shutdown()
    print("ALL MOCK PLATFORM TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
