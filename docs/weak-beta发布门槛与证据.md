# weak-beta 发布门槛与证据（1.4.5-weakbeta）

> P0 门槛**必须全部满足**才能把包发给组员。每个门槛都要有**证据文件**，不接受口头的“我跑过了”。
> 证据统一放在 `release_evidence/`（这个目录不进 Git，也不进发布包）。发版时把证据文件上传到组里的发布记录。

## 1. 门槛总表

| 门槛 | 测试 | 为什么必须 | 怎么验 | 证据 | 谁来做 |
|---|---|---|---|---|---|
| **G1** 离线回归在 Windows 上全部通过 | 11 个离线套件 | 组员改代码之前需要一个可信的基线 | `python scripts\selfcheck.py g1` | `G1_*.md`，结论“通过”，系统一栏显示 Windows | 发布人，在 Windows 上 |
| **G2** 干净的 Windows 机器能按文档装好 | 用户手册 §3 方式 A + §4 验收表 | 装不上就收不到任何反馈 | 找一台**没装过**的机器（或新建一个 Windows 用户），只看 useguide.md 来装 | §2.2 表格 + 用时 + 卡住的步骤 | 一位**没参与开发**的组员 |
| **G3** 至少 1 次真实运行走完全程 | 网页 F3 + 抽查 | 证明整条链路在真实平台上是通的；技能必须是 loaded=true | 网页里说“主动探索灵感” → 收到结果后运行 `python scripts\selfcheck.py g3 --config config.json [--online]` → 人工抽查 | `G3_*.md`：自动检查 PASS + 人工抽查打勾。出卡并通过抽查，或者“0 张卡 + 写清原因”，都算通过 | 发布人 |
| **G4** 评价写入后读取一致 | 11 张卡 × 动作组合模拟 + 真实数据只读审计 | 这是学习闭环的数据底座，错了以后的数据全要作废 | `python scripts\selfcheck.py g4`；有真实数据时再跑 `g4 --state inspiration --config config.json` | `G4_*.md`（两份） | 发布人；组员改了评价读写代码时也自己跑，把证据附在提交说明里 |
| **G5** 不泄露密钥和个人信息 | 白名单打包 + 内容扫描 | 发给别人的东西一旦泄露就收不回来 | `python scripts\build_release.py`（内置对 zip 的 G5 扫描），再用自己的敏感词扫一次：`python scripts\selfcheck.py g5 release\inspiration-skills-1.4.4-weakbeta.zip --deny-file <本地敏感词文件>` | `G5_*.md`（两份：打包时一份，带敏感词一份） | 发布人 |
| **G6** CONTRIBUTING.md | 文档审阅 | 组员需要知道哪里能改、哪里不能改、改完怎么证明没改坏 | 对照 §2.4 的清单审阅 | 审阅人签名 | 发布人 + 1 位审阅人 |

## 2. 本次发布的门槛状态

### 2.1 构建侧已完成的（2026-10-29，在 Linux 构建机上）

| 门槛 | 结果 | 说明 |
|---|---|---|
| G1（Linux 预跑） | 10/10 PASS | **只能算预跑**。G1 要求在 Windows 上跑，发布人还要在 Windows 上重跑一次并附证据 |
| G4 模拟 | 20/20 PASS | 构建时发现并修复了 v1.4.4 的一个读取不一致：周报采纳率把已撤销的正向评价也算了进去（详见 CHANGELOG） |
| G5 打包扫描 | PASS | 白名单打包；不含 config / platform_ids / inspiration / runs / dist / 令牌；内容扫描没有发现密钥、邮箱、个人目录或会话 ID。**还需要发布人用自己的敏感词文件再扫一次** |
| G6 | 已编写 | CONTRIBUTING.md 包含代码地图、契约（卡片字段、verdicts.jsonl 格式、flow_version、结果块标记）、自填测试表、反馈方式。**还需要审阅人签名** |

### 2.2 必须在 Windows / 真实平台上补的（发布前打勾）

| # | 项 | 结果 | 证据文件 / 备注 |
|---|---|---|---|
| ☐ | G1 在 Windows 上 10/10 PASS | | |
| ☐ | G2-1 干净机器 A1 离线自检通过 | | |
| ☐ | G2-2 setup：7 个技能都是 (zip)，`--check` 全 PASS，没有 traceback | | |
| ☐ | G2-3 网页技能页：7 个技能都在，insp-nightly 带 `scripts/vendor/find_quotes.py` | | |
| ☐ | G2-4 网页智能体页：灵感官绑定了 7 个技能 | | |
| ☐ | G2-5 评价页能打开，`--toast-test` 能弹出通知 | | |
| ☐ | G2-6 用时 ____ 分钟；卡住的步骤：____ | | |
| ☐ | G3 自动检查 PASS（insp-nightly loaded=true；有卡时 insp-card 也是 loaded=true） | | |
| ☐ | G3 人工抽查：arXiv 标题对得上、引文是逐字的、minimal_check 可执行 | | |
| ☐ | G4 真实数据只读审计 PASS | | |
| ☐ | G5 用发布人的敏感词再扫一次 PASS | | |
| ☐ | G6 审阅人签名：____ | | |

### 2.3 G3 的判定细则

- 状态必须是 `OK` 或 `NO_CANDIDATES`，而且这次运行已经写进本地 `runs.jsonl`（说明收货员收到了）；
- **0 张卡也算通过**，但必须写清原因：triage 计数（例如 `new=0`）、去重记录或校验记录，至少有一项能解释为什么没有卡；
- `loaded=true` 指的是**平台字段**（`GET /api/agent-run/{id}/skills`），不是看输出像不像。如果 g3 找不到 agent_run_id，就用 `--agent-run-id` 指定；还是拿不到时，退一步用网页技能页“总加载次数”增加的前后截图作为辅助证据，并在报告里写明是降级证据；
- 人工抽查至少 2 张卡（只有 1 张就抽 1 张）：打开 arXiv 链接核对标题；在摘要或原文里找到引文；判断 minimal_check 是否两天内可做。

### 2.4 G6 审阅清单

- ☐ 代码地图覆盖了 7 个技能、platform/ 下所有脚本、scripts/、tests/
- ☐ 列出了卡片字段契约，并指向真源文档
- ☐ verdicts.jsonl 的格式、读写规则和“只能通过 record() 写入”写清楚了
- ☐ flow_version 的 4 处同步位置和“不得写进提示词”写清楚了
- ☐ 4 种结果块标记都列出了产生方和解析方
- ☐ 有改契约的流程（升版本、兼容旧格式、更新文档和测试）
- ☐ 有自填测试表和提交说明模板
- ☐ 有反馈方式（collect_feedback、脱敏、上传位置）

## 3. 发布包里放什么、不放什么

`scripts/build_release.py` 按**白名单**打包，所以新增的本地文件默认不会被带进去。

| 放进包 | 为什么 |
|---|---|
| 7 个 `insp-*/` 技能目录（含 vendor 副本和状态模板） | 安装需要 |
| `platform/`（含 `config.example.json`、`agent_system_prompt.md`、`memory_seeds.json`、`windows/*.ps1`） | 本地运行需要；配置只给示例 |
| `scripts/`、`tests/`（含 `tests/fixtures/`，全部是 `9999.*` 测试 ID） | 组员要能复现基线（G1） |
| `README.md`、`useguide.md`、`CONTRIBUTING.md`、`CHANGELOG.md`、`评价读写约定.md`、`docs/`（含示意图） | 文档 |
| `VERSION`、`.gitignore`、`MANIFEST.txt`（打包时生成，含 sha256） | 版本标识、Git 协作、完整性核对 |

| 不放进包 | 为什么 |
|---|---|
| `config.json`、`config_exp.json`、`*.bak-*` | 你的本地配置，可能含 webhook 和个人路径 |
| `platform_ids.json`、`probe_report.json` | 你账号下的智能体 ID 和探测结果；组员要用自己的账号重新 setup |
| `inspiration/`、`inspiration_exp/` | 你的真实卡片、评价、画像，属于个人数据 |
| `runs/`、`feedback/`、`release_evidence/` | 含会话 ID 和运行日志 |
| `dist/` | 构建产物；setup 每次会根据源码重新生成，带上旧的反而可能和源码不一致 |
| `__pycache__/`、`.DS_Store` 等 | 垃圾文件 |
| `%USERPROFILE%\.sciencepal\*` | 令牌本来就不在包目录里，扫描规则会再兜一次底 |

**注意事项**：
1. 每次都要从**干净目录**打包。不要在你日常使用的目录里手工压缩，因为那里有 `config.json` 和 `inspiration/`；
2. 测试夹具只能用 `9999.*` 这类假 ID。真实 arXiv ID 写进夹具，会被当成“已见”混进别人的去重逻辑；
3. 中文文件名一定要用 `build_release.py` 打包（会设置 UTF-8 标志位）。Linux 自带的 `zip` 打出来的包在 Windows 上会乱码；
4. 文档和截图里不要出现完整的会话 ID、邮箱和机构账号。G5 能扫文本，但扫不了图片，**图片要人工检查**；
5. 用 `MANIFEST.txt` 核对组员拿到的包和你发出的包是否一致。

## 4. 组员评测记录模板

```markdown
### 评测 <编号> · <日期> · <评测人>
- 包版本：1.4.5-weakbeta（MANIFEST 首行）
- 环境：Windows __ / Python __ / 令牌：会话 + API Key | 仅 API Key
- 做了什么：网页上说的原话 / 执行的命令
- 结果：status=__，卡片 __ 张，耗时 __ 分钟；loaded（g3）：nightly=__ card=__
- 抽查：卡片 ID / 标题对得上 ☐ / 引文逐字 ☐ / minimal_check 可执行 ☐
- 卡片质量主观分（1–5）与理由：
- 异常：弹了表单 / 用了长命令 / 引用了 triage 以外的论文 / 生成了 docx ……
- 证据：feedback\xxx.md、release_evidence\G3_xxx.md
```
