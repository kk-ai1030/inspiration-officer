#!/usr/bin/env python3
"""演示前一键重置（v1.5.3）：关掉评价页 → 备份并清空本地用户数据 → 画像回到“需要首轮输入” → 重新打开评价页。

Windows 上直接双击包根目录的 demo_reset.bat（等价于下面第一条命令）。

    python platform/demo_reset.py --config config.json                 # 先列出要做什么，输入 Y 确认后执行
    python platform/demo_reset.py --config config.json --yes           # 不再询问
    python platform/demo_reset.py --config config.json --restore latest   # 演示完把最近一次备份换回来（demo_restore.bat）
    python platform/demo_reset.py --self-test

会清掉（整个状态目录先原样移到 demo_backups/<时间>/，随时可以换回来）：
  - 卡片、评价、改写、隐式信号、权重学习结果、负面清单、周报
  - 已看过 / 已推送的 arXiv 记录（seen_ids、pushed、quota_used）→ 之前推过的论文会重新被当成新的
  - 本地观察（library 发现）的历史：observed.jsonl、inspiration/library/*（扫描报告、文件清单、变化记录）
    → 你资料库里的东西会重新被“发现”一遍
  - 立刻动手的任务与回执、追问细节的记录
  - 用户画像（profile/ 下的 profile.json、AGENT.md、历史版本）→ 回到模板，网页里说“建立画像”会从头问 7 个问题
  - 平台记忆里的 [灵感画像] / [灵感配置] / [灵感状态] 条目（需要会话令牌；没有就只提醒）

不会动：
  - 你的原始资料（包根目录下的 library/code、notes、papers、data、logs，或 config.json → observe.roots 指向的目录）
  - config.json、platform_ids.json、平台上的智能体 / 技能 / 定时任务
  - 平台记忆里的 [灵感设置] 和打分准则种子
  - 状态里的“设置”：配额、预筛门槛、观察通道设置、arXiv 检索式和关键词（--reset-sources 才恢复模板默认）、taste/rubric.md
  - 收货员的“已检查会话”游标：旧会话里的卡片不会被重新收进来
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent

try:  # v1.5.6: taskkill must not pop a console window under pythonw
    from _spawn import run as _run_hidden
except ImportError:  # pragma: no cover - platform/ is normally sys.path[0] here
    sys.path.insert(0, str(HERE))
    from _spawn import run as _run_hidden

TEMPLATE = PKG / "insp-init" / "assets" / "灵感状态模板" / "inspiration"
BACKUP_DIR = "demo_backups"
SETTING_KEYS = ("quota", "quota_profile", "quota_test", "triage", "observed", "revival")
SOURCE_KEYS = ("sources", "keywords")
KEEP_FILES = ("inbox/receiver_cursor.json", "taste/rubric.md")   # old sessions stay "already checked"
MEMORY_PREFIXES = ("[灵感画像]", "[灵感配置]", "[灵感状态]")
MEMORY_TAGS = ("insp-profile", "insp-state")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def load_cfg(cfg_path: Path) -> tuple[dict, Path, Path]:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    base = cfg_path.resolve().parent
    sd = Path(cfg.get("state_dir", "./inspiration"))
    return cfg, base, (sd if sd.is_absolute() else base / sd).resolve()


# ----------------------------------------------------------------------------- review server
def stop_server(state_dir: Path, port: int, rs) -> str:
    info = state_dir / "inbox" / ".review_server.json"
    msg = "评价页没有在运行"
    try:
        pid = int(json.loads(info.read_text(encoding="utf-8"))["pid"])
        if os.name == "nt":
            _run_hidden(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
        msg = f"已关闭评价页（pid {pid}）"
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    for _ in range(25):   # wait until the port is free (Windows keeps files locked while the process lives)
        p = rs.ping(port, 0.3)
        if not p or Path(p.get("state_dir_abs") or "/nonexistent").resolve() != state_dir.resolve():
            break
        time.sleep(0.2)
    return msg


# ----------------------------------------------------------------------------- local data
def summarize(state_dir: Path) -> dict:
    def n_lines(p: Path) -> int:
        try:
            return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
        except OSError:
            return 0
    st = {}
    try:
        st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    cards = 0
    for f in (state_dir / "cards").glob("*/cards.json"):
        try:
            cards += len(json.loads(f.read_text(encoding="utf-8")).get("cards", []))
        except (OSError, json.JSONDecodeError):
            pass
    return {"cards": cards, "verdicts": n_lines(state_dir / "verdicts.jsonl"), "seen_ids": len(st.get("seen_ids") or []),
            "pushed": len(st.get("pushed") or []), "observed": n_lines(state_dir / "observed.jsonl"),
            "actions": len(list((state_dir / "actions").glob("*/job.json"))),
            "asks": len(list((state_dir / "asks").glob("*/ask.json"))),
            "profile": (state_dir / "profile" / "profile.json").is_file()}


def busy_workers(state_dir: Path) -> list[str]:
    out = []
    for p in list((state_dir / "actions").glob("*/job.json")) + list((state_dir / "asks").glob("*/ask.json")):
        try:
            s = json.loads(p.read_text(encoding="utf-8")).get("status")
        except (OSError, json.JSONDecodeError):
            continue
        if s in ("queued", "planning", "running", "approved"):
            out.append(p.parent.name)
    return out


def move_dir(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        try:
            shutil.move(str(src), str(dst))
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(1.0)   # Windows: a just-killed process may still hold a handle


def fresh_state(old_dir: Path, state_dir: Path, reset_sources: bool) -> dict:
    """New state dir from the template; carry over settings (not records) and the receiver cursor."""
    shutil.copytree(TEMPLATE, state_dir)
    tpl = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    try:
        old = json.loads((old_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        old = {}
    kept = []
    for k in SETTING_KEYS + (() if reset_sources else SOURCE_KEYS):
        if k in old:
            tpl[k] = json.loads(json.dumps(old[k]))
            kept.append(k)
    q = tpl.get("quota")
    if isinstance(q, dict) and isinstance(q.get("knowledge"), dict):
        q["knowledge"].setdefault("per_run", 5)
    if isinstance(tpl.get("revival"), dict):
        tpl["revival"]["counter"] = 0
    tpl.update(bootstrap=True, last_run_at=None, last_run_id=None, seen_ids=[], pushed=[], quota_used={})
    (state_dir / "state.json").write_text(json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8")
    for rel in KEEP_FILES:
        s = old_dir / rel
        if s.is_file():
            (state_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, state_dir / rel)
            kept.append(rel)
    return {"kept": kept}


# ----------------------------------------------------------------------------- platform memory
def purge_memory(cfg: dict, client=None, purge_auto: bool = False) -> str:
    pt = _load("platform/profile_tool.py", "profile_tool_demo")
    sp = client or pt.session_client(cfg)
    if sp is None:
        return ("没有会话令牌，平台记忆里的 [灵感画像] 没有删：网页里说“建立画像”会先复述旧画像（更新模式）。"
                "把会话令牌放进 ~/.sciencepal/session_token 后重跑本脚本即可")
    import re
    pat = re.compile(r"用户(擅长|熟悉|会|不会|不熟|背景|偏好|喜欢|要求|知识)|prefers?|familiar|expert|background", re.I)
    n = n_auto = n_dup = 0
    items = pt._mem_list(sp)   # v1.5.4 (S8): list_memory now pages through ALL memories (was: first 20 only)
    settings_seen: set[str] = set()
    for m in sorted(items, key=lambda m: str(m.get("updated_at") or m.get("created_at") or ""), reverse=True):
        c = str(m.get("content", ""))
        tags = m.get("tags") or []
        mine = c.startswith(MEMORY_PREFIXES) or any(t in tags for t in MEMORY_TAGS)
        auto = purge_auto and "auto_extraction" in json.dumps(m.get("source") or m.get("metadata") or {}, ensure_ascii=False) \
            and pat.search(c)
        dup = False
        if c.startswith("[灵感设置]"):   # kept on purpose, but only one copy per setting (older versions left duplicates)
            k = c.split("：", 1)[0].split(":", 1)[0]
            dup = k in settings_seen
            settings_seen.add(k)
        if mine or auto or dup:
            try:
                sp.delete_memory(m.get("id") or m.get("memory_id"))
            except Exception as exc:  # noqa: BLE001
                print(f"   删除失败：{c[:30]}…（{type(exc).__name__}）")
                continue
            n += 1
            n_auto += 1 if (auto and not mine) else 0
            n_dup += 1 if (dup and not mine) else 0
    left = [str(m.get("content", ""))[:40] for m in pt._mem_list(sp) if str(m.get("content", "")).startswith("[灵感")]
    msg = (f"平台记忆：共 {len(items)} 条，删除 {n} 条（[灵感画像] / [灵感配置] / [灵感状态]"
           + (f"，自动抽取的用户画像类 {n_auto} 条" if purge_auto else "") + (f"，重复的 [灵感设置] {n_dup} 条" if n_dup else "") + "）")
    bad = [x for x in left if x.startswith(MEMORY_PREFIXES)]
    msg += f"；剩余灵感类 {len(left)} 条" + ("：" + " | ".join(left[:6]) if left else "")
    if bad:
        msg += f"\n   警告：还有 {len(bad)} 条应删而未删，请在网页“记忆”页手动删除"
    return msg


# ----------------------------------------------------------------------------- main actions
def reset(cfg_path: Path, yes: bool = False, memory: bool = True, purge_auto: bool = False, reset_sources: bool = False,
          restart: bool = True, open_browser: bool = True, client=None, ask=input, publish: bool = True) -> int:
    cfg, base, state_dir = load_cfg(cfg_path)
    port = int((cfg.get("review") or {}).get("port", 8765))
    rs = _load("platform/review_server.py", "review_server_demo")
    before = summarize(state_dir) if state_dir.is_dir() else {}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = base / BACKUP_DIR / stamp
    print("== 演示重置 ==")
    print(f"状态目录：{state_dir.name}（备份到 {BACKUP_DIR}{os.sep}{stamp}{os.sep}{state_dir.name}）")
    if before:
        print(f"现在有：卡片 {before['cards']} 张 · 评价 {before['verdicts']} 条 · 看过的 arXiv {before['seen_ids']} 篇 · "
              f"推过 {before['pushed']} 篇 · 本地发现 {before['observed']} 条 · 立刻动手 {before['actions']} 个 · "
              f"追问 {before['asks']} 个 · 画像 {'有' if before['profile'] else '无'}")
    busy = busy_workers(state_dir) if state_dir.is_dir() else []
    if busy:
        print(f"注意：还有 {len(busy)} 个后台任务没结束（{', '.join(busy[:4])}）。重置后它们的结果会被丢弃。")
    print("原始资料（library/code 等）、config.json、平台上的智能体和技能都不会动。")
    if not yes:
        try:
            ok = ask("确认重置？输入 Y 回车继续，其他任意键取消：").strip().lower() in ("y", "yes", "是")
        except EOFError:
            ok = False
        if not ok:
            print("已取消，什么都没改。")
            return 1
    print("1/6 " + stop_server(state_dir, port, rs))
    if state_dir.is_dir():
        move_dir(state_dir, bdir / state_dir.name)
        print(f"2/6 已备份：{BACKUP_DIR}{os.sep}{stamp}")
    else:
        print("2/6 没有旧的状态目录，跳过备份")
    info = fresh_state(bdir / state_dir.name, state_dir, reset_sources)
    print("3/6 已建新的状态目录（保留设置：" + ("、".join(info["kept"]) or "无") + "）")
    mem = purge_memory(cfg, client, purge_auto) if memory else "按 --no-memory 跳过平台记忆"
    print("4/6 " + mem)
    report = {"at": datetime.now().isoformat(timespec="seconds"), "backup": str(Path(BACKUP_DIR) / stamp), "before": before,
              "kept": info["kept"], "memory": mem}
    (state_dir / "inbox").mkdir(exist_ok=True)
    (state_dir / "inbox" / "demo_reset.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("5/6 " + (publish_fresh(cfg_path, "demo_reset") if publish else "按参数跳过本地快照发布"))
    if restart:
        url = rs.ensure_server(cfg_path.resolve(), state_dir, port)
        if url:
            print("6/6 评价页已重新启动：" + url.split("?")[0])
            if open_browser:
                webbrowser.open(url)
        else:
            print("6/6 评价页没有启动成功：手动运行 python platform/review_server.py --config " + cfg_path.name + " --open")
    else:
        print("6/6 按 --no-restart 没有重启评价页")
    print("\n完成。演示建议：网页里新开一个对话，对灵感官说“建立画像”（会从头问 7 个问题）→ 再新开一个对话说“主动探索灵感”"
          "（或点评价页的按钮；两条入口都会用到第 5 步发布的本地观察，应出现 A 类优化卡）。")
    print(f"演示完想换回原来的数据：双击 demo_restore.bat，或 python platform/demo_reset.py --config {cfg_path.name} --restore latest")
    return 0


def publish_fresh(cfg_path: Path, reason: str) -> str:
    """v1.5.5 (P11): scan library/ right away and publish the fresh state into the platform skill, so the FIRST
    "主动探索灵感" typed in the web UI after a reset already has the local observations (A-class cards)."""
    try:
        rc = _load("platform/receiver.py", "receiver_demo")
        ctx = rc.Ctx(cfg_path)
        r = rc.publish_snapshot(ctx, scan=True, force=True, reason=reason) or {}
        ps = sys.modules.get("publish_state_rc")
        msg = ps.describe(r) if ps else str(r.get("status"))
        n = sum(1 for ln in (ctx.state_dir / "observed.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()) \
            if (ctx.state_dir / "observed.jsonl").is_file() else 0
        return f"已扫描本地 library：本地观察 {n} 条；{msg}"
    except Exception as exc:  # noqa: BLE001
        return f"本地扫描 / 快照发布失败（{type(exc).__name__}: {str(exc)[:160]}）。可手动运行 python platform/publish_state.py --config {cfg_path.name}"


def restore(cfg_path: Path, which: str = "latest", yes: bool = False, restart: bool = True, open_browser: bool = True,
            ask=input, publish: bool = True) -> int:
    cfg, base, state_dir = load_cfg(cfg_path)
    port = int((cfg.get("review") or {}).get("port", 8765))
    root = base / BACKUP_DIR
    cands = sorted(d for d in root.glob("*") if (d / state_dir.name).is_dir()) if root.is_dir() else []
    if not cands:
        print(f"没有找到备份（{BACKUP_DIR}{os.sep}<时间>{os.sep}{state_dir.name}）")
        return 1
    src = cands[-1] if which == "latest" else root / which
    if not (src / state_dir.name).is_dir():
        print(f"没有这个备份：{which}。可用：" + "、".join(d.name for d in cands))
        return 1
    print(f"把 {BACKUP_DIR}{os.sep}{src.name} 换回 {state_dir.name}（当前的演示数据会另存为 {BACKUP_DIR}{os.sep}<时间>-demo）")
    if not yes:
        try:
            if ask("确认？输入 Y 回车继续：").strip().lower() not in ("y", "yes", "是"):
                print("已取消。")
                return 1
        except EOFError:
            return 1
    rs = _load("platform/review_server.py", "review_server_demo")
    print(stop_server(state_dir, port, rs))
    if state_dir.is_dir():
        move_dir(state_dir, root / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-demo") / state_dir.name)
    move_dir(src / state_dir.name, state_dir)
    try:
        src.rmdir()
    except OSError:
        pass
    print("已换回。注意：平台记忆里的 [灵感画像] 不会自动回来——在网页里重新“建立画像”，或运行 "
          "python platform/profile_tool.py --config " + cfg_path.name + " --sync-memory")
    if publish:
        print(publish_fresh(cfg_path, "demo_restore"))
    if restart:
        url = rs.ensure_server(cfg_path.resolve(), state_dir, port)
        if url and open_browser:
            webbrowser.open(url)
    return 0


# ----------------------------------------------------------------------------- self-test
def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        sd = t / "inspiration"
        shutil.copytree(TEMPLATE, sd)
        st = json.loads((sd / "state.json").read_text(encoding="utf-8"))
        st.update(seen_ids=["2610.00001"], pushed=[{"id": "2610.00001", "at": "x"}], quota_used={"2026-10-30": {"knowledge": 4}},
                  sources=[{"kind": "arxiv", "query": "cat:quant-ph AND abs:Rydberg", "max_results": 60}], keywords=["Rydberg"],
                  quota_profile="test", quota_test={"per_run_max": 6})
        st["revival"]["counter"] = 7
        (sd / "state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        (sd / "cards/2026-10-30").mkdir(parents=True)
        (sd / "cards/2026-10-30/cards.json").write_text(json.dumps({"cards": [{"id": "ins_1"}]}), encoding="utf-8")
        (sd / "verdicts.jsonl").write_text('{"card_id":"ins_1","action":"adopt"}\n', encoding="utf-8")
        (sd / "observed.jsonl").write_text('{"id":"obs_1"}\n', encoding="utf-8")
        (sd / "library").mkdir()
        (sd / "library/manifest.json").write_text("{}", encoding="utf-8")
        (sd / "profile/profile.json").write_text('{"schema":"insp-profile/1.0"}', encoding="utf-8")
        (sd / "profile/AGENT.md").write_text("# me", encoding="utf-8")
        (sd / "actions/act_1").mkdir(parents=True)
        (sd / "actions/act_1/job.json").write_text('{"status":"succeeded"}', encoding="utf-8")
        (sd / "inbox").mkdir()
        (sd / "inbox/receiver_cursor.json").write_text('{"checked":{"thread-old":"2026-10-30"}}', encoding="utf-8")
        (sd / "taste/rubric.md").write_text((sd / "taste/rubric.md").read_text(encoding="utf-8") + "\n<!-- mine -->\n", encoding="utf-8")
        raw = t / "library" / "code"
        raw.mkdir(parents=True)
        (raw / "power_dep.py").write_text("print(1)\n", encoding="utf-8")
        (t / "config.json").write_text(json.dumps({"base_url": "https://sciencepal.ai", "state_dir": "./inspiration",
                                                    "review": {"port": 1}}), encoding="utf-8")

        class Mem:
            def __init__(self):
                self.items = [{"id": "1", "content": "[灵感画像] 身份与领域：x", "tags": ["insp-profile"]},
                              {"id": "2", "content": "[灵感状态] 已推送 arXiv ID：…", "tags": ["insp-state"]},
                              {"id": "3", "content": "[灵感设置] 论文读取模式", "tags": ["insp-settings"]},
                              {"id": "4", "content": "R07 优先能被一次验证证伪的问题", "tags": ["rubric"]},
                              {"id": "5", "content": "用户擅长 Python", "tags": [], "source": {"kind": "auto_extraction"}},
                              {"id": "6", "content": "[灵感设置] 论文读取模式", "tags": ["insp-settings"], "created_at": "2026-10-01"}]

            def list_memory(self):
                return {"memories": self.items}

            def delete_memory(self, i):
                self.items = [m for m in self.items if m["id"] != i]
        mem = Mem()
        # cancel at the prompt → nothing changes
        assert reset(t / "config.json", ask=lambda _: "n", restart=False, client=mem, publish=False) == 1 and summarize(sd)["cards"] == 1
        assert reset(t / "config.json", yes=True, restart=False, client=mem, publish=False) == 0
        s = summarize(sd)
        assert s == {"cards": 0, "verdicts": 0, "seen_ids": 0, "pushed": 0, "observed": 0, "actions": 0, "asks": 0, "profile": False}, s
        new = json.loads((sd / "state.json").read_text(encoding="utf-8"))
        assert new["quota_used"] == {} and new["revival"]["counter"] == 0 and new["bootstrap"] is True
        assert new["keywords"] == ["Rydberg"] and new["quota_profile"] == "test" and new["quota"]["knowledge"]["per_run"] == 5
        assert not (sd / "library").exists() and not (sd / "profile/AGENT.md").exists() and (sd / "profile/interests.md").is_file()
        assert "thread-old" in (sd / "inbox/receiver_cursor.json").read_text(encoding="utf-8")
        assert "<!-- mine -->" in (sd / "taste/rubric.md").read_text(encoding="utf-8")
        assert (raw / "power_dep.py").is_file(), "原始资料不能动"
        assert sorted(m["id"] for m in mem.items) in (["3", "4", "5"], ["4", "5", "6"]), mem.items   # one [灵感设置] kept, auto kept
        bk = list((t / BACKUP_DIR).glob("*/inspiration"))
        assert len(bk) == 1 and (bk[0] / "profile/profile.json").is_file() and (bk[0] / "cards/2026-10-30/cards.json").is_file()
        assert json.loads((sd / "inbox/demo_reset.json").read_text(encoding="utf-8"))["before"]["cards"] == 1
        # --purge-auto-memory also removes auto-extracted “用户擅长…”
        time.sleep(1.1)
        assert reset(t / "config.json", yes=True, restart=False, client=mem, purge_auto=True, reset_sources=True, publish=False) == 0
        assert len(mem.items) == 2 and "4" in [m["id"] for m in mem.items], mem.items
        assert json.loads((sd / "state.json").read_text(encoding="utf-8"))["keywords"] != ["Rydberg"]
        # no session token → reset still works, memory step only warns
        assert "没有会话令牌" in purge_memory({"base_url": "https://x", "setup_token_file": str(t / "none")})
        # restore the first backup
        first = sorted((t / BACKUP_DIR).glob("*"))[0].name
        assert restore(t / "config.json", first, yes=True, restart=False, publish=False) == 0
        assert summarize(sd)["cards"] == 1 and summarize(sd)["profile"] is True
        assert any(d.name.endswith("-demo") for d in (t / BACKUP_DIR).glob("*"))
    print("SELF-TEST OK: demo_reset")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=Path("config.json"))
    ap.add_argument("--yes", action="store_true", help="不再询问")
    ap.add_argument("--no-memory", action="store_true", help="不动平台记忆")
    ap.add_argument("--purge-auto-memory", action="store_true", help="另外删除平台自动抽取的“用户擅长 / 偏好…”记忆")
    ap.add_argument("--reset-sources", action="store_true", help="arXiv 检索式和关键词也恢复成模板默认")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--no-open", action="store_true", help="重启评价页但不打开浏览器")
    ap.add_argument("--restore", metavar="latest|<备份目录名>")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.config.is_file():
        print(f"找不到 {a.config}：在包根目录运行，或用 --config 指定")
        return 1
    if a.restore:
        return restore(a.config, a.restore, a.yes, not a.no_restart, not a.no_open)
    return reset(a.config, a.yes, not a.no_memory, a.purge_auto_memory, a.reset_sources, not a.no_restart, not a.no_open)


if __name__ == "__main__":
    raise SystemExit(main())
