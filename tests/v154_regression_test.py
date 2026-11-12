#!/usr/bin/env python3
"""v1.5.4 regression tests for the v1.5.3 test report (S3b / S4b / S5 / S6 / S7 / S8). Offline, no platform.

    python tests/v154_regression_test.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "platform"))
TEMPLATE = PKG / "insp-init" / "assets" / "灵感状态模板" / "inspiration"


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


SC = load("platform/sciencepal_client.py", "sc_v154")
RA = load("platform/run_action.py", "ra_v154")
AQ = load("platform/ask_detail.py", "aq_v154")
RV = load("insp-feedback/scripts/record_verdict.py", "rv_v154")
RS = load("platform/review_server.py", "rs_v154")
RC = load("platform/receiver.py", "rc_v154")

ok = 0


def check(name: str, cond: bool, detail: object = "") -> None:
    global ok
    if not cond:
        print(f"FAIL {name}: {detail}")
        raise SystemExit(1)
    ok += 1
    print(f"PASS {name}")


# ---------------------------------------------------------------- S8: memory paging
class FakeMem(SC.SciencePal):
    def __init__(self, n: int, mode: str):
        super().__init__("https://x", "t")
        self.all = [{"id": str(i), "content": f"m{i}"} for i in range(n)]
        self.mode = mode

    def request(self, method, path, *, params=None, **kw):
        p = params or {}
        lim = int(p.get("limit", 20))
        if lim > 100:
            return {"memories": []}
        if self.mode == "offset":
            start = int(p.get("offset", 0))
        elif self.mode == "page":
            start = (int(p.get("page", 1)) - 1) * lim
        else:
            start = 0
        return {"memories": self.all[start:start + lim], "total": len(self.all)}


for mode, n, want in (("offset", 250, 250), ("page", 230, 230), ("none", 150, 100), ("offset", 37, 37)):
    r = FakeMem(n, mode).list_memory()
    check(f"S8 list_memory {mode} n={n}", len(r["memories"]) == want and len({m["id"] for m in r["memories"]}) == want, r.get("paging"))

# ---------------------------------------------------------------- S6: links
cfg = {"base_url": "https://sciencepal.ai", "review": {"port": 8765}}
check("S6 direct link", SC.web_thread_url(cfg, "t1", "p1") == "https://sciencepal.ai/projects/p1/thread/t1")
check("S6 fallback via review server", SC.web_thread_url(cfg, "t1") == "http://127.0.0.1:8765/go/thread/t1")


class FakeDash(SC.SciencePal):
    def __init__(self):
        super().__init__("https://x", "t")

    def request(self, method, path, **kw):
        if path == "/api/dashboard/personal-conversations":
            return {"threads": [{"thread_id": "t9", "project_id": "p9"}]}
        raise SC.ApiError(404, path, "")


check("S6 project from initiate response", FakeDash().project_of_thread("tX", {"thread_id": "tX", "project_id": "pX"}) == "pX")
check("S6 project from dashboard", FakeDash().project_of_thread("t9") == "p9")
check("S6 unknown project → None", FakeDash().project_of_thread("nope") is None)

with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    (t / "inspiration" / "inbox").mkdir(parents=True)
    (t / "inspiration" / "inbox" / "thread_projects.json").write_text('{"c992015e-7ebe": "0c67f7b9"}', encoding="utf-8")
    (t / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    app = RS.App(t / "inspiration", 1, "tok", 1)
    app.cfg_path = t / "config.json"
    check("S6 /go/thread uses cache", app.thread_page("c992015e-7ebe") == "https://sciencepal.ai/projects/0c67f7b9/thread/c992015e-7ebe")
    check("S6 /go/thread rejects junk", app.thread_page("../../etc") == "https://sciencepal.ai")

# ---------------------------------------------------------------- S5: cancel text
j = {"status": "cancelled", "cancelled_from": "running", "cancel_why": "撤销「立刻动手」", "runs": [], "cancelled_at": "2026-10-31T05:05:00+08:00"}
check("S5 running, runs empty → 执行中被打断", RA.cancel_text(j).startswith("已取消（执行中被打断）"), RA.cancel_text(j))
check("S5 discarded → 已完成 · 结果已丢弃", RA.cancel_text(dict(j, discarded_result=True, finished_at="x")).startswith("已完成 · 结果已丢弃"))
check("S5 queued → 未开始", RA.cancel_text(dict(j, cancelled_from="queued")).startswith("已取消（未开始）"))
check("S5 plan ready → 方案已出", "方案已出" in RA.cancel_text(dict(j, cancelled_from="needs_approval", runs=[{"agent_run_id": "r"}])))

with tempfile.TemporaryDirectory() as tmp:
    sd = Path(tmp) / "inspiration"
    shutil.copytree(TEMPLATE, sd)
    card = {"id": "ins_20261031_014", "type": "knowledge", "title": "QFS", "source": {
        "kind": "arxiv", "ref": "2510.20798", "quote": "q", "url": "https://arxiv.org/abs/2510.20798",
        "pdf_url": "https://arxiv.org/pdf/2510.20798"}, "_trace": {"run_id": "rcv-1", "thread_id": "th-1"}}
    (sd / "cards" / "2026-10-31").mkdir(parents=True)
    (sd / "cards" / "2026-10-31" / "cards.json").write_text(json.dumps({"cards": [card]}), encoding="utf-8")
    # persist_runs: the worker writes runs while running, a cancel on disk wins
    RA.write_json(RA.job_dir(sd, "act_1") / "job.json", {"job_id": "act_1", "card_id": card["id"], "status": "cancelled",
                                                         "cancelled_from": "running", "runs": [], "tier": "T1", "phase": "execute"})
    RA.persist_runs(sd, {"job_id": "act_1", "status": "running", "runs": [{"agent_run_id": "run-1", "thread_id": "th"}]})
    cur = RA.read_job(sd, "act_1")
    check("S5 runs written while running, cancel kept", cur["status"] == "cancelled" and cur["runs"][0]["agent_run_id"] == "run-1", cur)

    # S4b: reject from the CLI cancels the running job and the open ask
    RA.write_json(RA.job_dir(sd, "act_2") / "job.json", {"job_id": "act_2", "card_id": card["id"], "status": "running", "runs": [],
                                                         "tier": "T1", "phase": "execute"})
    AQ.ask_dir(sd, "ask_1").mkdir(parents=True)
    (AQ.ask_dir(sd, "ask_1") / "ask.json").write_text(json.dumps({"ask_id": "ask_1", "card_id": card["id"], "status": "queued"}),
                                                    encoding="utf-8")
    e = RV.record(sd, card["id"], "reject", "不相关")
    j2, a1 = RA.read_job(sd, "act_2"), AQ.read_ask(sd, "ask_1")
    check("S4b CLI reject cancels job", j2["status"] == "cancelled" and j2["cancel_why"] == "评价改为否决", j2)
    check("S4b CLI reject cancels ask", a1["status"] == "cancelled", (a1, e.get("cancelled")))
    check("S4b record() reports it", e.get("cancelled", {}).get("jobs") == ["act_2"], e.get("cancelled"))
    check("S4b receipt text", RA.cancel_text(j2) == "已取消（执行中被打断） · 平台那边正在收尾 · 因评价改为否决", RA.cancel_text(j2))

    # S3b: the ask prompt carries links + the abstract the run already fetched
    (sd.parent / "runs" / "rcv-1").mkdir(parents=True)
    (sd.parent / "runs" / "rcv-1" / "triage.json").write_text(json.dumps({"shortlist": [
        {"id": "2510.20798v1", "abstract": "We propose analog quantum feature selection ... baseline: classical mRMR."}]}), encoding="utf-8")
    ab = AQ.paper_abstract(sd, card)
    check("S3b abstract found", "classical mRMR" in ab, ab)
    pr = AQ.build_prompt(card, ab)
    check("S3b prompt has links + abstract + no-asking rule",
          "arxiv.org/pdf/2510.20798" in pr and "classical mRMR" in pr and "不要向我索要论文" in pr)
    a = AQ.create_ask(sd, card["id"])
    check("S3b ask.json keeps the prompt", "arxiv.org/abs/2510.20798" in (AQ.read_ask(sd, a["ask_id"]).get("prompt") or ""))

    # S7: knowledge cards already in the library from the same run
    ctx = type("C", (), {"state_dir": sd})()
    check("S7 knowledge_already by thread", RC.knowledge_already(ctx, "th-1", None) == 1)
    check("S7 knowledge_already other thread", RC.knowledge_already(ctx, "th-2", "sbx-x") == 0)

print(f"V154 OK: {ok} checks")
