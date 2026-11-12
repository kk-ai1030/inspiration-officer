#!/usr/bin/env python3
"""B4 / K3 网页周报：离线回归（零联网）。

验证：脱敏摘要只含聚合数值、网页渲染器拒绝坏摘要与缺失、状态快照与收货员
确实把摘要带进沙箱、技能文档写明「没有摘要就拒绝」。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

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


def seed(sd: Path) -> None:
    verdict = {"ts": "2026-11-12T10:00:00+08:00", "card_id": "ins_b4_1", "source_ref": "9999.12345",
               "type": "knowledge", "subfield": "memory", "action": "adopt", "label": "采纳", "weight": 1.0,
               "weight_source": "default", "explicit": True, "reason": "正好要补基线，别泄出去",
               "rubric_rule_ids": ["R01"], "weights_version": 0}
    (sd / "verdicts.jsonl").write_text(json.dumps(verdict, ensure_ascii=False) + "\n", encoding="utf-8")
    (sd / "outcomes.jsonl").write_text(json.dumps(
        {"ts": "2026-11-12T11:00:00+08:00", "card_id": "ins_b4_1", "outcome": "confirmed",
         "label": "做了，结论成立", "conclusion": "实测提升了三个百分点", "measured": "3%", "via": "cli"},
        ensure_ascii=False) + "\n", encoding="utf-8")


def make_ctx(tmp: Path, sd: Path, rcv):
    cfg = json.loads((ROOT / "platform/config.example.json").read_text(encoding="utf-8"))
    cfg["state_dir"] = str(sd)
    cfg["work_dir"] = str(tmp / "runs")
    cfg["token_file"] = str(tmp / "no_run_token")
    cfg["setup_token_file"] = str(tmp / "no_session_token")
    cfg.setdefault("receiver", {})["push_state"] = False
    cfg["receiver"]["memory_digest"] = False
    p = tmp / "config.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return rcv.Ctx(p)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_b4_"))
    try:
        sd = tmp / "inspiration"
        shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
        seed(sd)
        vs = load("platform/verdict_summary.py", "b4_vsummary")
        wr = load("insp-review/scripts/weekly_review.py", "b4_weekly")
        ps = load("platform/publish_state.py", "b4_publish")
        rcv = load("platform/receiver.py", "b4_receiver")

        print("== B4-0 摘要模块自测")
        check("B4-0 模块自测", vs.self_test() == 0)

        print("== B4-1 脱敏与渲染")
        ctx = SimpleNamespace(state_dir=sd)
        check("B4-1 摘要不存在时文件集合不变", rcv.state_files_for(ctx) == rcv.STATE_FILES)
        p = vs.write(sd, today=date(2026, 11, 12))
        text = p.read_text(encoding="utf-8")
        s = json.loads(text)
        check("B4-1 schema 与聚合字段", s["schema"] == "insp-verdict-summary/1.0" and
              all(k in s for k in ("adoption", "repeat_rate", "feature_stats", "outcome_summary")))
        check("B4-1 提议未确认", s["proposal"] is None or s["proposal"]["confirmed"] is False)
        leaks = ("正好要补基线", "实测提升了三个百分点", "ins_b4_1", "9999.12345", "R01",
                 "C:\\", "/workspace", "@")
        check("B4-1 不含评价原文 / 卡片 / 路径 / 邮箱", not any(x in text for x in leaks), text[:300])
        md = wr.render_from_summary(s)
        check("B4-1 渲染含核心章节", all(x in md for x in ("## 1. 两个核心指标", "## 3. 本周权重调整提议",
                                                        "## 4. 隐式信号与采纳后结果", "边界")))
        check("B4-1 坏摘要被拒绝", _raises(lambda: wr.render_from_summary({"schema": "nope"})))
        check("B4-1 缺失 schema 被拒绝", _raises(lambda: wr.render_from_summary({"adoption": 0.5})))
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "insp-review/scripts/weekly_review.py"),
                            "--summary", str(p)], cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        check("B4-1 CLI 只读渲染", r.returncode == 0 and "## 1. 两个核心指标" in r.stdout, r.stdout + r.stderr)
        check("B4-1 CLI 不写任何文件", not (sd / "reports").exists())
        bad = tmp / "bad.json"
        bad.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
        r2 = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "insp-review/scripts/weekly_review.py"),
                             "--summary", str(bad)], cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30)
        check("B4-1 CLI 对坏摘要退出 2", r2.returncode == 2 and not r2.stdout.strip(), r2.stdout + r2.stderr)

        print("== B4-2 同步链路")
        check("B4-2 摘要存在时加入推送集合", rcv.state_files_for(ctx)[-1] == "verdict_summary.json")
        staged = rcv.stage_state(ctx, tmp / "run")
        check("B4-2 附件里带上摘要", any(f.name == "verdict_summary.json" for f in staged), str(staged))
        check("B4-2 本地快照包含摘要", "verdict_summary.json" in ps.state_files(sd))
        check("B4-2 FLAT 登记摘要", '"verdict_summary.json": "verdict_summary.json"' in
              (ROOT / "insp-nightly/scripts/insp_run.py").read_text(encoding="utf-8"))
        skill = (ROOT / "insp-review/SKILL.md").read_text(encoding="utf-8")
        check("B4-2 技能写明缺失即拒绝", "周报需要在本机运行" in skill and "不得编造" in skill and
              "永远不出本机" in skill and "--summary" in skill)

        print("== B4-3 收货后自动刷新摘要")
        real_ctx = make_ctx(tmp, sd, rcv)
        p.unlink()
        rcv.push_state = lambda *a, **k: 0
        rcv.publish_snapshot = lambda *a, **k: None
        rcv.memory_digest = lambda *a, **k: None
        rcv.settings_memory = lambda *a, **k: None
        rcv.sync_back(real_ctx, SimpleNamespace(thread_id=None, sp=None))
        check("B4-3 sync_back 重新生成摘要", (sd / "verdict_summary.json").is_file() and
              json.loads((sd / "verdict_summary.json").read_text(encoding="utf-8"))["schema"] == vs.SCHEMA)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("B4 OK" if OK else "B4 TESTS FAILED")
    return 0 if OK else 1


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except (ValueError, LookupError, KeyError):
        return True


if __name__ == "__main__":
    raise SystemExit(main())
