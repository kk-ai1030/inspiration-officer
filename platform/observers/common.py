"""Shared helpers of the local observers: config, library roots, file classes, redaction, stable ids."""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

DEFAULTS: dict = {
    "enabled": True,
    "auto_scan_before_run": True,
    "roots": [
        {"path": "./library/code", "kind": "code"},
        {"path": "./library/notes", "kind": "notes"},
        {"path": "./library/papers", "kind": "papers"},
        {"path": "./library/data", "kind": "data"},
        {"path": "./library/logs", "kind": "logs"},
    ],
    "exclude_dirs": [".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "env", ".tox", "build",
                     "dist", ".ipynb_checkpoints", ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "site-packages"],
    "exclude_globs": ["*.min.js", "*.lock", "*secret*", "*password*", "*.key", "*.pem", ".env"],
    "max_files": 20000,
    "max_hash_mb": 20,
    "max_text_mb": 2,
    "upload_code_snippets": True,
    "snippet_lines": 40,
    "evidence_max_chars": 1800,
    "max_items": 80,
    "lookback_days": 30,
    "time_budget_s": 180,
    "code": {"long_function_lines": 80, "complexity": 15, "nesting": 5, "dup_window": 8, "dup_min_lines": 12,
             "churn_days": 90, "hotspot_min_commits": 5, "max_perf_items": 8,
             "max_per_file": 3, "max_args": 7, "max_smell_files": 5},
    "logs": {"tail_mb": 2, "max_files": 300},
    "data": {"max_mb": 5, "max_rows": 50000, "z": 8.0, "max_files": 200},
    "notes": {"per_file": 3, "max_items": 20, "scan_code_comments": True},
    "papers": {"mark_owned_as_seen": True, "top_terms": 15, "max_pdf": 400},
    "usage": {"days": 14, "min_count": 3, "conversations": True, "shell_history": False, "shell_history_path": None},
}

CODE_EXT = {".py", ".pyx", ".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
            ".hh", ".cu", ".cuh", ".jl", ".m", ".r", ".java", ".js", ".ts", ".go", ".rs", ".cs", ".sh", ".bash", ".ps1",
            ".gp", ".plt", ".nb", ".wl", ".mma", ".pro", ".tcl"}
NOTEBOOK_EXT = {".ipynb"}
NOTE_EXT = {".md", ".markdown", ".org", ".rst", ".tex", ".txt", ".docx", ".rtf"}
PAPER_EXT = {".pdf", ".bib", ".ris", ".enw", ".djvu"}
DATA_EXT = {".csv", ".tsv", ".dat", ".npy", ".npz", ".h5", ".hdf5", ".nc", ".parquet", ".xlsx", ".xls", ".mat", ".json",
            ".jsonl", ".xyz", ".cif", ".vasp", ".pkl", ".feather"}
LOG_EXT = {".log", ".out", ".err", ".stderr", ".stdout"}
FIG_EXT = {".png", ".jpg", ".jpeg", ".svg", ".eps", ".tif", ".tiff", ".gif", ".webp"}
TEXT_CLASSES = {"code", "note", "log", "data"}

SECRET_PATTERNS = [
    re.compile(r"sp_live_[A-Za-z0-9]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}(\.[A-Za-z0-9_-]+)?"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\b(ghp|gho|github_pat|glpat|xox[bpas])[_-][A-Za-z0-9_-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)((?:api[_-]?key|secret|token|passwd|password|pwd)\s*[=:]\s*)(['\"]?)[^\s'\"]{4,}\2"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(open\.feishu\.cn/open-apis/bot/v2/hook/|qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=)\S+"),
    re.compile(r"([A-Za-z]:\\+Users\\+)[^\\/\s]+"),
    re.compile(r"(/Users/|/home/)[a-z][A-Za-z0-9._-]+"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def iso_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Env:
    """Everything an observer needs: merged config, base dir (= folder of config.json), state dir, library dir."""

    def __init__(self, cfg_path: Path | None = None, cfg: dict | None = None, base: Path | None = None):
        self.cfg_path = Path(cfg_path).resolve() if cfg_path else None
        self.full = cfg if cfg is not None else (json.loads(self.cfg_path.read_text(encoding="utf-8")) if self.cfg_path else {})
        self.base = Path(base).resolve() if base else (self.cfg_path.parent if self.cfg_path else Path.cwd())
        self.cfg = deep_merge(DEFAULTS, self.full.get("observe") or {})
        self.state_dir = (self.base / self.full.get("state_dir", "./inspiration")).resolve()
        self.out = self.state_dir / "library"
        self.started = datetime.now().timestamp()

    def over_budget(self) -> bool:
        return datetime.now().timestamp() - self.started > float(self.cfg.get("time_budget_s", 180))

    def roots(self) -> list[dict]:
        out, used = [], set()
        for r in self.cfg.get("roots") or []:
            if not isinstance(r, dict) or not r.get("path"):
                continue
            p = Path(os.path.expandvars(os.path.expanduser(str(r["path"]))))
            p = (self.base / p).resolve() if not p.is_absolute() else p.resolve()
            label = str(r.get("label") or p.name or "root")
            k = 2
            while label in used:
                label, k = f"{r.get('label') or p.name}{k}", k + 1
            used.add(label)
            out.append({"path": p, "kind": str(r.get("kind", "mixed")), "label": label, "exists": p.is_dir()})
        return out

    def write_json(self, name: str, obj) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        p = self.out / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        return p

    def read_json(self, name: str, default=None):
        try:
            return json.loads((self.out / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default


def classify(path: Path, root_kind: str) -> str:
    ext = path.suffix.lower()
    if ext in NOTEBOOK_EXT:
        return "code"
    if ext == ".m" and root_kind in ("notes", "papers"):
        return "other"
    if ext in CODE_EXT:
        return "code"
    if ext in FIG_EXT:
        return "figure"
    if ext in LOG_EXT:
        return "log"
    if ext in PAPER_EXT:
        if ext == ".pdf" and root_kind not in ("papers", "mixed", "notes"):
            return "figure" if root_kind in ("code", "data") and re.search(r"(?i)(fig|plot|chart)", path.name) else "paper"
        return "paper"
    if ext == ".txt":
        return {"logs": "log", "data": "data", "papers": "paper_note"}.get(root_kind, "note")
    if ext in NOTE_EXT:
        return "paper_note" if root_kind == "papers" else "note"
    if ext in DATA_EXT:
        return "data"
    return "other"


def excluded(rel: str, name: str, globs: list[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, g.lower()) or fnmatch.fnmatch(rel.lower(), g.lower()) for g in globs)


def read_text(p: Path, max_bytes: int) -> str | None:
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            data = f.read(max_bytes)
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    txt = data.decode("utf-8", errors="replace")
    return txt if size else ""


def read_tail(p: Path, max_bytes: int) -> tuple[str, int]:
    """Last max_bytes of a (log) file and the 1-based line number of its first line (approximate when cut)."""
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                head_lines = 0
                f.seek(0)
                remaining = size - max_bytes
                while remaining > 0:  # count newlines before the tail so line numbers stay real
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    head_lines += chunk.count(b"\n")
                    remaining -= len(chunk)
                data = f.read()
                cut = data.find(b"\n")
                data = data[cut + 1:] if cut >= 0 else data
                first = head_lines + 2
            else:
                data, first = f.read(), 1
    except OSError:
        return "", 1
    return data.decode("utf-8", errors="replace"), first


def notebook_code(text: str) -> str:
    """Code cells of a Jupyter notebook joined with markers, so line numbers point at "cell k, line n"."""
    try:
        nb = json.loads(text)
    except json.JSONDecodeError:
        return ""
    out = []
    for i, c in enumerate(nb.get("cells", []), 1):
        if c.get("cell_type") == "code":
            src = c.get("source", "")
            src = "".join(src) if isinstance(src, list) else str(src)
            out.append(f"# %% cell {i}\n{src}\n")
    return "\n".join(out)


def redact(s: str) -> str:
    if not s:
        return s
    for pat in SECRET_PATTERNS:
        if pat.groups >= 2 and "passw" in pat.pattern:
            s = pat.sub(lambda m: m.group(1) + "«已脱敏»", s)
        elif pat.pattern.startswith("([A-Za-z]:") or pat.pattern.startswith("(/Users/"):
            s = pat.sub(lambda m: m.group(1) + "«用户»", s)
        else:
            s = pat.sub("«已脱敏»", s)
    return s


def obs_id(*parts: str) -> str:
    return "obs_" + hashlib.sha1("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:12]


def numbered(lines: list[str], start: int, width: int = 0) -> str:
    """Lines prefixed with their real line numbers (the prefix is part of the evidence: the agent may quote it)."""
    width = width or len(str(start + len(lines)))
    return "\n".join(f"{start + i:>{width}}| {ln.rstrip()}" for i, ln in enumerate(lines))


def clamp(x: float, lo: float = 1.0, hi: float = 5.0) -> float:
    return round(max(lo, min(hi, x)), 2)


def finding(detector: str, kind: str, ctype: str, title: str, evidence: str, severity: float, *, fp: str,
            allowed: list[str] | None = None, summary: str = "", ref: str | None = None, metrics: dict | None = None,
            subfield: str | None = None) -> dict:
    return {"id": obs_id(detector, fp), "kind": kind, "candidate_type": ctype, "allowed_types": allowed or [ctype],
            "severity": clamp(severity), "title": title.strip(), "summary": summary.strip(), "evidence": evidence,
            "evidence_ref": ref, "detector": detector, "metrics": metrics or {},
            "subfield": subfield or detector.split(".", 1)[0], "status": "open"}
