---
name: insp-card
description: 灵感漏斗 L2：把 triage.json 中通过预筛的候选精炼成结构化灵感卡片（优化建议 / 知识建议 / 假说卡片 / 流程改进四类），逐条按 rubric 打分，按 triage.json 的 fetch_mode 读摘要（默认）或通读原文与相关文献后逐字引用，自带最小验证动作、判据、失败判据和成本申报，并遵守分类配额。用于“生成灵感卡片 / 写灵感卡 / inspiration card / 把候选整理成卡片 / 按 rubric 给候选打分”。由 insp-nightly 路由调用，必须实际加载本 Skill，不得凭记忆模拟。
---

# 灵感卡片（L2）

## 定位

一个裸的想法没法评估、没法去重、没法学习。本 Skill 把候选变成**可审计的数据**。它是整个漏斗里唯一需要强模型判断的环节，所以输出会被外部脚本逐字段校验。

## 运行标识（必须原样写入输出）

本 Skill 的流程版本号是：

```text
flow_version = "insp-card/1.1.0"
```

输出 JSON 的根节点必须包含 `"flow_version": "insp-card/1.1.0"`。这个值只出现在本 Skill 中，外部校验器用它判断本 Skill 是否真的被加载。不要猜测、改写或省略它。

## 必读规范

开始前完整读取：

- [灵感卡片契约.md](references/灵感卡片契约.md)
- [四类灵感与配额.md](references/四类灵感与配额.md)
- [证据核验规范.md](references/证据核验规范.md)
- [逐条打分规范.md](references/逐条打分规范.md)

## 先看读取模式（v1.4.1）

打开 `triage.json`，看顶层字段 `fetch_mode`：

| `fetch_mode` | 你读什么 | 卡片必须有的证据 | 不要做 |
|---|---|---|---|
| `abstract`（默认；字段缺失也按它） | 每个候选的 `title` + `abstract` | `source.quote`（摘要逐字片段） | 不要去抓原文、不要写 `fulltext_quotes` / `related_evidence`；摘要没说的机制细节只能作为 hypothesis 里的待验证假设 |
| `fulltext` | 另读 `ft_<ID>.md`（全文）与 `rel_<ID>.md`（相关文献全文） | `source.quote` + 规则 2a + 2b | 不得只凭摘要写卡 |

两种模式都**不要自己写链接**：校验脚本会给每张卡补 `source.url`（arXiv 摘要页）、`source.read_mode`，全文模式再补 `source.fulltext_url`；你写的链接会被覆盖。

## 输入在哪

| 运行方式 | triage.json（全文模式另有 ft_/rel_*.md、fulltext_index.json） | rubric.md、画像 | 写到哪 |
|---|---|---|---|
| 一句话跑灵感（insp-nightly 自举） | `/workspace/inspiration_out/`（shortlisted 一次读完同内容小文件 `triage_shortlisted.json`，不要 cat 整个 triage.json） | `/workspace/inspiration_out/_state/taste/rubric.md`；画像看记忆里 `[灵感画像]` 开头的条目 | 论文卡：**一次写成** `/workspace/inspiration_out/cards.json`（v1.4.6 写法）；观察型卡：一张卡一个文件 `cards/01.json` … |
| 附件模式（评价页按钮 / 本地兜底） | `/workspace/_refs/`（可能带子目录，按文件名查找） | 同目录下的 `rubric.md`、`interests.md` | 同上 |

## 调用链

```text
triage.json.shortlisted（只能使用这里的候选）
→ 0. 看 triage.json 的 fetch_mode：
      abstract → 只读每个候选的标题与摘要；
      fulltext → 读 fulltext_index.json 里列出的 ft_<ID>.md（候选论文全文）和 rel_<ID>.md（相关文献全文），
                 没有原文的候选不得出卡（编排器已经把它们移出 shortlisted）
→ 1. 对每个候选判断类型（A 优化 / B 知识 / C 假说 / D 流程）
→ 2. 从 rubric.md 选出最相关的 3–5 条准则，逐条打分并写理由
→ 3. 低于门槛或证据不足 → 写进 rejected，附准则编号
→ 4. 通过的候选写成卡片：逐字 quote、（仅 fulltext）fulltext_quotes（正文引文 + 章节）与 related_evidence（相关文献引文 + 关系）、
      why_me、minimal_check、expected_observable、falsifier、cost_estimate
→ 5. 数字逐个登记到 numbers_provenance
→ 6. 按配额截断：宁缺毋滥，按总分保留
→ 7. 运行 validate_cards.py 自检（找得到脚本时），不通过就修，最多两次
→ 8. 交回 insp-nightly 输出结果块
```

## 强制规则

1. **只能引用 `triage.json.shortlisted` 与 `triage.json.observed_shortlist` 里的条目。** 不得自行检索、补充或引用记忆中的论文。
2. **quote 必须是摘要或原文的逐字片段**（6–60 词），可以用 `...` 连接同一来源中的多个逐字片段，但不得改写任何一个词。
2a. **（仅 `fetch_mode = fulltext`）必须读原文**：`source.fulltext_quotes` 写 1–4 条 `{quote, section}`，quote 从 `ft_<ID>.md` 逐字复制（8–80 词），section 写原文里真实存在的章节标题（不能是 Abstract），**至少 1 条是摘要里没有的正文内容**。卡片的机制、最小验证、判据应当来自正文（方法 / 实验 / 局限），而不是摘要的复述。
2b. **（仅 `fetch_mode = fulltext`）必须对照相关文献**：`related_evidence` 至少 1 条 `{ref, relation, quote, why}`。ref 只能是 `fulltext_index.json` 里给出的相关文献；relation ∈ supports / contradicts / extends / method / baseline / context；quote 从 `rel_<ID>.md` 逐字复制；why 一句话说明它对这张卡片意味着什么（例如“这篇的结果与候选论文相反，所以 falsifier 要设成……”）。
2c. **（仅 `fetch_mode = fulltext`）找引文用 `find_quotes.py`**，一次一条短命令，见下方「命令纪律」。不得用 `&&` 串多个 grep。
3. **三字段缺一不得提交**：`minimal_check`（两天内能做完的下一步）、`expected_observable`（看到什么算成立）、`falsifier`（看到什么算不成立）。
4. **数字必须有出处。** `one_line`、`why_me`、`hypothesis`、`magnitude` 中的每个小数或百分数，都必须能在摘要里找到（`abstract`）、在原文里找到（`fulltext:<arXiv ID>`，可以是候选论文或相关文献），或者来自你在沙箱里实际执行的结果（`executed:<命令或文件>`）。不允许“大约 10 倍”这类没有出处的估计。
5. **why_me 必须对照用户当前项目**（`interests.md`，或记忆里 `[灵感画像]` 开头的条目），不得泛泛而谈。两处都没有画像时，写“未提供画像，相关性仅基于关键词：<命中的关键词>”。
6. **不要把用户没有证据的能力当作已知。** 没有证据的方法，就在 minimal_check 里把步骤写具体。
7. **复活候选**（`revival: true`）必须在 why_me 里写明“复活放行：你之前否决过这个方向”。
8. 卡片 `status` 一律为 `proposed`，`verdict` 一律为 `null`。

## 结果校准提示（③ · 只当提示词证据）

本机把「采纳后结果回填」聚合成**独立的校准提议**；用户在本机 `--confirm` 之后，生效的提示会随脱敏摘要 `verdict_summary.json` 的 `calibration.applied` 一起进沙箱。它可能出现在：

- `/workspace/inspiration_state/verdict_summary.json`（收货员同步回沙箱的状态）；
- `/workspace/_refs/verdict_summary.json`（评价页按钮触发运行时随附件挂载）。

找到摘要且 `calibration.applied` 非空时：

1. 读 `dimension` / `key` / `direction` / `hint`，把 `hint` 当作**写卡时的软性提示**：`strengthen_evidence` → 该类型卡片的 `expected_observable` / `falsifier` 写得更可量化、`minimal_check` 写清两天内能做完的步骤；`broaden_coverage` → 这类候选不必额外收紧。
2. 三字段、逐字引用、rubric 打分、分类配额**照旧**，不因校准提示抬高或降低任何门槛。
3. **不得**据此改 `rubric.md`、权重、配额或排序；不得把结论成立率写成采纳率；不得把校准样本数当论文证据写进卡片或 `numbers_provenance`。校准提示是研究流程提示，不是文献证据。
4. 摘要里没有 `calibration.applied` 时不猜、不编、不索要原始 `outcomes.jsonl`。校准提议默认 `confirmed=false`，网页和你都不能代用户确认。

## 命令纪律（v1.4.4，全文模式必守，摘要模式同样适用）

平台的命令执行器会把长命令、用 `&&` 串起来的命令切碎（真机：`rel_2512\n.15790.md`），grep 其实跑成功了也会被判失败；**连续 5 次工具失败，平台会强制停机，cards.json 和结果块就都交不出来了**。所以：

1. **一条命令只做一件事。** 禁止用 `&&`、`;`、`||`、管道把多个 grep / cat / sed 串在一起；一次只查一个文件。
2. **找引文只用这一条短命令**（prepare 输出里 `FIND_QUOTES` 那一行；一次 1–2 个 ID，命令保持在 60 字符以内）：
   `python3 /workspace/fq <ID>`
   - 没有 `/workspace/fq` 时用本技能的 `scripts/find_quotes.py`：`python3 /workspace/skills/insp-card/scripts/find_quotes.py <ID>`（只给 1 个 ID，避免超过 80 列被切碎）；
   - 给入围论文 ID：列出 ft_<ID>.md 里命中关键词的逐字原句（带 §章节、是否摘要里也有），并顺带列出它的相关文献 rel_*.md 里的候选句；
   - 换关键词：加 `--terms "a,b"`；看章节：`--sections`；读某一节：`--section "<标题>"`（代替 cat / sed）。
   - 输出的句子可以原样复制进 `fulltext_quotes` / `related_evidence.quote`，§ 后面的标题原样复制进 `section`。
3. **不要 cat 整个 ft_ / rel_ 文件**（每个 49–121 KB），不要自己写 grep 正则。
4. **命令保持一行、尽量短**（prepare / finalize 这两条写死的命令除外）；不用 heredoc、不用 `\` 续行；写卡片用文件写入工具（论文卡一次写成 `cards.json`，观察型卡一张一个 `cards/01.json` …），不用 `echo` / `cat >` 拼 JSON。
5. **同一条命令失败 1 次就换做法**（改用 find_quotes.py 或文件读取工具），不要原样重试；连续失败 2 次就停止找引文，用已经拿到的引文出卡（宁缺毋滥），实在不够就按 `NO_CANDIDATES` 交付——**交出结果块比多找一条引文重要**。

## 观察型候选（v1.5 · observed 通道）

`triage.json.observed_shortlist` 里的候选不是论文，而是用户电脑上的本地观察者从**用户自己的**代码、日志、数据、笔记、论文库和使用记录里发现的事实（`id` 以 `obs_` 开头）。每个候选带：`kind`（workspace / log / anomaly）、`candidate_type`、`allowed_types`、`severity`（1–5）、`title`、`summary`、`abstract`（= 证据原文：代码片段、日志行、笔记原句、统计摘要，行首的 `123| ` 是行号）、`evidence_ref`（本地位置，形如 `library://code/qmc.py#L12-L140`）、`detector`。prepare 还会把每个候选写成 `/workspace/inspiration_out/obs_<编号>.md`，读它即可。观察型卡在论文卡（cards.json）写完之后写，一张卡一个文件 `cards/01.json` …，id 接着论文卡往后编。

写卡规则（校验脚本逐条检查）：

- `source.kind` = 候选的 `kind`；`source.ref` = 候选 `id`（`obs_` 开头）；`source.verified` 写 `false`（脚本核对后改成 true）；
- `type` 只能取 `allowed_types` 里的值，通常就是 `candidate_type`：代码问题 → optimization（A），日志 / 数据反常 → hypothesis（C），“没搞懂 X” → knowledge（B），重复劳动 / 高频使用 → process（D）；
- `source.quote` 从证据原文**逐字复制** 6–400 个字符（可以是一行代码、一行日志、一句笔记，行号前缀可带可不带）；
- 卡片里出现的数字（行数、复杂度、次数、离群值）在 `numbers_provenance` 里写 `"source": "observed"`；
- `why_me` 直接说这是用户自己的哪份文件、什么现象；`minimal_check` 必须是可以在用户本地做完的动作（例如“同一输入跑新旧两版、比对输出并计时”“把第 41 步的输入单独重跑一次”）；`falsifier` 写清什么结果说明不值得改 / 不是真反常；
- 证据里只给了指标、没有代码原文时（用户关闭了上传代码），按指标写，不要猜代码内容；
- 使用统计类候选（`detector` 以 `usage.` 开头，如“最近常画图”）：建议把重复工作沉淀成技能 / 模板 / 函数库；提到开源项目或现成技能时**只写名称并注明“建议检索核实”**，不要编造链接、星数或版本；
- 不要写链接：脚本会补 `source.evidence_ref` 和 `source.read_mode = "observed"`。

观察型候选和论文候选共用同一套配额与门槛：宁缺毋滥，证据不足就写进 rejected。

**每轮至少 1 张 A 类（v1.5.1）。** `observed_shortlist` 里只要有 `candidate_type = optimization` 的候选，本轮就必须至少交 1 张 `type = optimization` 的卡（挑证据最具体的那条）。A 类没有数量上限，也不占每日总量。带 `"backfill": true` 的候选是“本轮没有达标 A 类、按规则补位”的，照样写，但在 `why_me` 末尾加一句“（补位：严重度低于门槛）”。校验输出出现 `WARNING A_FLOOR` 时：只补那一张 A 类卡再校验一次；补一次仍不过就照常交付，不要反复重写。

## 动手单 action_brief（可选，v1.5.1）

用户在评价页按“立刻动手”后，任务按一份 7 栏的**动手单**执行。你可以在卡片里直接写好（写得好就原样用；不写或写错，评价页会按卡片自动生成，不影响这张卡通过）：

```json
"action_brief": {
  "scene": "研究对象 + 子领域，一句话",
  "params": [{"name": "函数长度", "value": "129", "unit": "行", "source": "observed"},
             {"name": "计时重复次数", "value": "", "unit": "次", "source": "user"}],
  "method": "建立什么模型 / 用什么手段求解（可以直接执行的程度）",
  "output": "可量化的指标或可观测现象",
  "baseline": "对照：基线是什么",
  "criterion": "产出与基线满足什么关系才算成立",
  "deliverables": ["bench.py", "patch.diff", "report.md"]
}
```

- `source` 只能是 `library`（资料库文件 / 引用论文原文）、`observed`（本地观察或实测的数字）、`user`（由用户提供；`value` 留空 = 开跑前用户必须填）；
- `library` / `observed` 的 `value` 必须能在候选证据原文或本卡 `numbers_provenance` 里找到，否则校验会把整个 `action_brief` 去掉；
- 每个有值的参数都要写 `unit`，没有量纲写“无量纲”；`deliverables` 写文件名（带扩展名），回执会逐个核对是否交付。

## 输出

**v1.5.9 的写法（两处输入，finalize 合并）：**

- **论文卡（arXiv 入围候选，B 类为主）= v1.4.6 写法**：读完 shortlisted（`triage_shortlisted.json`）后，用文件写入工具**一次**写成 `/workspace/inspiration_out/cards.json`，结构就是下面的根节点；被否决的论文候选写进它的 `rejected`。写之前先看 prepare 打印的 `REMAINING`（各类型本轮还能入库几张），已满的类型不要写。
- **观察型卡（obs_ 候选）= 1.5.7 写法**：论文卡写完后，一张卡一个文件 `cards/01.json`、`02.json` …（文件内容 = 一张卡片对象）；被否决的观察候选写进 `cards/rejected.json`（数组）。
- 写完立刻运行 finalize：它合并两处、补根字段、校验，合并结果写 `cards_final.json`（**只是输出，不要读、改、删**）。不想要某张卡用 `finalize --drop <卡片 id 或文件名>`，不要用删除工具。在结果块打印出来之前不得结束回答。

cards.json 结构见 [卡片模板](assets/卡片模板/cards.json)。根节点：

```json
{
  "schema_version": "1.0",
  "flow_version": "insp-card/1.1.0",
  "run_id": "<编排器给的 run_id；手动运行时用 manual-YYYYMMDD-HHMM>",
  "generated_at": "<ISO 8601>",
  "status": "OK | NO_CANDIDATES",
  "cards": [ ... ],
  "rejected": [ {"id": "<候选 ID>", "reason": "<一句话>", "rule_ids": ["R02"]} ]
}
```

## 收尾（v1.4.5）

写完最后一张卡就回到 insp-nightly 运行 finalize，然后：**收尾：输出结果块之后直接结束回答，不要调用 `complete`（交付）工具，也不要往附件里放任何东西。** 结果块属于正文，不是附件；`bundle.json` 已经在沙箱里，本地收货员会自己去取。平台不强制调用 `complete`（feedback6 实测：不调也能正常 completed）；调了反而可能因为附件校验失败被重试 3 次后强制停机，前端显示“任务已中断”，诱导用户“从断点继续”再跑一轮。万一平台提示 `delivery_attachments_required`：最多补一次，`attachments` 里**只能放真实文件路径**（例如 `inspiration_out/bundle.json`），绝不放说明文字；仍然失败就直接结束，不要第三次。

## 自检

**一句话跑灵感（insp-nightly 路由）时**：不用单独自检，直接运行 `insp_run.py finalize`，它会用同一个校验器检查 `/workspace/inspiration_out/cards.json`。

附件模式（本地兜底）时：

```bash
python3 <insp-card 目录>/scripts/validate_cards.py cards.json \
  --triage /workspace/_refs/<...>/triage.json --state-dir <含 state.json 的目录> \
  --fulltext-dir /workspace/_refs/<...>/ --require-fulltext
```

沙箱里找脚本：`find / -name validate_cards.py -path "*insp-card*" 2>/dev/null | head -1`。找不到脚本时如实说明“未能自检”，**不要声称已经通过校验**。本地收货员无论如何都会重新校验。

## 停止条件

没有任何候选能同时满足证据、三字段和配额时，输出 `status: "NO_CANDIDATES"`、空的 `cards` 和完整的 `rejected`。这是正常结果。
