"""v1.5 本地观察者（observed 通道的生产者）。

只在你的电脑上运行（Windows / macOS / Linux，Python 3.9+ 标准库），读你放进资料库（library）的代码、笔记、论文、
数据和日志，把“发现”写成 inspiration/observed.jsonl。收货员触发运行时会把这个文件随状态一起送进沙箱，
insp_run.py prepare 按 candidate_type 分组、按 severity 排序取前 N，交给灵感官写成卡片（A 类优化卡的来源）。

模块：
  common.py     配置、资料库根目录、文件分类、脱敏、稳定编号
  library.py    资料库清单（manifest）+ 两次扫描之间的变化（changes.jsonl）
  codescan.py   代码静态扫描：函数长度 / 圈复杂度 / 嵌套、重复代码、churn 热点、逐元素循环
  anomaly.py    日志（报错 / NaN / 不收敛 / 数值离群）与数据文件（离群值、缺失值）
  notes.py      笔记与代码注释里的“这里有点怪”“以后可以试试”“没搞懂”
  papers.py     本地论文库 → 兴趣画像（高频主题、已有 arXiv 编号）
  usage.py      操作记录（oplog）+ 平台会话标题 + 产出统计 → “最近常做什么”
  observed.py   合并、去重、脱敏、截断，写 observed.jsonl 与 profile/observed.md 的自动段落
"""
