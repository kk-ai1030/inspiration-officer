#!/usr/bin/env python3
"""v1.5.1 评价页端到端测试（真浏览器，离线，不连平台）。

覆盖 U1–U9：
  U1 本地观察卡片标签“本地观察 · 检测器”与“补位”
  U2 按 6（立刻动手，auto 模式）→ 自动生成动手单并建任务 → 后台假任务跑完 → 回执面板出现结论与检查
  U3 回执面板“结果可用”确认
  U4 confirm 模式：按 6 弹出动手单；清空“你提供”的参数 → 不能开跑；填上 → 建任务
  U5 历史列表每一条可点开详情（没有分类按钮）
  U6 历史详情 ←/→ 翻页、空格翻面
  U7 历史详情 E 改写 → 保存成功，分类不变（列表显示“采纳 · 已改写”，cards.json 状态仍是 adopted）
  U8 Esc：详情 → 列表 → 评价
  U9 撤销“立刻动手”会取消未开始的任务（服务端接口）

需要 playwright + chromium（沙箱 / 开发机：pip install playwright && playwright install chromium）。
没有 playwright 时打印 SKIP 并返回 0（selfcheck 里不算失败）。
用法：python tests/ui_e2e_test.py [--shots DIR]
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  —— {detail}" if detail and not ok else ""), flush=True)


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def card(i: int, ctype: str, **kw) -> dict:
    c = {"id": f"ins_20261030_00{i}", "type": ctype, "created_at": "2026-10-30T02:40:00+08:00",
         "source": {"kind": "arxiv", "ref": f"2610.1000{i}v1", "quote": f"quote {i}", "verified": True, "read_mode": "abstract"},
         "title": f"卡片{i}", "one_line": f"一句话{i}", "why_me": "和当前项目有关", "hypothesis": "" if ctype != "hypothesis" else "门控能降成本",
         "minimal_check": "在 20 个任务上对比", "expected_observable": "解决率不降", "falsifier": "解决率下降超过 2 个点",
         "magnitude": "约 20 个任务", "cost_estimate": {"tokens": "~20k", "wall_clock": "1h", "risk": "low"},
         "status": "proposed", "verdict": None}
    c.update(kw)
    return c


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: 没有安装 playwright（pip install playwright && playwright install chromium）")
        return 0
    shots = Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="insp_ui_"))
    shutil.copytree(PKG / "insp-init/assets/灵感状态模板/inspiration", tmp / "inspiration")
    sd = tmp / "inspiration"
    cfg = json.loads((PKG / "platform/config.example.json").read_text(encoding="utf-8"))
    cfg["state_dir"] = "./inspiration"
    cfg["review"].update(windows_toast=False, fallback_open_browser=False)
    cfg["receiver"] = {"enabled": False}
    cfg["actions"].update(enabled=True, auto_tiers=["T0", "T1"], start_now_mode="direct", cancel_grace_s=1)
    os.environ["INSP_NO_OPEN"] = "1"   # v1.5.2: /api/action/reveal must not really open Explorer in tests
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    obs = card(2, "optimization", title="solver.py：吞错、写死路径（3 处）", subfield="code",
               source={"kind": "workspace", "ref": "obs_0123456789ab", "quote": "except: pass", "verified": True,
                       "read_mode": "observed", "detector": "code.smell", "backfill": True,
                       "evidence_ref": "library://code/solver.py#L12"})
    cards = [card(1, "knowledge"), obs, card(3, "hypothesis"), card(4, "knowledge"), card(5, "knowledge")]
    (sd / "cards/2026-10-30").mkdir(parents=True)
    (sd / "cards/2026-10-30/cards.json").write_text(json.dumps({"date": "2026-10-30", "cards": cards}, ensure_ascii=False),
                                                    encoding="utf-8")
    fake = tmp / "fake.txt"
    fake.write_text("""@@INSP_ACTION_RESULT_START@@
{"job_id": "__JOB_ID__", "status": "done", "summary": "跑完：新版 0.41 vs 旧版 0.38",
 "steps": [{"cmd": "python check.py", "exit_code": 0}],
 "outputs": [{"path": "/workspace/inspiration_actions/__JOB_ID__/summary.md", "what": "整理文档"},
             {"path": "/workspace/inspiration_actions/__JOB_ID__/check.py", "what": "脚本"},
             {"path": "/workspace/inspiration_actions/__JOB_ID__/report.md", "what": "报告"}],
 "minimal_check_result": {"observed": "0.41 vs 0.38", "criterion": "解决率不降", "verdict": "supported", "evidence": "check.py"},
 "brief_result": {"output_measured": "0.41", "baseline_measured": "0.38", "criterion_met": true, "deliverables": []},
 "next_step": "扩大样本"}
@@INSP_ACTION_RESULT_END@@
""", encoding="utf-8")
    RA = load("platform/run_action.py", "run_action_ui")
    fake.write_text(fake.read_text(encoding="utf-8").replace("@@INSP_ACTION_RESULT_START@@", RA.RES_START)
                    .replace("@@INSP_ACTION_RESULT_END@@", RA.RES_END), encoding="utf-8")
    os.environ["INSP_FAKE_AGENT_OUTPUT"] = str(fake)
    RS = load("platform/review_server.py", "review_server_ui")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ready, holder = threading.Event(), []
    threading.Thread(target=RS.serve, args=(sd, port, 0, ready, holder, cfg_path), daemon=True).start()
    ready.wait(10)
    url = f"http://127.0.0.1:{port}/?t={RS.get_token()}"

    def cards_status(cid: str) -> str:
        doc = json.loads((sd / "cards/2026-10-30/cards.json").read_text(encoding="utf-8"))
        return next(c for c in doc["cards"] if c["id"] == cid)["status"]

    def shot(page, name):
        if shots:
            page.screenshot(path=str(shots / f"{name}.png"), full_page=True)

    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 900, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#cd")

        def goto_card(cid):
            page.evaluate(f"k=queue.findIndex(c=>c.id==='{cid}');render()")
            page.wait_for_selector("#cd")

        # U1
        goto_card("ins_20261030_002")
        tags = page.inner_text("#cd .tags")
        check("U1 本地观察标签 + 补位", "本地观察 · 代码小问题" in tags and "补位" in tags, tags)
        shot(page, "u1_observed_tag")
        page.keyboard.press("1")  # adopt
        page.wait_for_timeout(400)
        # U2 start_now auto
        goto_card("ins_20261030_001")
        page.keyboard.press("6")
        page.wait_for_timeout(800)
        jobs = RA.list_jobs(sd)
        check("U2a 按 6 自动建任务（带动手单）", len(jobs) == 1 and jobs[0].get("brief") and jobs[0]["card_id"] == "ins_20261030_001",
              str([(j["card_id"], j["status"]) for j in jobs]))
        for _ in range(40):
            if RA.list_jobs(sd)[0]["status"] in RA.TERMINAL:
                break
            time.sleep(0.5)
        j = RA.list_jobs(sd)[0]
        check("U2b 后台任务跑完", j["status"] == "succeeded", f"{j['status']} {j.get('error')}")
        page.click("#rcpt")
        page.wait_for_selector(".job")
        txt = page.inner_text("#stage")
        check("U2c 回执面板：结论 + 判据 + 检查", "结论：成立" in txt and "判据：满足" in txt and "通过：动手单的交付物都在" in txt, txt[:600])
        page.click("summary")
        shot(page, "u2_receipt")
        # U3 ack
        page.click("[data-ack=accept]")
        page.wait_for_timeout(600)
        check("U3 回执确认“结果可用”", (RA.read_job(sd, j["job_id"]).get("ack") or {}).get("result") == "accept")
        # U4 form mode (v1.5.6): the user's config still says "auto" → 按 6 always opens the 1.5.1 动手单 form first
        cfg["actions"]["start_now_mode"] = "auto"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        page.keyboard.press("Escape")
        page.wait_for_selector("#cd")
        goto_card("ins_20261030_003")
        page.keyboard.press("6")
        page.wait_for_selector("#modal.on .mbox")
        mtxt = page.inner_text("#modal")
        check("U4a 按 6 先弹出动手单（场景/参数/方法/产出/对照/判据/交付），还没建任务",
              all(w in mtxt for w in ("动手单", "场景", "参数", "方法", "产出", "对照", "判据", "交付", "确认开跑"))
              and len(RA.list_jobs(sd)) == 1 and page.input_value("#modal textarea[data-b=scene]") != "", mtxt[:300])
        shot(page, "u4_brief_modal")
        page.fill("#prs .prow:last-child input[data-f=value]", "")
        page.select_option("#prs .prow:last-child select", "user")
        page.click("#bgo")
        page.wait_for_timeout(600)
        msg = page.inner_text("#modal")
        check("U4b “你提供”参数为空不能开跑", page.is_visible("#modal.on") and "补充" in msg and len(RA.list_jobs(sd)) == 1, msg[:300])
        page.fill("#prs .prow.need input[data-f=value]", "3")
        page.click("#bgo")
        page.wait_for_timeout(800)
        jobs = {x["card_id"]: x for x in RA.list_jobs(sd)}
        b3 = (jobs.get("ins_20261030_003") or {}).get("brief") or {}
        check("U4c 填好后建任务，动手单是你改过的", "ins_20261030_003" in jobs and b3.get("origin") == "user_edit"
              and any(pp.get("value") == "3" and pp.get("source") == "user" for pp in b3.get("params", [])), json.dumps(b3, ensure_ascii=False)[:300])
        # U5 history list → detail
        page.click("#hist")
        page.wait_for_selector(".list .row")
        rows = page.locator(".list .row").count()
        check("U5a 历史列表 3 条", rows == 3, str(rows))
        shot(page, "u5_history_list")
        page.locator(".list .row").nth(rows - 1).click()   # oldest = the adopted observed card
        page.wait_for_selector("#cd")
        check("U5b 详情显示卡片且没有分类按钮", page.locator(".acts button").count() == 0 and "本地观察" in page.inner_text("#cd .tags")
              and "你的评价：采纳" in page.inner_text(".hverd"))
        # U6 paging + flip
        pos1 = page.inner_text("#pos")
        page.keyboard.press("ArrowLeft")
        pos2 = page.inner_text("#pos")
        page.keyboard.press(" ")
        flipped = "flipped" in (page.get_attribute("#cd", "class") or "")
        page.keyboard.press("ArrowRight")
        check("U6 翻页 + 翻面", pos1 != pos2 and flipped and page.inner_text("#pos") == pos1, f"{pos1} → {pos2}")
        # U7 edit in history
        page.keyboard.press("e")
        page.fill("#cd textarea[data-k=one_line]", "历史里改写的一句话")
        page.keyboard.press("Control+Enter")
        page.wait_for_timeout(600)
        shot(page, "u7_history_edit")
        last = [json.loads(l) for l in (sd / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()][-1]
        check("U7a 改写已记录", last["action"] == "edited" and last["card_id"] == "ins_20261030_002"
              and last["edit_diff"]["one_line"]["after"] == "历史里改写的一句话")
        check("U7b 分类不变（cards.json 仍是 adopted）", cards_status("ins_20261030_002") == "adopted", cards_status("ins_20261030_002"))
        page.keyboard.press("Escape")
        page.wait_for_selector(".list .row")
        lt = page.inner_text(".list")
        check("U7c 列表显示“采纳 · 已改写”", "采纳 · 已改写" in lt, lt)
        page.locator(".list .row").nth(rows - 1).click()
        check("U7d 再点开看到改写后的内容", "历史里改写的一句话" in page.inner_text("#cd"))
        # U8 Esc chain
        page.keyboard.press("Escape")
        page.wait_for_selector(".list .row")
        page.keyboard.press("Escape")
        page.wait_for_selector(".acts button")
        check("U8 Esc：详情 → 列表 → 评价", page.locator(".acts button").count() == 4)
        # ---- v1.5.2 ----
        # U10 receipts never show a local absolute path; receipt.md / folder are links handled by the local server
        page.click("#rcpt")
        page.wait_for_selector(".job")
        st_txt = page.inner_text("#stage")
        api_txt = page.evaluate("fetch('/api/actions',{headers:{'X-Token':TOKEN}}).then(r=>r.text())") if page.evaluate("typeof TOKEN!=='undefined'") else ""
        leak = [s for s in (str(tmp), str(sd), str(Path.home())) if s in st_txt or (api_txt and s in api_txt)]
        links = page.locator("[data-rv]").count()
        page.click("[data-rv][data-what=folder]")
        page.wait_for_timeout(500)
        msg = page.inner_text("#toast")
        check("U10 回执里没有本地绝对路径；完整回执是链接（打开文件 / 所在文件夹）",
              not leak and links >= 2 and links % 2 == 0 and "receipt.md" in st_txt and "已在本机打开所在文件夹" in msg, f"leak={leak} links={links} msg={msg}")
        shot(page, "u10_receipt_links")
        # U11 a notification link (…&act=<job_id>) opens the receipts view and highlights that job
        page.goto(url + "&act=" + j["job_id"])
        page.wait_for_selector(".job.hl")
        hl = page.get_attribute(".job.hl", "id")
        check("U11 通知链接 act= → 回执页并高亮那一条", hl == "job-" + j["job_id"] and page.inner_text("#pos") == "回执"
              and "act=" not in page.evaluate("location.href"), f"{hl} {page.evaluate('location.href')}")
        shot(page, "u11_highlight")
        # U12 wording: no internal component name on the page; new button slogan
        html = page.content()
        check("U12 前端没有“收货员”；按钮是“主动探索灵感”", "收货员" not in html and "跑今天的灵感" not in html
              and page.inner_text("#runbtn") == "主动探索灵感", page.inner_text("#runbtn"))
        page.keyboard.press("Escape")
        page.wait_for_selector(".acts button")
        check("U0 页面没有 JS 报错", not errors, "; ".join(errors))
        b.close()
    # U9 undo cancels a not-started job (server API, no browser)
    cfg["actions"]["start_now_mode"] = "direct"
    cfg["actions"]["auto_tiers"] = []
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    import urllib.request

    def call(path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Token": RS.get_token(), "Content-Type": "application/json", "Host": f"127.0.0.1:{port}"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
    r = call("/api/verdict", {"card_id": "ins_20261030_004", "action": "start_now"})
    jid = (r.get("action") or {}).get("job_id")
    st0 = RA.read_job(sd, jid)["status"] if jid else None
    u = call("/api/undo", {"card_id": "ins_20261030_004"})
    check("U9 撤销“立刻动手”取消未开始的任务", jid and st0 in ("queued", "planning") and jid in (u.get("cancelled_jobs") or [])
          and RA.read_job(sd, jid)["status"] == "cancelled", f"{r.get('action')} {u}")
    # U9b undo also cancels a job that is already RUNNING (Q4: the worker grabbed it within 1 s)
    r = call("/api/verdict", {"card_id": "ins_20261030_005", "action": "start_now"})
    jid2 = (r.get("action") or {}).get("job_id")
    if jid2:
        jj = RA.read_job(sd, jid2)
        jj["status"] = "running"
        RA.save(sd, jj, "started")
    u2 = call("/api/undo", {"card_id": "ins_20261030_005"})
    check("U9b 撤销“立刻动手”也能取消已经在跑的任务", bool(jid2) and jid2 in (u2.get("cancelled_jobs") or [])
          and RA.read_job(sd, jid2)["status"] == "cancelled" and RA.read_job(sd, jid2).get("cancelled_from") == "running", f"{r.get('action')} {u2}")
    holder[0].shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    print("ALL UI E2E TESTS PASSED" if not bad else "UI E2E FAILED: " + ", ".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
