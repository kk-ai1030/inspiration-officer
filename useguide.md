# 灵感官 · 用户手册（1.5.9-test）

> 读者：第一次安装的组员，以及日常使用的人。
> 写法：每一步都写明**做什么 → 看到什么算成功 → 不成功怎么办**。照着清单一步步做，不需要先读代码。
> 机制原理见 [docs/开发文档.md](docs/开发文档.md)；改代码见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 0. 一句话 & 你会得到什么

在 SciencePal 网页里对“灵感官”说 **“主动探索灵感”**，它会自己抓 arXiv → 去重 → 读摘要（或通读全文）→ 写卡片。你电脑上的评价页会自动收到卡片，并弹出 Windows 通知。**不用上传任何文件，也没有任何定时任务**，只在你开口或点按钮时才运行。

每张卡片都是一次“申请”，包括：来源论文和逐字引文、为什么跟你有关、两天内能做完的最小验证、预期现象、什么结果说明它不成立、成本估计。你可以评“采纳 / 否决 / 已做过 / 不重要 / 追问 / 立刻动手”，也可以改写卡片。

| 功能 | 在哪里操作 | 用到的技能 / 脚本 |
|---|---|---|
| 建立 / 更新画像 | 网页对话 | insp-profile → 收货员落盘 |
| 跑一次灵感 | 网页对话，或评价页按钮 | insp-nightly + insp-card → 收货员入库 |
| 评价卡片 | 本地评价页（也可以用 Excel 或在对话里评） | review_server → record_verdict |
| 立刻动手 | 评价页标记 + 本地命令 | run_action.py |
| 周报与权重 | 本地命令 | run_weekly.py / weekly_review.py |
| 查看技能是否真的加载 | 网页技能页 + 本地命令 | selfcheck.py g3 / selfcheck.py skills / run_audit.py |

---

## 1. 安装前检查清单

| # | 检查项 | 怎么确认 |
|---|---|---|
| 1 | Windows 10/11 | — |
| 2 | Python ≥ 3.9，并且在 PATH 里 | PowerShell 里 `python --version` |
| 3 | 能登录 SciencePal 网页 | 浏览器打开 sciencepal.ai |
| 4 | 每人用**自己的**账号 | 灵感官、技能、记忆都按账号隔离，组里不能共用一个灵感官 |
| 5 | 解压路径里没有特殊字符 | 建议 `D:\insp\inspiration-skills\`。用 Windows 资源管理器或 7-Zip 解压，中文文件名不应乱码（包内文件名带 UTF-8 标志） |
| 6 | 核对包没被改过（可选） | 用 `MANIFEST.txt` 里的 sha256 核对（PowerShell：`Get-FileHash 文件 -Algorithm SHA256`） |

---

## 2. 准备两种令牌（必须来自同一个账号）

真实平台上两种令牌的权限不同（测评时实测过），**两种都要放**：

| 令牌 | 能做什么 | 不能做什么 | 存放位置 |
|---|---|---|---|
| **API Key**（`sp_live_…`） | 发起运行、读运行事件流、读写沙箱文件 | 读会话文件、列会话、读写记忆、导入技能（返回 401/403） | `%USERPROFILE%\.sciencepal\run_token` |
| **会话令牌**（网页登录后的 Bearer） | 导入技能、建智能体、读会话文件（收货）、列最近会话、读写记忆、查技能加载字段 | — | `%USERPROFILE%\.sciencepal\session_token` |

**拿 API Key**：在平台的 API Key / 访问密钥页面生成（对应接口 `/api/access-keys`），复制 `sp_live_` 开头的整串。

**拿会话令牌**（平台没有专门的入口，只能从浏览器里取；以平台实际界面为准）：
1. 登录 sciencepal.ai，按 `F12` 打开开发者工具，切到 **Network（网络）**；
2. 在页面上随便点一下（比如打开智能体列表），找一条以 `/api/` 开头的请求；
3. 在 **Request Headers** 里找到 `Authorization: Bearer xxxxx`，复制 `Bearer ` **后面**那一串。

**存放**（文件里只放令牌本身，不要带引号、空格或 `Bearer`）：

```powershell
mkdir $env:USERPROFILE\.sciencepal -Force
Set-Content -NoNewline $env:USERPROFILE\.sciencepal\run_token     "sp_live_……"
Set-Content -NoNewline $env:USERPROFILE\.sciencepal\session_token "eyJ……"
```

> ⚠ 令牌**不能**出现在包目录、对话、截图、反馈文件里。会话令牌会过期：过期后收货员自动退回“仅 API Key”模式，评价页状态行会显示出来，这时重新取一次即可。

---

## 3. 安装（约 15 分钟）

### 方式 A：脚本安装（推荐）

在包根目录打开 PowerShell，依次执行：

| 步 | 命令 | 成功的标志 | 不成功怎么办 |
|---|---|---|---|
| A1 离线自检 | `python scripts\selfcheck.py g1` | 11 项全部 `PASS`，并生成 `release_evidence\G1_*.md` | 把这份 G1 证据文件发给维护者，**先不要往下装** |
| A2 本地配置 | `copy platform\config.example.json config.json` | 根目录出现 `config.json` | — |
| A3 本地状态 | `xcopy /E /I insp-init\assets\灵感状态模板\inspiration inspiration` | 根目录出现 `inspiration\state.json` | — |
| A4 改检索方向（可选） | 编辑 `inspiration\state.json` 的 `sources[0].query` 和 `keywords`；也可以跳过，等画像访谈时自动生成 | `python insp-init\scripts\validate_insp_state.py inspiration` 输出 OK | 按报错修 JSON |
| A5 删旧版计划任务（仅从 v1.3 升级时） | `powershell -ExecutionPolicy Bypass -File platform\windows\remove_legacy_tasks.ps1` | 列出并删除 `InspNightly-*` | 没装过旧版就跳过 |
| A6 导入技能 + 建灵感官 | `python platform\setup_platform.py --config config.json --replace` | 输出“文件引用检查通过”，7 行 `skill insp-xxx: … (zip)`，`agent created/updated`，“安装检查通过” | 见 §3.3 |
| A7 清理平台上的残留定时任务 | `python platform\setup_platform.py --config config.json --cleanup-schedules`，先看列表，确认后加 `--yes` | 列表里只有灵感相关的任务；无关任务不会被删 | — |
| A8 安装检查 | `python platform\setup_platform.py --config config.json --check` | 全部 `PASS` | 按 FAIL 行的提示处理 |
| A9 启动评价页 | `powershell -ExecutionPolicy Bypass -File platform\windows\start_review_server.ps1`（想登录 Windows 时自动启动就加 `-Autostart`） | 浏览器打开 `http://127.0.0.1:8765/…`，状态行显示“会话令牌 + API Key” | 通知没弹出：`python platform\review_server.py --config config.json --toast-test` |

> `-Autostart` 只是在登录 Windows 时启动评价页服务，**不会**定时发起任何运行。

### 方式 B：网页手动安装（脚本被网络拦住时的退路）

1. `python platform\setup_platform.py --config config.json --build-only` → 在 `dist\` 下生成 7 个 `insp-*.zip`，同时完成文件引用检查；
2. 网页 **技能** 页 → **导入** → 逐个上传 7 个 zip（**必须用 zip**，用“新建技能 + 粘贴正文”的方式不会带上脚本，insp-nightly 和 insp-profile 就用不了）；
3. 网页 **智能体** 页 → **新建**：名字填 `灵感官`，系统提示词粘贴 `platform\agent_system_prompt.md` 的全文，**关闭网页搜索**；
4. 在灵感官的技能设置里勾选这 7 个技能；
5. 在根目录手写 `platform_ids.json`：`{"agent_id": "<灵感官的 ID>", "skills": {}}`。没有它，评价页按钮和账号核对都不能用；
6. 继续做方式 A 的 A7–A9。

### 3.3 安装常见报错

| 现象 | 原因 | 处理 |
|---|---|---|
| `FAIL 文件引用检查` | 某个技能的说明里引用了包里没有的文件（平台导入时会以 400 `missing_file` 拒收） | 运行 `python platform\skill_refs.py` 找到对应行；改完跑 `python scripts\sync_vendor.py` |
| `FAIL skill …: 未更新：…平台上仍是旧版本` | zip 被拒后改用文本方式创建，又遇到同名 409 | 先修好 zip 的问题，再重跑 `--replace`；`platform_ids.json` 里的 `stale` 标记会自动清除 |
| `FAIL … 是文本方式创建的，沙箱里没有脚本` | zip 导入失败 | 同上 |
| `FAIL API Key 看不到灵感官` | 两种令牌不属于同一个账号 | 在灵感官所在的账号重新生成 API Key |
| 401 / 403 | 令牌过期，或令牌类型用错 | 对照 §2 的权限表 |
| 中文文件名乱码 | 用了不支持 UTF-8 标志的解压工具 | 换 Windows 自带解压或 7-Zip |

---

## 4. 安装验收（= 发布门槛 G2；在干净机器上做）

| # | 验收项 | 通过标准 |
|---|---|---|
| G2-1 | A1 离线自检 | `G1_*.md` 结论“通过” |
| G2-2 | A6 / A8 | 7 个技能都是 `(zip)`，`--check` 全 PASS，没有 traceback |
| G2-3 | 网页技能页 | “个人”分类里有 7 个 `insp-*`；insp-nightly 的文件列表里有 `scripts/insp_run.py` 和 `scripts/vendor/find_quotes.py` |
| G2-4 | 网页智能体页 | 有“灵感官”，绑定 7 个技能 |
| G2-5 | 评价页 | 能打开，状态行两种令牌都在；`--toast-test` 能弹出通知 |
| G2-6 | 用时 | 记下从解压到 A9 一共花了多少分钟，以及卡住的步骤 |

---

## 5. 网页交互：每个功能的操作清单

> 通用规则：所有网页操作都是在 **SciencePal → 智能体 → 灵感官 → 新建会话** 里进行。右侧的“计算机”面板会实时显示它执行的命令、终端输出和退出码，这是判断“它真的做了”的直接证据，不要只看它在对话里怎么说。

### F1 建立画像（第一次必做，约 5 分钟）

| 步 | 你做什么 | 你应该看到什么 |
|---|---|---|
| 1 | 确认评价页服务开着（收货员内置在里面） | 评价页状态行正常 |
| 2 | 网页里说：**“建立画像”** | 计算机面板出现 `load_skill insp-profile`，它先复述 3 条硬纪律 |
| 3 | 它**用一条消息**问 7 个问题（身份与项目、关注方向、擅长 + 例子、不熟领域、表达偏好、arXiv 分类与关键词、不要假设我会） | 不会弹表单；如果弹了表单，说明纪律被违反，记下来反馈 |
| 4 | 简短回答。擅长的东西**要举例**，否则会被降级为“了解”；不确定分类就说“你帮我定” | 缺必填项时，它最多再追问一轮 |
| 5 | 它给出画像**预览** | 这一步还没写任何文件 |
| 6 | 回复 **“确认”**（要改就直接说改哪里） | 它运行 `build_profile.py`，回答最后附上 `<<<INSP_PROFILE_JSON … INSP_PROFILE_JSON>>>` |
| 7 | 等 1 分钟左右 | Windows 通知“用户画像已更新”；本地出现 `inspiration\profile\AGENT.md` 和 `profile.json`，`state.json` 的检索式也已更新 |
| 8 | 检查记忆页（可选） | 有若干条以 `[灵感画像]`、`[灵感配置]` 开头的记忆 |

**没收到？** 点评价页的 **检查新结果**，或者运行 `python platform\receiver.py --config config.json --thread <会话链接>`。

### F2 更新画像

在网页里说 **“更新画像”**（或“我换方向了”）→ 它先用 3–5 行复述现有画像，问你哪些要改 → 只改你提到的部分 → 预览 → “确认”。之后的流程和 F1 第 6–8 步相同。

### F3 跑一次灵感（核心功能，摘要模式 5–15 分钟）

| 步 | 你做什么 | 你应该看到什么（计算机面板 / 对话） |
|---|---|---|
| 1 | 网页里说：**“主动探索灵感”**。可以加方向，比如“今天看 surface code 解码器”；也可以临时指定模式：“…，**通读全文**”或“…，**只看摘要**” | `load_skill insp-nightly`，然后复述硬纪律 |
| 2 | 等它准备数据 | `python3 /workspace/skills/insp-nightly/scripts/insp_run.py prepare …`，输出 `MODE persisted/bootstrap/attached`，以及 `raw / seen / new / shortlisted` 计数 |
| 3 | （只在全文模式）等它抓原文 | 出现 `FIND_QUOTES python3 /workspace/fq <ID>`，之后它用 `python3 /workspace/fq <ID>` 找引文。最多读 4 篇 |
| 4 | 等它写卡 | `load_skill insp-card`，写 `/workspace/inspiration_out/cards.json` |
| 5 | 等它交付 | 运行 `insp_run.py finalize`；回答最后**原样**附上 `<<<INSP_BUNDLE_JSON … INSP_BUNDLE_JSON>>>`，然后直接结束（**不应**再调用 `complete` 交付工具） |
| 6 | 看评价页 | 1 分钟内弹出 Windows 通知，评价页顶部显示“新到 N 张卡片 · 点击载入” |

**也可以不进网页**：直接点评价页顶部的 **主动探索灵感**。按钮会自动带上已见 ID 和当前的读取模式。

**结果状态**：

| 状态 | 意思 | 你要做什么 |
|---|---|---|
| `OK` | 有新卡片 | 去评价 |
| `NO_CANDIDATES` | 没有新的、值得出卡的论文 | 正常结果。可以换个方向再说一次 |
| `FETCH_BLOCKED` | 沙箱连不上 arXiv | 不用管：收货员会自动改走本地抓取（日志在 `runs\fallback.log`） |
| `STATE_MISSING` | 状态文件损坏 | 运行 `python insp-init\scripts\validate_insp_state.py inspiration` |
| `VALIDATION_FAILED` / 收货被拒 | 整包没通过本地校验，或混入了测试 ID | 看评价页状态栏和 `inspiration\inbox\receiver_status.json`；这时**状态不会被修改** |

**运行中要留意的异常**（遇到就用 §11 的方式反馈）：
- 它弹了表单（ask）：跑灵感时禁止这样做；
- 它用 `&&` 把一串 grep 拼成一条长命令：平台会把长命令切碎，连续失败 5 次就强制停机；
- 它引用了 triage 里没有的论文，或者引用了记忆里的论文；
- 它生成了 docx / pdf；
- 它在结果块之后又调用了 `complete`（交付）工具，尤其是往 `attachments` 里塞了一句话而不是文件路径。

**网页显示“任务已中断”，但卡片已经到了？**（feedback6）
先看评价页状态栏：如果写着“最近结果 HH:MM 已入库（N 张）”，说明结果早就收到了。“任务已中断”是平台在收尾阶段的交付校验告警（典型报错 `DELIVERY_VALIDATION_LIMIT`），不影响已入库的卡片。**不要点“从断点继续”**：它只会让灵感官再跑一轮，新结果会被去重挡住，白花时间和 token。1.4.5 起技能和系统提示词都要求它“输出结果块后直接结束、不调用 complete”，这种情况应该很少再出现；如果还出现，请用 §11 的方式反馈，并附上会话链接。

### F4 在评价页评价卡片

| 按键 | 动作 | 结果 |
|---|---|---|
| 空格 | 翻面看细节 | 只记隐式信号（`implicit.jsonl`），**不影响权重** |
| 1 | 采纳 | +1.0 |
| 2 | 否决 | −1.0，来源论文进负面清单；可以填“屏蔽主题” |
| 3 | 已做过 | −1.0，进负面清单 |
| 4 | 不重要 | −0.5 |
| 5 | 追问 | +0.5，卡片仍然算“待评价”；本地观察卡会把证据文件的打码副本一起发过去；没完成时回执里有“重试追问” |
| 6 | 立刻动手 | +1.0；先弹出动手单（场景 / 参数 / 方法 / 产出 / 对照 / 判据 / 交付），点“确认开跑”才建任务 |
| E | 改写卡片（Ctrl+Enter 保存） | +0.8，自动保存改动前后的差异 |
| Z | 撤销最后一条评价 | 卡片恢复成上一条评价的状态，负面清单也一并恢复；撤销“立刻动手”会取消还没开始的任务 |
| → | 跳过 | 只记隐式信号 |

**卡片标签（v1.5.1）**：来自你本地文件的卡片标“**本地观察 · 检测器**”（长函数 / 重复代码 / 改动热点 / 性能嫌疑 / 代码小问题 / 日志 / 数据反常 / 笔记 / 使用统计）；标“补位”的是本轮没有达标的 A 类、按“每轮至少 1 张 A 类”补上来的；“有动手单”= 灵感官已经写好了动手单。

**历史（v1.5.1）**：右上角“历史”→ 已评价列表（按评价时间倒序）。**每一条都能点开**，和评价时一样显示卡片：

| 按键 | 动作 |
|---|---|
| 空格 / 点卡片 | 翻面 |
| ← / → | 上一张 / 下一张 |
| E（Ctrl+Enter 保存） | 改写，和评价时一样记一条“改写”（+0.8）；**分类不变**，列表显示“采纳 · 已改写” |
| Esc | 返回列表；列表里再按 Esc 回到评价 |

历史详情里没有 1–6 分类按钮；想改分类，只能在当场评价时按 Z 撤销后重评。

**另外两种评价方式**：
- **Excel**：打开 `inspiration\inbox\verdict_inbox.csv`，在 `action` 列填“采纳 / 否决 …”，保存后运行 `python insp-feedback\scripts\record_verdict.py --state-dir inspiration --batch inspiration\inbox\verdict_inbox.csv --config config.json`；
- **在对话里**：对灵感官说“采纳 ins_xxx，理由……”→ 它输出 `INSP_VERDICTS_JSONL` 结果块 → 把它的回复存成 txt → 运行 `python platform\run_local.py --config config.json --import-verdicts 回复.txt`。**在网页里说了不等于记下了**，导入之后才生效。

### F5 周报与权重（每周一次，在本地运行）

> 这一版的周报**只在本地跑**：评价日志 `verdicts.jsonl` 只存在你的电脑上，不会推到沙箱里，所以在网页里让灵感官做复盘，它拿不到数据。

| 步 | 命令 | 结果 |
|---|---|---|
| 1 | `python platform\run_weekly.py --config config.json` | `inspiration\reports\weekly_*.md` 和 `proposals_*.json` |
| 2 | 读周报：采纳率（健康区间 20%–60%）、两周重复率（≈0）、各类型 p̂、**本周唯一一条**权重调整提议 | 样本数 n<5 的只记录、不提议 |
| 3 | 同意就运行 `python insp-review\scripts\weekly_review.py --state-dir inspiration --apply inspiration\reports\proposals_XXX.json --confirm --config config.json` | `taste\weights.json` 的版本号 +1 |
| 4 | 后悔了就运行 `… weekly_review.py --state-dir inspiration --rollback` | 回到上一个版本 |

### F6 立刻动手（start_now）

1. 在评价页按 **6**。v1.5.1 起**按下去就建任务**（K1 已修，评价页需要用 `review_server.py --config config.json --open` 启动）：
   - 系统先把卡片整理成一份 **动手单**：场景 / 参数（数值 + 单位 + 来源：资料/文献、本地观察/实测、你提供）/ 方法 / 产出 / 对照 / 判据 / 交付；灵感官写好的就直接用，否则按卡片自动生成；
   - 动手单还缺“你提供”的参数时，会弹出动手单让你填，填完点“确认开跑”（Ctrl+Enter）；点“稍后再填”的话，它会留在“回执 → 待补动手单”里；
   - 默认（`actions.start_now_mode` = `"form"`，旧值 `"auto"` / `"confirm"` 同样处理）：按 6 先弹出预填好的动手单，核对或修改后点“确认开跑”才建任务；想动手单完整就直接开跑：`"direct"`；只想记评价不建任务：`"off"`；
   - 观察型卡片（例如 A 类代码优化）会把**卡片证据所在的本地文件**（脱敏后，≤ 2 MB）附给任务，这样沙箱里才能真的跑新旧两版；`observe.upload_code_snippets=false` 或 `actions.attach_local_files=false` 时不附；
2. 右上角“**回执**”（数字 = 等你处理的条数）：每个任务的分档、状态、会话链接、结论、动手单的“产出实测 / 对照实测 / 判据是否满足”、编排器检查（包括“动手单的交付物都在”）；T2 在这里点“批准执行”，跑完点“结果可用 / 需要重跑 / 结果作废”。任务结束时页面会弹提示。命令行的 `run_action.py --sweep` 仍然可以用来补建任务；
3. 系统按风险分档：**T0** 只读整理、**T1** 沙箱验证（这两档自动执行）；**T2** 先出方案，你运行 `run_action.py --approve act_…` 批准后才执行；**T3** 只出方案（涉及设备、湿实验、付款、对外发送），由你亲自执行；
4. 看进度用 `run_action.py --list`；回执在 `inspiration\actions\<job>\receipt.md`；
5. 确认回执：`run_action.py --ack act_… --result accept|rerun|discard --note "…"`（这一步**不影响权重**）。

> 组员评测时建议把 `config.json → actions.auto_tiers` 改成 `["T0"]`，先不让沙箱验证自动执行。

### F7 确认技能真的被加载了（评测必做）

“输出看起来对”不等于“技能真的生效了”。看这三样证据：
1. **网页技能页**：insp-nightly / insp-card 的“总加载次数”在运行后增加了；
2. **计算机面板**：出现了 `load_skill insp-nightly` 和 `load_skill insp-card`；
3. **平台字段**（最硬的证据）：`python scripts\selfcheck.py g3 --config config.json` 会查 `GET /api/agent-run/{id}/skills`，要求 `loaded=true`（只查最近一次运行）。
   想把收货员收回来的**每一次**运行都查一遍：`python scripts\selfcheck.py skills --config config.json`（默认最近 10 次，`--all` 全部重查）。结果追加到 `inspiration\audit.jsonl`；`loaded=false` 的那次运行产出的卡片，评测时不要算作技能效果。退出码 0 = 全部 true，1 = 有 false，2 = 没有会话令牌，3 = 有查不到的运行。

### F8 记忆管理（网页记忆页）

| 前缀 | 谁写的 | 怎么处理 |
|---|---|---|
| `[灵感画像]` `[灵感配置]` | 收货员，按你确认过的画像整体替换 | 不要手改；要改就走 F2 |
| `[灵感状态]` | 收货员（已推送 ID 的摘要） | 不要手改 |
| `[灵感设置]` | 收货员（论文读取模式） | 改 `config.json`，然后运行 `receiver.py --sync-settings` |
| 来源是 `auto_extraction` 的条目 | 平台自动抽取 | 可能把某次的临时要求固化成了长期偏好。周报第 6 节会列出这些条目，由你决定保留 / 改写 / 删除 |

### F9 切换论文读取模式

`config.json → arxiv_fetch_mode`：`"abstract"`（默认，快）或 `"fulltext"`（通读全文，大约慢 4–5 倍，每次最多 4 篇）。

| 怎么跑 | 什么时候生效 |
|---|---|
| 评价页按钮 | 下一次点击 |
| 网页里 @灵感官 | 评价页开着时 1 分钟内自动同步；想立刻生效就运行 `python platform\receiver.py --config config.json --sync-settings` |
| 只临时换一次 | 网页里说“…，通读全文”或“…，只看摘要” |

### F10 本地资料库与观察通道（v1.5 新增，约 10 分钟配置）

**它做什么**：在你电脑上扫描你自己的资料，把“发现”写进 `inspiration\observed.jsonl`。下一次跑灵感时，这些发现和 arXiv 新论文一起进入筛选，灵感官据此写卡（多数是 A 类优化建议）。**扫描只在本地运行**，上传到沙箱的只有入围发现的摘要和证据片段（默认每条最多 40 行）。

**第 1 步：建资料库**

```powershell
python platform\observe.py --config config.json init
```

会在包根目录建 `library\` 和五个子文件夹，往里放东西：

| 文件夹 | 放什么 | 会发现什么 |
|---|---|---|
| `library\code` | 你的物理 / 计算代码（py、c/cpp、f90、m、jl、r、sh…） | 过长或过复杂的函数、嵌套太深、重复代码块、循环里的低效写法；如果是 git 仓库，还会找“常改又复杂”的热点 |
| `library\logs` | 运行日志、输出文件 | 程序崩溃（按最后的异常行归并）、NaN / Inf、不收敛、数值序列里的离群点 |
| `library\data` | csv / tsv / dat / npy 等数据 | 缺失值、常数列、离群值 |
| `library\notes` | 科研笔记（md / txt / org） | 你写过的“有点怪 / 反常”“以后可以试试”“没搞懂”“待查” |
| `library\papers` | 收集的论文（pdf / bib / 文献清单） | 你的高频主题；和当前 arXiv 关键词的缺口（可一键并入关键词） |

**大的代码仓库不用复制**，在 `config.json → observe.roots` 里直接写原路径（`kind` 取 code / logs / data / notes / papers / mixed）：

```json
"observe": {
  "roots": [
    {"path": "library", "kind": "mixed", "label": "lib"},
    {"path": "D:\\research\\qmc-code", "kind": "code", "label": "qmc"}
  ]
}
```

没有 `observe` 这一段 = 不开观察通道，旧用户不受影响。`config.example.json` 里有带说明的完整版本（阈值、忽略目录 `exclude_dirs` / `exclude_globs`、文件数上限 `max_files`、时间上限 `time_budget_s` 等）。

**第 2 步：扫描并预览**

```powershell
python platform\observe.py --config config.json scan      # 第一次会“建立基线”
type inspiration\library\summary.md                         # 先看清单：扫了什么、发现了什么
python platform\observe.py --config config.json list      # 下次运行会入围哪几条（规则与沙箱完全一样）
python platform\observe.py --config config.json show obs_xxxxxxxxxxxx   # 看某一条的完整证据
```

**第 3 步：照常跑灵感**。评价页按钮或定时任务触发时，收货员会**先自动扫描一次**再上传（`observe.auto_scan_before_run`，默认开）。想只处理观察通道、不抓 arXiv：在网页里说“主动探索灵感，只看本地观察”（对应 prepare `--observed-only`）。

**卡片长什么样**：评价页和每日摘要里标“本地观察 · 文件位置”（如 `library://qmc/vmc.py#L12-L140`）；引文是你自己的代码行 / 日志行 / 笔记原句。评价方式和论文卡一样（1–6、E 改写）。

**它怎么避免刷屏**

| 规则 | 默认值（`state.json → observed` 可改） |
|---|---|
| 每类最多入围 | A 优化 3、B 知识 2、C 假说 2、D 流程 2 |
| 每次最多 | 6 条 |
| 严重度门槛 | 2.0（1–5） |
| 推过的 | 进 `state.pushed`，不再推；否决的进负面清单 |
| 问题修好了 | 下次扫描找不到 → 自动移出 `observed.jsonl`（记在 `library\observed_history.jsonl`） |
| 多少天没再出现 | 45 天后不再推 |

**可选增强**

- 记录你手动做过的事（用来发现“该沉淀成技能 / 模板”的重复劳动）：`python platform\observe.py --config config.json log "又手动调了一遍 colorbar" --tag 绘图`
- 读平台会话标题找高频主题：`observe.usage.conversations`，默认开（需要会话令牌；灵感系统自己的会话自动排除；`scan --no-platform` 可临时关闭）。
- 读 PowerShell 历史里运行过哪些脚本：`observe.usage.shell_history`，默认关，只记脚本名。
- 把论文库高频主题并入 arXiv 关键词：`python platform\observe.py --config config.json papers --apply-keywords`（写入 `state.json → keywords`，并打印加了哪些词）。

**常见问题**

- *扫出来一堆说明文档里的“待查”*：只把**你自己的**笔记放进 notes；别把软件文档、别人的仓库指成 notes / mixed。也可以在 `observe.exclude_dirs`（目录名）或 `observe.exclude_globs`（如 `"docs/**"`）里排除。
- *扫描很慢*：`observe.max_files`（默认 20000）和 `observe.time_budget_s`（默认 180 秒）都能调小；某一项出错或超时只会跳过这一项。
- *不想让某条再出现*：在评价页对它选“否决”或“已做过”。

---

## 6. 常用命令速查

| 想做什么 | 命令 |
|---|---|
| 打开评价页 | `python platform\review_server.py --config config.json --open` |
| 立刻检查新结果 | `python platform\receiver.py --config config.json --once` |
| 收某个指定会话 | `python platform\receiver.py --config config.json --thread <会话链接或 ID>` |
| 看收货员状态 | `python platform\receiver.py --config config.json --status` |
| 检查状态目录 | `python insp-init\scripts\validate_insp_state.py inspiration` |
| 检查真实评价数据是否一致（只读） | `python scripts\selfcheck.py g4 --state inspiration --config config.json` |
| 学习闭环进度 | `python platform\learning_check.py --config config.json` |
| 补查旧运行的技能加载情况 | `python platform\run_audit.py --config config.json`（需要会话令牌环境变量） |
| 建资料库 / 扫描 / 预览入围（v1.5） | `python platform\observe.py --config config.json init` / `scan` / `list` |
| 看一条本地发现的证据 | `python platform\observe.py --config config.json show <obs_编号>` |
| 记一条手动操作 | `python platform\observe.py --config config.json log "…" --tag 绘图` |

## 7. 安全与边界

- 不编造论文、引用、数字或运行结果；原文里没有的文献不得引用。
- 跑灵感时不弹表单；画像访谈用普通对话提问。
- 没有你的批准不执行任何动作；“立刻动手”按 T0–T3 分档。
- 不悄悄修改权重、配额或记忆：权重只在你 `--confirm` 后才变。
- 测试永远不碰真实状态：状态目录里没有 `.test_state` 标记时，含 `9999.*` 测试 ID 的结果一律拒收。
- **权威状态只有一份：本地 `inspiration\`。** 沙箱里和记忆里的都只是投影。
- **资料库不出本机**：`library\` 和 `inspiration\library\` 不进 Git、不进发布包（selfcheck g5 发现会报错）；上传到沙箱的只有入围发现的摘要和证据片段。

## 8. 升级 / 卸载

- **升级**：解压到新目录 → 把旧目录的 `config.json`、`platform_ids.json`、`inspiration\` 复制过去 → 依次运行 `selfcheck.py g1`、`setup_platform.py --replace`、`--check`。
- **从 1.5.0 升到 1.5.1**：另外运行 `python platform\migrate_state.py --config config.json`（先只看要改什么），确认后加 `--apply`（自动备份）。它只改还停在旧默认值的项：A 类每日上限 3 → 不限、每轮至少 1 张 A 类、`actions.start_now_mode` 等。如果是覆盖解压到旧目录，再跑一次 `python scripts\selfcheck.py stale --clean` 把新版已经删掉的旧文件移进 `.stale_backup\`。
- **卸载**：运行 `remove_legacy_tasks.ps1`（会一并列出 `InspReviewServer`）→ 网页里删除灵感官和 7 个技能 → 删除以 `[灵感` 开头的记忆 → 删除 `%USERPROFILE%\.sciencepal\`。

## 9. 仍需真机确认的假设

| 编号 | 假设 | 不成立时的退路 |
|---|---|---|
| V1 | 沙箱能访问 export.arxiv.org 和 arxiv.org/html | 收货员自动改走本地抓取 |
| V2 | 会话令牌能列出最近会话 | 网页里发起的会话用 `receiver.py --thread <链接>` 收 |
| V3 | 能往会话沙箱写文件 | 同一会话的第二次运行退回 bootstrap，由本地去重兜底 |
| V4 | 读会话文件的接口返回原文 | 从回答里的结果块取整包 |
| V5 | 会话令牌能写记忆 | 画像文件照常写，记忆先存进 `memory_pending.json`，之后用 `profile_tool.py --sync-memory` 补写 |

## 10. 包级验收

```text
python scripts/selfcheck.py package → 包结构 + 各技能脚本自测
python scripts/selfcheck.py g1      → 全部离线套件（T1–T20 / M1–M8 / S1–S6 / R1–R12 / O1–O15 本地观察 / 学习闭环彩排 / 各自测）
python scripts/selfcheck.py g4      → 评价写入后读取一致
python scripts/selfcheck.py g5      → 不含密钥和个人信息
python scripts/selfcheck.py g3 --config config.json   → 真实运行（需要平台）
python scripts/selfcheck.py skills --config config.json   → 收货员运行的平台 loaded 字段（需要会话令牌）
```

任何一项失败，都要保留错误输出并说明影响。不要把部分成功说成整套工作流已经可用。

## 11. 遇到问题怎么反馈

```powershell
python platform\collect_feedback.py --test F3 --run "python platform\receiver.py --config config.json --once"
python platform\collect_feedback.py --test F1 --result fail --note "第 6 步回复确认后 5 分钟没收到"
```

这两条命令会在 `feedback\` 下生成一份**已脱敏**的 Markdown，把它上传到组里约定的位置即可。脱敏规则会处理 `sp_live_`、JWT、Bearer、`?token=`、webhook。截图前请确认画面里没有令牌和邮箱。详见 [CONTRIBUTING.md §6](CONTRIBUTING.md#6-怎么提交反馈)。


## 演示重置的边界（v1.5.4）
- `demo_reset.bat` 只清数据，不会替你走画像访谈：重置后要在网页里**新开一个对话**，对灵感官说“建立画像”，回答 7 个问题。
- 平台记忆现在会翻页读全（以前只读前 20 条）：[灵感画像] / [灵感配置] / [灵感状态] 全部删除，[灵感设置] 每项只留一条；脚本最后列出剩下的灵感类记忆。
- 演示完用 `demo_restore.bat` 换回数据，再运行 `python platform\profile_tool.py --config config.json --sync-memory` 补回画像记忆。
