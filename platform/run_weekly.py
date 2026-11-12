#!/usr/bin/env python3
"""Weekly orchestrator: export platform memory → weekly_review.py → notify.
Also executes memory decisions the user wrote down (keep / patch / delete).

Usage:
    python3 platform/run_weekly.py --config config.json                 # generate weekly report
    python3 platform/run_weekly.py --config config.json --offline       # skip memory export
    python3 platform/run_weekly.py --config config.json --memory-actions actions.json
        actions.json: [{"id": "...", "action": "delete"}, {"id": "...", "action": "patch", "content": "仅当……：……"}]
Schedule:  0 10 * * 1
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
from sciencepal_client import SciencePal  # noqa: E402

spec = importlib.util.spec_from_file_location("weekly", PKG / "insp-review/scripts/weekly_review.py")
WEEKLY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(WEEKLY)  # type: ignore[union-attr]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--memory-actions", type=Path)
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    base = a.config.resolve().parent
    state_dir = (base / cfg["state_dir"]).resolve()
    sp = None
    if not a.offline:
        # /api/memory needs a session token; an API key (sp_live_) gets 401/403.
        token = os.environ.get(cfg.get("setup_token_env", "SCIENCEPAL_SETUP_TOKEN"), "") or os.environ.get(cfg.get("token_env", "SCIENCEPAL_TOKEN"), "")
        sp = SciencePal(cfg["base_url"], token, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer"))

    if a.memory_actions:
        if sp is None:
            print("ERROR: 执行记忆操作需要在线模式")
            return 1
        for act in json.loads(a.memory_actions.read_text(encoding="utf-8")):
            if act["action"] == "delete":
                sp.delete_memory(act["id"])
            elif act["action"] == "patch":
                sp.patch_memory(act["id"], {k: v for k, v in act.items() if k in ("content", "category", "tags", "confidence")})
            print("memory", act["action"], act["id"])
        return 0

    memory = None
    if sp is not None:
        raw = sp.list_memory()
        memory = raw if isinstance(raw, list) else (raw or {}).get("items") or (raw or {}).get("memories") or (raw or {}).get("data") or []
        (state_dir / "reports").mkdir(exist_ok=True)
        (state_dir / "reports" / "memory_export.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        md, props = WEEKLY.review(state_dir, date.today(), memory, a.config.resolve())
    except WEEKLY.WCFG.WeightConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    out = state_dir / "reports"
    out.mkdir(exist_ok=True)
    rid = props.get("review_id", props["week"])
    (out / f"weekly_{rid}.md").write_text(md, encoding="utf-8")
    (out / f"proposals_{rid}.json").write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {out}/weekly_{rid}.md")
    print(f"PROPOSAL: {json.dumps(props['proposal'], ensure_ascii=False) if props['proposal'] else '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
