#!/usr/bin/env python3
"""L0 + L1 of the inspiration funnel: incremental diff, dedup, hard gates, coarse scoring.

Pure script, zero LLM cost. Deterministic given the same inputs.

Usage:
    python3 triage.py --state-dir inspiration/ --raw raw_items.json --out triage.json
    python3 triage.py --state-dir inspiration/ --fetch-arxiv --save-raw raw_items.json --out triage.json
    python3 triage.py --self-test

raw_items.json: a JSON list of
    {"id": "2610.30755v1", "title": "...", "abstract": "...", "url": "...",
     "published": "2026-10-25", "source": "arxiv", "categories": ["cs.CL"]}

Exit codes: 0 ok · 2 bad input · 3 STATE_MISSING · 4 fetch failed (e.g. HTTP 429)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ARXIV_API = "https://export.arxiv.org/api/query"
ACTION_WORDS = ["code", "github", "open-source", "open source", "we release", "released",
                "benchmark", "dataset", "reproduc", "implementation", "toolkit"]
CHEAP_WORDS = ["training-free", "simple", "baseline", "plug-and-play", "lightweight",
               "without training", "naive", "zero-shot", "efficient"]
STOP = set("a an the of for and or to in on with by from is are we our this that via as at be".split())


def norm_id(raw_id: str) -> str:
    s = raw_id.strip()
    s = re.sub(r"^(arxiv:|https?://arxiv\.org/abs/)", "", s, flags=re.I)
    return re.sub(r"v\d+$", "", s)


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP and len(t) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def score_item(item: dict, keywords: list[str]) -> dict:
    title = item.get("title", "").lower()
    text = (item.get("title", "") + " " + item.get("abstract", "")).lower()
    matched = [k for k in keywords if k.lower() in text]
    in_title = sum(1 for k in keywords if k.lower() in title)
    hits = len(matched) + in_title
    relevance = 1 if hits == 0 else min(5, 2 + hits)
    act_hits = sum(1 for w in ACTION_WORDS if w in text)
    actionability = min(5, 2 + act_hits)
    cheap_hits = sum(1 for w in CHEAP_WORDS if w in text)
    cost = min(5, 2 + cheap_hits)  # 5 = cheapest
    total = round(0.5 * relevance + 0.3 * actionability + 0.2 * cost, 3)
    return {"relevance": relevance, "actionability": actionability, "cost": cost,
            "total": total, "matched_keywords": matched}


def assign_subfield(item: dict, subfields: dict[str, list[str]], keywords: list[str]) -> str:
    """Deterministic feature used by the learning loop: the keyword bucket with the most hits."""
    text = (item.get("title", "") + " " + item.get("abstract", "")).lower()
    buckets = subfields or {k: [k] for k in keywords}
    best, best_hits = "other", 0
    for name, kws in buckets.items():
        hits = sum(text.count(k.lower()) for k in kws)
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def load_weights(state_dir: Path) -> dict:
    try:
        return json.loads((state_dir / "taste" / "weights.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def run_triage(state_dir: Path, raw_items: list[dict], now: str | None = None,
               weights: dict | None = None) -> tuple[int, dict]:
    now = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    try:
        state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        negative = read_jsonl(state_dir / "negative.jsonl")
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return 3, {"status": "STATE_MISSING", "detail": str(exc)}
    if not state.get("seen_ids") and not state.get("bootstrap"):
        return 3, {"status": "STATE_MISSING", "detail": "seen_ids 为空且 bootstrap=false"}

    seen = {norm_id(x) for x in state.get("seen_ids", [])}
    # v1.5: papers already in the user's local library (observe.py papers) are never pushed as "new"
    seen |= {norm_id(x) for x in state.get("library_arxiv_ids", [])}
    pushed = state.get("pushed", [])
    pushed_ids = {norm_id(p["id"]) for p in pushed}
    neg_ids = {norm_id(n["value"]) for n in negative if n.get("kind") == "id"}
    neg_topics = [n["value"].lower() for n in negative if n.get("kind") == "topic"]
    tri = state.get("triage", {})
    min_rel = int(tri.get("min_relevance", 3))
    max_short = int(tri.get("max_shortlist", 8))
    sim_th = float(tri.get("title_similarity_threshold", 0.6))
    revival = dict(state.get("revival", {"every": 20, "counter": 0}))
    keywords = state.get("keywords", [])
    subfields = state.get("subfields") or {}
    max_new = tri.get("max_new_per_run")  # pacing: leave the rest unseen for the next run
    weights = load_weights(state_dir) if weights is None else weights
    w_sub = weights.get("subfield", {}) or {}

    counts = {"raw": len(raw_items), "seen": 0, "new": 0, "negative_filtered": 0,
              "gate_failed": 0, "similar_filtered": 0, "below_threshold": 0, "shortlisted": 0}
    if max_new is not None:
        counts["deferred"] = 0
    dropped: list[dict] = []
    survivors: list[dict] = []
    new_ids: list[str] = []
    batch_titles: list[tuple[str, str]] = []
    revived: list[str] = []

    for item in raw_items:
        iid = norm_id(str(item.get("id", "")))
        if not iid:
            counts["gate_failed"] += 1
            dropped.append({"id": None, "reason": "gate:missing_id"})
            continue
        if iid in seen:
            counts["seen"] += 1
            continue
        if max_new is not None and counts["new"] >= int(max_new):
            counts["deferred"] += 1
            continue
        counts["new"] += 1
        new_ids.append(iid)
        if iid in neg_ids or iid in pushed_ids:
            counts["negative_filtered"] += 1
            dropped.append({"id": iid, "reason": "negative:id" if iid in neg_ids else "already_pushed"})
            continue
        text = (item.get("title", "") + " " + item.get("abstract", "")).lower()
        hit_topic = next((t for t in neg_topics if t in text), None)
        is_revival = False
        if hit_topic:
            revival["counter"] = int(revival.get("counter", 0)) + 1
            if revival["counter"] % int(revival.get("every", 20)) == 0:
                is_revival = True
                revived.append(iid)
            else:
                counts["negative_filtered"] += 1
                dropped.append({"id": iid, "reason": f"negative:topic:{hit_topic}"})
                continue
        title, abstract = item.get("title", "").strip(), item.get("abstract", "").strip()
        if not title or len(abstract.split()) < 30:
            counts["gate_failed"] += 1
            dropped.append({"id": iid, "reason": "gate:no_title_or_short_abstract"})
            continue
        sim_hit = next((p for p in pushed if jaccard(title, p.get("title", "")) >= sim_th), None)
        batch_hit = next((bid for bid, bt in batch_titles if jaccard(title, bt) >= sim_th), None)
        if sim_hit or batch_hit:
            counts["similar_filtered"] += 1
            ref = sim_hit["card_id"] if sim_hit else batch_hit
            dropped.append({"id": iid, "reason": f"similar_to:{ref}"})
            continue
        batch_titles.append((iid, title))
        sc = score_item(item, keywords)
        sub = assign_subfield(item, subfields, keywords)
        w = float(w_sub.get(sub, 1.0))
        sc["subfield"], sc["w_subfield"], sc["final"] = sub, w, round(sc["total"] * w, 4)
        if sc["relevance"] < min_rel and not is_revival:
            counts["below_threshold"] += 1
            dropped.append({"id": iid, "reason": f"below_threshold:relevance={sc['relevance']}"})
            continue
        survivors.append({
            "id": iid, "raw_id": item.get("id"), "title": title, "abstract": abstract,
            "url": item.get("url") or f"https://arxiv.org/abs/{iid}",
            "published": item.get("published"), "source": item.get("source", "arxiv"),
            "scores": sc, "revival": is_revival, "subfield": sub,
        })

    survivors.sort(key=lambda x: (-x["scores"]["final"], x["id"]))
    shortlisted = survivors[:max_short]
    for extra in survivors[max_short:]:
        counts["below_threshold"] += 1
        dropped.append({"id": extra["id"], "reason": "over_shortlist_cap"})
    counts["shortlisted"] = len(shortlisted)
    obs_short, obs_counts, obs_dropped = observed_shortlist(state_dir, state, negative, w_sub)
    if obs_counts["observed"]:  # only when the observed channel has input: old count tables stay byte-identical
        counts.update(obs_counts)
    result = {
        "schema_version": "1.0",
        "generated_at": now,
        "status": "OK" if (shortlisted or obs_short) else "NO_CANDIDATES",
        "counts": counts,
        "new_ids": new_ids,
        "revival_state": revival,
        "revived_ids": revived,
        "weights_version": weights.get("version", 0),
        "ranked": [{"id": x["id"], "subfield": x["subfield"], "total": x["scores"]["total"],
                    "w_subfield": x["scores"]["w_subfield"], "final": x["scores"]["final"],
                    "shortlisted": i < max_short} for i, x in enumerate(survivors)],
        "shortlisted": shortlisted,
        "observed_shortlist": obs_short,
        "dropped": dropped + obs_dropped,
    }
    return 0, result


# ----------------------------------------------------------------------------- v1.5 observed channel
OBS_TYPES = ("optimization", "knowledge", "hypothesis", "process")
OBS_KINDS = ("workspace", "log", "anomaly")
OBS_DEFAULTS = {"enabled": True, "per_type": {"optimization": 3, "knowledge": 2, "hypothesis": 2, "process": 2},
                "max_total": 6, "min_severity": 2.0, "max_age_days": 45, "a_floor": True, "a_floor_min_severity": 1.0,
                "revive_after_days": 14, "revive_max": 2}
# v1.5.5 (P10): an A (optimization) finding that was pushed ≥ revive_after_days ago and is STILL in the code (the local
# scan found it again) may come back as "仍未修复：…" with a new id <obs_id>~r<k> (at most revive_max times; never after
# a reject). Without this the A channel dries up for good once each finding was shown once.
# v1.5.1 a_floor: every run keeps at least one A (optimization) candidate when one exists — a slot is reserved inside
# max_total, and when no A candidate reaches min_severity the best one ≥ a_floor_min_severity is back-filled (marked).
# per_type: one number for every type, or {type: n}. A (optimization) gets one more slot: design doc §8 — the daily
# value and the best training data; the weekly quota of validate_cards still caps what is actually pushed.


def _obs_entry(r: dict, w_sub: dict) -> dict:
    sub = str(r.get("subfield") or r.get("kind"))
    w = float(w_sub.get(sub, 1.0))
    allowed = [x for x in (r.get("allowed_types") or [r["candidate_type"]]) if x in OBS_TYPES] or [r["candidate_type"]]
    if r["candidate_type"] not in allowed:
        allowed.insert(0, r["candidate_type"])
    ent = {"id": str(r.get("id", "")), "origin": "observed", "kind": r["kind"], "candidate_type": r["candidate_type"],
           "allowed_types": allowed, "severity": round(float(r.get("severity", 0)), 2), "w_subfield": w,
           "final": round(float(r.get("severity", 0)) * w, 3), "title": str(r["title"]).strip(),
           "summary": str(r.get("summary", "")).strip(), "abstract": str(r["evidence"]),
           "evidence_ref": r.get("evidence_ref"), "detector": r.get("detector"), "subfield": sub,
           "metrics": r.get("metrics") or {}, "first_seen": r.get("first_seen"), "last_seen": r.get("last_seen"),
           "source": "observed", "revival": bool(r.get("revived_from"))}
    if r.get("revived_from"):
        ent["revived_from"] = r["revived_from"]
    return ent


def _revive_id(oid: str, r: dict, pushed: list, neg_ids: set, cfg: dict) -> str | None:
    try:
        days, most = float(cfg.get("revive_after_days", 14)), int(cfg.get("revive_max", 2))
    except (TypeError, ValueError):
        return None
    if days <= 0 or most <= 0 or r.get("candidate_type") != "optimization" or "~r" in oid:
        return None
    if any(n == oid or n.startswith(oid + "~r") for n in neg_ids):
        return None
    mine = [p for p in pushed if isinstance(p, dict) and (str(p.get("id")) == oid or str(p.get("id", "")).startswith(oid + "~r"))]
    k = sum(1 for p in mine if "~r" in str(p.get("id")))
    if k >= most:
        return None
    last = max((_iso_ts(p.get("pushed_at")) or 0 for p in mine), default=0)
    if not last or datetime.now(timezone.utc).timestamp() - last < days * 86400:
        return None
    return f"{oid}~r{k + 1}"


def observed_shortlist(state_dir: Path, state: dict, negative: list[dict], w_sub: dict | None = None) -> tuple[list, dict, list]:
    """Second channel next to arXiv: findings of the LOCAL observers (platform/observe.py) synced into the state
    dir as observed.jsonl. Read → drop pushed / negative / weak / stale → group by candidate_type → sort by
    severity (× learned subfield weight) → take the top per_type, at most max_total overall.

    Every entry keeps its evidence text in "abstract", so the card validator checks quotes and numbers exactly
    like it does for papers (the quote must be a verbatim piece of the local evidence)."""
    cfg = dict(OBS_DEFAULTS, **(state.get("observed") or {}))
    counts = {"observed": 0, "observed_filtered": 0, "observed_shortlisted": 0}
    of = state_dir / "observed.jsonl"
    rows, bad_lines = [], 0
    for ln in (of.read_text(encoding="utf-8").splitlines() if of.is_file() else []):
        if ln.strip():
            try:
                o = json.loads(ln)
                rows.append(o if isinstance(o, dict) else {})
            except json.JSONDecodeError:
                bad_lines += 1  # one damaged line must not kill the arXiv channel
                rows.append({})
    counts["observed"] = len(rows)
    if not cfg.get("enabled", True) or not rows:
        return [], counts, []
    w_sub = w_sub or {}
    pushed_ids = {norm_id(p.get("id", "")) for p in state.get("pushed", [])}
    neg_ids = {norm_id(n["value"]) for n in negative if n.get("kind") == "id"}
    neg_topics = [str(n["value"]).lower() for n in negative if n.get("kind") == "topic"]
    try:
        horizon = datetime.now(timezone.utc).timestamp() - float(cfg["max_age_days"]) * 86400
    except (TypeError, ValueError):
        horizon = 0
    dropped: list[dict] = []
    groups: dict[str, list[dict]] = {}
    a_backfill: list[dict] = []
    revived = 0
    for r in rows:
        oid = str(r.get("id", ""))
        why = None
        rv = _revive_id(oid, r, state.get("pushed", []), neg_ids, cfg) if oid in pushed_ids else None
        if rv:
            r = dict(r, id=rv, revived_from=oid, title="仍未修复：" + str(r.get("title", "")))
            oid = rv
            revived += 1
        if not oid.startswith("obs_") or r.get("kind") not in OBS_KINDS or r.get("candidate_type") not in OBS_TYPES \
                or not str(r.get("evidence", "")).strip() or not str(r.get("title", "")).strip():
            why = "observed:malformed"
        elif r.get("status", "open") != "open":
            why = f"observed:status={r.get('status')}"
        elif oid in neg_ids:          # v1.5.1: negative first — a rejected card must show WHY, not "already_pushed"
            why = "negative:id"
        elif oid in pushed_ids:
            why = "already_pushed"
        elif any(tp in (str(r.get("title", "")) + " " + str(r.get("summary", ""))).lower() for tp in neg_topics):
            why = "negative:topic"
        else:
            try:
                sev = float(r.get("severity", 0))
            except (TypeError, ValueError):
                sev = 0.0
            if sev < float(cfg["min_severity"]):
                why = f"observed:below_severity={sev}"
                ts = _iso_ts(r.get("last_seen"))
                if r.get("candidate_type") == "optimization" and sev >= float(cfg.get("a_floor_min_severity", 1.0)) \
                        and not (ts is not None and ts < horizon):
                    a_backfill.append(r)
            else:
                ts = _iso_ts(r.get("last_seen"))
                if ts is not None and ts < horizon:
                    why = "observed:stale"
        if why:
            counts["observed_filtered"] += 1
            dropped.append({"id": oid or None, "reason": why})
            continue
        ent = _obs_entry(r, w_sub)
        groups.setdefault(r["candidate_type"], []).append(ent)
    if revived:
        counts["observed_revived"] = revived
    picked: list[dict] = []
    pt = cfg["per_type"]
    for ctype in OBS_TYPES:
        try:
            per_type = max(0, int(pt.get(ctype, 2) if isinstance(pt, dict) else pt))
        except (TypeError, ValueError):
            per_type = 2
        g = sorted(groups.get(ctype, []), key=lambda x: (-x["final"], -x["severity"], str(x.get("last_seen") or ""), x["id"]))
        picked += g[:per_type]
        for extra in g[per_type:]:
            counts["observed_filtered"] += 1
            dropped.append({"id": extra["id"], "reason": "observed:over_per_type"})
    picked.sort(key=lambda x: (-x["final"], x["id"]))
    cap = max(0, int(cfg["max_total"]))
    for extra in picked[cap:]:
        counts["observed_filtered"] += 1
        dropped.append({"id": extra["id"], "reason": "observed:over_max_total"})
    kept_a = any(x["candidate_type"] == "optimization" for x in picked[:cap])
    if cfg.get("a_floor", True) and cap > 0 and not kept_a:
        cut_a = next((x for x in picked[cap:] if x["candidate_type"] == "optimization"), None)
        if cut_a is not None:              # an A candidate passed but lost the max_total cut → reserve the last slot
            picked.remove(cut_a)
            picked.insert(cap - 1, cut_a)
            counts["a_floor"] = "reserved"
        elif a_backfill:                   # no A candidate reached min_severity → back-fill the strongest one
            best = sorted(a_backfill, key=lambda r: (-float(r.get("severity", 0)), str(r.get("id"))))[0]
            ent = dict(_obs_entry(best, w_sub), backfill=True,
                       backfill_reason=f"本轮没有达到门槛的 A 类候选，按“每轮至少 1 张 A 类”补位（严重度 {round(float(best.get('severity', 0)), 2)} < 门槛 {cfg['min_severity']}）")
            picked.insert(min(cap - 1, len(picked)), ent)
            dropped[:] = [d for d in dropped if d.get("id") != best.get("id")]
            counts["observed_filtered"] -= 1
            counts["a_floor"] = "backfilled"
        else:
            counts["a_floor"] = "none_available"
    elif kept_a:
        counts["a_floor"] = "ok"
    for extra in picked[cap:]:
        if not any(d.get("id") == extra["id"] for d in dropped):
            counts["observed_filtered"] += 1
            dropped.append({"id": extra["id"], "reason": "observed:over_max_total"})
    picked = picked[:cap]
    counts["observed_shortlisted"] = len(picked)
    return picked, counts, dropped


def _iso_ts(s) -> float | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def fetch_arxiv(sources: list[dict], timeout: int = 30) -> list[dict]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items: list[dict] = []
    for src in sources:
        if src.get("kind") != "arxiv":
            continue
        q = urllib.parse.urlencode({
            "search_query": src["query"], "start": 0,
            "max_results": int(src.get("max_results", 50)),
            "sortBy": "submittedDate", "sortOrder": "descending",
        })
        req = urllib.request.Request(f"{ARXIV_API}?{q}", headers={"User-Agent": "insp-triage/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # may raise HTTPError 429
            root = ET.fromstring(resp.read())
        for e in root.findall("a:entry", ns):
            raw = (e.findtext("a:id", default="", namespaces=ns) or "").rsplit("/abs/", 1)[-1]
            items.append({
                "id": raw,
                "title": " ".join((e.findtext("a:title", default="", namespaces=ns)).split()),
                "abstract": " ".join((e.findtext("a:summary", default="", namespaces=ns)).split()),
                "url": f"https://arxiv.org/abs/{raw}",
                "published": (e.findtext("a:published", default="", namespaces=ns))[:10],
                "source": "arxiv",
            })
    seen, uniq = set(), []
    for it in items:
        k = norm_id(it["id"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq


def self_test() -> int:
    template = Path(__file__).resolve().parents[2] / "insp-init" / "assets" / "灵感状态模板" / "inspiration"
    filler = " ".join(["word"] * 35)
    raw = [
        {"id": f"9999.000{i}v1", "title": f"Old paper {i}", "abstract": filler} for i in range(3)
    ] + [
        {"id": "9999.1001v1", "title": "Naive RAG for agent memory", "abstract": "We study agent memory retrieval. Code released. " + filler},
        {"id": "9999.1002v1", "title": "Unrelated protein folding", "abstract": "Protein structures. " + filler},
        {"id": "9999.1003v2", "title": "Short", "abstract": "too short"},
        {"id": "9999.1004v1", "title": "Agent memory consolidation at scale", "abstract": "agent memory consolidation retrieval " + filler},
        {"id": "9999.1005v1", "title": "Naive RAG for agent memory systems", "abstract": "agent memory retrieval RAG " + filler},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp) / "inspiration"
        import shutil
        shutil.copytree(template, sd)
        st = json.loads((sd / "state.json").read_text(encoding="utf-8"))
        st["bootstrap"] = False
        st["seen_ids"] = [f"9999.000{i}" for i in range(3)]
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        (sd / "negative.jsonl").write_text(json.dumps({"kind": "topic", "value": "memory consolidation"}) + "\n", encoding="utf-8")
        code, res = run_triage(sd, raw, now="2026-10-28T02:40:00+08:00")
        c = res.get("counts", {})
        expect = {"raw": 8, "seen": 3, "new": 5, "negative_filtered": 1, "gate_failed": 1,
                  "similar_filtered": 1, "below_threshold": 1, "shortlisted": 1}
        if code != 0 or c != expect:
            print("SELF-TEST FAIL: counts", c, "expected", expect)
            return 1
        if res["shortlisted"][0]["id"] != "9999.1001":
            print("SELF-TEST FAIL: wrong shortlist", res["shortlisted"])
            return 1
        # same input twice after state update => zero new
        st["seen_ids"] += res["new_ids"]
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        code2, res2 = run_triage(sd, raw)
        if res2["counts"]["new"] != 0 or res2["status"] != "NO_CANDIDATES":
            print("SELF-TEST FAIL: rerun should yield zero new", res2["counts"])
            return 1
        # v1.5 observed channel: group by candidate_type, severity order, per_type cap, pushed / negative / weak dropped
        obs = [
            {"id": "obs_a1", "kind": "workspace", "candidate_type": "optimization", "severity": 4.5, "title": "long fn",
             "evidence": "def f(): pass", "subfield": "code"},
            {"id": "obs_a2", "kind": "workspace", "candidate_type": "optimization", "severity": 3.0, "title": "dup",
             "evidence": "x = 1", "subfield": "code"},
            {"id": "obs_a3", "kind": "workspace", "candidate_type": "optimization", "severity": 2.5, "title": "third",
             "evidence": "y = 2", "subfield": "code"},
            {"id": "obs_h1", "kind": "anomaly", "candidate_type": "hypothesis", "severity": 3.5, "title": "nan in log",
             "evidence": "loss = nan", "subfield": "logs"},
            {"id": "obs_w1", "kind": "log", "candidate_type": "knowledge", "severity": 1.0, "title": "weak", "evidence": "z"},
            {"id": "obs_p1", "kind": "workspace", "candidate_type": "optimization", "severity": 5, "title": "pushed", "evidence": "q"},
            {"id": "obs_n1", "kind": "log", "candidate_type": "process", "severity": 5, "title": "blocked", "evidence": "q"},
            {"id": "bad", "kind": "workspace", "candidate_type": "optimization", "severity": 5, "title": "x", "evidence": "q"},
        ]
        (sd / "observed.jsonl").write_text("".join(json.dumps(o) + "\n" for o in obs), encoding="utf-8")
        st["pushed"] = [{"id": "obs_p1", "title": "pushed"}]
        st["observed"] = {"per_type": {"optimization": 2}}  # dict form; the other types fall back to 2
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        (sd / "negative.jsonl").write_text(json.dumps({"kind": "id", "value": "obs_n1"}) + "\n", encoding="utf-8")
        _, r4 = run_triage(sd, [])
        ids = [x["id"] for x in r4["observed_shortlist"]]
        oc = {k: r4["counts"].get(k) for k in ("observed", "observed_filtered", "observed_shortlisted")}
        if ids != ["obs_a1", "obs_h1", "obs_a2"] or oc != {"observed": 8, "observed_filtered": 5, "observed_shortlisted": 3} \
                or r4["status"] != "OK" or r4["observed_shortlist"][0]["abstract"] != "def f(): pass":
            print("SELF-TEST FAIL: observed channel", ids, oc, r4["status"])
            return 1
        # v1.5.1: negative wins over pushed; a_floor reserve + backfill
        st["pushed"] = [{"id": "obs_p1"}, {"id": "obs_n1"}]
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        _, r6 = run_triage(sd, [])
        why = {d["id"]: d["reason"] for d in r6["dropped"]}
        if why.get("obs_n1") != "negative:id" or why.get("obs_p1") != "already_pushed":
            print("SELF-TEST FAIL: negative must be reported before already_pushed", why)
            return 1
        st["observed"] = {"per_type": {"optimization": 2}, "max_total": 1}
        obs2 = [o for o in obs if o["id"] != "obs_a1"] + [{"id": "obs_h2", "kind": "anomaly", "candidate_type": "hypothesis",
                                                           "severity": 4.9, "title": "big", "evidence": "e"}]
        (sd / "observed.jsonl").write_text("".join(json.dumps(o) + "\n" for o in obs2), encoding="utf-8")
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        _, r7 = run_triage(sd, [])
        if [x["id"] for x in r7["observed_shortlist"]] != ["obs_a2"] or r7["counts"].get("a_floor") != "reserved":
            print("SELF-TEST FAIL: a_floor reserve", [x["id"] for x in r7["observed_shortlist"]], r7["counts"])
            return 1
        st["observed"] = {"max_total": 3}
        obs3 = [{"id": "obs_lo", "kind": "workspace", "candidate_type": "optimization", "severity": 1.5, "title": "small", "evidence": "s"},
                {"id": "obs_h3", "kind": "anomaly", "candidate_type": "hypothesis", "severity": 4.0, "title": "h", "evidence": "e"}]
        (sd / "observed.jsonl").write_text("".join(json.dumps(o) + "\n" for o in obs3), encoding="utf-8")
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        _, r8 = run_triage(sd, [])
        sl = {x["id"]: x for x in r8["observed_shortlist"]}
        if "obs_lo" not in sl or not sl["obs_lo"].get("backfill") or r8["counts"].get("a_floor") != "backfilled" \
                or any(d["id"] == "obs_lo" for d in r8["dropped"]):
            print("SELF-TEST FAIL: a_floor backfill", list(sl), r8["counts"])
            return 1
        # v1.5.5 revival: A finding pushed 20 days ago and still present → obs_rv~r1; pushed 3 days ago → not yet
        _old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        _new = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        _rows = [{"id": "obs_rv", "kind": "workspace", "candidate_type": "optimization", "title": "loop reads file", "evidence": "x",
                  "severity": 3, "last_seen": datetime.now(timezone.utc).isoformat()}]
        _ps = [{"id": "obs_rv", "pushed_at": _old}]
        if _revive_id("obs_rv", _rows[0], _ps, set(), OBS_DEFAULTS) != "obs_rv~r1" \
                or _revive_id("obs_rv", _rows[0], [{"id": "obs_rv", "pushed_at": _new}], set(), OBS_DEFAULTS) is not None \
                or _revive_id("obs_rv", _rows[0], _ps, {"obs_rv~r1"}, OBS_DEFAULTS) is not None \
                or _revive_id("obs_rv", _rows[0], _ps + [{"id": "obs_rv~r1", "pushed_at": _old}, {"id": "obs_rv~r2", "pushed_at": _old}],
                              set(), OBS_DEFAULTS) is not None:
            print("SELF-TEST FAIL: observed revival")
            return 1
        st["observed"] = {"a_floor": False}
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        _, r9 = run_triage(sd, [])
        if any(x["id"] == "obs_lo" for x in r9["observed_shortlist"]):
            print("SELF-TEST FAIL: a_floor=false must not backfill")
            return 1
        st.pop("observed")
        st["library_arxiv_ids"] = ["9999.1001"]
        st["seen_ids"] = [f"9999.000{i}" for i in range(3)]
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        (sd / "observed.jsonl").unlink()
        _, r5 = run_triage(sd, raw)
        if any(x["id"] == "9999.1001" for x in r5["shortlisted"]) or "observed" in r5["counts"]:
            print("SELF-TEST FAIL: library_arxiv_ids must count as seen / no observed keys without observed.jsonl", r5["counts"])
            return 1
        # STATE_MISSING fuse
        (sd / "negative.jsonl").unlink()
        code3, res3 = run_triage(sd, raw)
        if code3 != 3 or res3["status"] != "STATE_MISSING":
            print("SELF-TEST FAIL: missing negative.jsonl must trip the fuse")
            return 1
    print("SELF-TEST OK: triage")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="灵感漏斗 L0+L1：增量、去重、硬门槛、粗打分")
    ap.add_argument("--state-dir", type=Path)
    ap.add_argument("--raw", type=Path, help="raw_items.json")
    ap.add_argument("--fetch-arxiv", action="store_true", help="按 state.sources 抓取 arXiv")
    ap.add_argument("--save-raw", type=Path, help="抓取结果另存为 raw_items.json")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.state_dir or not a.out or not (a.raw or a.fetch_arxiv):
        ap.error("需要 --state-dir、--out，以及 --raw 或 --fetch-arxiv")
    if a.fetch_arxiv:
        try:
            st = json.loads((a.state_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"STATE_MISSING: {exc}")
            return 3
        try:
            raw = fetch_arxiv(st.get("sources", []))
        except (urllib.error.URLError, ET.ParseError, TimeoutError) as exc:
            print(f"FETCH_FAILED: {exc}（arXiv 可能限流 429；稍后重试或改用 --raw）", file=sys.stderr)
            return 4
        if a.save_raw:
            a.save_raw.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        try:
            raw = json.loads(a.raw.read_text(encoding="utf-8"))
            assert isinstance(raw, list)
        except (OSError, json.JSONDecodeError, AssertionError) as exc:
            print(f"BAD_INPUT: {exc}", file=sys.stderr)
            return 2
    code, res = run_triage(a.state_dir, raw)
    if code == 3:
        print(f"STATE_MISSING: {res.get('detail')}")
        return 3
    a.out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    c = res["counts"]
    print(f"{res['status']}: raw={c['raw']} seen={c['seen']} new={c['new']} "
          f"negative={c['negative_filtered']} gate={c['gate_failed']} similar={c['similar_filtered']} "
          f"below={c['below_threshold']} shortlisted={c['shortlisted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
