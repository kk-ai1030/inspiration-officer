#!/usr/bin/env python3
"""收货员（v1.4）：把灵感官在 SciencePal 沙箱里跑出的“整包”自动收回本地，校验后入库、弹通知、开评价页。

用户这边只有一句话：在网页里 @灵感官 说“主动探索灵感”（或在评价页点“主动探索灵感”）。
收货员负责剩下的一切：发现结果 → 下载 bundle.json / triage.json / 原文 → 本地独立复核
（字段、原文逐字引文、去重、配额、arXiv 存在性）→ 写卡片库与 state → 通知 → 把最新状态同步回该会话的沙箱。

    python platform/receiver.py --config config.json --once            # 现在检查一次
    python platform/receiver.py --config config.json --watch           # 常驻（评价页服务默认已内置，不必单独开）
    python platform/receiver.py --config config.json --thread <会话链接或ID>   # 指定收某个会话
    python platform/receiver.py --config config.json --run             # 从本地触发一次（自动带上状态，零上传）
    python platform/receiver.py --config config.json --from-dir <本地文件夹>   # 离线：手动下载的 inspiration_out/
    python platform/receiver.py --config config.json --status

没有任何定时任务：收货员只“查看结果”，从不自己发起 agent 运行；--run 也只在你点按钮/敲命令时才发生。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
from sciencepal_client import (AUTH_DENIED, ApiError, MultiAuth, SciencePal, collect_strings,  # noqa: E402
                               extract_block, extract_blocks, extract_json_block)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PKG / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


RL = _load("platform/run_local.py", "run_local_rcv")
WC = _load("insp-feedback/scripts/weights_config.py", "weights_config_rcv")
BLOCK_START, BLOCK_END = "<<<INSP_BUNDLE_JSON", "INSP_BUNDLE_JSON>>>"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
DEFAULTS = {"enabled": True, "poll_s": 45, "lookback_h": 36, "max_threads_per_poll": 12,
            "out_dir": "/workspace/inspiration_out", "state_home": "/workspace/inspiration_state",
            "push_state": True, "memory_digest": True, "auto_fallback_local": True, "run_prompt": "主动探索灵感",
            "min_trigger_gap_s": 600, "reply_stream_timeout_s": 90,
            "auto_continue": 1}   # v1.5.5: a run that ended before writing its cards gets one automatic follow-up
TEST_MARK = ".test_state"


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


class Ctx:
    def __init__(self, cfg_path: Path):
        self.cfg_path = cfg_path.resolve()
        self.cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        WC.load(self.cfg_path)  # BUG-7: a broken weights block must fail at every entry point
        self.base = self.cfg_path.parent
        self.state_dir = (self.base / self.cfg["state_dir"]).resolve()
        self.rc = dict(DEFAULTS, **(self.cfg.get("receiver") or {}))
        self.inbox = self.state_dir / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        RL.RUNTIME.update(cfg=self.cfg, cfg_path=self.cfg_path)
        RL.VERDICT.set_config(self.cfg_path)

    def client(self) -> SciencePal | None:
        tok = RL.run_token(self.cfg)
        if not tok:
            return None
        return SciencePal(self.cfg["base_url"], tok, self.cfg.get("auth_header", "Authorization"), self.cfg.get("auth_scheme", "Bearer"))

    def setup_client(self) -> SciencePal | None:
        tok = os.environ.get(self.cfg.get("setup_token_env", "SCIENCEPAL_SETUP_TOKEN"), "")
        p = Path(os.path.expanduser(self.cfg.get("setup_token_file", "~/.sciencepal/session_token")))
        if not tok and p.is_file():
            tok = p.read_text(encoding="utf-8").strip()
        if not tok:
            return None
        return SciencePal(self.cfg["base_url"], tok, self.cfg.get("auth_header", "Authorization"), self.cfg.get("auth_scheme", "Bearer"))

    def reader(self) -> MultiAuth | None:
        """Credentials for reading conversation data (v1.4.2, feedback4 BUG-3/4).

        Session token first: thread files, the conversation list and thread → runs / sandbox lookups are NOT
        on the API-key allow-list ("SDK catalogs, agent, run and sandbox endpoints"). API key second: run
        streams and /api/sandboxes/* work with it, and an expired session token (401) falls through to it.
        """
        sess, key = self.setup_client(), self.client()
        if sess is None and key is None:
            return None
        return MultiAuth([sess, key], ["session", "api_key"])

    def ids(self) -> dict:
        p = self.base / self.cfg.get("platform_ids_file", "platform_ids.json")
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


# ----------------------------------------------------------------------------- ledgers
def _jl_read(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


class KeyLock:
    """Cross-process lock (O_EXCL lock file). feedback5 3.1/3.6: the review page's watcher and a waiting
    `receiver.py --run` collected the same run 2 s apart → duplicate runs.jsonl rows and memories written twice."""

    def __init__(self, ctx: Ctx, name: str, wait_s: float = 0.0, stale_s: float = 900.0):
        d = ctx.inbox / "locks"
        d.mkdir(parents=True, exist_ok=True)
        self.p = d / (hashlib.sha1(name.encode("utf-8")).hexdigest()[:20] + ".lock")
        self.wait_s, self.stale_s, self.ok = wait_s, stale_s, False

    def __enter__(self) -> "KeyLock":
        t_end = time.time() + self.wait_s
        while True:
            try:
                fd = os.open(str(self.p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.ok = True
                return self
            except FileExistsError:
                try:
                    if time.time() - self.p.stat().st_mtime > self.stale_s:  # crashed holder
                        self.p.unlink()
                        continue
                except OSError:
                    continue
                if time.time() >= t_end:
                    return self
                time.sleep(0.2)

    def __exit__(self, *exc) -> None:
        if self.ok:
            try:
                self.p.unlink()
            except OSError:
                pass


def collected_keys(ctx: Ctx) -> set[str]:
    return {r.get("key") for r in _jl_read(ctx.inbox / "collected.jsonl")}


def mark_collected(ctx: Ctx, key: str, row: dict) -> None:
    with (ctx.inbox / "collected.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "at": now().isoformat(timespec="seconds"), **row}, ensure_ascii=False) + "\n")


def status_write(ctx: Ctx, **kw) -> None:
    p = ctx.inbox / "receiver_status.json"
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cur = {}
    cur.update(kw, updated_at=now().isoformat(timespec="seconds"))
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def pending_runs(ctx: Ctx) -> list[dict]:
    p = ctx.inbox / "pending_runs.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save_pending(ctx: Ctx, rows: list[dict]) -> None:
    (ctx.inbox / "pending_runs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------- sources
def _unwrap(data: bytes | None) -> bytes | None:
    """The thread-file endpoint is untyped in the OpenAPI; accept raw bytes or a {"content": "..."} wrapper."""
    if data is None:
        return None
    try:
        j = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return data
    if isinstance(j, dict) and isinstance(j.get("content"), str) and len(j) <= 6 and "bundle_version" not in j \
            and "schema_version" not in j:
        return j["content"].encode("utf-8")
    return data


class DirSource:
    """A local folder plays the role of /workspace/inspiration_out (offline tests / manual download)."""
    def __init__(self, d: Path):
        self.d, self.thread_id, self.label = Path(d), None, f"dir:{d}"

    def read(self, rel: str) -> bytes | None:
        p = self.d / Path(rel).name
        return p.read_bytes() if p.is_file() else None


class ThreadSource:
    """Files in the conversation's sandbox. Never raises on 401/403: records it in .denied and returns None,
    so the caller can fall back to the INSP_BUNDLE_JSON block in the reply (feedback4: an uncaught 403 here
    used to kill the receiver before the fallback was reached)."""
    MISSING = (400, 404, 422)

    def __init__(self, sp, thread_id: str, out_dir: str, sandbox_id: str | None = None):
        self.sp, self.thread_id, self.out, self.label = sp, thread_id, out_dir.rstrip("/"), f"thread:{thread_id}"
        self.sandbox_id, self._sid_looked = sandbox_id, bool(sandbox_id)
        self.denied: str | None = None   # e.g. "thread-files HTTP 403; sandbox-files HTTP 403"
        self.via: str | None = None      # which route produced the last file

    def _sid(self) -> str | None:
        if not self._sid_looked:
            self._sid_looked = True
            try:
                info = self.sp.thread_sandbox(self.thread_id)
                self.sandbox_id = info.get("sandbox_id") or (info.get("sandbox") or {}).get("id")
            except ApiError:
                self.sandbox_id = None
        return self.sandbox_id

    def read(self, rel: str) -> bytes | None:
        path, why = f"{self.out}/{rel}", []
        try:
            data = _unwrap(self.sp.read_thread_file(self.thread_id, path))
            self.via = "thread-files/" + getattr(self.sp, "used", {}).get("read_thread_file", "api_key")
            return data
        except ApiError as exc:
            if exc.status in self.MISSING:
                return None
            if exc.status not in AUTH_DENIED:
                raise
            why.append(f"thread-files HTTP {exc.status}")
        sid = self._sid()
        if sid:
            try:
                data = _unwrap(self.sp.read_sandbox_file(sid, path, thread_id=self.thread_id))
                self.via = "sandbox-files/" + getattr(self.sp, "used", {}).get("read_sandbox_file", "api_key")
                return data
            except ApiError as exc:
                if exc.status in self.MISSING:
                    return None
                if exc.status not in AUTH_DENIED:
                    raise
                why.append(f"sandbox-files HTTP {exc.status}")
        else:
            why.append("没有 sandbox_id")
        self.denied = "; ".join(why)
        return None


def thread_id_of(s: str) -> str:
    m = re.search(r"thread[s]?/(" + UUID_RE.pattern + ")", s, re.I) or UUID_RE.search(s)
    if not m:
        raise SystemExit(f"认不出会话 ID：{s}（把浏览器地址栏里的会话链接整段贴进来即可）")
    return m.group(1) if m.lastindex else m.group(0)


def _is_bundle(o) -> bool:
    return isinstance(o, dict) and str(o.get("bundle_version", "")).startswith("insp-bundle/")


def runs_of_thread(sp, thread_id: str, known: str | None = None) -> list[str]:
    runs = [known] if known else []
    if not runs:
        try:  # session token only on the real platform; with just an API key this is 403
            r = sp.thread_agent_runs(thread_id)
            items = r.get("agent_runs", r.get("runs", r)) if isinstance(r, dict) else r
            runs = [x.get("id") or x.get("agent_run_id") for x in (items or []) if isinstance(x, dict)][-2:]
        except ApiError:
            runs = []
    return [x for x in runs if x]


def bundle_from_reply(sp, thread_id: str, agent_run_id: str | None = None, stream_timeout_s: int = 90) -> bytes | None:
    """Fallback when the file is unreadable: take the INSP_BUNDLE_JSON block out of the run.

    1) GET /api/agent-run/{id} (allowed for API keys); 2) replay the run's event stream (allowed for API keys;
    contains finalize's own stdout, so it does not depend on the model copying the block verbatim).
    Only a block that parses as a bundle is accepted — skill text that merely quotes the markers is skipped.
    """
    for rid in reversed(runs_of_thread(sp, thread_id, agent_run_id)):
        texts: list[str] = []
        try:
            texts = collect_strings(sp.get_run(rid))
        except ApiError:
            pass
        block = best_bundle_block(texts)
        if not block:
            try:
                block = best_bundle_block(collect_strings(sp.stream_run(rid, timeout_s=stream_timeout_s)))
            except ApiError:
                block = None
        if block:
            return block.encode("utf-8")
    return None


def best_bundle_block(texts: list[str]) -> str | None:
    """Among all blocks that parse as a bundle, prefer the latest one whose embedded triage_min checksum
    verifies (finalize's own stdout), over a copy the model may have shortened in its final answer."""
    good = []
    for blk in extract_blocks(texts, BLOCK_START, BLOCK_END):
        try:
            o = json.loads(blk)
        except (json.JSONDecodeError, ValueError):
            continue
        if _is_bundle(o):
            ok = isinstance(o.get("triage_min"), dict) and o.get("triage_min_sha256") == canon_sha(o["triage_min"])
            good.append((ok, blk))
    if not good:
        return None
    verified = [b for ok, b in good if ok]
    return (verified or [b for _, b in good])[-1]


def canon_sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- guards
def test_ids_guard(ctx: Ctx, ids: list[str]) -> str | None:
    """BUG-6: fixture ids (9999.*) must never land in a real state dir."""
    if any(str(i).startswith("9999.") for i in ids) and not (ctx.state_dir / TEST_MARK).exists():
        return ("拒绝写入：结果里含测试夹具 ID（9999.*），而状态目录不是测试目录。"
                f"测试请用临时状态目录（目录里放一个 {TEST_MARK} 文件）。")
    return None


def check_account(sp: SciencePal, agent_id: str | None) -> str | None:
    """BUG-3: the token and the 灵感官 agent must belong to the same account."""
    if not agent_id:
        return "platform_ids.json 里没有 agent_id：先运行 setup_platform.py"
    try:
        ids = {str(a.get("agent_id") or a.get("id")) for a in sp.whoami_agents()}
    except ApiError as exc:
        return f"token 无效或已过期（GET /api/agents → HTTP {exc.status}）"
    if ids and agent_id not in ids:
        return (f"token 与灵感官不属于同一个账号：agent {agent_id[:8]}… 不在这个 token 能看到的 {len(ids)} 个智能体里。"
                "请在灵感官所在账号重新生成 API Key 写进 ~/.sciencepal/run_token，或在当前账号重新运行 setup_platform.py")
    return None


# ----------------------------------------------------------------------------- ingest
def ingest(ctx: Ctx, src, origin: dict | None = None, reply_bytes: bytes | None = None) -> dict:
    """One collector per conversation at a time (see KeyLock); the loser reports ALREADY instead of a second row."""
    with KeyLock(ctx, f"ingest:{src.thread_id or src.label}") as lk:
        if not lk.ok:
            return {"status": "ALREADY", "key": f"{src.thread_id or src.label}:*", "note": "另一个收货进程正在处理这个会话"}
        return _ingest(ctx, src, origin, reply_bytes)


def knowledge_already(ctx: Ctx, thread_id: str | None, bundle_run_id: str | None) -> int:
    """v1.5.4 (S7): knowledge cards already in the library from THIS run (same conversation or same bundle run id).
    One run was collected twice in v1.5.3 (5 + 1 = 6 knowledge cards); the cap of 5 now counts both."""
    n = 0
    for f in (ctx.state_dir / "cards").glob("*/cards.json"):
        try:
            cards = json.loads(f.read_text(encoding="utf-8")).get("cards", [])
        except (OSError, json.JSONDecodeError):
            continue
        for c in cards:
            tr = c.get("_trace") or {}
            if c.get("type") == "knowledge" and ((thread_id and tr.get("thread_id") == thread_id)
                                                 or (bundle_run_id and tr.get("bundle_run_id") == bundle_run_id)):
                n += 1
    return n


def _ingest(ctx: Ctx, src, origin: dict | None = None, reply_bytes: bytes | None = None) -> dict:  # noqa: C901
    origin = origin or {}
    file_raw = None if reply_bytes else src.read("bundle.json")
    raw = file_raw or reply_bytes
    bundle_from = "file" if file_raw else ("reply" if reply_bytes else None)
    if raw is None:
        return {"status": "NO_BUNDLE"}
    try:
        b = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"status": "BAD_BUNDLE", "error": str(exc)}
    if not str(b.get("bundle_version", "")).startswith("insp-bundle/"):
        return {"status": "BAD_BUNDLE", "error": "不是灵感整包（缺 bundle_version）"}
    key = f"{src.thread_id or src.label}:{b.get('run_id')}"
    if key in collected_keys(ctx):
        return {"status": "ALREADY", "key": key}

    t0 = now()
    today = t0.date().isoformat()
    run_id = "rcv-" + t0.strftime("%Y%m%d-%H%M%S")
    run_dir = (ctx.base / ctx.cfg.get("work_dir", "runs") / run_id).resolve()
    k = 1
    while run_dir.exists():
        k += 1
        run_dir = run_dir.with_name(f"{run_id}_{k}")
    run_id = run_dir.name
    run_dir.mkdir(parents=True)
    (run_dir / "bundle.json").write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    row = {"run_id": run_id, "started_at": t0.isoformat(timespec="seconds"), "via": "receiver", "source": src.label,
           "thread_id": src.thread_id, "bundle_run_id": b.get("run_id"), "runner_version": b.get("runner_version"),
           "state_mode": b.get("state_mode"), "query_source": b.get("query_source"), "bundle_from": bundle_from,
           "bundle_notes": b.get("notes", []), "file_via": getattr(src, "via", None),
           "file_denied": getattr(src, "denied", None), "cards": [], "dedup_dropped": []}
    status = b.get("status")

    def done(res: dict) -> dict:
        mark_collected(ctx, key, {"run_id": run_id, "status": res.get("status"), "cards": res.get("cards", 0)})
        status_write(ctx, last_result={**res, "run_id": run_id, "source": src.label,
                                        "at": now().isoformat(timespec="seconds"), "thread": (src.thread_id or "")[:8]})
        return res

    if status in ("FETCH_BLOCKED", "STATE_MISSING"):
        row.update(status=status, finished_at=now().isoformat(timespec="seconds"))
        RL.log_run(ctx.state_dir, row)
        msg = "；".join(b.get("notes", []))[:300]
        fb = None
        if status == "FETCH_BLOCKED" and ctx.rc.get("auto_fallback_local", True) and RL.run_token(ctx.cfg):
            fb = spawn_local_fallback(ctx)
            msg += "　已自动改走本地抓取（约 10–20 分钟后出结果）" if fb else ""
        RL.review_notify(ctx.state_dir, f"灵感运行：{status}", msg or status, link=False)
        return done({"status": status, "fallback_pid": fb})

    tri_raw = src.read((b.get("files") or {}).get("triage", "triage.json"))
    problems = []
    if tri_raw is not None:
        row["triage_from"] = "file"
        (run_dir / "triage.json").write_bytes(tri_raw)
        tri = json.loads(tri_raw.decode("utf-8"))
        if b.get("triage_sha256") and hashlib.sha256(tri_raw).hexdigest() != b["triage_sha256"]:
            problems.append("triage.json 与整包里登记的 sha256 不一致")
    elif isinstance(b.get("triage_min"), dict):
        # v1.4.2 bundles carry the triage fields the receiver needs; the checksum catches a damaged copy
        tri = b["triage_min"]
        row["triage_from"] = "bundle"
        (run_dir / "triage.json").write_text(json.dumps(tri, ensure_ascii=False, indent=1), encoding="utf-8")
        if b.get("triage_min_sha256") != canon_sha(tri):
            problems.append("整包里的 triage_min 与登记的 sha256 不一致（结果块在复制时被改动）")
    else:
        why = f"（{src.denied}）" if getattr(src, "denied", None) else ""
        row.update(status="VALIDATION_FAILED", problem=f"读不到沙箱里的 triage.json{why}，整包里也没有 triage_min（v1.4.1 及更早的整包）。"
                   "把会话令牌放进 ~/.sciencepal/session_token 后重新收货")
        row["retryable"] = True
        RL.log_run(ctx.state_dir, row)
        # NOT marked as collected: once a session token is in place, the next poll / --thread collects it
        status_write(ctx, last_result={"status": "VALIDATION_FAILED", "problem": row["problem"], "run_id": run_id, "source": src.label})
        return {"status": "VALIDATION_FAILED", "problem": row["problem"], "retryable": True}
    if b.get("triage_counts") and tri.get("counts") != b["triage_counts"]:
        problems.append(f"计数对不上：整包 {b['triage_counts']} ≠ triage.json {tri.get('counts')}")
    c = tri.get("counts", {})
    if c and c.get("raw", 0) < c.get("seen", 0) + c.get("new", 0) - c.get("deferred", 0):
        problems.append(f"计数自相矛盾：raw {c.get('raw')} < seen {c.get('seen')} + new {c.get('new')}")
    guard = test_ids_guard(ctx, list(tri.get("new_ids", [])) + [x.get("id", "") for x in tri.get("shortlisted", [])])
    if guard:
        problems.append(guard)
    if problems:
        row.update(status="VALIDATION_FAILED", problem="；".join(problems), finished_at=now().isoformat(timespec="seconds"))
        RL.log_run(ctx.state_dir, row)
        print("VALIDATION_FAILED:", row["problem"])
        return done({"status": "VALIDATION_FAILED", "problem": row["problem"]})
    row["counts"] = tri.get("counts")

    if status == "NO_CANDIDATES" or not b.get("cards"):
        RL.commit_success(ctx.state_dir, tri, [], today, run_id)
        row.update(status="NO_CANDIDATES", finished_at=now().isoformat(timespec="seconds"))
        RL.log_run(ctx.state_dir, row)
        RL.review_notify(ctx.state_dir, "灵感运行完成：今天没有新卡片",
                         f"抓到 {c.get('raw', 0)} 篇，新增 {c.get('new', 0)}，入围 {c.get('shortlisted', 0)}（没有可出卡的候选）", link=False)
        sync_back(ctx, src)
        return done({"status": "NO_CANDIDATES", "cards": 0})

    # read mode of THIS bundle (decided when the run started); older 1.4.0 bundles: infer from fulltext_index
    bmode = b.get("fetch_mode") or tri.get("fetch_mode") or ("fulltext" if b.get("fulltext_index") else "abstract")
    bmode = RL.FT.resolve_fetch_mode(bmode)[0]
    row["fetch_mode"] = bmode
    cmode = current_fetch_mode(ctx)[0]
    if bmode != cmode:
        row["fetch_mode_note"] = (f"本次运行用的是 {bmode} 模式，而 config.json 当前是 {cmode}"
                                  "（网页里直接 @灵感官 时以记忆/指令为准；按评价页按钮触发会按 config.json）")

    # full texts: download what the cards quote, so quotes are re-checked locally (not just in the sandbox)
    bundle_dir = run_dir / "fulltext"
    bundle_dir.mkdir()
    ft_ok, missing = True, []
    for ref, ent in ((b.get("fulltext_index") or {}) if bmode == "fulltext" else {}).items():
        for f in [ent.get("file")] + [r.get("file") for r in ent.get("related", [])]:
            if not f or (bundle_dir / f).exists():
                continue
            data = src.read(f)
            if data is None:
                ft_ok = False
                missing.append(f)
            else:
                (bundle_dir / f).write_bytes(data)
    ftc = dict(RL.FT.DEFAULTS, **(ctx.cfg.get("fulltext") or {}))
    require = bmode == "fulltext" and ft_ok
    if not ft_ok:
        row["fulltext_local_check"] = f"下载失败 {len(missing)} 个原文文件，引文只在沙箱里核对过：{missing[:4]}"
    meta = b.get("cards_doc_meta") or {}
    doc = {"schema_version": meta.get("schema_version") or "1.0", "flow_version": meta.get("flow_version") or b.get("flow_version"),
           "run_id": meta.get("run_id") or b.get("run_id"), "generated_at": meta.get("generated_at") or b.get("generated_at"),
           "status": meta.get("status") or "OK", "cards": b.get("cards", []), "rejected": b.get("rejected", [])}
    (run_dir / "cards_raw.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    sel: list = []
    prior_k = knowledge_already(ctx, src.thread_id, b.get("run_id"))
    if prior_k:
        row["knowledge_prior"] = prior_k
    root, per, kept = RL.V_CARDS.validate(doc, tri, ctx.state_dir, RL.V_CARDS.FLOW_VERSION, None, sel,
                                          bundle_dir if (ft_ok and bmode == "fulltext") else None, require,
                                          int(ftc.get("min_related", 1)), prior_knowledge=prior_k)
    selected_ids = {r["card_id"] for r in sel}
    (run_dir / "selection.json").write_text(json.dumps({
        "log": sel, "knowledge_prior": prior_k,
        # v1.5.4: cards dropped BEFORE selection (already pushed / negative list / failed checks) — with their reasons
        "rejected_before_selection": {cid: errs for cid, errs in per.items() if errs and cid not in selected_ids}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    row["validation"] = {"root": root, "cards": per}
    dups = [cid for cid, errs in per.items() if any("已推送过" in e or "负面清单" in e for e in errs)]
    if dups:
        row["dedup_dropped"] = dups
    if root:
        row.update(status="VALIDATION_FAILED", problem="; ".join(root), finished_at=now().isoformat(timespec="seconds"))
        RL.log_run(ctx.state_dir, row)
        RL.notify(ctx.cfg, "灵感机制：整包没有通过本地校验（未入库、未改状态）", row["problem"])
        print("VALIDATION_FAILED:", row["problem"])
        return done({"status": "VALIDATION_FAILED", "problem": row["problem"]})
    abstracts = {re.sub(r"v\d+$", "", str(c.get("id", "")).split("/")[-1]): str(c.get("abstract") or "").strip()
                 for c in (tri.get("shortlisted") or []) + (tri.get("shortlist") or []) if isinstance(c, dict)}
    kept = json.loads(json.dumps(kept, ensure_ascii=False))   # never mutate the bundle: its hash is the collected key
    for card in kept:   # v1.5.5 (P2): keep the abstract with the card — 追问细节 needs it, runs/ may not be copied
        s = card.get("source") or {}
        akey = re.sub(r"v\d+$", "", str(s.get("ref", "")).replace("arXiv:", "").split("/")[-1].strip())
        if not s.get("abstract") and abstracts.get(akey):
            s["abstract"] = abstracts[akey][:3000]
    for card in kept:  # feedback3 §3.4: top-level source.section mirrors the first full-text quote
        q = (card.get("source") or {}).get("fulltext_quotes") or []
        if q and not card["source"].get("section"):
            card["source"]["section"] = q[0].get("section")
    reply = {"thread_id": src.thread_id, "agent_run_id": origin.get("agent_run_id"),
             "skill_loaded": f"bundle:{b.get('runner_version')}", "bundle_run_id": b.get("run_id")}
    RL.finalize_cards(ctx.cfg, ctx.state_dir, tri, kept, today, run_id, row, reply,
                      no_arxiv_check=bool(origin.get("no_arxiv_check")))
    sync_back(ctx, src)
    n_in = len(row.get("cards") or [])  # after the arXiv check (finalize_cards may drop cards)
    res = {"status": "OK" if n_in else "NO_CANDIDATES", "cards": n_in, "dedup_dropped": len(dups)}
    if row.get("arxiv_rejected"):
        res["arxiv_rejected"] = len(row["arxiv_rejected"])
    return done(res)


# ----------------------------------------------------------------------------- state back into the sandbox
STATE_FILES = ["state.json", "negative.jsonl", "taste/rubric.md", "taste/weights.json",
               "profile/interests.md", "profile/claims.md", "profile/observed.md",
               "observed.jsonl"]  # v1.5: local observers' findings (platform/observe.py) → sandbox observed channel
SUMMARY_FILE = "verdict_summary.json"  # B4 / K3: 脱敏周报摘要，只在文件存在时一起推（原始 verdicts.jsonl 永不上传）


def state_files_for(ctx: Ctx) -> list[str]:
    """B4：摘要是运行时生成的，缺省不存在时文件集合与旧版逐字一致。"""
    return STATE_FILES + ([SUMMARY_FILE] if (ctx.state_dir / SUMMARY_FILE).is_file() else [])


def sync_back(ctx: Ctx, src) -> None:
    # B4 / K3: 入库成功后刷新脱敏评价摘要，网页里做周报才有可信数据源（失败不影响收货）
    try:
        vs = sys.modules.get("verdict_summary_rc") or _load("platform/verdict_summary.py", "verdict_summary_rc")
        vs.write(ctx.state_dir, ctx.cfg_path)
    except Exception as exc:  # noqa: BLE001 - best effort; 本地状态仍是权威
        print("WARN 脱敏周报摘要未生成：", f"{type(exc).__name__}: {str(exc)[:200]}")
    if src.thread_id and ctx.rc.get("push_state", True):
        try:
            n = push_state(ctx, src.sp, src.thread_id, sandbox_id=getattr(src, "sandbox_id", None) or sandbox_of(ctx, src.thread_id))
            print(f"STATE → 会话沙箱：同步 {n} 个文件（下次在同一会话里跑会直接接着上次的状态）")
        except Exception as exc:  # best effort; the local copy stays authoritative
            print("WARN 状态同步回沙箱失败（不影响本地入库）：", str(exc)[:200])
    publish_snapshot(ctx, scan=False, reason="collected")
    if ctx.rc.get("memory_digest", True):
        try:
            memory_digest(ctx)
        except Exception as exc:
            print("WARN 记忆摘要未更新：", str(exc)[:200])
        try:
            settings_memory(ctx)
        except Exception as exc:
            print("WARN [灵感设置] 记忆未更新：", str(exc)[:200])


_PUBLISH = {"last_try": 0.0}


def publish_snapshot(ctx: Ctx, scan: bool = False, force: bool = False, reason: str = "") -> dict | None:
    """v1.5.5 (P11): local state + observations → platform skill insp-nightly, for runs started in the web UI."""
    _PUBLISH["last_try"] = time.time()
    try:
        ps = sys.modules.get("publish_state_rc") or _load("platform/publish_state.py", "publish_state_rc")
        r = ps.refresh(ctx, scan=scan, force=force, reason=reason)
        if r.get("status") not in ("unchanged", "disabled"):
            print(ps.describe(r))
        if r.get("status") != "unchanged":
            status_write(ctx, snapshot={k: r.get(k) for k in ("status", "at", "observed", "pushed", "message")})
        return r
    except Exception as exc:  # noqa: BLE001 - never break collecting
        print("WARN 本地快照未发布：", f"{type(exc).__name__}: {str(exc)[:200]}")
        return None


def known_runs(ctx: Ctx) -> list[dict]:
    """Runs this machine started (pending + last trigger + history) — they carry thread/run/sandbox ids."""
    rows = list(pending_runs(ctx))
    try:
        st = json.loads((ctx.inbox / "receiver_status.json").read_text(encoding="utf-8"))
        if isinstance(st.get("last_trigger"), dict):
            rows.append(st["last_trigger"])
    except (OSError, json.JSONDecodeError):
        pass
    rows += _jl_read(ctx.inbox / "triggered.jsonl")
    return rows


def run_of(ctx: Ctx, tid: str) -> str | None:
    return next((r.get("agent_run_id") for r in reversed(known_runs(ctx)) if r.get("thread_id") == tid and r.get("agent_run_id")), None)


def sandbox_of(ctx: Ctx, tid: str) -> str | None:
    return next((r.get("sandbox_id") for r in reversed(known_runs(ctx)) if r.get("thread_id") == tid and r.get("sandbox_id")), None)


def push_state(ctx: Ctx, sp, thread_id: str, sandbox_id: str | None = None) -> int:
    sid = sandbox_id
    if not sid:
        try:  # session token; API keys get 403 here — that is why triggers record sandbox_id
            info = sp.thread_sandbox(thread_id)
        except ApiError as exc:
            raise RuntimeError(f"查不到会话 {thread_id[:8]} 的沙箱（HTTP {exc.status}）：需要会话令牌，或用 --sandbox-id 指定") from None
        sid = info.get("sandbox_id") or (info.get("sandbox") or {}).get("id")
    if not sid:
        raise RuntimeError(f"拿不到会话 {thread_id[:8]} 的 sandbox_id")
    home = ctx.rc["state_home"].rstrip("/")
    n = 0
    for rel in state_files_for(ctx):
        p = ctx.state_dir / rel
        if p.is_file():
            sp.write_sandbox_file(sid, f"{home}/{rel}", p.read_bytes() or b"\n", name=Path(rel).name)
            n += 1
    return n


def upsert_single_memory(sp, match, text: str, category: str, tags: list[str]) -> str:
    """Exactly one memory matching `match` afterwards: keep one identical copy, delete every other match
    (also cleans up duplicates left by older versions, feedback5 3.6), create only if none was kept."""
    existing = sp.list_memory()
    items = existing.get("memories", existing.get("items", existing.get("data", existing))) if isinstance(existing, dict) else existing
    kept = False
    for m in items or []:
        if not isinstance(m, dict) or not match(m):
            continue
        if m.get("content") == text and not kept:
            kept = True
            continue
        sp.delete_memory(m.get("id") or m.get("memory_id"))
    if not kept:
        sp.create_memory(text, category=category, tags=tags, memory_type="fact")
    return "kept" if kept else "created"


def memory_digest(ctx: Ctx) -> str | None:
    """One "[灵感状态]" memory: recently pushed ids + blocked topics, so a run in a FRESH conversation (no state
    files) still skips them via insp_run.py --exclude. Needs a session token (API keys cannot write memory)."""
    sp = ctx.setup_client()
    if sp is None:
        return None
    st = json.loads((ctx.state_dir / "state.json").read_text(encoding="utf-8"))
    ids = [RL.V_CARDS.norm_id(p["id"]) for p in st.get("pushed", [])][-70:]
    neg = RL.TRIAGE.read_jsonl(ctx.state_dir / "negative.jsonl")
    topics = [n["value"] for n in neg if n.get("kind") == "topic"][-8:]
    text = (f"[灵感状态] 跑灵感时传给 insp_run.py --exclude 的已推送 arXiv ID：{','.join(ids)}" +
            (f"；已屏蔽主题：{'；'.join(topics)}" if topics else ""))[:990]
    with KeyLock(ctx, "memory-write", wait_s=60):
        upsert_single_memory(sp, lambda m: "insp-state" in (m.get("tags") or []) or str(m.get("content", "")).startswith("[灵感状态]"),
                             text, category="insp-state", tags=["insp-state", "auto-sync"])
    return text


def current_fetch_mode(ctx: Ctx) -> tuple[str, str | None]:
    """Re-read config.json every time, so editing arxiv_fetch_mode takes effect without restarting the review page."""
    try:
        cfg = json.loads(ctx.cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = ctx.cfg
    return RL.FT.cfg_fetch_mode(cfg)


def settings_memory(ctx: Ctx, force: bool = False) -> str | None:
    """One "[灵感设置]" memory with the paper read mode, so a run started directly in the web page
    (@灵感官, no local trigger) uses the same mode as config.json. Written only when the value changed.
    Needs a session token (API keys cannot write memory); without one the web route stays on the default (abstract)."""
    mode, _ = current_fetch_mode(ctx)
    text = (f"[灵感设置] 论文读取模式：跑灵感时传给 insp_run.py 的 --fetch-mode {mode}"
            f"（{'通读全文+相关文献，较慢' if mode == 'fulltext' else '只读摘要，默认、较快'}）；"
            + (f"全文模式另传 --fulltext-max {current_fulltext_max(ctx)}（每次最多读几篇），找引文只用 prepare 打印的短命令 python3 /workspace/fq <ID>；"
               if mode == "fulltext" else "")
            + "用户在本次指令里明确要求“通读全文/只看摘要”时以用户的话为准。")
    mark = ctx.inbox / "settings_memory.json"
    try:
        if not force and json.loads(mark.read_text(encoding="utf-8")).get("text") == text:
            return text
    except (OSError, json.JSONDecodeError):
        pass
    sp = ctx.setup_client()
    if sp is None:
        return None
    with KeyLock(ctx, "memory-write", wait_s=60):
        upsert_single_memory(sp, lambda m: "insp-settings" in (m.get("tags") or []) or str(m.get("content", "")).startswith("[灵感设置]"),
                             text, category="insp-settings", tags=["insp-settings", "auto-sync"])
    mark.write_text(json.dumps({"text": text, "at": now().isoformat(timespec="seconds")}, ensure_ascii=False), encoding="utf-8")
    return text


# ----------------------------------------------------------------------------- profile.json from insp-profile
PROFILE_START, PROFILE_END = "<<<INSP_PROFILE_JSON", "INSP_PROFILE_JSON>>>"


def profile_from_reply(sp, thread_id: str, agent_run_id: str | None = None, stream: bool = False,
                       stream_timeout_s: int = 90) -> bytes | None:
    """Fallback when /workspace/inspiration_out/profile.json is unreadable: take the block from the reply
    (GET /api/agent-run/{id}; optionally the replayed event stream — both allowed for API keys)."""
    for rid in reversed(runs_of_thread(sp, thread_id, agent_run_id)):
        texts: list[str] = []
        try:
            texts = collect_strings(sp.get_run(rid))
        except ApiError:
            pass
        block = extract_block(texts, PROFILE_START, PROFILE_END)
        if not block and stream:
            try:
                block = extract_block(collect_strings(sp.stream_run(rid, timeout_s=stream_timeout_s)), PROFILE_START, PROFILE_END)
            except ApiError:
                block = None
        if block:
            return block.encode("utf-8")
    return None


def ingest_profile(ctx: Ctx, src, raw: bytes | None = None) -> dict | None:
    raw = raw if raw is not None else src.read("profile.json")
    if raw is None:
        return None
    with KeyLock(ctx, f"profile:{src.thread_id or src.label}") as lk:
        if not lk.ok:
            return None
        return _ingest_profile(ctx, src, raw)


def _ingest_profile(ctx: Ctx, src, raw: bytes) -> dict | None:
    key = f"{src.thread_id or src.label}:profile:{hashlib.sha256(raw).hexdigest()[:16]}"
    if key in collected_keys(ctx):
        return None
    prof = json.loads(raw.decode("utf-8"))
    if (prof.get("provenance") or {}).get("confirmed_in_chat") is False:
        mark_collected(ctx, key, {"status": "PROFILE_REJECTED", "error": "画像未经用户确认"})
        return {"status": "PROFILE_REJECTED"}
    PT = _load("platform/profile_tool.py", "profile_tool_rcv")
    try:
        with KeyLock(ctx, "memory-write", wait_s=60):
            res = PT.apply_profile(ctx.cfg_path, prof, sync_memory=True)
    except ValueError as exc:
        mark_collected(ctx, key, {"status": "PROFILE_REJECTED", "error": str(exc)[:300]})
        status_write(ctx, profile_error=str(exc)[:300])
        return {"status": "PROFILE_REJECTED", "error": str(exc)}
    mark_collected(ctx, key, {"status": "PROFILE", **{k: res.get(k) for k in ("files", "memory")}})
    RL.review_notify(ctx.state_dir, "用户画像已更新", f"写入 {len(res.get('files', []))} 个画像文件；记忆 {res.get('memory')}", link=False)
    return res


# ----------------------------------------------------------------------------- discovery / polling
def check_thread(ctx: Ctx, sp, tid: str, agent_run_id: str | None = None, sandbox_id: str | None = None,
                 finished: bool = True, reply: bool | None = None, no_arxiv_check: bool = False) -> dict:
    """File first (session token → API key via /api/sandboxes), then the result block in the run's reply.

    reply (default = finished): look into the run's reply / replayed event stream when no file was found.
    Off while a run is still going (a live stream would block) and for conversations merely discovered in the
    dashboard list (those are read with the session token; replaying every recent chat would be wasteful).
    """
    res = _check_thread(ctx, sp, tid, agent_run_id, sandbox_id, finished, reply, no_arxiv_check)
    if res.get("status") == "NO_BUNDLE" and int(ctx.rc.get("auto_continue", 1)) >= 0:
        try:
            r2 = rescue_incomplete(ctx, sp, tid, agent_run_id, sandbox_id)
        except Exception as exc:  # noqa: BLE001
            r2 = {"rescue_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        if r2:
            res.update(r2)
    return res


def _check_thread(ctx: Ctx, sp, tid: str, agent_run_id: str | None = None, sandbox_id: str | None = None,
                  finished: bool = True, reply: bool | None = None, no_arxiv_check: bool = False) -> dict:
    reply = finished if reply is None else reply
    if f"thread-owned:{tid}" in collected_keys(ctx):  # run_local.py already ingested this conversation itself
        return {"status": "ALREADY", "key": f"thread-owned:{tid}"}
    src = ThreadSource(sp, tid, ctx.rc["out_dir"], sandbox_id=sandbox_id)
    origin = {"agent_run_id": agent_run_id, "no_arxiv_check": no_arxiv_check or getattr(ctx, "no_arxiv_check", False)}
    res = ingest(ctx, src, origin)
    if res["status"] == "NO_BUNDLE":
        rb = bundle_from_reply(sp, tid, agent_run_id, stream_timeout_s=int(ctx.rc.get("reply_stream_timeout_s", 90))) \
            if reply else None
        if rb is None and not reply and src.denied:
            res["note"] = "文件读不到（" + src.denied + "），等运行结束后从回复里取结果块"
        if rb:
            res = ingest(ctx, src, origin, reply_bytes=rb)
            res["bundle_from"] = "reply"
        if src.denied:
            res.setdefault("file_denied", src.denied)
    try:
        if ingest_profile(ctx, src) is None and src.read("profile.json") is None:
            rb = profile_from_reply(sp, tid, agent_run_id, stream=bool(agent_run_id and reply and src.denied)) if (reply or src.denied) else None
            if rb:
                ingest_profile(ctx, src, raw=rb)
    except Exception as exc:
        print("WARN profile:", str(exc)[:200])
    return res


# ----------------------------------------------------------------------------- v1.5.5 (P9): run ended without cards
FINISHED = ("completed", "failed", "stopped", "error", "cancelled", "canceled", "timeout", "succeeded")
CONTINUE_MSG = ("【自动续跑】上一轮在写卡片之前就结束了，没有产出（/workspace/inspiration_out/ 里没有 bundle.json）。"
                "不要重新加载技能、不要重新运行 prepare、不要解释计划。现在直接：\n"
                "1）论文卡：/workspace/inspiration_out/cards.json 还不存在的话，读 triage_shortlisted.json 后一次写成 cards.json"
                "（已存在就不要重写）；观察型卡：用文件写入工具逐张写成 cards/01.json、02.json …（已写好的不要重写，"
                "从缺的开始；证据见 obs_*.md）；\n"
                "2）运行 python3 /workspace/skills/insp-nightly/scripts/insp_run.py finalize；\n"
                "3）把 finalize 打印的结果块原样放进回答后结束。不要调用 ask，不要调用 complete。")


def latest_run(sp, tid: str, known: str | None = None) -> tuple[str | None, str]:
    """(agent_run_id, status) of the newest run in a conversation."""
    rid, status = known, ""
    if not rid:
        try:
            r = sp.thread_agent_runs(tid)
            items = r.get("agent_runs", r.get("runs", r.get("items", r.get("data", [])))) if isinstance(r, dict) else r
            items = [x for x in (items or []) if isinstance(x, dict)]
            if items:
                x = max(items, key=lambda i: str(i.get("created_at") or i.get("started_at") or ""))
                rid, status = x.get("id") or x.get("agent_run_id"), str(x.get("status") or "")
        except ApiError:
            return None, ""
    if rid and not status:
        try:
            g = sp.get_run(rid)
            status = str((g or {}).get("status") or (g or {}).get("agent_run", {}).get("status") or "") if isinstance(g, dict) else ""
        except ApiError:
            status = ""
    return rid, status.lower()


def rescue_incomplete(ctx: Ctx, sp, tid: str, agent_run_id: str | None = None, sandbox_id: str | None = None) -> dict | None:
    """A run that ran prepare (run_meta.json exists) but ended without bundle.json = it stopped before writing the cards
    (finish_reason=stop right after "让我写 cards.json"). The platform still calls that "completed".
    → one automatic follow-up in the same conversation (receiver.auto_continue, default 1); still nothing → NO_OUTPUT
    on the review page + notification, instead of silence."""
    src = ThreadSource(sp, tid, ctx.rc["out_dir"], sandbox_id=sandbox_id)
    if src.read("run_meta.json") is None or src.read("bundle.json") is not None:
        return None
    rid, status = latest_run(sp, tid, agent_run_id)
    if not rid:
        return None
    if status not in FINISHED:
        return {"running": True}
    led_p = ctx.inbox / "rescue.json"
    try:
        led = json.loads(led_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        led = {}
    ent = led.setdefault(tid, {"attempts": 0, "handled": []})
    if rid in ent["handled"]:
        return {"status": ent.get("last", "NO_BUNDLE")}
    ent["handled"].append(rid)
    from sciencepal_client import web_thread_url
    pid = None
    try:
        pid = (ctx.setup_client() or sp).project_of_thread(tid)
    except Exception:  # noqa: BLE001
        pid = None
    url = web_thread_url(ctx.cfg, tid, pid)
    cards_written = 0
    try:
        meta = json.loads(src.read("run_meta.json") or b"{}")
    except json.JSONDecodeError:
        meta = {}
    if ent["attempts"] < int(ctx.rc.get("auto_continue", 1)):
        agent_id = ctx.ids().get("agent_id")
        mid = sp.add_message(tid, CONTINUE_MSG)
        resp = sp.start_thread_agent(tid, agent_id, mid)
        new_rid = (resp or {}).get("agent_run_id") or (resp or {}).get("id") if isinstance(resp, dict) else None
        ent["attempts"] += 1
        ent.update(last="CONTINUED", continued_at=now().isoformat(timespec="seconds"), continued_run=new_rid)
        led_p.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
        if new_rid:
            save_pending(ctx, pending_runs(ctx) + [{"thread_id": tid, "agent_run_id": new_rid, "sandbox_id": sandbox_id,
                                                    "t": time.time(), "started_at": now().isoformat(timespec="seconds"),
                                                    "kind": "auto_continue"}])
        info = {"status": "CONTINUED", "at": ent["continued_at"], "thread_id": tid, "url": url, "from_run": rid, "run": new_rid}
        status_write(ctx, rescue=info)
        print(f"自动续跑：会话 {tid[:8]} 的运行 {rid[:8]} 结束了但没有写出卡片 → 已在同一会话续跑一次（{str(new_rid)[:8]}）")
        return {"status": "CONTINUED", "run": new_rid}
    ent["last"] = "NO_OUTPUT"
    led_p.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
    c = meta.get("counts") or {}
    row = {"run_id": "rcv-" + now().strftime("%Y%m%d-%H%M%S"), "status": "NO_OUTPUT", "thread_id": tid, "agent_run_id": rid,
           "sandbox_run_id": meta.get("run_id"), "counts": c, "started_at": meta.get("started_at"),
           "finished_at": now().isoformat(timespec="seconds"),
           "problem": f"运行结束了但没有写出卡片（已自动续跑 {ent['attempts']} 次）；候选 {c.get('shortlisted', '?')} 篇论文、"
                      "本地观察都没有被消耗（没有入库就不算推送过），重跑会重新捡起"}
    with (ctx.state_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    info = {"status": "NO_OUTPUT", "at": row["finished_at"], "thread_id": tid, "url": url, "from_run": rid,
            "shortlisted": c.get("shortlisted"), "cards_written": cards_written}
    status_write(ctx, rescue=info)
    RL.review_notify(ctx.state_dir, "灵感运行没有产出卡片",
                     f"模型在写卡片之前就结束了（已自动续跑 {ent['attempts']} 次）。候选没有被消耗，点“主动探索灵感”重跑即可", link=True)
    print(f"NO_OUTPUT：会话 {tid[:8]} 自动续跑后仍没有卡片，已在评价页提示可重跑")
    return {"status": "NO_OUTPUT"}


def _ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def poll_once(ctx: Ctx, sp=None) -> list[dict]:
    sp = sp or ctx.reader()
    if sp is None:
        status_write(ctx, watching=False, error="没有 API Key（~/.sciencepal/run_token 或 SCIENCEPAL_RUN_TOKEN）")
        return []
    results = []
    cur_p = ctx.inbox / "receiver_cursor.json"
    try:
        cur = json.loads(cur_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cur = {"checked": {}}
    # 1. runs started from the review page / --run (known thread ids)
    keep = []
    for p in pending_runs(ctx):
        st = ""
        try:
            r = sp.get_run(p["agent_run_id"]) if p.get("agent_run_id") else None
            st = json.dumps(r)[:4000].lower() if r else ""
        except ApiError:
            pass
        finished = any(f'"status": "{x}"' in st for x in ("completed", "failed", "stopped", "error", "cancelled"))
        res = check_thread(ctx, sp, p["thread_id"], p.get("agent_run_id"), sandbox_id=p.get("sandbox_id"), finished=finished)
        if res["status"] in ("NO_BUNDLE",) and not finished and time.time() - p.get("t", 0) < 3600:
            keep.append(p)
        if res.get("status") == "CONTINUED":   # the follow-up run was appended to pending by rescue_incomplete
            keep += [x for x in pending_runs(ctx) if x.get("agent_run_id") == res.get("run")]
        results.append({"thread_id": p["thread_id"], **res})
    save_pending(ctx, keep)
    # 2. conversations started in the web UI (@灵感官 …)
    try:
        threads = sp.dashboard_threads()
    except ApiError as exc:
        threads = None
        hint = "（没有会话令牌：把它放进 ~/.sciencepal/session_token 就能自动发现网页会话）" if not getattr(sp, "has", lambda _: False)("session") else ""
        status_write(ctx, discovery="pinned_only", discovery_error=f"HTTP {exc.status}：网页里发起的会话需要用 --thread <链接> 收货{hint}")
        for tid in [ctx.ids().get("insp_thread_id")] if ctx.ids().get("insp_thread_id") else []:
            results.append({"thread_id": tid, **check_thread(ctx, sp, tid)})
    if threads is not None:
        horizon = now() - timedelta(hours=float(ctx.rc["lookback_h"]))
        fresh = []
        for t in threads:
            tid = t.get("thread_id")
            upd = t.get("last_message_at") or t.get("updated_at")
            ts = _ts(upd)
            if not tid or (ts and ts < horizon) or cur["checked"].get(tid) == upd:
                continue
            fresh.append((ts or now(), tid, upd))
        fresh.sort(reverse=True)
        for _, tid, upd in fresh[: int(ctx.rc["max_threads_per_poll"])]:
            try:
                res = check_thread(ctx, sp, tid, reply=False)
            except ApiError as exc:  # one odd conversation must not stop the others
                res = {"status": "ERROR", "error": str(exc)[:200]}
            if res["status"] != "ERROR" and not res.get("running"):   # v1.5.5: re-check until the run has ended
                cur["checked"][tid] = upd
            if res["status"] != "NO_BUNDLE":
                results.append({"thread_id": tid, **res})
        status_write(ctx, discovery="dashboard", discovery_error=None)
    cur["last_poll"] = now().isoformat(timespec="seconds")
    cur_p.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    status_write(ctx, last_poll=cur["last_poll"], watching=True, error=None,
                 pending=len(keep), last_found=[r for r in results if r.get("status") not in ("NO_BUNDLE", "ALREADY")][-3:])
    return results


def watch_step(ctx: Ctx) -> list[dict]:
    key = ctx.client()
    sp = ctx.reader()
    if sp is None:
        status_write(ctx, watching=False, error="没有 API Key（~/.sciencepal/run_token 或 SCIENCEPAL_RUN_TOKEN）")
        return []
    status_write(ctx, credentials={"session_token": sp.has("session"), "api_key": sp.has("api_key")})
    if key is not None and not getattr(ctx, "_account_checked", False):
        problem = check_account(key, ctx.ids().get("agent_id"))
        ctx._account_checked = True
        if problem:
            status_write(ctx, account_problem=problem)
            print("ERROR:", problem)
    if ctx.rc.get("memory_digest", True):
        try:  # no-op unless arxiv_fetch_mode changed since the last sync
            settings_memory(ctx)
        except Exception as exc:
            print("WARN [灵感设置] 记忆未更新：", str(exc)[:200])
    out = poll_once(ctx, sp)
    if time.time() - _PUBLISH["last_try"] > _publish_every(ctx):   # v1.5.5 (P11): first step + every 30 min
        publish_snapshot(ctx, scan=True, reason="periodic")
    for r in out:
        if r.get("status") not in ("NO_BUNDLE", "ALREADY"):
            print(now().strftime("%H:%M:%S"), "收货", json.dumps(r, ensure_ascii=False), flush=True)
    return out


def _publish_every(ctx: Ctx) -> float:
    try:
        return float(_load("platform/publish_state.py", "publish_state_rc").every_s(ctx.cfg)) \
            if "publish_state_rc" not in sys.modules else float(sys.modules["publish_state_rc"].every_s(ctx.cfg))
    except Exception:  # noqa: BLE001
        return 1800.0


def next_wait(ctx: Ctx) -> float:
    return float(ctx.rc["poll_s"]) if not pending_runs(ctx) else min(20.0, float(ctx.rc["poll_s"]))


def watch(ctx: Ctx, stop=None) -> None:
    while not (stop and stop.is_set()):
        try:
            watch_step(ctx)
        except Exception as exc:  # the watcher must survive network blips
            status_write(ctx, error=f"{type(exc).__name__}: {str(exc)[:200]}")
        if stop:
            stop.wait(next_wait(ctx))
        else:
            time.sleep(next_wait(ctx))


# ----------------------------------------------------------------------------- trigger (manual only)
def observe_before_run(ctx: Ctx) -> dict | None:
    """v1.5: refresh observed.jsonl (local code / logs / notes / papers / usage) right before the state is uploaded.
    Opt-in: only when config.json has an "observe" section (copied from config.example.json). Never blocks a run."""
    oc = ctx.cfg.get("observe")
    if not isinstance(oc, dict) or not oc.get("enabled", True) or not oc.get("auto_scan_before_run", True):
        return None
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import observe as _observe
        from observers.common import Env as _ObsEnv
        try:
            sc = ctx.setup_client()
        except Exception:  # noqa: BLE001 - no session token: conversation titles are simply skipped
            sc = None
        rep = _observe.run_scan(_ObsEnv(ctx.cfg_path, cfg=ctx.cfg, base=ctx.base), quiet=True, client=sc,
                                platform=sc is not None)
        o = rep.get("observed", {})
        print(f"本地观察：observed.jsonl {o.get('total', 0)} 条（新 {o.get('new', 0)}）· {o.get('by_type', {})}")
        return rep
    except Exception as exc:  # noqa: BLE001 - a broken observer must never stop the paid run
        print(f"本地观察跳过（不影响本次运行）：{type(exc).__name__}: {str(exc)[:200]}")
        return None


def stage_state(ctx: Ctx, run_dir: Path) -> list[Path]:
    att = run_dir / "attach"
    att.mkdir(parents=True, exist_ok=True)
    out = []
    for rel in state_files_for(ctx):
        p = ctx.state_dir / rel
        if p.is_file():
            dst = att / Path(rel).name
            dst.write_bytes(p.read_bytes() or b"\n")
            out.append(dst)
    return out


def current_fulltext_max(ctx: Ctx) -> int:
    """fulltext.max_candidates from config.json (default 4; v1.4.3, feedback5 · G8)."""
    try:
        cfg = json.loads(Path(ctx.cfg_path).read_text(encoding="utf-8")) if getattr(ctx, "cfg_path", None) else ctx.cfg
    except (OSError, json.JSONDecodeError):
        cfg = ctx.cfg
    try:
        return max(1, int(((cfg or {}).get("fulltext") or {}).get("max_candidates", RL.FT.DEFAULTS["max_candidates"])))
    except (TypeError, ValueError):
        return int(RL.FT.DEFAULTS["max_candidates"])


def run_prompt(ctx: Ctx) -> str:
    mode = current_fetch_mode(ctx)[0]
    how = ("用 prepare 打印的 python3 /workspace/fq <ID> 找原文引文写卡片（一次一条短命令，禁止 && 串多个 grep）"
           if mode == "fulltext" else "只读摘要写卡片（一次读完 triage_shortlisted.json）")
    extra = f" --fulltext-max {current_fulltext_max(ctx)}" if mode == "fulltext" else ""
    return (f"{ctx.rc['run_prompt']}。\n（由评价页触发：状态文件已作为附件挂在 /workspace/_refs/。按 insp-nightly 的自举分支执行："
            f"先加载 insp-nightly 与 insp-card，运行 insp_run.py prepare --fetch-mode {mode}{extra}（论文读取模式按本地 config.json，"
            f"以这里为准，不要改），{how}，论文卡一次写成 /workspace/inspiration_out/cards.json（按 prepare 打印的 REMAINING 名额）；"
            "有观察型候选（OBSERVED）时再一张卡一个文件写 cards/01.json、02.json …；说了要写就必须真的调用文件写入工具。"
            "再运行 finalize（超配额的卡会自动移进 rejected，不是错误；有错只改它点名的那张，不要的卡用 finalize --drop <卡片 id>），"
            "最后原样输出 INSP_BUNDLE_JSON 结果块后直接结束：不要调用 complete 工具，不要放附件（结果块是正文）。禁止调用 ask。）")


def trigger_run(ctx: Ctx, force: bool = False) -> dict:
    sp = ctx.client()
    if sp is None:
        raise SystemExit("没有 API Key：把 sp_live_ 开头的 Key 写进 ~/.sciencepal/run_token")
    gap = float(ctx.rc.get("min_trigger_gap_s", 600))
    recent = [p for p in pending_runs(ctx) if time.time() - float(p.get("t", 0)) < gap]
    if recent and not force:  # feedback4: two clicks 22 s apart started two paid runs
        raise SystemExit(f"{int(gap // 60)} 分钟内已经触发过一次（会话 {str(recent[-1].get('thread_id'))[:8]}），还没收到结果。"
                         "等它结束再跑；确实要再跑一次就用 receiver.py --run --force")
    agent_id = ctx.ids().get("agent_id")
    problem = check_account(sp, agent_id)
    if problem:
        raise SystemExit("ERROR: " + problem)
    run_dir = (ctx.base / ctx.cfg.get("work_dir", "runs") / ("trigger-" + now().strftime("%Y%m%d-%H%M%S"))).resolve()
    observe_before_run(ctx)
    files = stage_state(ctx, run_dir)
    prompt = run_prompt(ctx)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")  # feedback5 3.5: keep what the agent was told
    resp = sp.initiate(prompt, agent_id=agent_id, files=files, model_name=ctx.cfg.get("model_name"),
                       web_search_on=False)
    row = {"thread_id": resp.get("thread_id"), "agent_run_id": resp.get("agent_run_id"), "sandbox_id": resp.get("sandbox_id"),
           "t": time.time(), "started_at": now().isoformat(timespec="seconds"), "fetch_mode": current_fetch_mode(ctx)[0]}
    save_pending(ctx, pending_runs(ctx) + [row])
    with (ctx.inbox / "triggered.jsonl").open("a", encoding="utf-8") as f:  # ids for later --thread / --push-state
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    pid = None
    if row["thread_id"]:   # v1.5.4 (S6): /thread/<id> is a 404 page; the web page is /projects/<project_id>/thread/<id>
        try:
            pid = sp.project_of_thread(row["thread_id"], resp)
        except Exception:  # noqa: BLE001
            pid = None
        if not pid:   # v1.5.5 (P5/P6): the API key cannot read the conversation list — ask with the session token
            try:
                ss = ctx.setup_client()
                pid = ss.project_of_thread(row["thread_id"]) if ss else None
            except Exception:  # noqa: BLE001
                pid = None
    row["project_id"] = pid
    from sciencepal_client import web_thread_url
    url = web_thread_url(ctx.cfg, row["thread_id"], pid)
    status_write(ctx, last_trigger={**row, "url": url})
    mode = row["fetch_mode"]
    print(f"已触发（论文读取模式 {mode}）：thread {row['thread_id']} · run {row['agent_run_id']}。"
          + ("大约 5–15 分钟后卡片会自动出现在评价页。" if mode == "abstract" else "全文模式较慢，大约 20–60 分钟后卡片会出现在评价页。"))
    return {**row, "url": url}


def spawn_local_fallback(ctx: Ctx) -> int | None:
    """Sandbox has no network → classic route: fetch + full text locally, upload automatically (still zero manual)."""
    log = (ctx.base / ctx.cfg.get("work_dir", "runs"))
    log.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-X", "utf8", str(HERE / "run_local.py"), "--config", str(ctx.cfg_path), "--fetch-arxiv"]
    kw: dict = {"stdout": (log / "fallback.log").open("a", encoding="utf-8"), "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL, "cwd": str(PKG)}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kw["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kw).pid
    except OSError as exc:
        print("WARN fallback:", exc)
        return None


# ----------------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="灵感收货员：自动收回灵感官的运行结果")
    ap.add_argument("--config", type=Path, required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--watch", action="store_true")
    g.add_argument("--thread")
    g.add_argument("--run", action="store_true")
    g.add_argument("--from-dir", type=Path)
    g.add_argument("--push-state", metavar="THREAD")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--force", action="store_true", help="配合 --run：即使刚触发过也再跑一次")
    ap.add_argument("--run-id", help="配合 --thread：这次运行的 agent_run_id（没有会话令牌时用它从回复里取结果块）")
    ap.add_argument("--sandbox-id", help="配合 --thread / --push-state：会话沙箱 ID（没有会话令牌时用它读写沙箱文件）")
    g.add_argument("--sync-settings", action="store_true", help="把 config.json 的 arxiv_fetch_mode 立即写进平台记忆 [灵感设置]")
    ap.add_argument("--no-arxiv-check", action="store_true")
    a = ap.parse_args()
    ctx = Ctx(a.config)
    if a.status:
        p = ctx.inbox / "receiver_status.json"
        print(p.read_text(encoding="utf-8") if p.exists() else "{}")
        return 0
    if a.from_dir:
        res = ingest(ctx, DirSource(a.from_dir), {"no_arxiv_check": a.no_arxiv_check})
        ingest_profile(ctx, DirSource(a.from_dir))
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["status"] in ("OK", "NO_CANDIDATES", "ALREADY") else 5
    if a.sync_settings:
        mode, warn = current_fetch_mode(ctx)
        if warn:
            print("WARN", warn)
        t = settings_memory(ctx, force=True)
        print(t or "没有会话令牌：记忆没写入（网页里直接 @灵感官 时会按默认 abstract；评价页按钮触发不受影响）")
        return 0 if t else 5
    ctx.no_arxiv_check = a.no_arxiv_check
    if a.run:
        trigger_run(ctx, force=a.force)
        return 0
    sp = ctx.reader()
    if sp is None:
        print("ERROR: 没有令牌：API Key 放 ~/.sciencepal/run_token，会话令牌放 ~/.sciencepal/session_token")
        return 5
    print("凭据：" + " + ".join({"session": "会话令牌", "api_key": "API Key"}[x] for x in sp.labels)
          + ("" if sp.has("session") else "（没有会话令牌：会话文件读不到时改用沙箱接口或回复里的结果块）"))
    if a.push_state:
        tid = thread_id_of(a.push_state)
        print("同步文件数：", push_state(ctx, sp, tid, sandbox_id=a.sandbox_id or sandbox_of(ctx, tid)))
        return 0
    if a.thread:
        tid = thread_id_of(a.thread)
        res = check_thread(ctx, sp, tid, agent_run_id=a.run_id or run_of(ctx, tid), sandbox_id=a.sandbox_id or sandbox_of(ctx, tid),
                            no_arxiv_check=a.no_arxiv_check)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["status"] in ("OK", "NO_CANDIDATES", "ALREADY") else 5
    if a.once:
        for r in poll_once(ctx, sp):
            print(json.dumps(r, ensure_ascii=False))
        return 0
    print(f"收货员已启动：每 {ctx.rc['poll_s']} 秒查看一次最近的会话（只读，不会发起任何运行）。Ctrl+C 退出。")
    watch(ctx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
