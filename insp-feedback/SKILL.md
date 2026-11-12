---
name: insp-feedback
description: 记录用户对灵感卡片的评价（采纳 / 否决 / 已做过 / 已读但不重要 / 改写 / 追问），写入只追加的 verdicts.jsonl，并把否决或已做过的条目加入负面清单、可选屏蔽整个主题。用于“采纳 ins_xxx / 否决这张卡 / 这个做过了 / 不重要 / 我改了这张卡 / record verdict / 灵感反馈”。只记录反馈，不调整权重（权重由 insp-review 在周度复盘中提议、用户确认后才改）。
---

# 灵感反馈记录

## 定位

用户对每条灵感的反应都是一条训练数据。本 Skill 只做一件事：**把反应准确、可追溯地记下来。** 它不推断偏好，不修改权重，不重新生成卡片。

## 必读规范

- [反馈事件与权重.md](references/反馈事件与权重.md)

## 两种使用方式

### 方式 1（推荐）：本地命令行

```bash
python3 <insp-feedback 目录>/scripts/record_verdict.py --state-dir inspiration/ \
  --card-id ins_20261028_001 --action adopt --reason "正好要补基线"
```

可选动作：`adopt` `start_now` `reject` `done` `not_important` `edited` `ask_detail` `read_no_action` `unread` `retract`。

`retract` = 撤销这张卡片最近一条显式评价（同时删掉那条评价加进 `negative.jsonl` 的行、把卡片状态恢复为 proposed）。日志只追加不删除；统计时按“撤销后剩下的最后一条显式评价”计算。

本地评价页 `platform/review_server.py`（弹出卡片、翻面、1–6 分类、E 改写、Z 撤销）也是调用本脚本写入，记录里带 `via: card_ui`；页面产生的停留时长、是否翻面等隐式信号单独写入 `implicit.jsonl`，**不参与权重调整**。

- 否决时可以加 `--block-topic "<短语>"`，屏蔽整个主题（仍受复活机制约束）。
- 改写时用 `--edited-file <改写后的卡片.json>`，脚本会自动存下改动的 diff。**改写是最值钱的数据。**

### 方式 1b：批量（Excel）

每次运行产生卡片后，编排器会重建 `inspiration/inbox/verdict_inbox.csv`，里面是所有还没有显式评价的卡片。用 Excel 打开，填 `action` 列（英文或中文：采纳 / 否决 / 已做过 / 不重要 / 追问 / 立刻动手），可选填 `reason`、`block_topic`，保存后运行：

```bash
python3 <insp-feedback 目录>/scripts/record_verdict.py --state-dir inspiration/ --batch inspiration/inbox/verdict_inbox.csv
```

UTF-8 和 GBK（Excel 默认的“CSV”）都能读；已经评价过的卡片会被跳过，重复运行不会重复记录。改写卡片仍然要用方式 1 单独记录。

### 方式 2：在 SciencePal 对话中

用户在对话里说“采纳 ins_20261028_001，理由……”时：

1. 加载本 Skill；
2. 只输出一个结果块，不做其它动作：

```text
<<<INSP_VERDICTS_JSONL
{"card_id": "ins_20261028_001", "action": "adopt", "reason": "……", "block_topic": null}
INSP_VERDICTS_JSONL>>>
```

   输出结果块后直接结束回答，**不要调用 `complete`（交付）工具，也不要放附件**（结果块属于正文）。

3. 提醒用户：这条反馈要由本地编排器导入（`run_local.py --import-verdicts <文件>`）后才会生效。agent 在平台沙箱里修改的只是副本，不是权威状态。

## 纪律

- 用户没有明确说出动作时，不要替他判断（例如把“嗯”记成采纳）。
- 理由要原话记录，不要改写。
- 记录后不在同一次对话里调整任何规则或权重。设计文档 §10：系统绝不能悄悄改变行为。

## 停止条件

找不到卡片 ID、动作不在可选范围内、`edited` 没有提供改写文件时，报错并停止，不写入任何文件。
