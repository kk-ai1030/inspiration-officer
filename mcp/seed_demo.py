#!/usr/bin/env python3
"""为「在别的 agent 上测试」准备一份演示状态目录（不碰你真实的 inspiration/）。

从 insp-init 的官方模板复制状态文件，再写入 3 张示例卡片，这样 MCP 的
看卡片 → 评价 → 结果回填 → 周报 / 摘要 → dry-run 脚本 闭环可以立刻跑通。

    python mcp/seed_demo.py                              # → ./mcp_demo_state/
    python mcp/seed_demo.py --state-dir D:/tmp/insp     # 指定目录
    python mcp/seed_demo.py --state-dir ... --force     # 已有卡片时覆盖示例卡文件

生成的是演示数据，不要提交、不要当作真实评价记录。
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "insp-init/assets/灵感状态模板/inspiration"
DEFAULT_TARGET = ROOT / "mcp_demo_state"   # 故意放在包目录外：不进发布包、不进包结构扫描


def demo_cards(day: str) -> list[dict]:
    def src(kind: str, ref: str, quote: str) -> dict:
        return {"kind": kind, "ref": ref, "quote": quote, "verified": True}
    return [
        {"id": "ins_demo_1", "type": "knowledge", "title": "中性原子阵列的纠错阈值测量",
         "one_line": "把最近的表面码阈值测量结果整理成可复现的对照表。",
         "why_you": "你在做中性原子，且已关注量子纠错。",
         "minimal_check": "跑 A / B 两组阈值拟合，比较置信区间",
         "expected_observable": "A 组阈值比 B 组高至少 3%",
         "falsifier": "两组阈值差异小于 1% 或置信区间重叠",
         "cost_estimate": {"tokens": "~1k", "time": "半天"},
         "source": src("arxiv", "2611.10001", "we report a threshold of 0.57%"),
         "status": "proposed", "verdict": None, "date": day,
         "rubric_scores": [{"rule_id": "R01", "score": 2}]},
        {"id": "ins_demo_2", "type": "hypothesis", "title": "本地日志里的重复失败是否是同一根因",
         "one_line": "把三次失败堆栈对齐，验证是否共享一个空值分支。",
         "why_you": "来自本机观察通道，和你的实验脚本直接相关。",
         "minimal_check": "对齐三个堆栈并统计共享帧数",
         "expected_observable": "三个堆栈共享至少两帧",
         "falsifier": "共享帧少于两帧",
         "cost_estimate": {"tokens": "~500", "time": "1 小时"},
         "source": src("observed", "local://logs/run-2026-11-12", "NullPointerException at fit.py:88"),
         "status": "proposed", "verdict": None, "date": day, "rubric_scores": []},
        {"id": "ins_demo_3", "type": "method", "title": "用 OpenAlex 结果补一条基线检索",
         "one_line": "给现有检索式加一条 OpenAlex 通道，检查是否漏掉综述。",
         "why_you": "B2 多来源通道默认关闭，这是一条可验证的启用路径。",
         "minimal_check": "同一检索词分别走 arXiv 与 OpenAlex，统计去重后的新增条目",
         "expected_observable": "新增条目中至少有 1 篇近两年综述",
         "falsifier": "新增条目里没有综述",
         "cost_estimate": {"tokens": "~800", "time": "2 小时"},
         "source": src("openalex", "oa:W123456", "A review of neutral atom arrays"),
         "status": "proposed", "verdict": None, "date": day, "rubric_scores": []},
    ]


def seed(target: Path, force: bool = False) -> Path:
    if not TEMPLATE.is_dir():
        raise LookupError(f"找不到官方状态模板 {TEMPLATE}")
    if not target.exists():
        shutil.copytree(TEMPLATE, target)
    day = date.today().isoformat()
    cards_dir = target / "cards" / day
    cards_dir.mkdir(parents=True, exist_ok=True)
    card_file = cards_dir / "cards.json"
    if card_file.is_file() and not force:
        raise FileExistsError(f"{card_file} 已存在，要重建演示卡片请加 --force")
    card_file.write_text(json.dumps({"date": day, "cards": demo_cards(day)}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_TARGET)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    try:
        target = seed(a.state_dir.resolve(), a.force)
    except (LookupError, FileExistsError, OSError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}")
        return 1
    n = len(json.loads((target / "cards" / date.today().isoformat() / "cards.json").read_text(encoding="utf-8"))["cards"])
    print(f"OK 演示状态已就绪：{target}（{n} 张示例卡片）")
    print("把 MCP 客户端的 --state-dir 指向它即可测试；这是演示数据，别提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
