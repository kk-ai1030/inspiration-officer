#!/usr/bin/env python3
"""v1.5 本地观察（observed 通道）：扫描你的资料库，把“发现”写进 inspiration/observed.jsonl。

只在你自己的电脑上运行，Python 3.9+ 标准库（读 PDF 标题时有 pypdf 更好，没有也能跑）。
收货员触发运行（评价页按钮 / 定时任务）前会自动跑一次 scan（config.json → observe.auto_scan_before_run），
所以平时不用手动跑；第一次配置、或想先看看会推什么时再手动跑。

    python platform\\observe.py --config config.json init                 # 建 library\\ 五个子文件夹；config.json 没有 observe 段时自动写入（先备份）
    python platform\\observe.py --config config.json scan                 # 全部扫描（代码 / 日志与数据 / 笔记 / 论文 / 使用统计）
    python platform\\observe.py --config config.json scan --only code,logs  # 只跑部分观察者：code logs notes papers usage
    python platform\\observe.py --config config.json scan --no-platform   # 不读平台会话标题（离线）
    python platform\\observe.py --config config.json list                 # 预览：下次运行哪些发现会入围（与沙箱 prepare 同规则）
    python platform\\observe.py --config config.json log "又手动调了一遍 colorbar" --tag 绘图   # 记一条操作
    python platform\\observe.py --config config.json papers --apply-keywords  # 把论文库高频主题并入 arXiv 关键词
    python platform\\observe.py --config config.json show obs_1a2b3c4d5e6f   # 看一条发现的完整证据

输出（都在 inspiration\\ 下，不进 Git、不进发布包）：
    observed.jsonl              observed 通道输入（随状态上传到沙箱，灵感官据此写 A/C/D/B 类卡）
    profile\\observed.md         画像 observed 段的自动部分（每条带路径与日期）
    library\\summary.md          资料库清单与本次变化（先看这个）
    library\\codescan.json / anomalies.json / papers.json / usage.json   各观察者的完整报告
    library\\oplog.jsonl         操作记录    library\\changes.jsonl   文件变化    library\\observed_history.jsonl  已消失的发现
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from observers import anomaly, codescan, library, notes, papers, usage  # noqa: E402
from observers.common import Env  # noqa: E402
from observers.observed import merge, read_observed  # noqa: E402

ALL = ["code", "logs", "notes", "papers", "usage"]
GROUP_OF = {"code": {"code"}, "logs": {"logs", "data"}, "notes": {"notes"}, "papers": {"papers"}, "usage": {"usage"}}


def run_scan(env: Env, only: list[str] | None = None, platform: bool = True, conversations_file: Path | None = None,
             quiet: bool = False, client=None) -> dict:
    """One full observation pass. Never raises for a single observer: its error is reported and the others continue."""
    say = (lambda *a: None) if quiet else print
    only = only or ALL
    prev_manifest = env.read_json("manifest.json", None)
    lib = library.scan(env)
    man = lib["manifest"]
    existing = [r for r in man["roots"] if r["exists"]]
    say(f"资料库：{len(existing)}/{len(man['roots'])} 个根目录存在，{len(man['files'])} 个文件"
        + ("（首次扫描，建立基线）" if lib["first_scan"] else f"，本次变化 {len(lib['changes'])} 项")
        + ("；⚠ 清单不完整（max_files / 超时）" if man.get("truncated") else ""))
    if not existing:
        say("  没有可扫描的目录：先运行 init，或在 config.json → observe.roots 里写你的资料路径。")
    usage.log_changes(env, lib["changes"])
    findings: list[dict] = []
    ran: set[str] = set()
    report: dict = {"files": len(man["files"]), "changes": len(lib["changes"]), "observers": {}}
    steps = [
        ("code", lambda: codescan.scan(env, man)),
        ("logs", lambda: anomaly.scan(env, man, prev_manifest)),
        ("notes", lambda: notes.scan(env, man)),
        ("papers", lambda: papers.scan(env, man)),
        ("usage", None),
    ]
    for name, fn in steps:
        if name not in only:
            continue
        if env.over_budget():
            say(f"  {name}：跳过（超过 time_budget_s）")
            report["observers"][name] = {"skipped": "time_budget"}
            continue
        try:
            if name == "usage":
                u = env.cfg["usage"]
                if conversations_file:
                    st = usage.fetch_conversations(env, json.loads(conversations_file.read_text(encoding="utf-8")))
                elif platform and u.get("conversations", True):
                    st = usage.fetch_conversations(env, client=client)
                else:
                    st = {"ok": False, "error": "已关闭（--no-platform 或 observe.usage.conversations=false）"}
                say(f"  平台会话标题：" + (f"读到 {st['fetched']} 个，保留 {st['kept']} 个（新 {st['new']}）" if st.get("ok") else st.get("error", "")))
                if st.get("dropped"):
                    names = {"no_id": "没有会话 ID", "no_title": "没有标题字段", "inspiration_run": "灵感系统自己的运行",
                             "inspiration_title": "标题含“灵感 / 主动探索…”"}
                    say("    排除：" + "；".join(f"{names.get(k, k)} {v}" for k, v in st["dropped"].items()))
                if st.get("fields_seen"):
                    say("    平台返回的字段名（没认出标题，请把这一行贴进反馈）：" + ", ".join(st["fields_seen"]))
                if st.get("note"):
                    say("    " + st["note"])
                fs, stats = usage.scan(env, man)
                stats["conversations_fetch"] = st
            else:
                fs, stats = fn()
        except Exception as exc:  # noqa: BLE001 - one broken observer must not stop the run
            say(f"  {name}：出错 {type(exc).__name__}: {str(exc)[:200]}（其它观察者继续）")
            report["observers"][name] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            continue
        ran |= GROUP_OF[name]
        findings += fs
        report["observers"][name] = stats
        say(f"  {name}：{json.dumps(stats, ensure_ascii=False)}")
    res = merge(env, findings, ran)
    report["observed"] = res
    say(f"observed.jsonl：{res['total']} 条（新 {res['new']}，已消失 {res['resolved']}）· 按类型 {res['by_type']}")
    env.write_json("last_scan.json", report)
    return report


def write_observe_section(cfg_path: Path) -> str:
    """v1.5.1: `init` copies the "observe" section of config.example.json into config.json (backup first).
    Never overwrites an existing observe section."""
    import shutil
    from datetime import datetime
    cfg_path = Path(cfg_path)
    if not cfg_path.is_file():
        return f"没有 {cfg_path}，跳过写入（先按 useguide 建好 config.json）"
    full = json.loads(cfg_path.read_text(encoding="utf-8"))
    if "observe" in full:
        return "config.json 已有 observe 段，保持不动"
    ex = json.loads((HERE / "config.example.json").read_text(encoding="utf-8"))
    bak = cfg_path.with_name(cfg_path.name + f".bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(cfg_path, bak)
    full["observe"] = ex["observe"]
    cfg_path.write_text(json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"已把 observe 段写进 {cfg_path.name}（原文件备份为 {bak.name}）。改路径 / 阈值直接编辑 config.json → observe。"


def preview(env: Env, show_all: bool = False) -> int:
    """Same selection rules as the sandbox (triage.py observed channel), run locally on the current state."""
    sys.path.insert(0, str(HERE.parent / "insp-triage" / "scripts"))
    import triage  # type: ignore

    st = json.loads((env.state_dir / "state.json").read_text(encoding="utf-8"))
    neg_p = env.state_dir / "negative.jsonl"
    neg = [json.loads(x) for x in neg_p.read_text(encoding="utf-8").splitlines() if x.strip()] if neg_p.is_file() else []
    w_sub = (triage.load_weights(env.state_dir) or {}).get("subfield", {}) or {}
    keep, counts, dropped = triage.observed_shortlist(env.state_dir, st, neg, w_sub)
    rows = {o.get("id"): o for o in read_observed(env.state_dir / "observed.jsonl")}
    print(f"observed.jsonl {counts['observed']} 条 → 入围 {len(keep)} 条（规则：state.json → observed，与沙箱 prepare 相同）")
    for o in keep:
        print(f"  ✔ [{o['candidate_type']}] 严重度 {o['severity']} · {o['title']}  ({o['id']} · {o.get('evidence_ref')})")
    if show_all:
        for d in dropped:
            o = rows.get(d.get("id"), {})
            print(f"  ✘ [{o.get('candidate_type')}] {o.get('title')}  ← {d['reason']}")
    elif dropped:
        print(f"  另有 {len(dropped)} 条未入围（加 --all 看原因）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=Path("config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    ini = sub.add_parser("init")
    ini.add_argument("--no-write-config", action="store_true", help="不把 observe 段写进 config.json（默认会写，原文件先备份）")
    sc = sub.add_parser("scan")
    sc.add_argument("--only", help="逗号分隔：" + ",".join(ALL))
    sc.add_argument("--no-platform", action="store_true")
    sc.add_argument("--conversations-file", type=Path, help="测试用：用本地 JSON 代替平台会话列表")
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    lg = sub.add_parser("log")
    lg.add_argument("text")
    lg.add_argument("--tag", action="append", default=[])
    pp = sub.add_parser("papers")
    pp.add_argument("--apply-keywords", action="store_true")
    sh = sub.add_parser("show")
    sh.add_argument("id")
    a = ap.parse_args()
    if not a.config.is_file():
        print(f"找不到配置文件 {a.config}：在包根目录运行，或用 --config 指定。")
        return 2
    env = Env(a.config)
    if a.cmd == "init":
        made = []
        for r in env.roots():
            if not r["exists"] and str(r["path"]).startswith(str(env.base)):
                r["path"].mkdir(parents=True, exist_ok=True)
                made.append(r["path"])
        (env.base / "library" / "README.txt").parent.mkdir(exist_ok=True)
        (env.base / "library" / "README.txt").write_text(
            "灵感机制本地资料库（不进 Git、不进发布包）\n"
            "code\\    物理 / 计算代码（大仓库不用复制：在 config.json → observe.roots 里写原路径）\n"
            "notes\\   科研零散笔记（.md .txt .org .tex）\n"
            "papers\\  收集的论文（.pdf .bib .ris 与阅读笔记）\n"
            "data\\    数据文件（.csv .tsv .dat；只检查最近 30 天改过的、5MB 以内的）\n"
            "logs\\    运行日志（.log .out .err .txt）\n"
            "运行：python platform\\observe.py --config config.json scan\n", encoding="utf-8")
        for r in env.roots():
            print(f"  {'新建' if r['path'] in made else ('存在' if r['path'].is_dir() else '缺失')} {r['label']:<8} {r['kind']:<7} {r['path']}")
        if "observe" not in env.full:
            if a.no_write_config:
                print("\n提示：config.json 里还没有 observe 段（--no-write-config：不写入），正在用默认值。想改路径 / 阈值，"
                      "把 platform/config.example.json 里的 \"observe\" 整段复制到 config.json。")
            else:
                print("\n" + write_observe_section(Path(a.config)))
        print("\n把资料放进上面的文件夹后运行：python platform\\observe.py --config config.json scan")
        return 0
    if a.cmd == "scan":
        if not env.cfg.get("enabled", True):
            print("observe.enabled = false：本地观察已关闭。")
            return 0
        only = [x.strip() for x in a.only.split(",")] if a.only else None
        bad = [x for x in only or [] if x not in ALL]
        if bad:
            print(f"--only 只能取 {ALL}，不认识：{bad}")
            return 2
        run_scan(env, only, platform=not a.no_platform, conversations_file=a.conversations_file)
        print(f"\n看清单：{env.out / 'summary.md'}\n预览入围：python platform\\observe.py --config {a.config} list")
        return 0
    if a.cmd == "list":
        return preview(env, a.all)
    if a.cmd == "log":
        e = usage.add_entry(env, a.text, a.tag)
        print(f"已记录：{e['t']} {e['text']} {e['tags']}")
        return 0
    if a.cmd == "papers":
        if a.apply_keywords:
            if not (env.out / "papers.json").is_file():
                papers.scan(env, library.scan(env)["manifest"])
            added = papers.apply_keywords(env)
            print("已并入 state.json → keywords：" + (", ".join(added) if added else "（没有新关键词）"))
        else:
            fs, stats = papers.scan(env, library.scan(env)["manifest"])
            print(json.dumps(stats, ensure_ascii=False, indent=1))
            rep = env.read_json("papers.json", {})
            print("建议关键词：", rep.get("suggested_keywords"))
            print("建议检索式：", rep.get("suggested_query"))
        return 0
    if a.cmd == "show":
        for o in read_observed(env.state_dir / "observed.jsonl"):
            if o.get("id") == a.id:
                print(json.dumps({k: v for k, v in o.items() if k != "evidence"}, ensure_ascii=False, indent=1))
                print("\n--- evidence ---\n" + o.get("evidence", ""))
                return 0
        print(f"observed.jsonl 里没有 {a.id}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
