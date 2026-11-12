#!/usr/bin/env python3
"""一次性安装（v1.4）：打包技能 → 导入 SciencePal → 建/更新“灵感官” → 显式绑定技能 → 检查账号一致 → 可选清理旧定时任务。
写 platform_ids.json。

    python platform/setup_platform.py --config config.json --build-only          # 只打 dist/*.zip
    python platform/setup_platform.py --config config.json --dry-run             # 只打包并列计划，不调接口
    python platform/setup_platform.py --config config.json --replace             # 正式安装（覆盖同名技能）
    python platform/setup_platform.py --config config.json --cleanup-schedules   # 列出平台上的旧灵感定时任务
    python platform/setup_platform.py --config config.json --cleanup-schedules --yes   # 删除它们
    python platform/setup_platform.py --config config.json --check               # 只做安装后检查

令牌：导入技能、建智能体、写记忆需要“会话令牌”（SCIENCEPAL_SETUP_TOKEN 或 ~/.sciencepal/session_token）；
API Key（sp_live_…，~/.sciencepal/run_token）只用于运行和收货。没有会话令牌时退回用 API Key 试一次。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
from sciencepal_client import ApiError, SciencePal  # noqa: E402
import skill_refs  # noqa: E402

SKILLS = ["insp-nightly", "insp-profile", "insp-card", "insp-init", "insp-triage", "insp-feedback", "insp-review"]
NEEDS_SCRIPTS = {"insp-nightly", "insp-profile"}  # 这两个技能的脚本必须在沙箱里能跑；文本方式创建等于不可用
LEGACY_TASK = re.compile(r"insp|inspiration|灵感", re.I)


def build_zip(skill: str, dist: Path) -> Path:
    src = PKG / skill
    dist.mkdir(parents=True, exist_ok=True)
    out = dist / f"{skill}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                z.write(f, f.relative_to(src).as_posix())  # SKILL.md 在 zip 根目录；中文文件名自动带 UTF-8 标志
    return out


def frontmatter(skill: str) -> tuple[str, str, str]:
    text = (PKG / skill / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, flags=re.S)
    fm = dict(re.findall(r"^(\w+):\s*(.*)$", m.group(1), flags=re.M))
    return fm["name"], fm["description"], m.group(2)


def inline_body(skill: str) -> str:
    """文本方式创建的兜底：SKILL 正文 + 内联 references（scripts/ 不可用）。"""
    _, _, body = frontmatter(skill)
    parts = [body, "\n\n---\n\n> 注意：本技能以纯文本方式创建，scripts/ 目录不可用。\n"]
    for ref in sorted((PKG / skill / "references").glob("*.md")):
        parts.append(f"\n\n---\n\n# 附：{ref.name}\n\n" + ref.read_text(encoding="utf-8"))
    return "".join(parts)


def pick_id(resp, *keys) -> str | None:
    if isinstance(resp, dict):
        for k in keys:
            if resp.get(k):
                return str(resp[k])
        for v in resp.values():
            r = pick_id(v, *keys)
            if r:
                return r
    return None


def _token(cfg: dict, env_key: str, env_default: str, file_key: str, file_default: str) -> str:
    tok = os.environ.get(cfg.get(env_key, env_default), "")
    p = Path(os.path.expanduser(cfg.get(file_key, file_default)))
    if not tok and p.is_file():
        tok = p.read_text(encoding="utf-8").strip()
    return tok


def clients(cfg: dict) -> tuple[SciencePal | None, SciencePal | None]:
    def mk(t):
        return SciencePal(cfg["base_url"], t, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer")) if t else None
    setup = _token(cfg, "setup_token_env", "SCIENCEPAL_SETUP_TOKEN", "setup_token_file", "~/.sciencepal/session_token")
    run = _token(cfg, "token_env", "SCIENCEPAL_RUN_TOKEN", "token_file", "~/.sciencepal/run_token") or os.environ.get("SCIENCEPAL_TOKEN", "")
    return mk(setup or run), mk(run)


def session_only(cfg: dict) -> SciencePal | None:
    """Scheduled tasks / memory / conversation lists are NOT on the API-key allow-list (feedback4 BUG-2)."""
    t = _token(cfg, "setup_token_env", "SCIENCEPAL_SETUP_TOKEN", "setup_token_file", "~/.sciencepal/session_token")
    return SciencePal(cfg["base_url"], t, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer")) if t else None


SESSION_HINT = "需要会话令牌（API Key 只能调 agent/run/sandbox 接口）：把会话令牌放进 ~/.sciencepal/session_token"


def _agent_ids(sp: SciencePal) -> set[str]:
    r = sp.list_agents()
    items = r.get("agents", r.get("items", r.get("data", []))) if isinstance(r, dict) else r
    return {str(a.get("agent_id") or a.get("id")) for a in items or [] if isinstance(a, dict)}


def legacy_tasks(sp: SciencePal) -> list[dict]:
    out = []
    for t in sp.list_scheduled_tasks():
        text = " ".join(str(t.get(k) or "") for k in ("name", "prompt", "description"))
        if LEGACY_TASK.search(text):
            out.append(t)
    return out


def cleanup_schedules(sp: SciencePal | None, yes: bool) -> int:
    if sp is None:
        print("ERROR: 列定时任务" + SESSION_HINT)
        return 1
    try:
        tasks = legacy_tasks(sp)
    except ApiError as exc:
        hint = "：" + SESSION_HINT if exc.status in (401, 403) else ""
        print(f"ERROR: 列定时任务失败（HTTP {exc.status}）{hint}：{exc.body[:200]}")
        return 1
    tasks = [t for t in tasks if str(t.get("status", "")).lower() not in ("deleted", "removed")]
    if not tasks:
        print("OK: 平台上没有灵感相关的定时任务")
        return 0
    for t in tasks:
        tid = t.get("id") or t.get("task_id")
        print(f"{'删除' if yes else '发现'}  {tid}  {t.get('name')}  {t.get('cron_expression') or t.get('schedule_type') or ''}")
        if yes:
            try:
                sp.delete_scheduled_task(str(tid))
            except ApiError as exc:
                print(f"  WARN: 删除失败 HTTP {exc.status}")
    if not yes:
        print(f"共 {len(tasks)} 个。确认无误后加 --yes 删除（v1.4 不再使用任何定时任务）。")
    return 0


def post_check(cfg: dict, ids: dict, sp_setup: SciencePal | None, sp_run: SciencePal | None,
               sp_session: SciencePal | None = None) -> int:
    """安装后检查：智能体存在、技能已绑定、API Key 与灵感官同账号（BUG-3）、没有残留定时任务。"""
    problems = 0
    aid = ids.get("agent_id")
    print("== 安装后检查 ==")
    if not aid:
        print("FAIL 没有 agent_id")
        return 1
    if sp_setup:
        try:
            bound = json.dumps(sp_setup.get_agent_skills(aid), ensure_ascii=False)
            miss = [s for s, v in ids.get("skills", {}).items() if v["skill_id"] not in bound]
            print("PASS 技能全部绑定" if not miss else f"FAIL 未绑定：{miss}")
            problems += bool(miss)
        except ApiError as exc:
            print(f"WARN 读绑定失败 HTTP {exc.status}")
    stale = [s for s, v in ids.get("skills", {}).items() if v.get("stale")]
    if stale:
        print(f"FAIL {stale} 上次安装没有更新成功，平台上仍是旧版本。修好后加 --replace 重装")
        problems += 1
    text_mode = [s for s, v in ids.get("skills", {}).items() if v.get("mode") == "text" and s in NEEDS_SCRIPTS]
    if text_mode:
        print(f"FAIL {text_mode} 是文本方式创建的，沙箱里没有脚本：网页一句话跑灵感/画像访谈不可用。请解决 zip 导入后加 --replace 重装")
        problems += 1
    if sp_run:
        try:
            vis = _agent_ids(sp_run)
            ok = not vis or aid in vis
            print("PASS API Key 能看到灵感官（同一账号）" if ok else
                  "FAIL API Key 看不到灵感官：Key 和灵感官不在同一账号。请在灵感官所在账号生成 Key 写进 ~/.sciencepal/run_token")
            problems += not ok
        except ApiError as exc:
            print(f"FAIL API Key 无效（GET /api/agents → HTTP {exc.status}）")
            problems += 1
    else:
        print("WARN 没有 API Key（~/.sciencepal/run_token）：收货员和评价页按钮都用不了")
    if sp_session is None:
        print("WARN 没检查残留定时任务：" + SESSION_HINT + "（收货员也会因此只能收按钮触发的运行、不能写记忆）")
    else:
        try:
            left = [t for t in legacy_tasks(sp_session) if str(t.get("status", "")).lower() not in ("deleted", "removed")]
            print("PASS 没有残留的灵感定时任务" if not left else f"WARN 还有 {len(left)} 个灵感定时任务：运行 --cleanup-schedules --yes")
        except ApiError as exc:
            print(f"WARN 无法列出定时任务（HTTP {exc.status}，跳过）")
        try:  # the receiver reads conversation files with the session token (feedback4 BUG-3)
            sp_session.dashboard_threads()
            print("PASS 会话令牌可读会话列表（收货员能自动发现网页里发起的会话）")
        except ApiError as exc:
            print(f"WARN 会话令牌读会话列表失败（HTTP {exc.status}）：令牌可能过期，网页会话需用 --thread 收货")
    print("安装检查通过" if not problems else f"安装检查有 {problems} 项失败")
    return 1 if problems else 0


def api_guidance(exc: ApiError) -> str:
    """Pull detail.code / detail.guidance out of a platform error body (e.g. 400 missing_file)."""
    try:
        d = json.loads(exc.body).get("detail")
    except (ValueError, AttributeError):
        return exc.body[:200]
    if isinstance(d, dict):
        return " · ".join(str(d[k]) for k in ("code", "guidance", "message") if d.get(k)) or exc.body[:200]
    return str(d)[:200]


def import_one(sp: SciencePal, s: str, z: Path, replace: bool, old: dict | None) -> tuple[dict | None, str]:
    """Import one skill. Never raises: returns (ids entry or None, message). v1.4.4 (feedback6 H1):
    zip 400 → text fallback; text fallback 409 (same name exists) → keep the old platform version and say so."""
    name, desc, _ = frontmatter(s)
    try:
        resp = sp.import_skill_zip(z, name=name, replace=replace)
        sid = pick_id(resp, "skill_id", "id")
        return ({"skill_id": sid, "mode": "zip"}, "zip") if sid else (None, f"无法从响应中取得 skill_id：{str(resp)[:300]}")
    except ApiError as exc:
        why = api_guidance(exc)
        print(f"WARN: zip 导入 {s} 失败（HTTP {exc.status}）：{why}")
        if "missing_file" in exc.body:
            print("      原因：技能说明里引用的文件不在这个技能包里。运行 python platform/skill_refs.py 看是哪一行")
        if exc.status == 409 and not replace:
            print("      提示：同名技能已存在，升级时请加 --replace")
    try:
        resp = sp.create_skill_item(name, desc, inline_body(s), display_name=name)
        sid = pick_id(resp, "skill_id", "id")
        return ({"skill_id": sid, "mode": "text"}, "text（改用文本方式创建）") if sid else (None, "文本方式创建没有返回 skill_id")
    except ApiError as exc:
        if exc.status == 409 and old and old.get("skill_id"):
            return ({**old, "stale": True}, f"未更新：同名技能已存在，文本方式也无法覆盖，平台上仍是旧版本 {old['skill_id']}")
        if exc.status == 409:
            return None, "未更新：同名技能已存在，但本地 platform_ids.json 里没有它的 id。修好 zip 问题后加 --replace 重装"
        return None, f"文本方式创建也失败（HTTP {exc.status}）：{api_guidance(exc)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace", action="store_true", help="导入时覆盖同名技能")
    ap.add_argument("--seed-memory", action="store_true", help="写入 memory_seeds.json 里的软偏好（可选）")
    ap.add_argument("--cleanup-schedules", action="store_true", help="列出（加 --yes 则删除）平台上的灵感定时任务")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--check", action="store_true", help="只做安装后检查")
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    dist = PKG / "dist"
    zips = {s: build_zip(s, dist) for s in SKILLS}
    for s, z in zips.items():
        print(f"built {z.relative_to(PKG).as_posix()} ({z.stat().st_size} bytes)")
    ref_bad = [b for s in SKILLS for b in skill_refs.problems(PKG / s)]
    for b in ref_bad:
        print("FAIL 文件引用检查：", b)
    if ref_bad:
        print("ERROR: 平台导入会以 missing_file 拒收这些技能包。先修正上面的引用（或运行 python platform/skill_refs.py）再安装")
        return 1
    print("文件引用检查通过（各技能引用的 scripts/ references/ assets/ 文件都在包内）")
    if a.build_only:
        return 0
    system_prompt = (HERE / "agent_system_prompt.md").read_text(encoding="utf-8")
    if a.dry_run:
        print("PLAN: import", SKILLS, "→ create/update agent", cfg["agent_name"], "→ PUT /api/agents/{id}/skills",
              "→ post-check (bound / same account / no legacy schedules)", "→ seed memory" if a.seed_memory else "")
        return 0

    sp, sp_run = clients(cfg)
    if sp is None:
        print("ERROR: 没有令牌。会话令牌放 ~/.sciencepal/session_token（或环境变量 SCIENCEPAL_SETUP_TOKEN）；API Key 放 ~/.sciencepal/run_token")
        return 1
    ids_file = Path(cfg.get("platform_ids_file", "platform_ids.json"))
    if not ids_file.is_absolute():
        ids_file = (a.config.resolve().parent / ids_file).resolve()
    ids = json.loads(ids_file.read_text(encoding="utf-8")) if ids_file.is_file() else {"skills": {}}
    ids.setdefault("skills", {})

    if a.cleanup_schedules:
        return cleanup_schedules(session_only(cfg), a.yes)
    if a.check:
        return post_check(cfg, ids, sp, sp_run, session_only(cfg))

    failed = []
    for s in SKILLS:
        entry, msg = import_one(sp, s, zips[s], a.replace, ids["skills"].get(s))
        if entry is None or entry.get("stale"):
            failed.append(s)
            print(f"FAIL skill {s}: {msg}")
            if entry is not None:
                ids["skills"][s] = entry
            continue
        entry.pop("stale", None)
        ids["skills"][s] = entry
        print(f"skill {s}: {entry['skill_id']} ({msg})")

    desc = "你说“主动探索灵感”，它自己找值得做的事并整理成卡片申请；说“建立画像”，它能记得你"
    if not ids.get("agent_id"):
        resp = sp.create_agent(cfg["agent_name"], system_prompt, description=desc)
        ids["agent_id"] = pick_id(resp, "agent_id", "id")
        print("agent created:", ids["agent_id"])
    else:
        sp.update_agent(ids["agent_id"], {"system_prompt": system_prompt, "description": desc})
        print("agent updated:", ids["agent_id"])
    sp.put_agent_skills(ids["agent_id"], [ids["skills"][s]["skill_id"] for s in SKILLS if s in ids["skills"]])

    if a.seed_memory:
        seeds = json.loads((HERE / "memory_seeds.json").read_text(encoding="utf-8"))
        ids["memory_ids"] = []
        for m in seeds:
            r = sp.create_memory(m["content"], m.get("category"), m.get("tags"), m.get("memory_type", "preference"), m.get("confidence", 1.0))
            ids["memory_ids"].append(pick_id(r, "id", "memory_id"))
        print("memory seeded:", ids["memory_ids"])

    ids["package_version"] = (PKG / "VERSION").read_text(encoding="utf-8").strip()
    ids_file.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: 写入 {ids_file}")
    rc = post_check(cfg, ids, sp, sp_run, session_only(cfg))
    if failed:
        print(f"ERROR: {len(failed)} 个技能没有更新成功：{failed}（其余技能和灵感官已更新）。修好后重跑 --replace")
        rc = 1
    print("下一步：python platform/receiver.py --config", a.config, "--status ；在网页里对灵感官说“建立画像”。")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
