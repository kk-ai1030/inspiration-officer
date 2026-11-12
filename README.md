# 灵感官 · Inspiration Officer

> 一个会**主动找事做**的科研助手：在你没下指令的时候，它去读新论文、看你的代码和数据，把值得做的事整理成可验证的卡片，送到你本机的评价页，等你一句话。

**当前状态：`1.5.9-test`（Beta，邀请测试）** · 平台：[SciencePal](https://sciencepal.ai)

---

## 为什么做这个

现在的 AI 科研助手基本都是「你说，它做」——你不说，它就停在那里。

但人和 AI 的能力结构其实是不对称的：

- **人的判断力强，但一天能投入的时间有限；**
- **AI 的判断力还不够，但它可以一直读、一直算。**

所以合理的分工不是等 AI 变聪明，而是**让它用「能一直工作」的长板，去接住我够不到的那部分**：
在没人发指令的时候，自己去找值得做的事，整理成建议，来找我申请。

> 它不替我做科研，它只是在我没下指令的时候，自己去找该做的事；
> 每一条建议都是一次申请，我的每一次评价都在把它调成更懂我的样子。

---

## 它做什么

| 能力 | 说明 |
|---|---|
| **主动探索论文** | 按你的方向抓 arXiv → 去重 → 读摘要（或全文）→ 打分 → 生成候选卡片 |
| **本地观察** | 扫你本机的代码 / 笔记 / 论文库 / 数据 / 日志，找出「函数太长」「日志里有 NaN」「笔记里说过想试 X」这类事 |
| **四类卡片** | A 优化建议 · B 知识建议 · C 假说 · D 流程改进，各自独立配额 |
| **可审计卡片** | 每张卡都有来源、逐字引文、为什么和你有关、最小验证、判据、**推翻条件**、成本估计 |
| **评价与学习** | 采纳 / 否决 / 已做过 / 不重要 / 追问 / 立刻动手 / 改写；权重按你的反馈保守调整，并给你周报 |
| **立刻动手** | 按 6 先弹「动手单」表单（场景/参数/方法/产出/对照/判据/交付），批准后才执行，产出回执含产出实测、对照实测、八项检查 |
| **本机评价页** | `http://127.0.0.1:8765/`，卡片一到达就弹 Windows 通知 |

---

## 工作流

```text
你在网页里说「主动探索灵感」
        │
        ▼
prepare ── 抓 arXiv / 读本地观察 / 去重 / 打分 / 入围
        │    （论文卡：一次读 triage_shortlisted.json → 一次写 cards.json）
        ▼
insp-card ── 生成卡片（论文卡 cards.json；观察卡 cards/NN.json）
        │
        ▼
finalize ── 校验 + 配额裁剪 + 输出 INSP_BUNDLE_JSON
        │
        ▼
本机收货员 ── 入库 → 评价页 + 通知
        │
        ▼
你的评价 ── verdicts.jsonl → 权重 / 周报 / 负面清单
```

---

## 快速开始

```powershell
# 1) 离线自检（不联网、不花 token）
python scripts\selfcheck.py g1

# 2) 本地配置 + 状态
copy platform\config.example.json config.json
xcopy /E /I insp-init\assets\灵感状态模板\inspiration inspiration

# 3) 导入技能、建灵感官（需要 SciencePal 会话令牌）
python platform\setup_platform.py --config config.json --replace
python platform\setup_platform.py --config config.json --check

# 4) 启动本机评价页
powershell -ExecutionPolicy Bypass -File platform\windows\start_review_server.ps1
```

然后打开 <http://127.0.0.1:8765/>，在 SciencePal 网页里对「灵感官」说 **“建立画像”**，
再新开一个对话说 **“主动探索灵感”**。

**完整安装步骤（含令牌、故障排查、升级、卸载）见 [INSTALL.md](INSTALL.md)。**

---

## 目录结构

```text
insp-nightly/ insp-card/ insp-profile/ …   7 个技能：导入到 SciencePal，在云端沙箱里运行
platform/                                   本机侧：安装、收货员、评价页、周报、立刻动手、本地观察
insp-init/                                  初始化模板（状态模板、画像问题）
scripts/                                    自检（selfcheck.py）、vendor 同步、打包
tests/                                      离线测试（不连平台、不花 token）
docs/                                       开发文档、发布门槛、验收节点
library/（自己建，不进仓库）                  你的资料库：code / notes / papers / data / logs
```

---

## 设计原则

1. **它不替我做决定，它替我找选项。** 所有建议都是申请，批准才执行。
2. **它是来找我聊天的，不是来刷屏的。** 按类型硬配额 + 三层去重，宁缺毋滥。
3. **它知道，不等于它会。** 每条判断都必须有来源、可追溯、可被一键删除。
4. **品味外化成可读可改的规则**，不藏在微调过的权重里；权重调整给你摘要，你确认才生效。
5. **先保住最贵的产出。** 论文卡先写，观察卡后写；出错只改被点名的那一个文件。

---

## 隐私

- 你的**原始文件不出本机**：本地观察只上传脱敏后的片段（可用 `observe.upload_code_snippets=false` 只传指标）；
- 上传到沙箱的只有「技能 + 一份本地状态快照（`state.json` / `observed.jsonl` / 画像 / 口味规则）」；
- 令牌保存在 `%USERPROFILE%\.sciencepal\`，不写进仓库、不写进日志；
- 本仓库不含任何个人配置或历史数据（见 `.gitignore`）。

---

## 已知限制（Beta）

- 论文卡的入围数量取决于检索式宽度和 `max_results`（默认 60；300–500 更稳，3000 会让 arXiv 返回 500）；
- `state.json` 里的 `seen_ids` 会记住已推送论文，重复跑同一方向可能「没有新论文」；
- 观察型卡一张一个文件，一轮 6 条候选大约多 10–12 步；
- 通知与桌面集成只在 Windows 完整验证；Linux/macOS 可用但未做完整测试。

---

## 文档

| 想做什么 | 读这个 |
|---|---|
| 第一次安装、日常使用 | [INSTALL.md](INSTALL.md) · [useguide.md](useguide.md) |
| 建本地资料库 / 接入已有仓库 | [LIBRARY_GUIDE.md](LIBRARY_GUIDE.md) |
| 看懂机制与模块 | [docs/开发文档.md](docs/开发文档.md) |
| 改代码 / 提 PR | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 测试与验收节点 | [docs/v1.5_测试验收节点.md](docs/v1.5_测试验收节点.md) |
| 发版门槛 | [docs/weak-beta发布门槛与证据.md](docs/weak-beta发布门槛与证据.md) |
| 版本变更 | [CHANGELOG.md](CHANGELOG.md) |

---

## 参与

- 发现问题：开 Issue，附 `VERSION`、复现步骤、期望/实际；
- 想改代码：先读 `CONTRIBUTING.md`，跑 `python scripts\selfcheck.py g1` 保持绿色；
- 想反馈使用体验：`python platform\collect_feedback.py --config config.json --test <节点> --result pass|fail|unsure --note "..."`，
  会在 `feedback/` 生成一个可上传的反馈包。

## 许可

[MIT](LICENSE)
