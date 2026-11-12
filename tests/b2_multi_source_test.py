#!/usr/bin/env python3
"""B2 多来源 L0：离线回归（零联网）。

锁定三件事：
  1. 默认关闭时 prepare 输出与只有 arXiv 时一致（COUNTS / shortlisted / notes）；
  2. 开启后额外条目并入同一漏斗，跨源同 URL 去重，非 arXiv 条目不生成假 arxiv 链接；
  3. arxiv=false 时只走额外来源，不触发 arXiv 可达性检查。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK = True
ABSTRACT = " ".join(["agent memory retrieval RAG experience reuse measurement"] * 6)


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:300]) if detail and not cond else ""))
    OK &= bool(cond)


def jread(path: Path, default=None):
    if not path.is_file():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def arxiv_item(i: int, title: str) -> dict:
    return {"id": f"2611.1000{i}v1", "title": title, "abstract": ABSTRACT,
            "url": f"https://arxiv.org/abs/2611.1000{i}", "published": "2026-11-10", "source": "arxiv"}


def extra_items() -> list[dict]:
    return [
        {"id": "W1", "title": "Neutral atom array with Rydberg gates", "abstract": ABSTRACT,
         "url": "https://doi.org/10.1/xyz", "published": "2026-11-11", "source": "openalex"},
        {"id": "W2", "title": "Another decoder threshold paper", "abstract": ABSTRACT,
         "url": "https://doi.org/10.1/xyz", "published": "2026-11-11", "source": "openalex"},
        {"id": "r1", "title": "Surface code decoder threshold study", "abstract": ABSTRACT,
         "url": "https://example.org/note", "published": "2026-11-09", "source": "rss"},
    ]


def prepare(tmp: Path, tag: str, st: dict, *, raw=None, extra_raw=None) -> tuple[int, str, Path]:
    home, refs, out = tmp / tag / "home", tmp / tag / "refs", tmp / tag / "out"
    home.mkdir(parents=True, exist_ok=True)
    refs.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    (home / "state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    if raw is not None:
        (tmp / tag / "raw.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    if extra_raw is not None:
        (tmp / tag / "extra.json").write_text(json.dumps(extra_raw, ensure_ascii=False), encoding="utf-8")
    cmd = [sys.executable, "-X", "utf8", str(ROOT / "insp-nightly/scripts/insp_run.py"), "prepare",
           "--out", str(out), "--state-home", str(home), "--refs", str(refs)]
    if raw is not None:
        cmd += ["--raw", str(tmp / tag / "raw.json")]
    if extra_raw is not None:
        cmd += ["--extra-raw", str(tmp / tag / "extra.json")]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               INSP_WORKSPACE_ROOT=str(tmp / "no_workspace"))
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return r.returncode, r.stdout + r.stderr, out


def counts(log: str) -> dict:
    for line in log.splitlines():
        if line.startswith("COUNTS "):
            return json.loads(line[7:])
    return {}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_b2_"))
    try:
        xs = load("insp-nightly/scripts/extra_sources.py", "b2_extra_sources")
        check("B2-0 模块自测", xs.self_test() == 0)
        state = json.loads((ROOT / "insp-init/assets/灵感状态模板/inspiration/state.json").read_text(encoding="utf-8"))
        state["triage"]["min_relevance"] = 0
        state["triage"]["max_shortlist"] = 20
        raw = [arxiv_item(1, "Agent memory for long horizon retrieval"),
               arxiv_item(2, "Experience reuse in RAG pipelines")]

        print("== B2-1 默认关闭：arXiv 路径不变")
        code, log, out = prepare(tmp, "off", state, raw=raw)
        check("B2-1 prepare 成功", code == 0 and "STATE_MISSING" not in log and "FETCH_BLOCKED" not in log, log[-900:])
        doc = jread(out / "triage.json")
        raw_doc = jread(out / "raw_items.json", [])
        meta = jread(out / "run_meta.json")
        check("B2-1 raw 只有 arXiv 两条", len(raw_doc) == 2 and all(x["source"] == "arxiv" for x in raw_doc))
        check("B2-1 COUNTS 没有新增键", set(counts(log)) <= {"raw", "seen", "new", "negative_filtered", "gate_failed",
                                                          "similar_filtered", "below_threshold", "shortlisted",
                                                          "observed", "observed_source", "observed_missing"}, str(counts(log)))
        check("B2-1 notes 没有额外来源", not any("额外来源" in n for n in meta.get("notes", [])), str(meta.get("notes")))
        check("B2-1 shortlisted 全是 arXiv ID", all(s["id"].startswith("2611.") for s in doc.get("shortlisted", [])))

        print("== B2-2 叠加额外来源")
        extra = extra_items()
        code, log, out = prepare(tmp, "addon", state, raw=raw, extra_raw=extra)
        check("B2-2 prepare 成功", code == 0, log[-900:])
        raw_doc = jread(out / "raw_items.json", [])
        doc = jread(out / "triage.json")
        meta = jread(out / "run_meta.json")
        check("B2-2 raw = 2 arXiv + 2 额外（同 URL 已去重）", len(raw_doc) == 4, str(len(raw_doc)))
        ids = [s["id"] for s in doc.get("shortlisted", [])]
        check("B2-2 新来源进入短名单", any(i.startswith("oa:") for i in ids) and any(i.startswith("rss:") for i in ids), str(ids))
        foreign = [s for s in doc.get("shortlisted", []) if not s["id"].startswith("2611.")]
        check("B2-2 非 arXiv 条目不用假 arxiv 链接", bool(foreign) and all(
            not str(s.get("links", {}).get("url", "")).startswith("https://arxiv.org/abs/") for s in foreign), str(foreign))
        check("B2-2 notes 记录合入", any("额外来源" in n for n in meta.get("notes", [])), str(meta.get("notes")))

        print("== B2-3 arxiv=false：只走额外来源，不联网")
        st_only = json.loads(json.dumps(state, ensure_ascii=False))
        st_only["candidate_sources"] = {"enabled": True, "arxiv": False,
                                        "providers": [{"kind": "fixture", "path": "extra.json"}]}
        home = tmp / "only" / "home"
        home.mkdir(parents=True)
        (home / "extra.json").write_text(json.dumps(extra, ensure_ascii=False), encoding="utf-8")
        code, log, out = prepare(tmp, "only", st_only)
        check("B2-3 prepare 未崩溃", code == 0, log[-900:])
        raw_doc = jread(out / "raw_items.json", [])
        meta = jread(out / "run_meta.json")
        check("B2-3 没有 FETCH_BLOCKED", code == 0 and "FETCH_BLOCKED" not in log, log[-400:])
        check("B2-3 raw 只含 2 条额外来源", len(raw_doc) == 2 and all(x["id"].startswith(("oa:", "rss:")) for x in raw_doc),
              str(raw_doc))
        check("B2-3 notes 标明只走额外来源", any("只走额外候选来源" in n for n in meta.get("notes", [])), str(meta.get("notes")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("B2 OK" if OK else "B2 TESTS FAILED")
    return 0 if OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
