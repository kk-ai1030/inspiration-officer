---
name: insp-triage
description: 灵感漏斗的 L0+L1 预筛：拉取新论文增量（与 state.json 快照做 diff）、负面清单与已推送去重、主题复活机制、硬门槛和三维粗打分，输出 triage.json。用于“筛选今天的候选 / 灵感预筛 / inspiration triage / 算一下新增了多少论文 / 跑增量”。全部由确定性脚本 triage.py 完成，零 LLM 成本；不生成卡片，不修改权威状态。
---

# 灵感预筛（L0 + L1）

## 定位

漏斗的原则是 **L0 宽进、L1 严筛、L2 少量**。本 Skill 覆盖 L0 和 L1，全部交给脚本：

- 计数必须可以由脚本重算（测评里的成功标准就是 `40 / 32 / 8 / 0 / 2` 这类计数完全一致）；
- 模型**不得**凭阅读来判断“哪些是新的”，也不得手工修改 `triage.json` 的计数。

v1.4 起有两条路：网页里一句话跑灵感时，由 insp-nightly 自带的 `insp_run.py prepare` 在沙箱里调用本脚本（随包附带的副本，与原文件逐字一致）；本地兜底时，由 `platform/run_local.py` 在本地运行它，再把 `triage.json` 作为附件上传。两条路的计数规则完全相同。

## 必读规范

- [候选来源与增量规范.md](references/候选来源与增量规范.md)
- [去重与硬门槛.md](references/去重与硬门槛.md)
- [粗打分规范.md](references/粗打分规范.md)

## 调用链

```text
raw_items.json（或 --fetch-arxiv 按 state.sources 抓取）
→ L0 增量：与 state.seen_ids 做 diff（ID 去掉版本号 vN 后比较）
→ L1-a ID 去重：negative(kind=id) + state.pushed
→ L1-b 主题去重：negative(kind=topic)，每 revival.every 次放行 1 次
→ L1-c 硬门槛：有 ID、有标题、摘要 ≥ 30 词
→ L1-d 语义近重复：与已推送标题、本批次标题的 Jaccard ≥ 阈值即丢
→ L1-e 粗打分：relevance / actionability / cost（1–5），低于 min_relevance 丢弃
→ 按总分取前 max_shortlist 条
→ triage.json
```

## 执行

```bash
python3 <insp-triage 目录>/scripts/triage.py \
  --state-dir <状态根目录>/inspiration \
  --raw raw_items.json \
  --out triage.json
```

平台沙箱里找脚本：`find / -name triage.py -path "*insp-triage*" 2>/dev/null | head -1`。

退出码：`0` 正常；`2` 输入格式错误；`3` **STATE_MISSING**（立即停止，交回 insp-nightly 熔断）；`4` 抓取失败（arXiv 限流 429 时，改用 `--raw` 或稍后重试，**不得**用记忆中的论文替代）。

## 输出

结构见 [筛选模板](assets/筛选模板/triage.json)。关键字段：

- `counts`：raw / seen / new / negative_filtered / gate_failed / similar_filtered / below_threshold / shortlisted；
- `new_ids`：本次所有新增 ID。编排器在**整次运行成功后**才把它们并入 `state.seen_ids`；
- `revival_state`：复活计数器的新值，由编排器写回 state；
- `shortlisted`：交给 insp-card 的候选（含摘要原文与分数）；
- `dropped`：每条被丢弃条目的原因，供周报审计。

## 停止条件

退出码为 3 或 4 时不得继续生成卡片。`shortlisted` 为空时状态是 `NO_CANDIDATES`，这是正常结果：没有增量就直接退出，不烧钱。
