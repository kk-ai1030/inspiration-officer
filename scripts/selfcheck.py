#!/usr/bin/env python3
"""一站式自检（合并了原 validate_package.py 与 release_gate.py）。只用标准库，Windows / macOS / Linux 都能跑。

    python scripts/selfcheck.py package                       # 包结构 + 各技能脚本自测（原 validate_package.py）
    python scripts/selfcheck.py g1                         # G1 离线回归：跑全部离线测试，写证据文件
    python scripts/selfcheck.py g4                         # G4 评价写入→读取一致（临时目录里模拟，不碰真实数据）
    python scripts/selfcheck.py g4 --state inspiration     # G4 只读审计你真实的 inspiration/（不写任何文件）
    python scripts/selfcheck.py g5 [路径 ...]              # G5 密钥 / 个人信息扫描（默认扫本包；可加 zip、反馈文件）
    python scripts/selfcheck.py g5 --deny-file my_words.txt   # 另加“我自己的敏感词”（姓名、邮箱、机构……，一行一个）
    python scripts/selfcheck.py g3 --config config.json    # G3 真实运行证据：最近一次运行 + 平台 loaded 字段 + 卡片抽查
    python scripts/selfcheck.py skills --config config.json   # 补查收货员收回来的运行的平台 loaded 字段（需要会话令牌）
    python scripts/selfcheck.py all                        # g1 + g4 + g5（不联网）
    python scripts/selfcheck.py stale [--clean]            # 原地覆盖升级后：列出 / 移走新版已删除的旧文件（按 MANIFEST.txt）

每一项都会在 release_evidence/ 下写一份 Markdown 证据（G<n>_<机器名>_<时间>.md），把它附在发布记录或反馈里。
退出码：0 全部通过 · 1 有失败 · 2 用法 / 前置条件不满足（例如 G3 / skills 没有会话令牌）· 3 skills 有查不到的运行。
G2（干净 Windows 安装）和 G6（CONTRIBUTING.md）是人工门槛，见 docs/weak-beta发布门槛与证据.md。
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import platform as pyplatform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
EVID = ROOT / "release_evidence"
PY = sys.executable



PKG_TOP = {"docs", "insp-card", "insp-feedback", "insp-init", "insp-nightly", "insp-profile", "insp-review", "insp-triage",
           "platform", "scripts", "tests", "mcp"}


def pkg_files():
    """v1.5.3 (R1 bug): only the package's own files. The user's state dir (inspiration/: actions/*/attach/local_*.py are
    redacted copies with «已脱敏» and do not compile), library/, research/ … are runtime data, not package source."""
    for f in ROOT.iterdir():
        if f.is_file():
            yield f
        elif f.name in PKG_TOP:
            yield from f.rglob("*")


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def header(gate: str) -> list[str]:
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "?"
    return [f"# {gate} 证据", "",
            f"- 包版本：`{ver}`",
            f"- 时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"- 系统：{pyplatform.platform()}",
            f"- Python：{sys.version.split()[0]}（{PY}）",
            f"- 机器名（可删）：{pyplatform.node()}", ""]


def write_evidence(gate: str, lines: list[str]) -> Path:
    EVID.mkdir(exist_ok=True)
    p = EVID / f"{gate}_{re.sub(r'[^A-Za-z0-9_-]', '_', pyplatform.node())[:20]}_{now_tag()}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class Checks:
    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def __call__(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"\n      {detail[:600]}"))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.rows)

    def table(self) -> list[str]:
        out = ["| 检查项 | 结果 | 说明 |", "|---|---|---|"]
        for n, ok, d in self.rows:
            out.append(f"| {n} | {'PASS' if ok else '**FAIL**'} | {d[:300].replace('|', '/').replace(chr(10), ' ')} |")
        return out


# ============================================================================ package（原 validate_package.py）
EXPECTED = {"insp-nightly", "insp-profile", "insp-init", "insp-triage", "insp-card", "insp-feedback", "insp-review"}
REMOVED = ["platform/windows/register_tasks.ps1", "platform/run_nightly.py", "insp-nightly/references/夜间运行协议.md"]
REQUIRED = ["VERSION", "insp-card/scripts/find_quotes.py", "platform/skill_refs.py", "insp-nightly/scripts/vendor/find_quotes.py", "useguide.md", "CHANGELOG.md", "platform/receiver.py", "platform/profile_tool.py", "platform/run_local.py",
            "platform/windows/remove_legacy_tasks.ps1", "platform/windows/start_review_server.ps1",
            "insp-nightly/scripts/insp_run.py", "insp-nightly/references/运行协议.md", "insp-profile/scripts/build_profile.py",
            "tests/mock_receiver_test.py", "README.md", "CONTRIBUTING.md", "docs/开发文档.md", "docs/weak-beta发布门槛与证据.md", "scripts/selfcheck.py", "scripts/build_release.py", ".gitignore",
            "platform/observe.py", "platform/observers/common.py", "platform/observers/observed.py", "tests/observe_test.py"]
CANARY = "insp-card/1.1.0"
FORBIDDEN = {"TODO": "残留 TODO", "FIXME": "残留 FIXME", "TBD": "残留 TBD"}
EXTRANEOUS = {"README.md", "CHANGELOG.md", "INSTALLATION_GUIDE.md"}


def frontmatter(text: str, label: str, errors: list[str]) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
    if not m:
        errors.append(f"{label}: frontmatter 缺失")
        return {}
    fields = dict(re.findall(r"^([A-Za-z0-9_-]+):\s*(.*)$", m.group(1), flags=re.M))
    if set(fields) != {"name", "description"}:
        errors.append(f"{label}: frontmatter 必须且只能包含 name 和 description")
    if len(fields.get("description", "")) > 2000:
        errors.append(f"{label}: description 超过 SciencePal 上限 2000 字符")
    return fields


def package_check(a=None) -> int:
    """包结构 + 各技能脚本自测。"""
    errors: list[str] = []
    actual = {p.name for p in ROOT.iterdir() if p.is_dir() and p.name.startswith("insp-")}
    if actual != EXPECTED:
        errors.append(f"Skill 集合不一致：missing={EXPECTED - actual} extra={actual - EXPECTED}")
    for s in sorted(EXPECTED & actual):
        d = ROOT / s
        sk = d / "SKILL.md"
        if not sk.is_file():
            errors.append(f"{s}: 缺少 SKILL.md")
            continue
        text = sk.read_text(encoding="utf-8")
        fm = frontmatter(text, s, errors)
        if fm.get("name") != s:
            errors.append(f"{s}: frontmatter name 与目录名不一致")
        if len(text.encode()) > 200_000:
            errors.append(f"{s}: SKILL.md 超过 body_markdown 上限")
        for m in re.finditer(r"\]\(([^)]+)\)", text):
            tgt = m.group(1).split("#", 1)[0].strip()
            if tgt and not tgt.startswith(("http://", "https://", "mailto:", "<")) and not (d / unquote(tgt)).exists():
                errors.append(f"{s}: 本地链接不存在：{tgt}")
        ay = d / "agents" / "openai.yaml"
        if not ay.is_file():
            errors.append(f"{s}: 缺少 agents/openai.yaml")
        else:
            y = ay.read_text(encoding="utf-8")
            sd = re.search(r'short_description:\s*"(.*?)"', y)
            dp = re.search(r'default_prompt:\s*"(.*?)"', y)
            if not re.search(r'display_name:\s*"(.+?)"', y):
                errors.append(f"{s}: display_name 缺失或未加引号")
            if not sd or not 25 <= len(sd.group(1)) <= 64:
                errors.append(f"{s}: short_description 必须为 25–64 字符")
            if not dp or f"${s}" not in dp.group(1):
                errors.append(f"{s}: default_prompt 必须包含 ${s}")
        if s != "insp-card":
            for f in d.rglob("*.md"):
                if CANARY in f.read_text(encoding="utf-8"):
                    errors.append(f"{f.relative_to(ROOT)}: 金丝雀 {CANARY} 只能出现在 insp-card 中")

    for rel in REMOVED:
        if (ROOT / rel).exists():
            errors.append(f"{rel}: v1.4 已删除，不应再出现")
    for rel in REQUIRED:
        if not (ROOT / rel).exists():
            errors.append(f"{rel}: 缺失")
    for ps1 in (ROOT / "platform" / "windows").glob("*.ps1"):
        if not ps1.read_bytes().startswith(b"\xef\xbb\xbf"):
            errors.append(f"{ps1.relative_to(ROOT)}: 缺 UTF-8 BOM（Windows PowerShell 5.1 会把中文读乱）")
    r = subprocess.run([sys.executable, str(ROOT / "platform" / "skill_refs.py")], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:  # v1.4.4: same rule as the platform's import check (HTTP 400 missing_file)
        errors.append("技能引用了本包里没有的文件（平台导入会报 missing_file）：" + r.stdout.strip()[:400])
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync_vendor.py"), "--check"], capture_output=True, text=True)
    if r.returncode != 0:
        errors.append("insp-nightly/scripts/vendor 与原文件不一致：运行 python scripts/sync_vendor.py " + (r.stdout + r.stderr).strip()[:200])
    for f in pkg_files():
        if f.is_file() and "__pycache__" not in f.parts and "dist" not in f.parts and f.suffix in {".md", ".py", ".json"}:
            if "run_nightly" in f.read_text(encoding="utf-8", errors="ignore") and f.name not in {"CHANGELOG.md", "selfcheck.py", "run_local.py"}:
                errors.append(f"{f.relative_to(ROOT)}: 仍引用已删除的 run_nightly")
    # v1.4.5 (feedback6): every delivery path must tell the agent NOT to call the complete tool
    for rel in ["platform/agent_system_prompt.md", "insp-nightly/SKILL.md", "insp-card/SKILL.md", "insp-profile/SKILL.md",
                "insp-feedback/SKILL.md", "insp-nightly/scripts/insp_run.py", "insp-profile/scripts/build_profile.py",
                "platform/receiver.py", "platform/run_local.py"]:
        if "complete" not in (ROOT / rel).read_text(encoding="utf-8") or "不要调用 complete" not in (ROOT / rel).read_text(encoding="utf-8").replace("`", "").replace("不调用 complete", "不要调用 complete"):
            errors.append(f"{rel}: 缺少收尾纪律“不要调用 complete 工具”（feedback6）")
    words = [base64.b64decode(x).decode("utf-8").lower() for x in BUILTIN_DENY_B64]
    for f in pkg_files():
        if f.is_file() and not ({"__pycache__", "dist", "release", "release_evidence", "runs"} & set(f.parts)) and f.suffix in {".md", ".py", ".json", ".html", ".yaml", ".ps1", ".txt"}:
            low = f.read_text(encoding="utf-8", errors="ignore").lower()
            if any(w in low for w in words):
                errors.append(f"{f.relative_to(ROOT)}: 含内置禁用词（第三方署名 / 个人账号），删掉后再发")
    for f in [ROOT / "platform" / "agent_system_prompt.md"]:
        if CANARY in f.read_text(encoding="utf-8"):
            errors.append(f"{f.relative_to(ROOT)}: 系统提示词不得包含金丝雀")
    spec = importlib.util.spec_from_file_location("rn", ROOT / "platform" / "run_local.py")
    rn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rn)  # type: ignore[union-attr]
    if CANARY in rn.build_prompt("r", "2026-01-01", {}):
        errors.append("run_local.build_prompt 泄露了金丝雀")

    for p in pkg_files():
        if "__pycache__" in p.parts or "dist" in p.parts:
            continue
        rel = p.relative_to(ROOT)
        if p.is_file() and p.name in EXTRANEOUS and rel.parts[0].startswith("insp-"):
            errors.append(f"{rel}: 多余文档")
        if p.suffix == ".py":
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as exc:
                errors.append(f"{rel}: 编译失败 {exc}")
        if p.suffix == ".json" and p.is_file():
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                errors.append(f"{rel}: JSON 解析失败 {exc}")
        if p.suffix in {".md", ".py"} and p.is_file() and rel.parts[0].startswith("insp-"):
            t = p.read_text(encoding="utf-8")
            for frag, msg in FORBIDDEN.items():
                if re.search(rf"\b{frag}\b", t):
                    errors.append(f"{rel}: {msg}")

    for script in sorted(ROOT.glob("insp-*/scripts/*.py")):
        if "--self-test" in script.read_text(encoding="utf-8"):
            r = subprocess.run([sys.executable, str(script), "--self-test"], capture_output=True, text=True)
            print(r.stdout.strip() or r.stderr.strip())
            if r.returncode != 0:
                errors.append(f"{script.relative_to(ROOT)}: 自测试失败")

    if errors:
        print("INVALID:")
        for e in errors:
            print(f"- {e}")
        return 1
    print(f"OK: 包结构有效（{len(EXPECTED)} 个 Skill）")
    return 0


# ============================================================================ G1
SUITES = [
    ("包结构 + 各脚本自测", ["scripts/selfcheck.py", "package"], "OK: 包结构有效"),
    ("端到端离线 T1–T20", ["tests/run_offline_e2e.py"], "ALL OFFLINE E2E TESTS PASSED"),
    ("模拟平台 M1–M8", ["tests/mock_platform_test.py"], "ALL MOCK PLATFORM TESTS PASSED"),
    ("安装脚本 S1–S6", ["tests/mock_setup_test.py"], "ALL MOCK SETUP TESTS PASSED"),
    ("收货员 R1–R12", ["tests/mock_receiver_test.py"], "ALL MOCK RECEIVER TESTS PASSED"),
    ("arXiv 核验（离线打桩）", ["tests/arxiv_check_test.py"], None),
    ("学习闭环彩排", ["tests/learning_loop_sim.py"], None),
    ("评价页服务自测", ["platform/review_server.py", "--self-test"], None),
    ("立刻动手自测", ["platform/run_action.py", "--self-test"], None),
    ("画像工具自测", ["platform/profile_tool.py", "--self-test"], None),
    ("平台字段补查自测", ["scripts/selfcheck.py", "skills", "--self-test"], "SELF-TEST OK: skills"),
    ("本地观察 O1–O15（v1.5）", ["tests/observe_test.py"], "ALL OBSERVE TESTS PASSED"),
    ("动手单自测（v1.5.1）", ["platform/action_brief.py", "--self-test"], "SELF-TEST OK: action_brief"),
    ("状态迁移自测（v1.5.1）", ["platform/migrate_state.py", "--self-test"], "SELF-TEST OK: migrate_state"),
    ("演示重置自测（v1.5.3）", ["platform/demo_reset.py", "--self-test"], "SELF-TEST OK: demo_reset"),
    ("追问细节自测（v1.5.3）", ["platform/ask_detail.py", "--self-test"], "SELF-TEST OK: ask_detail"),
    ("v1.5.3 测试回归 S3b–S8（v1.5.4）", ["tests/v154_regression_test.py"], "V154 OK"),
    ("v1.5.4 测试回归 P2–P12（v1.5.5）", ["tests/v155_regression_test.py"], "V155 OK"),
    ("无窗口子进程（v1.5.6，nowindow 补丁）", ["tests/no_window_test.py"], "NO_WINDOW OK"),
    ("追问 A 类 / 立刻动手表单 A1–A10 F1–F6（v1.5.6）", ["tests/v156_regression_test.py"], "V156 OK"),
    ("CARD1 删卡复活 / 超配额 / finalize 循环 C1–C10（v1.5.7）", ["tests/v157_regression_test.py"], "V157 OK"),
    ("论文卡回到 v1.4.6 写法 E1–E10（v1.5.9）", ["tests/v159_regression_test.py"], "V159 OK"),
    ("评价页浏览器 U0–U9（v1.5.1，无 playwright 时跳过）", ["tests/ui_e2e_test.py"], None),
    ("采纳后结果回填 B1", ["tests/b1_outcome_test.py"], "B1 OK"),
    ("多来源 L0 B2", ["tests/b2_multi_source_test.py"], "B2 OK"),
    ("网页周报脱敏摘要 B4", ["tests/b4_weekly_web_test.py"], "B4 OK"),
    ("最小验证 dry-run B7", ["tests/b7_dryrun_test.py"], "B7 OK"),
    ("MCP stdio 服务器", ["tests/mcp_server_test.py"], "MCP OK"),
]


def g1(a) -> int:
    c = Checks()
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env = {k: v for k, v in env.items() if not k.startswith("SCIENCEPAL_")}  # 离线回归不能碰真实平台
    logs: list[str] = []
    for name, cmd, must in SUITES:
        if not (ROOT / cmd[0]).exists():
            c(name, False, f"缺少 {cmd[0]}")
            continue
        t0 = time.time()
        try:
            r = subprocess.run([PY, "-X", "utf8", str(ROOT / cmd[0]), *cmd[1:]], cwd=ROOT, env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=a.timeout)
            out, code = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired as exc:
            out, code = f"超时（>{a.timeout}s）\n{exc.stdout or ''}", -1
        dt = time.time() - t0
        fails = [l for l in out.splitlines() if l.startswith(("FAIL", "INVALID", "SELF-TEST FAIL", "Traceback"))]
        ok = code == 0 and not fails and (must is None or must in out)
        c(f"{name}（{dt:.0f}s）", ok, "; ".join(fails[:3]) or ("" if ok else out[-400:]))
        logs += [f"### {name}", "", f"命令：`python {' '.join(cmd)}` · 退出码 {code} · {dt:.1f}s", "", "```text",
                 *out.strip().splitlines()[-25:], "```", ""]
    for p in ROOT.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    lines = header("G1 离线回归") + ["## 结果", ""] + c.table() + ["", f"**结论：{'通过' if c.ok else '未通过'}**", "",
                                                                "## 每个套件的输出（末尾 25 行）", ""] + logs
    print("证据：", write_evidence("G1", lines))
    return 0 if c.ok else 1


# ============================================================================ G4
def _expected_status(stack: list[dict], actions: dict) -> str:
    st = "proposed"
    for v in stack:
        s = actions.get(v.get("action"), {}).get("status")
        if s:
            st = s
    return st


def audit_state(sd: Path, c: Checks, today: date | None = None, cfg: Path | None = None) -> None:
    """Read-only consistency audit of a state dir: every reader must see the same 'current verdict' per card."""
    V = load("insp-feedback/scripts/record_verdict.py", "rv_gate")
    RS = load("platform/review_server.py", "rs_gate")
    WR = load("insp-review/scripts/weekly_review.py", "wr_gate")
    vp = sd / "verdicts.jsonl"
    raw = vp.read_text(encoding="utf-8").splitlines() if vp.exists() else []
    bad_json = []
    rows = []
    for i, l in enumerate(raw, 1):
        if not l.strip():
            continue
        try:
            rows.append(json.loads(l))
        except json.JSONDecodeError:
            bad_json.append(i)
    c("verdicts.jsonl 每一行都是合法 JSON", not bad_json, f"坏行：{bad_json[:10]}")
    cards: dict[str, dict] = {}
    for f in sorted((sd / "cards").glob("*/cards.json")):
        for card in json.loads(f.read_text(encoding="utf-8")).get("cards", []):
            cards[card["id"]] = card
    need = {"ts", "card_id", "source_ref", "type", "action", "label", "weight", "weight_source", "explicit"}
    miss = [f"{i}:{sorted(need - set(r))}" for i, r in enumerate(rows, 1) if need - set(r)]
    c("每条评价字段齐全（ts/card_id/source_ref/type/action/label/weight/weight_source/explicit）", not miss, "; ".join(miss[:5]))
    unk = [r.get("action") for r in rows if r.get("action") not in V.ACTIONS]
    c("action 都是约定里的动作", not unk, str(unk[:5]))
    wrong_exp = [r.get("card_id") for r in rows if r.get("action") in V.ACTIONS and r.get("explicit") != V.ACTIONS[r["action"]]["explicit"]]
    c("explicit 与动作表一致", not wrong_exp, str(wrong_exp[:5]))
    orphan = sorted({r.get("card_id") for r in rows} - set(cards))
    c("每条评价对应的卡片都在 cards/ 里", not orphan, str(orphan[:5]))
    bad_ret = [r.get("card_id") for r in rows if r.get("action") == "retract" and not r.get("retracts")]
    c("每条撤销都写明撤销了哪条（retracts）", not bad_ret, str(bad_ret[:5]))
    ts_bad = [r.get("ts") for r in rows if not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d([+-]\d\d:\d\d|Z)$", str(r.get("ts", "")))]
    c("时间是带时区的 ISO 8601", not ts_bad, str(ts_bad[:3]))

    stacks = V.verdict_stacks(rows)
    current = {k: st[-1]["action"] for k, st in stacks.items()}
    status_bad = []
    for cid, card in cards.items():
        exp = _expected_status(stacks.get(cid, []), V.ACTIONS)
        if card.get("status") != exp:
            status_bad.append(f"{cid}: cards.json={card.get('status')} 期望={exp}")
    c("cards.json 里的 status = 按评价栈推出的状态", not status_bad, "; ".join(status_bad[:5]))

    neg = [json.loads(l) for l in (sd / "negative.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (sd / "negative.jsonl").exists() else []
    alive_ts = {(cid, v["ts"]) for cid, st in stacks.items() for v in st}
    stale_neg = [f"{n.get('from_card')}@{n.get('added_at')}" for n in neg if n.get("from_card") and (n["from_card"], n.get("added_at")) not in alive_ts]
    c("负面清单里没有“已撤销评价”留下的行", not stale_neg, "; ".join(stale_neg[:5]))
    missing_neg = []
    for cid, st in stacks.items():
        for v in st:
            if v["action"] in ("reject", "done") and not any(n.get("from_card") == cid and n.get("added_at") == v["ts"] and n.get("kind") == "id" for n in neg):
                missing_neg.append(f"{cid}@{v['ts']}")
    c("每条有效的否决 / 已做过都在负面清单里", not missing_neg, "; ".join(missing_neg[:5]))

    judged = V.judged_ids(sd)
    data = RS.load_cards(sd, "all")
    ui_latest = {x["id"]: x["latest_action"] for x in data["cards"] if x["latest_action"]}
    c("评价页读到的“当前评价”= 写入端的评价栈", ui_latest == current,
      str({k: (ui_latest.get(k), current.get(k)) for k in set(ui_latest) | set(current) if ui_latest.get(k) != current.get(k)})[:300])
    pending_ui = {x["id"] for x in RS.load_cards(sd, "pending")["cards"]}
    c("评价页“待评价”= 全部卡片 − 已评价", pending_ui == set(cards) - judged,
      f"多出 {sorted(pending_ui - (set(cards) - judged))[:5]} 缺少 {sorted((set(cards) - judged) - pending_ui)[:5]}")
    inbox = sd / "inbox" / "verdict_inbox.csv"
    if inbox.exists():
        ids = {r.get("card_id") for r in V.read_csv_any(inbox)}
        extra = ids & judged
        c("Excel 收件箱里没有已评价的卡片", not extra, f"{sorted(extra)[:5]}（重建：record_verdict.py --rebuild-inbox）")

    if (sd / "state.json").exists() and (sd / "taste" / "weights.json").exists():
        today = today or date.today()
        md, prop = WR.review(sd, today, None, cfg)
        n_type = sum(s["n"] for s in prop["feature_stats"].get("type", {}).values())
        n_typed = sum(1 for cid, st in stacks.items() if st[-1].get("type"))
        c("周报学习用的样本数 = 每张卡一票（按当前评价）", n_type == n_typed, f"周报 n={n_type}，评价栈 {n_typed}")
        pos = {"adopt", "start_now", "edited"}
        week0 = today - timedelta(days=6)
        want = {k for k, st in stacks.items() if st[-1]["action"] in pos and week0 <= date.fromisoformat(st[-1]["ts"][:10]) <= today}
        m = re.search(r"采纳率 \| \d+%（(\d+)/(\d+)）", md)
        if m:
            c("周报采纳率的分子 = 本周当前仍是正向评价的卡片数（撤销过的不算）", int(m.group(1)) == len(want),
              f"周报写 {m.group(1)}，按评价栈应为 {len(want)}")
        else:
            print("      （本周没有推送记录，周报不计算采纳率，跳过这一项）")


def g4_sim(c: Checks) -> None:
    V = load("insp-feedback/scripts/record_verdict.py", "rv_gate")
    tmp = Path(tempfile.mkdtemp(prefix="g4_"))
    try:
        sd = tmp / "inspiration"
        shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
        (sd / ".test_state").write_text("selfcheck g4\n", encoding="utf-8")
        day = date.today().isoformat()
        cards = []
        for i in range(1, 12):
            cards.append({"id": f"ins_{day.replace('-', '')}_{i:03d}", "type": ["knowledge", "hypothesis", "process"][i % 3],
                          "subfield": "memory", "title": f"卡片{i}", "one_line": f"一句话{i}", "minimal_check": "跑一次",
                          "source": {"kind": "arxiv", "ref": f"9999.{70000 + i}v1", "quote": "q"},
                          "rubric_scores": [{"rule_id": "R01", "score": 4, "reason": "x"}], "status": "proposed", "verdict": None})
        (sd / "cards" / day).mkdir(parents=True, exist_ok=True)
        (sd / "cards" / day / "cards.json").write_text(json.dumps({"date": day, "cards": cards}, ensure_ascii=False), encoding="utf-8")
        cid = {i: cards[i - 1]["id"] for i in range(1, 12)}
        base = datetime.now().astimezone().replace(microsecond=0) - timedelta(hours=1)
        k = [0]

        def ts():
            k[0] += 1
            return (base + timedelta(seconds=k[0])).isoformat()

        R = V.record
        R(sd, cid[1], "adopt", "好", ts=ts(), via="card_ui")
        R(sd, cid[1], "read_no_action", ts=ts())                         # 隐式：不改变当前评价
        R(sd, cid[2], "reject", "不感兴趣", block_topic="memory consolidation", ts=ts())
        R(sd, cid[3], "reject", ts=ts()); R(sd, cid[3], "retract", ts=ts())     # 否决后撤销 → 待评价、负面清单清掉
        R(sd, cid[4], "ask_detail", ts=ts()); R(sd, cid[4], "adopt", ts=ts())  # 先追问后采纳 → 采纳
        R(sd, cid[5], "ask_detail", ts=ts())                             # 只追问 → 仍待评价
        R(sd, cid[6], "edited", edited_card={"one_line": "改过的一句话"}, ts=ts()); R(sd, cid[6], "not_important", ts=ts())
        R(sd, cid[7], "done", ts=ts())
        R(sd, cid[8], "start_now", ts=ts()); R(sd, cid[8], "retract", ts=ts())
        R(sd, cid[9], "adopt", ts=ts()); R(sd, cid[9], "retract", ts=ts()); R(sd, cid[9], "reject", ts=ts())
        R(sd, cid[10], "reject"); R(sd, cid[10], "retract")             # 不传 ts：模拟同一秒内连点（真实界面）
        R(sd, cid[11], "done", ts=ts()); R(sd, cid[11], "retract", ts=ts()); R(sd, cid[11], "done", ts=ts())
        V.rebuild_inbox(sd)
        st_doc = json.loads((sd / "state.json").read_text(encoding="utf-8"))
        st_doc["pushed"] = [{"id": c_["source"]["ref"], "card_id": c_["id"], "title": c_["title"], "pushed_at": base.isoformat()} for c_ in cards]
        (sd / "state.json").write_text(json.dumps(st_doc, ensure_ascii=False), encoding="utf-8")
        expect = {1: "adopt", 2: "reject", 4: "adopt", 5: "ask_detail", 6: "not_important", 7: "done", 9: "reject", 11: "done"}
        stacks = V.verdict_stacks(V.read_verdicts(sd))
        got = {i: stacks[cid[i]][-1]["action"] for i in range(1, 12) if cid[i] in stacks}
        c("写入 11 张卡 × 各种动作组合后，读回的当前评价与预期完全一致", got == expect, f"读回 {got}")
        c("撤销后的卡片回到待评价（3、8、10），只追问的卡片也算待评价（5）",
          {cid[3], cid[5], cid[8], cid[10]} == set(cid.values()) - V.judged_ids(sd), str(sorted(set(cid.values()) - V.judged_ids(sd))))
        neg = (sd / "negative.jsonl").read_text(encoding="utf-8")
        c("负面清单：2、7、9、11 在；3、10 的撤销已清除；主题屏蔽在",
          all(f"9999.{70000 + i}v1" in neg for i in (2, 7, 9, 11)) and all(f"9999.{70000 + i}v1" not in neg for i in (3, 10))
          and "memory consolidation" in neg, neg[-400:])
        c("改写留下快照和 _edited", any((sd / "edits").glob(f"{cid[6]}_*.json")))
        audit_state(sd, c, cfg=None)
        # 写入端与评价页 HTTP 接口：review_server 自测走的是同一个 record()
        r = subprocess.run([PY, "-X", "utf8", str(ROOT / "platform/review_server.py"), "--self-test"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONUTF8="1"))
        c("评价页 HTTP 路径（/api/verdict /api/edit /api/undo）写入后读取一致", r.returncode == 0, (r.stdout + r.stderr)[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def g4(a) -> int:
    c = Checks()
    if a.state:
        sd = Path(a.state).resolve()
        if not (sd / "verdicts.jsonl").exists():
            print("ERROR: 没找到", sd / "verdicts.jsonl")
            return 2
        print(f"== 只读审计真实状态 {sd}（不写任何文件）")
        audit_state(sd, c, cfg=Path(a.config) if a.config else None)
        title = f"G4 评价读写一致（真实数据只读审计：{sd.name}）"
    else:
        print("== 临时目录模拟：11 张卡 × 采纳/否决/撤销/追问/改写/立刻动手 组合")
        g4_sim(c)
        title = "G4 评价读写一致（模拟）"
    lines = header(title) + ["## 结果", ""] + c.table() + ["", f"**结论：{'通过' if c.ok else '未通过'}**"]
    print("证据：", write_evidence("G4", lines))
    return 0 if c.ok else 1


# ============================================================================ G5
FORBIDDEN_FILES = [
    (r"(^|/)config(_exp)?\.json$", "你的本地配置（可能含个人路径 / webhook）"),
    (r"(^|/)platform_ids\.json$", "你账号下的智能体 / 技能 ID"),
    (r"(^|/)(session_token|run_token|review_token)$", "令牌文件"),
    (r"(^|/)\.env$", "环境变量文件"),
    (r"\.bak-\d", "配置 / 状态备份"),
    (r"(^|/)probe_report\.json$", "平台探测报告（含账号信息）"),
    (r"^(inspiration|inspiration_exp)/", "你的真实灵感状态（卡片、评价、画像）"),
    (r"^(runs|feedback|release_evidence|release)/", "运行记录 / 反馈 / 证据 / 旧发布包（含会话 ID）"),
    (r"(^|/)__pycache__/", "Python 缓存"),
    (r"^dist/", "构建产物（setup 会重新生成）"),
    (r"^library/", "你的本地资料库（代码 / 笔记 / 论文 / 数据 / 日志，v1.5 observe）"),
]
ALLOW_IN = {"tests/mock_receiver_test.py", "tests/mock_setup_test.py", "tests/mock_platform_test.py", "scripts/selfcheck.py",
            "platform/collect_feedback.py"}
PATTERNS = [
    ("API Key", r"sp_live_[A-Za-z0-9]{12,}"),
    ("JWT 令牌", r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}"),
    ("Bearer 令牌值", r"Bearer\s+[A-Za-z0-9._~+/=-]{24,}"),
    ("飞书 / 企业微信 webhook", r"(open\.feishu\.cn/open-apis/bot/v2/hook/|qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=)[A-Za-z0-9-]{8,}"),
    ("邮箱", r"[A-Za-z0-9._%+-]+@(?!example\.(com|org)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ("个人目录", r"([A-Za-z]:\\\\?Users\\\\?(?!Public|Default|<)[A-Za-z0-9._-]{2,}|/Users/(?!Shared)[a-z][A-Za-z0-9._-]+|/home/(?!user/|runner/)[a-z][a-z0-9._-]+/)"),
    ("会话 / 运行 UUID", r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"),
]
# v1.4.5: built-in "never ship" words (third-party attribution / personal handles), base64 so the words themselves
# never appear in any file of the package. Always active, on top of --deny / --deny-file.
BUILTIN_DENY_B64 = ['Y3Zwci1za2lsbA==', 'bW9tbw==', 'NDIwNjE2NjkyMzU=', '6Z2e5YW45Z6L5Zyo6K+7', '5bCP57qi5Lmm']
UUID_ALLOW = {"11111111-2222-4333-8444-555555555555"}
TEXT_EXT = {".py", ".md", ".json", ".jsonl", ".txt", ".html", ".ps1", ".sh", ".yaml", ".yml", ".csv", ".toml", ".cfg", ".ini", ""}


def iter_files(target: Path):
    if target.is_file() and target.suffix.lower() == ".zip":
        with zipfile.ZipFile(target) as z:
            names = z.namelist()
            top = names[0].split("/")[0] + "/" if names and all(n.startswith(names[0].split("/")[0] + "/") for n in names) else ""
            for n in names:
                if not n.endswith("/"):
                    yield n[len(top):], z.read(n)
    elif target.is_file():
        yield target.name, target.read_bytes()
    else:
        for p in sorted(target.rglob("*")):
            if p.is_file() and ".git" not in p.parts:
                yield p.relative_to(target).as_posix(), p.read_bytes()


def g5(a) -> int:
    c = Checks()
    targets = [Path(t) for t in (a.paths or [str(ROOT)])]
    deny: list[str] = []
    if a.deny_file:
        deny = [w.strip() for w in Path(a.deny_file).read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")]
    deny += [w for w in (a.deny or "").split(",") if w.strip()]
    builtin = [base64.b64decode(x).decode("utf-8") for x in BUILTIN_DENY_B64]
    if not deny:
        print("提示：没有提供个人敏感词（--deny-file / --deny）。只按通用规则扫描；建议把你的姓名、邮箱、机构、账号写进一个本地文件再扫一次。")
    findings: list[str] = []
    file_hits: list[str] = []
    local_only: list[str] = []
    n = 0
    for t in targets:
        is_zip = t.suffix.lower() == ".zip"
        for rel, data in iter_files(t):
            n += 1
            hit = next((why for pat, why in FORBIDDEN_FILES if re.search(pat, rel)), None)
            if hit and is_zip:
                file_hits.append(f"{t.name}:{rel}（{hit}）")
            elif hit and t.is_dir():
                local_only.append(rel)  # 工作目录里本来就该有（config.json、inspiration/…），打包时 build_release 会排除
                continue
            if Path(rel).suffix.lower() not in TEXT_EXT or len(data) > 5_000_000:
                continue
            text = data.decode("utf-8", errors="ignore")
            for label, pat in PATTERNS:
                for m in re.finditer(pat, text):
                    s = m.group(0)
                    if rel in ALLOW_IN and label in ("API Key", "会话 / 运行 UUID", "Bearer 令牌值"):
                        continue
                    if label == "会话 / 运行 UUID" and (s in UUID_ALLOW or rel.endswith("toast.ps1")):
                        continue
                    if label == "邮箱" and (s.endswith((".py", ".md", ".json")) or "noreply" in s):
                        continue
                    line = text.count("\n", 0, m.start()) + 1
                    findings.append(f"{t.name}:{rel}:{line} {label} `{s[:6]}…`")
            low = text.lower()
            for w in builtin:
                if w.lower() in low:
                    line = text.count("\n", 0, low.find(w.lower())) + 1
                    findings.append(f"{t.name}:{rel}:{line} 内置禁用词（第三方署名 / 个人账号）")
            for w in deny:
                if w.lower() in low:
                    line = text.lower().count("\n", 0, low.find(w.lower())) + 1
                    findings.append(f"{t.name}:{rel}:{line} 个人敏感词 `{w[:2]}…`")
    if any(t.suffix.lower() == ".zip" for t in targets):
        c(f"发布包里不含不该发出去的文件（扫描 {n} 个文件）", not file_hits, "; ".join(file_hits[:12]))
    if local_only:
        print(f"提示：工作目录里有 {len(local_only)} 个本地专用文件（如 {', '.join(sorted({x.split('/')[0] for x in local_only})[:6])}），"
              "没有扫描内容；它们不会进发布包（build_release.py 按白名单打包）。")
    c("不含密钥 / 令牌 / webhook / 邮箱 / 个人目录 / 会话 ID" + ("（含个人敏感词）" if deny else ""), not findings, "; ".join(findings[:15]))
    for f in findings[15:60]:
        print("      ", f)
    lines = header("G5 不泄露密钥与个人信息") + [f"- 扫描对象：{', '.join(str(t) for t in targets)}",
                                                f"- 个人敏感词：{len(deny)} 个（只记数量，不记内容）", "", "## 结果", ""] + c.table() + \
        ["", f"**结论：{'通过' if c.ok else '未通过'}**", "", "说明：命中内容只显示前 6 个字符，证据文件本身不会再次泄露。"]
    print("证据：", write_evidence("G5", lines))
    return 0 if c.ok else 1


# ============================================================================ G3
def g3(a) -> int:
    c = Checks()
    cfg_path = Path(a.config).resolve()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    base = cfg_path.parent
    sd = (base / cfg.get("state_dir", "./inspiration")).resolve()
    runs = [json.loads(l) for l in (sd / "runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (sd / "runs.jsonl").exists() else []
    real = [r for r in runs if r.get("status") in ("OK", "NO_CANDIDATES") and r.get("via") in ("receiver", None, "local", "run_local")
            and (not a.thread or a.thread in str(r.get("thread_id")))]
    if not real:
        print("ERROR: runs.jsonl 里没有状态为 OK / NO_CANDIDATES 的真实运行。先在网页里对灵感官说“主动探索灵感”，等评价页收到结果。")
        return 2
    r = real[-1]
    cards = r.get("cards") or []
    print(f"== 最近一次真实运行 {r.get('run_id')} · status={r.get('status')} · 卡片 {len(cards)} 张 · 会话 {str(r.get('thread_id'))[:8]}…")
    c("走完全程：运行状态是 OK 或 NO_CANDIDATES，且已写进本地 runs.jsonl", True, f"{r.get('run_id')} {r.get('status')}")
    if not cards:
        why = r.get("problem") or r.get("triage_counts") or r.get("dedup_dropped") or r.get("note")
        c("0 张卡时写清了原因（计数 / 去重 / 校验记录）", bool(why) or r.get("status") == "NO_CANDIDATES", str(why)[:200])
    # ---- platform evidence: loaded=true
    SP = load("platform/setup_platform.py", "sp_gate")
    from sciencepal_client import skill_loaded  # type: ignore  # noqa: E402  (setup_platform put platform/ on sys.path)
    sp = SP.session_only(cfg)
    run_ids = [a.agent_run_id] if a.agent_run_id else ([r["agent_run_id"]] if r.get("agent_run_id") else [])
    if not sp:
        print("ERROR: G3 要用平台字段证明技能已加载，需要会话令牌（~/.sciencepal/session_token）。")
        return 2
    if not run_ids and r.get("thread_id"):
        try:
            res = sp.thread_agent_runs(r["thread_id"])
            items = res.get("agent_runs", res.get("runs", res.get("items", res.get("data", [])))) if isinstance(res, dict) else res
            run_ids = [str(x.get("agent_run_id") or x.get("id")) for x in items or [] if isinstance(x, dict)][-3:]
        except Exception as exc:  # noqa: BLE001
            print("WARN 列不出该会话的运行：", str(exc)[:200])
    need = ["insp-nightly"] + ([cfg.get("skill_to_check", "insp-card")] if cards else [])
    ev_lines = []
    loaded = {s: "unknown" for s in need}
    for rid in run_ids:
        try:
            ev = sp.run_skills(rid)
        except Exception as exc:  # noqa: BLE001
            ev_lines.append(f"{rid}: 读取失败 {str(exc)[:120]}")
            continue
        for s in need:
            v = skill_loaded(ev, s)
            if v == "true" or loaded[s] == "unknown":
                loaded[s] = v
        ev_lines.append(f"{rid[:8]}…: " + ", ".join(f"{s}={skill_loaded(ev, s)}" for s in need))
    for s in need:
        c(f"平台字段 GET /api/agent-run/{{id}}/skills：{s} loaded=true", loaded[s] == "true",
          f"{loaded[s]}；查过的运行：{'; '.join(ev_lines) or '无（用 --agent-run-id 指定）'}")
    # ---- spot check
    tri_p = (base / cfg.get("work_dir", "runs") / r["run_id"] / "triage.json")
    tri = json.loads(tri_p.read_text(encoding="utf-8")) if tri_p.exists() else {}
    abstracts = {str(x.get("id", "")).split("v")[0]: (x.get("abstract") or "") for x in tri.get("shortlisted", [])}
    lib = {}
    for f in (sd / "cards").glob("*/cards.json"):
        for card in json.loads(f.read_text(encoding="utf-8")).get("cards", []):
            lib[card["id"]] = card
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()  # noqa: E731
    spot = []
    for cid in [x if isinstance(x, str) else x.get("id") for x in cards][: a.spot]:
        card = lib.get(cid)
        if not card:
            spot.append((cid, False, "卡片库里找不到"))
            continue
        ref = str(card["source"]["ref"]).split("v")[0]
        q = card["source"].get("quote", "")
        parts = [p for p in q.split("...") if p.strip()]
        ok_q = bool(abstracts.get(ref)) and all(norm(p) in norm(abstracts[ref]) for p in parts)
        detail = f"{ref} 引文{'逐字在摘要里' if ok_q else '未在本地 triage 摘要里逐字找到'}"
        ok_f = all(card.get(k) for k in ("minimal_check", "expected_observable", "falsifier"))
        if a.online:
            try:
                import urllib.request
                html = urllib.request.urlopen(urllib.request.Request(f"https://arxiv.org/abs/{ref}", headers={"User-Agent": "insp-release-gate"}), timeout=20).read().decode("utf-8", "ignore")
                m = re.search(r'<meta name="citation_title" content="([^"]+)"', html)
                detail += f"；arXiv 页面存在，标题：{m.group(1)[:60] if m else '?'}"
            except Exception as exc:  # noqa: BLE001
                detail += f"；在线核对失败 {str(exc)[:60]}（不算失败，请人工打开链接）"
        spot.append((cid, ok_q and ok_f, detail + ("" if ok_f else "；缺 minimal_check/expected_observable/falsifier")))
    for cid, ok, d in spot:
        c(f"抽查卡片 {cid}", ok, d)
    lines = header("G3 真实运行") + [f"- 运行：`{r.get('run_id')}` · status `{r.get('status')}` · 卡片 {len(cards)} 张",
                                    f"- 会话 ID 只保留前 8 位：`{str(r.get('thread_id'))[:8]}`", "", "## 自动检查", ""] + c.table() + [
        "", "## 人工抽查（必须由人填写）", "",
        "| 卡片 | 打开 arXiv 链接，标题对得上 | 引文是原文逐字片段 | minimal_check 两天内能做 | 结论 |", "|---|---|---|---|---|",
        *[f"| {cid} | ☐ | ☐ | ☐ | |" for cid, _, _ in spot],
        "", f"**自动检查结论：{'通过' if c.ok else '未通过'}**（G3 最终通过 = 自动检查通过 + 人工抽查打勾）"]
    print("证据：", write_evidence("G3", lines))
    return 0 if c.ok else 1


# ============================================================================ skills（补查收货员运行的平台字段，已知问题 K4）
def _ids_from(res) -> list[str]:
    items = res.get("agent_runs", res.get("runs", res.get("items", res.get("data", [])))) if isinstance(res, dict) else res
    return [str(x.get("agent_run_id") or x.get("id")) for x in items or [] if isinstance(x, dict) and (x.get("agent_run_id") or x.get("id"))]


def audit_receiver_runs(sp, rows: list[dict], skill_to_check: str, loaded_fn) -> list[dict]:
    """每一行 runs.jsonl（收货员收回来的）→ 查这个会话里的运行的 GET /api/agent-run/{id}/skills。
    一个会话里可能有多次运行（追问、重跑），任意一次 loaded=true 就算 true。"""
    out = []
    for r in rows:
        need = ["insp-nightly"] + ([skill_to_check] if r.get("cards") else [])
        rids = [r["agent_run_id"]] if r.get("agent_run_id") else []
        note = ""
        if not rids and r.get("thread_id"):
            try:
                rids = _ids_from(sp.thread_agent_runs(r["thread_id"]))
            except Exception as exc:  # noqa: BLE001
                note = f"列不出会话里的运行：{str(exc)[:80]}"
        res = {s: "unknown" for s in need}
        for rid in rids:
            try:
                ev = sp.run_skills(rid)
            except Exception as exc:  # noqa: BLE001
                note = f"读取 {rid[:8]} 失败：{str(exc)[:80]}"
                continue
            for s in need:
                v = loaded_fn(ev, s)
                if v == "true" or res[s] == "unknown":
                    res[s] = v
        if not rids and not note:
            note = "这一行没有 thread_id / agent_run_id"
        out.append({"run_id": r.get("run_id"), "thread_id": r.get("thread_id"), "agent_run_ids": rids, "skills": res,
                    "skill_loaded": res.get(skill_to_check, res["insp-nightly"]), "cards": r.get("cards", []),
                    "note": note, "via": "selfcheck skills",
                    "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    return out


def _skills_selftest() -> int:
    sys.path.insert(0, str(ROOT / "platform"))
    from sciencepal_client import skill_loaded  # type: ignore  # noqa: E402

    class FakeSP:
        def thread_agent_runs(self, tid):
            return {"agent_runs": [{"id": f"{tid}-a"}, {"id": f"{tid}-b"}]} if tid != "t-empty" else {"agent_runs": []}

        def run_skills(self, rid):
            on = rid in {"t-ok-b", "t-nightly-only-a"}
            card = rid == "t-ok-b"
            return {"skills": [{"name": "insp-nightly", "loaded": on, "decision": "selected" if on else "not_recorded"},
                               {"name": "insp-card", "loaded": card, "decision": "selected" if card else "not_recorded"}]}

    rows = [{"run_id": "r1", "thread_id": "t-ok", "via": "receiver", "cards": ["c1"]},
            {"run_id": "r2", "thread_id": "t-nightly-only", "via": "receiver", "cards": ["c2"]},
            {"run_id": "r3", "thread_id": "t-none", "via": "receiver", "cards": []},
            {"run_id": "r4", "thread_id": "t-empty", "via": "receiver", "cards": []}]
    got = {x["run_id"]: x for x in audit_receiver_runs(FakeSP(), rows, "insp-card", skill_loaded)}
    checks = [
        ("会话里第二次运行 loaded=true 也算 true", got["r1"]["skills"] == {"insp-nightly": "true", "insp-card": "true"}),
        ("只加载了 insp-nightly 时 insp-card=false", got["r2"]["skills"] == {"insp-nightly": "true", "insp-card": "false"}),
        ("没出卡的运行只查 insp-nightly", list(got["r3"]["skills"]) == ["insp-nightly"] and got["r3"]["skill_loaded"] == "false"),
        ("会话里列不出运行 → unknown 并写原因", got["r4"]["skill_loaded"] == "unknown" and got["r4"]["note"]),
    ]
    for n, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + n)
    if all(ok for _, ok in checks):
        print("SELF-TEST OK: skills")
        return 0
    print("SELF-TEST FAIL: skills")
    return 1


def skills(a) -> int:
    if a.self_test:
        return _skills_selftest()
    if not a.config:
        print("用法：python scripts/selfcheck.py skills --config config.json [--last 10] [--all]")
        return 2
    cfg_path = Path(a.config).resolve()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    sd = (cfg_path.parent / cfg.get("state_dir", "./inspiration")).resolve()
    rp = sd / "runs.jsonl"
    rows = [json.loads(l) for l in rp.read_text(encoding="utf-8").splitlines() if l.strip()] if rp.exists() else []
    rows = [r for r in rows if r.get("via") == "receiver" and (r.get("thread_id") or r.get("agent_run_id"))]
    ap_path = sd / "audit.jsonl"
    done = set()
    if not a.all and ap_path.exists():
        done = {json.loads(l).get("run_id") for l in ap_path.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l).get("via") == "selfcheck skills" and json.loads(l).get("skill_loaded") in ("true", "false")}
    todo = [r for r in rows if r.get("run_id") not in done][-a.last:]
    if not todo:
        prev = {}
        if ap_path.exists():
            for l in ap_path.read_text(encoding="utf-8").splitlines():
                if l.strip() and json.loads(l).get("via") == "selfcheck skills":
                    prev[json.loads(l).get("run_id")] = json.loads(l).get("skill_loaded")
        bad_prev = sorted(k for k, v in prev.items() if v == "false" and k in {r.get("run_id") for r in rows})
        print(f"没有需要补查的运行（{rp} 里收货员收回来的运行 {len(rows)} 次，已查过 {len(done)} 次；加 --all 可全部重查）。")
        if bad_prev:
            print(f"之前查过、loaded=false 的运行 {len(bad_prev)} 次：{', '.join(bad_prev[:10])}（详见 {ap_path}）")
        return 2 if not rows else (1 if bad_prev else 0)
    SP = load("platform/setup_platform.py", "sp_selfcheck")
    from sciencepal_client import skill_loaded  # type: ignore  # noqa: E402
    sp = SP.session_only(cfg)
    if not sp:
        print("ERROR: 查平台字段需要会话令牌（~/.sciencepal/session_token 或环境变量 SCIENCEPAL_SETUP_TOKEN）；API Key 会被拒绝。")
        return 2
    skill = cfg.get("skill_to_check", "insp-card")
    res = audit_receiver_runs(sp, todo, skill, skill_loaded)
    with ap_path.open("a", encoding="utf-8") as f:
        for x in res:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    c = Checks()
    for x in res:
        sk = " · ".join(f"{k}={v}" for k, v in x["skills"].items())
        print(f"{x['run_id']}  thread {str(x['thread_id'])[:8]}  {sk}  卡片 {len(x['cards'])}  {x['note']}")
        c(f"{x['run_id']}（会话 {str(x['thread_id'])[:8]}，{len(x['cards'])} 张卡）：{sk}",
          all(v == "true" for v in x["skills"].values()), x["note"])
    bad = sum(1 for x in res if "false" in x["skills"].values())
    unk = sum(1 for x in res if "false" not in x["skills"].values() and "unknown" in x["skills"].values())
    lines = header("平台字段补查（收货员运行）") + [
        f"- 查了 {len(res)} 次运行：全部 loaded=true {len(res) - bad - unk} 次 · 有 false {bad} 次 · 查不到 {unk} 次",
        "- loaded=false 的运行：那几张卡**不是**按技能流程生成的，评测时不要计入技能效果。", "", "## 结果", ""] + c.table()
    print(f"SKILLS: 查了 {len(res)} 次运行，loaded=false {bad} 次，查不到 {unk} 次 → {ap_path}")
    print("证据：", write_evidence("SKILLS", lines))
    return 1 if bad else (3 if unk else 0)


def find_stale(root: Path = ROOT) -> tuple[list[str], int]:
    """Files inside the package-managed folders that are NOT in MANIFEST.txt (left over by an in-place upgrade).
    Only the whitelist folders of build_release.py are scanned; user files (config.json, inspiration/, library/,
    release_evidence/ ...) are never touched."""
    man = root / "MANIFEST.txt"
    if not man.is_file():
        return [], -1
    listed = set()
    for ln in man.read_text(encoding="utf-8").splitlines():
        parts = ln.split()
        if len(parts) >= 3 and not ln.startswith("#"):
            listed.add(" ".join(parts[2:]).replace("\\", "/"))
    br = load("scripts/build_release.py", "_br_for_stale")
    out = []
    for item in br.INCLUDE:
        d = root / item
        if not d.is_dir():
            continue
        for f in sorted(x for x in d.rglob("*") if x.is_file()):
            rel = f.relative_to(root).as_posix()
            if rel not in listed and not any(re.search(pat, rel) for pat in br.EXCLUDE):
                out.append(rel)
    return out, len(listed)


def stale(a) -> int:
    files, n = find_stale(ROOT)
    if n < 0:
        print("没有 MANIFEST.txt（开发目录？）。只有从发布 zip 解压 / 覆盖的目录才能做这项检查。")
        return 2
    if not files:
        print(f"OK  没有清单外的旧文件（MANIFEST {n} 项）")
        return 0
    print(f"发现 {len(files)} 个清单外的旧文件（新版已不再包含它们）：")
    for r in files:
        print("   ", r)
    if not a.clean:
        print("加 --clean 把它们移到 .stale_backup/<时间>/（不直接删除，确认无用后可整个删掉该文件夹）")
        return 1
    dst = ROOT / ".stale_backup" / now_tag()
    for r in files:
        (dst / r).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(ROOT / r), str(dst / r))
    print(f"OK  已移走 {len(files)} 个 → {dst}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="gate", required=True)
    p1 = sub.add_parser("g1"); p1.add_argument("--timeout", type=int, default=900)
    p3 = sub.add_parser("g3"); p3.add_argument("--config", required=True); p3.add_argument("--thread")
    p3.add_argument("--agent-run-id"); p3.add_argument("--spot", type=int, default=2); p3.add_argument("--online", action="store_true")
    p4 = sub.add_parser("g4"); p4.add_argument("--state"); p4.add_argument("--config")
    pk = sub.add_parser("package")
    ps = sub.add_parser("skills"); ps.add_argument("--config"); ps.add_argument("--last", type=int, default=10)
    ps.add_argument("--all", action="store_true"); ps.add_argument("--self-test", action="store_true")
    p5 = sub.add_parser("g5"); p5.add_argument("paths", nargs="*"); p5.add_argument("--deny-file"); p5.add_argument("--deny")
    pst = sub.add_parser("stale"); pst.add_argument("--clean", action="store_true")
    pa = sub.add_parser("all"); pa.add_argument("--timeout", type=int, default=900); pa.add_argument("--deny-file"); pa.add_argument("--deny")
    a = ap.parse_args()
    if a.gate == "package":
        return package_check(a)
    if a.gate == "skills":
        return skills(a)
    if a.gate == "g1":
        return g1(a)
    if a.gate == "g3":
        return g3(a)
    if a.gate == "g4":
        return g4(a)
    if a.gate == "g5":
        return g5(a)
    if a.gate == "stale":
        return stale(a)
    rc = [g1(a), g4(argparse.Namespace(state=None, config=None)),
          g5(argparse.Namespace(paths=[], deny_file=a.deny_file, deny=a.deny))]
    print("\n== 汇总：G1 {} · G4 {} · G5 {}".format(*["PASS" if x == 0 else "FAIL" for x in rc]))
    return 0 if not any(rc) else 1


if __name__ == "__main__":
    raise SystemExit(main())
