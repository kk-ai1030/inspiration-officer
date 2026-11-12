---
name: insp-nightly
description: 灵感机制“跑一次”的总入口（v1.4：只在用户手动触发时运行，已取消一切定时任务；名字沿用旧称）。用户说“主动探索灵感 / 抽卡 / 跑一次灵感 / 生成今日灵感卡片 / inspiration run / 看看今天有什么值得做”时使用。不需要用户上传任何文件：在沙箱里运行本技能自带的 scripts/insp_run.py 自己拉 arXiv、增量去重、粗打分（默认只读摘要；--fetch-mode fulltext 时再逐篇抓原文），再调用 insp-card 出卡，最后输出 INSP_BUNDLE_JSON 整包，本地收货员会自动入库并弹出评价页。不负责记录反馈和周度复盘。
---

# 跑一次灵感（入口）

## 定位

本 Skill 是灵感机制每次运行的**唯一入口**，由用户**手动**触发（网页里 @灵感官 一句话，或评价页的“主动探索灵感”按钮）。没有定时任务、没有无人值守运行。它不产生想法，只负责：

1. 拿到可信的状态（没有就从模板自举）；
2. 在沙箱里把“抓取 → 预筛（→ 全文模式下抓原文）”跑完（脚本已随本技能挂载，不需要用户上传）；
3. 路由给 insp-card 出卡；
4. 以机器可解析的整包交付结果，让本地收货员自动收货。

总原则：**它不替用户做决定，它替用户找选项；宁缺毋滥；每条判断都必须可追溯。**

## 必读规范

- [运行协议.md](references/运行协议.md)
- [熔断与停止条件.md](references/熔断与停止条件.md)

## 调用链（一句话跑完）

```text
用户：“主动探索灵感”
→ 0. 加载本 Skill，复述 3 条硬纪律
→ 1. 从已召回的长期记忆里找“[灵感配置]”（检索式/关键词）、“[灵感状态]”（已推送 ID）、“[灵感设置]”（论文读取模式）
→ 2. 运行 prepare（自己拉 arXiv、增量、去重、粗打分；fulltext 模式再抓原文 → /workspace/inspiration_out/）
      ├ 退出码 4 FETCH_BLOCKED → 原样输出结果块，结束（本地收货员会自动改走本地抓取）
      ├ NO_CANDIDATES          → 原样输出结果块，结束（不烧钱）
      └ 有入围 → 继续
→ 3. 加载 insp-card（必须实际加载）。先写论文卡（v1.4.6 写法）：abstract 模式一次读完 triage_shortlisted.json（= triage.json 的 shortlisted 标题与摘要），fulltext 模式用 find_quotes.py 在 ft_<ID>.md 与 rel_<ID>.md 里找引文（一次一条短命令）；按 REMAINING 的名额、按分数从高到低，**一次写成 /workspace/inspiration_out/cards.json**
→ 3b. 再写观察型卡（有 OBSERVED 时，1.5.7 写法不变）：读 obs_<编号>.md，一张卡一个文件写到 /workspace/inspiration_out/cards/01.json、02.json …
→ 4. 写完立刻运行 finalize（合并 cards.json + cards/；超配额的自动进 rejected，不算错；有错就改它点名的那张——论文卡在 cards.json、观察型卡在 cards/NN.json；不要的卡用 finalize --drop <卡片 id>；第 3 次自动只交通过的卡）
→ 5. 把 finalize 打印的 <<<INSP_BUNDLE_JSON ... INSP_BUNDLE_JSON>>> 原样放在最终回答最后
```

## 强制流程

### 0. 先加载、先复述

第一步调用技能加载工具加载本 Skill，并在回答开头原文复述：

1. 状态缺失就停，绝不把全部条目当作新增。
2. 每张卡片必须有真实来源、最小验证动作和失败判据，三者缺一不得提交。
3. 未经用户批准，不执行任何建议里的动作。

### 1. 读记忆里的配置（零上传的关键）

平台会在运行开始时自动召回长期记忆。找这两类条目：

| 记忆前缀 | 用法 |
|---|---|
| `[灵感配置]` | 里面写着 `--query` 与 `--keywords` 的值，原样传给 prepare |
| `[灵感状态]` | 里面是已推送过的 arXiv ID 列表，原样传给 `--exclude` |
| `[灵感设置]` | 里面写着 `--fetch-mode abstract` 或 `--fetch-mode fulltext`，原样传给 prepare |

用户在本次指令里明确说了方向（如“今天看 surface code 解码器”）时，以用户的话为准，自己写成 arXiv 检索式传给 `--query`。都没有时不传，脚本会用状态里的检索式。

**论文读取模式（`--fetch-mode`）按下面的优先级决定，只能二选一：**

| 优先级 | 来源 | 取值 |
|---|---|---|
| 1 | 触发指令里写明了 `--fetch-mode X`（评价页按钮 / 本地触发会写，来自本地 config.json） | 照抄 X，不要改 |
| 2 | 用户本次的话明确要求“通读全文 / 读全文 / 精读原文” → `fulltext`；“只看摘要 / 快速跑” → `abstract` | 按用户的话 |
| 3 | 记忆 `[灵感设置]` 里的值 | 照抄 |
| 4 | 以上都没有 | **不传**（脚本默认 `abstract`，只读摘要） |

不要因为“全文更好”就自作主张传 `fulltext`：它要多花 4–5 倍时间，只有用户（或配置）要求时才用。

### 写卡纪律（v1.5.9）

1. **论文卡（arXiv 入围候选）= v1.4.6 写法**：prepare 之后**一次**读完 `/workspace/inspiration_out/triage_shortlisted.json`（就是 triage.json 的 shortlisted，只留标题、摘要等出卡要用的字段；不要 cat 整个 triage.json——500 条时约 150 KB 会被截断），读完下一步就用文件写入工具**一次**写成 `/workspace/inspiration_out/cards.json`（根节点 `flow_version` / `run_id` / `cards` / `rejected`）。不要逐篇另找文件，不要先在回答里打草稿。
2. **观察型卡（obs_ 候选）= 1.5.7 写法不变**：论文卡写完后，读一条 `obs_<编号>.md` 写一张 `cards/01.json`、`02.json` …；id 接着论文卡往后编（重了 finalize 自动改号）。
3. **在 finalize 打印结果块之前，不得结束回答。** 说了“我去写卡片”，下一步就必须是文件写入工具的调用。
4. 万一被打断：本地会在同一会话自动发一条【自动续跑】消息。照它做——已经写好的 cards.json / 单卡文件不要重写，从缺的开始，然后运行 finalize。
5. 平台把 prepare 转到后台（返回 session_name / “still running”）时：用 watch_task 等它结束；`triage_shortlisted.json` 是 prepare 最后写的文件，它不存在就说明 prepare 还没跑完，不要去读别的旧文件。

### 2. prepare（命令写死，不要改写）

```bash
python3 /workspace/skills/insp-nightly/scripts/insp_run.py prepare \
  [--query '<检索式>'] [--keywords '<k1,k2>'] [--exclude '<id1,id2,...>'] [--fetch-mode abstract|fulltext] [--fulltext-max N] [--observed-only]
```

`--fulltext-max N`：全文模式每次最多读几篇入围论文（默认 **4**，按分数取前 N 篇，其余不标记为已见、下次再参与）。触发指令里写了就照抄，没写就不传。全文模式下 prepare 会打印 `FULLTEXT_MAX N`。

prepare 会打印 `FETCH_MODE abstract` 或 `FETCH_MODE fulltext`，并写进 `triage.json` 的 `fetch_mode` 字段；后面的步骤都按这个值走。

找不到路径时：`find / -name insp_run.py -path "*insp-nightly*" 2>/dev/null | head -1`。

脚本自己决定状态来源并打印 `MODE`：

| MODE | 含义 |
|---|---|
| `attached` | 本次附件里有 state.json（评价页按钮 / 本地兜底触发时自动附上） |
| `persisted` | 同一会话上次运行留下的状态，或收货员同步进沙箱的状态 |
| `bootstrap` | 新会话、没有状态：从模板自举，去重由 `--exclude` 和本地收货员兜底 |

附件里已经有 `triage.json`（本地兜底流程）时，脚本不会重新抓取，直接用附件。

arXiv 返回 429 时脚本自己退避重试（10 / 30 / 60 秒），**不要**自己另写抓取代码，也不要联网搜索论文。

### 3. 出卡

加载 insp-card，按它的完整契约把 prepare 列出的每个入围候选写成卡片：

- 输入：`/workspace/inspiration_out/triage.json` 的 shortlisted（`fetch_mode = abstract` 时**只有它**，按摘要出卡，不要自己去抓原文；一次读完同内容的小文件 `triage_shortlisted.json`）；`fetch_mode = fulltext` 时另有 `ft_<ID>.md`、`rel_<ID>.md`、`fulltext_index.json`；
- 输出：论文卡一次写成 `/workspace/inspiration_out/cards.json`（根节点带 insp-card 的 `flow_version`，`run_id` 用 prepare 打印的值）；观察型卡写 `cards/NN.json`（见下面观察型候选）；
- 只能引用 triage.json 的 shortlisted；引文从摘要 / 原文文件**复制**，不要复述；
- **全文模式找引文只用 prepare 输出的 `FIND_QUOTES` 那条短命令：`python3 /workspace/fq <ID>`**（一次 1–2 个 ID，命令保持在 60 字符以内；prepare 会把本技能自带的 `scripts/vendor/find_quotes.py` 复制成 `/workspace/fq`，万一没有就用 `python3 /workspace/skills/insp-nightly/scripts/vendor/find_quotes.py <ID>`）；不要 cat 整个原文文件，不要用 `&&` 串多个 grep，一次一条命令（见 insp-card「命令纪律」）；
- 命令连续失败 2 次就停止找引文，用已有引文出卡或按 NO_CANDIDATES 交付：平台连续 5 次工具失败会强制停机，结果块就交不出来了；
- **观察型候选（v1.5）**：prepare 输出 `OBSERVED n` 时，`triage.json.observed_shortlist` 里有来自用户本地代码 / 日志 / 笔记 / 使用记录的候选，证据在 `/workspace/inspiration_out/obs_<编号>.md`。论文卡写完后，按 insp-card「观察型候选」规则出卡，一张卡一个文件 `cards/NN.json`（`source.kind` = 候选 kind，`source.ref` = `obs_` 编号，quote 从证据原文逐字复制，数字来源写 `observed`）。只想处理观察通道、不抓 arXiv 时，prepare 加 `--observed-only`；
- 链接不用自己写：finalize 会给每张卡片补上 `source.url`（arXiv 摘要页），全文模式再补 `source.fulltext_url`（读过的全文）。

### 4. finalize

```bash
python3 /workspace/skills/insp-nightly/scripts/insp_run.py finalize
```

- 退出码 0：打印结果块，进入第 5 步；
- 输出里的 `QUOTA_TRIMMED`：合格但超出配额的卡已经按分数自动移进 rejected，**不是错误**，不要删文件、不要改它们；
- 退出码 2：只改报错里点名的那张卡（标 `cards.json` 的改 cards.json 里那一张，标 `cards/NN.json` 的改那个文件）后重跑；不想要某张卡就 `finalize --drop <卡片 id 或文件名>`（论文卡从 cards.json 移进 rejected，单卡文件移到 `cards/_dropped/`，**不要用删除工具；`cards_final.json` 是 finalize 的输出，不要读、改、删**）；
- 第 3 次仍不过：finalize 自动只交通过的卡片（`AUTO_PARTIAL`），不要再循环修改（v1.5.7，CARD1：循环 6 次把余额烧光）。

### 5. 交付

最终回答 = 一句话摘要（读取模式 / 抓到几篇 / 新增几篇 / 入围几篇 / 出卡几张，并列出每张卡片的 arXiv 链接）+ finalize 打印的整个 `<<<INSP_BUNDLE_JSON ... INSP_BUNDLE_JSON>>>` 结果块，**原样、不改动、不省略**。整包同时已写在 `/workspace/inspiration_out/bundle.json`，本地收货员优先读文件。不要生成 docx、pdf 或其它未要求的产物。

**收尾：输出结果块之后直接结束回答，不要调用 `complete`（交付）工具，也不要往附件里放任何东西。** 结果块属于正文，不是附件；`bundle.json` 已经在沙箱里，本地收货员会自己去取。平台不强制调用 `complete`（feedback6 实测：不调也能正常 completed）；调了反而可能因为附件校验失败被重试 3 次后强制停机，前端显示“任务已中断”，诱导用户“从断点继续”再跑一轮。万一平台提示 `delivery_attachments_required`：最多补一次，`attachments` 里**只能放真实文件路径**（例如 `inspiration_out/bundle.json`），绝不放说明文字；仍然失败就直接结束，不要第三次。

### 禁止提问

本流程中**不调用 ask 或任何弹表单的工具**（API 触发时表单无法回答）。信息不足时在摘要里写 `NEED_INPUT: <原因>` 并照常交付结果块。

## 状态从哪来（v1.5.5）

prepare 打印的 `MODE` 说明这一轮用了哪份状态：`attached`（评价页按钮附上的）、`skill_snapshot`（本地评价页发布进本技能的快照，含本地观察 observed.jsonl 与已推送清单，网页对话入口靠它）、`persisted`（同一会话上次留下的）、`bootstrap`（模板）。`COUNTS` 里 `observed_missing: true` 表示这一轮没有本地观察通道，A 类优化卡出不来，在最终摘要里写一句提醒用户打开本地评价页同步一次。

## 停止条件

`STATE_MISSING`、`FETCH_BLOCKED`、`NO_CANDIDATES`、`VALIDATION_FAILED` 都是正常的结束方式，见 [熔断与停止条件.md](references/熔断与停止条件.md)。任何一种出现时都不得“凑数”出卡。
