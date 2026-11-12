#!/usr/bin/env python3
"""v1.4.2 · receiver against a mock that enforces the REAL credential split seen in feedback4.

Real platform (2026-10-29): an API key (sp_live_…) gets
    403 {"detail":"API keys are limited to SDK catalogs, agent, run and sandbox endpoints"}
on /api/thread/*, /api/dashboard/*, /api/scheduled-tasks and /api/memory; only the session token may call them.
/api/agent-run/* , /api/sandboxes/* , /api/agents and /api/agent/initiate work with the key.

R1  API key only, file 403 → result block from the replayed run stream (v1.4.2 bundle carries triage_min) → cards in
R1b of several blocks (skill text quoting the markers, model copy with triage_min removed) the verified one is used
R2  feedback4 replay: v1.4.1 bundle (no triage_min) + API key only → VALIDATION_FAILED, retryable, NOT marked
    collected; add the session token → the same --thread collects it from the file (thread-files/session)
R3  session token: state pushed back into the sandbox (G5) and "[灵感状态]" memory written (G6 precondition)
R4  triage_min altered in the copy → VALIDATION_FAILED, nothing ingested
R5  --once with a pending button run, API key only → collected from the stream; status says pinned_only + session hint
R6  two "run" clicks inside 10 min → second refused; --force starts it
R7  setup_platform --check / --cleanup-schedules: API key only → clear "需要会话令牌"; session → PASS (deleted ignored)
R8  receiver never crashes on 403 (feedback4 BUG-3): no traceback in any log above
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_offline_e2e import FIX, PKG, abstract_only_cards, check, fresh_state, jload, jsave, make_config, run  # noqa: E402

KEY, SESSION = "sp_live_TESTKEY", "SESSIONTOKEN"
DENY = {"detail": "API keys are limited to SDK catalogs, agent, run and sandbox endpoints"}
TID, RID, SID, AID = "11111111-2222-4333-8444-555555555555", "run-42", "sb-42", "agent-insp"
W = {"out": None, "home": None, "stream_text": "", "writes": {}, "memory": [], "initiated": 0,
     "tasks": [{"id": "t-old", "name": "inspiration-nightly-test", "status": "deleted", "prompt": "跑灵感"}]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, raw: bytes | None = None):
        b = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _who(self):
        a = self.headers.get("Authorization", "")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = a.split(" ", 1)[1] if " " in a else (q.get("token") or [""])[0]
        return {KEY: "key", SESSION: "session"}.get(tok)

    def _gate(self, path: str):
        who = self._who()
        if who is None:
            self._send({"detail": "unauthorized"}, 401)
            return None
        key_ok = path.startswith(("/api/agent-run/", "/api/sandboxes/", "/api/agents", "/api/agent/initiate"))
        if who == "key" and not key_ok:
            self._send(DENY, 403)
            return None
        return who

    def _file(self, p: str) -> Path | None:
        for prefix, d in (("/workspace/inspiration_out/", W["out"]), ("/workspace/inspiration_state/", W["home"])):
            if p.startswith(prefix) and d is not None:
                f = d / p[len(prefix):]
                return f if f.is_file() else None
        return None

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        if self._gate(path) is None:
            return
        if path == f"/api/thread/{TID}/files/content" or path == f"/api/sandboxes/{SID}/files/content":
            f = self._file(q.get("path", [""])[0])
            return self._send(None, 200, f.read_bytes()) if f else self._send({"detail": "not found"}, 404)
        if path == f"/api/thread/{TID}/sandbox":
            return self._send({"sandbox_id": SID})
        if path == f"/api/thread/{TID}/agent-runs":
            return self._send({"agent_runs": [{"id": RID}]})
        if path == f"/api/agent-run/{RID}":
            return self._send({"id": RID, "status": "completed"})  # no reply text here → forces the stream replay
        if path == f"/api/agent-run/{RID}/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for ev in ({"type": "status", "status": "running"},
                       {"type": "assistant", "content": W["stream_text"]},
                       {"type": "status", "status": "completed"}):
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            return
        if path == "/api/dashboard/personal-conversations":
            return self._send({"threads": []})
        if path == "/api/agents":
            return self._send({"agents": [{"agent_id": AID, "name": "灵感官"}]})
        if path == f"/api/agents/{AID}/skills":
            return self._send({"skills": []})
        if path == "/api/memory":
            return self._send({"memories": W["memory"]})
        if path == "/api/scheduled-tasks":
            return self._send({"tasks": W["tasks"]})
        self._send({"detail": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        if self._gate(path) is None:
            return
        if path == f"/api/sandboxes/{SID}/files":
            m = re.search(rb'name="path"\r\n\r\n([^\r]+)', body)
            W["writes"][m.group(1).decode() if m else "?"] = len(body)
            return self._send({"ok": True})
        if path == "/api/memory":
            j = json.loads(body.decode())
            W["memory"].append({"id": f"m{len(W['memory'])}", **j})
            return self._send({"ok": True})
        if path == "/api/agent/initiate":
            W["initiated"] += 1
            return self._send({"thread_id": TID, "agent_run_id": RID, "sandbox_id": SID})
        self._send({"detail": "not found"}, 404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if self._gate(path) is None:
            return
        if path.startswith("/api/memory/"):
            W["memory"] = [m for m in W["memory"] if m.get("id") != path.rsplit("/", 1)[1]]
            return self._send({"ok": True})
        self._send({"detail": "not found"}, 404)


def make_bundle(t: Path, legacy: bool = False) -> tuple[Path, Path, str]:
    """Abstract-mode sandbox run (prepare → cards → finalize); returns out dir, state home, finalize stdout."""
    sd = t / "inspiration"
    sbx = t / "sandbox"
    home, out, refs = sbx / "inspiration_state", sbx / "inspiration_out", sbx / "_refs"
    home.mkdir(parents=True)
    for p in sd.rglob("*"):
        if p.is_file():
            (home / p.relative_to(sd)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, home / p.relative_to(sd))
    runner = PKG / "insp-nightly/scripts/insp_run.py"
    sb = ["--out", out, "--state-home", home, "--refs", refs]
    run([runner, "prepare", *sb, "--raw", FIX / "raw_items.json"], t)
    jsave(out / "cards.json", abstract_only_cards(FIX / "fake_reply_ok.txt"))
    _, log = run([runner, "finalize", *sb], t)
    if legacy:  # what a v1.4.1 sandbox produced: no triage_min in the bundle / block
        b = jload(out / "bundle.json")
        for k in ("triage_min", "triage_min_sha256"):
            b.pop(k, None)
        b["runner_version"] = "insp-run/1.4.1"
        jsave(out / "bundle.json", b)
        log = "<<<INSP_BUNDLE_JSON\n" + json.dumps(b, ensure_ascii=False) + "\nINSP_BUNDLE_JSON>>>"
    return out, home, log


def setup(t: Path, base: str, session: bool, push: bool = False) -> Path:
    sd = fresh_state(t)
    cfgp = make_config(t)
    cfg = jload(cfgp)
    cfg["base_url"] = base
    (t / "run_token").write_text(KEY, encoding="utf-8")
    cfg["token_file"] = str(t / "run_token")
    cfg["setup_token_file"] = str(t / "session_token")
    if session:
        (t / "session_token").write_text(SESSION, encoding="utf-8")
    cfg["receiver"].update(push_state=push, memory_digest=push, auto_fallback_local=False, reply_stream_timeout_s=10)
    jsave(cfgp, cfg)
    jsave(t / "platform_ids.json", {"agent_id": AID, "skills": {}})
    return sd


def rows(sd: Path) -> list[dict]:
    p = sd / "runs.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []


def ncards(sd: Path) -> int:
    return sum(len(jload(p)["cards"]) for p in sd.glob("cards/*/cards.json"))


def main() -> int:  # noqa: C901
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    rcv = PKG / "platform/receiver.py"
    tmp = Path(tempfile.mkdtemp(prefix="insp_rcv_"))
    logs: list[str] = []
    try:
        # ---------------------------------------------------------------- R1 / R1b
        t = tmp / "r1"
        t.mkdir()
        sd = setup(t, base, session=False)
        out, home, fin = make_bundle(t)
        W.update(out=out, home=home)
        good = fin[fin.index("<<<INSP_BUNDLE_JSON"):]
        model_copy = json.loads(good.split("\n", 1)[1].rsplit("\n", 1)[0].split("INSP_BUNDLE_JSON>>>")[0])
        model_copy.pop("triage_min", None)  # a "shortened" copy in the final answer
        skill_quote = "最终回答末尾放 `<<<INSP_BUNDLE_JSON ... INSP_BUNDLE_JSON>>>` 原样"
        W["stream_text"] = (skill_quote + "\n工具输出：\n" + good + "\n\n最终回答：今天出卡 2 张。\n<<<INSP_BUNDLE_JSON\n"
                            + json.dumps(model_copy, ensure_ascii=False) + "\nINSP_BUNDLE_JSON>>>")
        print("== R1 只有 API Key：会话文件 403 → 从运行事件流取结果块 → 入库")
        code, log = run([rcv, "--config", "config.json", "--thread", f"{base}/thread/{TID}", "--run-id", RID, "--no-arxiv-check"], t, None)
        logs.append(log)
        r = rows(sd)[-1] if rows(sd) else {}
        check("R1 API Key 也能收货（bundle_from=reply, triage_from=bundle）",
              code == 0 and r.get("status") == "OK" and r.get("bundle_from") == "reply" and r.get("triage_from") == "bundle"
              and "403" in str(r.get("file_denied")) and ncards(sd) == 2 and "仅" not in log[:0], log)
        check("R1b 多个结果块里选了校验通过的那份（triage_min sha 一致）", "triage_min" in jload(sorted(t.glob("runs/rcv-*/bundle.json"))[-1]), log)
        code, log = run([rcv, "--config", "config.json", "--thread", TID, "--run-id", RID, "--no-arxiv-check"], t, None)
        check("R1c 再收一次 → ALREADY（幂等）", '"ALREADY"' in log and ncards(sd) == 2, log)

        # ---------------------------------------------------------------- R2 feedback4 replay
        t = tmp / "r2"
        t.mkdir()
        sd = setup(t, base, session=False)
        out, home, fin = make_bundle(t, legacy=True)
        W.update(out=out, home=home, stream_text=fin)
        print("== R2 复现 feedback4：v1.4.1 整包 + 只有 API Key → 可重试的失败；放入会话令牌后同一命令收进来")
        code, log = run([rcv, "--config", "config.json", "--thread", TID, "--run-id", RID, "--no-arxiv-check"], t, None)
        logs.append(log)
        r = rows(sd)[-1] if rows(sd) else {}
        collected = (sd / "inbox/collected.jsonl").read_text(encoding="utf-8") if (sd / "inbox/collected.jsonl").is_file() else ""
        check("R2a 没有 traceback；VALIDATION_FAILED 且 retryable；未标记已收", code == 5 and "Traceback" not in log
              and r.get("status") == "VALIDATION_FAILED" and r.get("retryable") and "会话令牌" in r.get("problem", "")
              and TID not in collected and ncards(sd) == 0, log)
        (t / "session_token").write_text(SESSION, encoding="utf-8")
        code, log = run([rcv, "--config", "config.json", "--thread", TID, "--no-arxiv-check"], t, None)
        logs.append(log)
        r = rows(sd)[-1]
        check("R2b 有会话令牌 → 读文件收货（file_via=thread-files/session，triage_from=file）",
              code == 0 and r.get("status") == "OK" and r.get("bundle_from") == "file" and r.get("triage_from") == "file"
              and r.get("file_via") == "thread-files/session" and ncards(sd) == 2 and "会话令牌 + API Key" in log, log)

        # ---------------------------------------------------------------- R3 push state + memory
        t = tmp / "r3"
        t.mkdir()
        sd = setup(t, base, session=True, push=True)
        out, home, fin = make_bundle(t)
        W.update(out=out, home=home, stream_text=fin, writes={}, memory=[])
        print("== R3 有会话令牌：状态写回沙箱（G5）+ 写 [灵感状态] 记忆")
        code, log = run([rcv, "--config", "config.json", "--thread", TID, "--no-arxiv-check"], t, None)
        logs.append(log)
        r = rows(sd)[-1]
        check("R3 写回 state.json/negative.jsonl 到 /workspace/inspiration_state；记忆 1 条",
              code == 0 and r.get("status") == "OK" and "/workspace/inspiration_state/state.json" in W["writes"]
              and "/workspace/inspiration_state/negative.jsonl" in W["writes"]
              and sum(str(m.get("content", "")).startswith("[灵感状态]") for m in W["memory"]) == 1, log + json.dumps(W["writes"]))

        # ---------------------------------------------------------------- R4 tampered copy
        t = tmp / "r4"
        t.mkdir()
        sd = setup(t, base, session=False)
        out, home, fin = make_bundle(t)
        b = json.loads(fin[fin.index("<<<INSP_BUNDLE_JSON"):].split("\n", 1)[1].split("\nINSP_BUNDLE_JSON>>>")[0])
        b["triage_min"]["counts"]["new"] = 99
        W.update(out=out, home=home, stream_text="<<<INSP_BUNDLE_JSON\n" + json.dumps(b, ensure_ascii=False) + "\nINSP_BUNDLE_JSON>>>")
        print("== R4 结果块被改动（triage_min 与 sha 不符）→ 拒收")
        code, log = run([rcv, "--config", "config.json", "--thread", TID, "--run-id", RID, "--no-arxiv-check"], t, None)
        logs.append(log)
        check("R4", code == 5 and rows(sd)[-1].get("status") == "VALIDATION_FAILED" and ncards(sd) == 0, log)

        # ---------------------------------------------------------------- R5 poll with a pending button run
        t = tmp / "r5"
        t.mkdir()
        sd = setup(t, base, session=False)
        out, home, fin = make_bundle(t)
        W.update(out=out, home=home, stream_text=fin)
        (sd / "inbox").mkdir(exist_ok=True)
        jsave(sd / "inbox/pending_runs.json", [{"thread_id": TID, "agent_run_id": RID, "t": 1e10, "fetch_mode": "abstract"}])
        print("== R5 --once：按钮触发的运行（只有 API Key）→ 运行结束后从事件流收货；状态行提示缺会话令牌")
        code, log = run([rcv, "--config", "config.json", "--once", "--no-arxiv-check"], t, None)
        logs.append(log)
        st = jload(sd / "inbox/receiver_status.json")
        check("R5", ncards(sd) == 2 and st.get("discovery") == "pinned_only" and "会话令牌" in st.get("discovery_error", ""), log)
        lr = st.get("last_result") or {}
        check("R5b 入库后状态里记下结果时间（评价页据此提示“任务已中断不用点从断点继续”，feedback6）",
              lr.get("status") == "OK" and lr.get("cards") == 2 and str(lr.get("at", "")).startswith("20"), json.dumps(lr, ensure_ascii=False))

        # ---------------------------------------------------------------- R6 trigger guard
        t = tmp / "r6"
        t.mkdir()
        sd = setup(t, base, session=False)
        W["initiated"] = 0
        print("== R6 10 分钟内重复触发 → 第二次被拒；--force 可以")
        c1, l1 = run([rcv, "--config", "config.json", "--run"], t, None)
        c2, l2 = run([rcv, "--config", "config.json", "--run"], t, None)
        c3, l3 = run([rcv, "--config", "config.json", "--run", "--force"], t, None)
        trig = (sd / "inbox/triggered.jsonl").read_text(encoding="utf-8").splitlines()
        check("R6", c1 == 0 and c2 != 0 and "已经触发过" in l2 and c3 == 0 and W["initiated"] == 2
              and len(trig) == 2 and json.loads(trig[0]).get("sandbox_id") == SID, l1 + l2 + l3)

        # ---------------------------------------------------------------- R7 setup_platform with the right token
        t = tmp / "r7"
        t.mkdir()
        setup(t, base, session=False)
        sp_ = PKG / "platform/setup_platform.py"
        print("== R7 setup_platform：定时任务检查必须用会话令牌")
        c1, l1 = run([sp_, "--config", "config.json", "--cleanup-schedules"], t, None)
        _, l2 = run([sp_, "--config", "config.json", "--check"], t, None)
        (t / "session_token").write_text(SESSION, encoding="utf-8")
        c3, l3 = run([sp_, "--config", "config.json", "--cleanup-schedules"], t, None)
        _, l4 = run([sp_, "--config", "config.json", "--check"], t, None)
        check("R7", c1 == 1 and "需要会话令牌" in l1 and "需要会话令牌" in l2 and "WARN 无法列出定时任务" not in l2
              and c3 == 0 and "没有灵感相关的定时任务" in l3 and "PASS 没有残留的灵感定时任务" in l4
              and "PASS 会话令牌可读会话列表" in l4, "\n".join([l1, l2, l3, l4]))

        # ---------------------------------------------------------------- R9–R12 (v1.4.3, feedback5 §3)
        check("R12 按钮触发的运行保存 prompt.txt（可复现当时的指令）",
              any(p.is_file() and "insp_run.py prepare" in p.read_text(encoding="utf-8") for p in t.parent.glob("r6/runs/trigger-*/prompt.txt")))
        t = tmp / "r9"
        t.mkdir()
        sd = setup(t, base, session=False)
        out, _, _ = make_bundle(t)
        print("== R9 两个收货进程同时收同一个运行 → 只记 1 行、卡片不重复")
        import subprocess as _sp
        spec9 = importlib.util.spec_from_file_location("rcv_r9", rcv)
        R9 = importlib.util.module_from_spec(spec9)
        spec9.loader.exec_module(R9)  # type: ignore[union-attr]
        ctx9 = R9.Ctx(t / "config.json")
        with R9.KeyLock(ctx9, f"ingest:dir:{out}") as held:  # deterministic: another collector holds this conversation
            c9, l9 = run([rcv, "--config", "config.json", "--from-dir", out, "--no-arxiv-check"], t, None)
        check("R9a 另一个收货进程持有锁时 → ALREADY，不写 runs.jsonl、不入库", held.ok and '"ALREADY"' in l9
              and not rows(sd) and ncards(sd) == 0, l9)
        cmd = [sys.executable, "-X", "utf8", str(rcv), "--config", "config.json", "--from-dir", str(out), "--no-arxiv-check"]
        ps = [_sp.Popen(cmd, cwd=t, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, encoding="utf-8") for _ in range(3)]
        outs = [x.communicate(timeout=300)[0] for x in ps]
        logs += outs
        ok_rows = [r for r in rows(sd) if r.get("status") == "OK"]
        check("R9b 3 个进程同时收：只记 1 行", len(ok_rows) == 1 and ncards(sd) == 2 and sum('"ALREADY"' in o for o in outs) == 2, "\n".join(outs))
        print("== R10 NO_CANDIDATES 时 runs.jsonl 的 cards / dedup_dropped 是 []（不是 null）")
        nc = t / "nc"
        nc.mkdir()
        b = jload(out / "bundle.json")
        b.update(status="NO_CANDIDATES", cards=[], run_id="nc-1")
        jsave(nc / "bundle.json", b)
        _, l10 = run([rcv, "--config", "config.json", "--from-dir", nc, "--no-arxiv-check"], t, None)
        r10 = rows(sd)[-1]
        check("R10", r10.get("status") == "NO_CANDIDATES" and r10.get("cards") == [] and r10.get("dedup_dropped") == [], l10)
        print("== R11 记忆幂等：旧版本留下的重复 [灵感状态] / [灵感设置] 被清成 1 条")
        spec = importlib.util.spec_from_file_location("rcv_r11", rcv)
        R = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(R)  # type: ignore[union-attr]

        class FakeMem:
            def __init__(self):
                self.mem = [{"id": f"m{i}", "content": c, "tags": tg} for i, (c, tg) in enumerate([
                    ("[灵感状态] old", ["insp-state"]), ("[灵感状态] old", ["insp-state"]), ("[灵感状态] new", ["insp-state"]),
                    ("[灵感画像] keep me", ["insp-profile"])])]
                self.n = 10

            def list_memory(self):
                return {"memories": list(self.mem)}

            def delete_memory(self, mid):
                self.mem = [m for m in self.mem if m["id"] != mid]

            def create_memory(self, text, **kw):
                self.n += 1
                self.mem.append({"id": f"m{self.n}", "content": text, "tags": kw.get("tags", [])})
        fm = FakeMem()
        match = lambda m: str(m.get("content", "")).startswith("[灵感状态]")
        a = R.upsert_single_memory(fm, match, "[灵感状态] new", category="insp-state", tags=["insp-state"])
        st_mem = [m for m in fm.mem if match(m)]
        b2 = R.upsert_single_memory(fm, match, "[灵感状态] newer", category="insp-state", tags=["insp-state"])
        st2 = [m for m in fm.mem if match(m)]
        check("R11", a == "kept" and len(st_mem) == 1 and st_mem[0]["content"] == "[灵感状态] new" and b2 == "created"
              and len(st2) == 1 and st2[0]["content"] == "[灵感状态] newer" and any("keep me" in m["content"] for m in fm.mem), str(fm.mem))

        check("R8 所有收货日志里没有 traceback（403 不再让收货员崩溃）", not any("Traceback" in x for x in logs), "\n".join(logs)[-3000:])
        print("ALL MOCK RECEIVER TESTS PASSED")
        return 0
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
