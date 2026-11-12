#!/usr/bin/env python3
"""Local card-review server: pop-up cards, flip, classify (1-6), edit, undo.

Python standard library only. Binds 127.0.0.1. Writes go through insp-feedback/scripts/record_verdict.py,
so verdicts.jsonl / negative.jsonl / cards.json keep exactly the same contract as the CLI and Excel paths.

    python platform/review_server.py --config config_exp.json --open      # start (or reuse) and open the browser
    python platform/review_server.py --config config_exp.json --url       # print the review URL only
    python platform/review_server.py --config config_exp.json --stop
    python platform/review_server.py --self-test

Security: listens on 127.0.0.1 only, checks the Host header (DNS rebinding) and a per-user token kept in
~/.sciencepal/review_token (stable across restarts, so URLs in older Windows notifications keep working).
Implicit signals (flipped / dwell / position) go to implicit.jsonl and are never used to change weights.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sciencepal_client import web_thread_url  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UI_FILE = Path(__file__).with_name("review_ui.html")
DEFAULT_PORT = 8765
TOKEN_FILE = Path.home() / ".sciencepal" / "review_token"
VERSION = "review/1.0.0"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VERDICT = _load("insp-feedback/scripts/record_verdict.py", "verdict_rs")
OUTCOME = _load("insp-feedback/scripts/record_outcome.py", "outcome_rs")
DRYRUN = _load("platform/dryrun_template.py", "dryrun_rs")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_token() -> str:
    try:
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(tok) >= 16:
            return tok
    except OSError:
        pass
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


def resolve(cfg_path: Path | None, state_dir: Path | None, port: int | None) -> tuple[Path, int]:
    cfg = {}
    if cfg_path:
        cfg_path = cfg_path if cfg_path.is_absolute() else (Path.cwd() / cfg_path)
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if state_dir is None:
        sd = cfg.get("state_dir", "./inspiration")
        state_dir = Path(sd) if Path(sd).is_absolute() else ((cfg_path.parent if cfg_path else Path.cwd()) / sd)
    port = port or int((cfg.get("review") or {}).get("port", DEFAULT_PORT))
    return state_dir.resolve(), port


# ----------------------------------------------------------------------------- data
PUBLIC = ("id", "type", "subfield", "created_at", "title", "one_line", "why_me", "hypothesis", "minimal_check",
          "expected_observable", "falsifier", "magnitude", "cost_estimate", "source", "status", "verdict", "_edited",
          "action_brief", "numbers_provenance")


CANARY = "◆"


def strip_canary(s):
    return s.rstrip().rstrip(CANARY).rstrip() if isinstance(s, str) else s


def raw_view_title(state_dir: Path, cid: str):
    for f in sorted((state_dir / "cards").glob("*/cards.json")):
        for c in json.loads(f.read_text(encoding="utf-8")).get("cards", []):
            if c.get("id") == cid:
                return (c.get("_edited") or {}).get("title", c.get("title"))
    return None


def load_cards(state_dir: Path, scope: str = "pending") -> dict:
    rows = VERDICT.read_verdicts(state_dir)
    stacks = VERDICT.verdict_stacks(rows)
    judged = VERDICT.judged_ids(state_dir)
    outcomes = OUTCOME.latest_by_card(state_dir)
    out = []
    for f in sorted((state_dir / "cards").glob("*/cards.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        for c in doc.get("cards", []):
            if scope == "pending" and c.get("id") in judged:
                continue
            item = {k: c.get(k) for k in PUBLIC}
            item["title"] = strip_canary(item["title"])  # cards made before v1.2.1 still carry the canary
            if isinstance(item.get("_edited"), dict) and "title" in item["_edited"]:
                item["_edited"] = {**item["_edited"], "title": strip_canary(item["_edited"]["title"])}
            item["date"] = doc.get("date") or f.parent.name
            item["outcome"] = outcomes.get(c.get("id"))
            st = stacks.get(c.get("id")) or []
            item["latest_action"] = st[-1]["action"] if st else None
            item["asked"] = any(v.get("action") == "ask_detail" for v in st)
            cls = [v for v in st if v.get("action") not in ("edited", "ask_detail")]   # v1.5.1 history view
            item["classified_action"] = cls[-1]["action"] if cls else None
            item["verdict_ts"] = (cls[-1] if cls else st[-1])["ts"] if st else None
            item["verdict_reason"] = cls[-1].get("reason", "") if cls else ""
            item["edit_count"] = sum(1 for v in st if v.get("action") == "edited")
            out.append(item)
    total = sum(len(json.loads(f.read_text(encoding="utf-8")).get("cards", []))
                for f in (state_dir / "cards").glob("*/cards.json"))
    actions = [{"key": k, "label": VERDICT.ACTIONS[k]["label"]}
               for k in ("adopt", "reject", "done", "not_important", "ask_detail", "start_now")]
    return {"cards": out, "total": total, "pending": total - len(judged), "actions": actions,
            "editable": list(VERDICT.EDITABLE_FIELDS), "state_dir": state_dir.name}


def log_implicit(state_dir: Path, card_id: str, event: str, data: dict | None) -> None:
    d = data or {}
    row = {"ts": now_iso(), "card_id": card_id, "event": event,
           "flipped": bool(d.get("flipped")), "dwell_ms": int(d.get("dwell_ms") or 0),
           "position": d.get("position"), "queue_size": d.get("queue_size"), "session": str(d.get("session", ""))[:40]}
    with (state_dir / "implicit.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------------------- http
class App:
    def __init__(self, state_dir: Path, port: int, token: str, idle_min: float):
        self.state_dir, self.port, self.token, self.idle_min = state_dir, port, token, idle_min
        self.lock = threading.Lock()
        self.last = time.time()
        self.rcv = None        # receiver module (v1.4), loaded when serve() gets a config
        self.rcv_ctx = None
        self.rcv_busy = threading.Lock()
        self.cfg_path: Path | None = None   # v1.5.1: needed to create “立刻动手” tasks (run_action.dispatch)
        self.ra = None                      # run_action module, loaded lazily
        self.aq = None                      # v1.5.3 ask_detail module, loaded lazily
        self._pid_cache: dict[str, str] = {}

    def config(self) -> dict:
        try:
            return json.loads(self.cfg_path.read_text(encoding="utf-8-sig")) if self.cfg_path else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def thread_page(self, thread_id: str) -> str:
        """v1.5.4 (S6): /go/thread/<id> → https://…/projects/<project_id>/thread/<id>. The project is looked up once
        (dashboard list, session token first, API key second) and cached in inbox/thread_projects.json."""
        cfg = self.config()
        base = str(cfg.get("base_url") or "https://sciencepal.ai").rstrip("/")
        if not re.fullmatch(r"[0-9A-Za-z-]{6,80}", thread_id or ""):
            return base
        cache_f = self.state_dir / "inbox" / "thread_projects.json"
        try:
            cache = json.loads(cache_f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
        pid = cache.get(thread_id)
        if not pid and cfg:
            clients = []
            try:
                ss = _load("platform/profile_tool.py", "pt_go").session_client(cfg)
                if ss:
                    clients.append(ss)
                rn = _load("platform/run_local.py", "rn_go")
                tok = rn.run_token(cfg)
                if tok:
                    from sciencepal_client import SciencePal
                    clients.append(SciencePal(cfg["base_url"], tok, cfg.get("auth_header", "Authorization"), cfg.get("auth_scheme", "Bearer")))
            except Exception:  # noqa: BLE001
                pass
            for c in clients:
                pid = c.project_of_thread(thread_id)
                if pid:
                    break
            if pid:
                cache[thread_id] = pid
                try:
                    cache_f.parent.mkdir(exist_ok=True)
                    cache_f.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
                except OSError:
                    pass
        return f"{base}/projects/{pid}/thread/{thread_id}" if pid else f"{base}/dashboard"


# ----------------------------------------------------------------------------- v1.5.1 立刻动手：动手单 + 建任务 + 回执
def _ra(app: "App"):
    if app.cfg_path is None:
        raise LookupError("评价页不是用 --config 启动的，不能建任务（只记录了评价）。用 review_server.py --config config.json --open 重开")
    if app.ra is None:
        app.ra = _load("platform/run_action.py", "run_action_rs")
        app.ra.VERDICT.set_config(app.cfg_path)
    return app.ra


def start_now_mode(app: "App") -> str:
    """v1.5.6: form (default) = 按 6 总是先弹出预填好的动手单（场景/参数/方法/产出/对照/判据/交付），你确认或修改后才开跑；
    the old values auto / confirm both mean form now (the 1.5.1 page behaviour you asked back).
    direct = 动手单完整就直接建任务（旧 auto）；off = 只记录评价。"""
    try:
        cfg = json.loads(app.cfg_path.read_text(encoding="utf-8-sig")) if app.cfg_path else {}
    except (OSError, json.JSONDecodeError):
        cfg = {}
    m = str((cfg.get("actions") or {}).get("start_now_mode", "form")).lower()
    return {"auto": "form", "confirm": "form"}.get(m, m) if m in ("form", "auto", "confirm", "direct", "off") else "form"


def after_start_now(app: "App", card_id: str, verdict_ts: str | None) -> dict:
    """Called right after a start_now verdict is recorded (K1).
    form: return the pre-filled brief → the page opens the 动手单 form → 确认开跑 POSTs /api/action/start → task.
    direct: build the brief and create the task at once (asks only when values are missing); off: record only."""
    mode = start_now_mode(app)
    if mode == "off":
        return {"mode": mode, "message": "actions.start_now_mode=off：只记录了评价，没有建任务"}
    try:
        ra = _ra(app)
        if mode == "form":
            if not (ra.load_cfg(app.cfg_path)[2] or {}).get("enabled", True):
                return {"mode": mode, "message": "actions.enabled=false：只记录了评价，没有建任务"}
            old = [j for j in ra.list_jobs(app.state_dir) if j["card_id"] == card_id and j["status"] != "cancelled"]
            if old:   # the card already has a task → show it instead of a second form
                return {"mode": mode, **ra.dispatch(app.cfg_path, card_id, via="card_ui", verdict_ts=verdict_ts, run=False)}
            b = ra.make_brief(app.state_dir, card_id)
            need = list(b.get("problems") or []) + [f"填写 {m}" for m in (b.get("missing") or [])]
            return {"mode": mode, "needs_input": True, "brief": b,
                    "message": ("动手单还需要你补充：" + "；".join(need)) if need else "请确认动手单后开跑"}
        return {"mode": mode, **ra.dispatch(app.cfg_path, card_id, via="card_ui", verdict_ts=verdict_ts)}
    except (LookupError, ValueError) as exc:
        return {"mode": mode, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the verdict is already saved; never lose it because of the task
        return {"mode": mode, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def _aq(app: "App"):
    """v1.5.3: 追问细节 → platform/ask_detail.py (same background pattern as 立刻动手)."""
    if app.cfg_path is None:
        raise LookupError("评价页不是用 --config 启动的，不能向灵感官追问（只记录了评价）。用 review_server.py --config config.json --open 重开")
    if getattr(app, "aq", None) is None:
        app.aq = _load("platform/ask_detail.py", "ask_detail_rs")
    return app.aq


def after_ask(app: "App", card_id: str, verdict_ts: str | None) -> dict:
    try:
        return _aq(app).dispatch(app.cfg_path, card_id, via="card_ui", verdict_ts=verdict_ts)
    except (LookupError, ValueError) as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the verdict is already saved
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}


HIDDEN_DEFAULT = ["done", "not_important"]   # v1.5.3: buttons hidden on the page only; the API still accepts them


def hidden_actions(app: "App") -> list[str]:
    try:
        cfg = json.loads(app.cfg_path.read_text(encoding="utf-8-sig")) if app.cfg_path else {}
    except (OSError, json.JSONDecodeError):
        cfg = {}
    h = (cfg.get("review") or {}).get("hidden_actions", HIDDEN_DEFAULT)
    return [str(x) for x in h] if isinstance(h, list) else list(HIDDEN_DEFAULT)


UI_WORDS = {"收货员": "自动同步"}   # v1.5.2: internal component names never reach the page / toasts


def ui_text(s: str) -> str:
    for a, b in UI_WORDS.items():
        s = s.replace(a, b)
    return s


def scrub_paths(obj, app: "App"):
    """v1.5.2: the page must never show local absolute paths (C:\\Users\\<name>\\...). Replace the package root,
    the state dir and the home dir; any other absolute path is cut down to its last two parts."""
    roots = []
    try:
        sd = app.state_dir.resolve()
        roots = [(str(sd), sd.name), (str(sd.parent), "."), (str(Path.home()), "~")]
    except OSError:
        pass
    # v1.5.3: (?<![\w.]) keeps URLs intact — "https://…" used to match "s:/" and the 打开会话 links got cut
    pat = re.compile(r"(?<![\w.])(?:[A-Za-z]:[\\/]|/(?:home|Users|mnt|root|tmp)/)[^\s\"'<>|]*")

    def fix(s: str) -> str:
        for a, b in roots:
            if a and len(a) > 3:
                s = s.replace(a, b).replace(a.replace("\\", "/"), b)
        return pat.sub(lambda m: "…/" + "/".join(re.split(r"[\\/]+", m.group(0).rstrip("\\/"))[-2:]), s)

    if isinstance(obj, str):
        return fix(obj)
    if isinstance(obj, list):
        return [scrub_paths(x, app) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_paths(v, app) for k, v in obj.items()}
    return obj


def open_local(path: Path, select: bool) -> str:
    """Open the receipt (or reveal it in its folder) on THIS computer. INSP_NO_OPEN=1 → do nothing (tests)."""
    if os.environ.get("INSP_NO_OPEN"):
        return "skipped"
    if os.name == "nt":
        if select:
            subprocess.Popen(["explorer", "/select,", str(path)], creationflags=0x08000000)
        else:
            os.startfile(str(path))  # noqa: S606 - local file of our own state dir
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if select else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if select else path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "opened"


def jobs_view(app: "App", limit: int = 30) -> dict:
    if app.cfg_path is None:
        return {"enabled": False, "jobs": [], "message": "评价页不是用 --config 启动的：没有任务回执"}
    ra = _ra(app)
    out = []
    try:
        base = str(json.loads(app.cfg_path.read_text(encoding="utf-8")).get("base_url") or "https://sciencepal.ai").rstrip("/")
    except (OSError, json.JSONDecodeError):
        base = "https://sciencepal.ai"
    jobs = ra.list_jobs(app.state_dir)
    for j in sorted(jobs, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]:
        last = (j.get("runs") or [{}])[-1]
        r = j.get("receipt") or {}
        out.append({k: j.get(k) for k in ("job_id", "card_id", "card_title", "card_type", "tier", "tier_label", "tier_reasons",
                                          "phase", "status", "created_at", "started_at", "finished_at", "error", "ack", "brief",
                                          "local_files", "notes")}
                   | {"thread_id": last.get("thread_id"), "receipt": r,
                      "thread_url": web_thread_url(app.config(), last.get("thread_id"),
                                                   last.get("project_id") or next((x.get("project_id") for x in j.get("runs") or []
                                                                                   if x.get("thread_id") == last.get("thread_id") and x.get("project_id")), None)),
                      "checks": _receipt_checks(ra.job_dir(app.state_dir, j["job_id"]) / "receipt.json"),
                      "has_receipt": (ra.job_dir(app.state_dir, j["job_id"]) / "receipt.md").is_file(),
                      "cancel_text": ra.cancel_text(j), "cancelled_at": j.get("cancelled_at"),
                      "awaiting": "approve" if j.get("status") == "needs_approval" else
                                  ("ack" if j.get("status") in ra.TERMINAL and not j.get("ack") and j.get("status") != "cancelled" else None)})
    # cards whose latest verdict is “立刻动手” but no task exists yet (the brief still needs your values)
    have = {j["card_id"] for j in jobs if j.get("status") != "cancelled"}
    titles = {c["id"]: (c.get("_edited") or {}).get("title") or c.get("title") for c in load_cards(app.state_dir, "all")["cards"]}
    stacks = VERDICT.verdict_stacks(VERDICT.read_verdicts(app.state_dir))
    pend = [{"card_id": cid, "title": titles.get(cid, cid), "ts": st[-1]["ts"]} for cid, st in stacks.items()
            if st[-1].get("action") == "start_now" and cid not in have]
    try:   # v1.5.3: 追问细节 conversations
        asks = _aq(app).asks_view(json.loads(app.cfg_path.read_text(encoding="utf-8-sig")), app.state_dir)
    except Exception:  # noqa: BLE001
        asks = []
    return scrub_paths({"enabled": True, "jobs": out, "asks": asks, "pending_briefs": sorted(pend, key=lambda x: x["ts"], reverse=True),
            "running": sum(1 for j in out if j["status"] in ("queued", "running", "planning", "approved")),
            "awaiting": sum(1 for j in out if j["awaiting"]) + len(pend)}, app)


def _receipt_checks(p: Path) -> list:
    try:
        return [{"name": x.get("name"), "ok": bool(x.get("ok")), "detail": x.get("detail", "")}
                for x in json.loads(p.read_text(encoding="utf-8")).get("checks") or []]
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def receiver_status(app: "App") -> dict:
    p = app.state_dir / "inbox" / "receiver_status.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        st = {}
    total = 0
    for f in (app.state_dir / "cards").glob("*/cards.json"):
        try:
            total += len(json.loads(f.read_text(encoding="utf-8")).get("cards", []))
        except (OSError, json.JSONDecodeError):
            pass
    out = {"enabled": app.rcv_ctx is not None, "total_cards": total, **st}
    try:
        out["a_status"] = a_status(app.state_dir)
    except Exception:  # noqa: BLE001
        pass
    return out


def a_status(state_dir: Path) -> dict:
    """v1.5.5 (P10): why there is (no) A-class card — the local observation ledger in one line."""
    def jl(p: Path) -> list[dict]:
        out = []
        for ln in (p.read_text(encoding="utf-8").splitlines() if p.is_file() else []):
            try:
                o = json.loads(ln)
                if isinstance(o, dict):
                    out.append(o)
            except json.JSONDecodeError:
                pass
        return out
    rows = jl(state_dir / "observed.jsonl")
    try:
        st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        st = {}
    pushed = {str(p.get("id")) for p in st.get("pushed") or [] if isinstance(p, dict)}
    neg = {str(n.get("value")) for n in jl(state_dir / "negative.jsonl") if n.get("kind") == "id"}
    a = [r for r in rows if r.get("candidate_type") == "optimization"]
    n_neg = sum(1 for r in a if str(r.get("id")) in neg)
    n_push = sum(1 for r in a if str(r.get("id")) in pushed and str(r.get("id")) not in neg)
    n_closed = sum(1 for r in a if r.get("status", "open") != "open" and str(r.get("id")) not in pushed | neg)
    return {"observed": len(rows), "a_total": len(a), "a_pushed": n_push, "a_rejected": n_neg, "a_closed": n_closed,
            "a_open": len(a) - n_push - n_neg - n_closed}


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "insp-review"

        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else ui_text(json.dumps(body, ensure_ascii=False)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, url: str):
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _guard(self) -> dict | None:
            app.last = time.time()
            host = (self.headers.get("Host") or "").lower()
            if host not in (f"127.0.0.1:{app.port}", f"localhost:{app.port}"):
                self._send(403, {"error": "bad host"})
                return None
            q = parse_qs(urlparse(self.path).query)
            tok = self.headers.get("X-Token") or (q.get("t") or [""])[0]
            if not secrets.compare_digest(tok, app.token):
                self._send(403, {"error": "bad token：请从通知或 review_server.py --open 打开"})
                return None
            return q

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/ping":  # no token: lets the launcher detect a running server
                # BUG-4: compare the ABSOLUTE state dir; two packages both call theirs "inspiration"
                return self._send(200, {"ok": True, "version": VERSION, "state_dir": app.state_dir.name,
                                        "state_dir_abs": str(app.state_dir), "receiver": app.rcv_ctx is not None})
            if path.startswith("/go/thread/"):   # v1.5.4 (S6): no token — it only redirects to the platform page
                return self._redirect(app.thread_page(path.rsplit("/", 1)[-1]))
            q = self._guard()
            if q is None:
                return
            if path in ("/", "/index.html"):
                html = UI_FILE.read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/cards":
                with app.lock:
                    return self._send(200, {**load_cards(app.state_dir, (q.get("scope") or ["pending"])[0]),
                                            "hidden": hidden_actions(app)})
            if path == "/api/receiver/status":
                return self._send(200, receiver_status(app))
            if path == "/api/outcome":
                cid = (q.get("card_id") or [""])[0]
                if (q.get("scope") or [""])[0] == "all":
                    return self._send(200, {"outcomes": OUTCOME.read_outcomes(app.state_dir),
                                            "summary": OUTCOME.summarize(app.state_dir)})
                return self._send(200, {"outcome": OUTCOME.latest_by_card(app.state_dir).get(cid)})
            try:
                if path == "/api/actions":
                    return self._send(200, jobs_view(app))
                if path == "/api/action/brief":
                    cid = (q.get("card_id") or [""])[0]
                    return self._send(200, {"brief": _ra(app).make_brief(app.state_dir, cid)})
                if path in ("/api/action/script", "/api/action/dryrun"):   # B7: 最小验证脚本 / 本地 dry-run 回执
                    cid = (q.get("card_id") or [""])[0]
                    card = next((c for c in load_cards(app.state_dir, "all")["cards"] if c.get("id") == cid), None)
                    if card is None:
                        return self._send(404, {"error": f"找不到卡片 {cid}"})
                    if path == "/api/action/script":
                        return self._send(200, DRYRUN.build(card))
                    d = DRYRUN.receipt(card)
                    return self._send(200, {"dry_run": d, "md": DRYRUN.render_receipt_md(d)})
            except (LookupError, ValueError) as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if self._guard() is None:
                return
            if path in ("/api/receiver/check", "/api/run"):
                return self._receiver_post(path)
            if path.startswith("/api/action/"):
                return self._action_post(path)
            if path == "/api/ask/retry":   # v1.5.6 (ASK1): 追问失败 → 同一张卡再问一次（不再记一条评价）
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(n, 1_000_000)).decode("utf-8") or "{}")
                    aq = _aq(app)
                    aid = str(body.get("ask_id", ""))
                    if not re.fullmatch(r"ask_[0-9A-Za-z_\-]+", aid):
                        raise ValueError("没有这条追问")
                    old = aq.read_ask(app.state_dir, aid)
                    if old.get("status") not in ("failed", "timeout"):
                        raise ValueError("这条追问不是失败 / 超时状态，不需要重试")
                    with app.lock:
                        r = aq.dispatch(app.cfg_path, old["card_id"], via="retry", verdict_ts=old.get("verdict_ts"))
                    return self._send(200, scrub_paths(r, app))
                except (LookupError, ValueError, OSError, KeyError) as exc:
                    return self._send(400, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    return self._send(500, {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 1_000_000)).decode("utf-8") or "{}")
                cid = str(body.get("card_id", ""))
                extra: dict = {}
                if path == "/api/outcome":   # B1: 结果回填不是评价，不写 verdicts.jsonl、不调权重
                    with app.lock:
                        entry = OUTCOME.record(app.state_dir, cid, str(body.get("outcome") or ""),
                                               str(body.get("conclusion") or ""), str(body.get("measured") or ""),
                                               via="card_ui")
                    return self._send(200, {"ok": True, "entry": entry})
                with app.lock:
                    if path == "/api/verdict":
                        action = body.get("action")
                        if action not in ("adopt", "reject", "done", "not_important", "ask_detail", "start_now"):
                            raise ValueError(f"不支持的动作：{action}")
                        e = VERDICT.record(app.state_dir, cid, action, (body.get("reason") or "").strip(),
                                           (body.get("block_topic") or "").strip() or None, via="card_ui")
                        log_implicit(app.state_dir, cid, "verdict:" + action, body.get("implicit"))
                        if action == "start_now":   # v1.5.1 (K1): record → brief → task, in one key press
                            extra["action"] = after_start_now(app, cid, e.get("ts"))
                        elif action == "ask_detail":   # v1.5.3: record → ask the agent with the fixed template
                            extra["ask"] = after_ask(app, cid, e.get("ts"))
                        elif action == "reject" and app.cfg_path is not None:   # v1.5.3: a rejected card stops costing time
                            try:
                                extra["cancelled_jobs"] = _ra(app).cancel_for_card(app.state_dir, cid, *_ra(app).load_cfg(app.cfg_path)[:1],
                                                                                   app.cfg_path, why="reject")
                                extra["cancelled_asks"] = _aq(app).cancel_for_card(app.state_dir, cid, "评价改为否决")
                            except Exception as exc:  # noqa: BLE001
                                extra["cancel_error"] = str(exc)[:200]
                    elif path == "/api/edit":
                        fields = {k: str(v) for k, v in (body.get("fields") or {}).items() if k in VERDICT.EDITABLE_FIELDS}
                        rt = raw_view_title(app.state_dir, cid)
                        if "title" in fields and isinstance(rt, str) and fields["title"] == strip_canary(rt) != rt:
                            fields.pop("title")  # only the hidden canary differs → not a real edit
                        e = VERDICT.record(app.state_dir, cid, "edited", (body.get("reason") or "").strip(),
                                           edited_card=fields, via="card_ui")
                        log_implicit(app.state_dir, cid, "edited", body.get("implicit"))
                    elif path == "/api/undo":
                        st = VERDICT.verdict_stacks(VERDICT.read_verdicts(app.state_dir)).get(cid) or []
                        e = VERDICT.record(app.state_dir, cid, "retract", via="card_ui")
                        if st and st[-1].get("action") == "start_now" and app.cfg_path is not None:
                            try:
                                ra_ = _ra(app)
                                extra["cancelled_jobs"] = ra_.cancel_for_card(app.state_dir, cid, *ra_.load_cfg(app.cfg_path)[:1], app.cfg_path)
                            except Exception as exc:  # noqa: BLE001
                                extra["cancel_error"] = str(exc)[:200]
                        if st and st[-1].get("action") == "ask_detail" and app.cfg_path is not None:
                            try:
                                extra["cancelled_asks"] = _aq(app).cancel_for_card(app.state_dir, cid)
                            except Exception as exc:  # noqa: BLE001
                                extra["cancel_error"] = str(exc)[:200]
                        log_implicit(app.state_dir, cid, "undo", body.get("implicit"))
                    elif path == "/api/view":  # implicit only (e.g. skipped card); never a verdict
                        log_implicit(app.state_dir, cid, "view", body.get("implicit"))
                        return self._send(200, {"ok": True})
                    else:
                        return self._send(404, {"error": "not found"})
                    try:
                        VERDICT.rebuild_inbox(app.state_dir)  # keep the Excel fallback consistent
                    except OSError:
                        pass
                    pending = len(load_cards(app.state_dir)["cards"])
                self._send(200, {"ok": True, "entry": {k: e.get(k) for k in ("ts", "card_id", "action", "label", "weight", "edit_diff", "retracts")},
                                 "pending": pending, **extra})
            except (ValueError, LookupError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # never crash the server
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        def _action_post(self, path: str):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 1_000_000)).decode("utf-8") or "{}")
                ra = _ra(app)
                with app.lock:
                    if path == "/api/action/start":       # 动手单确认 / 补全后开跑
                        cid = str(body.get("card_id", ""))
                        st = VERDICT.verdict_stacks(VERDICT.read_verdicts(app.state_dir)).get(cid) or []
                        if not st or st[-1].get("action") != "start_now":
                            raise ValueError("这张卡最近的评价不是“立刻动手”，不能建任务")
                        r = ra.dispatch(app.cfg_path, cid, via="card_ui", verdict_ts=st[-1].get("ts"),
                                        brief=body.get("brief") if isinstance(body.get("brief"), dict) else None)
                        return self._send(200 if r.get("job_id") else 400 if r.get("needs_input") else 200, scrub_paths(r, app))
                    jid = str(body.get("job_id", ""))
                    if path == "/api/action/reveal":       # v1.5.2: open the receipt / its folder locally (path never sent to the page)
                        if not re.fullmatch(r"act_[0-9A-Za-z_]+", jid) or not (ra.job_dir(app.state_dir, jid) / "job.json").is_file():
                            raise ValueError("没有这个任务")
                        jd = ra.job_dir(app.state_dir, jid)
                        target = jd / "receipt.md"
                        if not target.is_file():
                            target = jd
                        what = "file" if body.get("what") == "file" and target.is_file() else "folder"
                        how = open_local(target, select=(what == "folder"))
                        return self._send(200, {"ok": True, "job_id": jid, "opened": what, "how": how})
                    if path == "/api/action/approve":
                        j = ra.approve(app.state_dir, jid, str(body.get("note") or ""))
                        pid = ra.spawn(app.cfg_path, j["job_id"], app.state_dir)
                        return self._send(200, {"ok": True, "job_id": jid, "status": j["status"], "worker_pid": pid})
                    if path == "/api/action/ack":
                        res = str(body.get("result") or "")
                        j = ra.ack(app.state_dir, jid, res, str(body.get("note") or ""))
                        pid = ra.spawn(app.cfg_path, j["job_id"], app.state_dir) if res == "rerun" and j["status"] in ("queued", "approved") else None
                        return self._send(200, {"ok": True, "job_id": jid, "status": j["status"], "worker_pid": pid})
                return self._send(404, {"error": "not found"})
            except (ValueError, LookupError) as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})

        def _receiver_post(self, path: str):
            if app.rcv_ctx is None:
                return self._send(400, {"error": "收货员未启用：用 --config 启动评价页，并确认 config.receiver.enabled=true"})
            if path == "/api/receiver/check":
                def job():
                    if app.rcv_busy.acquire(blocking=False):
                        try:
                            app.rcv.poll_once(app.rcv_ctx)
                        except Exception as exc:
                            app.rcv.status_write(app.rcv_ctx, error=f"{type(exc).__name__}: {str(exc)[:200]}")
                        finally:
                            app.rcv_busy.release()
                threading.Thread(target=job, daemon=True).start()
                return self._send(202, {"ok": True, "message": "正在检查最近的会话…"})
            try:
                r = app.rcv.trigger_run(app.rcv_ctx)
                return self._send(200, {"ok": True, **r, "message": "已触发，5–15 分钟后卡片会自动出现"})
            except SystemExit as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:
                return self._send(500, {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    return H


def start_receiver(app: "App", cfg_path: Path) -> None:
    """v1.4: the review server doubles as the receiver (read-only polling, never starts agent runs on its own)."""
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not (cfg.get("receiver") or {}).get("enabled", True):
            return
        app.rcv = _load("platform/receiver.py", "receiver_rs")
        app.rcv_ctx = app.rcv.Ctx(cfg_path)
    except Exception as exc:
        print("WARN 收货员没有启动：", exc, flush=True)
        app.rcv, app.rcv_ctx = None, None
        return
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            if app.rcv_busy.acquire(blocking=False):
                try:
                    app.rcv.watch_step(app.rcv_ctx)
                except Exception as exc:
                    app.rcv.status_write(app.rcv_ctx, error=f"{type(exc).__name__}: {str(exc)[:200]}")
                finally:
                    app.rcv_busy.release()
            stop.wait(app.rcv.next_wait(app.rcv_ctx))
    threading.Thread(target=loop, daemon=True, name="receiver").start()
    print("RECEIVER: 已启动（只读轮询，发现灵感官的结果就自动入库）", flush=True)


def serve(state_dir: Path, port: int, idle_min: float = 0, ready: threading.Event | None = None,
          holder: list | None = None, cfg_path: Path | None = None) -> None:
    if not (state_dir / "state.json").exists():
        raise SystemExit(f"找不到状态目录 {state_dir}/state.json")
    app = App(state_dir, port, get_token(), idle_min)
    if cfg_path is not None:
        app.cfg_path = cfg_path.resolve()
        start_receiver(app, cfg_path.resolve())
        if app.rcv_ctx is not None:
            app.idle_min = idle_min = 0  # the receiver must keep running while you wait for results
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    info = {"port": port, "pid": os.getpid(), "started": now_iso(), "state_dir": str(state_dir), "version": VERSION}
    (state_dir / "inbox").mkdir(exist_ok=True)
    (state_dir / "inbox" / ".review_server.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    if holder is not None:
        holder.append(httpd)
    if idle_min and idle_min > 0:
        def watchdog():
            while True:
                time.sleep(30)
                if time.time() - app.last > idle_min * 60:
                    httpd.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
    print(f"REVIEW SERVER: http://127.0.0.1:{port}/  （状态目录 {state_dir}）", flush=True)
    if ready:
        ready.set()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


# ----------------------------------------------------------------------------- launcher (used by run_local)
def url_for(port: int) -> str:
    return f"http://127.0.0.1:{port}/?t={get_token()}"


def ping(port: int, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def ensure_server(cfg_path: Path | None, state_dir: Path, port: int, wait_s: float = 8) -> str | None:
    """Return the review URL, starting a detached server if none is answering on the port."""
    p = ping(port)
    if p and p.get("state_dir_abs") and Path(p["state_dir_abs"]).resolve() == state_dir.resolve():
        return url_for(port)
    if p:  # some other review server (different state dir, or a pre-1.4 server that only reports the dir NAME)
        print(f"WARN review: 端口 {port} 被另一个评价服务占用（{p.get('state_dir_abs') or p.get('state_dir') + '（旧版，只报目录名）'}）。"
              f"关掉它（python platform/review_server.py --config <那个包的 config> --stop），或在 config 里改 review.port")
        return None
    exe = sys.executable
    if os.name == "nt":
        w = Path(exe).with_name("pythonw.exe")
        exe = str(w) if w.exists() else exe
    args = [exe, str(Path(__file__).resolve()), "--state-dir", str(state_dir), "--port", str(port), "--serve", "--idle-min", "720"]
    if cfg_path:
        args[2:2] = ["--config", str(Path(cfg_path).resolve())]
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(ROOT)}
    if os.name == "nt":
        base = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        for flags in (base | 0x01000000, base):      # try BREAKAWAY_FROM_JOB first (survive the scheduled task)
            try:
                subprocess.Popen(args, creationflags=flags, **kw)
                break
            except OSError:
                continue
    else:
        subprocess.Popen(args, start_new_session=True, **kw)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if ping(port, 0.5):
            return url_for(port)
        time.sleep(0.3)
    print("WARN review: 评价服务没有在规定时间内启动")
    return None


def toast(title: str, body: str, url: str | None, button: str | None = None) -> dict:
    """Windows toast; clicking opens url. Returns diagnostics: {"called": bool, "visible": True/False/None, ...}.
    ToastNotifier.Show() never fails loudly, so "called" alone does NOT mean the user saw anything."""
    if os.name != "nt":
        return {"called": False, "visible": False, "why": "not_windows"}
    ps1 = Path(__file__).with_name("windows") / "toast.ps1"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"title": ui_text(title), "body": ui_text(body), "url": url or "", "button": button or ""}, f, ensure_ascii=False)
        tmp = f.name
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Payload", tmp],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=0x08000000)
        diag: dict = {"called": r.returncode == 0}
        for line in (r.stdout or "").splitlines():
            if line.startswith("TOASTDIAG "):
                try:
                    diag.update(json.loads(line[10:]))
                except json.JSONDecodeError:
                    pass
        if r.returncode != 0:
            diag["error"] = (r.stderr or r.stdout).strip()[:300]
        diag["visible"], diag["why"] = judge_toast(diag)
        return diag
    except Exception as exc:
        return {"called": False, "visible": False, "why": f"exception: {exc}"}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def judge_toast(d: dict) -> tuple[bool | None, str]:
    if not d.get("called"):
        return False, "PowerShell 调用失败：" + str(d.get("error", ""))[:120]
    if d.get("setting") and d["setting"] != "Enabled":
        return False, f"Windows 拒绝了这个应用的通知（{d['setting']}）"
    if d.get("app_enabled") == 0:
        return False, "设置 → 系统 → 通知 里“Windows PowerShell”的通知被关掉了"
    if d.get("global_toast") == 0:
        return False, "系统通知总开关是关的"
    if d.get("session_id") == 0:
        return False, "任务运行在会话 0（后台服务会话），桌面看不到通知"
    if d.get("history") == 0:
        return False, "Show() 已调用，但通知中心里没有这条（常见原因：勿扰/专注模式，或首次使用需在通知设置里允许 PowerShell）"
    if d.get("history"):
        return True, "通知中心里能查到"
    return None, "已调用，无法确认是否显示"


# ----------------------------------------------------------------------------- self-test
def self_test() -> int:
    import shutil
    global TOKEN_FILE
    tmp = Path(tempfile.mkdtemp())
    TOKEN_FILE = tmp / "review_token"
    sd = tmp / "inspiration"
    shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
    (sd / "cards" / "2026-10-28").mkdir(parents=True, exist_ok=True)
    mk = lambda i, t: {"id": f"ins_t_{i}", "type": t, "subfield": "memory", "title": f"卡片{i}", "one_line": "一句话",
                       "why_me": "w", "hypothesis": "h", "minimal_check": "m", "expected_observable": "e", "falsifier": "f",
                       "magnitude": "x", "cost_estimate": {"tokens": "~1k", "wall_clock": "1h", "risk": "low"},
                       "source": {"kind": "arxiv", "ref": f"2610.0000{i}v1", "quote": "q", "verified": True},
                       "rubric_scores": [], "status": "proposed", "verdict": None, "_trace": {"weights_version": 0}}
    (sd / "cards/2026-10-28/cards.json").write_text(json.dumps({"date": "2026-10-28", "cards": [mk(1, "knowledge"), mk(2, "hypothesis"), mk(3, "process")]}, ensure_ascii=False), encoding="utf-8")
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    ready, holder = threading.Event(), []
    threading.Thread(target=serve, args=(sd, port, 0, ready, holder), daemon=True).start()
    ready.wait(5)
    tok = get_token()
    base = f"http://127.0.0.1:{port}"

    def call(path, data=None, token=tok, host=None):
        req = urllib.request.Request(base + path, data=None if data is None else json.dumps(data).encode(),
                                     headers={"X-Token": token, "Content-Type": "application/json", **({"Host": host} if host else {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode()) if "json" in r.headers.get("Content-Type", "") else r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok &= bool(cond)

    check("bad token 403", call("/api/cards", token="x")[0] == 403)
    check("bad host 403", call("/api/cards", host="evil.com")[0] == 403)
    code, page = call("/?t=" + tok)
    check("ui served with token", code == 200 and tok in page and "__TOKEN__" not in page)
    check("3 pending", len(call("/api/cards")[1]["cards"]) == 3)
    imp = {"flipped": True, "dwell_ms": 4200, "position": 0, "queue_size": 3, "session": "s1"}
    check("adopt", call("/api/verdict", {"card_id": "ins_t_1", "action": "adopt", "implicit": imp})[1].get("pending") == 2)
    check("reject + block topic", call("/api/verdict", {"card_id": "ins_t_2", "action": "reject", "block_topic": "foo"})[0] == 200)
    neg = (sd / "negative.jsonl").read_text(encoding="utf-8")
    check("negative rows added", "2610.00002v1" in neg and '"foo"' in neg)
    code, r = call("/api/undo", {"card_id": "ins_t_2"})
    check("undo reject", code == 200 and r["pending"] == 2 and r["entry"]["retracts"]["action"] == "reject")
    neg = (sd / "negative.jsonl").read_text(encoding="utf-8")
    check("undo removed negative rows", "2610.00002v1" not in neg and '"foo"' not in neg)
    card2 = json.loads((sd / "cards/2026-10-28/cards.json").read_text(encoding="utf-8"))["cards"][1]
    check("undo restored status", card2["status"] == "proposed" and card2["verdict"] is None)
    check("undo with nothing → 400", call("/api/undo", {"card_id": "ins_t_3"})[0] == 400)
    code, r = call("/api/edit", {"card_id": "ins_t_3", "fields": {"minimal_check": "改成只跑 1 次", "title": "卡片3"}})
    check("edit diff only changed fields", code == 200 and list(r["entry"]["edit_diff"]) == ["minimal_check"])
    check("edit no-op → 400", call("/api/edit", {"card_id": "ins_t_3", "fields": {"minimal_check": "改成只跑 1 次"}})[0] == 400)
    check("edit file saved", len(list((sd / "edits").glob("ins_t_3_*.json"))) == 1)
    allc = {c["id"]: c for c in call("/api/cards?scope=all")[1]["cards"]}
    check("edited content visible", allc["ins_t_3"]["_edited"]["minimal_check"] == "改成只跑 1 次")
    check("edit then classify", call("/api/verdict", {"card_id": "ins_t_3", "action": "adopt"})[0] == 200)
    check("bad action 400", call("/api/verdict", {"card_id": "ins_t_2", "action": "edited"})[0] == 400)
    rows = VERDICT.read_verdicts(sd)
    st = VERDICT.verdict_stacks(rows)
    check("stacks: t1 adopt, t2 none, t3 edited→adopt", st["ins_t_1"][-1]["action"] == "adopt" and "ins_t_2" not in st
          and [v["action"] for v in st["ins_t_3"]] == ["edited", "adopt"])
    check("via=card_ui recorded", all(v.get("via") == "card_ui" for v in rows))
    imp_rows = [json.loads(l) for l in (sd / "implicit.jsonl").read_text(encoding="utf-8").splitlines()]
    check("implicit logged separately", imp_rows[0]["dwell_ms"] == 4200 and imp_rows[0]["flipped"] is True)
    inbox = (sd / "inbox" / "verdict_inbox.csv").read_text(encoding="utf-8-sig")
    check("excel inbox rebuilt (only t2 left)", "ins_t_2" in inbox and "ins_t_1" not in inbox)
    # legacy card whose title still carries the memory canary "◆"
    cf = sd / "cards/2026-10-28/cards.json"
    doc = json.loads(cf.read_text(encoding="utf-8"))
    c4 = mk(4, "knowledge"); c4["title"] = "卡片4◆"
    doc["cards"].append(c4); cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    shown = {c["id"]: c for c in call("/api/cards")[1]["cards"]}
    check("canary hidden in UI", shown.get("ins_t_4", {}).get("title") == "卡片4")
    check("canary-only title diff ignored",
          call("/api/edit", {"card_id": "ins_t_4", "fields": {"title": "卡片4"}})[0] == 400)
    code, _ = call("/api/edit", {"card_id": "ins_t_4", "fields": {"title": "卡片4", "one_line": "新的一句话"}})
    last = VERDICT.read_verdicts(sd)[-1]
    check("edit with hidden canary records only real change",
          code == 200 and set(last.get("edit_diff", {})) == {"one_line"})
    holder[0].shutdown()
    print("SELF-TEST OK: review_server" if ok else "SELF-TEST FAILED: review_server")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="灵感卡片评价页（本地）")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--state-dir", type=Path)
    ap.add_argument("--port", type=int)
    ap.add_argument("--open", action="store_true", help="确保服务在运行，并打开浏览器")
    ap.add_argument("--url", action="store_true", help="只打印评价页地址")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--serve", action="store_true", help="前台运行（计划任务 / 调试用）")
    ap.add_argument("--idle-min", type=float, default=0, help="空闲多少分钟后自动退出，0=不退出")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--toast-test", action="store_true", help="弹一条测试通知并打印诊断（只在 Windows 上有意义）")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    state_dir, port = resolve(a.config, a.state_dir, a.port)
    if a.toast_test:
        d = toast("灵感机制 · 通知测试", "看到这条说明通知通路正常；点击打开评价页", url_for(port))
        print(json.dumps(d, ensure_ascii=False, indent=2))
        (state_dir / "inbox").mkdir(parents=True, exist_ok=True)
        (state_dir / "inbox" / "toast_last.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结论：", {True: "可见", False: "不可见", None: "无法确认"}[d.get("visible")], "—", d.get("why"))
        return 0 if d.get("visible") is not False else 2
    if a.url:
        print(url_for(port))
        return 0
    if a.stop:
        info = state_dir / "inbox" / ".review_server.json"
        try:
            pid = json.loads(info.read_text(encoding="utf-8"))["pid"]
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, 15)
            print("STOPPED", pid)
        except Exception as exc:
            print("没有找到运行中的评价服务：", exc)
        return 0
    if a.open:
        url = ensure_server(a.config, state_dir, port)
        if not url:
            return 1
        print("打开", url.split("?")[0])
        webbrowser.open(url)
        return 0
    serve(state_dir, port, a.idle_min, cfg_path=a.config.resolve() if a.config else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
