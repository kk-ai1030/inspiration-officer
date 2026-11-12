#!/usr/bin/env python3
"""Publish the local state INTO the platform skill insp-nightly (v1.5.5, report P11).

Why: a conversation started in the web UI (“@灵感官 主动探索灵感”) never goes through the review page, so nothing local
is attached — the sandbox started from the empty template: no observed.jsonl (= no A-class optimization cards), no
pushed list, no negative list. The skill folder IS mounted in every sandbox, so we ship the state inside it:

    insp-nightly/assets/local_state/{state.json, negative.jsonl, observed.jsonl, taste/…, profile/…, snapshot.json}

insp_run.py prepare uses it (mode “skill_snapshot”) whenever nothing newer was attached / persisted.

When it runs (automatically; all best effort):
  - right after every collected run (receiver.sync_back)          - demo_reset.bat (after the reset)
  - the review page's background sync, every observe.publish_every_min minutes (default 30), only if something changed
  - by hand:  python platform/publish_state.py --config config.json [--no-scan] [--force] [--dry-run]

Needs the session token (~/.sciencepal/session_token): skill import is not on the API-key allow-list.
config.json (optional): "observe": {"publish_snapshot": true, "publish_every_min": 30}
The package folder itself is never modified (the zip is built in a temp folder).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
SKILL = "insp-nightly"
SKILLS = ["insp-init", "insp-triage", "insp-card", "insp-feedback", "insp-review", "insp-nightly", "insp-profile"]
STATE_FILES = ["state.json", "negative.jsonl", "taste/rubric.md", "taste/weights.json",
               "profile/interests.md", "profile/claims.md", "profile/observed.md", "observed.jsonl",
               "verdict_summary.json"]  # B4 / K3: 脱敏周报摘要；不存在时 state_files() 自动跳过
MAX_FILE = 2_000_000   # a runaway file must not blow up the skill upload


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def enabled(cfg: dict) -> bool:
    return bool((cfg.get("observe") or {}).get("publish_snapshot", True))


def every_s(cfg: dict) -> float:
    try:
        return max(5.0, float((cfg.get("observe") or {}).get("publish_every_min", 30))) * 60
    except (TypeError, ValueError):
        return 1800.0


def state_files(state_dir: Path) -> dict[str, bytes]:
    out = {}
    for rel in STATE_FILES:
        p = state_dir / rel
        if p.is_file() and p.stat().st_size <= MAX_FILE:
            out[rel] = p.read_bytes()
    return out


def digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode() + b"\0" + hashlib.sha256(files[rel]).digest())
    h.update((PKG / "VERSION").read_bytes() if (PKG / "VERSION").is_file() else b"")
    return h.hexdigest()


def _count(files: dict[str, bytes]) -> dict:
    def lines(rel):
        return sum(1 for ln in files.get(rel, b"").decode("utf-8", "replace").splitlines() if ln.strip())
    try:
        st = json.loads(files.get("state.json", b"{}").decode("utf-8") or "{}")
    except json.JSONDecodeError:
        st = {}
    return {"observed": lines("observed.jsonl"), "negative": lines("negative.jsonl"),
            "pushed": len(st.get("pushed") or []), "seen_ids": len(st.get("seen_ids") or [])}


def build_zip(files: dict[str, bytes], dist: Path, info: dict) -> Path:
    src = PKG / SKILL
    out = dist / f"{SKILL}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc" and "local_state" not in f.parts:
                z.write(f, f.relative_to(src).as_posix())
        for rel, data in files.items():
            z.writestr(f"assets/local_state/{rel}", data or b"\n")
        z.writestr("assets/local_state/snapshot.json", json.dumps(info, ensure_ascii=False, indent=2))
    return out


def skill_name() -> str:
    text = (PKG / SKILL / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^name:\s*(.+)$", text, flags=re.M)
    return m.group(1).strip() if m else SKILL


def _pick_id(resp, *keys):
    if isinstance(resp, dict):
        for k in keys:
            if resp.get(k):
                return str(resp[k])
        for v in resp.values():
            r = _pick_id(v, *keys)
            if r:
                return r
    return None


def publish(cfg: dict, cfg_path: Path, state_dir: Path, client, force: bool = False, dry_run: bool = False,
            reason: str = "") -> dict:
    """Returns {"status": published|unchanged|disabled|no_token|dry_run|error, ...}; never raises."""
    rec_p = state_dir / "inbox" / "snapshot_published.json"
    try:
        last = json.loads(rec_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        last = {}
    if not enabled(cfg) and not force:
        return {"status": "disabled", "message": "observe.publish_snapshot=false：不发布本地快照（网页对话入口拿不到本地观察）"}
    files = state_files(state_dir)
    if "state.json" not in files:
        return {"status": "error", "message": f"{state_dir} 里没有 state.json"}
    sha = digest(files)
    counts = _count(files)
    if sha == last.get("sha") and not force:
        return {"status": "unchanged", "sha": sha[:12], **counts, "published_at": last.get("at")}
    ver = (PKG / "VERSION").read_text(encoding="utf-8").strip() if (PKG / "VERSION").is_file() else "?"
    info = {"published_at": now_iso(), "published_epoch": time.time(), "sha": sha, "package_version": ver,
            "files": sorted(files), **counts, "reason": reason}
    if dry_run:
        return {"status": "dry_run", "sha": sha[:12], **counts}
    if client is None:
        return {"status": "no_token", **counts,
                "message": "没有会话令牌（~/.sciencepal/session_token）：本地快照发不到技能里，网页对话入口看不到本地观察"}
    ids_p = (cfg_path.parent / cfg.get("platform_ids_file", "platform_ids.json")).resolve()
    try:
        ids = json.loads(ids_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        ids = {}
    try:
        with tempfile.TemporaryDirectory() as d:
            z = build_zip(files, Path(d), info)
            resp = client.import_skill_zip(z, name=skill_name(), replace=True)
        sid = _pick_id(resp, "skill_id", "id")
        if not sid:
            return {"status": "error", "message": f"导入技能没有返回 skill_id：{str(resp)[:200]}"}
        skills = ids.setdefault("skills", {})
        old = (skills.get(SKILL) or {}).get("skill_id")
        skills[SKILL] = {**(skills.get(SKILL) or {}), "skill_id": sid, "mode": "zip", "snapshot_at": info["published_at"]}
        skills[SKILL].pop("stale", None)
        rebound = False
        if old != sid and ids.get("agent_id"):
            client.put_agent_skills(ids["agent_id"], [skills[s]["skill_id"] for s in SKILLS if (skills.get(s) or {}).get("skill_id")])
            rebound = True
        if ids:
            ids_p.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status", None)
        hint = "（会话令牌过期或无权限：重新登录网页复制令牌）" if status in (401, 403) else ""
        return {"status": "error", "message": f"{type(exc).__name__}{' HTTP ' + str(status) if status else ''}: "
                                               f"{str(exc)[:200]}{hint}", **counts}
    rec = {"sha": sha, "at": info["published_at"], "skill_id": sid, "rebound": rebound, "reason": reason, **counts}
    rec_p.parent.mkdir(parents=True, exist_ok=True)
    rec_p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "published", **rec}


def describe(r: dict) -> str:
    s = r.get("status")
    if s == "published":
        return (f"本地快照已发布到技能 {SKILL}（本地观察 {r.get('observed', 0)} 条 · 已推送 {r.get('pushed', 0)} · "
                f"负面清单 {r.get('negative', 0)}）：网页里直接说“主动探索灵感”也会用到它")
    if s == "unchanged":
        return f"本地快照没有变化（上次发布 {r.get('published_at') or '—'}，本地观察 {r.get('observed', 0)} 条）"
    if s == "dry_run":
        return f"（试运行）将发布：本地观察 {r.get('observed', 0)} 条 · 已推送 {r.get('pushed', 0)}"
    return f"本地快照未发布：{r.get('message') or s}"


def refresh(ctx, scan: bool = True, force: bool = False, reason: str = "") -> dict:
    """Scan the local library (observe) and publish if anything changed. ctx = receiver.Ctx."""
    if not enabled(ctx.cfg) and not force:
        return {"status": "disabled"}
    try:
        client = ctx.setup_client()
    except Exception:  # noqa: BLE001
        client = None
    if client is None and not force:   # nothing to publish to: do not even scan
        return publish(ctx.cfg, ctx.cfg_path, ctx.state_dir, None, force=True, reason=reason)
    if scan:
        rc = sys.modules.get("receiver_ps") or _load("receiver.py", "receiver_ps")
        rc.observe_before_run(ctx)
    return publish(ctx.cfg, ctx.cfg_path, ctx.state_dir, client, force=force, reason=reason)


# ----------------------------------------------------------------------------- self-test
def self_test() -> int:
    class FakeSP:
        def __init__(self):
            self.imports, self.bound = [], []

        def import_skill_zip(self, z, name=None, replace=False, agent_type="general"):
            with zipfile.ZipFile(z) as zz:
                names = zz.namelist()
                snap = json.loads(zz.read("assets/local_state/snapshot.json"))
            self.imports.append((names, snap, replace))
            return {"skill": {"id": f"sk_{len(self.imports)}"}}

        def put_agent_skills(self, agent_id, ids):
            self.bound.append((agent_id, ids))

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        sd = t / "inspiration"
        (sd / "inbox").mkdir(parents=True)
        (sd / "state.json").write_text(json.dumps({"pushed": [{"id": "obs_1"}], "seen_ids": ["a", "b"]}), encoding="utf-8")
        (sd / "observed.jsonl").write_text('{"id":"obs_1"}\n{"id":"obs_2"}\n', encoding="utf-8")
        (t / "platform_ids.json").write_text(json.dumps({"agent_id": "ag", "skills": {s: {"skill_id": "old_" + s} for s in SKILLS}}),
                                             encoding="utf-8")
        cfg = {"observe": {}}
        sp = FakeSP()
        r = publish(cfg, t / "config.json", sd, sp, reason="test")
        assert r["status"] == "published" and r["observed"] == 2 and r["pushed"] == 1, r
        names, snap, rep = sp.imports[0]
        assert rep and "SKILL.md" in names and "scripts/insp_run.py" in names and "assets/local_state/observed.jsonl" in names, names
        assert snap["observed"] == 2 and snap["published_epoch"] > 0
        ids = json.loads((t / "platform_ids.json").read_text(encoding="utf-8"))
        assert ids["skills"][SKILL]["skill_id"] == "sk_1" and sp.bound and "sk_1" in sp.bound[0][1], (ids, sp.bound)
        assert publish(cfg, t / "config.json", sd, sp)["status"] == "unchanged" and len(sp.imports) == 1
        (sd / "observed.jsonl").write_text('{"id":"obs_1"}\n', encoding="utf-8")
        assert publish(cfg, t / "config.json", sd, sp)["status"] == "published" and len(sp.imports) == 2
        assert publish(cfg, t / "config.json", sd, None, force=True)["status"] == "no_token"
        assert publish({"observe": {"publish_snapshot": False}}, t / "config.json", sd, sp)["status"] == "disabled"
        assert not (PKG / SKILL / "assets" / "local_state").exists(), "the package folder must stay clean"
    print("SELF-TEST OK: publish_state")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把本地状态（含本地观察）发布进平台技能 insp-nightly，网页对话入口也能用")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--no-scan", action="store_true", help="不先扫描本地 library")
    ap.add_argument("--force", action="store_true", help="没变化也重新发布")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.config:
        ap.error("--config is required")
    rc = _load("receiver.py", "receiver_ps")
    ctx = rc.Ctx(a.config)
    if a.dry_run:
        if not a.no_scan:
            rc.observe_before_run(ctx)
        r = publish(ctx.cfg, ctx.cfg_path, ctx.state_dir, None, force=True, dry_run=True)
    else:
        r = refresh(ctx, scan=not a.no_scan, force=a.force, reason="manual")
    print(describe(r))
    return 0 if r.get("status") in ("published", "unchanged", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
