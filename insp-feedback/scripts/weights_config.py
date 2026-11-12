#!/usr/bin/env python3
"""Single source of truth for the two layers of weight parameters (v1.3).

Layer 1  weights.events    : how much one feedback event counts (design doc §10b)       -> used when a verdict is
                             recorded (record_verdict.py) and when acceptance is computed (weekly_review.py,
                             learning_check.py).
Layer 2  weights.learning  : how acceptance turns into a weight change (design doc §10c) -> alpha / beta / lambda /
                             min_n / min_delta / floor / dimensions / review_files.

Both live in config.json (the same file run_local.py / review_server.py use) under the key "weights".
The *learned* values (type / subfield multipliers) are NOT parameters: they stay in inspiration/taste/weights.json,
because they are the output of learning, carry a version history and can be rolled back.

Resolution order (first hit wins, the source is always reported):
  1. explicit --config <file>            2. env INSP_CONFIG=<file>
  3. legacy: state.json["learning"]      (layer 2 only; WARN, migrate with apply_test_profile.py --migrate-weights)
  4. built-in DEFAULTS                   (WARN: only for isolated use, e.g. a skill running on the platform sandbox)

    python3 weights_config.py --config config.json            # print the effective parameters and their source
    python3 weights_config.py --self-test
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_EVENTS = {
    "adopt": 1.0, "start_now": 1.0, "reject": -1.0, "done": -1.0,
    "not_important": -0.5,   # not in the design doc's table: an assumption
    "edited": 0.8, "ask_detail": 0.5, "read_no_action": -0.2, "unread": -0.1,
}
DEFAULT_LEARNING = {
    "profile": "prod", "alpha": 2.0, "beta": 2.0, "lambda": 0.3, "min_n": 5,
    "min_delta": 0.01, "floor": 0.1, "dimensions": ["type", "subfield"], "review_files": "weekly",
}
TEST_LEARNING = dict(DEFAULT_LEARNING, profile="test", **{"lambda": 1.0}, review_files="per_review")
DIMENSIONS = {"type", "subfield"}


class WeightConfigError(ValueError):
    pass


def default_block() -> dict:
    return {"events": dict(DEFAULT_EVENTS), "learning": copy.deepcopy(DEFAULT_LEARNING)}


def check(block: dict) -> list[str]:
    """Return a list of problems; empty = valid. Missing keys are errors (no silent defaults inside config)."""
    errs: list[str] = []
    if not isinstance(block, dict):
        return ["weights 必须是对象"]
    ev = block.get("events")
    if not isinstance(ev, dict):
        errs.append("weights.events 缺失或不是对象")
    else:
        for k in sorted(set(DEFAULT_EVENTS) - set(ev)):
            errs.append(f"weights.events 缺少 {k}")
        for k in sorted(set(ev) - set(DEFAULT_EVENTS)):
            if not k.startswith("_"):
                errs.append(f"weights.events 有未知动作 {k}（可选：{', '.join(DEFAULT_EVENTS)}）")
        for k, v in ev.items():
            if k.startswith("_"):
                continue
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not -1.0 <= float(v) <= 1.0:
                errs.append(f"weights.events.{k}={v!r} 必须是 -1 到 1 之间的数")
    le = block.get("learning")
    if not isinstance(le, dict):
        errs.append("weights.learning 缺失或不是对象")
        return errs
    for k in ("alpha", "beta", "lambda", "min_n", "min_delta", "floor", "dimensions"):
        if k not in le:
            errs.append(f"weights.learning 缺少 {k}")
    num = lambda k: isinstance(le.get(k), (int, float)) and not isinstance(le.get(k), bool)
    for k in ("alpha", "beta"):
        if k in le and not (num(k) and le[k] > 0):
            errs.append(f"weights.learning.{k} 必须 > 0")
    if "lambda" in le and not (num("lambda") and 0 < le["lambda"] <= 1):
        errs.append("weights.learning.lambda 必须在 (0, 1] 内")
    if "min_n" in le and not (isinstance(le["min_n"], int) and not isinstance(le["min_n"], bool) and le["min_n"] >= 1):
        errs.append("weights.learning.min_n 必须是 ≥1 的整数")
    if "min_delta" in le and not (num("min_delta") and 0 <= le["min_delta"] < 1):
        errs.append("weights.learning.min_delta 必须在 [0, 1) 内")
    if "floor" in le and not (num("floor") and 0 < le["floor"] <= 1):
        errs.append("weights.learning.floor 必须在 (0, 1] 内")
    if "dimensions" in le and (not isinstance(le["dimensions"], list) or not le["dimensions"]
                               or set(le["dimensions"]) - DIMENSIONS):
        errs.append(f"weights.learning.dimensions 只能取 {sorted(DIMENSIONS)}")
    if le.get("review_files", "weekly") not in ("weekly", "per_review"):
        errs.append("weights.learning.review_files 只能是 weekly / per_review")
    return errs


def config_path(explicit: Path | str | None = None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("INSP_CONFIG", "").strip()
    return Path(env) if env else None


def load(explicit: Path | str | None = None, state: dict | None = None, quiet: bool = False) -> tuple[dict, dict]:
    """Return (block, source) where block = {"events": {...}, "learning": {...}} and source says where each layer
    came from. Raises WeightConfigError when a config file has a broken "weights" section."""
    p = config_path(explicit)
    block, source = default_block(), {"events": "default", "learning": "default", "config": None}
    if p is not None:
        if not p.is_file():
            raise WeightConfigError(f"找不到配置文件 {p}")
        try:
            cfg = json.loads(p.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise WeightConfigError(f"{p} 不是合法 JSON：{exc}") from exc
        source["config"] = str(p)
        if "weights" in cfg:
            errs = check(cfg["weights"])
            if errs:
                raise WeightConfigError(f"{p.name} 的 weights 配置有误：" + "；".join(errs))
            block = {"events": {k: float(v) for k, v in cfg["weights"]["events"].items() if not k.startswith("_")},
                     "learning": dict(DEFAULT_LEARNING, **{k: v for k, v in cfg["weights"]["learning"].items()
                                                           if not k.startswith("_")})}
            source.update(events=f"config:{p.name}", learning=f"config:{p.name}")
            return block, source
    legacy = (state or {}).get("learning")
    if isinstance(legacy, dict) and legacy:
        block["learning"] = dict(DEFAULT_LEARNING, **legacy)
        source["learning"] = "legacy:state.json.learning"
    if not quiet:
        where = f"{p.name} 里没有 weights 段" if p is not None else "没有给 --config / INSP_CONFIG"
        print(f"WARN 权重参数：{where}，事件权重用内置默认值，学习参数来自 {source['learning']}。"
              f"迁移：python platform/apply_test_profile.py --config <config> --state-dir <state> --migrate-weights",
              file=sys.stderr)
    return block, source


def effective_weight(verdict: dict, events: dict) -> float:
    """Learning uses the CURRENT config weight of the action, so editing config re-weights history consistently.
    The weight stored in the verdict row is a snapshot for audit only."""
    a = verdict.get("action")
    return float(events[a]) if a in events else float(verdict.get("weight", 0.0))


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        good = {"weights": default_block()}
        (t / "c.json").write_text(json.dumps(good), encoding="utf-8")
        b, s = load(t / "c.json")
        assert s["events"] == "config:c.json" and b["learning"]["lambda"] == 0.3, s
        bad = json.loads(json.dumps(good))
        bad["weights"]["events"]["adopt"] = 3
        del bad["weights"]["learning"]["min_n"]
        bad["weights"]["events"]["like"] = 1
        (t / "b.json").write_text(json.dumps(bad), encoding="utf-8")
        try:
            load(t / "b.json")
            raise AssertionError("broken config must raise")
        except WeightConfigError as exc:
            msg = str(exc)
            assert "adopt" in msg and "min_n" in msg and "like" in msg, msg
        (t / "n.json").write_text("{}", encoding="utf-8")
        b, s = load(t / "n.json", state={"learning": {"lambda": 1.0}}, quiet=True)
        assert s["learning"] == "legacy:state.json.learning" and b["learning"]["lambda"] == 1.0 and b["events"]["adopt"] == 1.0
        assert effective_weight({"action": "edited", "weight": 0.8}, dict(DEFAULT_EVENTS, edited=0.6)) == 0.6
        assert effective_weight({"action": "zzz", "weight": 0.3}, DEFAULT_EVENTS) == 0.3
        assert not check(default_block()) and not check({"events": DEFAULT_EVENTS, "learning": TEST_LEARNING})
    print("SELF-TEST OK: weights_config")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="打印生效的两层权重参数及其来源")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--state-dir", type=Path)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    state = None
    if a.state_dir and (a.state_dir / "state.json").is_file():
        state = json.loads((a.state_dir / "state.json").read_text(encoding="utf-8"))
    try:
        block, src = load(a.config, state)
    except WeightConfigError as exc:
        print("ERROR", exc)
        return 2
    print(json.dumps({"source": src, **block}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
