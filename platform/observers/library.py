"""Library manifest: what is in the user's material folders, and what changed since the last scan.

Writes (local only, never uploaded):
  inspiration/library/manifest.json   every file: ref, class, size, mtime, sha1, lines
  inspiration/library/changes.jsonl   added / modified / deleted between two scans (the "file churn" when there is no git)
  inspiration/library/summary.md      a human-readable inventory
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from .common import DEFAULTS, TEXT_CLASSES, Env, classify, excluded, iso_of, now_iso


def _sha1(p: Path, max_bytes: int) -> str | None:
    try:
        size = p.stat().st_size
        if size > max_bytes:
            return None
        h = hashlib.sha1()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _lines(p: Path, max_bytes: int) -> int | None:
    try:
        if p.stat().st_size > max_bytes:
            return None
        with p.open("rb") as f:
            return f.read().count(b"\n") + 1
    except OSError:
        return None


def scan(env: Env) -> dict:
    cfg = env.cfg
    # user lists ADD to the built-in ones (a user "exclude_globs": [] must never re-enable *secret* / *.key files)
    ex_dirs = {d.lower() for d in list(DEFAULTS["exclude_dirs"]) + list(cfg.get("exclude_dirs") or [])}
    globs = list(dict.fromkeys(list(DEFAULTS["exclude_globs"]) + list(cfg.get("exclude_globs") or [])))
    max_files = int(cfg.get("max_files", 20000))
    max_hash = int(float(cfg.get("max_hash_mb", 20)) * 1024 * 1024)
    max_text = int(float(cfg.get("max_text_mb", 2)) * 1024 * 1024)
    prev = {f["ref"]: f for f in (env.read_json("manifest.json", {}) or {}).get("files", [])}
    files: list[dict] = []
    roots_info = []
    truncated = False
    for r in env.roots():
        info = {"label": r["label"], "kind": r["kind"], "exists": r["exists"], "files": 0}
        roots_info.append(info)
        if not r["exists"]:
            continue
        for dirpath, dirnames, filenames in os.walk(r["path"]):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in ex_dirs and not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".") or name.startswith("~$"):
                    continue
                p = Path(dirpath) / name
                rel = p.relative_to(r["path"]).as_posix()
                if excluded(rel, name, globs):
                    continue
                if len(files) >= max_files:
                    truncated = True
                    break
                try:
                    st = p.stat()
                except OSError:
                    continue
                ref = f"library://{r['label']}/{rel}"
                cls = classify(p, r["kind"])
                old = prev.get(ref)
                same = old and old.get("size") == st.st_size and abs(float(old.get("mtime", 0)) - st.st_mtime) < 1e-3
                ent = {"ref": ref, "root": r["label"], "root_kind": r["kind"], "rel": rel, "cls": cls,
                       "ext": p.suffix.lower(), "size": st.st_size, "mtime": round(st.st_mtime, 3)}
                if same:  # unchanged: reuse hash / line count (fast rescans of big repos)
                    ent["sha1"], ent["lines"] = old.get("sha1"), old.get("lines")
                else:
                    ent["sha1"] = _sha1(p, max_hash)
                    ent["lines"] = _lines(p, max_text) if cls in TEXT_CLASSES or cls == "paper_note" else None
                files.append(ent)
                info["files"] += 1
            if truncated or env.over_budget():
                truncated = True
                break
    now = now_iso()
    cur = {f["ref"]: f for f in files}
    changes = []
    if prev:
        for ref, f in cur.items():
            o = prev.get(ref)
            if o is None:
                changes.append({"t": now, "ref": ref, "change": "added", "cls": f["cls"], "lines": f.get("lines")})
            elif (o.get("sha1") or f"{o.get('size')}:{o.get('mtime')}") != (f.get("sha1") or f"{f['size']}:{f['mtime']}"):
                d = (f.get("lines") or 0) - (o.get("lines") or 0) if f.get("lines") is not None and o.get("lines") is not None else None
                changes.append({"t": now, "ref": ref, "change": "modified", "cls": f["cls"], "lines_delta": d,
                                "mtime": iso_of(f["mtime"])})
        if not truncated:  # a truncated scan cannot tell "deleted" from "not reached"
            for ref, o in prev.items():
                if ref not in cur and any(ref.startswith(f"library://{ri['label']}/") for ri in roots_info if ri["exists"]):
                    changes.append({"t": now, "ref": ref, "change": "deleted", "cls": o.get("cls")})
    man = {"generated_at": now, "truncated": truncated, "roots": roots_info, "files": files}
    env.write_json("manifest.json", man)
    if changes:
        env.out.mkdir(parents=True, exist_ok=True)
        with (env.out / "changes.jsonl").open("a", encoding="utf-8") as fh:
            for c in changes:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    write_summary(env, man, changes, first=not prev)
    return {"manifest": man, "changes": changes, "first_scan": not prev}


def recent_changes(env: Env, days: float) -> list[dict]:
    p = env.out / "changes.jsonl"
    if not p.is_file():
        return []
    horizon = datetime.now().timestamp() - days * 86400
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            c = json.loads(ln)
            if datetime.fromisoformat(c["t"]).timestamp() >= horizon:
                out.append(c)
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
    return out


def write_summary(env: Env, man: dict, changes: list[dict], first: bool) -> None:
    by: dict[str, dict[str, int]] = {}
    for f in man["files"]:
        by.setdefault(f["root"], {}).setdefault(f["cls"], 0)
        by[f["root"]][f["cls"]] += 1
    L = [f"# 资料库清单 · {man['generated_at']}", "",
         "| 根目录 | 类型提示 | 存在 | 文件数 | 分类 |", "|---|---|---|---|---|"]
    for r in man["roots"]:
        cls = ", ".join(f"{k} {v}" for k, v in sorted(by.get(r["label"], {}).items()))
        L.append(f"| {r['label']} | {r['kind']} | {'是' if r['exists'] else '否（目录不存在）'} | {r['files']} | {cls or '—'} |")
    if man.get("truncated"):
        L += ["", "> 注意：文件数超过 max_files 或扫描超时，清单不完整；可以在 config.json → observe 里调大 max_files / time_budget_s。"]
    L += ["", "## 本次变化", ""]
    if first:
        L.append("首次扫描：建立基线（下次扫描起记录新增 / 修改 / 删除）。")
    elif not changes:
        L.append("无变化。")
    else:
        for c in changes[:40]:
            L.append(f"- {c['change']} · {c['ref']}" + (f"（行数 {c['lines_delta']:+d}）" if isinstance(c.get("lines_delta"), int) else ""))
        if len(changes) > 40:
            L.append(f"- …… 另有 {len(changes) - 40} 项，见 changes.jsonl")
    (env.out / "summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")
