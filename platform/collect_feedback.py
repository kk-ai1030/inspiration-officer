#!/usr/bin/env python3
"""Run one test command and bundle everything needed for diagnosis into ONE redacted Markdown file.

Usage (run from the package root):
  python3 platform/collect_feedback.py --test T2 --run "python3 platform/run_local.py --config config.json --fetch-arxiv"
  python3 platform/collect_feedback.py --test T7 --result fail --note "第3步卡住：不知道 token 去哪拿"   # no command

Output: feedback/<test>_<timestamp>.md  -> upload it to the shared Drive folder.
Secrets (sp_live_* keys, JWTs, Bearer values, ?token=, webhook URLs, env tokens) are redacted.
Standard library only; works on Windows (UTF-8 forced) and macOS/Linux.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:  # v1.5.6: git / shell / schtasks must not pop a console window under pythonw
    from _spawn import run as _run_hidden
except ImportError:  # pragma: no cover - platform/ is normally sys.path[0] here
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _spawn import run as _run_hidden

ROOT = Path(__file__).resolve().parent.parent
MAX_FILE = 6000          # chars kept per embedded file
SECRET_PATTERNS = [
    (re.compile(r"sp_live_[A-Za-z0-9_\-]+"), "sp_live_***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "<JWT***>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"(?i)([?&](?:token|access_token|key)=)[^&\s\"']+"), r"\1***"),
    (re.compile(r"https://open\.(?:feishu|larksuite)\.(?:cn|com)/open-apis/bot/v2/hook/[A-Za-z0-9\-]+"), "<FEISHU_WEBHOOK***>"),
    (re.compile(r"(?i)(\"?(?:api[_-]?key|token|secret|password)\"?\s*[:=]\s*\"?)[^\"\s,}]{8,}"), r"\1***"),
]


def redact(text: str) -> str:
    for name, val in os.environ.items():
        if val and len(val) >= 12 and any(k in name.upper() for k in ("TOKEN", "KEY", "SECRET", "WEBHOOK")):
            text = text.replace(val, f"<${name}***>")
    for pat, rep in SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


def clip(text: str, n: int = MAX_FILE) -> str:
    if len(text) <= n:
        return text
    half = n // 2
    return text[:half] + f"\n\n... [省略 {len(text) - n} 字符] ...\n\n" + text[-half:]


def read(p: Path, n: int = MAX_FILE) -> str:
    try:
        return clip(redact(p.read_text(encoding="utf-8", errors="replace")), n)
    except OSError as exc:
        return f"<读取失败: {exc}>"


def fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text.rstrip()}\n```"


def sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:10]
    except OSError:
        return "missing"


def package_fingerprint() -> list[str]:
    rows = []
    for rel in ["platform/run_local.py", "platform/sciencepal_client.py", "platform/probe.py",
                "platform/setup_platform.py", "insp-card/SKILL.md", "insp-card/scripts/validate_cards.py",
                "insp-triage/scripts/triage.py", "scripts/selfcheck.py"]:
        rows.append(f"| {rel} | {sha(ROOT / rel)} |")
    ver = ROOT / "VERSION"
    head = f"VERSION={ver.read_text(encoding='utf-8').strip()}" if ver.exists() else "VERSION=<无>"
    try:
        g = _run_hidden(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        if g.returncode == 0:
            head += f"  git={g.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    return [head, "", "| 文件 | sha256[:10] |", "|---|---|", *rows]


def run_command(cmd: str, timeout: int) -> tuple[int, str, float]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    t0 = time.time()
    try:
        p = _run_hidden(cmd, shell=True, cwd=ROOT, capture_output=True, timeout=timeout, env=env)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out, time.time() - t0
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        return -999, out + f"\n<TIMEOUT after {timeout}s>", time.time() - t0


def state_hash(state_dir: Path) -> str:
    p = state_dir / "state.json"
    return sha(p)


def main() -> int:
    ap = argparse.ArgumentParser(description="一键收集测试反馈（自动脱敏）")
    ap.add_argument("--test", default="manual", help="测试编号，如 G4 / T2（不填记为 manual）")
    ap.add_argument("--run", help="要执行并记录的命令（在包根目录执行）")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--result", choices=["pass", "fail", "unsure"], help="你的主观判断（可选）")
    ap.add_argument("--note", default="", help="一句话说明：期望什么、看到什么")
    ap.add_argument("--timeout", type=int, default=2400)
    ap.add_argument("--out-dir", default="feedback")
    a = ap.parse_args()

    cfg_path = (ROOT / a.config) if not Path(a.config).is_absolute() else Path(a.config)
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
    base = cfg_path.parent
    state_dir = (base / cfg.get("state_dir", "./inspiration")).resolve()
    work_dir = (base / cfg.get("work_dir", "./runs")).resolve()
    before_runs = {p.name for p in work_dir.glob("run-*")} if work_dir.exists() else set()
    st_before = state_hash(state_dir)

    code, out, secs = (None, "", 0.0)
    if a.run:
        print(f"[collect_feedback] 执行: {a.run}")
        code, out, secs = run_command(a.run, a.timeout)
        print(out[-3000:])
        print(f"[collect_feedback] 退出码 {code}，耗时 {secs:.0f}s")
    st_after = state_hash(state_dir)

    new_runs = sorted(p for p in work_dir.glob("run-*") if p.name not in before_runs) if work_dir.exists() else []
    latest = new_runs[-1] if new_runs else (max(work_dir.glob("run-*"), default=None, key=lambda p: p.stat().st_mtime) if work_dir.exists() else None)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    L: list[str] = [f"# 反馈 {a.test} · {ts}", "",
                    f"- 主观结果：**{a.result or '未填'}**", f"- 说明：{redact(a.note) or '（无）'}",
                    f"- 环境：{platform.system()} {platform.release()} · Python {sys.version.split()[0]}",
                    f"- state.json 哈希：执行前 `{st_before}` → 执行后 `{st_after}`（{'有变化' if st_before != st_after else '未变化'}）", ""]
    if a.run:
        L += ["## 命令与输出", f"- 命令：`{redact(a.run)}`", f"- 退出码：**{code}** · 耗时 {secs:.0f}s", "",
              fence(clip(redact(out), 12000)), ""]
    L += ["## 包版本指纹", *package_fingerprint(), ""]
    if cfg:
        L += ["## config（脱敏）", fence(redact(json.dumps(cfg, ensure_ascii=False, indent=2)), "json"), ""]
    tokens = {k: ("已设置" if os.environ.get(k) else "未设置") for k in
              [cfg.get("token_env", "SCIENCEPAL_TOKEN"), cfg.get("setup_token_env", "SCIENCEPAL_SETUP_TOKEN"),
               "SCIENCEPAL_RUN_TOKEN", "SCIENCEPAL_TOKEN", "SCIENCEPAL_SETUP_TOKEN"]}
    L += ["## 令牌环境变量（只报是否设置）", *[f"- {k}: {v}" for k, v in tokens.items()], ""]

    runs_log = state_dir / "runs.jsonl"
    if runs_log.exists():
        lines = runs_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        L += ["## runs.jsonl 最后 3 行", fence(redact("\n".join(lines)), "json"), ""]
    probe = base / "probe_report.json"
    if probe.exists():
        L += ["## probe_report.json", fence(read(probe, 5000), "json"), ""]

    if latest:
        L += [f"## 本次运行目录 `{latest.name}`{'（本次新建）' if latest in new_runs else '（最近一次，非本次新建）'}", "",
              "| 文件 | 字节 |", "|---|---|"]
        files = sorted(p for p in latest.rglob("*") if p.is_file())
        L += [f"| {p.relative_to(latest)} | {p.stat().st_size} |" for p in files] + [""]
        tri = latest / "triage.json"
        if tri.exists():
            try:
                t = json.loads(tri.read_text(encoding="utf-8"))
                L += ["### triage 计数", fence(json.dumps({"status": t.get("status"), "counts": t.get("counts"),
                                                         "shortlisted": [s.get("id") for s in t.get("shortlisted", [])]},
                                                        ensure_ascii=False, indent=2), "json"), ""]
            except json.JSONDecodeError:
                pass
        for name, n in [("prompt.txt", 3000), ("skills_evidence.json", 4000), ("selection.json", 4000), ("cards_raw.json", 5000), ("run.json", 3000)]:
            if (latest / name).exists():
                L += [f"### {name}", fence(read(latest / name, n)), ""]
        for rp in sorted(latest.glob("reply_*.txt")):
            L += [f"### {rp.name}", fence(read(rp, 5000)), ""]
        se = latest / "stream.json"
        if se.exists():
            try:
                ev = json.loads(se.read_text(encoding="utf-8"))
                kinds: dict[str, int] = {}
                for e in ev if isinstance(ev, list) else []:
                    k = str(e.get("type") or e.get("event") or "?") if isinstance(e, dict) else "str"
                    kinds[k] = kinds.get(k, 0) + 1
                tail = [e for e in ev if isinstance(e, dict) and "_orchestrator" in e]
                L += ["### stream 事件统计", fence(json.dumps({"total": len(ev), "by_type": kinds, "orchestrator": tail},
                                                            ensure_ascii=False, indent=2), "json"), ""]
            except (json.JSONDecodeError, AttributeError):
                pass

    today_cards = sorted((state_dir / "cards").glob("*/cards.json")) if (state_dir / "cards").exists() else []
    if today_cards:
        doc = json.loads(today_cards[-1].read_text(encoding="utf-8"))
        L += [f"## 最新卡片 `{today_cards[-1].parent.name}`", "", "| id | type | title | source | canary/verify |", "|---|---|---|---|---|"]
        for c in doc.get("cards", []):
            mc = c.get("memory_canary")
            if mc is None:  # cards made before v1.2.1 kept the marker in the title
                mc = str(c.get("title", "")).rstrip().endswith("◆")
            vf = (c.get("source") or {}).get("verified")
            flags = f"canary={'Y' if mc else 'N'} skill={c.get('_trace', {}).get('skill_loaded', '?')} verified={vf}"
            L.append(f"| {c.get('id')} | {c.get('type')} | {redact(str(c.get('title')))[:60]} | {c.get('source', {}).get('ref')} | {flags} |")
        L.append("")

    wf = state_dir / "taste" / "weights.json"
    if wf.exists():
        try:
            w = json.loads(wf.read_text(encoding="utf-8"))
            L += ["## 权重（weights.json）", fence(json.dumps({k: w.get(k, 0 if k == "version" else None) for k in ("version", "type", "subfield", "history")},
                                                             ensure_ascii=False, indent=2), "json"), ""]
        except json.JSONDecodeError:
            pass
    rep = state_dir / "reports"
    for pat, n in [("learning_check_*.md", 6000), ("proposals_*.json", 3000)]:
        files = sorted(rep.glob(pat), key=lambda p: p.stat().st_mtime) if rep.exists() else []
        if files:
            L += [f"## 最新 {files[-1].name}", fence(read(files[-1], n)), ""]
    tok_file = Path(os.path.expanduser(cfg.get("token_file", "~/.sciencepal/run_token")))
    L += [f"- 令牌文件 {tok_file}: {'存在' if tok_file.is_file() else '不存在'}", ""]

    # review page health (v1.2+): is the local server up, are the Windows tasks registered, last toast result
    rv = cfg.get("review") or {}
    port = int(rv.get("port", 8765))
    L += ["## 评价页状态"]
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            L.append(f"- 127.0.0.1:{port}/api/ping: {r.status} {r.read(200).decode('utf-8', 'replace')}")
    except Exception as exc:  # noqa: BLE001 - report only
        L.append(f"- 127.0.0.1:{port}/api/ping: 未响应（{type(exc).__name__}）")
    if os.name == "nt":
        for tn in ("InspReviewServer", "InspNightly-0930", "InspNightly-2130"):
            try:
                q = _run_hidden(["schtasks", "/Query", "/TN", tn, "/V", "/FO", "LIST"], capture_output=True,
                                text=True, timeout=10, encoding="mbcs", errors="replace")
                keep = [l.strip() for l in q.stdout.splitlines()
                        if any(k in l for k in ("Status", "Last Run", "Last Result", "状态", "上次运行", "上次结果"))]
                L.append(f"- 任务 {tn}: " + ("; ".join(keep[:4]) if q.returncode == 0 else "未注册"))
            except Exception as exc:  # noqa: BLE001
                L.append(f"- 任务 {tn}: 查询失败（{type(exc).__name__}）")
    impl = state_dir / "implicit.jsonl"
    n_impl = sum(1 for _ in impl.open(encoding="utf-8")) if impl.exists() else 0
    n_ui = 0
    vf = state_dir / "verdicts.jsonl"
    if vf.exists():
        n_ui = sum(1 for l in vf.open(encoding="utf-8") if '"via": "card_ui"' in l)
    L.append(f"- verdicts.jsonl 中来自评价页的记录: {n_ui}；implicit.jsonl 行数: {n_impl}")
    slog = (base / cfg.get("work_dir", "./runs") / "scheduled.log")
    if slog.exists():
        hits = [l.strip() for l in slog.read_text(encoding="utf-8", errors="replace").splitlines()
                if "TOAST" in l or "评价页" in l or "WARN" in l or "通知" in l][-8:]
        L.append("- scheduled.log 中通知/告警相关的最后几行：" + ("" if hits else "无"))
        L += [f"  - `{redact(h)[:200]}`" for h in hits]
    tl = state_dir / "inbox" / "toast_last.json"
    if tl.exists():
        L += ["- 最近一次通知诊断（inbox/toast_last.json）：", fence(read(tl, 1500), "json")]
    L.append("")

    # dedup health (v1.2.2+): the same paper must never be pushed twice
    L += ["## 去重体检"]
    refs: dict = {}
    for f in sorted((state_dir / "cards").glob("*/cards.json")):
        try:
            for c in json.loads(f.read_text(encoding="utf-8")).get("cards", []):
                r = re.sub(r"v\d+$", "", str((c.get("source") or {}).get("ref", "")))
                refs.setdefault(r, []).append(c.get("id"))
        except (OSError, json.JSONDecodeError):
            pass
    dup = {r: ids for r, ids in refs.items() if r and len(ids) > 1}
    total = sum(len(v) for v in refs.values())
    L.append(f"- 卡片库共 {total} 张、{len(refs)} 篇不同论文；重复推送 {len(dup)} 篇" + ("" if not dup else "：" + "; ".join(f"{r}→{','.join(map(str, ids))}" for r, ids in dup.items())))
    try:
        st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        pushed = {re.sub(r"v\d+$", "", str(p.get("id", ""))) for p in st.get("pushed", [])}
        behind = [r for r in refs if r and r not in pushed]
        L.append(f"- state.json：seen_ids {len(st.get('seen_ids', []))} 条、pushed {len(pushed)} 条、bootstrap={st.get('bootstrap')}、last_run_id={st.get('last_run_id')}")
        L.append("- 卡片库里有、state.pushed 里没有的论文：" + (", ".join(behind) if behind else "无"))
        baks = sorted(p.name for p in state_dir.glob("state.json.bak*"))
        L.append("- state.json 备份文件：" + (", ".join(baks[-6:]) if baks else "无"))
    except (OSError, json.JSONDecodeError) as exc:
        L.append(f"- state.json 读取失败：{exc}")
    L.append("")

    out_dir = ROOT / a.out_dir
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{a.test}_{ts}.md"
    text = redact("\n".join(L))
    out_file.write_text(text, encoding="utf-8")
    leaked = [p.pattern for p, _ in SECRET_PATTERNS[:2] if p.search(text)]
    print(f"[collect_feedback] 已生成 {out_file}（{len(text)} 字符）{' ⚠ 疑似残留密钥: ' + str(leaked) if leaked else ''}")
    print("[collect_feedback] 上传到 Drive 的 feedback/ 文件夹，然后告诉我测试编号。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
