#!/usr/bin/env python3
"""Offline test of run_local's arXiv check: Greek/TeX titles pass, wrong titles are dropped LOUDLY,
network trouble (429) keeps the card as verified=false. arXiv itself is patched out (no network)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]


def main() -> int:
    spec = importlib.util.spec_from_file_location("rn", PKG / "platform/run_local.py")
    rn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rn)
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", t / "inspiration")
        (t / "inspiration" / ".test_state").write_text("", encoding="utf-8")  # fixtures allowed here (BUG-6 guard)
        st = json.loads((t / "inspiration/state.json").read_text(encoding="utf-8"))
        st["bootstrap"] = False
        st["seen_ids"] = json.loads((PKG / "tests/fixtures/seen_ids.json").read_text())["seen_ids"]
        (t / "inspiration/state.json").write_text(json.dumps(st), encoding="utf-8")
        cfg = json.loads((PKG / "platform/config.example.json").read_text(encoding="utf-8"))
        cfg["review"]["enabled"] = False
        cfg["fulltext"]["cache_dir"] = str(PKG / "tests/fixtures/fulltext_cache")
        (t / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        reply = (PKG / "tests/fixtures/fake_reply_ok.txt").read_text(encoding="utf-8")
        refs = [c["source"]["ref"] for c in json.loads(reply.split("<<<INSP_CARDS_JSON")[1].split("INSP_CARDS_JSON>>>")[0])["cards"]]
        raw = json.loads((PKG / "tests/fixtures/raw_items.json").read_text(encoding="utf-8"))
        title = {rn.V_CARDS.norm_id(x["id"]): x["title"] for x in raw}

        RAW = [PKG / "tests/fixtures/raw_items.json"]

        def run(behaviour: dict) -> tuple[list, dict]:
            shutil.rmtree(t / "inspiration/cards", ignore_errors=True)
            (t / "inspiration/state.json").write_text(json.dumps(st), encoding="utf-8")
            calls = {"n": 0}

            def fake_fetch(ref, timeout=30):
                calls["n"] += 1
                return behaviour[ref](title[ref])
            rn.ARXIV.fetch = fake_fetch
            rn.time.sleep = lambda s: None
            sys.argv = ["run_local.py", "--config", str(t / "config.json"), "--raw", str(RAW[0]),
                        "--fake-agent-output", str(PKG / "tests/fixtures/fake_reply_ok.txt")]
            assert rn.main() == 0
            cards = [c for f in (t / "inspiration/cards").glob("*/cards.json") for c in json.loads(f.read_text(encoding="utf-8"))["cards"]]
            log = [json.loads(l) for l in (t / "inspiration/runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
            return cards, log[-1]

        a, b = rn.V_CARDS.norm_id(refs[0]), rn.V_CARDS.norm_id(refs[1])
        # A1: the arXiv API (triage) says "$Λ$-…", the abs page meta says "$\\Lambda$-…" (real T3 case)
        orig = title[a]
        for x in raw:
            if rn.V_CARDS.norm_id(x["id"]) == a:
                x["title"] = "$Λ$-enhanced " + orig
        (t / "raw.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        RAW[0] = t / "raw.json"
        cards, row = run({a: lambda s: ("ok", "$\\Lambda$-enhanced " + orig), b: lambda s: ("ok", s)})
        fz = row.get("arxiv_fuzzy") or []
        if fz:
            print("FAIL A1: 应该是精确匹配，不需要模糊兜底", fz)
            return 1
        if len(cards) != 2 or not all(c["source"]["verified"] for c in cards) or row.get("arxiv_rejected"):
            print("FAIL A1: TeX/Unicode 希腊字母标题应判为一致", row.get("arxiv_rejected"))
            return 1
        print("PASS  A1 $\\Lambda$ 与 $Λ$ 标题判为一致，两张卡都保留并 verified=true")
        RAW[0] = PKG / "tests/fixtures/raw_items.json"
        # A2: one wrong title -> dropped, reported with both titles in runs.jsonl and the digest
        cards, row = run({a: lambda s: ("ok", s), b: lambda s: ("ok", "A completely different paper about cats")})
        dig = (t / "inspiration/digest").glob("*.md").__next__().read_text(encoding="utf-8")
        rej = row.get("arxiv_rejected") or [{}]
        if len(cards) != 1 or rej[0].get("ref") != b or "cats" not in str(rej[0].get("page_title")) or "本次需要你知道的" not in dig:
            print("FAIL A2: 标题不符应丢弃并在 runs.jsonl / 摘要中写明", row.get("arxiv_rejected"))
            return 1
        print("PASS  A2 标题真不一致 → 丢弃，runs.jsonl 与摘要里写明两个标题")
        # A3: 429 three times -> kept as verified=false, listed as arxiv_unverified
        cards, row = run({a: lambda s: ("ok", s), b: lambda s: ("http_429", None)})
        ub = [c for c in cards if rn.V_CARDS.norm_id(c["source"]["ref"]) == b]
        if len(cards) != 2 or not ub or ub[0]["source"]["verified"] is not False or not row.get("arxiv_unverified"):
            print("FAIL A3: 429 应保留为未核验", row)
            return 1
        print("PASS  A3 arXiv 429（重试后仍失败）→ 保留卡片，verified=false，记入 arxiv_unverified")
    print("ALL ARXIV CHECK TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
