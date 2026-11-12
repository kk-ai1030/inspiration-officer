#!/usr/bin/env python3
"""B7 最小验证可执行化：离线回归（零联网）。

验证：脚本模板只打印清单、卡片文本被安全转义；dry-run 回执是独立字段，
不污染动手任务回执的 checks / 结论 / ack；评价页两个只读接口可用。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK = True


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:300]) if detail and not cond else ""))
    OK &= bool(cond)


def card() -> dict:
    return {"id": "ins_b7_1", "type": "hypothesis", "title": '标题带 "引号"',
            "one_line": "一句话", "minimal_check": "跑 A / B 两组", "expected_observable": "A 比 B 高 3%",
            "falsifier": "A 不比 B 高", "cost_estimate": {"tokens": "~1k"},
            "source": {"kind": "arxiv", "ref": "9999.12345", "quote": "q", "verified": True},
            "status": "proposed", "verdict": None}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_b7_"))
    try:
        sd = tmp / "inspiration"
        shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
        (sd / "cards" / "2026-11-12").mkdir(parents=True, exist_ok=True)
        (sd / "cards" / "2026-11-12" / "cards.json").write_text(
            json.dumps({"date": "2026-11-12", "cards": [card()]}, ensure_ascii=False), encoding="utf-8")
        dt = load("platform/dryrun_template.py", "b7_dryrun")
        ra = load("platform/run_action.py", "b7_run_action")
        rs = load("platform/review_server.py", "b7_review_server")

        print("== B7-1 模板与回执")
        check("B7-1 模块自测", dt.self_test() == 0)
        b = dt.build(card())
        check("B7-1 模板可编译", _compiles(b["content"]))
        check("B7-1 模板无执行 / 联网能力", not any(x in b["content"] for x in dt.DANGEROUS))
        check("B7-1 引号被转义", '\\"引号\\"' in b["content"])
        d = dt.receipt(card(), job_id="act_b7_1")
        check("B7-1 回执恒未执行 / 无定论", d["executed"] is False and d["verdict"] == "inconclusive")
        md = dt.render_receipt_md(d)
        check("B7-1 回执写明不能当证据", "不能当证据" in md and "最小验证" in md and "推翻条件" in md)

        print("== B7-2 与动手任务回执隔离")
        jid = "act_b7_1"
        jd = ra.job_dir(sd, jid)
        jd.mkdir(parents=True, exist_ok=True)
        job = {"job_id": jid, "card_id": "ins_b7_1", "card_title": card()["title"], "tier": "T0",
               "tier_label": "本地可验证", "tier_reasons": ["最小验证可离线跑"], "phase": "plan",
               "status": "succeeded", "runs": [{"thread_id": "t1", "agent_run_id": "r1", "run_status": "completed"}],
               "created_at": "2026-11-12T10:00:00+08:00", "started_at": "2026-11-12T10:00:01+08:00",
               "finished_at": "2026-11-12T10:00:02+08:00", "verdict_ts": "2026-11-12T09:00:00+08:00",
               "card_snapshot": card(), "brief": None, "local_files": [], "ack": None, "error": None, "attempts": 1}
        rec = ra.write_receipt(sd, job, None, {}, [], 1.0)
        saved = json.loads((jd / "receipt.json").read_text(encoding="utf-8"))
        check("B7-2 receipt.json 带独立 dry_run", saved["dry_run"]["mode"] == "dry_run" and
              saved["dry_run"]["executed"] is False)
        check("B7-2 checks 未被污染", len(rec["checks"]) == 2 and all("dry" not in c["name"] for c in rec["checks"]),
              str(rec["checks"]))
        check("B7-2 all_checks_ok 仍由 checks 决定", rec["all_checks_ok"] == all(c["ok"] for c in rec["checks"]))
        check("B7-2 结论与 claims 不变", "summary" in rec["claims"] and "dry_run" not in rec["claims"]
              and rec["conclusion"] != "inconclusive", str(rec["conclusion"]))
        rmd = (jd / "receipt.md").read_text(encoding="utf-8")
        check("B7-2 receipt.md 有独立 dry-run 段落", "## 本地 dry-run（未执行真实实验）" in rmd and
              "不能当证据" in rmd and "## 你的确认" in rmd)
        check("B7-2 重渲染（模拟 ack）仍保留段落", "## 本地 dry-run" in
              ra.render_receipt({**saved, "ack": {"result": "accept", "ts": "2026-11-12T12:00:00+08:00", "note": "ok"}}))
        check("B7-2 没有写 verdicts.jsonl", not (sd / "verdicts.jsonl").exists() or
              (sd / "verdicts.jsonl").read_text(encoding="utf-8").strip() == "")

        print("== B7-3 评价页只读接口")
        rs.TOKEN_FILE = tmp / "review_token"
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        ready, holder = threading.Event(), []
        threading.Thread(target=rs.serve, args=(sd, port, 0, ready, holder), daemon=True).start()
        ready.wait(5)
        tok = rs.get_token()

        def call(path: str):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Token": tok})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        code, r = call("/api/action/script?card_id=ins_b7_1")
        check("B7-3 脚本接口返回模板", code == 200 and r["filename"].endswith(".py") and
              "DRY-RUN" in r["content"] and r["executed"] is False, str(r)[:200])
        code, r = call("/api/action/dryrun?card_id=ins_b7_1")
        check("B7-3 回执接口返回结构化 + Markdown", code == 200 and
              r["dry_run"]["verdict"] == "inconclusive" and "不能当证据" in r["md"], str(r)[:200])
        code, r = call("/api/action/script?card_id=missing")
        check("B7-3 找不到卡片返回 404", code == 404, str(r))
        if holder:
            holder[0].shutdown()

        print("== B7-4 UI 接线")
        ui = (ROOT / "platform/review_ui.html").read_text(encoding="utf-8")
        check("B7-4 UI 有下载入口与回执行", all(x in ui for x in ("downloadScript", "/api/action/script",
                                                               "data-ds", "验证脚本")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("B7 OK" if OK else "B7 TESTS FAILED")
    return 0 if OK else 1


def _compiles(text: str) -> bool:
    try:
        compile(text, "dryrun.py", "exec")
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
