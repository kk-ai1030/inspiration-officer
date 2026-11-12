#!/usr/bin/env python3
"""Offline dress rehearsal of the 4-day learning-loop experiment (no network, no platform, ~10 s).

It runs the REAL scripts in the same order you will run them for real:
  apply_test_profile → run_local ×N (fake agent) → fill verdict_inbox.csv → record_verdict --batch
  → learning_check (PROGRESS) → run_weekly → confirm → weekly_review --apply → run_local ×4 → verdicts
  → run_weekly → learning_check --expected (must be PASS) → weekly_review --rollback

The simulated user follows a fixed, pre-registered preference (expected.json):
  hypothesis → 采纳 ; process → 否决 ; knowledge → 采纳 if subfield == memory else 不重要
Usage:  python tests/learning_loop_sim.py [--keep DIR]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
PY = sys.executable
TYPES = ["knowledge", "hypothesis", "process"]
SUB = {"memory": ["memory"], "retrieval": ["retrieval", "rag"], "eval": ["benchmark", "evaluation"]}
TITLE_WORDS = ("scalable sparse adaptive robust causal hierarchical streaming federated contrastive neural symbolic "
               "temporal multimodal lightweight efficient grounded interactive compositional bayesian continual "
               "lifelong episodic structured latent").split()
WORDS = "agents plan tools across long horizons and store experience for reuse while the system tracks errors carefully".split()


def sh(args: list[str], cwd: Path, ok=(0,)) -> str:
    p = subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONUTF8="1"))
    if p.returncode not in ok:
        print(p.stdout, p.stderr)
        raise SystemExit(f"FAIL: {' '.join(args)} → exit {p.returncode}")
    return p.stdout


REL_ID = "2611.99001"
REL_SENT = "A shared related study measures how stored experience is reused by agents across long task horizons."


def body_sentence(i: int) -> str:
    return f"In our fourth section we ablate component number {i} and report that recall gating changes the solve rate."


def write_fulltext_cache(cache: Path, pool: list[dict]) -> None:
    """Offline stand-in for arXiv HTML: every pool paper gets a full text that cites one shared related paper."""
    cache.mkdir(parents=True, exist_ok=True)
    filler = " ".join(["This paragraph describes the setup, datasets, metrics and controls of the experiment."] * 50)
    docs = {REL_ID: ("Shared Related Study", f"## 3 Results\n{REL_SENT} {filler}\n")}
    for i, p in enumerate(pool):
        docs[p["id"].split("v")[0]] = (p["title"], f"## 4 Experiments\n{body_sentence(i)} {filler}\n"
                                                    f"## References\n[1] Shared study. arXiv:{REL_ID}.\n")
    for ref, (title, body) in docs.items():
        md = f"# {title}\n\n###### Abstract\nshort\n\n{body}"
        (cache / f"{ref}.md").write_text(md, encoding="utf-8")
        (cache / f"{ref}.json").write_text(json.dumps({"ref": ref, "status": "ok", "method": "html", "url": "offline", "title": title,
                                                      "chars": len(md), "body_chars": len(md), "sha256": "offline",
                                                      "sections": [title, "Abstract", "4 Experiments", "3 Results", "References"]}),
                                           encoding="utf-8")
        (cache / f"{ref}.related.json").write_text("[]", encoding="utf-8")


def make_pool(n: int = 220) -> list[dict]:
    rnd = random.Random(7)
    pool = []
    for i in range(n):
        sub = list(SUB)[i % 3]
        kw = SUB[sub][i % len(SUB[sub])]
        body = " ".join(rnd.choice(WORDS) for _ in range(40))
        title_words = rnd.sample(TITLE_WORDS, 5)
        pool.append({"id": f"2611.{10000 + i}v1", "title": f"{' '.join(title_words[:3]).title()} {kw} {' '.join(title_words[3:])}",
                     "abstract": f"We study {kw} in agent systems. {body} We release code and a simple baseline.",
                     "url": f"https://arxiv.org/abs/2611.{10000 + i}", "published": "2026-10-27", "source": "arxiv"})
    return pool


def fake_reply(shortlist: list[dict], run_no: int) -> str:
    cards = []
    for k, c in enumerate(shortlist[:8]):
        t = TYPES[(k + run_no) % 3]
        words = c["abstract"].split()
        cards.append({
            "id": f"ins_{datetime.now():%Y%m%d}_{k + 1:03d}", "type": t, "created_at": datetime.now().astimezone().isoformat(),
            "source": {"kind": "arxiv", "ref": c["raw_id"] or c["id"], "quote": " ".join(words[:12]), "verified": False,
                       "fulltext_quotes": [{"quote": body_sentence(int(c["id"].split(".")[1][:5]) - 10000), "section": "4 Experiments"}]},
            "related_evidence": [{"ref": REL_ID, "relation": "context", "quote": REL_SENT, "why": "共享的相关研究"}],
            "title": f"卡片 {c['id']} {t}", "one_line": f"关于 {c['subfield']} 的建议",
            "why_me": "与当前项目的经验复用直接相关。", "hypothesis": "机制假说：检索门控优于常驻注入。" if t == "hypothesis" else "",
            "minimal_check": "在现有基准上加一组对照跑一轮", "expected_observable": "对照组明显更好则成立",
            "falsifier": "对照组不优于基线则不成立", "magnitude": "未给出", "cost_estimate": {"tokens": "~40k", "wall_clock": "1d", "risk": "low"},
            "rubric_scores": [{"rule_id": "R01", "score": 3 + (k % 3), "reason": "相关"}, {"rule_id": "R02", "score": 4, "reason": "判据"},
                              {"rule_id": "R07", "score": 3 + ((k + 1) % 2), "reason": "可证伪"}],
            "numbers_provenance": [], "status": "proposed", "verdict": None})
    doc = {"schema_version": "1.0", "flow_version": "insp-card/1.1.0", "run_id": f"sim-{run_no}",
           "generated_at": datetime.now().astimezone().isoformat(), "status": "OK", "cards": cards, "rejected": []}
    return f"已加载 insp-card。\n<<<INSP_CARDS_JSON\n{json.dumps(doc, ensure_ascii=False)}\nINSP_CARDS_JSON>>>\n"


def one_run(tmp: Path, run_no: int, triage_mod) -> str:
    _, tri = triage_mod.run_triage(tmp / "inspiration", json.loads((tmp / "pool.json").read_text(encoding="utf-8")))
    (tmp / "reply.txt").write_text(fake_reply(tri["shortlisted"], run_no), encoding="utf-8")
    return sh(["platform/run_local.py", "--config", str(tmp / "config.json"), "--raw", str(tmp / "pool.json"),
               "--fake-agent-output", str(tmp / "reply.txt"), "--no-arxiv-check"], PKG)


def judge_inbox(tmp: Path) -> int:
    """Simulated user: opens verdict_inbox.csv in Excel, fills the action column, saves as GBK (Excel 'CSV')."""
    p = tmp / "inspiration" / "inbox" / "verdict_inbox.csv"
    rows = list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8-sig"))))
    for r in rows:
        r["action"] = {"hypothesis": "采纳", "process": "否决"}.get(r["type"]) or ("采纳" if r["subfield"] == "memory" else "不重要")
        r["reason"] = "按预先登记的偏好"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["card_id"])
    w.writeheader()
    w.writerows(rows)
    p.write_bytes(buf.getvalue().encode("gbk"))
    sh(["insp-feedback/scripts/record_verdict.py", "--config", str(tmp / "config.json"), "--state-dir", str(tmp / "inspiration"), "--batch", str(p)], PKG)
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=Path)
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp()) if not a.keep else a.keep
    if a.keep and tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", tmp / "inspiration")
    (tmp / "inspiration" / ".test_state").write_text("", encoding="utf-8")  # fixtures allowed here (BUG-6 guard)
    st = json.loads((tmp / "inspiration/state.json").read_text(encoding="utf-8"))
    st["keywords"] = ["memory", "retrieval", "rag", "benchmark", "evaluation"]
    (tmp / "inspiration/state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    cfg = json.loads((PKG / "platform/config.example.json").read_text(encoding="utf-8"))
    cfg["review"]["enabled"] = False  # no toast / review server during tests
    cfg["fulltext"]["cache_dir"] = "./ftcache"
    cfg["token_file"], cfg["setup_token_file"] = "./no_run_token", "./no_session_token"  # never read real tokens
    cfg["arxiv_fetch_mode"] = "fulltext"  # the simulated agent writes full-text evidence; keep that path covered
    # 全文模式默认每次只读 4 篇（v1.4.3 起），而测试档每次运行有 5 个名额：候选不够时所有卡都会入选，
    # 权重没有竞争，反事实重放（L4b）永远看不到差异。彩排里放宽到 8 篇，恢复“名额竞争”。
    cfg["fulltext"]["max_candidates"] = 8
    cfg.update(state_dir="./inspiration", work_dir="./runs")
    (tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    pool = make_pool()
    (tmp / "pool.json").write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    write_fulltext_cache(tmp / "ftcache", pool)
    (tmp / "subfields.json").write_text(json.dumps(SUB), encoding="utf-8")
    (tmp / "expected.json").write_text(json.dumps({"type.hypothesis": "up", "type.process": "down"}), encoding="utf-8")

    sh(["platform/apply_test_profile.py", "--config", str(tmp / "config.json"), "--profile", "test", "--subfields", str(tmp / "subfields.json")], PKG)
    cfg_now = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
    st_now = json.loads((tmp / "inspiration/state.json").read_text(encoding="utf-8"))
    assert cfg_now["weights"]["learning"]["lambda"] == 1.0 and "learning" not in st_now, "测试档的学习参数应写进 config.json"
    print("  学习参数已写入 config.json → weights.learning（λ=1.0），state.json 里没有 learning")
    st = json.loads((tmp / "inspiration/state.json").read_text(encoding="utf-8"))
    st["quota_test"]["daily_total_max"] = 100  # all simulated runs happen on the same calendar day
    (tmp / "inspiration/state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    sys.path.insert(0, str(PKG / "insp-triage/scripts"))
    import triage as triage_mod  # noqa: E402

    lc = ["platform/learning_check.py", "--config", str(tmp / "config.json"), "--expected", str(tmp / "expected.json")]
    run_no = 0
    print("== 阶段 1：收集数据（每次运行后评价）")
    while True:
        run_no += 1
        out = one_run(tmp, run_no, triage_mod)
        n = judge_inbox(tmp)
        print(f"  run {run_no}: {out.strip().splitlines()[-1][:60]} · 评价 {n} 张")
        res = sh(lc, PKG, ok=(0, 1, 2))
        if "P1   PASS" in res or run_no >= 6:
            break
    assert "P1   PASS" in res, res
    print("  PASS  P1 三种类型都达到 n≥5")

    print("== 检查点：复盘 → 确认 → 应用")
    out = sh(["platform/run_weekly.py", "--config", str(tmp / "config.json"), "--offline"], PKG)
    prop_file = sorted((tmp / "inspiration/reports").glob("proposals_*.json"))[-1]
    p = json.loads(prop_file.read_text(encoding="utf-8"))
    assert p["proposal"] and p["proposal"]["dimension"] == "type" and p["proposal"]["key"] in ("hypothesis", "process"), p["proposal"]
    print(f"  提议 {p['proposal']['dimension']}.{p['proposal']['key']} {p['proposal']['from']}→{p['proposal']['to']}")
    r = sh(["insp-review/scripts/weekly_review.py", "--state-dir", str(tmp / "inspiration"), "--apply", str(prop_file)], PKG, ok=(1,))
    assert "REFUSED" in r
    print("  PASS  未确认的提议被拒绝")
    print("  " + sh(["insp-review/scripts/weekly_review.py", "--state-dir", str(tmp / "inspiration"), "--apply", str(prop_file), "--confirm"], PKG).strip())
    r = sh(["insp-review/scripts/weekly_review.py", "--state-dir", str(tmp / "inspiration"), "--apply", str(prop_file)], PKG, ok=(1,))
    assert "已经应用过" in r
    print("  PASS  同一提议不能应用两次")

    print("== 阶段 2：新权重下继续运行")
    for _ in range(4):
        run_no += 1
        out = one_run(tmp, run_no, triage_mod)
        judge_inbox(tmp)
        print(f"  run {run_no}: {out.strip().splitlines()[-1][:60]}")
    sh(["platform/run_weekly.py", "--config", str(tmp / "config.json"), "--offline"], PKG)
    res = sh(lc, PKG, ok=(0, 1, 2))
    print("\n".join("  " + l for l in res.strip().splitlines()))
    assert "OVERALL: PASS" in res, "learning_check 应为 PASS"

    print("== 回滚")
    print("  " + sh(["insp-review/scripts/weekly_review.py", "--state-dir", str(tmp / "inspiration"), "--rollback"], PKG).strip())
    w = json.loads((tmp / "inspiration/taste/weights.json").read_text(encoding="utf-8"))
    assert all(v == 1.0 for v in w["type"].values()), w["type"]
    print("  PASS  回滚后权重恢复")
    print(f"\nALL LEARNING-LOOP SIMULATION CHECKS PASSED  （产物目录：{tmp}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
