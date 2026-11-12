#!/usr/bin/env python3
"""B7：把卡片的「最小验证」变成可下载、可跑的本地 dry-run 模板 + 回执。

设计红线：模板**只打印清单**，不执行命令、不联网、不读写任何本机数据、不产生结论。
所有卡片文本用 json.dumps 注入为字符串常量，避免引号 / 换行 / 反斜杠把脚本写坏或注入代码。

dry-run 回执与 run_action.receipt.json 的「执行层证据」严格分开：
它作为独立顶层字段 dry_run 存在，不进 checks（那 8 项是平台运行的事实核对），
不影响 all_checks_ok / conclusion / claims / ack。

Usage:
    python platform/dryrun_template.py --self-test
    python platform/dryrun_template.py --print --card-json card.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

MODE = "dry_run"
VERDICT = "inconclusive"
HEADER = '''#!/usr/bin/env python3
"""灵感卡最小验证 · 本地 dry-run 模板。

这份脚本只打印验证清单：不执行实验、不跑命令、不联网、不读写任何本机数据。
请在你自己的实验环境里按清单补全实现，再把判断写进 verdict。
"""
import json

CARD = '''
FOOTER = '''

def main():
    print("=" * 60)
    print("灵感卡最小验证 · DRY-RUN（未执行真实实验）")
    print("=" * 60)
    print("卡片:", CARD["id"], CARD["title"])
    print("来源:", CARD["source_ref"] or "（未给出）")
    print()
    print("[1] 最小验证:", CARD["minimal_check"])
    print("[2] 预期可观察结果:", CARD["expected_observable"])
    print("[3] 推翻条件:", CARD["falsifier"])
    print()
    print("这份脚本只打印清单，不会跑命令、不会联网、不会读你的数据。")
    print("请按上面三步在你自己的环境里实现，并把结果记为 supported / refuted / inconclusive。")
    print()
    print("verdict: inconclusive  # dry-run 不产生结论")
    print("executed: false")


if __name__ == "__main__":
    main()
'''
DANGEROUS = ("subprocess", "os.system", "eval(", "exec(", "socket", "urllib", "requests",
             "http://", "https://", "open(", "__import__", "shutil")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def fields(card: dict) -> dict:
    src = card.get("source") or {}
    return {
        "id": str(card.get("id") or ""),
        "title": str(card.get("title") or card.get("one_line") or ""),
        "minimal_check": str(card.get("minimal_check") or ""),
        "expected_observable": str(card.get("expected_observable") or ""),
        "falsifier": str(card.get("falsifier") or ""),
        "source_ref": str(src.get("ref") if isinstance(src, dict) else ""),
    }


def build(card: dict) -> dict:
    f = fields(card)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", f["id"] or "card")[:60]
    content = HEADER + json.dumps(f, ensure_ascii=False, indent=2) + FOOTER
    return {"filename": f"dryrun_{safe_id}.py", "content": content, "fields": f,
            "mode": MODE, "executed": False}


def receipt(card: dict, job_id: str | None = None) -> dict:
    """本地 dry-run 回执：恒为未执行 / 无定论，自己的 checks 与平台回执的 8 项分开。"""
    b = build(card)
    f = b["fields"]
    complete = all(f[k] for k in ("minimal_check", "expected_observable", "falsifier"))
    return {
        "mode": MODE, "executed": False, "verdict": VERDICT,
        "card_id": f["id"], "job_id": job_id, "generated_at": now_iso(),
        "filename": b["filename"], "fields": f,
        "checks": [
            {"name": "模板不执行真实实验", "ok": True, "detail": "只打印清单，无命令 / 网络 / 文件读写"},
            {"name": "最小验证与判据齐备", "ok": complete,
             "detail": "三项都有" if complete else "卡片缺少最小验证 / 判据 / 推翻条件"},
        ],
        "note": "dry-run 不是实验结果，不能当证据；真正的结论要由你运行后回填。",
    }


def render_receipt_md(dry: dict) -> str:
    f = dry.get("fields") or {}
    complete = all(x.get("ok") for x in dry.get("checks") or [])
    return "\n".join([
        "> 本地 dry-run 回执：**没有执行真实实验**，结论恒为「无定论」，不能当证据。",
        "",
        f"- 模板：`{dry.get('filename')}`（评价页可下载；纯打印，不跑命令、不联网、不读数据）",
        f"- 最小验证：{f.get('minimal_check') or '—'}",
        f"- 预期可观察结果：{f.get('expected_observable') or '—'}",
        f"- 推翻条件：{f.get('falsifier') or '—'}",
        f"- 判据齐备：{'是' if complete else '否'}",
        f"- 备注：{dry.get('note') or '—'}",
    ]) + "\n"


def self_test() -> int:
    card = {"id": 'ins_b7_"quoted"', "title": '标题带 "引号" 和\n换行', "source": {"ref": "9999.12345"},
            "minimal_check": "跑 A / B 两组，比较 \\ 路径", "expected_observable": "A 比 B 高",
            "falsifier": "A 不比 B 高"}
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + ((" · " + detail[:200]) if detail and not cond else ""))
        ok &= bool(cond)

    b = build(card)
    try:
        compile(b["content"], b["filename"], "exec")
        syntax_ok = True
    except SyntaxError as exc:
        syntax_ok = False
        print("syntax:", exc)
    check("模板语法合法", syntax_ok)
    check("文件名安全", b["filename"] == "dryrun_ins_b7__quoted_.py", b["filename"])
    check("危险能力一个都没有", not any(x in b["content"] for x in DANGEROUS), b["content"])
    check("卡片文本被 JSON 转义", '\\"引号\\"' in b["content"] and "\\n" in b["content"])
    check("字段齐全", b["fields"]["source_ref"] == "9999.12345" and b["fields"]["falsifier"] == "A 不比 B 高")
    d = receipt(card, job_id="job_b7")
    check("回执恒为未执行 / 无定论", d["executed"] is False and d["verdict"] == VERDICT and d["mode"] == MODE)
    check("回执 checks 独立且齐备", len(d["checks"]) == 2 and all(x["ok"] for x in d["checks"]))
    md = render_receipt_md(d)
    check("回执 Markdown 含三项与免责", "最小验证" in md and "推翻条件" in md and "不能当证据" in md)
    print("SELF-TEST OK: dryrun_template" if ok else "SELF-TEST FAILED: dryrun_template")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--card-json", type=Path)
    ap.add_argument("--print", action="store_true", help="打印脚本内容（默认打印回执）")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.card_json:
        ap.error("需要 --card-json（或用 --self-test）")
    card = json.loads(a.card_json.read_text(encoding="utf-8"))
    if a.print:
        print(build(card)["content"], end="")
    else:
        print(json.dumps(receipt(card), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
