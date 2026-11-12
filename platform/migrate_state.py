#!/usr/bin/env python3
"""把已有的状态目录 / config.json 升级到 1.5.1 的默认值（幂等，改之前先备份，只用标准库）。

    python platform/migrate_state.py --config config.json            # 只看要改什么（默认 dry-run）
    python platform/migrate_state.py --config config.json --apply    # 真的改（先写 .bak-<时间> 备份）

只改“还停在旧默认值”的项，你自己改过的值一律不动：
  state.json  quota.optimization.per_day   3 → 999      （A 类不设硬上限，§8）
  state.json  quota.knowledge.per_run      缺 → 5       （v1.5.3：B 类每次最多 5 张；不迁移也按 5 处理）
  state.json  observed.a_floor             缺 → true    （每轮至少 1 张 A 类）
  state.json  observed.a_floor_min_severity 缺 → 1.0
  config.json actions.start_now_mode       缺 → "form"  （按 6 先弹出动手单，确认后开跑；"direct" = 完整就直接建任务）
  config.json actions.attach_local_files   缺 → true    （观察型卡片把本地证据文件附给任务）
  config.json actions.max_local_file_mb    缺 → 2
  config.json observe.code.max_per_file / max_args / max_smell_files  缺 → 3 / 7 / 5
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


def plan(cfg: dict, state: dict) -> tuple[list[str], dict, dict]:
    notes: list[str] = []
    st = json.loads(json.dumps(state))
    cf = json.loads(json.dumps(cfg))
    q = st.setdefault("quota", {}).setdefault("optimization", {})
    if q.get("per_day", 3) == 3:
        q["per_day"] = 999
        notes.append("state.json quota.optimization.per_day: 3 → 999（A 类不设硬上限）")
    kq = st["quota"].setdefault("knowledge", {})
    if "per_run" not in kq:   # v1.5.3
        kq["per_run"] = 5
        notes.append("state.json quota.knowledge.per_run: 缺 → 5（B 类每次最多 5 张）")
    ob = st.get("observed")
    if isinstance(ob, dict):
        for k, v in (("a_floor", True), ("a_floor_min_severity", 1.0)):
            if k not in ob:
                ob[k] = v
                notes.append(f"state.json observed.{k}: 缺 → {json.dumps(v)}")
    ac = cf.setdefault("actions", {})
    for k, v in (("start_now_mode", "form"), ("attach_local_files", True), ("max_local_file_mb", 2)):
        if k not in ac:
            ac[k] = v
            notes.append(f"config.json actions.{k}: 缺 → {json.dumps(v)}")
    if isinstance(cf.get("observe"), dict):
        code = cf["observe"].setdefault("code", {})
        for k, v in (("max_per_file", 3), ("max_args", 7), ("max_smell_files", 5)):
            if k not in code:
                code[k] = v
                notes.append(f"config.json observe.code.{k}: 缺 → {v}")
    return notes, cf, st


def fix_half_runs(state_dir: Path, apply: bool) -> list[str]:
    """v1.5.4: jobs from v1.5.2 can carry run rows with run_status=None although the job ended long ago."""
    notes = []
    for f in sorted((state_dir / "actions").glob("*/job.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if j.get("status") not in ("succeeded", "failed", "timeout", "blocked", "plan_ready", "cancelled", "rejected_plan",
                                   "needs_approval"):
            continue
        bad = [r for r in j.get("runs") or [] if isinstance(r, dict) and r.get("run_status") is None]
        if not bad:
            continue
        notes.append(f"actions/{f.parent.name}/job.json：{len(bad)} 条 run_status=None → \"unknown\"（旧版本留下的半截记录）")
        if apply:
            for r in bad:
                r["run_status"] = "unknown"
                r.setdefault("finished_at", j.get("finished_at") or j.get("cancelled_at"))
            f.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--state-dir", type=Path, help="不给就用 config.state_dir")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        n, cf, st = plan({"actions": {"start_now_mode": "confirm"}, "observe": {"code": {}}},
                         {"quota": {"optimization": {"per_day": 3}}, "observed": {"per_type": 2}})
        assert st["quota"]["optimization"]["per_day"] == 999 and st["observed"]["a_floor"] is True
        assert cf["actions"]["start_now_mode"] == "confirm" and cf["actions"]["attach_local_files"] is True
        assert cf["observe"]["code"]["max_per_file"] == 3
        n2, _, _ = plan(cf, st)
        assert not n2, n2
        n3, _, st3 = plan({}, {"quota": {"optimization": {"per_day": 5}}})
        assert st3["quota"]["optimization"]["per_day"] == 5, "用户改过的值不能动"
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            jd = Path(tmp) / "actions" / "act_1"
            jd.mkdir(parents=True)
            (jd / "job.json").write_text(json.dumps({"status": "cancelled", "finished_at": "x",
                                                     "runs": [{"agent_run_id": "r", "run_status": None}]}), encoding="utf-8")
            assert len(fix_half_runs(Path(tmp), True)) == 1 and not fix_half_runs(Path(tmp), True)
            assert json.loads((jd / "job.json").read_text(encoding="utf-8"))["runs"][0]["run_status"] == "unknown"
        print("SELF-TEST OK: migrate_state")
        return 0
    if not a.config:
        ap.error("需要 --config")
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    sd = a.state_dir or (a.config.resolve().parent / cfg.get("state_dir", "./inspiration")).resolve()
    sp = sd / "state.json"
    state = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else {}
    notes, cf, st = plan(cfg, state)
    run_notes = fix_half_runs(sd, False)
    if run_notes:
        print(("将要修复：" if not a.apply else "已修复：") + "\n  " + "\n  ".join(run_notes))
        fix_half_runs(sd, a.apply)
    if not sp.is_file():
        notes = [x for x in notes if not x.startswith("state.json")]
        print(f"（没有 {sp}，只处理 config.json）")
    if not notes:
        print("已经是 1.5.1 默认值，不用改。")
        return 0
    print(("将要修改：" if not a.apply else "已修改：") + "\n  " + "\n  ".join(notes))
    if not a.apply:
        print("\n加 --apply 才会真的改（改前自动备份）。")
        return 0
    tag = datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(a.config, a.config.with_name(a.config.name + f".bak-{tag}"))
    a.config.write_text(json.dumps(cf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if sp.is_file():
        shutil.copy2(sp, sp.with_name(f"state.json.bak-{tag}"))
        sp.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"备份：*.bak-{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
