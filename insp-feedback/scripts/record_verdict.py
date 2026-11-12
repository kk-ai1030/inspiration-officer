#!/usr/bin/env python3
"""Record a user verdict on an inspiration card (append-only), and update the negative list.

Usage:
    python3 record_verdict.py --state-dir inspiration/ --card-id ins_20261028_001 --action adopt --reason "正好要做基线"
    python3 record_verdict.py --state-dir inspiration/ --card-id ins_20261028_002 --action reject \
        --reason "方向不感兴趣" --block-topic "memory consolidation"
    python3 record_verdict.py --state-dir inspiration/ --card-id ins_... --action edited --edited-file new_card.json
    python3 record_verdict.py --state-dir inspiration/ --batch inspiration/inbox/verdict_inbox.csv   # 批量：Excel 填 action 列
    python3 record_verdict.py --state-dir inspiration/ --rebuild-inbox                                 # 重建待评价清单
    python3 record_verdict.py --self-test
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import weights_config as WCFG  # noqa: E402

# Semantics of each action (explicit? which card status?). The NUMBERS ("weight") are only defaults:
# since v1.3 the authoritative event weights live in config.json -> weights.events (see weights_config.py).
ACTIONS = {
    "adopt":          {"weight": 1.0,  "explicit": True,  "status": "adopted",  "label": "采纳"},
    "start_now":      {"weight": 1.0,  "explicit": True,  "status": "adopted",  "label": "立刻动手"},
    "reject":         {"weight": -1.0, "explicit": True,  "status": "rejected", "label": "否决"},
    "done":           {"weight": -1.0, "explicit": True,  "status": "done",     "label": "已做过"},
    "not_important":  {"weight": -0.5, "explicit": True,  "status": "dismissed", "label": "已读但不重要"},
    "edited":         {"weight": 0.8,  "explicit": True,  "status": "edited",   "label": "改写了卡片"},
    "ask_detail":     {"weight": 0.5,  "explicit": True,  "status": None,       "label": "追问细节"},
    "read_no_action": {"weight": -0.2, "explicit": False, "status": None,       "label": "已读无动作"},
    "unread":         {"weight": -0.1, "explicit": False, "status": None,       "label": "未读"},
    # Undo the card's latest explicit verdict (card UI "Z"). Append-only: the earlier row stays, readers skip it.
    "retract":        {"weight": 0.0,  "explicit": False, "status": "proposed", "label": "撤销上一条评价"},
}

_CFG: dict = {"path": None, "events": None, "source": None}


def set_config(path: Path | str | None) -> None:
    """Called by orchestrators (run_local / review_server / CLI --config). Reloads on next use."""
    _CFG.update(path=path, events=None, source=None)


def event_weight(action: str) -> tuple[float, str]:
    if action == "retract":
        return 0.0, "fixed"
    if _CFG["events"] is None:
        block, src = WCFG.load(_CFG["path"], quiet=True)
        _CFG["events"], _CFG["source"] = block["events"], src["events"]
    return float(_CFG["events"][action]), str(_CFG["source"])


EDITABLE_FIELDS = ("title", "one_line", "why_me", "hypothesis", "minimal_check", "expected_observable",
                   "falsifier", "magnitude")


def read_verdicts(state_dir: Path) -> list[dict]:
    p = state_dir / "verdicts.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def verdict_stacks(rows: list[dict]) -> dict[str, list[dict]]:
    """card_id -> its explicit verdicts in order, with retracted ones removed (each retract pops the latest)."""
    stacks: dict[str, list[dict]] = {}
    for v in rows:
        cid = v.get("card_id")
        if v.get("action") == "retract":
            if stacks.get(cid):
                stacks[cid].pop()
        elif v.get("explicit"):
            stacks.setdefault(cid, []).append(v)
    return {k: st for k, st in stacks.items() if st}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def find_card(state_dir: Path, card_id: str) -> tuple[Path | None, dict | None]:
    for f in sorted((state_dir / "cards").glob("*/cards.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        for c in doc.get("cards", []):
            if c.get("id") == card_id:
                return f, c
    return None, None


def record(state_dir: Path, card_id: str, action: str, reason: str = "", block_topic: str | None = None,
           edited_file: Path | None = None, ts: str | None = None, edited_card: dict | None = None,
           via: str | None = None) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"未知 action：{action}，可选 {sorted(ACTIONS)}")
    spec = ACTIONS[action]
    ts = ts or now_iso()
    cards_file, card = find_card(state_dir, card_id)
    if card is None:
        raise LookupError(f"在 {state_dir}/cards 下找不到卡片 {card_id}")
    weight, weight_source = event_weight(action)
    entry = {
        "ts": ts, "card_id": card_id, "source_ref": card["source"]["ref"], "type": card["type"],
        "action": action, "label": spec["label"], "weight": weight, "weight_source": weight_source,
        "explicit": spec["explicit"], "reason": reason,
        "rubric_rule_ids": [r.get("rule_id") for r in card.get("rubric_scores", [])],
        "subfield": card.get("subfield"),
        "weights_version": (card.get("_trace") or {}).get("weights_version"),
    }
    if via:
        entry["via"] = via
    retracted = None
    if action == "retract":
        st = verdict_stacks(read_verdicts(state_dir)).get(card_id) or []
        if not st:
            raise LookupError(f"卡片 {card_id} 没有可撤销的显式评价")
        retracted = st[-1]
        entry["retracts"] = {"ts": retracted["ts"], "action": retracted["action"]}
    if action == "edited":
        if edited_card is None:
            if not edited_file:
                raise ValueError("action=edited 需要 --edited-file（改写后的卡片 JSON）")
            edited_card = json.loads(edited_file.read_text(encoding="utf-8"))
        base = dict(card, **(card.get("_edited") or {}))  # diff against what the user saw (incl. earlier edits)
        entry["edit_diff"] = {k: {"before": base.get(k), "after": edited_card.get(k)}
                              for k in edited_card if edited_card.get(k) != base.get(k) and k not in ("status", "verdict")}
        if not entry["edit_diff"]:
            raise ValueError("改写内容与原卡片相同，没有记录")
        edits = state_dir / "edits"
        edits.mkdir(exist_ok=True)
        (edits / f"{card_id}_{ts.replace(':', '').replace('+', 'p')}.json").write_text(
            json.dumps(dict(base, **edited_card), ensure_ascii=False, indent=2), encoding="utf-8")
    with (state_dir / "verdicts.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    neg_rows = []
    if action in ("reject", "done"):
        neg_rows.append({"kind": "id", "value": card["source"]["ref"], "reason": spec["label"] + ("：" + reason if reason else ""),
                         "added_at": ts, "from_card": card_id})
    if block_topic:
        neg_rows.append({"kind": "topic", "value": block_topic, "reason": reason or "用户屏蔽主题",
                         "added_at": ts, "from_card": card_id})
    if neg_rows:
        with (state_dir / "negative.jsonl").open("a", encoding="utf-8") as f:
            for r in neg_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if retracted is not None:  # undo the negative-list rows that verdict added
        neg = state_dir / "negative.jsonl"
        if neg.exists():
            keep = [l for l in neg.read_text(encoding="utf-8").splitlines() if l.strip()
                    and not (json.loads(l).get("from_card") == card_id and json.loads(l).get("added_at") == retracted["ts"])]
            neg.write_text("".join(l + "\n" for l in keep), encoding="utf-8")
    if spec["status"] and cards_file is not None:
        doc = json.loads(cards_file.read_text(encoding="utf-8"))
        for c in doc["cards"]:
            if c["id"] == card_id:
                if action == "retract":
                    rest = (verdict_stacks(read_verdicts(state_dir)).get(card_id) or [])
                    prev = rest[-1] if rest else None
                    c["status"] = ACTIONS[prev["action"]]["status"] or "proposed" if prev else "proposed"
                    c["verdict"] = {"action": prev["action"], "reason": prev.get("reason", ""), "ts": prev["ts"]} if prev else None
                else:
                    cls = [v for v in (verdict_stacks(read_verdicts(state_dir)).get(card_id) or [])
                           if v.get("action") not in ("edited", "ask_detail")]
                    if action == "edited" and cls:   # v1.5.1: editing an already-classified card (history view) keeps its classification
                        prev = cls[-1]
                        c["status"] = ACTIONS[prev["action"]]["status"] or c.get("status")
                        c["verdict"] = {"action": prev["action"], "reason": prev.get("reason", ""), "ts": prev["ts"]}
                    else:
                        c["status"] = spec["status"]
                        c["verdict"] = {"action": action, "reason": reason, "ts": ts}
                if action == "edited":
                    c["_edited"] = dict(c.get("_edited") or {}, **{k: v["after"] for k, v in entry["edit_diff"].items()})
        cards_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    if action in CANCEL_ON and via != "card_ui":   # the review page cancels by itself (and shows it)
        cancelled = cancel_pending(state_dir, card_id, action)
        if cancelled:
            return dict(entry, cancelled=cancelled)
    return entry


CANCEL_ON = {"reject": "评价改为否决", "done": "评价改为已做过", "not_important": "评价改为不重要"}


def cancel_pending(state_dir: Path, card_id: str, action: str) -> dict:
    """v1.5.4 (S4b): a reject / done / not_important recorded OUTSIDE the review page (CLI, Excel inbox, run_local)
    also cancels this card's unfinished 立刻动手 jobs and 追问 — before, only the review page did. Needs the local
    platform/ folder; inside the platform sandbox (skill copy only) this is a no-op."""
    import importlib.util
    plat = Path(__file__).resolve().parents[2] / "platform"
    out: dict = {}
    if not (plat / "run_action.py").is_file():
        return out
    why = CANCEL_ON[action]

    def load(name: str):
        spec = importlib.util.spec_from_file_location(f"{name}_rv", plat / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    errs = []
    try:
        if (state_dir / "actions").is_dir():
            ra = load("run_action")
            cfg_path = Path(_CFG["path"]) if _CFG.get("path") else state_dir.resolve().parent / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.is_file() else None
            jobs = ra.cancel_for_card(state_dir, card_id, cfg, cfg_path if cfg else None, why=why)
            if jobs:
                out["jobs"] = jobs
    except Exception as exc:  # noqa: BLE001 - the verdict is already saved
        errs.append(f"立刻动手 {type(exc).__name__}: {str(exc)[:160]}")
    try:
        if (state_dir / "asks").is_dir() and (plat / "ask_detail.py").is_file():
            asks = load("ask_detail").cancel_for_card(state_dir, card_id, why)
            if asks:
                out["asks"] = asks
    except Exception as exc:  # noqa: BLE001
        errs.append(f"追问 {type(exc).__name__}: {str(exc)[:160]}")
    if errs:
        out["error"] = "；".join(errs)
    return out


ALIASES = {"采纳": "adopt", "立刻动手": "start_now", "立刻做": "start_now", "否决": "reject", "已做过": "done",
           "不重要": "not_important", "已读但不重要": "not_important", "追问": "ask_detail", "追问细节": "ask_detail"}
INBOX_COLS = ["card_id", "date", "type", "subfield", "title", "one_line", "minimal_check", "action", "reason", "block_topic"]


def read_csv_any(path: Path) -> list[dict]:
    """Excel on Windows may save as UTF-8 (with/without BOM) or GBK; accept all three."""
    import csv
    import io
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path} 不是 UTF-8 或 GBK 编码")


def judged_ids(state_dir: Path) -> set[str]:
    return {cid for cid, st in verdict_stacks(read_verdicts(state_dir)).items()
            if any(v.get("action") != "ask_detail" for v in st)}


def rebuild_inbox(state_dir: Path) -> Path | None:
    """Rewrite inbox/verdict_inbox.csv = all cards without an explicit verdict; keeps cells the user already filled."""
    import csv
    p = state_dir / "inbox" / "verdict_inbox.csv"
    p.parent.mkdir(exist_ok=True)
    filled = {}
    if p.exists():
        try:
            filled = {r.get("card_id"): r for r in read_csv_any(p)}
        except (ValueError, OSError):
            filled = {}
    done = judged_ids(state_dir)
    rows = []
    for cf in sorted((state_dir / "cards").glob("*/cards.json")):
        for c in json.loads(cf.read_text(encoding="utf-8")).get("cards", []):
            if c["id"] in done:
                continue
            old = filled.get(c["id"], {})
            rows.append([c["id"], cf.parent.name, c.get("type"), c.get("subfield", ""), c.get("title"), c.get("one_line"),
                         c.get("minimal_check"), old.get("action", ""), old.get("reason", ""), old.get("block_topic", "")])
    try:
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(INBOX_COLS)
            w.writerows(rows)
        return p
    except PermissionError:  # file open in Excel on Windows
        alt = p.with_name(f"verdict_inbox_{datetime.now().strftime('%H%M%S')}.csv")
        with alt.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(INBOX_COLS)
            w.writerows(rows)
        print(f"WARN: {p.name} 被占用（Excel 打开中？），已写到 {alt.name}")
        return alt


def record_batch(state_dir: Path, csv_path: Path) -> tuple[int, int, list[str]]:
    ok, skipped, errors = 0, 0, []
    done = judged_ids(state_dir)
    for i, r in enumerate(read_csv_any(csv_path), start=2):
        act = (r.get("action") or "").strip()
        if not act:
            continue
        act = ALIASES.get(act, act)
        cid = (r.get("card_id") or "").strip()
        if cid in done and act != "ask_detail":
            skipped += 1
            continue
        if act == "edited":
            errors.append(f"第 {i} 行 {cid}: 改写请用 --action edited --edited-file 单独记录")
            continue
        try:
            record(state_dir, cid, act, (r.get("reason") or "").strip(), (r.get("block_topic") or "").strip() or None)
            done.add(cid)
            ok += 1
        except (ValueError, LookupError) as exc:
            errors.append(f"第 {i} 行 {cid}: {exc}")
    return ok, skipped, errors


def self_test() -> int:
    template = Path(__file__).resolve().parents[2] / "insp-init" / "assets" / "灵感状态模板" / "inspiration"
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp) / "inspiration"
        shutil.copytree(template, sd)
        (sd / "cards" / "2026-10-28").mkdir(parents=True)
        card = {"id": "ins_20261028_001", "type": "knowledge", "source": {"ref": "9999.1001v1"},
                "rubric_scores": [{"rule_id": "R01"}], "status": "proposed", "verdict": None, "one_line": "x"}
        (sd / "cards" / "2026-10-28" / "cards.json").write_text(json.dumps({"cards": [card]}), encoding="utf-8")
        record(sd, "ins_20261028_001", "reject", "不感兴趣", block_topic="memory consolidation", ts="2026-10-28T08:10:00+08:00")
        neg = [json.loads(l) for l in (sd / "negative.jsonl").read_text(encoding="utf-8").splitlines()]
        ver = [json.loads(l) for l in (sd / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()]
        doc = json.loads((sd / "cards" / "2026-10-28" / "cards.json").read_text(encoding="utf-8"))
        if [n["kind"] for n in neg] != ["id", "topic"] or ver[0]["weight"] != -1.0 or doc["cards"][0]["status"] != "rejected":
            print("SELF-TEST FAIL", neg, ver, doc)
            return 1
        try:
            record(sd, "ins_nope", "adopt")
            print("SELF-TEST FAIL: unknown card should raise")
            return 1
        except LookupError:
            pass
        card2 = dict(card, id="ins_20261028_002", source={"ref": "9999.1002"}, subfield="memory", type="hypothesis")
        doc["cards"].append(card2)
        (sd / "cards" / "2026-10-28" / "cards.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        inbox = rebuild_inbox(sd)
        rows = read_csv_any(inbox)
        if [r["card_id"] for r in rows] != ["ins_20261028_002"]:
            print("SELF-TEST FAIL: inbox should only list unjudged cards", rows)
            return 1
        txt = inbox.read_text(encoding="utf-8-sig").replace("ins_20261028_002,2026-10-28,hypothesis,memory,", "ins_20261028_002,2026-10-28,hypothesis,memory,", 1)
        lines = txt.splitlines()
        cols = lines[1].split(",")
        cols[7], cols[8] = "采纳", "好"
        lines[1] = ",".join(cols)
        inbox.write_bytes(("\r\n".join(lines) + "\r\n").encode("gbk"))  # simulate Excel ANSI save
        ok, skipped, errs = record_batch(sd, inbox)
        ver = [json.loads(l) for l in (sd / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()]
        if ok != 1 or errs or ver[-1]["action"] != "adopt" or ver[-1]["subfield"] != "memory":
            print("SELF-TEST FAIL: batch", ok, errs, ver[-1])
            return 1
        if record_batch(sd, inbox)[0] != 0:
            print("SELF-TEST FAIL: batch must be idempotent")
            return 1
    print("SELF-TEST OK: record_verdict")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="记录用户对灵感卡片的评价")
    ap.add_argument("--state-dir", type=Path)
    ap.add_argument("--card-id")
    ap.add_argument("--action", choices=sorted(ACTIONS))
    ap.add_argument("--reason", default="")
    ap.add_argument("--block-topic")
    ap.add_argument("--edited-file", type=Path)
    ap.add_argument("--batch", type=Path, help="verdict_inbox.csv（action 列可填英文或中文：采纳/否决/已做过/不重要/追问/立刻动手）")
    ap.add_argument("--rebuild-inbox", action="store_true")
    ap.add_argument("--config", type=Path, help="config.json（含 weights.events）；也可用环境变量 INSP_CONFIG")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    set_config(a.config)
    try:
        event_weight("adopt")
    except WCFG.WeightConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    if _CFG["source"] == "default":
        print("WARN: 没有给 --config（或 INSP_CONFIG），事件权重用内置默认值；正式使用请带上 config.json", file=sys.stderr)
    if a.state_dir and a.batch:
        ok, skipped, errors = record_batch(a.state_dir, a.batch)
        for e in errors:
            print("ERROR:", e)
        inbox = rebuild_inbox(a.state_dir)
        print(f"BATCH: 记录 {ok} 条，跳过已评价 {skipped} 条，错误 {len(errors)} 条；待评价清单已更新 → {inbox}")
        return 1 if errors else 0
    if a.state_dir and a.rebuild_inbox:
        print(f"OK: {rebuild_inbox(a.state_dir)}")
        return 0
    if not (a.state_dir and a.card_id and a.action):
        ap.error("需要 --state-dir、--card-id、--action")
    try:
        e = record(a.state_dir, a.card_id, a.action, a.reason, a.block_topic, a.edited_file)
    except (ValueError, LookupError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"OK: {e['card_id']} ← {e['label']}（权重 {e['weight']:+}，来源 {e['weight_source']}）")
    if e.get("cancelled"):
        c = e["cancelled"]
        print(f"   同时取消：立刻动手任务 {c.get('jobs') or []} · 追问 {c.get('asks') or []}" + (f"（{c['error']}）" if c.get("error") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
