# CONTRIBUTING · 给改代码和做评测的组员

> 一句话：**先跑绿 → 再改 → 契约不动或者显式升版本 → 自填测试表 → 带证据提交。**
> 机制原理见 [docs/开发文档.md](docs/开发文档.md)，操作步骤见 [useguide.md](useguide.md)。

---

## 1. 开工前

1. 解压后先运行 `python scripts\selfcheck.py g1`，确认基线全绿（G1）。**基线不绿就不要开始改**，把 `release_evidence\G1_*.md` 发给维护者。
2. 建议用私有 Git 协作：`git init` 之后第一次提交整包。`.gitignore` 已经排除了 `config.json`、`platform_ids.json`、`inspiration/`、`runs/`、`dist/`、`release/`、`release_evidence/` 等本地文件。
3. 分支命名用 `feat/<主题>`、`fix/K<编号>-<主题>`、`eval/<评测名>`。一个分支只解决一件事。
4. 用自己的 SciencePal 账号和自己的 `inspiration/` 做测试；**不要把真实状态目录提交到 Git**。

---

## 2. 代码地图（哪个文件负责哪一层）

```text
              ┌──────────────── 云端：SciencePal 会话沙箱 ────────────────┐
  入口/编排    insp-nightly/  SKILL.md · scripts/insp_run.py (prepare/finalize) · scripts/vendor/*（副本，别手改）
  L0+L1       insp-triage/   scripts/triage.py               增量 · 去重 · 硬门槛 · 粗打分 · 复活机制
  L2          insp-card/     SKILL.md（金丝雀）· scripts/validate_cards.py · verify_arxiv.py · fetch_fulltext.py · find_quotes.py
  画像        insp-profile/  scripts/build_profile.py
  状态        insp-init/     scripts/validate_insp_state.py · assets/灵感状态模板/
  反馈        insp-feedback/ scripts/record_verdict.py（verdicts 唯一写入口）· weights_config.py
  学习        insp-review/   scripts/weekly_review.py
              ┌──────────────── 本地：用户的 Windows ─────────────────────┐
  平台适配    platform/sciencepal_client.py
  安装        platform/setup_platform.py · agent_system_prompt.md · skill_refs.py · memory_seeds.json
  收货        platform/receiver.py
  评价        platform/review_server.py · review_ui.html · windows/*.ps1
  兜底/共用   platform/run_local.py（finalize_cards 是收货员和本地编排共用的尾段）
  动手        platform/run_action.py
  学习/验收   platform/run_weekly.py · learning_check.py · run_audit.py
  画像        platform/profile_tool.py
  工具        platform/collect_feedback.py · probe.py · apply_test_profile.py
              ┌──────────────── 工程 ─────────────────────────────────────┐
  校验/打包   scripts/selfcheck.py · sync_vendor.py · build_release.py
  测试        tests/*
```

> 改 `insp-triage`、`insp-card`、`insp-init` 里**被 vendor 的脚本**（triage.py、validate_cards.py、verify_arxiv.py、fetch_fulltext.py、find_quotes.py、状态模板）之后，**必须**运行 `python scripts\sync_vendor.py`。只改原件不同步的话，云端跑的仍是旧代码，`selfcheck package` 也会报失败。

---

## 3. 不能随意改的契约

“契约”是指**两端由不同代码、甚至不同机器读写的格式**。改了一端、另一端没改，就会出现静默错误，或者让历史数据作废。改契约必须走 §3.6 的流程。

### 3.1 卡片字段（insp-card ↔ validate_cards ↔ 收货员 ↔ 评价页 ↔ 周报）

真源：`insp-card/references/灵感卡片契约.md`。重点：

| 字段 | 不能动的原因 |
|---|---|
| `id` = `ins_YYYYMMDD_NNN` | verdicts、actions、edits 都靠它关联 |
| `type` ∈ optimization / knowledge / hypothesis / process | 配额、学习维度、立刻动手分档都按它走 |
| `source.ref`（必须在 triage 的 shortlisted 里）、`source.quote`（逐字引文） | 去重和证据校验的根基 |
| `minimal_check` / `expected_observable` / `falsifier` / `cost_estimate` | 设计核心，缺一项就拒收 |
| `rubric_scores[].rule_id` | 周报的准则诊断要用 |
| `status` 初始值固定为 `proposed`，`verdict` 初始值固定为 `null` | 只能由 `record()` 修改 |
| `source.url` / `fulltext_url` / `read_mode` | **由脚本写入**，不能让模型写 |

### 3.2 verdicts.jsonl 格式（学习闭环的数据底座）

真源：[评价读写约定.md](评价读写约定.md)。

- **只能通过 `record()` 写入**，只追加；纠错的办法是先追加一条 `retract`，再写一条新的评价；
- 必有字段：`ts`（ISO 8601，带时区）、`card_id`、`source_ref`、`type`、`action`、`label`、`weight`、`weight_source`、`explicit`；可选字段：`reason`、`subfield`、`rubric_rule_ids`、`weights_version`、`via`、`edit_diff`、`retracts`；
- 读取一律用 `verdict_stacks()`：每张卡一票，`retract` 弹栈；“已评价”看 `judged_ids()`；
- `weight` 只是写入时的快照，学习时按当前 `config.json → weights.events` 重新计算；
- `explicit=false` 的行和 `implicit.jsonl` **永不参与调权重**。

> 这一条一旦写错，以后积累的数据就全部作废。所以改动任何读写评价的代码（record_verdict、review_server、weekly_review、learning_check、run_action）之后，都**必须**跑 `python scripts\selfcheck.py g4`，并把生成的 `release_evidence\G4_*.md` 附在提交说明里。

### 3.3 flow_version（金丝雀）

`insp-card/1.1.0` 同时出现在下面 4 处，必须一起改：

| 位置 | 作用 |
|---|---|
| `insp-card/SKILL.md`（两处）和 `assets/卡片模板/cards.json` | 只有加载了技能的模型才知道这个值 |
| `insp-card/scripts/validate_cards.py: FLOW_VERSION`（以及 vendor 副本） | 校验 |
| `platform/config.example.json: expect_flow_version` | 本地编排器的期望值 |
| `scripts/selfcheck.py: CANARY` | 防止金丝雀泄露到提示词里 |

**禁止**把它写进 `agent_system_prompt.md` 或 `run_local.build_prompt()`（selfcheck package 会检查）。否则不加载技能也能“猜对”，金丝雀就失效了。还要同步测试夹具 `tests/fixtures/fake_reply_ok.txt` 和 `tests/learning_loop_sim.py`。

### 3.4 结果块标记（云端 ↔ 本地的唯一通道）

| 标记（各占一行） | 产生方 | 解析方 |
|---|---|---|
| `<<<INSP_BUNDLE_JSON` … `INSP_BUNDLE_JSON>>>` | insp_run.py finalize | receiver.py、run_local.py |
| `<<<INSP_PROFILE_JSON` … `INSP_PROFILE_JSON>>>` | build_profile.py | receiver.py → profile_tool |
| `<<<INSP_VERDICTS_JSONL` … `INSP_VERDICTS_JSONL>>>` | 灵感官在对话里（insp-feedback） | run_local.py `--import-verdicts` |
| `<<<INSP_CARDS_JSON` … `INSP_CARDS_JSON>>>` | 旧版（只在附件模式 ③ 里兼容） | run_local.py |

块内必须是严格合法的 JSON，不能用代码围栏包裹。整包格式 `bundle_version` 以 `insp-bundle/` 开头（当前是 `insp-bundle/1.0`），必须带 `triage_min` 和 `triage_min_sha256`。

### 3.5 其它不能随意改的东西

| 项 | 原因 |
|---|---|
| `state.json` 的 `schema_version`、`seen_ids`、`pushed` 结构 | 去重的根基；两份模板（insp-init、insp-nightly）要保持一致 |
| `negative.jsonl` 的 `kind=id/topic`、`from_card`、`added_at` | 撤销时靠 `from_card + added_at` 回滚 |
| 五种结束状态 | 收货员按状态码分流 |
| `[灵感画像]`、`[灵感配置]`、`[灵感状态]`、`[灵感设置]` 这些记忆前缀 | 收货员按前缀幂等替换；改了会导致记忆重复堆积 |
| 技能目录名和 frontmatter `name` | setup 和平台绑定都靠它 |
| 测试 ID 前缀 `9999.` 和 `.test_state` 标记 | 测试隔离 |

### 3.6 确实要改契约时

1. 先开 issue 或在群里说明：改什么、为什么改、旧数据怎么办（迁移，还是兼容读取）；
2. 升版本号：卡片契约升 `flow_version`，整包升 `insp-bundle/x.y`，状态升 `schema_version`；
3. 两端一起改，读取端**兼容旧格式至少一个版本**；
4. 更新真源文档、CHANGELOG 和测试夹具；
5. 跑 §4 的全部测试，外加 `selfcheck g4 --state <你的真实状态>`。

---

## 4. 改完必须跑的测试（自填表，贴进提交说明）

> 只改文档的话，可以只跑 1、9。其它改动按“改了什么”一列勾选需要跑的项；**G1 每次都要跑**。

| # | 命令 | 什么时候必须跑 | 通过标准 | 结果（填） |
|---|---|---|---|---|
| 1 | `python scripts\selfcheck.py package` | 每次 | `OK: 包结构有效（7 个 Skill）` | ☐ |
| 2 | `python scripts\selfcheck.py g1` | 每次（包含 1、3–6） | 11 项 PASS，生成 G1 证据 | ☐ |
| 3 | `python tests\run_offline_e2e.py` | 改了技能、triage、卡片、收货员 | `ALL OFFLINE E2E TESTS PASSED` | ☐ |
| 4 | `python tests\mock_receiver_test.py` | 改了 receiver、sciencepal_client | `ALL MOCK RECEIVER TESTS PASSED` | ☐ |
| 5 | `python tests\mock_setup_test.py` | 改了 setup、skill_refs、SKILL.md | `ALL MOCK SETUP TESTS PASSED` | ☐ |
| 6 | `python tests\learning_loop_sim.py` | 改了反馈、周报、权重 | 退出码 0 | ☐ |
| 7 | `python scripts\selfcheck.py g4` | 改了任何读写评价的代码 | 全部 PASS | ☐ |
| 8 | `python scripts\selfcheck.py g4 --state inspiration --config config.json` | 同上，并且你本地有真实数据 | 全部 PASS（只读） | ☐ |
| 9 | `python scripts\selfcheck.py g5` | 每次（尤其是改了文档或夹具） | PASS | ☐ |
| 10 | `python scripts\sync_vendor.py --check` | 改了被 vendor 的脚本 | 退出码 0 | ☐ |
| 11 | `python platform\setup_platform.py --config config.json --replace` 然后 `--check` | 改了技能、系统提示词 | 7 个技能都是 zip，全 PASS | ☐ |
| 12 | 网页真跑一次 F3 + `selfcheck.py g3 --config config.json` | 改了技能、insp_run、收货员 | 自动检查 PASS + 人工抽查打勾 | ☐ |
| 13 | `python scripts\selfcheck.py skills --config config.json` | 同上；评测时统计“哪些卡是技能真的加载后产出的” | 退出码 0（每次运行 insp-nightly / insp-card 都是 `loaded=true`） | ☐ |

**提交说明模板**：

```text
[fix/K1] 评价页按 6 后自动建任务
改了什么：review_server.py（start_now 后调用 run_action 建任务）、review_ui.html（回执面板）
为什么：K1，用户必须手动运行 --sweep
契约影响：无 / 有（写清楚）
测试：1 ☑ 2 ☑（G1_xxx.md）7 ☑ 9 ☑ 12 ☐（还没真跑）
风险 / 回滚：……
```

**新增功能必须同时新增测试**：离线可测的放进 `tests/run_offline_e2e.py`，按 `T21`、`T22`… 继续编号；涉及平台接口的放进对应的 mock 测试。测试不能联网，不能依赖真实令牌（G1 会清掉 `SCIENCEPAL_*` 环境变量再运行）。

---

## 5. 代码风格与硬约束

- 只用 Python 标准库（`pypdf` 是可选的兜底），兼容 Python 3.9+，Windows 优先：读写文件一律写明 `encoding="utf-8"`，子进程带 `-X utf8`；
- 每个技能脚本都要支持 `--self-test`，`selfcheck package` 会调用它；
- 技能的 SKILL.md：frontmatter 只能有 `name` 和 `description`，description ≤ 2000 字符；**不得引用其它技能目录下的文件**；不得残留 TODO/FIXME/TBD；
- 只在沙箱里运行的命令要短，单行不超过 80 字符，不用 `&&` 串联；
- 新的失败路径必须落到五种结束状态之一，并且不能修改本地状态；
- 不打印令牌；日志里会话 ID 只保留前 8 位；
- 发布包只能用 `python scripts\build_release.py` 打，不要手工压缩。

---

## 6. 怎么提交反馈

| 类型 | 怎么做 | 放到哪里 |
|---|---|---|
| 测试失败 / bug | `python platform\collect_feedback.py --test <编号> --run "<出问题的命令>"`，它会生成已脱敏的 `feedback\<编号>_<时间>.md` | 组里约定的共享目录 / Git issue |
| 没有命令可附的问题（文档看不懂、卡在某一步） | `python platform\collect_feedback.py --test <编号> --result fail --note "第 N 步……"` | 同上 |
| 评测结论 | 用 [docs/weak-beta发布门槛与证据.md](docs/weak-beta发布门槛与证据.md) 第 3 节的评测记录模板 | 同上 |
| 网页现象 | 附截图之前，先确认画面里没有令牌、邮箱和完整的会话 ID | 同上 |

**编号约定**：安装用 G2-x，真实运行用 F3-x，评价用 F4-x，代码问题用 K-x 或你自己的分支名。
**一份好反馈的标准**：写清楚做了什么（命令 / 网页上说的话）、期望看到什么、实际看到什么，附上证据文件，并说明是否能复现。

上传前自查：`python scripts\selfcheck.py g5 feedback\你的文件.md --deny-file <你的敏感词文件>`。
