#!/usr/bin/env python3
"""B1 采纳后结果回填：离线回归。

验证三件事：
  1. outcomes.jsonl 与 verdicts.jsonl / 卡片状态 / 权重完全隔离；
  2. 评价页 GET / POST /api/outcome 与历史卡片列表可用；
  3. 周报第 9 节与 proposals.outcome_summary 能消费回填数据。
不联网、不读真实令牌、只用临时状态目录。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK = True


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    print(("PASS " if cond else "FAIL ") + name + ((" · " + detail) if detail and not cond else ""))
    OK &= bool(cond)


def make_card(i: int) -> dict:
    return {"id": f"ins_b1_{i}", "type": "knowledge", "subfield": "memory", "title": f"卡片{i}",
            "one_line": "一句话", "minimal_check": "跑两组", "expected_observable": "A 更高",
            "falsifier": "A 不高", "cost_estimate": {"tokens": "~1k"},
            "source": {"kind": "arxiv", "ref": f"9999.1000{i}", "quote": "q", "verified": True},
            "status": "proposed", "verdict": None}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="insp_b1_"))
    try:
        sd = tmp / "inspiration"
        shutil.copytree(ROOT / "insp-init/assets/灵感状态模板/inspiration", sd)
        (sd / "cards" / "2026-11-12").mkdir(parents=True, exist_ok=True)
        (sd / "cards" / "2026-11-12" / "cards.json").write_text(
            json.dumps({"date": "2026-11-12", "cards": [make_card(1), make_card(2)]}, ensure_ascii=False),
            encoding="utf-8")
        verdict = {"ts": "2026-11-12T10:00:00+08:00", "card_id": "ins_b1_1", "source_ref": "9999.10001",
                   "type": "knowledge", "action": "adopt", "label": "采纳", "weight": 1.0,
                   "weight_source": "default", "explicit": True, "rubric_rule_ids": [], "weights_version": 0}
        (sd / "verdicts.jsonl").write_text(json.dumps(verdict, ensure_ascii=False) + "\n", encoding="utf-8")

        out = load("insp-feedback/scripts/record_outcome.py", "b1_outcome")
        rs = load("platform/review_server.py", "b1_review_server")
        wr = load("insp-review/scripts/weekly_review.py", "b1_weekly")

        print("== B1-1 写入隔离")
        before_v = len((sd / "verdicts.jsonl").read_text(encoding="utf-8").strip().splitlines())
        e = out.record(sd, "ins_b1_1", "confirmed", "按最小验证跑通了", "A=0.82 B=0.79", via="test")
        check("B1-1 追加一条回填", e["outcome"] == "confirmed" and len(out.read_outcomes(sd)) == 1)
        check("B1-1 verdicts.jsonl 未变", len((sd / "verdicts.jsonl").read_text(encoding="utf-8").strip().splitlines()) == before_v)
        check("B1-1 卡片状态未变", out.find_card(sd, "ins_b1_1")["status"] == "proposed")
        check("B1-1 无评价的卡拒绝回填", _raises(lambda: out.record(sd, "ins_b1_2", "confirmed")))
        check("B1-1 非法结果拒绝", _raises(lambda: out.record(sd, "ins_b1_1", "made_up")))
        out.record(sd, "ins_b1_1", "refuted", "复现失败", "", via="test")
        check("B1-1 取最后一条", out.latest_by_card(sd)["ins_b1_1"]["outcome"] == "refuted")
        s = out.summarize(sd)
        check("B1-1 聚合正确", s["total"] == 1 and s["by_outcome"]["refuted"] == 1 and s["confirmed_rate"] == 0.0, str(s))
        check("B1-1 模块自测", out.self_test() == 0)

        print("== B1-2 评价页接口")
        rs.TOKEN_FILE = tmp / "review_token"
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        ready, holder = threading.Event(), []
        threading.Thread(target=rs.serve, args=(sd, port, 0, ready, holder), daemon=True).start()
        ready.wait(5)
        tok = rs.get_token()

        def call(path: str, data=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                         data=None if data is None else json.dumps(data).encode("utf-8"),
                                         headers={"X-Token": tok, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        code, r = call("/api/outcome?card_id=ins_b1_1")
        check("B1-2 GET 返回最新回填", code == 200 and r["outcome"]["outcome"] == "refuted", str(r))
        code, r = call("/api/outcome?scope=all")
        check("B1-2 GET scope=all 带聚合", code == 200 and r["summary"]["total"] == 1 and len(r["outcomes"]) == 2, str(r))
        code, r = call("/api/outcome", {"card_id": "ins_b1_1", "outcome": "confirmed",
                                        "conclusion": "补跑后成立", "measured": "提升 3.2%"})
        check("B1-2 POST 写入成功", code == 200 and r["entry"]["via"] == "card_ui", str(r))
        code, r = call("/api/outcome", {"card_id": "ins_b1_1", "outcome": "nope"})
        check("B1-2 POST 非法结果 400", code == 400, str(r))
        code, r = call("/api/outcome", {"card_id": "ins_b1_2", "outcome": "abandoned"})
        check("B1-2 POST 未评价卡 400", code == 400, str(r))
        code, r = call("/api/cards?scope=all")
        c1 = next((c for c in r["cards"] if c["id"] == "ins_b1_1"), {})
        check("B1-2 卡片列表带 outcome", code == 200 and c1.get("outcome", {}).get("outcome") == "confirmed", str(c1))
        check("B1-2 verdicts 仍只有一条", len((sd / "verdicts.jsonl").read_text(encoding="utf-8").strip().splitlines()) == before_v)
        if holder:
            holder[0].shutdown()

        print("== B1-3 周报消费")
        text, proposals = wr.review(sd, date(2026, 11, 12), None)
        check("B1-3 周报含第 9 节", "## 9. 采纳后结果回填" in text and "结论成立率" in text)
        check("B1-3 周报含实测摘录", "补跑后成立" in text)
        check("B1-3 proposals 带聚合", proposals.get("outcome_summary", {}).get("confirmed_rate") == 1.0,
              str(proposals.get("outcome_summary")))
        check("B1-3 采纳率章节未被改动", "## 1. 两个核心指标" in text and "## 3. 本周权重调整提议" in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("B1 OK" if OK else "B1 TESTS FAILED")
    return 0 if OK else 1


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except (ValueError, LookupError):
        return True


if __name__ == "__main__":
    raise SystemExit(main())
