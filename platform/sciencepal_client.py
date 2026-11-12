#!/usr/bin/env python3
"""Minimal SciencePal API client (stdlib only).

Endpoints used were checked against https://sciencepal.ai/openapi.json (2026-10-28).
The OpenAPI does NOT declare a security scheme. The SDK download endpoint mentions
"SCIENCEPAL_ACCESS_TOKEN ... from Authorization Bearer", so Bearer is the default here;
run platform/probe.py first to confirm, and change auth_header / auth_scheme in config if needed.
Response bodies for several endpoints are not typed in the OpenAPI, so parsing is defensive.
"""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} {url}: {body[:500]}")
        self.status, self.body, self.url = status, body, url


class SciencePal:
    def __init__(self, base_url: str, token: str, auth_header: str = "Authorization",
                 auth_scheme: str = "Bearer", timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.token = token
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.timeout = timeout

    # ---------- low level ----------
    def _headers(self, extra: dict | None = None) -> dict:
        val = f"{self.auth_scheme} {self.token}".strip() if self.auth_scheme else self.token
        h = {self.auth_header: val, "Accept": "application/json", "User-Agent": "inspiration-orchestrator/1.0"}
        h.update(extra or {})
        return h

    def request(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None,
                data: bytes | None = None, headers: dict | None = None, raw: bool = False,
                timeout: int | None = None) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        h = self._headers(headers)
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode("utf-8", "replace"), url) from None
        if raw:
            return body
        txt = body.decode("utf-8", "replace")
        try:
            return json.loads(txt) if txt.strip() else None
        except json.JSONDecodeError:
            return txt

    @staticmethod
    def _multipart(fields: dict[str, Any], files: Iterable[tuple[str, Path]]) -> tuple[bytes, str]:
        boundary = "----insp" + uuid.uuid4().hex
        out = bytearray()
        for k, v in fields.items():
            if v is None:
                continue
            vals = v if isinstance(v, list) else [v]
            for one in vals:
                if isinstance(one, bool):
                    one = "true" if one else "false"
                out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode()
                out += str(one).encode("utf-8") + b"\r\n"
        for field, path in files:
            path = Path(path)
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                    f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
            out += path.read_bytes() + b"\r\n"
        out += f"--{boundary}--\r\n".encode()
        return bytes(out), f"multipart/form-data; boundary={boundary}"

    def post_multipart(self, path: str, fields: dict, files: Iterable[tuple[str, Path]] = (), params: dict | None = None) -> Any:
        body, ctype = self._multipart(fields, list(files))
        return self.request("POST", path, data=body, headers={"Content-Type": ctype}, params=params)

    # ---------- agents & skills ----------
    def list_agents(self) -> Any:
        return self.request("GET", "/api/agents")

    def create_agent(self, name: str, system_prompt: str, description: str = "", tool_profile: str | None = None) -> Any:
        body = {"name": name, "system_prompt": system_prompt, "description": description}
        if tool_profile:
            body["tool_profile"] = tool_profile
        return self.request("POST", "/api/agents", json_body=body)

    def update_agent(self, agent_id: str, body: dict) -> Any:
        return self.request("PUT", f"/api/agents/{agent_id}", json_body=body)

    def import_skill_zip(self, zip_path: Path, name: str | None = None, replace: bool = False, agent_type: str = "general") -> Any:
        return self.post_multipart("/api/skills/import",
                                   {"name": name, "agent_type": agent_type, "replace_existing": replace},
                                   [("package", zip_path)])

    def create_skill_item(self, name: str, description: str, body_markdown: str, display_name: str | None = None,
                          tags: list[str] | None = None, category: str | None = "inspiration") -> Any:
        return self.request("POST", "/api/skills/items", json_body={
            "name": name, "description": description[:2000], "body_markdown": body_markdown,
            "display_name": display_name, "tags": tags or ["inspiration"], "category": category})

    def put_agent_skills(self, agent_id: str, skill_ids: list[str]) -> Any:
        return self.request("PUT", f"/api/agents/{agent_id}/skills", json_body={"skill_ids": skill_ids})

    def get_agent_skills(self, agent_id: str) -> Any:
        return self.request("GET", f"/api/agents/{agent_id}/skills")

    def run_skills(self, agent_run_id: str) -> Any:
        return self.request("GET", f"/api/agent-run/{agent_run_id}/skills")

    # ---------- runs ----------
    def initiate(self, prompt: str, *, agent_id: str | None, files: list[Path], model_name: str | None = None,
                 web_search_on: bool = False, workspace_context: str | None = None,
                 extra_fields: dict | None = None) -> dict:
        fields = {
            "prompt": prompt, "agent_id": agent_id, "model_name": model_name,
            "agent_selection_explicit": True if agent_id else None,
            "web_search_on": "true" if web_search_on else "false",
            "workspace_context": workspace_context, "stream": True,
        }
        fields.update(extra_fields or {})
        return self.post_multipart("/api/agent/initiate", fields, [("files", f) for f in files])

    def get_run(self, agent_run_id: str) -> Any:
        return self.request("GET", f"/api/agent-run/{agent_run_id}")

    def stream_run(self, agent_run_id: str, timeout_s: int = 1200) -> list[Any]:
        """Collect SSE events until the stream closes or the timeout expires."""
        url = f"{self.base}/api/agent-run/{agent_run_id}/stream?" + urllib.parse.urlencode({"token": self.token})
        req = urllib.request.Request(url, headers=self._headers({"Accept": "text/event-stream"}))
        events: list[Any] = []
        deadline = time.time() + timeout_s
        try:
            with urllib.request.urlopen(req, timeout=min(300, timeout_s)) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        try:
                            events.append(json.loads(payload))
                        except json.JSONDecodeError:
                            events.append(payload)
                        if _looks_terminal(events[-1]):
                            break
                    if time.time() > deadline:
                        events.append({"_orchestrator": "timeout"})
                        break
        except (urllib.error.URLError, TimeoutError) as exc:
            events.append({"_orchestrator": f"stream_error: {exc}"})
        return events

    def wait_run(self, agent_run_id: str, timeout_s: int = 1200, poll_s: int = 10) -> Any:
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            last = self.get_run(agent_run_id)
            st = str(_find_key(last, "status") or "").lower()
            if st in {"completed", "failed", "stopped", "error", "cancelled", "asked"}:
                return last
            time.sleep(poll_s)
        return last

    def read_thread_file(self, thread_id: str, path: str) -> bytes:
        return self.request("GET", f"/api/thread/{thread_id}/files/content", params={"path": path}, raw=True)

    def read_sandbox_file(self, sandbox_id: str, path: str, thread_id: str | None = None) -> bytes:
        """Sandbox endpoint — allowed for API keys (unlike /api/thread/{id}/files/content)."""
        return self.request("GET", f"/api/sandboxes/{sandbox_id}/files/content",
                            params={"path": path, "thread_id": thread_id}, raw=True)

    # ---------- v1.4: receiver (collect results from web-triggered runs) ----------
    def whoami_agents(self) -> list[dict]:
        r = self.list_agents()
        items = r.get("agents", r.get("items", r.get("data", []))) if isinstance(r, dict) else (r or [])
        return [a for a in items if isinstance(a, dict)]

    def project_of_thread(self, thread_id: str, resp: Any = None) -> str | None:
        """v1.5.4 (S6): the web page of a conversation is /projects/<project_id>/thread/<thread_id>.
        project_id from the initiate response, else the dashboard list, else GET /api/thread/<id>."""
        pid = _find_key(resp, "project_id") if resp is not None else None
        if pid:
            return str(pid)
        try:
            for t in self.dashboard_threads():
                if str(t.get("thread_id") or t.get("id")) == str(thread_id):
                    pid = t.get("project_id") or _find_key(t, "project_id")
                    if pid:
                        return str(pid)
        except Exception:  # noqa: BLE001
            pass
        try:
            pid = _find_key(self.request("GET", f"/api/thread/{thread_id}"), "project_id")
            return str(pid) if pid else None
        except Exception:  # noqa: BLE001
            return None

    def dashboard_threads(self) -> list[dict]:
        """v1.5.5 (P12): threads carry no title; the title is projects[].name — join on project_id."""
        r = self.request("GET", "/api/dashboard/personal-conversations")
        if not isinstance(r, dict):
            return []
        names = {p.get("project_id") or p.get("id"): p.get("name") for p in (r.get("projects") or []) if isinstance(p, dict)}
        out = []
        for th in r.get("threads") or []:
            if isinstance(th, dict):
                th = dict(th)
                if not th.get("title") and names.get(th.get("project_id")):
                    th["title"] = names[th.get("project_id")]
                out.append(th)
        return out

    def thread_agent_runs(self, thread_id: str) -> Any:
        return self.request("GET", f"/api/thread/{thread_id}/agent-runs")

    def thread_agent(self, thread_id: str) -> Any:
        return self.request("GET", f"/api/thread/{thread_id}/agent")

    def thread_sandbox(self, thread_id: str) -> dict:
        r = self.request("GET", f"/api/thread/{thread_id}/sandbox")
        return r if isinstance(r, dict) else {}

    def write_sandbox_file(self, sandbox_id: str, path: str, data: bytes, name: str = "file") -> Any:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / name
            p.write_bytes(data)
            return self.post_multipart(f"/api/sandboxes/{sandbox_id}/files", {"path": path, "overwrite": "true"}, [("file", p)])

    def add_message(self, thread_id: str, content: str) -> str:
        mid = str(uuid.uuid4())
        self.request("POST", f"/api/thread/{thread_id}/messages", json_body={"message_id": mid, "content": content})
        return mid

    def start_thread_agent(self, thread_id: str, agent_id: str | None, message_id: str | None = None) -> Any:
        body = {"agent_id": agent_id, "message_id": message_id, "stream": True}
        return self.request("POST", f"/api/thread/{thread_id}/agent/start", json_body={k: v for k, v in body.items() if v is not None})

    def list_scheduled_tasks(self) -> list[dict]:
        r = self.request("GET", "/api/scheduled-tasks")
        items = r.get("tasks", r.get("items", r.get("data", []))) if isinstance(r, dict) else (r or [])
        return [t for t in items if isinstance(t, dict)]

    def delete_scheduled_task(self, task_id: str) -> Any:
        return self.request("DELETE", f"/api/scheduled-tasks/{task_id}")

    # ---------- workspace & memory ----------
    def read_user_workspace_file(self, path: str) -> bytes:
        return self.request("GET", "/api/workspace/files/content", params={"path": path}, raw=True)

    def upload_user_workspace_files(self, target_dir: str, files: list[Path]) -> Any:
        return self.post_multipart("/api/workspace/files/upload", {"target_dir": target_dir}, [("files", f) for f in files])

    def list_memory(self, limit: int = 100, max_pages: int = 30) -> Any:
        """v1.5.4 (S8): ALL memories. GET /api/memory without limit returns only 20, and limit>100 returns nothing,
        so page with limit=100. The paging parameter is not documented: offset is tried first, then page / skip;
        a page that brings nothing new ends the loop. Returns {"memories": [...], "total": n, "paging": mode}."""
        def items_of(r: Any) -> list:
            if isinstance(r, list):
                return [m for m in r if isinstance(m, dict)]
            if isinstance(r, dict):
                for k in ("memories", "items", "data", "results"):
                    if isinstance(r.get(k), list):
                        return [m for m in r[k] if isinstance(m, dict)]
            return []

        def mid(m: dict) -> str:
            return str(m.get("id") or m.get("memory_id") or json.dumps(m, sort_keys=True, ensure_ascii=False)[:200])
        try:
            first = items_of(self.request("GET", "/api/memory", params={"limit": limit}))
        except ApiError:
            first = items_of(self.request("GET", "/api/memory"))
            return {"memories": first, "total": len(first), "paging": "none"}
        out, seen = list(first), {mid(m) for m in first}
        if len(first) < limit:
            return {"memories": out, "total": len(out), "paging": "single"}
        modes = {"offset": lambda i: {"limit": limit, "offset": i * limit}, "page": lambda i: {"limit": limit, "page": i + 1},
                 "skip": lambda i: {"limit": limit, "skip": i * limit}}
        mode = None
        for name, mk in modes.items():   # find the parameter that actually moves to page 2
            try:
                page = items_of(self.request("GET", "/api/memory", params=mk(1)))
            except ApiError:
                continue
            new = [m for m in page if mid(m) not in seen]
            if new:
                mode = name
                out += new
                seen |= {mid(m) for m in new}
                if len(page) < limit:
                    return {"memories": out, "total": len(out), "paging": name}
                break
        if mode is None:
            return {"memories": out, "total": len(out), "paging": "first_page_only"}
        for i in range(2, max_pages):
            try:
                page = items_of(self.request("GET", "/api/memory", params=modes[mode](i)))
            except ApiError:
                break
            new = [m for m in page if mid(m) not in seen]
            out += new
            seen |= {mid(m) for m in new}
            if not new or len(page) < limit:
                break
        return {"memories": out, "total": len(out), "paging": mode}

    def create_memory(self, content: str, category: str | None = None, tags: list[str] | None = None,
                      memory_type: str = "preference", confidence: float = 1.0) -> Any:
        return self.request("POST", "/api/memory", json_body={
            "content": content[:1000], "category": category, "tags": tags or [],
            "memory_type": memory_type, "confidence": confidence})

    def patch_memory(self, memory_id: str, body: dict) -> Any:
        return self.request("PATCH", f"/api/memory/{memory_id}", json_body=body)

    def delete_memory(self, memory_id: str) -> Any:
        return self.request("DELETE", f"/api/memory/{memory_id}")


def _find_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _looks_terminal(ev: Any) -> bool:
    if isinstance(ev, dict):
        st = str(ev.get("status", "")).lower()
        typ = str(ev.get("type", "")).lower()
        return (typ == "status" and st in {"completed", "failed", "stopped", "error", "asked"}) or st in {"completed", "failed", "stopped", "asked"}
    return False


def collect_strings(obj: Any, out: list[str] | None = None, depth: int = 0) -> list[str]:
    """Walk events recursively; JSON-looking strings are parsed and walked too."""
    out = [] if out is None else out
    if depth > 8:
        return out
    if isinstance(obj, str):
        out.append(obj)
        s = obj.strip()
        if s[:1] in "{[":
            try:
                collect_strings(json.loads(s), out, depth + 1)
            except json.JSONDecodeError:
                pass
    elif isinstance(obj, dict):
        for v in obj.values():
            collect_strings(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            collect_strings(v, out, depth + 1)
    return out


AUTH_DENIED = (401, 403)


class MultiAuth:
    """Call the same client method with several credentials in order; fall through on 401/403.

    Real platform (v1.4.1 feedback): an API key (sp_live_…) is limited to "SDK catalogs, agent, run and
    sandbox endpoints". Thread files, the conversation list, thread → runs / sandbox lookups, scheduled
    tasks and memory need the session token. Order is usually [session, api_key]: the session token is
    tried first, an expired session token (401) still falls back to the key.
    """

    def __init__(self, clients: list, labels: list[str]):
        pairs = [(c, l) for c, l in zip(clients, labels) if c is not None]
        if not pairs:
            raise ValueError("MultiAuth needs at least one client")
        self._clients = [c for c, _ in pairs]
        self.labels = [l for _, l in pairs]
        self.used: dict[str, str] = {}      # method → credential that worked last
        self.denied: dict[str, list] = {}   # method → [(credential, status)]

    @property
    def base(self) -> str:
        return self._clients[0].base

    def has(self, label: str) -> bool:
        return label in self.labels

    def __getattr__(self, name: str):
        first = getattr(self._clients[0], name)
        if not callable(first):
            return first

        def call(*a, **kw):
            last = None
            for c, label in zip(self._clients, self.labels):
                try:
                    r = getattr(c, name)(*a, **kw)
                    self.used[name] = label
                    return r
                except ApiError as exc:
                    if exc.status not in AUTH_DENIED:
                        raise
                    self.denied.setdefault(name, []).append((label, exc.status))
                    last = exc
            raise last
        return call


def extract_blocks(texts: list[str], start: str, end: str) -> list[str]:
    """Every complete marker block (in order of appearance) from each string and from the concatenation."""
    out: list[str] = []
    for t in texts + ["".join(texts)]:
        i = t.find(start)
        while i != -1:
            j = t.find(end, i + len(start))
            if j == -1:
                break
            k = t.rfind(start, i, j)  # nearest opening marker before this end
            out.append(t[k + len(start):j].strip())
            i = t.find(start, j + len(end))
    return out


def extract_json_block(texts: list[str], start: str, end: str, accept) -> str | None:
    """LAST block that parses as JSON and passes accept(obj). Skill text quoting the markers is ignored."""
    for blk in reversed(extract_blocks(texts, start, end)):
        try:
            obj = json.loads(blk)
        except (json.JSONDecodeError, ValueError):
            continue
        if accept(obj):
            return blk
    return None


def extract_block(texts: list[str], start: str, end: str) -> str | None:
    """Return the LAST complete marker block found in any single string or in the concatenation."""
    found = None
    for t in texts + ["".join(texts)]:
        i = t.rfind(start)
        if i == -1:
            continue
        j = t.find(end, i + len(start))
        if j != -1:
            found = t[i + len(start):j].strip()
    return found


def skill_loaded(evidence: Any, skill_name: str) -> str:
    """Return 'true' / 'false' / 'unknown' from GET /api/agent-run/{id}/skills evidence."""
    hits: list[dict] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            names = " ".join(str(o.get(k, "")) for k in ("name", "skill_name", "slug", "display_name", "id"))
            if skill_name in names and ("loaded" in o or "decision" in o):
                hits.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(evidence)
    if not hits:
        return "unknown"
    return "true" if any(h.get("loaded") is True or str(h.get("decision")) == "selected" for h in hits) else "false"


def web_thread_url(cfg: dict, thread_id: str | None, project_id: str | None = None) -> str | None:
    """v1.5.4 (S6): link to a platform conversation. /thread/<id> alone is a 404 page; with the project id we link
    straight to /projects/<p>/thread/<id>, otherwise to the local review server's /go/thread/<id>, which looks the
    project up (dashboard) when you click and redirects."""
    if not thread_id or str(thread_id).startswith("fake"):
        return None
    base = str(cfg.get("base_url") or "https://sciencepal.ai").rstrip("/")
    if project_id:
        return f"{base}/projects/{project_id}/thread/{thread_id}"
    port = int((cfg.get("review") or {}).get("port", 8765))
    return f"http://127.0.0.1:{port}/go/thread/{thread_id}"
