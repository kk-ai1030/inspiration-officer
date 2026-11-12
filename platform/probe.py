#!/usr/bin/env python3
"""Day-0 probes: confirm the platform assumptions BEFORE relying on them (design doc §19).

  P1 auth           which header works (Authorization: Bearer / x-api-key)
  P2 attach+stream  initiate with an attached file containing a nonce; does the agent see it under /workspace/_refs?
                    can we get the reply out of the stream / run object?
  P3 skill loaded   (after setup_platform.py) does an explicit "load insp-nightly" show loaded=true in agent-run/skills?
  P4 output file    can we read /workspace/inspiration_out/... back via /api/thread/{id}/files/content?

Usage:
    export SCIENCEPAL_TOKEN=...
    python3 platform/probe.py --config config.json [--only P1,P2]
Writes probe_report.json next to the config.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from sciencepal_client import ApiError, SciencePal, collect_strings, skill_loaded  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--only", default="P1,P2,P3,P4")
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    base = a.config.resolve().parent
    token = os.environ.get(cfg.get("token_env", "SCIENCEPAL_TOKEN"), "")
    if not token:
        print("ERROR: token 未设置")
        return 1
    only = set(a.only.split(","))
    report: dict = {}

    sp = SciencePal(cfg["base_url"], token, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer"))
    if "P1" in only:
        tried = []
        for header, scheme in [("Authorization", "Bearer"), ("x-api-key", ""), ("X-API-Key", "")]:
            c = SciencePal(cfg["base_url"], token, header, scheme)
            try:
                c.list_agents()
                tried.append({"header": header, "scheme": scheme, "ok": True})
                sp = c
                break
            except ApiError as exc:
                tried.append({"header": header, "scheme": scheme, "ok": False, "status": exc.status})
        report["P1_auth"] = tried
        print("P1", tried)
        if not any(t["ok"] for t in tried):
            print("P1 失败：所有鉴权方式都被拒绝，后续探测跳过")
            (base / "probe_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1

    ids_path = base / cfg.get("platform_ids_file", "platform_ids.json")
    ids = json.loads(ids_path.read_text(encoding="utf-8")) if ids_path.exists() else {}
    agent_id = ids.get("agent_id")

    if "P2" in only or "P4" in only:
        nonce = "NONCE-" + secrets.token_hex(4)
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "probe_state.json"
            f.write_text(json.dumps({"nonce": nonce}), encoding="utf-8")
            prompt = ("【仅本次任务】执行：ls -R /workspace/_refs；找到 probe_state.json 并 cat 它；"
                      "然后执行 mkdir -p /workspace/inspiration_out && cp <找到的文件> /workspace/inspiration_out/probe.json。"
                      "最后只回复一行：FOUND <nonce 值> 或 NOT_FOUND。禁止调用 ask。")
            resp = sp.initiate(prompt, agent_id=agent_id, files=[f], web_search_on=False,
                               workspace_context=cfg.get("workspace_context"))
        run_id, thread_id = resp.get("agent_run_id"), resp.get("thread_id")
        events = sp.stream_run(run_id, timeout_s=600) if run_id else []
        final = sp.wait_run(run_id, timeout_s=120, poll_s=5) if run_id else None
        texts = collect_strings(events) + collect_strings(final)
        seen = any(nonce in t for t in texts)
        report["P2_attach_stream"] = {"thread_id": thread_id, "agent_run_id": run_id, "nonce_seen_in_output": seen,
                                      "n_events": len(events), "final_status": (final or {}).get("status") if isinstance(final, dict) else None}
        print("P2", report["P2_attach_stream"])
        if "P4" in only and thread_id:
            res = {}
            for p in ["/workspace/inspiration_out/probe.json", "inspiration_out/probe.json"]:
                try:
                    res[p] = nonce in sp.read_thread_file(thread_id, p).decode("utf-8", "replace")
                except ApiError as exc:
                    res[p] = f"HTTP {exc.status}"
            report["P4_read_output_file"] = res
            print("P4", res)

    if "P3" in only:
        if not agent_id:
            report["P3_skill_loaded"] = "skipped: 先运行 setup_platform.py"
        else:
            resp = sp.initiate("【仅本次任务】第一步：调用技能加载工具加载 insp-nightly，复述其中 3 条硬纪律，然后结束。禁止调用 ask。",
                               agent_id=agent_id, files=[], web_search_on=False)
            run_id = resp.get("agent_run_id")
            sp.stream_run(run_id, timeout_s=600)
            ev = sp.run_skills(run_id)
            report["P3_skill_loaded"] = {"agent_run_id": run_id, "insp-nightly": skill_loaded(ev, "insp-nightly"), "raw": ev}
            print("P3", report["P3_skill_loaded"]["insp-nightly"])

    (base / "probe_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("报告已写入 probe_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
