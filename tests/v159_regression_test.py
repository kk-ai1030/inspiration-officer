#!/usr/bin/env python3
"""v1.5.9 regression tests (2026-11-01): the paper (arXiv) card route — B cards — goes back to the v1.4.6 flow:
prepare → read triage.json.shortlisted in one go → write ONE cards.json → finalize. Observed (A-type) cards keep the
1.5.7 one-card-per-file route; finalize merges both. Offline, no platform.

    python tests/v159_regression_test.py
    INSP_V146_DIR=<v1.4.6 解压目录> python tests/v159_regression_test.py     # + E9/E10 与 v1.4.6 的对照

  E1  prepare：写 triage_shortlisted.json（= triage.json 的 shortlisted，同 ID、同摘要），不再生成 cand_*.md，
      旧的 cand_*.md 被清掉；打印“一次读完 triage_shortlisted.json”与三步写卡规则（论文卡一次写成 cards.json）
  E2  只有 cards.json（v1.4.6 写法）→ finalize 退出码 0；cards.json 原样不动；合并结果写 cards_final.json
  E3  cards_final.json 不回读：删掉 cards.json 后 finalize 读不到卡（不复活）
  E4  论文卡 cards.json + 单卡文件 cards/01.json 混合：两张都入库；id 撞了自动改号，不丢卡
  E5  finalize --drop <论文卡 id>：从 cards.json 移进 rejected，这次就通过
  E6  论文卡有缺陷：报错标“cards.json [id]”；第 3 次 AUTO_PARTIAL 只交通过的卡
  E7  1.5.7 留下的 cards.json（assembled_from=cards/，finalize 的旧输出）不当输入
  E8  技能正文 / 系统提示词 / 评价页触发指令 / 自动续跑都改成 triage_shortlisted.json + cards.json，不再出现 cand_
  E9  与 v1.4.6 对照：同一份 arXiv 列表 + 同一份 cards.json，两版的 COUNTS、入围 ID、入库卡片（id / ref / 链接）一致
  E10 arXiv 抓取与打分代码与 v1.4.6 逐字一致（fetch_arxiv / score_item / fetch_with_backoff / arxiv_reachable …）
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
FIX = PKG / "tests" / "fixtures"
RUNNER = PKG / "insp-nightly" / "scripts" / "insp_run.py"
V146 = Path(os.environ["INSP_V146_DIR"]) if os.environ.get("INSP_V146_DIR") else None
ok = 0


def check(name: str, cond: bool, detail: object = "") -> None:
    global ok
    if not cond:
        print(f"FAIL {name}: {str(detail)[-2500:]}")
        raise SystemExit(1)
    ok += 1
    print(f"PASS {name}")


def run(*args, runner: Path = RUNNER) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-X", "utf8", str(runner), *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return p.returncode, p.stdout + p.stderr


def jload(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def jsave(p: Path, o) -> None:
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(o, ensure_ascii=False, indent=2), encoding="utf-8")


def base_cards() -> list[dict]:
    txt = (FIX / "fake_reply_ok.txt").read_text(encoding="utf-8")
    doc = json.loads(txt.split("<<<INSP_CARDS_JSON", 1)[1].split("INSP_CARDS_JSON>>>", 1)[0])
    for c in doc["cards"]:
        c["source"].pop("fulltext_quotes", None)
        c.pop("related_evidence", None)
        c["numbers_provenance"] = [p for p in c.get("numbers_provenance", []) if p.get("source") == "abstract"]
    return doc["cards"]


def paper_doc(cards: list[dict], run_id: str = "sbx-test-159") -> dict:
    return {"schema_version": "1.0", "flow_version": "insp-card/1.1.0", "run_id": run_id,
            "generated_at": "2026-11-01T03:00:00+08:00", "status": "OK", "cards": cards, "rejected": []}


def sandbox(t: Path, pkg: Path = PKG, runner: Path = RUNNER) -> tuple[list, int, str]:
    home = t / "home"
    shutil.copytree(pkg / "insp-init/assets/灵感状态模板/inspiration", home)
    st = jload(home / "state.json")
    st["bootstrap"] = False
    st["seen_ids"] = jload(FIX / "seen_ids.json")["seen_ids"]
    jsave(home / "state.json", st)
    sb = ["--out", t / "out", "--state-home", home, "--refs", t / "_refs"]
    code, log = run("prepare", *sb, "--raw", FIX / "raw_items.json", "--run-id", "sbx-test-159", runner=runner)
    return sb, code, log


# ------------------------------------------------------------------ E1
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    (t / "out").mkdir()
    (t / "out" / "cand_9999.00001.md").write_text("stale", encoding="utf-8")
    sb, code, log = sandbox(t)
    out = t / "out"
    tri, sl = jload(out / "triage.json"), jload(out / "triage_shortlisted.json")
    same = [(c["id"], c.get("abstract")) for c in tri["shortlisted"]] == [(c["id"], c.get("abstract")) for c in sl["shortlisted"]]
    check("E1 prepare 写 triage_shortlisted.json（= shortlisted），不生成 cand_*.md，旧的被清掉；打印一次读完 + 论文卡一次写成 cards.json",
          code == 0 and same and len(sl["shortlisted"]) >= 2 and not list(out.glob("cand_*.md"))
          and "一次读完" in log and "triage_shortlisted.json" in log and "cand_" not in log and "第一步 · 论文卡" in log
          and str(out / "cards.json") in log and sl["run_id"] == "sbx-test-159" and sl["remaining"]["knowledge"] == 5,
          (code, log[-1500:], sorted(p.name for p in out.iterdir())))

    # -------------------------------------------------------------- E2 / E3 paper cards only (v1.4.6 cards.json)
    cards = base_cards()
    jsave(out / "cards.json", paper_doc(cards))
    before = (out / "cards.json").read_text(encoding="utf-8")
    code, log = run("finalize", *sb)
    b1 = jload(out / "bundle.json")
    check("E2 只有 cards.json（v1.4.6 写法）→ finalize 退出码 0；cards.json 原样不动；合并结果在 cards_final.json",
          code == 0 and [c["id"] for c in b1["cards"]] == [c["id"] for c in cards]
          and (out / "cards.json").read_text(encoding="utf-8") == before
          and jload(out / "cards_final.json").get("assembled_from") == "cards/" and "论文卡 2 张" in log, log)
    (out / "cards.json").unlink()
    code, log = run("finalize", *sb)
    check("E3 cards_final.json 不回读：删掉 cards.json 后 finalize 读不到卡（不复活）",
          code == 2 and "还没有卡片" in log, log)

# ------------------------------------------------------------------ E4 mixed + id clash
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    sb, code, log = sandbox(t)
    out = t / "out"
    c1, c2 = base_cards()
    c2_same_id = dict(c2, id=c1["id"])
    jsave(out / "cards.json", paper_doc([c1]))
    jsave(out / "cards" / "01.json", c2_same_id)
    code, log = run("finalize", *sb)
    b = jload(out / "bundle.json") if (out / "bundle.json").is_file() else {}
    refs = sorted((c.get("source") or {}).get("ref") for c in b.get("cards", []))
    check("E4 cards.json + cards/01.json 混合：两张都入库；id 撞了自动改号",
          code == 0 and len(b.get("cards", [])) == 2 and "RENUMBER" in log
          and refs == sorted([c1["source"]["ref"], c2["source"]["ref"]]) and len({c["id"] for c in b["cards"]}) == 2, log)

# ------------------------------------------------------------------ E5 --drop a paper card
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    sb, code, log = sandbox(t)
    out = t / "out"
    good, bad = base_cards()
    bad["one_line"] = "这一句话故意写得非常非常长，" * 6
    jsave(out / "cards.json", paper_doc([good, bad]))
    code, log = run("finalize", *sb, "--drop", bad["id"])
    b = jload(out / "bundle.json") if (out / "bundle.json").is_file() else {}
    doc = jload(out / "cards.json")
    check("E5 finalize --drop <论文卡 id>：从 cards.json 移进 rejected，这一次就通过",
          code == 0 and f"DROPPED cards.json（{bad['id']}）" in log and [c["id"] for c in b.get("cards", [])] == [good["id"]]
          and [c["id"] for c in doc["cards"]] == [good["id"]] and any(r.get("card_id") == bad["id"] for r in doc["rejected"]), log)

# ------------------------------------------------------------------ E6 defect in a paper card → label, AUTO_PARTIAL
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    sb, code, log = sandbox(t)
    out = t / "out"
    good, bad = base_cards()
    bad["one_line"] = "这一句话故意写得非常非常长，" * 6
    jsave(out / "cards.json", paper_doc([good, bad]))
    r1 = run("finalize", *sb)
    r2 = run("finalize", *sb)
    r3 = run("finalize", *sb)
    b = jload(out / "bundle.json") if (out / "bundle.json").is_file() else {}
    check("E6 论文卡有缺陷：报错标 cards.json [id]；第 3 次 AUTO_PARTIAL 只交通过的 1 张",
          r1[0] == 2 and f"cards.json [{bad['id']}]" in r1[1] and r2[0] == 2 and r3[0] == 0 and "AUTO_PARTIAL" in r3[1]
          and [c["id"] for c in b.get("cards", [])] == [good["id"]], (r1[1][-800:], r3[1][-800:]))

# ------------------------------------------------------------------ E7 leftover 1.5.7 output
sys.path.insert(0, str(RUNNER.parent))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("ir159", RUNNER)
IR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(IR)  # type: ignore[union-attr]
with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp)
    jsave(out / "cards.json", {"cards": [{"id": "ins_20261031_001"}], "assembled_from": "cards/"})
    doc, how = IR.assemble_cards(out, {})
    check("E7 1.5.7 finalize 写出的旧 cards.json（assembled_from）不当输入", doc is None, (doc, how))

# ------------------------------------------------------------------ E8 instructions
texts = {rel: (PKG / rel).read_text(encoding="utf-8") for rel in
         ("insp-nightly/SKILL.md", "insp-card/SKILL.md", "platform/agent_system_prompt.md", "platform/receiver.py")}
bad = [rel for rel, s in texts.items() if "cand_" in s or "triage_shortlisted.json" not in s or "cards.json" not in s]
rcv = texts["platform/receiver.py"]
cont = rcv[rcv.index("CONTINUE_MSG"):rcv.index("def latest_run")]
check("E8 技能正文 / 系统提示词 / 评价页触发指令 / 自动续跑：triage_shortlisted.json + 论文卡一次写成 cards.json，不再出现 cand_",
      not bad and "一次写成 cards.json" in cont, bad)

# ------------------------------------------------------------------ E9 / E10 vs v1.4.6
if V146 and V146.is_dir():
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        res = {}
        for tag, pkg in (("new", PKG), ("old", V146)):
            sb, code, log = sandbox(t / tag, pkg, pkg / "insp-nightly/scripts/insp_run.py")
            out = t / tag / "out"
            jsave(out / "cards.json", paper_doc(base_cards()))
            fc, flog = run("finalize", *sb, runner=pkg / "insp-nightly/scripts/insp_run.py")
            tri, b = jload(out / "triage.json"), jload(out / "bundle.json")
            counts = {k: v for k, v in tri["counts"].items() if not k.startswith("observed")}
            res[tag] = (code, fc, counts, [c["id"] for c in tri["shortlisted"]],
                        [(c["id"], c["source"]["ref"], c["source"].get("url"), c["type"]) for c in b["cards"]])
        check("E9 与 v1.4.6 对照：同一份 arXiv 列表 + 同一份 cards.json → COUNTS、入围 ID、入库卡片一致",
              res["new"] == res["old"] and res["new"][0] == res["new"][1] == 0, res)

    def func_src(path: Path, name: str) -> str:
        src = path.read_text(encoding="utf-8")
        for node in ast.parse(src).body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(src, node) or ""
        return "<missing>"
    diffs = []
    for rel, names in (("insp-triage/scripts/triage.py", ("fetch_arxiv", "score_item", "norm_id", "tokens", "jaccard")),
                       ("insp-nightly/scripts/insp_run.py", ("fetch_with_backoff", "arxiv_reachable"))):
        diffs += [f"{rel}::{n}" for n in names if func_src(PKG / rel, n) != func_src(V146 / rel, n)]
    check("E10 arXiv 抓取 / 打分代码与 v1.4.6 逐字一致", not diffs, diffs)
else:
    print("SKIP E9/E10（设置 INSP_V146_DIR=<v1.4.6 解压后的 inspiration-skills 目录> 才和 v1.4.6 对照）")

print(f"V159 OK: {ok} checks")
