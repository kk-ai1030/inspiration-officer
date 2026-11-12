"""Operation log + usage statistics: "what has the user been doing a lot lately?"

Sources (all local except the conversation list):
  oplog.jsonl          inspiration/library/oplog.jsonl — one line per operation. Written by
                       `observe.py log "…"` (manual), by every scan (file added / modified, from changes.jsonl),
                       and by the conversation fetch below.
  conversation titles  GET /api/dashboard/personal-conversations (session token; API keys get 403). Runs started by
                       the inspiration system itself are excluded.
  shell history        opt-in (observe.usage.shell_history = true): PowerShell PSReadLine / bash history → which
                       scripts you run how often. Only script names are kept.
  produced files       figures / data written in the last N days (library manifest).

Topics (plotting, literature, debugging, data processing, writing, fitting, derivation, simulation) are matched by a
small keyword lexicon; frequent words in conversation titles outside the lexicon are reported too. A topic seen at
least `min_count` times in `days` days becomes a "process" finding: "沉淀成技能 / 模板，或找现成的开源技能".
Plotting gets extra evidence: plotting scripts and their repeated boilerplate lines.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import FIG_EXT, Env, finding, now_iso, read_text

TOPICS = {
    "plotting": ("绘图", ["画图", "绘图", "作图", "出图", "配色", "图例", "坐标轴", "可视化", "图表", "plot", "figure", "matplotlib",
                        "seaborn", "plotly", "gnuplot", "origin", "colorbar", "subplot", "legend", "savefig"]),
    "literature": ("文献整理", ["文献", "论文", "综述", "引用", "参考文献", "paper", "review", "citation", "bibtex", "zotero", "arxiv"]),
    "debugging": ("代码报错排查", ["报错", "调试", "bug", "debug", "traceback", "异常", "崩溃", "segfault", "error"]),
    "data_processing": ("数据处理", ["数据处理", "清洗", "格式转换", "合并数据", "pandas", "csv", "excel", "dataframe", "预处理"]),
    "writing": ("论文写作", ["润色", "写作", "摘要", "翻译", "latex", "overleaf", "投稿", "rebuttal", "cover letter", "审稿"]),
    "fitting": ("拟合与统计", ["拟合", "回归", "误差棒", "统计", "最小二乘", "fit", "fitting", "curve_fit", "error bar"]),
    "derivation": ("公式推导", ["推导", "公式", "证明", "哈密顿", "微扰", "格林函数", "derivation", "hamiltonian", "sympy"]),
    "simulation": ("数值模拟", ["模拟", "仿真", "蒙特卡洛", "monte carlo", "dft", "vasp", "lammps", "quantum espresso",
                             "有限元", "求解器", "solver", "分子动力学"]),
}
TITLE_STOP = set("如何 怎么 什么 为什么 帮我 请问 问题 可以 这个 一个 一下 关于 是否 有没有 能否 需要 我们 你好 新会话 新对话 "
                 "untitled new chat how what why the and for with".split())
SELF_RE = re.compile(r"灵感|insp_run|insp-|INSP_|主动探索灵感|跑今天的|画像访谈|灵感官")
PLOT_IMPORT = re.compile(r"^\s*(import|from)\s+(matplotlib|seaborn|plotly|bokeh|altair|proplot)\b|^\s*using\s+(Plots|Makie|PyPlot)"
                         r"|^\s*set\s+terminal\b|\bplt\.(plot|savefig|subplots)\(", re.M)
SCRIPT_RE = re.compile(r"(?:^|[\s&;|])(?:python3?|py|julia|matlab(?:\s+-batch)?|Rscript|gnuplot|bash|sh)\s+(?:-\S+\s+)*"
                       r"[\"']?([\w./\\:-]+\.(?:py|jl|m|R|r|gp|plt|sh))", re.I)


def oplog_path(env: Env) -> Path:
    return env.out / "oplog.jsonl"


def add_entry(env: Env, text: str, tags: list[str] | None = None, source: str = "manual", action: str = "note",
              target: str | None = None, t: str | None = None) -> dict:
    env.out.mkdir(parents=True, exist_ok=True)
    e = {"t": t or now_iso(), "source": source, "action": action, "text": text.strip(), "tags": tags or []}
    if target:
        e["target"] = target
    with oplog_path(env).open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def read_oplog(env: Env, days: float | None = None) -> list[dict]:
    p = oplog_path(env)
    if not p.is_file():
        return []
    horizon = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() if days else None
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(ln)
            if horizon is None or datetime.fromisoformat(e["t"]).timestamp() >= horizon:
                out.append(e)
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
    return out


def log_changes(env: Env, changes: list[dict]) -> int:
    n = 0
    for c in changes:
        if c.get("cls") in ("other",) or c.get("change") == "deleted":
            continue
        add_entry(env, f"{c['change']} {c['ref']}", [c.get("cls") or "file"], source="fs", action=c["change"],
                  target=c["ref"], t=c.get("t"))
        n += 1
    return n


def _self_threads(env: Env) -> set[str]:
    ids = set()
    for name in ("inbox/triggered.jsonl", "runs.jsonl"):
        p = env.state_dir / name
        if p.is_file():
            for ln in p.read_text(encoding="utf-8").splitlines():
                try:
                    tid = json.loads(ln).get("thread_id")
                    if tid:
                        ids.add(str(tid))
                except json.JSONDecodeError:
                    continue
    return ids


TITLE_KEYS = ("name", "title", "thread_name", "thread_title", "display_name", "project_name", "subject", "summary",
              "first_message", "first_user_message", "preview", "last_message", "query", "prompt")


def _title_of(t: dict) -> str:
    for k in TITLE_KEYS:
        v = t.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):                      # e.g. {"last_message": {"content": "..."}}
            for kk in ("content", "text", "title", "name"):
                if isinstance(v.get(kk), str) and v[kk].strip():
                    return v[kk].strip()
    for k in ("project", "metadata", "thread"):      # nested objects
        v = t.get(k)
        if isinstance(v, dict):
            s = _title_of(v) if v is not t else ""
            if s:
                return s
    return ""


def fetch_conversations(env: Env, threads: list[dict] | None = None, client=None) -> dict:
    """threads=None → ask the platform (session token). Returns a status dict; snapshot in conversations.json.
    client: an already authenticated SciencePal client (the receiver passes its session client)."""
    status = {"ok": False, "fetched": 0, "kept": 0, "new": 0, "via": "file" if threads is not None else "platform"}
    if threads is None and client is not None:
        try:
            threads = client.dashboard_threads()
        except Exception as exc:  # noqa: BLE001 - 403 for API keys, offline, …
            status["error"] = f"{type(exc).__name__}: {str(exc)[:160]}（读会话列表需要会话令牌）"
            return status
    if threads is None:
        try:
            import sys
            here = Path(__file__).resolve().parents[1]
            if str(here) not in sys.path:
                sys.path.insert(0, str(here))
            from sciencepal_client import ApiError, SciencePal  # type: ignore
        except ImportError as exc:
            status["error"] = f"找不到 sciencepal_client：{exc}"
            return status
        full = env.full
        tok = os.environ.get(full.get("setup_token_env", "SCIENCEPAL_SETUP_TOKEN"), "")
        tf = Path(os.path.expanduser(full.get("setup_token_file", "~/.sciencepal/session_token")))
        if not tok and tf.is_file():
            tok = tf.read_text(encoding="utf-8").strip()
        if not tok:
            status["error"] = "没有会话令牌（~/.sciencepal/session_token）：跳过平台会话标题"
            return status
        try:
            sp = SciencePal(full.get("base_url", "https://sciencepal.ai"), tok, full.get("auth_header", "Authorization"),
                            full.get("auth_scheme", "Bearer"))
            threads = sp.dashboard_threads()
        except ApiError as exc:
            status["error"] = f"HTTP {exc.status}：读会话列表失败（需要会话令牌，API Key 会被拒绝）"
            return status
        except Exception as exc:  # noqa: BLE001 - offline / DNS
            status["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            return status
    mine = _self_threads(env)
    prev = {c["thread_id"]: c for c in (env.read_json("conversations.json", {}) or {}).get("threads", []) if c.get("thread_id")}
    keep = []
    why = {"no_id": 0, "no_title": 0, "inspiration_run": 0, "inspiration_title": 0}
    keys_seen: set[str] = set()
    for t in threads or []:
        if not isinstance(t, dict):
            continue
        keys_seen |= set(t.keys())
        tid = str(t.get("thread_id") or t.get("id") or "")
        title = _title_of(t)
        if not tid:
            why["no_id"] += 1
            continue
        if not title:
            why["no_title"] += 1
            continue
        if tid in mine:
            why["inspiration_run"] += 1
            continue
        if SELF_RE.search(title):
            why["inspiration_title"] += 1
            continue
        at = t.get("created_at") or t.get("last_message_at") or t.get("updated_at") or now_iso()
        keep.append({"thread_id": tid, "title": title[:200], "at": str(at)})
        if tid not in prev:
            add_entry(env, title[:200], ["conversation"], source="platform", action="conversation", target=tid, t=str(at))
            status["new"] += 1
    # v1.5.1: say WHY titles were dropped (feedback: "读了 31 个、保留 0 个" was undiagnosable)
    status.update(ok=True, fetched=len(threads or []), kept=len(keep), dropped={k: v for k, v in why.items() if v})
    if why["no_title"] and not keep:
        status["fields_seen"] = sorted(keys_seen)[:30]      # field NAMES only, never values
    if keep and len(keep) < int(env.cfg["usage"].get("min_count", 3)):
        status["note"] = f"保留的会话标题少于 min_count={env.cfg['usage'].get('min_count', 3)}，还不足以形成“高频主题”"
    env.write_json("conversations.json", {"fetched_at": now_iso(), "threads": keep})
    return status


def shell_scripts(env: Env) -> dict[str, int]:
    u = env.cfg["usage"]
    if not u.get("shell_history"):
        return {}
    cands = [u.get("shell_history_path")] if u.get("shell_history_path") else [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"),
        os.path.expanduser("~/.bash_history"), os.path.expanduser("~/.zsh_history")]
    counts: dict[str, int] = {}
    for c in cands:
        if not c or "%" in c or not Path(c).is_file():
            continue
        lines = (read_text(Path(c), 3_000_000) or "").splitlines()[-5000:]
        for ln in lines:
            for m in SCRIPT_RE.finditer(ln):
                name = re.split(r"[\\/]", m.group(1))[-1]
                counts[name] = counts.get(name, 0) + 1
    return counts


def _in_window(s: str, horizon: float) -> bool:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp() >= horizon
    except ValueError:
        return True


def _hits(text: str, kws: list[str]) -> bool:
    low = text.lower()
    return any((k in text) if not k.isascii() else re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low) for k in kws)


def _title_terms(title: str) -> set[str]:
    out = {w for w in re.findall(r"[A-Za-z][A-Za-z0-9+\-]{2,}", title.lower()) if w not in TITLE_STOP}
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", title):
        out |= {seg[i:i + 2] for i in range(len(seg) - 1)} - TITLE_STOP
    return out


def scan(env: Env, manifest: dict) -> tuple[list[dict], dict]:
    u = env.cfg["usage"]
    days, min_count = float(u["days"]), int(u["min_count"])
    horizon = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    month = datetime.now().strftime("%Y-%m")
    convs = [c for c in (env.read_json("conversations.json", {}) or {}).get("threads", []) if _in_window(c.get("at", ""), horizon)]
    ops = [e for e in read_oplog(env, days) if e.get("source") in ("manual", "shell")]
    scripts = shell_scripts(env)
    roots = {r["label"]: r["path"] for r in env.roots()}
    recent_files = [f for f in manifest.get("files", []) if f["mtime"] >= horizon]
    figs = [f for f in recent_files if f["ext"] in FIG_EXT or (f["cls"] == "figure")]
    plot_scripts: list[str] = []
    boiler: dict[str, set[str]] = {}
    for f in manifest.get("files", []):
        if f["cls"] != "code" or f["ext"] not in (".py", ".jl", ".m", ".gp", ".plt", ".r", ".ipynb") or env.over_budget():
            continue
        text = read_text(roots[f["root"]] / f["rel"], 1_000_000) or ""
        if not PLOT_IMPORT.search(text):
            continue
        plot_scripts.append(f["ref"])
        for ln in text.splitlines():
            s = re.sub(r"\s+", " ", ln.strip())
            if re.search(r"\b(plt|ax|fig|axes|mpl|rcParams|sns)\b|set (xlabel|ylabel|terminal|output)|savefig|colorbar", s) and 12 <= len(s) <= 140:
                boiler.setdefault(s, set()).add(f["ref"])
    rep_lines = sorted(((s, len(r)) for s, r in boiler.items() if len(r) >= 3), key=lambda kv: -kv[1])[:6]

    stats: dict = {"window_days": days, "conversations": len(convs), "oplog": len(ops), "figures": len(figs),
                   "plot_scripts": len(plot_scripts), "shell_scripts": dict(sorted(scripts.items(), key=lambda kv: -kv[1])[:15]),
                   "topics": {}}
    findings = []
    covered_titles: set[str] = set()
    for key, (label, kws) in TOPICS.items():
        ct = [c for c in convs if _hits(c["title"], kws)]
        ot = [e for e in ops if _hits(e.get("text", "") + " " + " ".join(e.get("tags", [])), kws)]
        files_n = len(figs) if key == "plotting" else 0
        run_n = sum(n for s, n in scripts.items() if key == "plotting" and re.search(r"(?i)plot|fig|draw|画", s))
        score = len(ct) + len(ot) + min(3, files_n // 5) + min(2, run_n // 5) + (min(2, len(plot_scripts) // 3) if key == "plotting" else 0)
        stats["topics"][key] = {"conversations": len(ct), "oplog": len(ot), "files": files_n, "runs": run_n, "score": score}
        if score < min_count:
            continue
        covered_titles |= {c["thread_id"] for c in ct}
        L = [f"使用统计 · 最近 {days:g} 天 · 主题「{label}」",
             f"平台会话 {len(ct)} 次 · 操作记录 {len(ot)} 条" + (f" · 新产出图片 {files_n} 个 · 绘图脚本 {len(plot_scripts)} 个" if key == "plotting" else "")
             + (f" · 命令行运行绘图脚本 {run_n} 次" if run_n else "")]
        if ct:
            L += ["", "会话标题："] + [f"  - {str(c['at'])[:10]} {c['title'][:80]}" for c in sorted(ct, key=lambda c: str(c["at"]), reverse=True)[:8]]
        if ot:
            L += ["", "操作记录："] + [f"  - {str(e['t'])[:10]} {e.get('text', '')[:80]}" for e in ot[-6:]]
        if key == "plotting" and plot_scripts:
            L += ["", "绘图脚本：" + ", ".join(Path(r).name for r in plot_scripts[:8]) + (" …" if len(plot_scripts) > 8 else "")]
            if rep_lines:
                L += ["在 3 个以上脚本里重复出现的绘图样板行："] + [f"  [{n} 个脚本] {s}" for s, n in rep_lines]
        if key == "plotting" and figs:
            L += ["最近产出的图：" + ", ".join(Path(f["rel"]).name for f in sorted(figs, key=lambda f: -f["mtime"])[:6])]
        allowed = ["process", "optimization", "knowledge"] if key == "plotting" else ["process", "knowledge"]
        findings.append(finding(
            f"usage.{key}", "log", "process", f"最近 {days:g} 天你有 {score} 次在做「{label}」：可以沉淀成技能 / 模板",
            "\n".join(L), 2.4 + min(2.0, (score - min_count) * 0.3) + (0.5 if rep_lines and key == "plotting" else 0),
            fp=f"{key}|{month}", allowed=allowed, ref=f"oplog://usage/{key}", subfield="usage",
            summary=("高频重复的工作适合沉淀：写成一个技能 / 模板 / 函数库，或找现成的开源技能、工具。"
                     "推荐开源项目时只写名称并注明“建议检索核实”，不要编造链接或星数。"),
            metrics=stats["topics"][key]))
    # frequent words in conversation titles outside the lexicon
    df: dict[str, set[str]] = {}
    for c in convs:
        if c["thread_id"] in covered_titles:
            continue
        for t in _title_terms(c["title"]):
            df.setdefault(t, set()).add(c["thread_id"])
    extra = sorted(((t, ids) for t, ids in df.items() if len(ids) >= min_count), key=lambda kv: (-len(kv[1]), -len(kv[0])))
    used: set[str] = set()
    for t, ids in extra:
        if ids <= used or len([x for x in findings if x["detector"] == "usage.term"]) >= 2:
            continue
        used |= ids
        titles = [c for c in convs if c["thread_id"] in ids]
        ev = (f"使用统计 · 最近 {days:g} 天 · 会话标题里反复出现“{t}”（{len(ids)} 次）\n"
              + "\n".join(f"  - {str(c['at'])[:10]} {c['title'][:80]}" for c in titles[:8]))
        findings.append(finding(
            "usage.term", "log", "process", f"最近常问“{t}”相关的问题（{len(ids)} 次）", ev, 2.3 + min(1.5, (len(ids) - min_count) * 0.3),
            fp=f"{t}|{month}", allowed=["process", "knowledge"], ref="oplog://usage/term", subfield="usage",
            summary="同类问题反复问：可以整理成一份针对你项目的说明文档，或固化成技能。", metrics={"count": len(ids)}))
    stats["generated_at"] = datetime.now().isoformat(timespec="seconds")
    env.write_json("usage.json", stats)
    return findings, {"conversations": len(convs), "plot_scripts": len(plot_scripts), "figures": len(figs),
                      "findings": len(findings)}
