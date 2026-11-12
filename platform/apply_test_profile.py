#!/usr/bin/env python3
"""Switch the inspiration state between the production profile and the accelerated learning-loop test profile.

Usage (from the package root):
  python platform/apply_test_profile.py --state-dir inspiration --profile test \
      --subfields "{\"memory\": [\"memory\"], \"retrieval\": [\"retrieval\", \"rag\"], \"eval\": [\"benchmark\", \"evaluation\"]}"
  python platform/apply_test_profile.py --state-dir inspiration --profile prod
  python platform/apply_test_profile.py --state-dir inspiration --show

Input : config.json (weights.learning), inspiration/state.json, inspiration/taste/weights.json
Output: the same files, modified in place; backups config.json.bak-<ts> / state.json.bak-<ts> / weights.json.bak-<ts>

v1.3: the learning parameters moved from state.json["learning"] to config.json["weights"]["learning"]
(single source of truth, see insp-feedback/scripts/weights_config.py). One-time migration:
  python platform/apply_test_profile.py --config config_exp.json --migrate-weights

test profile (why each setting exists):
  quota_profile=test         per-run slots instead of weekly quotas, otherwise hypothesis (1/week) can never reach n>=5
  quota_test.per_run_max=5   5 cards per run x 8 runs = up to 40 cards in 4 days
  per_type_per_run=2         keeps types balanced so every type collects data
  explore_floor=true         best card of every type is kept first -> no type starves (design doc §10f spirit)
  learning.lambda=1.0        production uses 0.3; 1.0 makes a single adjustment large enough to be observable
  learning.min_n=5           the sample discipline is NOT relaxed
  triage.max_new_per_run=25  backlog pacing: unseen papers are spread over runs, so the 2nd daily run is not empty
  sources[].max_results>=150 enough backlog for 8 runs
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
from datetime import datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location("wcfg_atp", Path(__file__).resolve().parents[1] / "insp-feedback/scripts/weights_config.py")
WCFG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(WCFG)  # type: ignore[union-attr]

TEST = {
    "quota_profile": "test",
    "quota_test": {"per_run_max": 5, "per_type_per_run": 2, "daily_total_max": 12, "explore_floor": True,
                   "agent_max_cards": 8, "target_types": ["knowledge", "hypothesis", "process"]},
}
# learning parameters now go to config.json -> weights.learning
TEST_LEARNING = dict(WCFG.TEST_LEARNING)
PROD_LEARNING = dict(WCFG.DEFAULT_LEARNING)


def write_json(p: Path, obj: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def set_learning(cfg_path: Path, cfg: dict, learning: dict | None, legacy: dict | None, ts: str) -> dict:
    """Make sure config has a complete weights section; optionally overwrite weights.learning."""
    shutil.copy(cfg_path, cfg_path.with_name(f"{cfg_path.name}.bak-{ts}"))
    w = cfg.get("weights") or {}
    events = w.get("events") or dict(WCFG.DEFAULT_EVENTS)
    le = learning or w.get("learning") or dict(WCFG.DEFAULT_LEARNING, **(legacy or {}))
    cfg["weights"] = {"_说明": "两层权重参数：events=单条反馈事件的权重；learning=采纳率→权重调整的参数。"
                             "学到的权重值本身在 inspiration/taste/weights.json，不在这里。",
                      "events": events, "learning": dict(WCFG.DEFAULT_LEARNING, **le)}
    errs = WCFG.check(cfg["weights"])
    if errs:
        raise SystemExit("ERROR weights 配置无效：" + "；".join(errs))
    write_json(cfg_path, cfg)
    return cfg["weights"]


def main() -> int:
    ap = argparse.ArgumentParser(description="切换 测试/生产 配置")
    ap.add_argument("--config", type=Path, help="config.json / config_exp.json（学习参数写在这里的 weights.learning）")
    ap.add_argument("--state-dir", type=Path, help="默认取 config 里的 state_dir")
    ap.add_argument("--profile", choices=["test", "prod"])
    ap.add_argument("--migrate-weights", action="store_true", help="一次性：把 state.json 里的 learning 挪进 config.json 的 weights 段")
    ap.add_argument("--subfields", help='subfields.json 文件路径（推荐，PowerShell 里不用转义引号），或 JSON 字符串 {"子方向名": ["关键词", ...]}；不给就每个 keyword 一个子方向')
    ap.add_argument("--max-new-per-run", type=int, default=25)
    ap.add_argument("--min-results", type=int, default=150)
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    cfg = None
    if a.config:
        cfg = json.loads(a.config.read_text(encoding="utf-8-sig"))
        if a.state_dir is None:
            a.state_dir = (a.config.resolve().parent / cfg["state_dir"]).resolve()
    if a.state_dir is None:
        ap.error("需要 --config（推荐）或 --state-dir")
    if (a.profile or a.migrate_weights) and cfg is None:
        ap.error("v1.3 起学习参数在 config.json 的 weights 段：切换配置档 / 迁移需要 --config")

    sf, wf = a.state_dir / "state.json", a.state_dir / "taste" / "weights.json"
    st = json.loads(sf.read_text(encoding="utf-8"))
    w = json.loads(wf.read_text(encoding="utf-8"))
    if a.show or not (a.profile or a.migrate_weights):
        print(json.dumps({k: st.get(k) for k in ("quota_profile", "quota_test", "subfields", "triage", "sources")},
                         ensure_ascii=False, indent=2))
        try:
            block, src = WCFG.load(a.config.resolve() if a.config else None, st)
            print("weights 参数（来源 " + json.dumps(src, ensure_ascii=False) + "）:")
            print(json.dumps(block, ensure_ascii=False, indent=2))
        except WCFG.WeightConfigError as exc:
            print("ERROR", exc)
        print("learned weights.json:", json.dumps({k: w.get(k) for k in ("version", "type", "subfield")}, ensure_ascii=False))
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy(sf, sf.with_name(f"state.json.bak-{ts}"))
    shutil.copy(wf, wf.with_name(f"weights.json.bak-{ts}"))
    legacy = st.pop("learning", None)  # v1.3: never kept in state.json any more

    if a.migrate_weights and not a.profile:
        wb = set_learning(a.config, cfg, None, legacy, ts)
        write_json(sf, st)
        print(f"OK: weights 段已写入 {a.config.name}（学习参数来自 {'state.json.learning' if legacy else '默认值'}）；"
              f"state.json 里的 learning 已移除。备份 {a.config.name}.bak-{ts} / state.json.bak-{ts}")
        print(json.dumps(wb, ensure_ascii=False, indent=2))
        return 0

    if a.profile == "test":
        st.update(json.loads(json.dumps(TEST)))
        st.setdefault("triage", {})["max_new_per_run"] = a.max_new_per_run
        st["triage"]["max_shortlist"] = max(int(st["triage"].get("max_shortlist", 8)), 10)
        for src in st.get("sources", []):
            src["max_results"] = max(int(src.get("max_results", 50)), a.min_results)
        if a.subfields:
            p = Path(a.subfields)
            st["subfields"] = json.loads(p.read_text(encoding="utf-8") if p.suffix == ".json" and p.exists() else a.subfields)
        elif not st.get("subfields"):
            kws = st.get("keywords", [])[:4]
            st["subfields"] = {k: [k] for k in kws}
            print(f"WARN: 未给 --subfields，暂用 keywords 各自成组：{list(st['subfields'])}。建议按你的方向手动分 2–4 组。")
        set_learning(a.config, cfg, TEST_LEARNING, legacy, ts)
    else:
        st["quota_profile"] = "prod"
        st.get("triage", {}).pop("max_new_per_run", None)
        set_learning(a.config, cfg, PROD_LEARNING, legacy, ts)

    w.setdefault("version", 0)
    w.setdefault("type", {}).update({t: w["type"].get(t, 1.0) for t in ("optimization", "knowledge", "hypothesis", "process")})
    w.setdefault("subfield", {})
    for name in st.get("subfields", {}):
        w["subfield"].setdefault(name, 1.0)
    w["subfield"].setdefault("other", 1.0)
    for p, obj in ((sf, st), (wf, w)):
        write_json(p, obj)
    print(f"OK: profile={a.profile}；学习参数写入 {a.config.name} → weights.learning（λ={(TEST_LEARNING if a.profile == 'test' else PROD_LEARNING)['lambda']}）；"
          f"备份 {a.config.name}.bak-{ts} / state.json.bak-{ts} / weights.json.bak-{ts}")
    print("subfields:", json.dumps(st.get("subfields"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
