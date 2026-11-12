#!/usr/bin/env python3
"""Validate an inspiration/ state directory (state contract v1.0)."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REQUIRED_FILES = [
    "state.json",
    "negative.jsonl",
    "verdicts.jsonl",
    "taste/rubric.md",
    "taste/weights.json",
    "profile/interests.md",
    "profile/claims.md",
    "profile/observed.md",
]
STATE_KEYS = {
    "schema_version", "bootstrap", "last_run_at", "last_run_id", "sources", "keywords",
    "seen_ids", "pushed", "quota", "quota_used", "triage", "revival",
}
CARD_TYPES = ("optimization", "knowledge", "hypothesis", "process")


def read_jsonl(path: Path, errors: list[str]) -> list[dict]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name} 第 {i} 行不是合法 JSON：{exc}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"{path.name} 第 {i} 行必须是对象")
            continue
        rows.append(obj)
    return rows


def rubric_rule_ids(text: str) -> list[str]:
    return re.findall(r"^##\s+(R\d{2})\b", text, flags=re.MULTILINE)


def validate_state(state: Any, errors: list[str]) -> None:
    if not isinstance(state, dict):
        errors.append("state.json 根节点必须是对象")
        return
    missing = STATE_KEYS - set(state)
    if missing:
        errors.append("state.json 缺少字段：" + ", ".join(sorted(missing)))
        return
    if state["schema_version"] != "1.0":
        errors.append("state.json schema_version 必须是 1.0")
    if not isinstance(state["bootstrap"], bool):
        errors.append("bootstrap 必须是布尔值")
    if not isinstance(state["seen_ids"], list):
        errors.append("seen_ids 必须是数组")
    elif not state["seen_ids"] and not state["bootstrap"]:
        errors.append("seen_ids 为空但 bootstrap=false：状态疑似被截断，禁止运行")
    if state["last_run_at"] and not state["seen_ids"]:
        errors.append("last_run_at 不为空但 seen_ids 为空：状态疑似被截断")
    if not isinstance(state["keywords"], list) or not state["keywords"]:
        errors.append("keywords 至少需要 1 个关键词")
    if not isinstance(state["sources"], list) or not state["sources"]:
        errors.append("sources 至少需要 1 个来源")
    if not isinstance(state["pushed"], list):
        errors.append("pushed 必须是数组")
    else:
        for i, p in enumerate(state["pushed"]):
            if not isinstance(p, dict) or not {"id", "card_id", "type"} <= set(p):
                errors.append(f"pushed[{i}] 需要 id / card_id / type")
    quota = state["quota"]
    if not isinstance(quota, dict) or "daily_total_max" not in quota:
        errors.append("quota.daily_total_max 缺失")
    else:
        for t in CARD_TYPES:
            if t not in quota:
                errors.append(f"quota 缺少类型 {t}")
    tri = state["triage"]
    if not isinstance(tri, dict) or not {"min_relevance", "max_shortlist"} <= set(tri):
        errors.append("triage 需要 min_relevance 与 max_shortlist")
    rev = state["revival"]
    if not isinstance(rev, dict) or not isinstance(rev.get("every"), int) or rev.get("every", 0) < 2:
        errors.append("revival.every 必须是 ≥2 的整数（复活机制不可关闭）")


def validate_dir(root: Path) -> list[str]:
    errors: list[str] = []
    if not root.is_dir():
        return [f"{root} 不是目录"]
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            errors.append(f"缺少文件：{rel}")
    if errors:
        return errors
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"state.json 无法解析：{exc}"]
    validate_state(state, errors)
    for row in read_jsonl(root / "negative.jsonl", errors):
        if row.get("kind") not in {"id", "topic"} or not row.get("value"):
            errors.append(f"negative.jsonl 条目需要 kind∈{{id,topic}} 且 value 非空：{row}")
    read_jsonl(root / "verdicts.jsonl", errors)
    ids = rubric_rule_ids((root / "taste/rubric.md").read_text(encoding="utf-8"))
    if len(ids) < 3:
        errors.append("rubric.md 至少需要 3 条 '## Rxx' 准则")
    if len(ids) != len(set(ids)):
        errors.append("rubric.md 存在重复的准则编号")
    try:
        weights = json.loads((root / "taste/weights.json").read_text(encoding="utf-8"))
        if not isinstance(weights.get("type"), dict):
            errors.append("weights.json 需要 type 对象")
    except json.JSONDecodeError as exc:
        errors.append(f"weights.json 无法解析：{exc}")
    return errors


def self_test() -> int:
    template = Path(__file__).resolve().parents[1] / "assets" / "灵感状态模板" / "inspiration"
    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "good"
        shutil.copytree(template, good)
        errs = validate_dir(good)
        if errs:
            print("SELF-TEST FAIL: 模板应通过", errs)
            return 1
        bad = Path(tmp) / "bad"
        shutil.copytree(template, bad)
        st = json.loads((bad / "state.json").read_text(encoding="utf-8"))
        st["bootstrap"] = False
        st["last_run_at"] = "2026-10-27T02:40:00+08:00"
        (bad / "state.json").write_text(json.dumps(st), encoding="utf-8")
        (bad / "negative.jsonl").unlink()
        errs = validate_dir(bad)
        need = ["缺少文件：negative.jsonl"]
        if not all(any(n in e for e in errs) for n in need):
            print("SELF-TEST FAIL: 反例应报缺少 negative.jsonl", errs)
            return 1
        (bad / "negative.jsonl").write_text("", encoding="utf-8")
        errs = validate_dir(bad)
        if not any("截断" in e for e in errs):
            print("SELF-TEST FAIL: 反例应报状态截断", errs)
            return 1
    print("SELF-TEST OK: validate_insp_state")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 inspiration/ 状态目录")
    parser.add_argument("state_dir", nargs="?", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.state_dir is None:
        parser.error("必须提供 state_dir，或使用 --self-test")
    errors = validate_dir(args.state_dir)
    if errors:
        print("INVALID:")
        for e in errors:
            print(f"- {e}")
        return 1
    print(f"OK: {args.state_dir} 状态目录有效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
