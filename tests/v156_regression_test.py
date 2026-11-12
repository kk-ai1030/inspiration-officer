#!/usr/bin/env python3
"""v1.5.6 regression tests. Offline, no platform.

  ASK1 (追问 A 类卡失败 · stream_repetition)：
    A1–A4  追问 prompt 按来源分支；本地观察卡附打码副本、写明 evidence_ref、禁止去找原文件；论文卡不变
    A5–A8  平台“重复调用被中止”→ 同会话自动重试一次；仍失败时写清原因 + 可重试
    A9     评价页 /api/ask/retry
  K1 (按 6 立刻动手 = 1.5.1 的动手单表单)：
    F1–F5  form 模式（默认；旧值 auto / confirm 同样处理）先弹预填动手单，确认开跑才建任务；direct / 已有任务

    python tests/v156_regression_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "platform"))
os.environ["INSP_NO_OPEN"] = "1"


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


AQ = load("platform/ask_detail.py", "aq_v156")
RS = load("platform/review_server.py", "rs_v156")
RA = load("platform/run_action.py", "ra_v156")
ok = 0


def check(name: str, cond: bool, detail: object = "") -> None:
    global ok
    if not cond:
        print(f"FAIL {name}: {str(detail)[:1500]}")
        raise SystemExit(1)
    ok += 1
    print(f"PASS {name}")


OBS_CARD = {"id": "ins_20261031_005", "type": "optimization", "title": "power_dep.py:静默吞错与参数过多(5 处)", "subfield": "code",
            "one_line": "5 处 except: pass 与 9 个参数的函数", "why_me": "你的主分析脚本", "minimal_check": "改写后对同一输入跑一遍比对输出",
            "expected_observable": "输出一致且异常被显式记录", "falsifier": "输出不一致", "magnitude": "5 处",
            "cost_estimate": {"tokens": "~10k", "wall_clock": "30min", "risk": "low"},
            "source": {"kind": "workspace", "ref": "obs_603d31159b85", "quote": "L145:except Exception: pass", "verified": True,
                       "detector": "code.smell", "evidence_ref": "library://code/power_dep.py#L145"}, "status": "proposed"}
PAPER_CARD = {"id": "ins_20261031_001", "type": "knowledge", "title": "MemBubble", "subfield": "memory", "one_line": "x",
              "why_me": "y", "minimal_check": "在 20 个任务上 A/B", "expected_observable": "解决率不降", "falsifier": "下降",
              "magnitude": "x", "cost_estimate": {"tokens": "~40k", "wall_clock": "2h", "risk": "low"},
              "source": {"kind": "arxiv", "ref": "2610.25127v1", "quote": "q", "verified": True,
                         "abstract": "We study memory bubbles."}, "status": "proposed"}
ANSWER = ("power_dep.py · 数据处理 用显式异常记录替换 except: pass\n产出：异常被记录的条数\n对照：现版本（全部吞掉）\n"
          "判据：输出与现版本一致且记录条数 ≥ 1\n我最不确定的是那 5 处是否都会触发。你常用哪组输入？")

with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    sd = t / "inspiration"
    shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", sd)
    (sd / "cards/2026-10-31").mkdir(parents=True, exist_ok=True)
    (sd / "cards/2026-10-31/cards.json").write_text(
        json.dumps({"date": "2026-10-31", "cards": [OBS_CARD, PAPER_CARD,
                                                     dict(PAPER_CARD, id="ins_20261031_002", title="第二张"),
                                                     dict(PAPER_CARD, id="ins_20261031_003", title="第三张")]}, ensure_ascii=False),
        encoding="utf-8")
    (t / "library/code").mkdir(parents=True)
    (t / "library/code/power_dep.py").write_text("TOKEN = 'sk-abcdefghijklmnopqrstuvwxyz123456'\ntry:\n    x = 1\nexcept Exception:\n    pass\n",
                                                encoding="utf-8")
    cfg = json.loads((PKG / "platform/config.example.json").read_text(encoding="utf-8"))
    cfg.update(state_dir="./inspiration")
    cfg["review"].update(windows_toast=False, fallback_open_browser=False)
    cfg["receiver"] = {"enabled": False}
    cfg["ask"] = {"cancel_grace_s": 0, "open_thread": False}
    cfg["actions"].update(enabled=True, auto_tiers=[], start_now_mode="auto", cancel_grace_s=0)
    cfgp = t / "config.json"
    cfgp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    # ---------------------------------------------------------------- A1–A4 prompt by source kind
    p_bare = AQ.build_prompt(OBS_CARD)
    check("A1 本地观察卡：写明 evidence_ref、沙箱里没有原文件、禁止 find/search_memory；不再出现“打开上面的原文链接”",
          "library://code/power_dep.py#L145" in p_bare and "沙箱里没有原文件" in p_bare and "search_memory" in p_bare
          and "打开上面的原文链接" not in p_bare and "同一条命令或同一个查询不要重复执行" in p_bare, p_bare)
    p_paper = AQ.build_prompt(PAPER_CARD, "We study memory bubbles.")
    check("A2 论文卡不变：原文链接 + 摘要全文 + 自己打开链接去读", "https://arxiv.org/abs/2610.25127v1" in p_paper
          and "摘要全文（运行时已抓取）：We study memory bubbles." in p_paper and "打开上面的原文链接" in p_paper
          and "沙箱里没有原文件" not in p_paper, p_paper)
    d = AQ.dispatch(cfgp, OBS_CARD["id"], run=False)
    a = AQ.read_ask(sd, d["ask_id"])
    att = AQ.ask_dir(sd, a["ask_id"]) / "attach" / "local_power_dep.py"
    pr = (AQ.ask_dir(sd, a["ask_id"]) / "prompt.md").read_text(encoding="utf-8")
    check("A3 追问也附本地证据（打码副本），prompt 指向 /workspace/_refs/ 的附件",
          a.get("attach") == ["local_power_dep.py"] and att.is_file() and "abcdefghijklmnop" not in att.read_text(encoding="utf-8")
          and "local_power_dep.py" in pr and "/workspace/_refs/" in pr and "直接读这个附件" in pr, json.dumps(a, ensure_ascii=False)[:600])
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["observe"]["upload_code_snippets"] = False
    cfgp2 = t / "config2.json"
    cfgp2.write_text(json.dumps(cfg2, ensure_ascii=False), encoding="utf-8")
    AQ.cancel_for_card(sd, OBS_CARD["id"])
    d2 = AQ.dispatch(cfgp2, OBS_CARD["id"], run=False)
    pr2 = (AQ.ask_dir(sd, d2["ask_id"]) / "prompt.md").read_text(encoding="utf-8")
    check("A4 关掉 upload_code_snippets：不附文件，prompt 改说“沙箱里没有原文件，只依据片段”",
          not AQ.read_ask(sd, d2["ask_id"]).get("attach") and "沙箱里没有原文件" in pr2 and "local_" not in pr2, pr2[-800:])
    AQ.cancel_for_card(sd, OBS_CARD["id"])

    # ---------------------------------------------------------------- A5–A8 stream_repetition → one retry in the same thread
    fake = t / "rep_then_ok.txt"
    fake.write_text("STATUS:failed stream_repetition\nls /workspace\n@@RETRY@@\n" + ANSWER, encoding="utf-8")
    d = AQ.dispatch(cfgp, OBS_CARD["id"], run=False)
    r = AQ.execute(cfgp, d["ask_id"], fake)
    check("A5 平台判“重复”中止 → 同会话自动重试一次后回答", r["status"] == "answered" and r.get("retry", {}).get("reason") == "stream_repetition"
          and r["answer"].startswith("power_dep.py") and "自动重试" in (r.get("note") or ""), json.dumps(r, ensure_ascii=False)[:800])
    fake2 = t / "rep_twice.txt"
    fake2.write_text("STATUS:failed stream_repetition\nls\n@@RETRY@@\nSTATUS:failed stream_repetition\nls", encoding="utf-8")
    d = AQ.dispatch(cfgp, OBS_CARD["id"], run=False)
    r = AQ.execute(cfgp, d["ask_id"], fake2)
    check("A6 重试后仍被中止 → failed，原因写清（反复调用工具 + 已自动重试 1 次 + 可点重试追问）",
          r["status"] == "failed" and r.get("failure_reason") == "stream_repetition" and "反复调用同样的工具" in r["error"]
          and "自动重试 1 次" in r["error"] and "重试追问" in r["error"], r.get("error"))
    fake3 = t / "other_fail.txt"
    fake3.write_text("STATUS:failed\n", encoding="utf-8")
    d = AQ.dispatch(cfgp, PAPER_CARD["id"], run=False)
    r = AQ.execute(cfgp, d["ask_id"], fake3)
    check("A7 其它失败不重试，文案带“重试追问”", r["status"] == "failed" and not r.get("retry") and "重试追问" in r["error"], r)
    v = {x["ask_id"]: x for x in AQ.asks_view(cfg, sd)}
    check("A8 回执里失败的追问可重试", v[d["ask_id"]]["retryable"] is True and v[d["ask_id"]]["status_text"] == "没有完成", v[d["ask_id"]])
    check("A8b failure_reason 认得平台原文", AQ.failure_reason({"error": json.dumps({"code": "model_stream_interrupted",
                                                                                     "failure_reason": "stream_repetition"})}) == "stream_repetition"
          and AQ.failure_reason({"status": "failed"}) is None)

    # A10 the real platform path (mocked client): failed + stream_repetition → add_message + start_thread_agent in the same thread
    calls: list = []

    class MockSP:
        def __init__(self, *a, **k):
            pass

        def initiate(self, prompt, **kw):
            calls.append(("initiate", [Path(f).name for f in kw.get("files") or []]))
            return {"thread_id": "th-1", "agent_run_id": "run-1", "project_id": "pj-1"}

        def project_of_thread(self, tid, resp=None):
            return "pj-1"

        def stream_run(self, rid, timeout_s=0):
            return [{"type": "tool", "content": "ls /workspace"}] if rid == "run-1" else [{"type": "assistant", "content": ANSWER}]

        def wait_run(self, rid, timeout_s=0, poll_s=0):
            if rid == "run-1":
                return {"status": "failed", "error": json.dumps({"code": "model_stream_interrupted", "timeout_phase": "stream_repetition",
                                                                  "recoverable": True, "failure_reason": "stream_repetition"})}
            return {"status": "completed"}

        def add_message(self, tid, content):
            calls.append(("add_message", tid, content[:6]))
            return "m-1"

        def start_thread_agent(self, tid, agent_id, mid):
            calls.append(("start_thread_agent", tid, mid))
            return {"agent_run_id": "run-2"}

    (t / "platform_ids.json").write_text(json.dumps({"agent_id": "ag-1"}), encoding="utf-8")
    os.environ["SCIENCEPAL_RUN_TOKEN"] = "sp_live_test"
    real_sp = AQ.SciencePal
    AQ.SciencePal = MockSP
    try:
        dA = AQ.dispatch(cfgp, OBS_CARD["id"], run=False)
        r = AQ.execute(cfgp, dA["ask_id"])
    finally:
        AQ.SciencePal = real_sp
    check("A10 平台路径：附件随 initiate 上传；被判重复后在同一会话 add_message + start_thread_agent 重试一次并拿到回答",
          ("initiate", ["local_power_dep.py"]) in calls and ("add_message", "th-1", "【自动重试】") in calls
          and ("start_thread_agent", "th-1", "m-1") in calls and r["status"] == "answered" and r["agent_run_id"] == "run-2"
          and r["retry"]["from_run"] == "run-1", json.dumps([calls, r], ensure_ascii=False, default=str)[:900])

    # ---------------------------------------------------------------- server: /api/ask/retry + K1 form
    dummy = t / "fake_action.txt"
    dummy.write_text("no block", encoding="utf-8")
    os.environ["INSP_FAKE_AGENT_OUTPUT"] = str(dummy)   # any task the form creates runs offline
    os.environ["INSP_FAKE_ASK_OUTPUT"] = str(fake)
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ready, holder = threading.Event(), []
    threading.Thread(target=RS.serve, args=(sd, port, 0, ready, holder, cfgp), daemon=True).start()
    ready.wait(10)
    srv = holder[0]

    def call(path, body, method="POST"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={"X-Token": RS.get_token(), "Content-Type": "application/json",
                                                             "Host": f"127.0.0.1:{port}"})
        try:
            return 200, json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    code, rr = call("/api/ask/retry", {"ask_id": d["ask_id"]})
    check("A9 “重试追问”：失败的追问重新发一次（新 ask，via=retry）", code == 200 and rr.get("ask_id") and rr["ask_id"] != d["ask_id"]
          and AQ.read_ask(sd, rr["ask_id"])["via"] == "retry", rr)
    code, rr = call("/api/ask/retry", {"ask_id": "../../etc"})
    check("A9b 非法 ask_id 拒绝", code == 400, rr)

    # F1 default form (config still says "auto"): 6 → pre-filled brief, NO job yet
    jobs0 = len(RA.list_jobs(sd))
    code, rr = call("/api/verdict", {"card_id": "ins_20261031_002", "action": "start_now"})
    act = rr.get("action") or {}
    b = act.get("brief") or {}
    check("F1 按 6（start_now_mode=auto 视同 form）：返回预填动手单，还没建任务",
          act.get("mode") == "form" and act.get("needs_input") is True and b.get("scene") and b.get("method") is not None
          and "output" in b and "baseline" in b and "criterion" in b and isinstance(b.get("params"), list)
          and isinstance(b.get("deliverables"), list) and len(RA.list_jobs(sd)) == jobs0, act)
    # F2 确认开跑 → task
    send = {k: v for k, v in b.items() if k not in ("problems", "missing")}
    send["params"] = [dict(p, value=p.get("value") or "3", unit=p.get("unit") or "个") for p in (send.get("params") or [])] or \
        [{"name": "任务数", "value": "20", "unit": "个", "source": "user"}]
    send["deliverables"] = send.get("deliverables") or ["report.md"]
    code, rr = call("/api/action/start", {"card_id": "ins_20261031_002", "brief": send})
    check("F2 动手单“确认开跑”后才建任务", code == 200 and rr.get("job_id") and len(RA.list_jobs(sd)) == jobs0 + 1, rr)
    # F3 pressing 6 again on the same card → shows the existing task, no second form
    code, rr = call("/api/verdict", {"card_id": "ins_20261031_002", "action": "start_now"})
    act = rr.get("action") or {}
    check("F3 已有任务的卡再按 6：直接给那条任务，不再弹表单", act.get("job_id") and not act.get("needs_input")
          and len(RA.list_jobs(sd)) == jobs0 + 1, act)
    # F4 direct = old auto behaviour
    cfg["actions"]["start_now_mode"] = "direct"
    cfgp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    code, rr = call("/api/verdict", {"card_id": "ins_20261031_003", "action": "start_now"})
    act = rr.get("action") or {}
    check("F4 start_now_mode=direct：动手单完整就直接建任务（旧 auto）", act.get("mode") == "direct"
          and (act.get("job_id") or act.get("needs_input")), act)
    # F5 off
    cfg["actions"]["start_now_mode"] = "off"
    cfgp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    code, rr = call("/api/verdict", {"card_id": "ins_20261031_001", "action": "start_now"})
    check("F5 off：只记评价", (rr.get("action") or {}).get("mode") == "off", rr)
    srv.shutdown()
    check("F6 config.example 默认 form", json.loads((PKG / "platform/config.example.json").read_text(encoding="utf-8"))["actions"]["start_now_mode"] == "form")

print(f"V156 OK: {ok} checks")
