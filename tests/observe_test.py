#!/usr/bin/env python3
"""v1.5 observed channel, offline end-to-end (no network, no platform): O1–O12.

Builds a synthetic library (Python / Fortran code, logs, a CSV, notes, a .bib + a PDF, plotting scripts, figures,
a conversation list), runs platform/observe.py scan, then checks:
  O1  every observer produced its finding (code / logs / data / notes / papers / usage)
  O2  findings carry kind / candidate_type / severity / evidence / evidence_ref and obs_ ids
  O3  secrets in logs are redacted in observed.jsonl
  O4  rescan keeps ids stable, times_seen += 1; a removed note is "resolved" into observed_history.jsonl
  O5  papers: arXiv IDs of the local library land in state.json → library_arxiv_ids; --apply-keywords merges keywords
  O6  usage: the inspiration system's own conversations are excluded; plotting boilerplate is in the evidence
  O7  profile/observed.md auto section has path + date, text outside the markers is kept
  O8  triage observed channel: grouped by candidate_type, per_type cap (optimization 3, others 2), sorted by severity
  O9  a card written from an observed candidate passes validate_cards (verbatim quote) and gets evidence_ref
  O10 a card with a made-up quote / wrong kind is rejected
  O11 a pushed observed id is not shortlisted again (dedup via state.pushed)
  O13 receiver.observe_before_run: scan before upload, 403 never blocks, no observe section = off
  O15 user exclude lists are additive to built-in exclusions
  O14 sandbox prepare --observed-only → finalize → receiver ingest → state.pushed → never shortlisted again
  O12 observe.py CLI: init / scan / list / log / show / papers exit 0; upload_code_snippets=false hides code
    python tests/observe_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "platform"))
sys.path.insert(0, str(PKG / "insp-triage" / "scripts"))
sys.path.insert(0, str(PKG / "insp-card" / "scripts"))

import triage  # noqa: E402
import validate_cards as vc  # noqa: E402
from observers.common import Env  # noqa: E402

import observe  # noqa: E402

TEMPLATE = PKG / "insp-init" / "assets" / "灵感状态模板" / "inspiration"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  ← {detail}"))


def long_py_function(n_if: int = 20) -> str:
    body = ["def analyse(samples, beta, cutoff, mode, verbose=False):", "    total = 0.0"]
    for i in range(n_if):
        body += [f"    if mode == {i} and beta > {i}:", f"        total += samples[{i}] * beta", "        if verbose:",
                 f"            print('branch {i}', total)"]
    body += ["    return total", ""]
    return "\n".join(body)


SHARED = "\n".join([
    "def load_block(path):",
    "    rows = []",
    "    with open(path) as fh:",
    "        for line in fh:",
    "            if line.startswith('#'):",
    "                continue",
    "            parts = line.split()",
    "            energy = float(parts[1])",
    "            magnet = float(parts[2])",
    "            rows.append((energy, magnet))",
    "    rows.sort(key=lambda r: r[0])",
    "    mean_e = sum(r[0] for r in rows) / len(rows)",
    "    mean_m = sum(r[1] for r in rows) / len(rows)",
    "    print('loaded', len(rows), mean_e, mean_m)",
    "    return rows, mean_e, mean_m",
    ""])

PERF = """
def correlate(spins, L):
    out = np.zeros((L, L))
    for i in range(L):
        for j in range(L):
            out[i, j] = spins[i] * spins[j]
    acc = np.array([])
    for k in range(L):
        acc = np.append(acc, out[k].sum())
    return out, acc
"""

FORTRAN = ["subroutine sweep(lattice, n, beta)", "  integer :: n, i, j", "  real(8) :: beta"]
for _i in range(30):
    FORTRAN += ["  do i = 1, n", f"    if (lattice(i) > {_i}) then", f"      lattice(i) = lattice(i) - {_i}", "    end if", "  end do"]
FORTRAN += ["end subroutine sweep", ""]

PLOT = """import matplotlib.pyplot as plt
import numpy as np
plt.rcParams['font.size'] = 14
fig, ax = plt.subplots(figsize=(6, 4))
ax.plot(np.arange(10), np.arange(10) ** {k})
ax.set_xlabel('T')
ax.set_ylabel('C')
plt.savefig('fig{k}.pdf', bbox_inches='tight')
"""


def build_library(base: Path) -> None:
    lib = base / "library"
    code, logs, data, notes, papers = (lib / x for x in ("code", "logs", "data", "notes", "papers"))
    for d in (code, logs, data, notes, papers):
        d.mkdir(parents=True, exist_ok=True)
    (code / "qmc.py").write_text("import numpy as np\n\n" + long_py_function() + "\n" + SHARED + PERF, encoding="utf-8")
    (code / "qmc_old.py").write_text("import os\n\n" + SHARED + "\n# 这里有点怪：beta=2 附近能量会跳一下，先记着\n", encoding="utf-8")
    (code / "sweep.f90").write_text("\n".join(FORTRAN), encoding="utf-8")
    for k in (1, 2, 3):
        (code / f"plot_{k}.py").write_text(PLOT.format(k=k), encoding="utf-8")
    for k in range(6):
        (code / f"fig{k}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(20))
    log = [f"step {i} energy = {-1.2345 - 0.0001 * (i % 7):.6f} acc = 0.5{i % 10}" for i in range(60)]
    log[41] = "step 41 energy = 523.100000 acc = 0.51"
    log[50] = "step 50 energy = nan acc = 0.50   api_key = sk-abcdefghijklmnopqrstuvwx"
    tb = ["Traceback (most recent call last):", '  File "qmc.py", line 88, in <module>', "    analyse(s, 2.0, 3, 1)",
          "ZeroDivisionError: float division by zero"]
    (logs / "run1.log").write_text("\n".join(log + tb) + "\n", encoding="utf-8")
    (logs / "run2.log").write_text("\n".join(["start", "beta = 2.0"] + tb) + "\n", encoding="utf-8")
    rows = ["T,E,C"] + [f"{1 + 0.01 * i:.2f},{-1.0 - 0.001 * i:.4f},{0.5 + 0.001 * (i % 5):.4f}" for i in range(100)]
    rows[30] = "1.29,-1.0290,950.0"
    rows[60] = "1.59,,0.5010"
    (data / "obs.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (notes / "2026-10-20.md").write_text(
        "# 组会后\n\n- 以后可以试试 用 worm 算法替换局部更新\n- 没搞懂 Kosterlitz-Thouless 相变的有限尺寸标度\n- 普通的一行\n",
        encoding="utf-8")
    (notes / "old.md").write_text("# 临时\n\n- 这里有点怪：C 的峰位置和文献差一截\n", encoding="utf-8")
    bib = []
    for i, t in enumerate(["Quantum spin liquid on the kagome lattice", "Spin liquid signatures in Raman scattering",
                           "Dirac spin liquid and monopoles", "Thermal Hall effect in a spin liquid candidate",
                           "Variational Monte Carlo for spin liquid states"]):
        bib.append(f"@article{{p{i},\n  title = {{{t}}},\n  eprint = {{2409.1000{i}}},\n  archivePrefix = {{arXiv}},\n  year = {{2024}}\n}}\n")
    (papers / "refs.bib").write_text("\n".join(bib), encoding="utf-8")
    (papers / "2410.12345v2.pdf").write_bytes(b"%PDF-1.4\n1 0 obj << /Title (Kagome spin liquid from tensor networks) >> endobj\n%%EOF\n")
    conv = [{"thread_id": f"t{i}", "name": n, "created_at": "2099-01-01T00:00:00+00:00"} for i, n in enumerate(
        ["帮我画图：比热随温度", "matplotlib 图例太挤怎么办", "画图配色要色盲友好", "这个 colorbar 怎么改", "Kagome 晶格的能带",
         "主动探索灵感", "灵感官 画像访谈"])]
    (base / "conversations.json").write_text(json.dumps(conv, ensure_ascii=False), encoding="utf-8")


def make_env(tmp: Path, observe_over: dict | None = None) -> Env:
    shutil.copytree(TEMPLATE, tmp / "inspiration")
    cfg = json.loads((PKG / "platform" / "config.example.json").read_text(encoding="utf-8"))
    cfg["state_dir"] = "./inspiration"
    if observe_over:
        cfg.setdefault("observe", {}).update(observe_over)
    (tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return Env(tmp / "config.json")


def rows_of(env: Env) -> list[dict]:
    return [json.loads(x) for x in (env.state_dir / "observed.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def card_for(cand: dict, quote: str, kind: str | None = None) -> dict:
    return {
        "id": "ins_20261030_101", "type": cand["candidate_type"], "created_at": "2026-10-30T02:50:00+08:00",
        "source": {"kind": kind or cand["kind"], "ref": cand["id"], "quote": quote, "verified": False},
        "title": "拆分 analyse 函数", "one_line": "把 20 个分支改成查表", "why_me": "这个函数每次改都要动",
        "hypothesis": "", "minimal_check": "同一输入跑新旧两版比对输出", "expected_observable": "输出一致，行数减半",
        "falsifier": "新版输出与旧版不一致", "magnitude": "函数长度见证据",
        "cost_estimate": {"tokens": "~20k", "wall_clock": "1h", "risk": "low"},
        "rubric_scores": [{"rule_id": "R01", "score": 4, "reason": "直接相关"}, {"rule_id": "R02", "score": 4, "reason": "收益明确"},
                          {"rule_id": "R07", "score": 5, "reason": "一次比对可判定"}],
        "numbers_provenance": [], "status": "proposed", "verdict": None}


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        build_library(tmp)
        env = make_env(tmp)
        observe.run_scan(env, platform=False, conversations_file=tmp / "conversations.json", quiet=True)
        rows = rows_of(env)
        det = {r["detector"] for r in rows}
        need = {"code.long_function", "code.duplication", "code.perf_loop", "logs.traceback", "logs.nan_inf", "logs.outlier",
                "data.anomaly", "notes.try", "notes.learn", "notes.odd", "papers.interest_gap", "usage.plotting"}
        check("O1 每个观察者都有发现", need <= det, f"缺 {sorted(need - det)}；有 {sorted(det)}")
        langs = {r["evidence_ref"].split("#")[0].rsplit(".", 1)[-1] for r in rows if r["detector"] == "code.long_function"}
        check("O1b Python 与 Fortran 的长函数都识别", {"py", "f90"} <= langs, str(langs))
        ok2 = all(r["id"].startswith("obs_") and r["kind"] in ("workspace", "log", "anomaly") and r["candidate_type"] in
                  triage.OBS_TYPES and 1 <= r["severity"] <= 5 and r["evidence"].strip() and r.get("evidence_ref") for r in rows)
        check("O2 字段齐全（id/kind/candidate_type/severity/evidence/evidence_ref）", ok2)
        blob = (env.state_dir / "observed.jsonl").read_text(encoding="utf-8")
        check("O3 日志里的密钥已脱敏", "sk-abcdefghijkl" not in blob and "已脱敏" in blob)
        ids1 = {r["id"] for r in rows}
        (tmp / "library" / "notes" / "old.md").unlink()
        observe.run_scan(env, platform=False, conversations_file=tmp / "conversations.json", quiet=True)
        rows2 = rows_of(env)
        ids2 = {r["id"] for r in rows2}
        hp = env.out / "observed_history.jsonl"
        hist = hp.read_text(encoding="utf-8") if hp.is_file() else ""
        stable = all(r["times_seen"] == 2 for r in rows2 if r["id"] in ids1)
        check("O4 重扫编号稳定、times_seen+1、删掉的笔记进 history", stable and len(ids1 - ids2) >= 1 and "old.md" in hist,
              f"消失 {len(ids1 - ids2)} 条")
        st = json.loads((env.state_dir / "state.json").read_text(encoding="utf-8"))
        lib_ids = set(st.get("library_arxiv_ids", []))
        check("O5 论文库 arXiv 编号写进 library_arxiv_ids", {"2410.12345", "2409.10000"} <= lib_ids, str(sorted(lib_ids)))
        from observers import papers as P
        added = P.apply_keywords(env)
        st = json.loads((env.state_dir / "state.json").read_text(encoding="utf-8"))
        check("O5b --apply-keywords 并入关键词", any("spin liquid" in k for k in st["keywords"]), f"{added} / {st['keywords']}")
        u = next((r for r in rows2 if r["detector"] == "usage.plotting"), {"evidence": ""})
        check("O6 使用统计排除系统自己的会话、带绘图样板行", "主动探索灵感" not in u["evidence"] and "灵感官" not in u["evidence"]
              and "plt.rcParams" in u["evidence"] and "[3 个脚本]" in u["evidence"], u["evidence"][:400])
        prof = (env.state_dir / "profile" / "observed.md").read_text(encoding="utf-8")
        check("O7 observed.md 自动段带路径与日期，原文保留", "observe:auto:start" in prof and "library://" in prof
              and prof.lstrip().startswith("#"), prof[:200])
        neg: list = []
        short, counts, dropped = triage.observed_shortlist(env.state_dir, st, neg, {})
        types = [c["candidate_type"] for c in short]
        per_ok = all(types.count(x) <= (3 if x == "optimization" else 2) for x in set(types)) and len(short) <= 6
        sev_ok = all(short[i]["final"] >= short[i + 1]["final"] for i in range(len(short) - 1))
        check("O8 triage 按类型分组（A 类≤3、其它≤2）、总数≤6、按严重度排序", per_ok and sev_ok and "optimization" in types
              and counts["observed"] == len(rows2), f"{types} {counts}")
        cand = next(c for c in short if c["candidate_type"] == "optimization")
        quote = next(ln for ln in cand["abstract"].splitlines() if 12 < len(ln.strip()) < 300)
        tri = {"shortlisted": [], "observed_shortlist": short}
        doc = {"schema_version": "1.0", "flow_version": vc.FLOW_VERSION, "run_id": "o1", "generated_at": "2026-10-30T02:50:00+08:00",
               "status": "OK", "cards": [card_for(cand, quote)], "rejected": []}
        root, per, kept = vc.validate(doc, tri, env.state_dir, vc.FLOW_VERSION, None)
        so = kept[0]["source"] if kept else {}
        check("O9 观察型卡片（逐字引文）通过校验并补 evidence_ref", not root and kept and not per["ins_20261030_101"]
              and so.get("evidence_ref") == cand["evidence_ref"] and so.get("read_mode") == "observed", f"{root} {per} {so}")
        bad = card_for(cand, "这一行是编造出来的证据文本，不在原文里", kind="arxiv")
        _, perb, keptb = vc.validate(dict(doc, cards=[bad]), tri, env.state_dir, vc.FLOW_VERSION, None)
        msg = " | ".join(perb["ins_20261030_101"])
        check("O10 编造引文 / kind 不符被拒", not keptb and "逐字片段" in msg and "source.kind" in msg, msg)
        st["pushed"] = st.get("pushed", []) + [{"id": cand["id"], "card_id": "ins_x", "at": "2026-10-30"}]
        short2, _, dropped2 = triage.observed_shortlist(env.state_dir, st, neg, {})
        check("O11 推送过的 observed 编号不再入围", cand["id"] not in {c["id"] for c in short2}
              and any(d["id"] == cand["id"] for d in dropped2), str([d for d in dropped2 if d["id"] == cand["id"]]))

        envp = dict(os.environ, PYTHONUTF8="1")
        cli = [sys.executable, "-X", "utf8", str(PKG / "platform" / "observe.py"), "--config", str(tmp / "config.json")]
        codes = []
        for args in (["init"], ["scan", "--no-platform"], ["list", "--all"], ["log", "手动调了 colorbar", "--tag", "绘图"],
                     ["show", rows2[0]["id"]], ["papers"]):
            r = subprocess.run(cli + args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=envp, cwd=str(tmp))
            codes.append((args[0], r.returncode, (r.stdout + r.stderr)[-400:]))
        check("O12 命令行 init/scan/list/log/show/papers 正常退出", all(c == 0 for _, c, _ in codes),
              str([c for c in codes if c[1]]))
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        build_library(tmp)
        env = make_env(tmp, {"upload_code_snippets": False})
        observe.run_scan(env, only=["code"], platform=False, quiet=True)
        code_rows = [r for r in rows_of(env) if r["detector"] in ("code.long_function", "code.duplication", "code.perf_loop")]
        check("O12b upload_code_snippets=false 时证据里没有代码原文", bool(code_rows) and all(
            "samples[" not in r["evidence"] and "upload_code_snippets = false" in r["evidence"] for r in code_rows))
    # O13 receiver: observe_before_run refreshes observed.jsonl before upload; a failing platform call never blocks
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        build_library(tmp)
        make_env(tmp)
        import receiver

        class Boom:
            def dashboard_threads(self):
                raise RuntimeError("403 Forbidden")
        ctx = receiver.Ctx(tmp / "config.json")
        ctx.setup_client = lambda: Boom()
        rep = receiver.observe_before_run(ctx)
        files = [f.name for f in receiver.stage_state(ctx, tmp / "runs" / "r1")]
        cfg2 = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
        cfg2.pop("observe")
        (tmp / "config2.json").write_text(json.dumps(cfg2), encoding="utf-8")
        off = receiver.observe_before_run(receiver.Ctx(tmp / "config2.json"))
        check("O13 收货员触发前自动扫描并随状态上传 observed.jsonl；平台 403 不阻塞；没有 observe 段就不扫",
              rep is not None and rep["observed"]["total"] > 0 and "observed.jsonl" in files and off is None
              and "403" in str(rep["observers"]["usage"].get("conversations_fetch", {}).get("error", "")), f"{files} {off}")
    # O14 full path: sandbox prepare --observed-only → card → finalize → receiver ingest → pushed → not again
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        build_library(tmp)
        env = make_env(tmp)
        observe.run_scan(env, platform=False, conversations_file=tmp / "conversations.json", quiet=True)
        import receiver
        refs = tmp / "refs"
        att = receiver.stage_state(receiver.Ctx(tmp / "config.json"), tmp / "runs" / "r1")
        refs.mkdir()
        for f in att:
            shutil.copy(f, refs / f.name)
        envp = dict(os.environ, PYTHONUTF8="1", INSP_OUT=str(tmp / "out"), INSP_STATE_HOME=str(tmp / "home"), INSP_REFS=str(refs))
        run = [sys.executable, "-X", "utf8", str(PKG / "insp-nightly" / "scripts" / "insp_run.py")]
        r1 = subprocess.run(run + ["prepare", "--observed-only"], capture_output=True, text=True, encoding="utf-8", env=envp)
        tri = json.loads((tmp / "out" / "triage.json").read_text(encoding="utf-8"))
        meta = json.loads((tmp / "out" / "run_meta.json").read_text(encoding="utf-8"))
        cand = tri["observed_shortlist"][0]
        ev_file = (tmp / "out" / f"{cand['id']}.md").read_text(encoding="utf-8")
        quote = next(ln for ln in cand["abstract"].splitlines() if 12 < len(ln.strip()) < 300)
        doc = {"schema_version": "1.0", "flow_version": vc.FLOW_VERSION, "run_id": meta["run_id"],
               "generated_at": "2026-10-30T02:50:00+08:00", "status": "OK", "cards": [card_for(cand, quote)], "rejected": []}
        (tmp / "out" / "cards.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        r2 = subprocess.run(run + ["finalize"], capture_output=True, text=True, encoding="utf-8", env=envp)
        res = receiver.ingest(receiver.Ctx(tmp / "config.json"), receiver.DirSource(tmp / "out"))
        st = json.loads((env.state_dir / "state.json").read_text(encoding="utf-8"))
        pushed = {p.get("id") for p in st.get("pushed", [])}
        short3, _, _ = triage.observed_shortlist(env.state_dir, st, [], {})
        dig = "".join(p.read_text(encoding="utf-8") for p in (env.state_dir / "digest").glob("*.md"))
        check("O14 沙箱 prepare --observed-only → finalize → 收货入库 → 进 pushed、不再入围、摘要标“本地观察”",
              r1.returncode == 0 and "OBSERVED" in r1.stdout and quote in ev_file and r2.returncode == 0
              and res.get("status") == "OK" and cand["id"] in pushed and cand["id"] not in {c["id"] for c in short3}
              and "本地观察" in dig, f"prepare={r1.returncode} finalize={r2.returncode} {r2.stdout[-300:]} res={res}")
    # O15 user exclude lists ADD to built-ins (empty user list must not re-enable *secret* / node_modules)
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        build_library(tmp)
        code = tmp / "library" / "code"
        (code / "my_secret_keys.py").write_text("x = 1\n", encoding="utf-8")
        (code / "node_modules").mkdir()
        (code / "node_modules" / "dep.py").write_text("y = 2\n", encoding="utf-8")
        (code / "docs_skip").mkdir()
        (code / "docs_skip" / "a.py").write_text("z = 3\n", encoding="utf-8")
        env = make_env(tmp, {"exclude_dirs": ["docs_skip"], "exclude_globs": []})
        from observers import library as _lib
        man = _lib.scan(env)["manifest"]
        rels = " ".join(str(m.get("rel", m) if isinstance(m, dict) else m) for m in (man.values() if isinstance(man, dict) else man))
        rels = rels + " " + " ".join(man.keys()) if isinstance(man, dict) else rels
        check("O15 用户排除规则是追加：空 exclude_globs 不会放出 *secret*；node_modules 内置跳过；exclude_dirs 生效",
              "qmc.py" in rels and "secret" not in rels and "node_modules" not in rels and "docs_skip" not in rels, rels[:300])
    # O16 v1.5.1: several long functions per file + new small-certain detectors (io_in_loop / py_accum / code.smell)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        code = tmp / "library" / "code"
        code.mkdir(parents=True)
        two = long_py_function(20) + "\n" + long_py_function(18).replace("def analyse", "def analyse2") + "\n" \
            + long_py_function(16).replace("def analyse", "def analyse3")
        (code / "multi.py").write_text("import numpy as np\n" + two, encoding="utf-8")
        (code / "smelly.py").write_text("\n".join([
            "import numpy as np", "import json",
            "def run(a, b, c, d, e, f, g, h):", "    s = 0", "    for i in range(len(a)):", "        s += a[i]",
            "    for fn in ['x', 'y']:", "        cfg = json.load(open(fn))",
            "    try:", "        pass", "    except:", "        pass",
            "    out = 'D:/data/out.csv'", "    return s, out", ""]), encoding="utf-8")
        env = make_env(tmp)
        observe.run_scan(env, only=["code"], platform=False, quiet=True)
        rows = rows_of(env)
        longs = [r for r in rows if r["detector"] == "code.long_function" and "multi.py" in str(r.get("evidence_ref"))]
        smell = [r for r in rows if r["detector"] == "code.smell"]
        perf = [r for r in rows if r["detector"] == "code.perf_loop" and "smelly.py" in str(r.get("evidence_ref"))]
        kinds = {r["metrics"].get("pattern") for r in perf}
        check("O16 同一文件报多个长函数；新增吞错/写死路径/参数过多/循环读文件/循环累加",
              len(longs) == 3 and len(smell) == 1 and set(smell[0]["metrics"]["kinds"]) == {"bare_except", "hardcoded_path", "many_args"}
              and {"io_in_loop", "py_accum"} <= kinds and all(r["candidate_type"] == "optimization" for r in smell + perf),
              f"longs={len(longs)} smell={[s['metrics'] for s in smell]} perf={kinds}")
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过" + (f"；失败：{bad}" if bad else ""))
    if not bad:
        print("ALL OBSERVE TESTS PASSED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
