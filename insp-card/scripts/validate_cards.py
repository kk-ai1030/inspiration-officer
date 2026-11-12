#!/usr/bin/env python3
"""Validate inspiration cards against the card contract, evidence, quotas and dedup state.

Usage:
    python3 validate_cards.py cards.json --triage triage.json --state-dir inspiration/ \
        [--expect-flow insp-card/1.0.0] [--today 2026-10-28] [--write-valid valid_cards.json]
    python3 validate_cards.py --self-test

Root-level errors are fatal (exit 1). Card-level errors mark that card invalid;
with --write-valid, the surviving cards are written out (exit 0 if root is valid).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_fulltext as FT  # noqa: E402

RELATIONS = {"supports", "contradicts", "extends", "method", "baseline", "context"}

FLOW_VERSION = "insp-card/1.1.0"  # 1.1.0: full-text evidence (fulltext_quotes + related_evidence)
TYPES = {"optimization", "knowledge", "hypothesis", "process"}
RISKS = {"low", "medium", "high"}
CARD_KEYS = [
    "id", "type", "created_at", "source", "title", "one_line", "why_me", "hypothesis",
    "minimal_check", "expected_observable", "falsifier", "magnitude", "cost_estimate",
    "rubric_scores", "numbers_provenance", "status", "verdict",
]
NUM_FIELDS = ["one_line", "why_me", "hypothesis", "magnitude"]
NUM_RE = re.compile(r"\d+(?:\.\d+)?\s?%|\d+\.\d+")


def norm_text(s: str) -> str:
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("...", " ").replace("…", " ")
    return " ".join(re.sub(r"[^\w%.'\-]+", " ", s.lower()).split())


def norm_id(x: str) -> str:
    return re.sub(r"v\d+$", "", re.sub(r"^arxiv:", "", str(x).strip(), flags=re.I))


def quote_in_abstract(quote: str, abstract: str) -> bool:
    """Every segment of the quote (split on ellipsis) must be a verbatim substring."""
    abs_n = norm_text(abstract)
    segs = [norm_text(s).strip(".-' ") for s in re.split(r"\.\.\.|…", quote) if norm_text(s).strip(".-' ")]
    return bool(segs) and all(seg in abs_n for seg in segs)


BRIEF_TEXT = ("scene", "method", "output", "baseline", "criterion")
BRIEF_SOURCES = {"library", "observed", "user"}


def brief_problems(b: Any, evidence_text: str, provenance: list) -> list[str]:
    """v1.5.1 optional card.action_brief (动手单). Same contract as platform/action_brief.py, plus: a library /
    observed parameter value must appear in the candidate's evidence or in numbers_provenance (no invented numbers).
    A bad brief never fails the card — the validator removes it and the review page derives one from the card."""
    if not isinstance(b, dict):
        return ["action_brief 必须是对象"]
    out = [f"{k} 为空" for k in BRIEF_TEXT if not str(b.get(k) or "").strip()]
    d = b.get("deliverables")
    if not (isinstance(d, list) and any(str(x).strip() for x in d)) and not (isinstance(d, str) and d.strip()):
        out.append("deliverables 为空")
    ps = b.get("params")
    if not isinstance(ps, list) or not ps:
        return out + ["params 至少 1 项"]
    prov = {str(x.get("value")) for x in provenance or [] if isinstance(x, dict)}
    ev = norm_text(evidence_text)
    for i, p in enumerate(ps, 1):
        if not isinstance(p, dict) or not str(p.get("name") or "").strip():
            out.append(f"params 第 {i} 项缺 name")
            continue
        src, val = str(p.get("source") or "").lower(), str(p.get("value") if p.get("value") is not None else "").strip()
        if src not in BRIEF_SOURCES:
            out.append(f"参数 {p['name']} 的 source={src!r}，只允许 library / observed / user")
        elif src != "user" and not val:
            out.append(f"参数 {p['name']} 来源 {src} 却没有数值")
        elif src != "user" and val not in prov and norm_text(val) not in ev:
            out.append(f"参数 {p['name']}={val} 在证据与 numbers_provenance 里都找不到")
        if val and not str(p.get("unit") or "").strip():
            out.append(f"参数 {p['name']} 缺 unit（没有量纲写“无量纲”）")
    return out


def has(v: Any) -> bool:
    return v is not None and (not isinstance(v, (str, list, dict)) or bool(v if not isinstance(v, str) else v.strip()))


def rubric_ids(path: Path) -> set[str]:
    return set(re.findall(r"^##\s+(R\d{2})\b", path.read_text(encoding="utf-8"), flags=re.M))


def window_count(quota_used: dict, today: date, days: int, ctype: str) -> int:
    total = 0
    for d_str, counts in quota_used.items():
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        if today - timedelta(days=days - 1) <= d <= today:
            total += int(counts.get(ctype, 0))
    return total


def remaining_quota(state: dict, today: date, prior_knowledge: int = 0) -> dict:
    """v1.5.7: how many more cards of each type THIS run may still keep — the same windows select() enforces.
    prepare prints it (REMAINING) so the agent writes at most that many instead of overshooting and deleting later.
    999 or more = no practical cap."""
    q = state.get("quota") or {}
    used = state.get("quota_used") or {}
    if state.get("quota_profile") == "test":
        qt = state.get("quota_test", {})
        per_type = int(qt.get("per_type_per_run", 2))
        today_total = sum(int(v) for v in used.get(today.isoformat(), {}).values())
        room = max(0, min(int(qt.get("per_run_max", 5)), int(qt.get("daily_total_max", 12)) - today_total))
        out = {t: min(per_type, room) for t in sorted(TYPES)}
        out["knowledge"] = min(out["knowledge"], max(0, int((q.get("knowledge") or {}).get("per_run", KNOWLEDGE_PER_RUN)) - prior_knowledge))
        out["_total"] = room
        return out
    win = {"optimization": (1, (q.get("optimization") or {}).get("per_day", 999)),
           "knowledge": (7, (q.get("knowledge") or {}).get("per_week", 3)),
           "hypothesis": (7, (q.get("hypothesis") or {}).get("per_week", 1)),
           "process": (14, (q.get("process") or {}).get("per_14_days", 2))}
    out = {t: max(0, int(lim) - window_count(used, today, days, t)) for t, (days, lim) in win.items()}
    out["knowledge"] = min(out["knowledge"], max(0, int((q.get("knowledge") or {}).get("per_run", KNOWLEDGE_PER_RUN)) - prior_knowledge))
    today_total = sum(int(v) for v in used.get(today.isoformat(), {}).values())
    day_room = max(0, int(q.get("daily_total_max", 3)) - today_total)
    for t in ("knowledge", "hypothesis", "process"):   # A 类不计入每日总配额
        out[t] = min(out[t], day_room)
    out["_non_optimization_total"] = day_room
    return out


LINE_PREFIX = re.compile(r"(?m)^[ \t]*\d{1,6}[ \t]*[|│:][ \t]?")


def strip_line_numbers(s: str) -> str:
    """v1.5.7: evidence files show code / notes with "  97| " line-number prefixes. A quote that spans two lines
    without the prefixes (or with them) is still verbatim — compare both sides without the prefixes."""
    return LINE_PREFIX.sub("", s or "")


def section_ok(section: str, sections: list[str]) -> bool:
    s = norm_text(section)
    return bool(s) and any(s == norm_text(h) or s in norm_text(h) or norm_text(h) in s for h in sections if norm_text(h))


def check_fulltext_evidence(card: dict, cand: dict, texts: dict, ctx: dict) -> list[str]:
    """v1.3 / insp-card 1.1.0: every card must be backed by the paper's full text AND related literature's full text."""
    e: list[str] = []
    ft = cand.get("fulltext") or {}
    if ft.get("status") != "ok" or not texts:
        if ctx.get("require_fulltext"):
            e.append("该候选没有拿到原文（只有摘要），不得出卡")
        return e
    src = card["source"]
    fq = src.get("fulltext_quotes")
    if not isinstance(fq, list) or not 1 <= len(fq) <= 4:
        return e + ["source.fulltext_quotes 必须是 1–4 条原文引文 {quote, section}"]
    body_hits = 0
    for x in fq:
        if not isinstance(x, dict) or not has(x.get("quote")) or not has(x.get("section")):
            e.append(f"fulltext_quotes 条目需要 quote 和 section：{x}")
            continue
        q, sec = str(x["quote"]), str(x["section"])
        wc = len(q.split())
        if wc < 8 or wc > 80:
            e.append(f"原文引文须 8–80 词（当前 {wc} 词）：{q[:40]}…")
        if not quote_in_abstract(q, texts["main"]):
            e.append(f"原文引文不是 ft_{norm_id(cand['id'])}.md 的逐字片段：{q[:60]}…")
            continue
        if FT.is_abstract(sec) or not section_ok(sec, ft.get("sections", [])):
            e.append(f"原文引文的 section「{sec}」必须是正文里真实存在的章节标题（不能是 Abstract）")
            continue
        if not quote_in_abstract(q, cand.get("abstract", "")):
            body_hits += 1
    if fq and body_hits == 0 and not e:
        e.append("fulltext_quotes 全部来自摘要：至少要有 1 条摘要里没有的正文引文（证明读过原文）")
    rel = card.get("related_evidence")
    have = {r["ref"]: r for r in ft.get("related", [])}
    need = min(int(ctx.get("min_related", 1)), len(have))
    if not isinstance(rel, list) or len(rel) < need:
        return e + [f"related_evidence 至少需要 {need} 条（附件里给了 {len(have)} 篇相关文献原文）"]
    for r in rel:
        if not isinstance(r, dict) or not all(has(r.get(k)) for k in ("ref", "quote", "relation", "why")):
            e.append(f"related_evidence 条目需要 ref / quote / relation / why：{r}")
            continue
        rr = norm_id(r["ref"])
        if rr not in have:
            e.append(f"related_evidence.ref {r['ref']} 不在附件提供的相关文献里（禁止引用附件以外的文献）")
            continue
        if r["relation"] not in RELATIONS:
            e.append(f"related_evidence.relation 必须属于 {sorted(RELATIONS)}")
        wc = len(str(r["quote"]).split())
        if wc < 6 or wc > 80:
            e.append(f"相关文献引文须 6–80 词（当前 {wc} 词）")
        if not quote_in_abstract(str(r["quote"]), texts["related"].get(rr, "")):
            e.append(f"相关文献引文不是 rel_{rr}.md 的逐字片段：{str(r['quote'])[:60]}…")
    return e


OBS_QUOTE_MIN, OBS_QUOTE_MAX = 6, 400  # characters (code / Chinese notes have no word spaces)


def check_observed(card: dict, cand: dict, quote: str) -> list[str]:
    """v1.5 observed card (source = a local finding from observed.jsonl, not a paper):
    source.kind must equal the candidate's kind, the card type must be one the detector allows, and the quote must be
    a verbatim piece (6–400 characters) of the evidence text that prepare printed into obs_<id>.md."""
    e: list[str] = []
    src = card["source"]
    if src.get("kind") != cand.get("kind"):
        e.append(f"观察型候选的 source.kind 必须是 {cand.get('kind')!r}（当前 {src.get('kind')!r}）")
    allowed = cand.get("allowed_types") or [cand.get("candidate_type")]
    if card.get("type") not in allowed:
        e.append(f"观察型候选 {cand['id']} 只能写成 {allowed} 类卡片（当前 {card.get('type')}）")
    n = len("".join(quote.split()))
    if n < OBS_QUOTE_MIN or n > OBS_QUOTE_MAX:
        e.append(f"观察型卡片的 quote 必须是 {OBS_QUOTE_MIN}–{OBS_QUOTE_MAX} 个字符的证据原文片段（当前 {n}）")
    elif not (quote_in_abstract(quote, cand.get("abstract", ""))
              or quote_in_abstract(strip_line_numbers(quote), strip_line_numbers(cand.get("abstract", "")))):
        e.append(f"quote 不是 obs_{cand['id'][4:]}.md 证据原文的逐字片段（代码、日志行、笔记原句都要原样复制）")
    return e


def validate_card(card: Any, ctx: dict) -> list[str]:
    e: list[str] = []
    if not isinstance(card, dict):
        return ["卡片必须是对象"]
    miss = [k for k in CARD_KEYS if k not in card]
    if miss:
        return ["缺少字段：" + ", ".join(miss)]
    if not re.fullmatch(r"ins_\d{8}_\d{3}", str(card["id"])):
        e.append("id 必须形如 ins_YYYYMMDD_NNN")
    if card["type"] not in TYPES:
        e.append(f"type 必须属于 {sorted(TYPES)}")
    for k in ["title", "one_line", "why_me", "minimal_check", "expected_observable", "falsifier", "magnitude"]:
        if not has(card[k]):
            e.append(f"{k} 不得为空")
    if card["type"] == "hypothesis" and not has(card["hypothesis"]):
        e.append("hypothesis 型卡片必须填写 hypothesis")
    if isinstance(card["one_line"], str) and len(card["one_line"]) > 40:
        e.append(f"one_line 超过 40 字（{len(card['one_line'])}）")
    if card["status"] != "proposed" or card["verdict"] is not None:
        e.append("status 必须是 proposed，verdict 必须是 null")
    ce = card["cost_estimate"]
    if not isinstance(ce, dict) or not all(has(ce.get(k)) for k in ("tokens", "wall_clock", "risk")):
        e.append("cost_estimate 需要 tokens / wall_clock / risk")
    elif ce["risk"] not in RISKS:
        e.append("cost_estimate.risk 必须是 low / medium / high")

    src = card["source"]
    cand = None
    if not isinstance(src, dict) or not all(k in src for k in ("kind", "ref", "quote", "verified")):
        e.append("source 需要 kind / ref / quote / verified")
    else:
        ref = norm_id(src["ref"])
        cand = ctx["shortlist"].get(ref)
        if cand is None:
            e.append(f"source.ref {src['ref']} 不在本次 triage.shortlisted 中（禁止引用候选以外的条目）")
        if ref in ctx["pushed_ids"]:
            e.append(f"source.ref {ref} 已推送过（重复）")
        if ref in ctx["neg_ids"]:
            e.append(f"source.ref {ref} 在负面清单中")
        if not isinstance(src["verified"], bool):
            e.append("source.verified 必须是布尔值")
        q = str(src["quote"])
        if cand is not None and cand.get("origin") == "observed":
            e.extend(check_observed(card, cand, q))
        else:
            wc = len(q.split())
            if wc < 6 or wc > 60:
                e.append(f"quote 必须是 6–60 词的逐字片段（当前 {wc} 词）")
            texts = (ctx.get("texts") or {}).get(ref) or {}
            if cand is not None and not quote_in_abstract(q, cand.get("abstract", "")) \
                    and not (texts and quote_in_abstract(q, texts["main"])):
                e.append("quote 不是摘要或原文的逐字片段")
            if cand is not None:
                e.extend(check_fulltext_evidence(card, cand, texts, ctx))

    rs = card["rubric_scores"]
    if not isinstance(rs, list) or not 3 <= len(rs) <= 5:
        e.append("rubric_scores 必须有 3–5 条逐条打分")
    else:
        for r in rs:
            if not isinstance(r, dict) or r.get("rule_id") not in ctx["rubric"]:
                e.append(f"rubric_scores 中的 rule_id 不存在于 rubric.md：{r}")
            elif not (isinstance(r.get("score"), int) and 1 <= r["score"] <= 5) or not has(r.get("reason")):
                e.append(f"rubric_scores {r.get('rule_id')} 需要 1–5 的整数分和理由")

    prov = card["numbers_provenance"]
    if not isinstance(prov, list):
        e.append("numbers_provenance 必须是数组（没有数字时为空数组）")
        prov = []
    prov_vals = set()
    for p in prov:
        if not isinstance(p, dict) or not has(p.get("value")) or not has(p.get("source")):
            e.append(f"numbers_provenance 条目需要 value 和 source：{p}")
            continue
        v = norm_text(str(p["value"]))
        if p["source"] in ("abstract", "observed"):  # observed candidates keep their evidence text in "abstract"
            if cand is not None and v not in norm_text(cand.get("abstract", "")):
                e.append(f"数字 {p['value']} 标注来自摘要，但摘要中找不到")
        elif str(p["source"]).startswith("fulltext:"):
            fr = norm_id(str(p["source"]).split(":", 1)[1])
            tx = (ctx.get("texts") or {}).get(norm_id(card["source"].get("ref", "")) if isinstance(card.get("source"), dict) else "", {})
            pool = tx.get("main", "") if fr == norm_id(card["source"].get("ref", "")) else tx.get("related", {}).get(fr, "")
            if v not in norm_text(pool):
                e.append(f"数字 {p['value']} 标注来自原文 {fr}，但原文中找不到")
        elif not str(p["source"]).startswith("executed:"):
            e.append(f"数字来源只能是 'abstract'、'observed'、'fulltext:<arXiv ID>' 或 'executed:<命令或文件>'：{p}")
        prov_vals.add(v)
    abstract_n = norm_text(cand.get("abstract", "")) if cand else ""
    for f in NUM_FIELDS:
        for num in NUM_RE.findall(str(card.get(f, ""))):
            n = norm_text(num)
            if n not in abstract_n and n not in prov_vals and n.replace(" ", "") not in prov_vals:
                e.append(f"{f} 中的数字 {num} 没有出处（既不在摘要里，也不在 numbers_provenance 里）")
    if cand is not None and cand.get("revival") and "复活" not in str(card["why_me"]):
        e.append("复活放行的候选，why_me 必须写明“复活”并提醒用户曾否决过该方向")
    return e


def card_features(card: dict, shortlist: dict, weights: dict) -> dict:
    """Features + weighted score used for selection. Deterministic, so it can be replayed with other weights."""
    cand = shortlist.get(norm_id(card.get("source", {}).get("ref", ""))) or {}
    sub = cand.get("subfield") or card.get("subfield") or "other"
    scores = [float(r.get("score", 3)) for r in card.get("rubric_scores", []) if isinstance(r, dict)
              and isinstance(r.get("score"), (int, float))]
    avg = sum(scores) / len(scores) if scores else 3.0
    wt = float((weights.get("type") or {}).get(card["type"], 1.0))
    ws = float((weights.get("subfield") or {}).get(sub, 1.0))
    return {"subfield": sub, "avg_rubric": round(avg, 3), "w_type": wt, "w_subfield": ws,
            "score": round(avg * wt * ws, 4)}


KNOWLEDGE_PER_RUN = 5   # v1.5.3: B 类（knowledge）每次运行最多 5 张；state.quota.knowledge.per_run 可改，A 类不受影响


def select(valid: list[dict], state: dict, weights: dict, shortlist: dict, today: date,
           prior_knowledge: int = 0) -> tuple[list[dict], list[dict]]:
    """Quota + weight-aware selection. Returns (kept, log). Cards are annotated with subfield/_selection.

    prod profile : rolling windows per type (design doc §8), ranked by weighted score.
    test profile : per-run slots (quota_test), exploration floor = best card of each type first,
                   remaining slots by weighted score. Used for the accelerated learning-loop experiment.
    """
    feats = {}
    for c in valid:
        f = card_features(c, shortlist, weights)
        feats[c["id"]] = f
        c["subfield"] = f["subfield"]
        c["_selection"] = f
    order = sorted(valid, key=lambda c: (-feats[c["id"]]["score"], c["id"]))
    q = state["quota"]
    used = state.get("quota_used", {})
    today_total = sum(int(v) for v in used.get(today.isoformat(), {}).values())
    log: list[dict] = []
    kept: list[dict] = []
    batch: dict[str, int] = {t: 0 for t in TYPES}
    # v1.5.4 (S7): the knowledge cap is per RUN, not per bundle — one run can be collected twice (two bundles from the
    # same conversation); prior_knowledge = knowledge cards this run already put into the library
    k_cap = int(q.get("knowledge", {}).get("per_run", KNOWLEDGE_PER_RUN))
    k_room = max(0, k_cap - int(prior_knowledge or 0))
    k_msg = f"超出 knowledge 本次上限（每次最多 {k_cap} 张" + (f"，这一轮之前已入库 {prior_knowledge} 张）" if prior_knowledge else "）")

    def entry(c: dict, ok: bool, reason: str) -> None:
        log.append({"card_id": c["id"], "type": c["type"], **feats[c["id"]], "kept": ok, "reason": reason})

    if state.get("quota_profile") == "test":
        qt = state.get("quota_test", {})
        per_run = int(qt.get("per_run_max", 5))
        per_type = int(qt.get("per_type_per_run", 2))
        day_max = int(qt.get("daily_total_max", 12))
        room = max(0, min(per_run, day_max - today_total))
        chosen: list[dict] = []
        if qt.get("explore_floor", True):  # one card per type first: no type is ever starved of data
            for t in sorted(TYPES):
                best = next((c for c in order if c["type"] == t), None)
                if best is not None and len(chosen) < room and (t != "knowledge" or k_room > 0):
                    chosen.append(best)
        for c in order:
            if c in chosen or len(chosen) >= room:
                continue
            cap = min(per_type, k_room) if c["type"] == "knowledge" else per_type
            if sum(1 for x in chosen if x["type"] == c["type"]) >= cap:
                continue
            chosen.append(c)
        for c in order:
            if c in chosen:
                kept.append(c)
                entry(c, True, "selected")
            else:
                full = len(chosen) >= room
                k_full = c["type"] == "knowledge" and sum(1 for x in chosen if x["type"] == "knowledge") >= k_room
                entry(c, False, k_msg if k_full and k_room < per_type else "超出本次运行名额 per_run_max/daily_total_max" if full else
                      f"超出 {c['type']} 本次运行上限 per_type_per_run={per_type}")
        kept.sort(key=lambda c: (-feats[c["id"]]["score"], c["id"]))
        return kept, log

    for card in order:
        t = card["type"]
        limits = {
            "optimization": (1, q["optimization"].get("per_day", 999)),   # v1.5.1: A has no hard cap by default (§8)
            "knowledge": (7, q["knowledge"].get("per_week", 3)),
            "hypothesis": (7, q["hypothesis"].get("per_week", 1)),
            "process": (14, q["process"].get("per_14_days", 2)),
        }[t]
        already = window_count(used, today, limits[0], t) + batch[t]
        if already + 1 > limits[1]:
            entry(card, False, f"超出 {t} 配额（{limits[0]} 天内上限 {limits[1]}）")
            continue
        if t == "knowledge" and batch[t] + 1 > k_room:   # v1.5.3 / v1.5.4: per run, including earlier bundles of this run
            entry(card, False, k_msg)
            continue
        if t != "optimization" and today_total + sum(batch.values()) + 1 > q.get("daily_total_max", 3):
            entry(card, False, "超出每日总配额 daily_total_max")
            continue
        batch[t] += 1
        kept.append(card)
        entry(card, True, "selected")
    return kept, log


def validate(cards_doc: Any, triage: dict, state_dir: Path, expect_flow: str, today: date | None,
             selection_log: list | None = None, fulltext_dir: Path | None = None, require_fulltext: bool = False,
             min_related: int = 1, prior_knowledge: int = 0) -> tuple[list[str], dict[str, list[str]], list[dict]]:
    root_errors: list[str] = []
    per_card: dict[str, list[str]] = {}
    if not isinstance(cards_doc, dict):
        return ["根节点必须是对象"], {}, []
    for k in ("schema_version", "flow_version", "run_id", "generated_at", "status", "cards", "rejected"):
        if k not in cards_doc:
            root_errors.append(f"根节点缺少 {k}")
    if root_errors:
        return root_errors, {}, []
    if cards_doc["flow_version"] != expect_flow:
        root_errors.append(f"flow_version={cards_doc['flow_version']!r}，期望 {expect_flow!r}：agent 很可能没有加载 insp-card 技能")
    if cards_doc["status"] not in {"OK", "NO_CANDIDATES"}:
        root_errors.append("status 必须是 OK 或 NO_CANDIDATES")
    if not isinstance(cards_doc["cards"], list) or not isinstance(cards_doc["rejected"], list):
        root_errors.append("cards 与 rejected 必须是数组")
        return root_errors, {}, []

    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    neg = [json.loads(l) for l in (state_dir / "negative.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    ctx = {
        "shortlist": {norm_id(c["id"]): c for c in triage.get("shortlisted", []) + triage.get("observed_shortlist", [])},
        "pushed_ids": {norm_id(p["id"]) for p in state.get("pushed", [])},
        "neg_ids": {norm_id(n["value"]) for n in neg if n.get("kind") == "id"},
        "rubric": rubric_ids(state_dir / "taste" / "rubric.md"),
        "texts": FT.load_texts(triage, fulltext_dir) if fulltext_dir else {},
        "require_fulltext": require_fulltext, "min_related": min_related,
    }
    seen_refs: set[str] = set()
    valid: list[dict] = []
    for i, card in enumerate(cards_doc["cards"]):
        cid = card.get("id", f"#{i}") if isinstance(card, dict) else f"#{i}"
        errs = validate_card(card, ctx)
        if isinstance(card, dict) and isinstance(card.get("source"), dict):
            r = norm_id(card["source"].get("ref", ""))
            if r in seen_refs:
                errs.append(f"同一批次重复引用 {r}")
            seen_refs.add(r)
        per_card[cid] = errs
        if not errs and "action_brief" in card:   # v1.5.1: optional; a bad one is removed, never fails the card
            cand = ctx["shortlist"].get(norm_id((card.get("source") or {}).get("ref", ""))) or {}
            bp = brief_problems(card["action_brief"], str(cand.get("abstract", "")) + " " + json.dumps(cand.get("metrics") or {}),
                                card.get("numbers_provenance") or [])
            if bp:
                card.pop("action_brief")
                card["_brief_dropped"] = "；".join(bp)[:400]
        if not errs:
            valid.append(card)

    weights = {}
    try:
        weights = json.loads((state_dir / "taste" / "weights.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    today = today or date.fromisoformat(str(cards_doc["generated_at"])[:10])
    kept, log = select(valid, state, weights, ctx["shortlist"], today, prior_knowledge)
    for row in log:
        if not row["kept"]:
            per_card[row["card_id"]].append(row["reason"])
    if selection_log is not None:
        selection_log.extend(log)
    for card in kept:
        attach_links(card, ctx["shortlist"].get(norm_id(card["source"].get("ref", ""))))
    return root_errors, per_card, kept


def attach_links(card: dict, cand: dict | None) -> None:
    """v1.4.1: every kept card carries clickable links, written by the script (model-written links are replaced).
    source.url = arXiv abstract page (both modes); source.read_mode = abstract | fulltext;
    source.fulltext_url = the HTML/PDF the full text was actually read from (fulltext mode only)."""
    src = card.get("source")
    if isinstance(src, dict) and (cand or {}).get("origin") == "observed":  # v1.5: local evidence, no web link
        for k in ("url", "pdf_url", "fulltext_url", "read_mode"):
            src.pop(k, None)
        src["verified"] = True  # the quote was just matched against the local evidence text
        src["evidence_ref"] = cand.get("evidence_ref")
        if cand.get("detector"):
            src["detector"] = cand.get("detector")       # v1.5.1: review page tag “本地观察 · 检测器”
        if cand.get("backfill"):
            src["backfill"] = True
        src["detector"] = cand.get("detector")
        src["read_mode"] = "observed"
        if cand.get("subfield") and not card.get("subfield"):
            card["subfield"] = cand["subfield"]
        return
    if not isinstance(src, dict) or not FT.is_arxiv_ref(src.get("ref", "")):
        return
    for k in ("url", "pdf_url", "fulltext_url", "read_mode"):
        src.pop(k, None)
    src.update(FT.arxiv_links(src["ref"], (cand or {}).get("fulltext")))
    for r in card.get("related_evidence") or []:
        if isinstance(r, dict) and FT.is_arxiv_ref(r.get("ref", "")):
            r["url"] = FT.arxiv_links(r["ref"])["url"]


def self_test() -> int:
    template = Path(__file__).resolve().parents[2] / "insp-init" / "assets" / "灵感状态模板" / "inspiration"
    abstract = ("We find that naive retrieval over raw history matches complex agentic memory systems, "
                "with gaps of 0.13 to 5.53 points across four benchmarks. These findings challenge the "
                "necessity of complex agentic memory and establish naive RAG as an essential baseline.")
    triage = {"shortlisted": [{"id": "9999.1001", "title": "Naive RAG", "abstract": abstract, "revival": False}]}
    good = {
        "id": "ins_20261028_001", "type": "knowledge", "created_at": "2026-10-28T02:50:00+08:00",
        "source": {"kind": "arxiv", "ref": "9999.1001v1",
                   "quote": "These findings challenge the necessity of complex agentic memory and establish naive RAG as an essential baseline.",
                   "verified": True},
        "title": "把原始历史+稠密检索设为记忆基线", "one_line": "复杂记忆前先跑朴素 RAG 基线",
        "why_me": "当前项目在设计经验记忆，缺少朴素基线。", "hypothesis": "",
        "minimal_check": "在现有 benchmark 上加一个 raw-history RAG 基线", "expected_observable": "差距小于 2 分则说明整合收益有限",
        "falsifier": "RAG 基线显著落后（超出 5.53 分）", "magnitude": "摘要报告差距 0.13–5.53 分",
        "cost_estimate": {"tokens": "~50k", "wall_clock": "1-2d", "risk": "low"},
        "rubric_scores": [{"rule_id": "R01", "score": 4, "reason": "直接相关"},
                          {"rule_id": "R02", "score": 4, "reason": "有数值"},
                          {"rule_id": "R07", "score": 5, "reason": "一次对比可判定"}],
        "numbers_provenance": [{"value": "0.13", "source": "abstract"}, {"value": "5.53", "source": "abstract"}],
        "status": "proposed", "verdict": None,
    }
    doc = {"schema_version": "1.0", "flow_version": FLOW_VERSION, "run_id": "r1",
           "generated_at": "2026-10-28T02:50:00+08:00", "status": "OK", "cards": [good], "rejected": []}
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp) / "inspiration"
        shutil.copytree(template, sd)
        root, per, kept = validate(doc, triage, sd, FLOW_VERSION, None)
        if root or per[good["id"]] or len(kept) != 1:
            print("SELF-TEST FAIL: 正例应通过", root, per)
            return 1
        ks = kept[0]["source"]
        if ks.get("url") != "https://arxiv.org/abs/9999.1001v1" or ks.get("read_mode") != "abstract" or "fulltext_url" in ks:
            print("SELF-TEST FAIL: 摘要模式卡片应由脚本补上摘要页链接", ks)
            return 1
        # v1.5.1 action_brief: good one kept, invented number → removed (card still passes)
        g2 = json.loads(json.dumps(good))
        g2["action_brief"] = {"scene": "记忆基线", "params": [{"name": "差距上界", "value": "5.53", "unit": "分", "source": "library"},
                                                         {"name": "重复次数", "value": "", "unit": "次", "source": "user"}],
                              "method": "跑 raw-history RAG", "output": "分数差", "baseline": "现有系统", "criterion": "差距 < 2 分",
                              "deliverables": ["report.md"]}
        d2 = dict(doc, cards=[g2])
        _, per2, kept2 = validate(d2, triage, sd, FLOW_VERSION, None)
        if per2[g2["id"]] or "action_brief" not in kept2[0]:
            print("SELF-TEST FAIL: 合格动手单应保留", per2)
            return 1
        g3 = json.loads(json.dumps(g2))
        g3["action_brief"]["params"][0]["value"] = "42.0"
        _, per3, kept3 = validate(dict(doc, cards=[g3]), triage, sd, FLOW_VERSION, None)
        if per3[g3["id"]] or "action_brief" in kept3[0] or "42.0" not in kept3[0].get("_brief_dropped", ""):
            print("SELF-TEST FAIL: 编造数字的动手单应被移除且卡片仍通过", per3, kept3[0].get("_brief_dropped"))
            return 1
        bad = json.loads(json.dumps(good))
        bad["id"] = "ins_20261028_002"
        bad["source"]["quote"] = "Naive RAG is always better than every memory system ever built."
        bad["magnitude"] = "快 10.5 倍"
        bad["falsifier"] = ""
        bad["rubric_scores"] = bad["rubric_scores"][:2]
        doc2 = dict(doc, cards=[bad])
        _, per2, kept2 = validate(doc2, triage, sd, FLOW_VERSION, None)
        msgs = " | ".join(per2[bad["id"]])
        for need in ("逐字片段", "10.5", "falsifier", "3–5"):
            if need not in msgs:
                print("SELF-TEST FAIL: 反例应报", need, msgs)
                return 1
        # --- v1.3 full-text evidence ---
        ftd = Path(tmp) / "ft"
        ftd.mkdir()
        body = ("## Abstract\n" + abstract + "\n## 4 Experiments\nRaw-RAG ranks first among the compared memory systems in "
                "six of seven benchmark settings while ingesting 12.4% of the tokens.\n")
        (ftd / "ft_9999.1001.md").write_text(body, encoding="utf-8")
        (ftd / "rel_9999.2002.md").write_text("## 3 Method\nWe consolidate memories into a graph and retrieve over "
                                               "the graph with a learned policy.\n", encoding="utf-8")
        tri_ft = {"shortlisted": [dict(triage["shortlisted"][0], fulltext={
            "status": "ok", "file": "ft_9999.1001.md", "sections": ["Abstract", "4 Experiments"],
            "related": [{"ref": "9999.2002", "file": "rel_9999.2002.md", "relation": "search"}]})]}
        g2 = json.loads(json.dumps(good))
        g2["source"]["fulltext_quotes"] = [{"quote": "Raw-RAG ranks first among the compared memory systems in six of seven benchmark settings",
                                            "section": "4 Experiments"}]
        g2["related_evidence"] = [{"ref": "9999.2002", "relation": "baseline", "why": "这就是要对比的复杂记忆做法",
                                   "quote": "We consolidate memories into a graph and retrieve over the graph"}]
        g2["magnitude"] = "正文：只用 12.4% 的 token"
        g2["numbers_provenance"].append({"value": "12.4%", "source": "fulltext:9999.1001"})
        _, perf, keptf = validate(dict(doc, cards=[g2]), tri_ft, sd, FLOW_VERSION, None, None, ftd, True)
        if perf[g2["id"]] or len(keptf) != 1:
            print("SELF-TEST FAIL: 原文证据正例应通过", perf)
            return 1
        _, perf0, _ = validate(doc, tri_ft, sd, FLOW_VERSION, None, None, ftd, True)
        if not any("fulltext_quotes" in m for m in perf0[good["id"]]):
            print("SELF-TEST FAIL: 没有原文引文应拦截", perf0)
            return 1
        b2 = json.loads(json.dumps(g2))
        b2["source"]["fulltext_quotes"] = [{"quote": "These findings challenge the necessity of complex agentic memory and establish",
                                            "section": "Abstract"}]
        b2["related_evidence"][0]["quote"] = "We invented this sentence and it is nowhere in the paper"
        _, perb, _ = validate(dict(doc, cards=[b2]), tri_ft, sd, FLOW_VERSION, None, None, ftd, True)
        mb = " | ".join(perb[b2["id"]])
        if "不能是 Abstract" not in mb or "rel_9999.2002.md" not in mb:
            print("SELF-TEST FAIL: 摘要冒充原文 / 编造相关文献引文应报错", mb)
            return 1
        tri_noft = {"shortlisted": [dict(triage["shortlisted"][0])]}
        _, pern, _ = validate(doc, tri_noft, sd, FLOW_VERSION, None, None, ftd, True)
        if not any("没有拿到原文" in m for m in pern[good["id"]]):
            print("SELF-TEST FAIL: 没有原文的候选应拦截", pern)
            return 1
        # v1.5 observed card: local evidence instead of a paper
        ev = ("代码扫描 · library://code/solver.py#L12-L140 · 函数 build_hamiltonian\n"
              "长度 129 行 · 圈复杂度 23 · 最大嵌套 6\n"
              "    for i in range(len(sites)):\n        for j in range(len(sites)):\n            H[i, j] = hop(sites[i], sites[j])")
        tri_o = {"shortlisted": [], "observed_shortlist": [
            {"id": "obs_0a1b2c3d4e5f", "origin": "observed", "kind": "workspace", "candidate_type": "optimization",
             "allowed_types": ["optimization"], "abstract": ev, "evidence_ref": "library://code/solver.py#L12-L140",
             "detector": "code.long_function", "subfield": "code"}]}
        oc = json.loads(json.dumps(good))
        oc.update(id="ins_20261028_005", type="optimization", magnitude="129 行、圈复杂度 23",
                  numbers_provenance=[{"value": "129", "source": "observed"}, {"value": "23", "source": "observed"}])
        oc["source"] = {"kind": "workspace", "ref": "obs_0a1b2c3d4e5f", "verified": False,
                        "quote": "for j in range(len(sites)):\n            H[i, j] = hop(sites[i], sites[j])"}
        _, pero, kepto = validate(dict(doc, cards=[oc]), tri_o, sd, FLOW_VERSION, None)
        so = kepto[0]["source"] if kepto else {}
        if pero[oc["id"]] or not kepto or so.get("verified") is not True or so.get("read_mode") != "observed" \
                or so.get("evidence_ref") != "library://code/solver.py#L12-L140" or "url" in so:
            print("SELF-TEST FAIL: 观察型正例应通过并由脚本补 evidence_ref", pero, so)
            return 1
        ob = json.loads(json.dumps(oc))
        ob["type"], ob["source"]["kind"], ob["source"]["quote"] = "hypothesis", "arxiv", "for k in range(n): H += 1"
        ob["hypothesis"] = "x"
        _, perob, _ = validate(dict(doc, cards=[ob]), tri_o, sd, FLOW_VERSION, None)
        mo = " | ".join(perob[ob["id"]])
        if "source.kind" not in mo or "只能写成" not in mo or "逐字片段" not in mo:
            print("SELF-TEST FAIL: 观察型反例（kind / 类型 / 编造引文）应报错", mo)
            return 1
        doc3 = dict(doc, flow_version="unknown")
        root3, _, _ = validate(doc3, triage, sd, FLOW_VERSION, None)
        if not any("没有加载" in r for r in root3):
            print("SELF-TEST FAIL: 金丝雀 flow_version 不匹配应报错")
            return 1
        st = json.loads((sd / "state.json").read_text(encoding="utf-8"))
        st["quota_used"] = {"2026-10-25": {"knowledge": 3}}
        st["quota"]["knowledge"]["per_week"] = 3  # v1.4 template ships 999 (= unlimited); test the mechanism itself
        (sd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        _, per4, kept4 = validate(doc, triage, sd, FLOW_VERSION, None)
        if kept4 or not any("配额" in m for m in per4[good["id"]]):
            print("SELF-TEST FAIL: knowledge 周配额已满应拦截", per4)
            return 1
        # v1.5.3: B 类每次最多 5 张；A 类不受影响
        mkc = lambda i, ty: {"id": f"ins_x_{i:02d}", "type": ty, "rubric_scores": [{"score": 5 - i % 3}], "source": {}}
        many = [mkc(i, "knowledge") for i in range(8)] + [mkc(10 + i, "optimization") for i in range(6)]
        stq = {"quota": {"daily_total_max": 999, "optimization": {"per_day": 999}, "knowledge": {"per_week": 999, "per_run": 5},
                         "hypothesis": {"per_week": 1}, "process": {"per_14_days": 2}}, "quota_used": {}}
        keptq, logq = select(many, stq, {}, {}, date(2026, 10, 31))
        nk = sum(1 for c in keptq if c["type"] == "knowledge")
        no = sum(1 for c in keptq if c["type"] == "optimization")
        if nk != 5 or no != 6 or not any("本次上限" in r["reason"] for r in logq if not r["kept"]):
            print("SELF-TEST FAIL: knowledge 每次应最多 5 张、optimization 不受影响", nk, no)
            return 1
        if sum(1 for c in select(many, stq, {}, {}, date(2026, 10, 31), prior_knowledge=5)[0] if c["type"] == "knowledge") != 0:
            print("SELF-TEST FAIL: 同一轮之前已入库 5 张 knowledge，第二个整包不应再收")
            return 1
        _k, _log = select(many, stq, {}, {}, date(2026, 10, 31), prior_knowledge=4)
        if sum(1 for c in _k if c["type"] == "knowledge") != 1 or not any("已入库 4 张" in r["reason"] for r in _log if not r["kept"]):
            print("SELF-TEST FAIL: 之前已入库 4 张时只能再收 1 张，并写明原因")
            return 1
        stq["quota"]["knowledge"].pop("per_run")   # old state.json without per_run → default 5
        if sum(1 for c in select(many, stq, {}, {}, date(2026, 10, 31))[0] if c["type"] == "knowledge") != 5:
            print("SELF-TEST FAIL: 旧 state.json 没有 per_run 时也应按 5 张")
            return 1
        stq.update(quota_profile="test", quota_test={"per_run_max": 20, "per_type_per_run": 9, "daily_total_max": 99})
        if sum(1 for c in select(many, stq, {}, {}, date(2026, 10, 31))[0] if c["type"] == "knowledge") != 5:
            print("SELF-TEST FAIL: 测试配置档里 knowledge 也应最多 5 张")
            return 1
    print("SELF-TEST OK: validate_cards")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="校验灵感卡片")
    ap.add_argument("cards", nargs="?", type=Path)
    ap.add_argument("--triage", type=Path)
    ap.add_argument("--state-dir", type=Path)
    ap.add_argument("--expect-flow", default=FLOW_VERSION)
    ap.add_argument("--today")
    ap.add_argument("--write-valid", type=Path)
    ap.add_argument("--fulltext-dir", type=Path, help="原文附件目录（含 ft_<id>.md / rel_<id>.md）；给了就逐字核对原文引文")
    ap.add_argument("--require-fulltext", action="store_true", help="没有原文的候选一律不通过（编排器的默认行为）")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not (a.cards and a.triage and a.state_dir):
        ap.error("需要 cards、--triage、--state-dir")
    try:
        doc = json.loads(a.cards.read_text(encoding="utf-8"))
        tri = json.loads(a.triage.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    today = date.fromisoformat(a.today) if a.today else None
    root, per, kept = validate(doc, tri, a.state_dir, a.expect_flow, today, None, a.fulltext_dir, a.require_fulltext)
    if root:
        print("INVALID (root):")
        for r in root:
            print(f"- {r}")
        return 1
    for cid, errs in per.items():
        print(f"{'OK     ' if not errs else 'INVALID'} {cid}")
        for x in errs:
            print(f"    - {x}")
    print(f"SUMMARY: {len(kept)}/{len(doc['cards'])} 张卡片通过")
    a_cand = [c for c in tri.get("observed_shortlist") or [] if c.get("candidate_type") == "optimization"]
    if a_cand and not any(c.get("type") == "optimization" for c in kept):
        print(f"WARNING A_FLOOR: 入围里有 {len(a_cand)} 条 A 类候选（{a_cand[0].get('id')} …），但通过的卡片里没有 A 类。"
              "按 insp-card 规则每轮至少出 1 张 A 类卡：请补一张后重跑本校验（只补这一张，别重写其他卡）。")
    for c in kept:
        if c.get("_brief_dropped"):
            print(f"NOTE {c['id']}: action_brief 不合格已移除（{c['_brief_dropped'][:120]}），评价页会按卡片自动生成动手单")
    if a.write_valid:
        out = dict(doc, cards=kept, validation={k: v for k, v in per.items() if v})
        a.write_valid.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if (a.write_valid or all(not v for v in per.values())) else 1


if __name__ == "__main__":
    raise SystemExit(main())
