# CHANGELOG

## 1.5.9-test（2026-11-01）· 论文卡（B 类）+ arXiv 抓取回到 v1.4.6 的流程；其余保持 1.5.7

**基线是 1.5.7，不是 1.5.8**：1.5.8 的后台抓取 / wait 整个撤掉（它在 Windows 上让 D1 必失败，Y2 也没变快）。

- 现象（v158 Y2，thread cd2ff09c）：prepare 之后模型逐个 cat 8 个 cand_*.md + 6 个 obs_*.md + 2 个画像文件，第 21 步才写第一张卡，再逐张写 9 个文件，整轮 20+ 分钟。根因是 1.5.5 起的“每篇一个 cand 文件 + 每张卡一个文件”把论文卡从 v1.4.6 的 3 步（读 → 写 → finalize）拉长成了约 20 步，每一步都是一次模型调用。
- 论文卡（arXiv 入围候选，B 类知识卡为主）= v1.4.6 流程：
  - prepare：抓取 / 增量 / 去重 / 打分 / 入围与 v1.4.6 一致（函数逐字相同，E10）；同步执行，不再后台化；不再生成 cand_*.md；
  - 读：一次读完 triage.json 的 shortlisted。因为 500 条时 triage.json 约 150 KB 会被平台截断，prepare 把同样的 shortlisted 另存成小文件 `triage_shortlisted.json`（只留标题、摘要等出卡字段，还带 run_id / flow_version / REMAINING）；它是 prepare 最后写的文件，存在 = prepare 已跑完；
  - 写：一次写成 `/workspace/inspiration_out/cards.json`（v1.4.6 的根节点格式）；
  - 对照测试 E9：同一份 arXiv 列表 + 同一份 cards.json，1.5.9 与 v1.4.6 的 COUNTS、入围 ID、入库卡片（id / ref / 链接 / 类型）完全一致。
- 观察型卡（A 类等）保持 1.5.7：读 obs_*.md，一张卡一个文件 cards/NN.json；在论文卡写完之后写。
- finalize：合并 cards.json（论文卡）+ cards/NN.json（观察型卡）；合并结果写 `cards_final.json`（只是输出，永不回读——CARD1 的复活问题仍然不会出现）；id 撞了自动给单卡文件那张改号；`--drop <卡片 id>` 也能去掉 cards.json 里的论文卡；报错标明“cards.json [id]”或“cards/NN.json [id]”。超配额自动进 rejected、第 3 次 AUTO_PARTIAL 等 1.5.7 行为不变。1.5.7 留在会话里的旧 cards.json（带 assembled_from）不当输入。
- 指令同步：insp-nightly / insp-card 技能正文、系统提示词第 13 条、评价页“主动探索灵感”触发指令（论文部分回到 v1.4.6 的“只读摘要写卡片，再运行 finalize”）、【自动续跑】消息。prepare 被平台转后台时：watch_task 等它结束，不读旧文件。
- `RUNNER_VERSION` → `insp-run/1.5.9`；新增 `tests/v159_regression_test.py`（E1–E10；E9/E10 需 `INSP_V146_DIR`），并入 selfcheck g1；改了 T11c / P9 / C1 / C3 的断言（cards.json 不再是 finalize 的输出）。技能有改动，升级后要 `setup_platform.py --replace` 重新导入。

## 1.5.7-test（2026-11-01）· 修 CARD1：删掉的卡片“复活”、finalize 反复失败把余额烧光

- 现象（1.5.6 实测）：一轮写了 13 张卡 → finalize 连续失败 6 次 → 模型删了 9 次文件（含 3 次删 cards.json）→ 第一个 run 10 分 42 秒后 Insufficient Balance；第二个 run 才交出 7 张。
- 根因 1（主因，1.5.5 P9 引入的回归）：`insp_run.py::assemble_cards()` 先读上一次 finalize 写出的 `cards.json`，再让单卡文件按 id 覆盖——**删掉的单卡文件会从 cards.json 合并回来**；finalize 又把合并结果写回 cards.json，形成闭环（9 个文件读出 13 张）。
  - 修：`cards/` 里只要有单卡文件，就**只认单卡文件**，cards.json 完全不读；finalize 写出的 cards.json 带 `assembled_from: "cards/"`，只是输出（给收货员兜底用），以后也不会被当输入。cards/ 里一个单卡文件都没有、且 cards.json 是手写的（没有这个标记）时，才按旧写法读 cards.json。
- 根因 2：**合格但超配额的卡被当成“错误”**，finalize 失败，模型只能靠删文件补救。
  - 修：选择器本来就按分数保留高的；超配额的卡现在打印为 `QUOTA_TRIMMED`（“不是错误，不要删文件”），自动进 bundle 的 `rejected`（`auto: "quota"`），finalize 一次通过。
- 根因 3：配额只在 finalize 时才暴露，记忆里的配额数又是旧的（记忆写 process 1/2，状态里已是 2/2）。
  - 修：prepare 在写卡规则前打印 `REMAINING {...}`（与 finalize 同一套窗口计算），写明已满的类型“不要写”、“以这一行为准，不要用记忆里的配额数字”。
- 根因 4：重试没有刹车，报错还叫人“修改 cards.json”。
  - 修：报错改为点名单卡文件（`02.json [ins_…] …`），提示用 `finalize --drop`；失败次数记在 `.finalize_attempts.json`（prepare 清零），**第 3 次仍不过就自动只交通过的卡**（`AUTO_PARTIAL`，缺陷卡进 rejected，`auto: "validation"`）。
- 新增 `finalize --drop <文件名|卡片 id|source.ref>`：脚本把单卡文件移到 `cards/_dropped/` 并记进 `cards/rejected.json`，不再依赖平台的删除工具（实测有一次删除没生效）。
- 观察型 quote：跨两行、去掉了 `  97| ` 行号前缀也算逐字（CARD1 第 13 张因此被误判）。
- 提示词同步：insp-nightly / insp-card SKILL.md、熔断与停止条件、运行协议、开发文档、agent_system_prompt.md、评价页触发的运行指令都改成新规则（REMAINING / QUOTA_TRIMMED / --drop / 不碰 cards.json / 第 3 次自动收尾）。技能有改动，升级后要 `setup_platform.py --replace` 重新导入。
- `RUNNER_VERSION` → `insp-run/1.5.7`；新增 `tests/v157_regression_test.py`（13 项 C1–C10，含 CARD1 原场景“删 4 个文件后 9 个文件读 9 张”），并入 selfcheck g1。

## 1.5.6-test（2026-10-31）· 修 ASK1（追问 A 类卡失败）+ 按 6 恢复 1.5.1 动手单表单 + 合入 nowindow 补丁

- ASK1 追问本地观察（A 类）卡 → 平台 `stream_repetition` 中止、评价页只写“平台运行状态：failed”：
  - 追问 prompt 按来源分支（`ask_detail.build_prompt`）：本地观察卡写明 `evidence_ref`，不再套论文模板的“自己打开上面的原文链接去读”；论文卡（有链接 + 摘要全文）行为不变。
  - 追问也附本地证据：复用“立刻动手”的 `run_action.attach_local_evidence`，把证据所在文件的打码副本（`local_<文件名>`）随追问上传到 `/workspace/_refs/`，prompt 让模型直接读附件；关掉 `observe.upload_code_snippets` / `actions.attach_local_files` / `ask.attach_local` 时不附，prompt 改为“沙箱里没有原文件，只依据片段，不要用 find / ls / search_memory 去找”。
  - 所有追问 prompt 增加第 6 条：同一条命令 / 同一个查询不要重复执行，一次没找到就停止查找直接回答。
  - 平台判“重复调用”中止（`failure_reason=stream_repetition`，recoverable）→ 在同一会话自动重试一次（【自动重试】不要调用工具，直接按四行模板回答）；`ask.auto_retry`（默认 1，0 = 关）。重试成功：已回答 + 备注“第一次被平台中止，已自动重试”。
  - 仍失败：`ask.json` 记 `failure_reason`，评价页写清原因（“模型反复调用同样的工具，被平台中止（已在同一会话自动重试 1 次，仍没有完成）”），回执里失败 / 超时的追问有“重试追问”按钮（`POST /api/ask/retry`，不重复记评价）。
- K1 按 6“立刻动手”恢复 1.5.1 的动手单表单：按 6 先弹出预填好的动手单（场景 / 参数〔数值 + 单位 + 来源 library·observed·user〕/ 方法 / 产出 / 对照 / 判据 / 交付），核对或修改后点“确认开跑”才建任务；Esc = 稍后再填（回执 → 待补动手单）。
  - `actions.start_now_mode`：新默认 `form`；旧值 `auto` / `confirm` 都按 `form` 处理（不用改 config.json）；`direct` = 旧 auto（动手单完整就直接建任务）；`off` = 只记评价。
  - 已经有任务的卡再按 6：直接显示那条任务，不弹第二张表单。
  - 表单页面与 1.5.1 逐字一致（openBrief / drawBrief / submitBrief 未改）。
- 合入 nowindow 补丁（原样，见下一节）：后台子进程不再弹黑框；`MANIFEST.txt` 由打包脚本重算。
- 新增 `tests/v156_regression_test.py`（18 项：A1–A10 追问、F1–F6 动手单表单），并入 selfcheck g1；`ui_e2e_test` U4a 改为“config 写 auto 时按 6 先弹出七段动手单、还没建任务”。

## nowindow 本地补丁（合入 1.5.6-test）· 后台子进程不再弹黑框

- 现象：后台（`pythonw.exe`，没有控制台）周期性发布快照时会闪出 cmd / PowerShell / bash 窗口。
- 根因：`platform/observers/codescan.py` 的 `git_churn()` 用 `subprocess.run(["git", …])` 调 git，没传 `CREATE_NO_WINDOW`；Windows 会给 console 子进程新建控制台窗口。周期性发布（`receiver.py` 每 30 分钟 `publish_snapshot(scan=True)`）→ `codescan` → 每个 `observe.roots` 一次 `git rev-parse`（5 个目录 = 5 个窗口）。
- 修复：
  - 新增 `platform/_spawn.py`：`NO_WIN` / `is_console_tool()` / `kw()` / `run()` / `popen()`，只对 console 工具（git、schtasks、taskkill、powershell、cmd、bash、python…）或 `shell=True` 注入 `CREATE_NO_WINDOW`，对 `explorer`/`chrome` 等 GUI 程序不加；已有的 `creationflags` 用按位或合并。
  - `codescan.git_churn()` 两处 git 调用改走 `_spawn.run`（周期性发布的窗口来源）。
  - `collect_feedback.py` 的 git / shell / schtasks 三处、`demo_reset.py` 的 taskkill 一并改走 `_spawn.run`（手动生成反馈、重置演示时也不会闪）。
  - 新增 `tests/no_window_test.py`（18 项：辅助函数行为 + 三处调用点接线 + 只依赖标准库），并入 `selfcheck g1`。
  - `MANIFEST.txt` 补上两个新文件并重新计数（151 项）。
- 验证：`pythonw -X utf8 platform/observe.py --config config.json scan` 前后 `conhost` 进程数不变（0 增量）；`selfcheck g1` 20 组全 PASS；`selfcheck stale` 通过。
- 兼容：非 Windows 上 `NO_WIN=0`，`kw()` 不注入任何 flag，行为与之前完全一致。

## 1.5.5-test（2026-10-31）· 修 v1.5.4 测试报告

- P9 写卡前停住（“现在写 cards.json”后就结束，平台判完成、收货员无货可收、没有通知）：
  - 一张卡一个文件：/workspace/inspiration_out/cards/01.json、02.json …（被筛掉的写 cards/rejected.json）；finalize 自己合成 cards.json 再校验，中途停住已写的卡不丢。
  - prepare 为每个候选生成 cand_<ID>.md / obs_<ID>.md（标题+摘要），不再整份读 triage.json；技能正文、按钮提示词、灵感官系统提示都写明“读一篇写一张；说了要写就必须调用写文件工具；finalize 打印结果块前不得结束”（系统提示第 13 条）。
  - 收货员兜底（rescue_incomplete）：发现会话跑过 prepare（有 run_meta.json）、没有 bundle.json 且运行已结束 → 在同一会话自动发一次【自动续跑】（已写的卡不重写，补齐后直接 finalize）；续跑后仍无卡 → runs.jsonl 记 NO_OUTPUT、评价页标红“本轮没有产出卡片，可重跑”并弹通知；每个会话只续跑一次。按钮入口和网页对话入口都覆盖。
- P11 网页里说“主动探索灵感”没有 A 类卡：网页对话不经过本地，沙箱里没有 observed.jsonl 和已推送清单。新增 platform/publish_state.py：把本地状态（state.json、observed.jsonl、negative.jsonl 等）作为快照发布进平台上的 insp-nightly 技能（assets/local_state/），prepare 优先用快照（MODE skill_snapshot；同会话内更新的持久化状态优先）；没有本地观察时计数里标 observed_missing:true。发布时机：demo_reset 第 5 步（先 scan 再强制发布）、每次收货后、评价页后台每 30 分钟有变化才发布、手动 publish_state.py。评价页显示“网页入口快照 hh:mm（本地观察 N 条）”，失败标红。config.json → observe.publish_snapshot=false 可关闭（代价：网页入口没有 A 类）。快照目录不进发布包。
- P5/P6 会话链接拿不到 project_id（API Key 查不了会话列表）：查不到时改用会话令牌经 dashboard 反查。
- P12 平台会话标题为空：dashboard 的 threads 按 project_id 并上 projects[].name。
- P3 旧追问回答反复弹出：只弹最新一条并提示“另有 N 条未读追问”，点开后全部记为已读。
- P2 追问找不到摘要：收货时把摘要直接存进卡片 source.abstract（不再依赖 runs/ 是否同步）。
- P10 A 类悄悄用完：评价页显示“A 类候选：待用 N（已出卡 · 已否决）”；优化类发现出卡 14 天后代码里仍在，会以“仍未修复：…”换新 id 再提醒一次，最多两次，被否决过的不复活。
- 修：收货员给卡片补摘要时变量名覆盖了“已收货”键（会导致同一整包被重复处理）；不再改动整包对象（其哈希就是已收货键）。
- 新增 tests/v155_regression_test.py（22 项），并入 selfcheck g1；run_offline_e2e T11c 改为逐张写卡文件。

## 1.5.4-test（2026-10-31）· 修 v1.5.3 测试报告

- S3b 追问反过来向用户要论文：追问 prompt 现在带上摘要页 / PDF 链接和运行时已抓到的摘要全文（runs/<run>/triage.json）；规则改为“需要细节自己打开链接去读，不要向我索要论文；读不到写待测并给最接近推断”；追问默认允许联网（ask.web_search_on=true）；ask.json 保存当时的 prompt。
- S3 偏差：评价页底部提示“来聊聊新的idea吧”一直保留，直到你点开（24 小时内的回答，浏览器本地记住已看过）；追问通知也写 inbox/toast_last.json。
- S4b CLI / Excel / run_local 记录否决（以及已做过 / 不重要）时，同样取消这张卡没结束的立刻动手任务和追问（record_verdict.cancel_pending），命令行会打印取消了什么。
- S5 运行中取消的文案：按“从什么状态取消”判断（running → 已取消（执行中被打断）· 平台那边正在收尾；平台跑完 → 已完成 · 结果已丢弃 / 已取消（执行中被打断））；新增“已取消（方案已出，未执行）”。平台运行一开始就写进 job.json → runs（以前跑完才写），取消时能发出停止请求；migrate_state 修 v1.5.2 留下的 run_status=None。
- S6 会话链接 404：网页地址是 /projects/<project_id>/thread/<id>。建会话时记录 project_id（initiate 返回 / dashboard 反查）；旧记录走评价页 http://127.0.0.1:8765/go/thread/<id>，点开时反查并跳转（结果缓存在 inbox/thread_projects.json）。回执、追问、通知、收货员触发链接全部改。
- S7 B 类一轮 6 张：上限按“这一轮”算（同一会话 / 同一整包 run_id 之前已入库的 knowledge 一起计），被截掉的写“超出 knowledge 本次上限（每次最多 5 张，这一轮之前已入库 N 张）”；selection.json 新增 rejected_before_selection（去重 / 负面清单等原因）和 knowledge_prior；卡片 _trace 记 bundle_run_id。
- S8 平台记忆只删 1 条：GET /api/memory 默认只回 20 条，list_memory 改为 limit=100 自动翻页读全（也修了 [灵感设置] 重复写入、收货员记忆 upsert、周报记忆导出）；demo_reset 删除后列出剩余灵感类记忆，[灵感设置] 每项只留一条。
- 新增 tests/v154_regression_test.py（25 项），并入 selfcheck g1。

## 1.5.3-test（2026-10-31）

### 已知问题修复（v152_report）
- selfcheck 只检查包内文件（新 pkg_files）：状态目录里 actions/*/attach/local_*.py（含 «已脱敏» 的副本）、library/、research/ 不再被当源码编译，跑过 T1 附件任务后 g1 不再永久失败。
- 回执取消状态说清楚：已取消（未开始）/ 已取消（执行中被打断）/ 已完成 · 结果已丢弃（取消于 …），后面跟原因（因撤销「立刻动手」/ 因评价改为否决）；显示“取消于”。
- 否决一张卡时，同时取消它还没结束的立刻动手任务和追问，并提示。
- 每次尝试的 runs 记录都写结束时间和状态，不再留 run_status=None 的半截记录。
- 回执页“打开会话 ↗”链接修复：去路径的正则会把 https:// 里的 “s:/” 当成盘符，把链接截断。

### 需求
1. 评价页隐藏“已做过”“不重要”两个按钮（快捷键 3、4 也不再响应）；接口、权重、Excel/CLI 都照常接受这两个动作。剩下 4 个按钮保留原快捷键：1 采纳 · 2 否决 · 5 追问细节 · 6 立刻动手。config.json → review.hidden_actions 可改。
2. 追问细节（5）：照常记录评价，并用固定模板（研究对象+子领域 / 模型或手段 / 产出 / 对照 / 判据）新开平台对话向灵感官提问；回答后 Windows 通知“来聊聊新的idea吧”，点击或自动（ask.open_thread）打开这个对话；Z 撤销 / 否决会取消。新文件 platform/ask_detail.py，记录在 inspiration/asks/；回执页新增“追问细节 · 对话”区。
3. demo_reset.bat / demo_restore.bat + platform/demo_reset.py：关评价页 → 整个状态目录备份到 demo_backups/<时间>/ → 从模板新建（清卡片、评价、arXiv 已看/已推、本地发现历史、任务、追问、画像）→ 删平台记忆 [灵感画像]/[灵感配置]/[灵感状态] → 重开评价页。保留原始资料、config、设置、arXiv 检索式、rubric、收货员游标（旧会话不会被重收）。
4. B 类（knowledge）每次运行最多 5 张：state.quota.knowledge.per_run=5（旧 state 没有这个字段也按 5）；A 类机制不变。

## 1.5.2-test（2026-10-31）

修 Q4 反馈 + 4 项需求。

### Q4 修复
- **结果块识别**：不再把事件流里回显的提示词模板当结果（之前回执摘要里出现“≤120 字”等指令原文的原因）；只接受 job_id 一致、能解析的最后一个结果块（容忍代码围栏、尾逗号）；拿不到时读 result.json（会话文件 → 沙箱文件）。
- **自动补问 / 重试**（`actions.max_attempts`，默认 2）：没有结果块或停在 asked 时，先在同一会话补问“立刻输出结果块”；平台拒绝补问就带提醒重跑一次。失败原因写清楚是 asked 还是没有结果块，以及重试了几次。
- **提示词**：禁止 ask（后台没人能回答）、禁止 complete；无论成功失败最后都必须输出结果块；“只写方案”阶段允许写 result.json。事件流把 asked 视为结束，不再空等到超时。
- **Z 撤销**：按 6 后先等 `actions.cancel_grace_s`（默认 5 秒）再开跑；正在跑的任务（方案或执行阶段）也能撤销：立刻标为已取消，结果回来后丢弃、不发通知；尽量请平台停止运行（停止接口未验证）。
- **回执时间**：区分“本次建任务 → 开始（N 秒后）”和“首次按立刻动手”（间隔超过 10 分钟时注明“当时动手单待补 / 由 --sweep 补建”）。
- prepare 找状态时增加 /workspace/state.json。

### 需求
1. 回执页不再出现本地绝对路径：“完整回执：receipt.md · 打开所在文件夹”改成两个链接，由本机评价服务打开文件或文件夹（新接口 `/api/action/reveal`）；回执接口返回的内容统一做路径脱敏。
2. 立刻动手的完成通知：Windows 通知按钮改为“查看回执”，点开直接进入回执页并高亮对应任务（URL 带 `act=<job_id>`，打开后自动去掉）；页内提示也可以点击跳转。
3. “跑今天的灵感”全部改为“主动探索灵感”（按钮、技能、系统提示词、文档）；智能体简介改为：你说“主动探索灵感”，它自己找值得做的事并整理成卡片申请；说“建立画像”，它能记得你。
4. 页面和通知里不再出现“收货员”（改为“自动同步”）。

### 测试
- run_action 自测新增：模板拒收、补问成功、asked 两次后失败、运行中撤销后丢弃结果、时间线。
- 浏览器测试新增 U9b（运行中撤销）、U10（回执没有路径、链接能用）、U11（act= 跳转并高亮）、U12（文案）；共 22 项。

## 1.5.1-test（2026-10-31）

针对 1.5.0 测试报告里的问题，外加历史回放详情页。

### 评价页
- **历史详情**：“历史”列表里每一条都能点开，显示和评价时一样的卡片：空格翻面、←/→ 翻页、E 改写（Ctrl+Enter 保存）、Esc 返回列表；没有分类按钮。已分类的卡片改写后**分类保持不变**（`record_verdict` 不再把状态改成 edited），列表显示“采纳 · 已改写”。
- **K1 修复：按 6 = 建任务**。先生成**动手单**（场景 / 参数〔数值 + 单位 + 来源 library·observed·user〕/ 方法 / 产出 / 对照 / 判据 / 交付），缺“你提供”的参数就弹窗让你填；`actions.start_now_mode` = auto | confirm | off。
- **回执面板**（右上角“回执”+ 待处理数字）：状态、会话链接、结论、动手单实测与判据、编排器检查、动手单内容；T2 批准、结果可用 / 重跑 / 作废；任务结束时弹提示；“待补动手单”列表。
- 标签：“本地观察 · 检测器”、“补位”、“有动手单”。

### 立刻动手（run_action）
- 新模块 `platform/action_brief.py`；任务提示词带【动手单】，结果块多 `brief_result`；回执多“交付物都在”“判据给出了是 / 否”两项检查；`brief.md` 落在任务目录。
- 观察型卡片会把证据所在的本地文件（只在 observe 根目录内、≤ 2 MB、脱敏）附给任务（`actions.attach_local_files`）。
- CLI：`--brief <card>` 查看动手单，`--card-id … --brief-file brief.json` 用改好的动手单建任务。

### A 类每轮可见
- codescan：每个文件最多报 3 个长函数（原来只报 1 个）；新增 `code.smell`（吞错 / 写死绝对路径 / 参数 ≥ 7），perf 新增“循环里读文件”“for-range 逐元素累加”。
- triage `a_floor`：A 类被 `max_total` 挤掉时留一个位置；没有达标的 A 类时补位（标 backfill）。
- validate_cards：A 类默认不设上限、不计入每日总量；入围有 A 类、通过的卡片却没有 A 类时打印 `WARNING A_FLOOR`；insp-card 规则写明“每轮至少 1 张 A 类”。
- 负面清单的判断先于“已推送”：被否决的观察型候选显示 `negative:id`，不再显示 `already_pushed`。

### 卡片可选字段 action_brief
- insp-card 可以直接写动手单；validate_cards 校验参数来源与数值出处（library / observed 的数值必须在证据或 numbers_provenance 里），不合格就移除这个字段并写 `_brief_dropped`，**卡片照常通过**。

### 1.5.0 报告其余问题
- P0 画像自测在有会话令牌的机器上失败 → 首次写入也留历史，自测不依赖本机令牌。
- 覆盖升级留下旧文件 → `selfcheck.py stale [--clean]`。
- `observe init` 不写 config → 自动写入 observe 段（先备份；`--no-write-config` 关闭）。
- 读了会话标题却一个都没保留 → 按原因分别统计排除数，一个都没认出来时打印字段名。

### 升级
- `platform/migrate_state.py --config config.json [--apply]`：只改还停在旧默认值的项，改前备份。
- 测试：新增 `tests/ui_e2e_test.py`（真浏览器 U0–U9）、observe O16、triage / validate_cards / run_action 自测新增用例；均已接进 `selfcheck.py g1`。

## 1.5.0-test（2026-10-30）

新增**本地观察通道**：灵感不再只来自 arXiv，也来自用户自己的代码、日志、数据、笔记、论文库和使用记录。对应设计文档 §6 L0 的后三类候选（任务日志里的“以后可以试试”、计算结果里的异常、工作区文件变化）和 §8 的 A 类优化建议。

### 为什么要做
- **发生了什么**：1.4 的候选只有 arXiv 新论文，A 类（优化建议）几乎产不出来——灵感官看不到用户的代码。
- **为什么**：沙箱默认看不到用户电脑上的文件（报告实验 B：`_refs` 是空的），定时任务也读不到工作区。
- **结果**：设计文档里“日常价值来源、最好的训练数据”的 A 类一直空着，反馈数据量上不去。

### 做了什么
- **`platform/observe.py`**（子命令 `init` / `scan` / `list` / `log` / `papers` / `show`）+ **`platform/observers/`**（library、codescan、anomaly、notes、papers、usage、observed、common）。纯标准库；读 PDF 标题时有 pypdf 更好。
- 发现写入 `inspiration/observed.jsonl`；`library/summary.md` 给人看。修好的问题下次扫描自动移出。
- **收货员**：`config.observe` 存在时，触发运行前自动扫描并随状态上传；平台 403 / 没有会话令牌时只跳过读会话标题。
- **triage / prepare**：新增 `observed_shortlist()`（与 `observe list` 共用）、`state.json → observed` 配额（A 3 / B 2 / C 2 / D 2，每次 ≤ 6，严重度 ≥ 2.0，45 天过期）；prepare 打印 `OBSERVED n`，支持 `--observed-only`，每条候选写成 `obs_<id>.md`。
- **insp-card / insp-nightly SKILL**：新增「观察型候选」规则（quote 从证据原文逐字复制，数字来源 `observed`，不写链接）。
- **validate_cards**：obs_ 候选做逐字引文校验，补 `source.evidence_ref`、`read_mode=observed`。
- **评价页 / 每日摘要**：观察型卡片标“本地观察 · 文件位置”。
- **隐私**：`library/`、`observed.jsonl` 写进 `.gitignore`；`build_release` / selfcheck g5 发现包内有 `library/` 会报错。
- **测试**：新增 `tests/observe_test.py`（O1–O15），已接进 `selfcheck.py g1`。

### 测试中修掉的问题
- 日志里 `energy = …` 这种键值序列认不出；C / MATLAB 函数识别不出、嵌套层数算错；Fortran 圈复杂度把 `end do` 也算进去。
- 同一个崩溃在多份日志里被拆成多条 → 按最后的异常行归并。
- 长函数严重度封顶后大量并列 → 改为按超标倍数连续打分。
- 用户在 config 里写 `"exclude_globs": []` 会覆盖内置排除（`*secret*`、`*.key`、`node_modules`…）→ 改为用户规则**追加**到内置规则（O15）。
- 笔记“反常 / 想试”标记误命中 Markdown 表格、代码块和标识符里的英文词 → 跳过表格和代码块，英文关键词整词匹配；修了关键词表里一处漏逗号（两个词被拼成一个）。

### 已知限制
- 把扫描目录指向大量说明文档时，规则句子仍可能被误认为笔记。只放你自己的笔记。
- 读会话标题依赖会话令牌；Windows 上 PowerShell 历史默认不读。
- 观察型卡片的 `minimal_check` 由模型写，代码修改仍须你批准后走“立刻动手”。

## 1.4.5-weakbeta（2026-10-29）

修 feedback6 的“任务已中断”问题，并恢复学习闭环彩排。

### 组员包调整（2026-10-29 晚，功能代码不变）
- `scripts/validate_package.py` 和 `scripts/release_gate.py` 合并成 **`scripts/selfcheck.py`**，放进组员包：`package`（原 validate_package）、`g1` / `g3` / `g4` / `g5`（原 release_gate）用法不变，只是换了文件名。
- 新增 `selfcheck.py skills --config config.json`：把收货员收回来的每次运行都按会话列出 agent run，查平台 `GET /api/agent-run/{id}/skills`，结果追加到 `inspiration/audit.jsonl`（需要会话令牌）。以前只有 `g3` 能查最近一次（已知问题 K4）。
- G1 多了一项“平台字段补查自测”（共 11 项）。
- 组员改了读写评价的代码，自己跑 `selfcheck.py g4`，把 G4 证据附在提交说明里。
- 文档里的命令全部换成 selfcheck.py；`build_release.py` 改为调用 `selfcheck.py g5`；`collect_feedback.py` 的版本指纹改用 selfcheck.py。

### 卡片早已入库，网页却显示“任务已中断”
- **发生了什么**：一次运行在输出结果块之后又调用了 `complete` 交付工具，并把一句说明文字（“结果块已在上一行回答中原样交付”）放进了 `attachments`。平台把它当成一个缺失的文件，要求补交；重试 3 次后以 `DELIVERY_VALIDATION_LIMIT` 强制停机。
- **结果**：卡片 7 分半之前就已经入库和通知了，但前端显示“任务已中断”，用户点“从断点继续”又白跑了一轮。
- **实验**：不调用 `complete` 的运行能正常 completed，产物照常收货。所以平台并不强制调用它。
- **修法**：
  1. 系统提示词新增第 12 条，insp-nightly / insp-card / insp-profile / insp-feedback 的交付段，以及运行协议和熔断文档都写明：输出结果块后直接结束，**不调用 `complete`、不放附件**；万一被要求补交，`attachments` 里只能放真实文件路径，补一次仍失败就结束。
  2. `insp_run.py finalize` 和 `build_profile.py` 在结果块之后紧接着打印一行收尾提示（模型做下一步决定时最先看到的就是这一行）。
  3. 评价页按钮触发的指令（receiver）和本地编排器的指令（run_local）也加了同一句话。
  4. 收货员入库后在 `receiver_status.json` 的 `last_result` 里记下时间；评价页状态栏显示“最近结果 HH:MM 已入库（N 张）· 网页若显示‘任务已中断’，不用点‘从断点继续’”（12 小时内）。
  5. 用户手册 F3 加了 FAQ；开发文档登记为 K12（软约束，长期方案见那里）。

### 学习闭环彩排 L4b 失败（测试问题，不是功能问题）
- **原因**：1.4.3 把全文模式每次最多读的篇数改成 4，而彩排的测试档每次运行有 5 个名额，候选全部入选，权重之间没有竞争，“用旧权重重放”永远选出同样的卡。
- **修法**：彩排里把 `fulltext.max_candidates` 放宽到 8（只影响测试），恢复名额竞争；L4b 恢复为 2/4 次运行结果不同。

### 发布扫描
- 清理了一处第三方署名；`release_gate.py g5` 和 `validate_package.py` 新增内置禁用词检查（词表以 base64 存放，词本身不出现在包里），始终生效。

### 测试
- e2e 新增 T21a（收尾提示紧跟结果块）；收货员测试新增 R5b（`last_result.at`）；`validate_package.py` 检查 9 个交付相关文件都有收尾纪律。

## 1.4.4-weakbeta（2026-10-29）

基于 1.4.4-test 发布给组员，用于二次开发和评测。功能代码只改了 1 处。
- 修复（G4）：weekly_review 的本周采纳率和三个月回访把已撤销的正向评价也算了进去（模拟数据上周报写 5，正确值是 2），现在按评价栈的栈顶计算。
- 新增：scripts/release_gate.py（G1/G3/G4/G5 检查和证据）、scripts/build_release.py（白名单打包、MANIFEST、G5 扫描）、.gitignore。
- 文档：README、用户手册（useguide.md 重写，包含网页操作清单 F1–F9）、docs/开发文档.md（含 4 张示意图和已知问题 K1–K11）、CONTRIBUTING.md、docs/weak-beta发布门槛与证据.md；更正评价读写约定里“已评价”的定义和“立刻动手”的触发方式。

## 1.4.4-test（2026-10-29）

修 feedback6 H1：v1.4.3 在真实平台 `setup --replace` 失败。

### 平台新增的技能导入校验：missing_file
- **原因**：v1.4.3 的 insp-nightly/SKILL.md 和 references/运行协议.md 写了 `/workspace/skills/insp-card/scripts/find_quotes.py`。平台导入 zip 时会检查“说明里引用的相对路径文件必须在本包内”，它把完整路径里的 `scripts/find_quotes.py` 当成 insp-nightly 自己的文件去找，找不到就以 HTTP 400 `missing_file` 拒收整个包。
- **修法（按你的第二条建议：脚本放进 nightly 包）**：`find_quotes.py` 和 triage.py 等脚本一样同步一份到 `insp-nightly/scripts/vendor/`（`sync_vendor.py` 管理，原件仍在 insp-card，副本与原件不一致时校验失败）。insp-nightly 不再提及其它技能的文件路径。
- **更短的命令**：prepare 在全文模式下把 find_quotes 复制成 `/workspace/fq`（无扩展名，默认目录指向本次运行的输出目录），并打印 `FIND_QUOTES python3 /workspace/fq <ID>`。技能、系统提示词、按钮指令统一用这条命令，双 ID 也不超过 60 字符；原来的完整路径带 2 个 ID 会超过 80 列，同样可能被平台切碎。
- **本地预检**：新增 `platform/skill_refs.py`，按同样的规则检查 7 个技能（`.md` 里出现的 `scripts/` `references/` `assets/` 文件都要在本包内）。`validate_package.py` 和 `setup_platform.py` 安装前都会先跑，不通过就停止，不调用接口。

### setup 文本回退遇到 409 就崩
- zip 导入失败时打印平台给出的 `code · guidance`；遇到 `missing_file` 会提示运行 `skill_refs.py`。
- 文本回退返回 409（同名技能已存在）时不再抛 traceback：**保留平台上的旧版本**（platform_ids.json 里标记 `stale`），其余技能和灵感官照常更新，最后以退出码 1 报“N 个技能没有更新成功”。`--check` 会把 `stale` 列为 FAIL；修好后重跑 `--replace` 会清除这个标记。

### 测试
- e2e 新增 T19g（`/workspace/fq` 短路径）、T20a/b（文件引用检查，并复现 feedback6）；setup 测试新增 S1b、S6、S6b（模拟平台 400 missing_file，以及文本回退时的 409）。

## 1.4.3-test（2026-10-29）

修 feedback5（v1.4.2 真机：Beta 放行条件全部满足，G8 全文模式未通过）。

### G8：全文模式被平台“工具连续失败 5 次”停机
- **原因链**：全文模式一次读 8 篇全文 + 23 篇相关文献 → agent 用 `cd … && grep … rel_2512.15790.md && grep …` 这类长命令找引文 → 平台执行器把长命令按约 80 列切碎（`rel_2512\n.15790.md`、结尾追加的 rc 重定向也被切断）→ grep 其实成功了，但退出码被判失败 → 连续 5 次 → `TOOL_FAILURE_LIMIT`，48 分钟后停机，cards.json 从未生成。
- **新增 `insp-card/scripts/find_quotes.py`**：把“在 N 个文件里找引文”变成一条短命令
  `python3 /workspace/skills/insp-card/scripts/find_quotes.py 2407.04363 2512.15790`。
  对每个入围论文列出 ft_<ID>.md 里命中关键词的**逐字原句**（带 §章节、标出“正文·摘要里没有”），并顺带列出其相关文献 rel_*.md 的候选句；`--terms` 换关键词、`--sections` 列章节、`--section "<标题>"` 读一节（代替 cat/sed）。输出与校验器同一套规范化，复制即通过；找不到文件打印 `NOT_FOUND` 但退出码 0（不计入连续失败）。附件模式会自动去 `/workspace/_refs/` 找。
- **命令纪律**（insp-card / insp-nightly / 运行协议 / 灵感官 system prompt / 本地兜底指令）：一条命令只做一件事，**禁止用 `&&`、`;`、管道串多个 grep/cat/sed，一次一个文件**；不 cat 整个原文；同一命令失败 1 次就换做法，连续失败 2 次就用已有引文出卡或按 NO_CANDIDATES 交付结果块。
- **全文模式默认只读 4 篇**：`fulltext.max_candidates`（默认 4）；`insp_run.py prepare --fulltext-max N`；按分数取前 N 篇，其余不进 `new_ids`（不标记为已见），下次再参与排序；`counts.fulltext_deferred` 记录留到下次的篇数。评价页按钮、`[灵感设置]` 记忆会把 `--fulltext-max N` 一并带上。

### feedback5 §3 的其它问题
- **3.1 / 3.6 同一运行被收两次、记忆写两遍**：收货与画像入库加跨进程锁（`inbox/locks/`，同一会话同一时刻只有一个收货进程），另一个进程直接返回 `ALREADY`；`[灵感状态]` / `[灵感设置]` 改为幂等 upsert（保留 1 条、删掉其余重复，旧版本留下的重复也会被清掉）；画像记忆同步在同一把“写记忆锁”里进行。
- **3.2** `NO_CANDIDATES` 时 `runs.jsonl` 的 `cards` / `dedup_dropped` 写 `[]`，不再是 `null`。
- **3.3** 评价页“主动探索灵感 / 检查新结果”改为事件委托绑定，页面重绘后不刷新也能再点。
- **3.4** `collect_feedback.py --test` 改为可选（默认 `manual`）。
- **3.5** 按钮触发的运行在 `runs/trigger-*/prompt.txt` 保存当时发给灵感官的指令。

### 测试
- 离线 e2e 新增 T19a–f（find_quotes、篇数上限、触发指令、技能纪律）；收货员测试新增 R9–R12（3 个进程并发收同一运行只记 1 行、NO_CANDIDATES 字段、记忆幂等、prompt.txt）。

## 1.4.2-test（2026-10-29）

修 feedback4（Windows 真机 + 真实平台，v1.4.1）里阻塞 G4 的问题，以及 G0 / G1 的两个工具问题。

### 阻塞修复：收货员读不到会话文件（BUG-3，G4）
- **原因**：真实平台的 API Key 只能调用“SDK catalogs / agent / run / sandbox”接口；收货员用 API Key 读 `/api/thread/{id}/files/content` → 403，而且 403 没被捕获，进程在走到“回复兜底”之前就崩了，8 张卡留在沙箱里没入库。
- **凭据顺序**：收货员读会话数据时改为**会话令牌优先、API Key 兜底**（`sciencepal_client.MultiAuth`：401/403 自动换下一个凭据；会话令牌过期也能退回 Key）。会话列表、会话 → 运行 / 沙箱查询、状态回写都走这条。
- **403 不再崩溃**：`ThreadSource.read()` 遇 401/403 记录原因（`runs.jsonl` 的 `file_denied`）并返回空，继续走兜底：① 若已知 sandbox_id，用 API Key 可用的 `/api/sandboxes/{id}/files/content`；② 运行结束后从 `GET /api/agent-run/{id}` 和**回放事件流**里取 `INSP_BUNDLE_JSON` 结果块（API Key 可用）。多个结果块时只认能解析成整包的，且优先校验和一致的那份（finalize 自己的输出），技能正文里引用的标记、模型抄短的副本都会被跳过。
- **整包自包含**：`insp_run.py`（runner `insp-run/1.4.2`）在整包里写入 `triage_min` + `triage_min_sha256`，只拿到结果块也能做本地复核（计数自洽、测试 ID 守卫、去重）；校验和不符即拒收。
- **v1.4.1 旧整包**：只有 API Key 时会得到 `VALIDATION_FAILED`（`retryable=true`，提示放会话令牌），**不标记为已收**；放好会话令牌后同一条 `--thread` 命令即可收进来——feedback4 里那 8 张卡可以这样补收。
- `receiver.py --thread` 新增 `--run-id`、`--sandbox-id`；按钮触发时把 `sandbox_id` 一并记进 `inbox/triggered.jsonl`，之后 `--thread` / `--push-state` 自动带上。
- 收货结果里的 `cards` 改为 arXiv 核验**之后**真正入库的张数（另报 `arxiv_rejected`）。
- 一个会话出错不再中断整轮轮询；运行中的会话不会每轮都去回放事件流。

### G1：定时任务检查用错凭据（BUG-2）
- `setup_platform.py --cleanup-schedules` / `--check` 查定时任务改用会话令牌；没有会话令牌时明确提示“需要会话令牌”，不再报模糊的“无法列出”。`status=deleted` 的旧任务不算残留。
- `--check` 新增一项：会话令牌能否读会话列表（决定收货员能否自动发现网页会话）。

### G0：T15 在 Windows 上失败（BUG-1）
- `setup_platform.py` 打印路径统一用正斜杠；T15 同时按文件数判断，不再依赖分隔符。

### 防重复触发（feedback4 里 22 秒内触发了两次付费运行）
- 评价页按钮：运行中点击只提示、不再发请求；服务端 10 分钟内（`receiver.min_trigger_gap_s`）拒绝第二次触发，`receiver.py --run --force` 可强制。

### 其它
- `run_local.py` / `run_action.py` 读会话文件同样会话令牌优先；`run_local.py` 现在也读取 `~/.sciencepal/session_token`（以前只读环境变量）。
- 评价页状态行：缺会话令牌时显示“仅 API Key”，pinned_only 时说明原因。
- 测试配置一律指向不存在的令牌文件，测试不会读到你机器上的真实令牌。

### 测试
- 新增 `tests/mock_receiver_test.py` R1–R8：mock 平台按真实白名单返回 403（API Key 读会话文件 / 会话列表 / 定时任务 / 记忆一律 403），覆盖事件流兜底、feedback4 现场复现与补收、状态回写 + 记忆、结果块被改动、轮询、防重复触发、setup 检查、不再出现 traceback。

## 1.4.1-test（2026-10-29）

### 新功能：论文读取模式开关 `arxiv_fetch_mode`
- `config.json` 新增一个字段 `"arxiv_fetch_mode"`：`"abstract"`（**默认**）只读 arXiv 标题 + 摘要出卡；`"fulltext"` 出卡前通读候选论文全文 + 相关文献全文（约慢 4–5 倍）。字段缺失、为空或写错一律按 `abstract`；旧字段 `fulltext.required` 废弃、不再生效。
- **行为变化**：v1.3–v1.4.0 默认读全文；升级后不改配置就回到摘要模式。要继续读全文，把字段设为 `"fulltext"`。
- 三条路线都跟随这个字段：
  - 沙箱 `insp_run.py prepare --fetch-mode abstract|fulltext`（不传 = abstract）；`triage.json` 和整包都带 `fetch_mode`；摘要模式不抓原文，finalize 不要求正文引文。
  - 评价页按钮：每次触发都重新读 config.json，把 `--fetch-mode` 写进指令（改配置不用重启评价页）。
  - 本地 `run_local.py`：摘要模式不抓、不上传原文，指令换成摘要版纪律。
  - 网页里直接 @灵感官：收货员把模式同步成记忆 `[灵感设置]`（值变化时才写）；`receiver.py --sync-settings` 立即同步；用户本次说“通读全文 / 只看摘要”时以用户为准。
- 收货员按**整包自己的模式**复核：摘要整包不会因为 config 已改成 fulltext 被拒；不一致时在 `runs.jsonl` 记 `fetch_mode_note`。全文模式的严格校验不变（只有摘要引文的卡片仍被拒）。

### 卡片链接
- 校验通过的卡片由脚本写入 `source.url`（arXiv 摘要页）、`source.pdf_url`、`source.read_mode`；全文模式另写 `source.fulltext_url`（实际读到的 HTML/PDF）；`related_evidence[].url` 同样补上。模型写的链接一律覆盖。
- 评价页：标签行显示“只读摘要 / 通读全文”，背面显示“摘要页 ↗”“全文 ↗”；运行中状态显示本次模式。今日摘要 `digest/<日期>.md` 增加模式行和链接行。

### 测试
- 新增 T1b、T8d、T9b、T17a–d、T18a–e（默认模式、全文严格性、整包模式优先、模式解析、指令跟随配置、记忆同步）和 M9（不写字段 → 不抓不传原文）；M1–M8 与学习闭环模拟改为显式使用 fulltext，以保留全文路线的覆盖。

## 1.4.0-test（2026-10-29）

### 核心变化：一句话跑完，取消一切定时
- 在网页里对灵感官说“主动探索灵感”即可跑完整条链路；沙箱里的 `insp-nightly/scripts/insp_run.py`（prepare / finalize）自己抓 arXiv、去重、抓原文、校验，最后输出 `INSP_BUNDLE_JSON` 整包。
- 新增本地收货员 `platform/receiver.py`，已内置进评价页服务：只读轮询最近的会话，本地重新校验、去重后入库，弹通知，再把状态推回沙箱和记忆。
- `run_nightly.py` 改名为 `run_local.py`，只作手动兜底；`FETCH_BLOCKED` 时由收货员自动拉起。
- 删除 `platform/windows/register_tasks.ps1`；新增 `remove_legacy_tasks.ps1`（移除旧计划任务）和 `start_review_server.ps1`（启动评价页，`-Autostart` 表示登录时启动，不会定时运行）。
- `setup_platform.py --cleanup-schedules [--yes]`：列出或删除平台上的灵感定时任务。

### 新技能 insp-profile
- 网页访谈 7 问 → 预览 → 用户确认 → `build_profile.py --confirmed` → `INSP_PROFILE_JSON`；收货员调用 `profile_tool.apply_profile` 落盘 AGENT.md / interests.md / claims.md，同步检索式和记忆投影。没有举例的“擅长”自动降级为“了解”；未经确认的画像一律拒收。

### 安装脚本
- 共 7 个技能；导入用会话令牌，运行用 API Key；安装后自动检查四项（技能已绑定、关键技能以 zip 导入、Key 与灵感官同账号、无残留定时任务）；`platform_ids.json` 路径改为相对 config 所在目录。

### 评价页
- 顶部新增“主动探索灵感”和“检查新结果”按钮，以及收货员状态栏（上次检查时间、运行中、错误原因）；新卡片到达时显示“新到 N 张卡片 · 点击载入”，标签页标题显示计数；这一轮已评完时自动载入。

### 配额
- knowledge 每周上限和每日总量改为 999（等于不限）；hypothesis / process 仍保留低频配额。

### 修复
- BUG-1：离线端到端测试改为跨平台的 `tests/run_offline_e2e.py`。
- BUG-3：收货、触发和安装时都核对“Key 与灵感官同账号”。
- BUG-4：`/api/ping` 返回状态目录的绝对路径，避免两个包共用一个评价服务。
- BUG-5：后台 worker 透传 `--fake-agent-output`（或环境变量 `INSP_FAKE_AGENT_OUTPUT`），并把 argv 写进 worker.log。
- BUG-6：状态目录里没有 `.test_state` 标记时，含 `9999.*` 测试 ID 的结果一律拒收。
- BUG-7：weights 配置损坏时，每个入口都会报错退出。
- `finalize --accept-partial` 现在只交出通过校验的卡片。
- validate_cards 的配额自测不再依赖模板里的配额数值。

### 测试
- `run_offline_e2e.py` T1–T16（新增 T13 网页画像、T14 BUG-5、T15 技能 zip、T16 评价页按钮与收货员接口）；新增 `tests/mock_setup_test.py` S1–S5；`validate_package.py` 新增检查：已删除文件不得再出现、vendor 副本一致、ps1 带 BOM、不得再引用 run_nightly。
