#!/usr/bin/env python3
"""Back-fill platform evidence for runs that were executed with an API key (skill_loaded = unverified/unknown).

Why: an API key (sp_live_) can start runs but gets 401/403 on GET /api/agent-run/{id}/skills.
Unattended runs therefore rely on the flow_version canary; this script, run with a session token
(e.g. once a day when you are logged in), asks the platform whether insp-card was really loaded.

Usage (from the package root):
  $env:SCIENCEPAL_SETUP_TOKEN="<session token>"; python platform/run_audit.py --config config.json
Input : inspiration/runs.jsonl (rows with agent_run_id and skill_loaded in {unverified, unknown})
Output: inspiration/audit.jsonl (append-only), one row per run: {run_id, agent_run_id, skill_loaded, checked_at}
Exit  : 0 all audited runs loaded=true · 1 some loaded=false (those cards were NOT produced by the skill) · 2 token missing
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from sciencepal_client import ApiError, SciencePal, skill_loaded  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--all", action="store_true", help="连已审计过的也重查")
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    sd = (a.config.resolve().parent / cfg["state_dir"]).resolve()
    tok = os.environ.get(cfg.get("setup_token_env", "SCIENCEPAL_SETUP_TOKEN"), "")
    if not tok:
        print("ERROR: 需要会话令牌环境变量", cfg.get("setup_token_env", "SCIENCEPAL_SETUP_TOKEN"))
        return 2
    sp = SciencePal(cfg["base_url"], tok, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer"))
    runs = [json.loads(l) for l in (sd / "runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    ap_path = sd / "audit.jsonl"
    done = set() if a.all or not ap_path.exists() else {json.loads(l)["run_id"] for l in ap_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    todo = [r for r in runs if r.get("agent_run_id") and r.get("skill_loaded") in ("unverified", "unknown") and r["run_id"] not in done]
    bad = 0
    with ap_path.open("a", encoding="utf-8") as f:
        for r in todo:
            try:
                loaded = skill_loaded(sp.run_skills(r["agent_run_id"]), cfg.get("skill_to_check", "insp-card"))
            except ApiError as exc:
                loaded = f"error:{exc.status}"
            bad += loaded == "false"
            row = {"run_id": r["run_id"], "agent_run_id": r["agent_run_id"], "skill_loaded": loaded,
                   "cards": r.get("cards", []), "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"{r['run_id']}  loaded={loaded}  cards={len(r.get('cards', []))}")
    print(f"AUDIT: 查了 {len(todo)} 次运行，loaded=false {bad} 次 → {ap_path}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
