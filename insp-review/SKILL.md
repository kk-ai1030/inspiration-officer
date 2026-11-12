---
name: insp-review
description: 灵感机制的周度复盘：统计采纳率与两周重复率，按带先验的贝叶斯平均计算各类型采纳率，一次只提议调整一个权重（用户确认后才生效），诊断哪条 rubric 准则常与否决同时出现，审计平台自动抽取的长期记忆，并列出三个月前采纳卡片的回访问题。用于“灵感周报 / 本周灵感复盘 / inspiration weekly review / 调一下灵感权重 / 清理灵感相关记忆”。
---

# 灵感周度复盘

## 定位

反馈闭环的“学习”发生在这里，但学习方式是**提议 + 确认**，而不是悄悄改变行为。“它最近怎么老推这种东西”的体验，比推得不准更致命。

## 必读规范

- [周度复盘协议.md](references/周度复盘协议.md)
- [权重更新规则.md](references/权重更新规则.md)
- [记忆审计规范.md](references/记忆审计规范.md)

## 调用链

```text
（编排器）GET /api/memory → memory.json
→ weekly_review.py 生成 weekly_<周>.md 与 proposals_<周>.json
→ 用户阅读周报
→ 用户把 proposals 中的 confirmed 改成 true（或不改）
→ weekly_review.py --apply（未确认就拒绝执行）
→ 用户逐条处理记忆审计列表（编排器用 PATCH / DELETE /api/memory/{id} 执行）
```

## 执行

```bash
python3 <insp-review 目录>/scripts/weekly_review.py --state-dir inspiration/ \
  --memory memory.json --out-dir inspiration/reports
```

确认后：

```bash
python3 <insp-review 目录>/scripts/weekly_review.py --state-dir inspiration/ \
  --apply inspiration/reports/proposals_<周>.json
```

## 在平台对话中运行时（B4 / K3）

**评价原文 `verdicts.jsonl` 永远不出本机**，网页里拿不到它。唯一可信的数据源是本机收货员推送的脱敏聚合摘要 `verdict_summary.json`，位置可能是：

- `/workspace/inspiration_state/verdict_summary.json`（收货员同步回会话沙箱）；
- `/workspace/_refs/verdict_summary.json`（评价页按钮触发运行时随附件挂载）。

执行顺序：

1. 先找摘要。**两个位置都没有**：直接回答「周报需要在本机运行，网页里没有评价数据」，然后结束；**不得**手算、不得估算、不得编造采纳率 / p̂ / 权重提议，也不得从记忆里凑数字。
2. 找到摘要后，加载本 Skill，在沙箱里运行：

```bash
python3 <insp-review 目录>/scripts/weekly_review.py --summary /workspace/inspiration_state/verdict_summary.json
```

（摘要在附件里时把路径换成 `/workspace/_refs/verdict_summary.json`。脚本找不到或摘要不是 `insp-verdict-summary/1.0` 时如实报错，不要改成自己写一份周报。）

3. 原样展示脚本输出的周报；可以在末尾补一段**它自己认为本周最有价值的一条卡片**及理由（设计文档 §12），但不得改动脚本给出的任何数字。
4. **不得**执行 `--apply`，**不得**修改或删除任何记忆，不得读取或索要原始评价文件。权重调整与记忆清理只能由用户确认后，在本地执行。
5. 用户要「按完整数据复盘 / 看评价原文 / 应用权重」时，告诉他本机命令：`python platform/run_weekly.py --config config.json`，以及 `weekly_review.py --apply ... --confirm`。

## 停止条件

样本不足（任一类型 n < 5）时只记录、不提议；提议未确认时拒绝应用；记忆审计只列清单，不自动删除。
