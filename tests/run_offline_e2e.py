#!/usr/bin/env python3
"""Offline end-to-end test (cross-platform; replaces the bash-only version — feedback3 BUG-1).

No SciencePal calls, no network. Works with `python` on Windows (no python3 / sed / process substitution needed).
    python tests/run_offline_e2e.py
T1–T7: the classic local route (run_local.py).  T8–T11: v1.4 self-bootstrap route (insp_run.py in a simulated
sandbox → bundle → receiver.py --from-dir), dedup, idempotency, BUG-6 guard.  T13–T16: web profile interview
(insp-profile → receiver), BUG-5 worker argv pass-through, setup_platform zips, review page run button + receiver API.
T17–T18 (v1.4.1): paper read mode — default "abstract" (no full text fetched, abstract-only cards accepted, links added)
vs "fulltext" (only when config/flag says so), mode resolution, prompts follow config.json without restart.
"""
from __future__ import annotations

import glob
import importlib.util
import socket
import time
import urllib.error
import urllib.request
import zipfile
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
PY = sys.executable
ENV = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
FIX = PKG / "tests" / "fixtures"


def run(args: list, cwd: Path, expect: int | None = 0) -> tuple[int, str]:
    r = subprocess.run([PY, "-X", "utf8", *map(str, args)], cwd=cwd, env=ENV, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = r.stdout + r.stderr
    if expect is not None and r.returncode != expect:
        print(out)
        raise SystemExit(f"FAIL  exit {r.returncode} (expected {expect}): {' '.join(map(str, args[:3]))}")
    return r.returncode, out


def check(name: str, cond: bool, log: str = "") -> None:
    if not cond:
        print(log[-3000:])
        raise SystemExit(f"FAIL  {name}")
    print(f"PASS  {name}")


def jload(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def jsave(p: Path, obj) -> None:
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def fresh_state(t: Path, name: str = "inspiration", seen: bool = True) -> Path:
    sd = t / name
    shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", sd)
    (sd / ".test_state").write_text("", encoding="utf-8")
    st = jload(sd / "state.json")
    if seen:
        st["bootstrap"] = False
        st["seen_ids"] = jload(FIX / "seen_ids.json")["seen_ids"]
    jsave(sd / "state.json", st)
    return sd


def make_config(t: Path) -> Path:
    cfg = jload(PKG / "platform/config.example.json")
    cfg["review"]["enabled"] = False
    cfg["fulltext"]["cache_dir"] = str(FIX / "fulltext_cache")
    cfg["state_dir"], cfg["work_dir"] = "./inspiration", "./runs"
    cfg["token_file"] = str(t / "no_run_token")          # never touch the real platform from a test
    cfg["setup_token_file"] = str(t / "no_session_token")
    cfg.setdefault("receiver", {})["push_state"] = False
    cfg["receiver"]["memory_digest"] = False
    jsave(t / "config.json", cfg)
    return t / "config.json"


def sha1(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def latest_cards(sd: Path) -> list[dict]:
    return jload(sorted(glob.glob(str(sd / "cards/*/cards.json")))[-1])["cards"]


def main() -> int:  # noqa: C901
    tmp = Path(tempfile.mkdtemp(prefix="insp_e2e_"))
    try:
        sd = fresh_state(tmp)
        make_config(tmp)
        raw, ok_reply = FIX / "raw_items.json", FIX / "fake_reply_ok.txt"
        common = ["--config", "config.json", "--raw", raw, "--no-arxiv-check"]

        print("== T1 正常运行：计数 40/32/8/0 + 2 张卡片")
        _, log = run([PKG / "platform/run_local.py", *common, "--fake-agent-output", ok_reply], tmp)
        check("T1", "'raw': 40, 'seen': 32, 'new': 8, 'negative_filtered': 0" in log and "OK: 2 张卡片" in log, log)
        c1 = latest_cards(sd)
        r1 = [json.loads(x) for x in (sd / "runs.jsonl").read_text(encoding="utf-8").splitlines()][-1]
        check("T1b 默认摘要模式：不抓原文，卡片带摘要页链接",
              "FETCH_MODE abstract" in log and "FULLTEXT" not in log and r1.get("fetch_mode") == "abstract"
              and not list((tmp / "runs").glob("*/fulltext/ft_*.md"))
              and all(c["source"].get("url", "").startswith("https://arxiv.org/abs/") and c["source"].get("read_mode") == "abstract"
                      and "fulltext_url" not in c["source"] for c in c1), log)

        print("== T2 同一输入再跑：增量为 0，不调用 agent")
        _, log = run([PKG / "platform/run_local.py", *common, "--fake-agent-output", ok_reply], tmp)
        check("T2", "NO_CANDIDATES" in log and "'new': 0" in log, log)

        print("== T3 技能未加载（flow_version 不对）：整批丢弃，状态不更新")
        st = jload(sd / "state.json")
        st["seen_ids"], st["pushed"], st["quota_used"] = st["seen_ids"][:32], [], {}
        jsave(sd / "state.json", st)
        shutil.rmtree(sd / "cards", ignore_errors=True)
        before = sha1(sd / "state.json")
        code, log = run([PKG / "platform/run_local.py", *common, "--fake-agent-output", FIX / "fake_reply_no_skill.txt"], tmp, None)
        check("T3", code == 5 and "没有加载" in log and before == sha1(sd / "state.json"), log)

        print("== T4 熔断：删除 negative.jsonl → STATE_MISSING，退出码 3")
        (sd / "negative.jsonl").rename(tmp / "neg.bak")
        code, log = run([PKG / "platform/run_local.py", *common, "--fake-agent-output", ok_reply], tmp, None)
        (tmp / "neg.bak").rename(sd / "negative.jsonl")
        check("T4", code == 3 and "STATE_MISSING" in log, log)

        print("== T5 反馈：导入 verdict 结果块；否决的来源进入负面清单")
        run([PKG / "platform/run_local.py", *common, "--fake-agent-output", ok_reply], tmp)
        cards = latest_cards(sd)
        check("T5 卡片 ID 唯一", len({c["id"] for c in cards}) == len(cards))
        last = {c["type"]: c["id"] for c in cards}
        blk = (FIX / "verdict_block.txt").read_text(encoding="utf-8").replace("ins_20261028_002", last["hypothesis"])
        (tmp / "verdict_block.txt").write_text(blk, encoding="utf-8")
        run([PKG / "platform/run_local.py", "--config", "config.json", "--import-verdicts", "verdict_block.txt"], tmp)
        run([PKG / "insp-feedback/scripts/record_verdict.py", "--config", "config.json", "--state-dir", "inspiration",
             "--card-id", last["knowledge"], "--action", "adopt", "--reason", "正好要补基线"], tmp)
        nv = len((sd / "verdicts.jsonl").read_text(encoding="utf-8").strip().splitlines())
        check("T5", '"9999.20002"' in (sd / "negative.jsonl").read_text(encoding="utf-8") and nv == 2)

        print("== T6 周报（离线）")
        _, log = run([PKG / "platform/run_weekly.py", "--config", "config.json", "--offline"], tmp)
        reps = list((sd / "reports").glob("weekly_*.md"))
        check("T6", bool(reps) and "采纳率" in reps[0].read_text(encoding="utf-8"), log)

        print("== T7 状态目录仍然有效")
        run([PKG / "insp-init/scripts/validate_insp_state.py", sd], tmp)
        print("PASS  T7")

        # ------------------------------------------------------------------ v1.4 self-bootstrap route
        t2 = tmp / "v14"
        t2.mkdir()
        sd2 = fresh_state(t2)
        make_config(t2)
        sbx = t2 / "sandbox"
        home, out, refs = sbx / "inspiration_state", sbx / "inspiration_out", sbx / "_refs"
        home.mkdir(parents=True)
        for p in sd2.rglob("*"):  # the receiver pushed the state into this conversation earlier (persisted mode)
            if p.is_file():
                (home / p.relative_to(sd2)).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, home / p.relative_to(sd2))
        runner = PKG / "insp-nightly/scripts/insp_run.py"
        sb = ["--out", out, "--state-home", home, "--refs", refs]

        print("== T8 沙箱自举：prepare → 写卡片 → finalize → 整包")
        _, log = run([runner, "prepare", *sb, "--raw", raw, "--cache-dir", FIX / "fulltext_cache", "--fetch-mode", "fulltext"], t2)
        check("T8a prepare（--fetch-mode fulltext）计数 40/32/8 + 原文 2 篇", '"raw": 40, "seen": 32, "new": 8' in log
              and '"fulltext_ok": 2' in log and "FETCH_MODE fulltext" in log
              and (out / "ft_9999.20001.md").is_file() and (out / "rel_9999.30001.md").is_file(), log)
        txt = ok_reply.read_text(encoding="utf-8")
        (out / "cards.json").write_text(txt.split("<<<INSP_CARDS_JSON", 1)[1].split("INSP_CARDS_JSON>>>", 1)[0], encoding="utf-8")
        _, log = run([runner, "finalize", *sb], t2)
        b = jload(out / "bundle.json")
        check("T8b 整包字段齐全", {"state_patch", "triage_counts", "cards", "rejected", "fulltext_index"} <= set(b)
              and b["status"] == "OK" and "<<<INSP_BUNDLE_JSON" in log and len(b["cards"]) == 2, log)
        check("T8d 全文模式：整包 fetch_mode=fulltext，卡片带全文链接",
              b.get("fetch_mode") == "fulltext" and b["fulltext_index"]
              and all(c["source"].get("read_mode") == "fulltext" and c["source"].get("fulltext_url", "").startswith("https://arxiv.org/")
                      and c["source"]["url"].startswith("https://arxiv.org/abs/") for c in b["cards"]), json.dumps(b["cards"])[:2000])
        check("T21a 收尾提示紧跟结果块：不要调用 complete、不要放附件（feedback6 DELIVERY_VALIDATION_LIMIT）",
              "不要调用 complete" in log.split("INSP_BUNDLE_JSON>>>", 1)[-1], log[-600:])
        check("T8c 会话状态已前进", len(jload(home / "state.json")["seen_ids"]) == 40)

        print("== T9 收货员：从整包入库（本地独立复核原文引文）")
        code, log = run([PKG / "platform/receiver.py", "--config", "config.json", "--from-dir", out, "--no-arxiv-check"], t2)
        cards2 = latest_cards(sd2)
        st2 = jload(sd2 / "state.json")
        check("T9", '"status": "OK"' in log and len(cards2) == 2 and len(st2["seen_ids"]) == 40 and len(st2["pushed"]) == 2
              and all(c["_trace"]["skill_loaded"].startswith("bundle:insp-run/") for c in cards2)
              and all(c["source"].get("section") for c in cards2), log)
        r9 = [json.loads(x) for x in (sd2 / "runs.jsonl").read_text(encoding="utf-8").splitlines()][-1]
        check("T9b 收货员按整包自己的模式复核（config=abstract，整包=fulltext → 照收并记 note）",
              r9.get("fetch_mode") == "fulltext" and "fetch_mode_note" in r9
              and all(c["source"].get("fulltext_url") for c in cards2), json.dumps(r9, ensure_ascii=False))

        print("== T10 幂等：同一整包再收一次 → ALREADY，不重复入库")
        _, log = run([PKG / "platform/receiver.py", "--config", "config.json", "--from-dir", out, "--no-arxiv-check"], t2)
        check("T10", '"ALREADY"' in log and len(latest_cards(sd2)) == 2, log)

        print("== T11 新会话（无状态 bootstrap）跑出同样的卡片 → 收货员按本地状态去重")
        sbx3 = t2 / "sandbox_fresh"
        sb3 = ["--out", sbx3 / "out", "--state-home", sbx3 / "home", "--refs", sbx3 / "_refs"]
        # bootstrap would shortlist papers without cached full text; --exclude (from the memory digest) keeps it offline
        excl = ",".join(i for i in jload(FIX / "seen_ids.json")["seen_ids"])
        _, log = run([runner, "prepare", *sb3, "--raw", raw, "--cache-dir", FIX / "fulltext_cache", "--exclude", excl,
                      "--fetch-mode", "fulltext"], t2)
        check("T11a 无状态也能跑（bootstrap + --exclude）", "MODE bootstrap" in log and '"new": 8' in log, log)
        # v1.5.5 (P9): one card per file in out/cards/ — finalize assembles cards.json itself
        _doc = jload(out / "cards.json")
        for _i, _c in enumerate(_doc["cards"], 1):
            (sbx3 / "out" / "cards" / f"{_i:02d}.json").write_text(json.dumps(_c, ensure_ascii=False, indent=2), encoding="utf-8")
        (sbx3 / "out" / "cards" / "rejected.json").write_text(json.dumps(_doc.get("rejected") or []), encoding="utf-8")
        _, _log = run([runner, "finalize", *sb3], t2)
        check("T11c 单卡文件：finalize 自己合成 cards.json 并通过校验",
              "CARDS 读到 2 张卡（来自 cards.json 论文卡 0 张 + 2 个观察型单卡文件）" in _log and len(jload(sbx3 / "out" / "bundle.json")["cards"]) == 2, _log)
        code, log = run([PKG / "platform/receiver.py", "--config", "config.json", "--from-dir", sbx3 / "out", "--no-arxiv-check"], t2, None)
        runs = [json.loads(x) for x in (sd2 / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
        check("T11b 重复卡片被本地去重，没有二次推送", len(latest_cards(sd2)) == 2 and runs[-1].get("dedup_dropped")
              and len(jload(sd2 / "state.json")["pushed"]) == 2, log)

        print("== T12 BUG-6：测试夹具不能写进真实状态目录")
        real = t2 / "real"
        real.mkdir()
        sdr = fresh_state(real)
        (sdr / ".test_state").unlink()
        make_config(real)
        before = sha1(sdr / "state.json")
        code, log = run([PKG / "platform/run_local.py", *common, "--fake-agent-output", ok_reply], real, None)
        code2, log2 = run([PKG / "platform/receiver.py", "--config", "config.json", "--from-dir", out, "--no-arxiv-check"], real, None)
        check("T12", code == 3 and "REFUSED" in log and code2 == 5 and "测试夹具" in (sdr / "runs.jsonl").read_text(encoding="utf-8")
              and before == sha1(sdr / "state.json"), log + log2)

        print("== T13 网页画像访谈：insp-profile 产出 → 收货员落盘 AGENT.md、更新检索式；未确认的画像拒收")
        pdir = t2 / "profile_out"
        ans = {"identity": "物理系博三，做中性原子量子计算", "interests": ["量子纠错"],
               "strengths": [{"item": "Python 数据分析", "evidence": "写过整套拟合脚本"}, "光学对准"],
               "gaps": ["深度学习训练细节"], "preferences": "中文；结论先行",
               "arxiv_categories": ["quant-ph"], "keywords": ["Rydberg", "neutral atom"]}
        pdir.mkdir()
        jsave(pdir / "profile_answers.json", ans)
        bp = PKG / "insp-profile/scripts/build_profile.py"
        _, log = run([bp, "--answers", pdir / "profile_answers.json", "--out", pdir, "--preview"], t2)
        check("T13a 预览不写文件", "PREVIEW" in log and not (pdir / "profile.json").exists(), log)
        _, log = run([bp, "--answers", pdir / "profile_answers.json", "--out", pdir, "--confirmed"], t2)
        check("T13b 确认后写 profile.json + 结果块", "<<<INSP_PROFILE_JSON" in log and (pdir / "profile.json").is_file(), log)
        run([PKG / "platform/receiver.py", "--config", "config.json", "--from-dir", pdir, "--no-arxiv-check"], t2, None)
        agent_md = sd2 / "profile" / "AGENT.md"
        q = [x for x in jload(sd2 / "state.json").get("sources", []) if x.get("kind") == "arxiv"]
        check("T13c 落盘 + 检索式同步 + 无证据能力降级",
              agent_md.is_file() and "Python 数据分析" in agent_md.read_text(encoding="utf-8")
              and "光学对准：了解" in agent_md.read_text(encoding="utf-8")
              and (not q or "Rydberg" in q[0].get("query", ""))
              and (sd2 / "profile" / "memory_pending.json").is_file())
        bad = jload(pdir / "profile.json")
        bad["provenance"]["confirmed_in_chat"] = False
        bad["identity"]["summary"] = "篡改"
        bdir = t2 / "profile_bad"
        bdir.mkdir()
        jsave(bdir / "profile.json", bad)
        run([PKG / "platform/receiver.py", "--config", "config.json", "--from-dir", bdir, "--no-arxiv-check"], t2, None)
        check("T13d 未确认的画像拒收", "篡改" not in agent_md.read_text(encoding="utf-8"))

        print("== T14 BUG-5：后台 worker 透传 --fake-agent-output，并把 argv 写进 worker.log")
        spec = importlib.util.spec_from_file_location("ra_t14", PKG / "platform/run_action.py")
        ra = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(PKG / "platform"))
        spec.loader.exec_module(ra)  # type: ignore[union-attr]
        seen_cmd: list = []

        class FakePopen:
            def __init__(self, cmd, **kw):
                seen_cmd.extend(cmd)
                self.pid = 4242
        real_popen = ra.subprocess.Popen
        ra.subprocess.Popen = FakePopen  # same module object as ours: restore right after
        try:
            ra.job_dir(sd2, "job_t14").mkdir(parents=True, exist_ok=True)
            pid = ra.spawn(t2 / "config.json", "job_t14", sd2, FIX / "fake_reply_ok.txt")
        finally:
            ra.subprocess.Popen = real_popen
        wl = (ra.job_dir(sd2, "job_t14") / "worker.log").read_text(encoding="utf-8")
        check("T14", pid == 4242 and "--fake-agent-output" in seen_cmd and "spawn argv=" in wl and "--fake-agent-output" in wl, wl)

        print("== T15 setup_platform --build-only：7 个技能 zip，脚本齐全、无 __pycache__、中文文件名带 UTF-8 标志")
        _, log = run([PKG / "platform/setup_platform.py", "--config", "config.json", "--build-only"], t2)
        zs = {n: zipfile.ZipFile(PKG / "dist" / f"{n}.zip") for n in ("insp-nightly", "insp-profile", "insp-card")}
        names = {n: z.namelist() for n, z in zs.items()}
        cjk_ok = all(i.flag_bits & 0x800 for z in zs.values() for i in z.infolist() if any(ord(ch) > 127 for ch in i.filename))
        check("T15", log.replace(chr(92), "/").count("built dist/") == 7 and len(list((PKG / "dist").glob("insp-*.zip"))) == 7 and "SKILL.md" in names["insp-profile"]
              and "scripts/build_profile.py" in names["insp-profile"] and "scripts/insp_run.py" in names["insp-nightly"]
              and "assets/state_template/state.json" in names["insp-nightly"]
              and not any("__pycache__" in x for v in names.values() for x in v) and cjk_ok, log)
        for z in zs.values():
            z.close()

        print("== T16 评价页：有“主动探索灵感”按钮；收货员接口可用；没有 API Key 时按钮给出明确错误")
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            port = so.getsockname()[1]
        srv = subprocess.Popen([PY, "-X", "utf8", str(PKG / "platform/review_server.py"), "--config", "config.json", "--serve",
                                "--port", str(port)], cwd=t2, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(40):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.5)
                    break
                except OSError:
                    time.sleep(0.25)
            _, url = run([PKG / "platform/review_server.py", "--config", "config.json", "--port", str(port), "--url"], t2)
            tok = url.strip().split("t=")[-1]

            def call(path, body=None):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                             headers={"X-Token": tok, "Content-Type": "application/json"}, method="GET" if body is None else "POST")
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, r.read().decode("utf-8")
                except urllib.error.HTTPError as e:
                    return e.code, e.read().decode("utf-8")
            c1, page = call(f"/?t={tok}")
            c2, st = call("/api/receiver/status")
            c3, rr = call("/api/run", {})
            check("T16", c1 == 200 and "主动探索灵感" in page and "/api/receiver/status" in page and c2 == 200
                  and json.loads(st).get("enabled") is True and c3 == 400 and "API Key" in rr, page[-300:] + st + rr)
        finally:
            srv.terminate()
            srv.wait(timeout=10)

        t17(raw, ok_reply)
        t18()
        t19(raw)
        print("ALL OFFLINE E2E TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def abstract_only_cards(ok_reply: Path) -> dict:
    txt = ok_reply.read_text(encoding="utf-8")
    doc = json.loads(txt.split("<<<INSP_CARDS_JSON", 1)[1].split("INSP_CARDS_JSON>>>", 1)[0])
    for c in doc["cards"]:
        c["source"].pop("fulltext_quotes", None)
        c.pop("related_evidence", None)
        c["source"]["url"] = "https://evil.example/fake"  # model-written links must be replaced by the script
        c["numbers_provenance"] = [p for p in c.get("numbers_provenance", []) if p.get("source") == "abstract"]
    return doc


def t17(raw: Path, ok_reply: Path) -> None:
    """Default abstract mode on the sandbox route + fulltext strictness + receiver follows the bundle's mode."""
    t = Path(tempfile.mkdtemp(prefix="insp_t17_"))
    try:
        sd = fresh_state(t)
        cfgp = make_config(t)
        sbx = t / "sandbox"
        home, out, refs = sbx / "inspiration_state", sbx / "inspiration_out", sbx / "_refs"
        home.mkdir(parents=True)
        for p in sd.rglob("*"):
            if p.is_file():
                (home / p.relative_to(sd)).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, home / p.relative_to(sd))
        runner = PKG / "insp-nightly/scripts/insp_run.py"
        sb = ["--out", out, "--state-home", home, "--refs", refs]
        print("== T17 默认摘要模式（沙箱路线）：不传 --fetch-mode → 不抓原文；只有摘要引文的卡片通过；链接由脚本写")
        # deliberately NO --cache-dir: abstract mode must not touch the full-text fetcher at all (offline-safe)
        _, log = run([runner, "prepare", *sb, "--raw", raw], t)
        tri = jload(out / "triage.json")
        check("T17a prepare 默认 abstract", "FETCH_MODE abstract" in log and tri.get("fetch_mode") == "abstract"
              and not list(out.glob("ft_*.md")) and not (out / "fulltext_index.json").exists()
              and "fulltext_ok" not in tri.get("counts", {}) and len(tri["shortlisted"]) >= 2
              and all(c["links"]["url"].startswith("https://arxiv.org/abs/") for c in tri["shortlisted"]), log)
        doc = abstract_only_cards(ok_reply)
        jsave(out / "cards.json", doc)
        _, log = run([runner, "finalize", *sb], t)
        b = jload(out / "bundle.json")
        check("T17b finalize 通过；整包 fetch_mode=abstract、fulltext_index 为空；链接被脚本覆盖",
              b["status"] == "OK" and len(b["cards"]) == 2 and b.get("fetch_mode") == "abstract" and b["fulltext_index"] == {}
              and all(c["source"]["url"].startswith("https://arxiv.org/abs/") and c["source"]["read_mode"] == "abstract"
                      and "fulltext_url" not in c["source"] for c in b["cards"]), log)
        # receiver: even if the user has meanwhile switched config to fulltext, an abstract bundle is validated as abstract
        cfg = jload(cfgp)
        cfg["arxiv_fetch_mode"] = "fulltext"
        jsave(cfgp, cfg)
        _, log = run([PKG / "platform/receiver.py", "--config", "config.json", "--from-dir", out, "--no-arxiv-check"], t)
        r = [json.loads(x) for x in (sd / "runs.jsonl").read_text(encoding="utf-8").splitlines()][-1]
        cards = latest_cards(sd)
        check("T17c 收货员按整包模式（abstract）入库，不因 config=fulltext 拒收",
              '"status": "OK"' in log and len(cards) == 2 and r.get("fetch_mode") == "abstract" and "fetch_mode_note" in r
              and all(c["source"]["url"].startswith("https://arxiv.org/abs/") for c in cards), log)

        print("== T17d 全文模式下，只有摘要引文的卡片会被拒（全文模式的严格性没被放松）")
        t4 = t / "ft"
        sd4 = fresh_state(t4) if t4.mkdir() is None else None
        h4, o4 = t4 / "home", t4 / "out"
        shutil.copytree(sd4, h4)
        sb4 = ["--out", o4, "--state-home", h4, "--refs", t4 / "_refs"]
        _, log = run([runner, "prepare", *sb4, "--raw", raw, "--cache-dir", FIX / "fulltext_cache", "--fetch-mode", "fulltext"], t)
        jsave(o4 / "cards.json", abstract_only_cards(ok_reply))
        code, log = run([runner, "finalize", *sb4], t, None)
        check("T17d", code == 2 and "fulltext_quotes" in log, log)
    finally:
        shutil.rmtree(t, ignore_errors=True)


def t18() -> None:
    """Mode resolution + every trigger path reads the CURRENT config.json."""
    sys.path.insert(0, str(PKG / "insp-card/scripts"))
    spec = importlib.util.spec_from_file_location("ft_t18", PKG / "insp-card/scripts/fetch_fulltext.py")
    FT = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(FT)  # type: ignore[union-attr]
    print("== T18 模式解析：默认/大小写/写错/旧字段；评价页触发指令跟随 config.json（不用重启）")
    cases = [({}, "abstract", False), ({"arxiv_fetch_mode": "abstract"}, "abstract", False),
             ({"arxiv_fetch_mode": " FullText "}, "fulltext", False), ({"arxiv_fetch_mode": "full"}, "abstract", True),
             ({"arxiv_fetch_mode": ""}, "abstract", False), ({"arxiv_fetch_mode": None}, "abstract", False),
             ({"fulltext": {"required": True}}, "abstract", False)]  # legacy v1.3 key no longer switches full text on
    got = [(FT.cfg_fetch_mode(c)[0], FT.cfg_fetch_mode(c)[1] is not None) for c, _, _ in cases]
    check("T18a", got == [(m, w) for _, m, w in cases], str(got))
    ex = jload(PKG / "platform/config.example.json")
    check("T18b 示例配置默认 abstract，且没有残留 fulltext.required", ex.get("arxiv_fetch_mode") == "abstract"
          and "required" not in ex.get("fulltext", {}))
    t = Path(tempfile.mkdtemp(prefix="insp_t18_"))
    try:
        fresh_state(t)
        cfgp = make_config(t)
        spec = importlib.util.spec_from_file_location("rcv_t18", PKG / "platform/receiver.py")
        rcv = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(PKG / "platform"))
        spec.loader.exec_module(rcv)  # type: ignore[union-attr]
        ctx = rcv.Ctx(cfgp)
        p1 = rcv.run_prompt(ctx)
        cfg = jload(cfgp)
        cfg["arxiv_fetch_mode"] = "fulltext"
        jsave(cfgp, cfg)
        p2 = rcv.run_prompt(ctx)  # same Ctx object = review page still running
        check("T18c 触发指令写明 --fetch-mode，并在改配置后立即跟随", "--fetch-mode abstract" in p1 and "--fetch-mode fulltext" in p2, p1 + "\n" + p2)

        class FakeSP:  # session-token client stand-in: records memory writes
            def __init__(self):
                self.mem, self.created, self.deleted = [], 0, 0

            def list_memory(self):
                return {"memories": list(self.mem)}

            def delete_memory(self, mid):
                self.deleted += 1
                self.mem = [m for m in self.mem if m["id"] != mid]

            def create_memory(self, text, **kw):
                self.created += 1
                self.mem.append({"id": f"m{self.created}", "content": text, "tags": kw.get("tags", [])})
        fsp = FakeSP()
        ctx.setup_client = lambda: fsp
        a1 = rcv.settings_memory(ctx)
        a2 = rcv.settings_memory(ctx)  # unchanged → no API call
        cfg["arxiv_fetch_mode"] = "abstract"
        jsave(cfgp, cfg)
        a3 = rcv.settings_memory(ctx)
        check("T18e [灵感设置] 记忆：首次写入、未变不重写、改配置后替换为一条",
              "--fetch-mode fulltext" in a1 and a1 == a2 and "--fetch-mode abstract" in a3
              and fsp.created == 2 and fsp.deleted == 1 and len(fsp.mem) == 1, f"{fsp.created} {fsp.deleted} {fsp.mem}")
        spec = importlib.util.spec_from_file_location("rl_t18", PKG / "platform/run_local.py")
        rl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rl)  # type: ignore[union-attr]
        pa = rl.build_prompt("r", "2026-10-29", {"raw": 1}, {}, "abstract")
        pf = rl.build_prompt("r", "2026-10-29", {"raw": 1}, {}, "fulltext")
        check("T18d 本地兜底指令按模式区分", "摘要模式" in pa and "ft_<arXiv ID>.md" not in pa and "全文模式" in pf and "ft_<arXiv ID>.md" in pf)
    finally:
        shutil.rmtree(t, ignore_errors=True)


def t19(raw: Path) -> None:
    """v1.4.3 (feedback5 · G8): find_quotes.py short command + full-text cap + command discipline in the skill texts."""
    import importlib.util
    t = Path(tempfile.mkdtemp(prefix="insp_t19_"))
    try:
        runner = PKG / "insp-nightly/scripts/insp_run.py"
        fq = PKG / "insp-card/scripts/find_quotes.py"
        spec = importlib.util.spec_from_file_location("vc_t19", PKG / "insp-card/scripts/validate_cards.py")
        vc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vc)  # type: ignore[union-attr]

        def sandbox(name: str) -> tuple[list, Path]:
            base = t / name
            base.mkdir()
            sd = fresh_state(base)
            home, out = base / "home", base / "out"
            shutil.copytree(sd, home)
            return ["--out", out, "--state-home", home, "--refs", base / "_refs"], out

        print("== T19 全文模式：find_quotes.py 一条短命令找引文；篇数上限；命令纪律")
        sb, out = sandbox("a")
        _, log = run([runner, "prepare", *sb, "--raw", raw, "--cache-dir", FIX / "fulltext_cache", "--fetch-mode", "fulltext"], t)
        check("T19a 全文模式默认上限 4 篇（打印 FULLTEXT_MAX 4）", "FULLTEXT_MAX 4" in log and '"fulltext_ok": 2' in log, log)
        tri = jload(out / "triage.json")
        ids = [vc.norm_id(c["id"]) for c in tri["shortlisted"]]
        fq_short = out.parent / "fq"
        _, glog = run([fq_short, ids[0]], t)  # no --dir: the installed copy defaults to this run's out dir
        check("T19g prepare 把 find_quotes 装到短路径 <workspace>/fq（无扩展名）；不带 --dir 也能找到本次原文；双 ID 命令 ≤ 60 字符",
              f"FIND_QUOTES 找引文只用这条短命令：python3 {fq_short} <ID>" in log and fq_short.is_file()
              and f"ft_{ids[0]}.md" in glog and "NOT_FOUND" not in glog
              and len(f"python3 /workspace/fq {ids[0]} {ids[-1]}") <= 60, log + glog)
        _, flog = run([fq, *ids, "--dir", out], t)
        main_txt = (out / f"ft_{ids[0]}.md").read_text(encoding="utf-8")
        quotes = [ln.strip()[1:-1] for ln in flog.splitlines() if ln.strip().startswith('"') and ln.strip().endswith('"')]
        secs = [ln.split("§", 1)[1].split(" 【", 1)[0] for ln in flog.splitlines() if ln.strip().startswith("Q") and "§" in ln]
        idx = jload(out / "fulltext_index.json")
        rel_ok = all(f"相关文献 {r['ref']}" in flog for r in idx[ids[0]]["related"])
        verbatim = quotes and all(any(vc.quote_in_abstract(q, p.read_text(encoding="utf-8"))
                                      for p in list(out.glob("ft_*.md")) + list(out.glob("rel_*.md"))) for q in quotes)
        all_heads = [h for p in list(out.glob("ft_*.md")) + list(out.glob("rel_*.md"))
                     for h in re.findall(r"^#{1,6} (.+)$", p.read_text(encoding="utf-8"), flags=re.M)]
        check("T19b find_quotes：多个 ID 一条命令；引文全部逐字可在原文找到；§章节真实存在；有【正文·摘要里没有】；顺带列出相关文献",
              bool(verbatim) and "正文·摘要里没有" in flog and rel_ok and secs and len(main_txt) > 0
              and all(vc.section_ok(s, all_heads) for s in secs), flog)
        _, slog = run([fq, ids[0], "--dir", out, "--sections"], t)
        sec = next((ln.strip()[2:] for ln in slog.splitlines() if ln.strip().startswith("- ") and "abstract" not in ln.lower()), "")
        _, rlog = run([fq, ids[0], "--dir", out, "--section", sec, "--chars", "300"], t)
        code, nlog = run([fq, "9999.99999", "--dir", out], t, None)
        check("T19c --sections / --section 代替 cat；不存在的 ID 打印 NOT_FOUND 但退出码 0（不触发连续失败熔断）",
              bool(sec) and f"§{sec}" in rlog and code == 0 and "NOT_FOUND 9999.99999" in nlog, slog + rlog + nlog)

        sb2, out2 = sandbox("b")
        _, log = run([runner, "prepare", *sb2, "--raw", raw, "--cache-dir", FIX / "fulltext_cache", "--fetch-mode", "fulltext",
                      "--fulltext-max", "1"], t)
        tri2 = jload(out2 / "triage.json")
        c2 = tri2["counts"]
        deferred = [i for i in ids if i not in [vc.norm_id(c["id"]) for c in tri2["shortlisted"]]]
        check("T19d --fulltext-max 1：只读 1 篇，其余留到下次（不进 new_ids，不会被标记为已见）",
              "FULLTEXT_CAP" in log and c2.get("fulltext_ok") == 1 and c2.get("fulltext_deferred") == len(ids) - 1
              and deferred and not any(vc.norm_id(i) in deferred for i in tri2.get("new_ids", []))
              and not (out2 / f"ft_{deferred[0]}.md").exists(), log)

        cfgp = make_config(t)
        spec = importlib.util.spec_from_file_location("rcv_t19", PKG / "platform/receiver.py")
        rcv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rcv)  # type: ignore[union-attr]
        cfg = jload(cfgp)
        cfg["arxiv_fetch_mode"] = "fulltext"
        cfg.setdefault("fulltext", {})["max_candidates"] = 3
        jsave(cfgp, cfg)
        ctx = rcv.Ctx(cfgp)
        pf = rcv.run_prompt(ctx)
        cfg["arxiv_fetch_mode"] = "abstract"
        jsave(cfgp, cfg)
        pa = rcv.run_prompt(ctx)
        check("T19e 按钮触发指令：全文模式带 --fulltext-max（跟随 config）+ find_quotes；摘要模式不带",
              "--fulltext-max 3" in pf and "python3 /workspace/fq <ID>" in pf and "--fulltext-max" not in pa, pf + "\n" + pa)

        card = (PKG / "insp-card/SKILL.md").read_text(encoding="utf-8")
        night = (PKG / "insp-nightly/SKILL.md").read_text(encoding="utf-8")
        sysp = (PKG / "platform/agent_system_prompt.md").read_text(encoding="utf-8")
        check("T19f 技能纪律：find_quotes 短命令 + 禁止 && 串 grep + 连续失败 2 次就交付",
              all("/workspace/fq <ID>" in x for x in (card, night, sysp)) and "命令纪律" in card and "&&" in card
              and "连续失败 2 次" in card and "--fulltext-max" in night)
        # ---- T20 (v1.4.4, feedback6 H1): platform import rejects cross-skill file references (missing_file)
        spec = importlib.util.spec_from_file_location("skill_refs_t20", PKG / "platform/skill_refs.py")
        sr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sr)  # type: ignore[union-attr]
        skills = [p for p in sorted(PKG.glob("insp-*")) if (p / "SKILL.md").is_file()]
        bad = [b for s in skills for b in sr.problems(s)]
        check("T20a 7 个技能的 .md 里引用的 scripts/ references/ assets/ 文件都在各自包内（平台 missing_file 校验）",
              len(skills) == 7 and not bad, "\n".join(bad))
        fake = t / "insp-fake"
        shutil.copytree(PKG / "insp-nightly", fake)
        with (fake / "SKILL.md").open("a", encoding="utf-8") as f:
            f.write("\n- 用 `python3 /workspace/skills/insp-card/scripts/find_quotes.py <ID>`\n")
        fb = sr.problems(fake)
        check("T20b 复现 feedback6：nightly 里写 insp-card 的完整路径 → 本地检查报 scripts/find_quotes.py 缺失",
              len(fb) == 1 and "scripts/find_quotes.py" in fb[0], "\n".join(fb))
    finally:
        shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
