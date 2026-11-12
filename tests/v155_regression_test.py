#!/usr/bin/env python3
"""v1.5.5 regression tests for the v1.5.4 test report (P2 / P3 / P5 / P9 / P10 / P11 / P12). Offline, no platform.

    python tests/v155_regression_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "platform"))


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


SC = load("platform/sciencepal_client.py", "sc_v155")
RC = load("platform/receiver.py", "rc_v155")
RS = load("platform/review_server.py", "rs_v155")
AQ = load("platform/ask_detail.py", "aq_v155")
ok = 0


def check(name: str, cond: bool, detail: object = "") -> None:
    global ok
    if not cond:
        print(f"FAIL {name}: {str(detail)[:1500]}")
        raise SystemExit(1)
    ok += 1
    print(f"PASS {name}")


NOW = datetime.now(timezone.utc).isoformat()
OBS = [{"id": "obs_a1", "kind": "workspace", "candidate_type": "optimization", "title": "power_dep.py 第 520 行循环里反复读文件",
        "evidence": "for f in files:\n    data = np.loadtxt(path)", "severity": 3.0, "last_seen": NOW, "first_seen": NOW,
        "detector": "loop_io", "evidence_ref": "library/code/power_dep.py:520", "status": "open"},
       {"id": "obs_h1", "kind": "workspace", "candidate_type": "hypothesis", "title": "笔记里的以后想试", "evidence": "以后想试：空间结构",
        "severity": 2.5, "last_seen": NOW, "status": "open"}]


def run(args: list, env: dict | None = None) -> tuple[int, str]:
    p = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
    return p.returncode, p.stdout + p.stderr


# ---------------------------------------------------------------- P11: web entry uses the skill snapshot
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    skill = t / "skills" / "insp-nightly"
    shutil.copytree(PKG / "insp-nightly", skill, ignore=shutil.ignore_patterns("__pycache__"))
    runner = skill / "scripts" / "insp_run.py"
    sbx = ["--out", t / "out", "--state-home", t / "home", "--refs", t / "_refs"]

    code, log = run([runner, "prepare", *sbx, "--observed-only"])
    tri = json.loads((t / "out" / "triage.json").read_text(encoding="utf-8"))
    check("P11 no snapshot → bootstrap, observed_missing is visible", "MODE bootstrap" in log
          and tri["counts"].get("observed_missing") is True and tri["counts"].get("observed") == 0, log)

    snap = skill / "assets" / "local_state"
    snap.mkdir(parents=True)
    st = json.loads((PKG / "insp-nightly/assets/state_template/state.json").read_text(encoding="utf-8"))
    st["pushed"] = [{"id": "obs_h1", "pushed_at": NOW}]
    (snap / "state.json").write_text(json.dumps(st), encoding="utf-8")
    (snap / "observed.jsonl").write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in OBS) + "\n", encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({"published_at": NOW, "published_epoch": 4102444800, "observed": 2}), encoding="utf-8")
    code, log = run([runner, "prepare", *sbx, "--observed-only"])
    tri = json.loads((t / "out" / "triage.json").read_text(encoding="utf-8"))
    ids = [c["id"] for c in tri.get("observed_shortlist") or []]
    check("P11 snapshot → MODE skill_snapshot, A candidate offered, pushed one filtered",
          "MODE skill_snapshot" in log and ids == ["obs_a1"] and "OBSERVED 1" in log
          and tri["counts"].get("observed_source") == "skill_snapshot" and not tri["counts"].get("observed_missing"), log)
    check("P9 prepare prints the one-card-per-file rules and creates cards/", "一张卡 = 一个文件" in log
          and (t / "out" / "cards").is_dir() and (t / "out" / "obs_a1.md").is_file(), log[-800:])

    # persisted state newer than the snapshot wins (same conversation, second run)
    (t / "home").mkdir(exist_ok=True)
    (snap / "snapshot.json").write_text(json.dumps({"published_epoch": 1}), encoding="utf-8")
    (t / "home" / "state.json").write_text(json.dumps(st), encoding="utf-8")
    code, log = run([runner, "prepare", *sbx, "--observed-only"])
    tri = json.loads((t / "out" / "triage.json").read_text(encoding="utf-8"))
    check("P11 newer persisted state wins, snapshot still fills an empty observed.jsonl",
          "MODE persisted" in log and tri["counts"].get("observed") == 2, log)

    # ------------------------------------------------------------ P9: finalize assembles single-card files
    IR = load("insp-nightly/scripts/insp_run.py", "ir_v155")
    out = t / "asm"
    (out / "cards").mkdir(parents=True)
    (out / "cards" / "01.json").write_text(json.dumps({"id": "ins_1", "type": "optimization"}), encoding="utf-8")
    (out / "cards" / "02.json").write_text(json.dumps({"id": "ins_2", "type": "knowledge"}), encoding="utf-8")
    (out / "cards" / "rejected.json").write_text(json.dumps([{"id": "x", "reason": "r"}]), encoding="utf-8")
    doc, how = IR.assemble_cards(out, {"run_id": "sbx-1"})
    check("P9 assemble_cards: 2 cards + rejected + root fields", [c["id"] for c in doc["cards"]] == ["ins_1", "ins_2"]
          and doc["rejected"][0]["id"] == "x" and doc["run_id"] == "sbx-1" and doc["status"] == "OK"
          and (out / "cards_final.json").is_file() and how.endswith("2 个观察型单卡文件"), (doc, how))
    (out / "cards" / "03.json").write_text("{broken", encoding="utf-8")
    doc, how = IR.assemble_cards(out, {"run_id": "sbx-1"})
    check("P9 a broken single-card file names itself", doc is None and "03.json" in how, how)
    empty = t / "empty"
    (empty / "cards").mkdir(parents=True)
    check("P9 no cards at all → None", IR.assemble_cards(empty, {})[0] is None)


# ---------------------------------------------------------------- P9: receiver rescues a run that wrote nothing
class FakeSrc:
    files: dict = {}

    def __init__(self, sp, tid, out_dir, sandbox_id=None):
        self.thread_id = tid

    def read(self, rel):
        return FakeSrc.files.get(rel)


class FakeSP:
    def __init__(self):
        self.status = "running"
        self.runs = [{"id": "run1", "created_at": "2026-10-31T16:29:24", "status": "running"}]
        self.msgs, self.starts = [], []

    def thread_agent_runs(self, tid):
        for r in self.runs:
            r.setdefault("status", self.status)
        return {"agent_runs": self.runs}

    def get_run(self, rid):
        return {"status": next((r["status"] for r in self.runs if r["id"] == rid), "")}

    def add_message(self, tid, text):
        self.msgs.append(text)
        return "m1"

    def start_thread_agent(self, tid, agent_id, mid):
        self.starts.append((tid, agent_id, mid))
        self.runs.append({"id": "run2", "created_at": "2026-10-31T16:40:00", "status": "running"})
        return {"agent_run_id": "run2"}

    def project_of_thread(self, tid, resp=None):
        return "p1"


class Ctx:
    def __init__(self, d: Path):
        self.state_dir = d
        self.inbox = d / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.rc = dict(RC.DEFAULTS)
        self.cfg = {"base_url": "https://sciencepal.ai", "review": {"enabled": False}}

    def ids(self):
        return {"agent_id": "ag1"}

    def setup_client(self):
        return None


RC.ThreadSource = FakeSrc
with tempfile.TemporaryDirectory() as tmp:
    ctx = Ctx(Path(tmp))
    sp = FakeSP()
    FakeSrc.files = {}
    check("P9 no run_meta (not an inspiration run) → ignored", RC.rescue_incomplete(ctx, sp, "t1") is None)
    FakeSrc.files = {"run_meta.json": json.dumps({"run_id": "sbx-1", "counts": {"shortlisted": 8}}).encode()}
    check("P9 run still going → running (thread is re-checked later)", RC.rescue_incomplete(ctx, sp, "t1") == {"running": True})
    sp.runs[0]["status"] = "completed"
    r = RC.rescue_incomplete(ctx, sp, "t1")
    check("P9 finished without bundle → one automatic follow-up in the same thread",
          r["status"] == "CONTINUED" and r["run"] == "run2" and len(sp.msgs) == 1 and "自动续跑" in sp.msgs[0]
          and "cards/01.json" in sp.msgs[0] and sp.starts[0][1] == "ag1", (r, sp.msgs))
    check("P9 follow-up run is tracked as pending", any(p.get("agent_run_id") == "run2" for p in RC.pending_runs(ctx)))
    st = json.loads((ctx.inbox / "receiver_status.json").read_text(encoding="utf-8"))
    check("P9 review page sees CONTINUED + link", st["rescue"]["status"] == "CONTINUED"
          and st["rescue"]["url"] == "https://sciencepal.ai/projects/p1/thread/t1", st)
    RC.rescue_incomplete(ctx, sp, "t1")   # run2 still running
    check("P9 no second message while the follow-up runs", len(sp.msgs) == 1)
    sp.runs[1]["status"] = "completed"
    r = RC.rescue_incomplete(ctx, sp, "t1")
    rows = [json.loads(x) for x in (ctx.state_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    check("P9 follow-up also empty → NO_OUTPUT row + status, no third run",
          r["status"] == "NO_OUTPUT" and rows[-1]["status"] == "NO_OUTPUT" and len(sp.msgs) == 1
          and json.loads((ctx.inbox / "receiver_status.json").read_text(encoding="utf-8"))["rescue"]["status"] == "NO_OUTPUT", rows)
    RC.rescue_incomplete(ctx, sp, "t1")
    check("P9 handled runs are not reported twice", len((ctx.state_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 1)
    FakeSrc.files["bundle.json"] = b"{}"
    check("P9 bundle present → nothing to rescue", RC.rescue_incomplete(ctx, sp, "t9") is None)

# ---------------------------------------------------------------- P12 / P5: dashboard titles + project ids
class Dash(SC.SciencePal):
    def __init__(self):
        super().__init__("https://x", "t")

    def request(self, method, path, **kw):
        return {"projects": [{"project_id": "p1", "name": "信息价值统一检索"}],
                "threads": [{"thread_id": "t1", "project_id": "p1"}, {"thread_id": "t2", "project_id": "p2"}], "statuses": []}


th = Dash().dashboard_threads()
check("P12 dashboard threads get projects[].name as title", th[0]["title"] == "信息价值统一检索" and "title" not in th[1], th)
check("P5 project_of_thread via dashboard", Dash().project_of_thread("t2") == "p2")

# ---------------------------------------------------------------- P10: A-class status line
with tempfile.TemporaryDirectory() as tmp:
    sd = Path(tmp)
    rows = [dict(OBS[0], id=f"obs_o{i}") for i in range(5)] + [OBS[1]]
    (sd / "observed.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    (sd / "state.json").write_text(json.dumps({"pushed": [{"id": f"obs_o{i}"} for i in range(4)]}), encoding="utf-8")
    (sd / "negative.jsonl").write_text(json.dumps({"kind": "id", "value": "obs_o4"}), encoding="utf-8")
    a = RS.a_status(sd)
    check("P10 a_status: 5 A findings = 4 pushed + 1 rejected → 0 open",
          a == {"observed": 6, "a_total": 5, "a_pushed": 4, "a_rejected": 1, "a_closed": 0, "a_open": 0}, a)

# ---------------------------------------------------------------- P2: abstract stored with the card
check("P2 paper_abstract prefers card.source.abstract",
      AQ.paper_abstract(Path("/nonexistent"), {"source": {"ref": "2510.1", "abstract": "We study X."}}) == "We study X.")

ui = (PKG / "platform/review_ui.html").read_text(encoding="utf-8")
check("P3 UI pops only the newest unread answer", "另有 ${more} 条未读追问" in ui and "unread.forEach(a=>markAskSeen" in ui)
check("P9/P10/P11 UI lines present", "已在同一会话自动续跑一次" in ui and "A 类候选：待用" in ui and "网页入口快照" in ui)

print(f"V155 OK: {ok} checks")
