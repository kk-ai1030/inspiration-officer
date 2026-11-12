#!/usr/bin/env python3
"""v1.5.7 regression tests for CARD1 (2026-10-31): deleted single-card files came back from cards.json, finalize
failed six times in a row and the run burnt the balance. Offline, no platform.

    python tests/v157_regression_test.py

  C1  删单卡文件 = 删卡：上一次 finalize 写出的 cards.json 不再把它合并回来（9 个文件读 13 张的复现场景）
  C2  删光所有单卡文件 → 0 张（cards.json 是输出，不当输入）
  C3  旧写法（只有手写 cards.json、cards/ 为空）照常可用
  C4  超配额的合格卡不是错误：finalize 一次通过，按分数保留，其余自动进 rejected（auto=quota），退出码 0
  C5  真缺陷：第 1、2 次失败（退出码 2，报错写出单卡文件名、提示 --drop、不再说“修改 cards.json”）；
      第 3 次自动只交通过的卡（AUTO_PARTIAL），缺陷卡进 rejected（auto=validation）
  C6  finalize --drop 05 / 卡片 id：文件移到 cards/_dropped/、记进 rejected.json，不用删除工具
  C7  prepare 打印 REMAINING（与 finalize 同一套窗口计算；已满的类型写明“不要写”；“不要用记忆里的配额数字”）
  C8  prepare 清掉上一轮的失败计数
  C9  观察型 quote 跨两行、去掉了“ 97| ”行号前缀也算逐字
  C10 remaining_quota 与 select 一致：process 14 天已用 2/2 → 0；knowledge per_run 5；A 类不限
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

PKG = Path(__file__).resolve().parents[1]
FIX = PKG / "tests" / "fixtures"
RUNNER = PKG / "insp-nightly" / "scripts" / "insp_run.py"
ok = 0


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def check(name: str, cond: bool, detail: object = "") -> None:
    global ok
    if not cond:
        print(f"FAIL {name}: {str(detail)[-2500:]}")
        raise SystemExit(1)
    ok += 1
    print(f"PASS {name}")


def run(*args) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-X", "utf8", str(RUNNER), *map(str, args)], capture_output=True, text=True,
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


def sandbox(t: Path, quota: dict | None = None) -> list:
    """A fresh sandbox with the template state (seen ids from the fixture) + prepare --raw (abstract mode)."""
    home, out, refs = t / "home", t / "out", t / "_refs"
    shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", home)
    st = jload(home / "state.json")
    st["bootstrap"] = False
    st["seen_ids"] = jload(FIX / "seen_ids.json")["seen_ids"]
    if quota:
        for k, v in quota.items():
            st["quota"].setdefault(k, {}).update(v) if isinstance(v, dict) else st["quota"].__setitem__(k, v)
    jsave(home / "state.json", st)
    sb = ["--out", out, "--state-home", home, "--refs", refs]
    code, log = run("prepare", *sb, "--raw", FIX / "raw_items.json")
    if code != 0:
        print(log)
        raise SystemExit("prepare failed")
    return sb


IR = load("insp-nightly/scripts/insp_run.py", "ir_v157")
VC = load("insp-card/scripts/validate_cards.py", "vc_v157")

# ------------------------------------------------------------------ C1–C3 assemble: cards/ is the only source
with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp)
    cd = out / "cards"
    cd.mkdir()
    for i in range(1, 14):
        jsave(cd / f"{i:02d}.json", {"id": f"ins_{i:02d}", "type": "knowledge"})
    doc, how = IR.assemble_cards(out, {"run_id": "sbx-1"})          # first finalize writes cards.json with all 13
    assert len(doc["cards"]) == 13 and (out / "cards_final.json").is_file() and not (out / "cards.json").exists()
    for i in (5, 7, 8, 12):
        (cd / f"{i:02d}.json").unlink()
    doc, how = IR.assemble_cards(out, {"run_id": "sbx-1"})
    check("C1 删 4 个单卡文件后：9 个文件读出 9 张（1.5.6 是 13 张），删掉的不复活",
          len(doc["cards"]) == 9 and how.endswith("9 个观察型单卡文件") and not {"ins_05", "ins_07", "ins_08", "ins_12"} & {c["id"] for c in doc["cards"]}
          and jload(out / "cards_final.json").get("assembled_from") == "cards/", (how, [c["id"] for c in doc["cards"]]))
    for i in (11, 13):
        (cd / f"{i:02d}.json").unlink()
    doc, _ = IR.assemble_cards(out, {})
    check("C1b 再删 2 个、不碰 cards.json：7 个文件读 7 张", len(doc["cards"]) == 7, [c["id"] for c in doc["cards"]])
    for f in cd.glob("*.json"):
        f.unlink()
    doc, how = IR.assemble_cards(out, {})
    check("C2 单卡文件删光：0 张（finalize 写出的 cards.json 不当输入）", doc is None, (doc, how))
    (out / "cards.json").write_text(json.dumps({"schema_version": "1.0", "cards": [{"id": "legacy_1"}], "rejected": []}),
                                    encoding="utf-8")
    doc, how = IR.assemble_cards(out, {})
    check("C3 v1.4.6 写法：只有手写 cards.json（没有 assembled_from）→ 照常读", doc and doc["cards"][0]["id"] == "legacy_1"
          and "论文卡 1 张" in how and not (out / "cards.json").read_text(encoding="utf-8").count("assembled_from"), (doc, how))

# ------------------------------------------------------------------ C4 over-quota is not an error
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    # CARD1 shape: the hypothesis slot is already used up this week (1/1); the agent wrote one anyway
    sb = sandbox(t, {"hypothesis": {"per_week": 0}, "daily_total_max": 999})
    out = t / "out"
    k, h = base_cards()
    jsave(out / "cards" / "01.json", k)
    jsave(out / "cards" / "02.json", h)
    code, log = run("finalize", *sb)
    b = jload(out / "bundle.json") if (out / "bundle.json").is_file() else {}
    auto = [r for r in b.get("rejected", []) if r.get("auto") == "quota"]
    check("C4 合格但超配额的 hypothesis 卡：一次通过（退出码 0），保留 knowledge，hypothesis 自动进 rejected，日志写明“不是错误”",
          code == 0 and [c["id"] for c in b.get("cards", [])] == [k["id"]] and [r["card_id"] for r in auto] == [h["id"]]
          and "QUOTA_TRIMMED 1" in log and "02.json" in log and "不是错误" in log and "VALIDATION_FAILED" not in log
          and b["sandbox_validation"]["quota_trimmed"] == [h["id"]], log)

# ------------------------------------------------------------------ C5 real defects: two failures, then auto partial
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    sb = sandbox(t)
    out = t / "out"
    good, bad = base_cards()
    bad = json.loads(json.dumps(bad))
    bad["one_line"] = "这一句话故意写得非常非常长，" * 6           # > 40 字
    jsave(out / "cards" / "01.json", good)
    jsave(out / "cards" / "02.json", bad)
    c1, l1 = run("finalize", *sb)
    c2, l2 = run("finalize", *sb)
    check("C5a 第 1、2 次：退出码 2；报错带单卡文件名 02.json、提示 finalize --drop、不再叫人“修改 cards.json”",
          c1 == 2 and c2 == 2 and "02.json [ins_20261028_002]" in l1 and "--drop" in l1 and "第 1 次" in l1 and "第 2 次" in l2
          and "修改 cards.json" not in l1, l1)
    c3, l3 = run("finalize", *sb)
    b = jload(out / "bundle.json")
    check("C5b 第 3 次：AUTO_PARTIAL，只交通过的 1 张，缺陷卡进 rejected（auto=validation），计数清零",
          c3 == 0 and "AUTO_PARTIAL" in l3 and [c["id"] for c in b["cards"]] == [good["id"]]
          and any(r.get("auto") == "validation" and r.get("card_id") == bad["id"] for r in b["rejected"])
          and b["sandbox_validation"]["auto_partial"] is True and not (out / IR.ATTEMPTS).exists(), l3)

# ------------------------------------------------------------------ C6 --drop
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    sb = sandbox(t)
    out = t / "out"
    good, bad = base_cards()
    bad["one_line"] = "这一句话故意写得非常非常长，" * 6
    jsave(out / "cards" / "01.json", good)
    jsave(out / "cards" / "05.json", bad)
    code, log = run("finalize", *sb, "--drop", "05")
    b = jload(out / "bundle.json") if (out / "bundle.json").is_file() else {}
    rej = jload(out / "cards" / "rejected.json")
    check("C6 finalize --drop 05：05.json 移到 cards/_dropped/、记进 rejected.json，这一次就通过",
          code == 0 and "DROPPED 05.json" in log and (out / "cards" / "_dropped" / "05.json").is_file()
          and not (out / "cards" / "05.json").exists() and len(b.get("cards", [])) == 1
          and any(r.get("card_id") == bad["id"] for r in rej), log)
    jsave(out / "cards" / "02.json", base_cards()[1])
    code, log = run("finalize", *sb, "--drop", base_cards()[1]["id"])
    check("C6b --drop 也认卡片 id", "DROPPED 02.json" in log and not (out / "cards" / "02.json").exists(), log)

# ------------------------------------------------------------------ C7 / C8 prepare prints REMAINING, clears the counter
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    home = t / "home"
    shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", home)
    st = jload(home / "state.json")
    st["bootstrap"] = False
    st["seen_ids"] = jload(FIX / "seen_ids.json")["seen_ids"]
    st["quota"]["process"]["per_14_days"] = 2
    st["quota"]["hypothesis"]["per_week"] = 1
    st["quota_used"] = {date.today().isoformat(): {"process": 2, "hypothesis": 1}}
    jsave(home / "state.json", st)
    (t / "out").mkdir()
    (t / "out" / IR.ATTEMPTS).write_text('{"failed": 2}', encoding="utf-8")
    code, log = run("prepare", "--out", t / "out", "--state-home", home, "--refs", t / "_refs", "--raw", FIX / "raw_items.json")
    line = next((x for x in log.splitlines() if x.startswith("REMAINING ")), "")
    rem = json.loads(line[len("REMAINING "):]) if line else {}
    check("C7 prepare 打印 REMAINING：process 0、hypothesis 0、knowledge ≤5、A 不限；已满的写明不要写；以这一行为准",
          rem.get("process") == 0 and rem.get("hypothesis") == 0 and 0 < rem.get("knowledge", 0) <= 5
          and rem.get("optimization", 0) >= 999 and "已满：这类候选不要写" in log and "不要用记忆里的配额数字" in log
          and "finalize --drop" in log, log[-1500:])
    check("C8 prepare 清掉上一轮的 finalize 失败计数", not (t / "out" / IR.ATTEMPTS).exists())

# ------------------------------------------------------------------ C9 observed quote across numbered lines
ev = (" 97| 另外，作者每次翻转的自旋数量会越来越多\n 98| To prevent trapping into local minima, we select clusters.\n"
      " 99| 但它们宣称的这个优点就有些看不懂\n100| Unlike other photonic Ising machines [23], no information about the "
      "target Hamiltonian is used to electronically affect the spin evolution.")
cand = {"id": "obs_655f8661c1dc", "kind": "notes", "allowed_types": ["knowledge"], "candidate_type": "knowledge", "abstract": ev}
card = {"type": "knowledge", "source": {"kind": "notes"}}
q_two = "但它们宣称的这个优点就有些看不懂\nUnlike other photonic Ising machines [23], no information about the target Hamiltonian"
q_pref = " 99| 但它们宣称的这个优点就有些看不懂\n100| Unlike other photonic Ising machines [23]"
check("C9 观察型 quote 跨两行（去掉或保留行号前缀）都算逐字；编造的仍然不算",
      VC.check_observed(card, cand, q_two) == [] and VC.check_observed(card, cand, q_pref) == []
      and VC.check_observed(card, cand, "这个优点完全是错的，作者在胡说八道") != [],
      [VC.check_observed(card, cand, q_two), VC.check_observed(card, cand, q_pref)])

# ------------------------------------------------------------------ C10 remaining_quota == what select() enforces
st = {"quota": {"daily_total_max": 999, "optimization": {"per_day": 999}, "knowledge": {"per_week": 999, "per_run": 5},
                "hypothesis": {"per_week": 1}, "process": {"per_14_days": 2}},
      "quota_used": {date.today().isoformat(): {"process": 2, "knowledge": 9}}}
rem = VC.remaining_quota(st, date.today())
check("C10 remaining_quota：process 2/2 → 0，hypothesis 1，knowledge 本轮 5，A 不限",
      rem["process"] == 0 and rem["hypothesis"] == 1 and rem["knowledge"] == 5 and rem["optimization"] >= 999, rem)

print(f"V157 OK: {ok} checks")
