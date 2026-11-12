# 补充手册：本地资料库（library）与"接入我已有的仓库"

> 适用版本：`1.5.9-test` · 读者：第一次配置本地观察的人
> 相关文档：[INSTALL.md](INSTALL.md)（安装）· [useguide.md](useguide.md)（日常使用）· [docs/开发文档.md](docs/开发文档.md)（机制）

---

## 0. 先分清两个「library」

| 路径 | 是什么 | 谁创建 | 进不进 Git |
|---|---|---|---|
| `library/`（包根目录） | **你的资料库**：代码 / 笔记 / 论文 / 数据 / 日志，是扫描的**输入** | `observe.py init` 或你手动建 | **不进**（`.gitignore` 已忽略） |
| `inspiration/library/` | **观察结果**：`manifest.json`、`codescan.json`、`summary.md`、`changes.jsonl`、`observed_history.jsonl` | 每次 `scan` 自动写 | 不进（`inspiration/` 整体忽略） |

**下面说的「library」都指前者**——你的资料库。

---

## 1. library 是怎么被创建出来的

它**不在发布包里**，因为里面是你的私人数据。创建方式有两种，效果一样：

### 方式 A：一条命令（推荐）

```powershell
copy platform\config.example.json config.json          # 如果还没有 config.json
python platform\observe.py --config config.json init
```

它会：

1. 按 `config.json → observe.roots` 逐个检查目录；
2. **只对包目录内**（`./library/code` 这类相对路径）的缺失目录执行 `mkdir -p`；
3. 写一个 `library\README.txt`（说明每个子目录放什么）；
4. 如果 `config.json` 里还没有 `observe` 段，把示例里的整段写进去（原文件先备份成 `config.json.bak-<时间戳>`）；
5. 打印每个根的 `新建 / 存在 / 缺失`：

```text
  新建 code     code    C:\...\inspiration-skills\library\code
  新建 notes    notes   C:\...\inspiration-skills\library\notes
  新建 papers   papers  C:\...\inspiration-skills\library\papers
  新建 data     data    C:\...\inspiration-skills\library\data
  新建 logs     logs    C:\...\inspiration-skills\library\logs
```

### 方式 B：手动建

```powershell
mkdir library\code, library\notes, library\papers, library\data, library\logs
```

### 创建后目录长这样

```text
inspiration-skills\
├── config.json
├── library\
│   ├── README.txt
│   ├── code\       ← 你的代码（含 git 仓库时，会读 git log 算改动热点）
│   ├── notes\      ← .md .txt .org .tex 笔记
│   ├── papers\     ← .pdf .bib .ris 与阅读笔记
│   ├── data\       ← .csv .tsv .dat（只看最近 N 天改过、5 MB 以内的）
│   └── logs\       ← .log .out .err .txt
└── inspiration\    ← 运行时状态（不是资料库）
```

> 空目录完全正常：扫描器会跳过没有文件的根，不会报错。

---

## 2. 一个扫描周期发生了什么

```text
observe.py scan
  ├─ 遍历 observe.roots（每个根有 label / kind / 是否存在）
  ├─ library.py    → 文件清单 + 变化（新增/修改/删除）→ inspiration/library/{manifest.json,changes.jsonl,summary.md}
  ├─ codescan.py   → 代码问题（长函数 / 参数多 / 嵌套深 / 静默吞错 / 重复 / 改动热点）→ codescan.json
  ├─ anomaly.py    → 日志与数据异常（NaN、异常退出、指标突变）
  ├─ notes/papers/usage → 笔记、论文、使用习惯
  └─ observed.py   → 合并成 inspiration/observed.jsonl（**只写发现，不写全文**）
```

运行时（`auto_scan_before_run: true`）会在每次「主动探索灵感」前自动扫一次；
也可以手动：

```powershell
python platform\observe.py --config config.json scan
python platform\observe.py --config config.json list        # 预览入围的观察
python platform\observe.py --config config.json scan --all  # 看未入围的原因
python platform\observe.py --config config.json show <id>   # 看单条详情
```

`scan` 成功时会打印：

```text
observed.jsonl 12 条 → 入围 6 条（规则：state.json → observed，与沙箱 prepare 相同）
  ✔ [optimization] 严重度 2.9 · power_dep.py 的循环里反复读文件  (obs_xxx · library://code/power_dep.py#L520)
```

---

## 3. 接入你已有的仓库（重点）

**不需要把仓库复制进 `library/`。** 直接把 `observe.roots` 指向它。

### 3.1 改哪里

`config.json` 里的 `observe.roots` 数组，每一项三个字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `path` | ✅ | 绝对路径（`D:/research/qmc`）或相对 `config.json` 的路径（`./library/code`） |
| `kind` | 建议 | `code` / `notes` / `papers` / `data` / `logs` / `mixed`，只影响 `.txt`、`.pdf` 这类歧义扩展名归到哪一类 |
| `label` | 建议 | 证据里显示成 `library://<label>/<相对路径>`；不写则用目录名。**同名会自动加数字**（`qmc2`） |

> `path` 支持 `%USERPROFILE%`、`~` 环境变量/家目录展开；相对路径按 **`config.json` 所在目录** 解析。
> 不存在的路径不会报错，只会在 `init` 里显示「缺失」、扫描时跳过。

### 3.2 示例：把一个 git 仓库接进来

```json
{
  "observe": {
    "enabled": true,
    "auto_scan_before_run": true,
    "roots": [
      { "path": "D:/research/qmc",        "kind": "code",  "label": "qmc" },
      { "path": "D:/research/paper-notes","kind": "notes", "label": "notes" },
      { "path": "./library/papers",       "kind": "papers" },
      { "path": "./library/data",         "kind": "data" },
      { "path": "./library/logs",         "kind": "logs" }
    ]
  }
}
```

要点：

- 原来的 `./library/code` 那一项**可以整条删掉**，换成你的仓库；
- 想同时观察多个仓库，就写多项（`label` 不同）；
- **用正斜杠 `/`**（Windows 也认），避免 JSON 里 `\` 转义踩坑；
- 路径含空格可以正常写（`"D:/My Research/qmc"`）。

### 3.3 改完怎么验证

```powershell
python platform\observe.py --config config.json init     # 看 存在/缺失
python platform\observe.py --config config.json scan     # 真的扫一遍
```

`init` 输出里你的仓库应显示 **`存在`**；`scan` 后看 `inspiration/observed.jsonl` 是否有新条目，
或 `python platform\observe.py --config config.json list`。

### 3.4 为什么证据里看不到我的绝对路径

`observed.jsonl` 里的 `evidence_ref` 是 `library://<label>/<相对路径>#L<行号>`，
例如 `library://qmc/src/solver.py#L145`。绝对路径只留在本机 `inspiration/library/*.json`。

### 3.5 不想改 JSON 的替代做法

| 做法 | 命令 | 注意 |
|---|---|---|
| 在 `library/code` 下建**目录联接**（junction） | `mklink /J library\code\qmc D:\research\qmc` | 扫描器会跟进去；但删除 junction 时别用 `rmdir /s` 误删源目录 |
| 直接把仓库复制进 `library/code` | `xcopy /E /I D:\research\qmc library\code\qmc` | 占空间、容易不同步，不推荐 |
| 软链接（symlink） | `mklink /D ...` | Windows 需要开发者模式或管理员权限 |

**推荐还是改 `observe.roots`**：零拷贝、路径不进证据、可随时换。

---

## 4. 常用调参

| 配置项 | 默认 | 什么时候改 |
|---|---|---|
| `exclude_dirs` | 已内置 `.git`、`node_modules`、`__pycache__`、`venv`、`build`、`dist` 等 | 你的仓库有大目录（`data/raw`、`third_party`）时加上 |
| `exclude_globs` | `*.min.js`、`*.lock`、`*secret*`、`*.key`、`*.pem`、`.env` 等 | 想排除 `docs/**`、`*.bak`、`*.ipynb` 时加 |
| `max_files` | 20000 | 超大仓库扫太慢时降到 2000–5000 |
| `lookback_days` | 30 | 只看最近两周就改 14 |
| `max_items` | 80 | 观察太多、卡片被稀释时降到 30–40 |
| `time_budget_s` | 180 | 扫描超时；想更快降到 60 |
| `upload_code_snippets` | `true` | 改成 `false`：只上传指标（行数/复杂度/位置），不上传代码原文 |
| `snippet_lines` | 40 | 代码片段最多几行 |
| `code.long_function_lines` / `complexity` / `nesting` / `max_args` | 80 / 15 / 5 / 7 | 觉得太吵就调高阈值 |
| `code.churn_days` | 90 | git 改动热点的回看天数 |

---

## 5. 隐私边界（请确认后再开）

- **原始文件不出本机**；上传到沙箱的是：
  - `state.json` / `observed.jsonl` / 画像 / 口味规则（快照）；
  - 观察证据里的**脱敏片段**（`upload_code_snippets=true` 时，最多 `snippet_lines` 行）；
- **密钥、令牌、邮箱、个人目录会自动脱敏**；
- 想更保守：`"upload_code_snippets": false`，灵感官仍能提"这个函数太长/这里吞了异常"，
  但看不到具体代码行，提出的改法会更笼统；
- `exclude_globs` 再加一层：`["*secret*", "*token*", "*.env", "config*.json"]`。

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `init` 显示 `缺失` | 路径写错，或仓库不在那个位置 | 用绝对路径重试；确认盘符 |
| `scan` 后 `observed.jsonl` 是 0 条 | 根目录不存在 / 全被排除 / 阈值太高 / 最近没有改动 | 先 `scan --all` 看原因；再放宽阈值或 `lookback_days` |
| 扫描很久 | 仓库太大 | 调 `max_files`、`time_budget_s`，加 `exclude_dirs` |
| 卡片里没有代码行 | `upload_code_snippets=false` | 改回 `true`（或接受更笼统的建议） |
| 换了目录后旧观察还在 | `observed.jsonl` 是累积的 | 备份后删除 `inspiration/observed.jsonl`，重新 `scan` |
| `A 类候选：待用 0` | 观察型卡片配额用完了（已出卡/已否决） | 评价页看 `A 类候选` 统计；重置演示数据可清 |
| 不想观察任何东西 | — | `"observe": {"enabled": false}` 或删掉整段 |

---

## 7. 一页速查

```powershell
# 1) 建库（或改 config.json 的 observe.roots 指向已有仓库）
python platform\observe.py --config config.json init

# 2) 扫一次
python platform\observe.py --config config.json scan

# 3) 看结果
python platform\observe.py --config config.json list
python platform\observe.py --config config.json scan --all

# 4) 让它自动在每次「主动探索灵感」前扫
#    config.json → observe.auto_scan_before_run = true（默认）
```
