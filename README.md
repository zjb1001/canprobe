# CanProbe —— CAN 总线回放与信号分析工具

面向汽车电子问题调查的 CAN 报文回放 / 信号解析 / 功能状态分析工具。功能对标
CANoe 的 Graphics 窗口：加载 DBC 与报文日志，回放报文、显示信号曲线，并在此
基础上叠加一层**功能级证据分析**——根据你声明的"进入 / 退出"条件，直接给出
*什么时候进入、为什么进入、为什么退出、为什么**没有**进入* 的可追溯证据。

## 特性

- **DBC 加载解析**：基于 `cantools`，支持多路信号、缩放/偏移、单位、枚举（choices）。
- **报文日志回放**：支持 `.asc`(Vector) / `.csv` / `.json` / `.trc`(PEAK)，
  以及 `.blf`（`python-can`）/ `.mf4`（`asammdf`，Vector 总线日志实测）。
- **信号曲线**：每信号一行、独立坐标轴（CANoe Graphics 条带式布局），缩放/平移、
  光标取值、枚举信号阶梯显示。
- **关键事件面板（下边框）**：把"问题发生在哪一刻"集中管理起来 —— 功能规格算出的
  每次进入 / 退出 / 状态迁移都是一行（时间 + 功能 + 原因摘要），点一行即把
  Graphics 光标打到那个时刻（时刻在视窗外会自动平移 X 窗口）；回放时反向高亮
  当前所处的事件。图表上方还有一条事件 ribbon，是同一批事件在时间轴上的投影，
  与图表 X 轴对齐，点刻度同样跳转。支持按功能 chip / 事件类型 / 关键字筛选，
  `,` `.` 键在事件间前后跳。
- **功能状态泳道**：每个功能一条激活/未激活泳道，进入（▲绿）/退出（▼红）标记。
- **证据引擎**：进入/退出事件 + 被阻止的进入（"为什么没进入"），逐条列出满足 /
  未满足条件及实际信号值、差值。
- **两种功能描述方式**：声明式条件（`enter/exit/trigger`）**或直接嵌入 Python
  参考功能代码**（`code` 字段），支持计时/计数/滞回等复杂逻辑。
- **超大日志增量加载**：流式解析 + 紧凑 NumPy 帧存储 + 按信号惰性解码，可指定
  时间窗口 / 帧数上限加载，内存占用大幅降低。
- **右侧信号值面板**：随光标/回放实时列出每个已选信号的当前精确值（枚举显示
  名称），点击时间轴即可对齐查看。
- **手动添加信号**：顶部输入框键入信号名（自动补全）快速添加，无需在报文树中翻找。
- **DBC / 日志 / 规格 不配套告警**：三个文件各自独立替换，很容易换了 DBC 和规格却
  忘了换日志——此时坐标轴照画（轴信息来自 DBC），但每条曲线都是空的、关键事件 0 条。
  工具会明确区分「DBC 里没这个信号」和「DBC 里有但这份日志没录到」：顶栏黄条给出
  结论，信号树里没录到的报文整组置灰，功能按钮显示 `有数据/总数` + ⚠ 及原因，
  事件面板的空状态直接说明是哪一种情况。
- **在线编辑规格**：随时修改功能条件并重新分析。
- **零配置启动**：首次打开自动加载内置示例（定速巡航 + 过温保护场景）。
- **通信诊断**：总线负载、周期报文缺失、节点离线、总线静默（Tier 1，日志可证）；
  错误帧风暴、错误状态机 Bus-Off/Error-Passive（Tier 2，需错误帧记录）；波特率
  不匹配 / 终端布线（Tier 3，置信度排序的推断 + 测量建议）。附能力清单——明确
  "该日志格式能查什么"，避免「没查到」被误读成「没问题」。

## 快速开始

```bash
# 安装依赖（已装可跳过）
python3 -m pip install --user -r requirements.txt

# 启动（自动打开浏览器）
python3 run.py
# 或指定端口
python3 run.py --port 9000 --no-browser
```

首次启动会自动加载 `samples/` 下的示例（`cruise.dbc` + `cruise.csv` +
`function_specs/functions.yaml`）。也可以手动：顶部 **加载示例** / 拖拽 DBC、日志、规格文件到页面。

## 目录结构

```
canprobe/
├── run.py                 # 入口
├── canprobe/              # 后端 (FastAPI)
│   ├── main.py            #   API + 静态服务
│   ├── dbc_loader.py      #   DBC 加载与信号元数据
│   ├── log_parser.py      #   asc/csv/json/trc/blf/mf4 解析 + 流式 iter_frames
│   ├── blf_fast.py        #   BLF 向量化读取（直接出 NumPy 数组，不经逐帧对象）
│   ├── fast_decode.py     #   信号向量化抽位（与 cantools 对拍后才启用）
│   ├── decoder.py         #   帧 → 信号时序 (紧凑存储 + 惰性解码)
│   ├── analyzer.py        #   功能状态机 + 证据引擎（声明式）
│   ├── executor.py        #   Python 参考功能代码执行
│   └── store.py           #   内存项目状态
├── web/                   # 前端 (原生 JS + ECharts)
│   ├── index.html / app.js / style.css
│   └── vendor/echarts.min.js
├── samples/               # 示例数据 + 生成脚本
│   ├── generate_samples.py
│   ├── cruise.dbc / cruise.csv
│   └── function_specs/    #   功能规格 yaml
│       └── functions.yaml / functions_code.yaml / ...
└── tests/                 # pytest
```

## 功能分析规格（function_specs/functions.yaml）

条件表达式语法：

| 形式 | 含义 |
|---|---|
| `{signal: 名, op: 操作符, value: 阈值}` | 数值/枚举比较，`op ∈ >,<,>=,<=,==,!=` |
| `{rising: 信号}` / `{falling: 信号}` / `{changed: 信号}` | 边沿触发（0→1 / 1→0 / 任意变化） |
| `{all: [...]}` / `{any: [...]}` / `{not: {...}}` | 逻辑与 / 或 / 非 |

**一个 function 写一个完整功能，不是一个信号。** 把某个 bit 单独定义成 function
（`enter: {signal: X, op: "==", value: 1}`）看着简洁，排查时却没用：点按钮只加一条
曲线，事件栏里全是这个 bit 的翻转记录。正确的粒度是一条完整链路——驾驶员请求 →
总线传输 → ECU 判决 → 对外状态 → 上下文，用 `signals:` 段**按请求→反馈→上下文的
顺序**全列出来（该顺序即加入 Graphics 的顺序），状态机吐出功能层面的处境，配合
下方事件栏就是一条可读的事件历程。参考 `samples/function_specs/functions_ep35_switch.yaml`：
同一份日志，21 个单信号 function 产出 1250 条事件（其中 663 条是一个开关位的翻转），
重写成 4 个功能级 function 后是 17 条，条条是结论。

信号会以 ~100 ms 反复翻转的场景（总线上有多个发送者、抖动、竞争）尤其要注意：
请求值先去抖再参与判决，并且把"翻转"本身作为**一条**结论输出，而不是让它刷成几十条
进入/退出。

每个功能字段：

| 字段 | 说明 |
|---|---|
| `id` / `name` / `description` | 标识、名称、描述 |
| `signals` | 显式关联的信号，**保持书写顺序**排在最前（条件/`code` 里自动发现的按字母序追加）。功能按钮据此一次性把整组信号加入 Graphics |
| `enter` | 进入条件（满足即进入） |
| `exit` | 退出条件（可选，满足即退出） |
| `trigger` | "尝试"条件：触发它但 `enter` 未满足 → 记为**被阻止的进入**（回答"为什么没进入"）。缺省时自动取 `enter` 中的边沿条件 |
| `initial` | 初始是否已激活（默认 false） |

示例（`samples/function_specs/functions.yaml`）：

```yaml
functions:
  - id: cruise
    name: "定速巡航 (Cruise Control)"
    description: "SET 按下且车速≥30 且未刹车 → 进入；刹车/取消 → 退出"
    enter:
      all:
        - rising: CruiseSetBtn
        - {signal: VehSpd, op: ">=", value: 30}
        - {signal: BrakePedal, op: "==", value: 0}
    exit:
      any:
        - {signal: BrakePedal, op: "==", value: 1}
        - {signal: CruiseCancelBtn, op: "==", value: 1}

  - id: overtemp
    name: "电机过温保护"
    enter: {signal: MotorTemp, op: ">", value: 120}
    exit:  {signal: MotorTemp, op: "<", value: 105}
```

内置示例的结果（用于验证）：

```
进入  cruise   t=8.000   SET 上升沿 + VehSpd>=30(32.0) + 未刹车
退出  cruise   t=15.000  刹车=1
进入  overtemp t=16.760  MotorTemp>120(121.0)
退出  overtemp t=26.700  MotorTemp<105(104.0)
未进入 cruise  t=5.000   触发: SET 上升沿
     ✔ SET 上升沿  ✔ 未刹车
     ✘ VehSpd >= 30 (实际 20.0)  ← 阻止进入
```

## 嵌入 Python 参考功能代码

当声明式条件写不动的逻辑（计数器、定时器、滞回、跨信号运算），可以在 function
里写 `code` 字段，直接执行你的"参考功能代码"：

```yaml
functions:
  - id: cruise_code
    name: "定速巡航 (Python)"
    code: |
      ACTIVE = "ACTIVE"          # 可选：哪些状态算"激活"
      INITIAL = "IDLE"           # 可选：初始状态

      def update(t, s, dt):
          # s['VehSpd'] / s.VehSpd  信号当前值（零阶保持）
          # s.prev.VehSpd           上一采样值
          # s.rising('X') / s.falling('X') / s.changed('X')
          if s.rising("CruiseSetBtn"):
              if s.VehSpd >= 30 and s.BrakePedal == 0:
                  reason(f"SET 按下且车速 {s.VehSpd:.1f} >= 30")
                  return "ACTIVE"
              else:
                  attempt(f"SET 按下但车速 {s.VehSpd:.1f} < 30")   # → 记为"未进入"
          if s.BrakePedal == 1:
              reason("刹车")
              return "IDLE"
          return None            # None = 保持当前状态
```

`update(t, s, dt)` 在每个采样点被调用一次，返回新状态（`None` 表示保持）。
`reason(msg)` 给本次跳变标注原因；`attempt(msg)` 标记一次被阻止的尝试；
模块级变量可在多次调用间保持（用于计时）。完整示例见
`samples/function_specs/functions_code.yaml`（含"温度>120 持续 0.5s 才进入"的防抖计时）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload/dbc` / `/upload/log` / `/upload/spec` | 上传文件；`/upload/log` 可带 `start`/`end`/`max_frames` 增量加载窗口 |
| POST | `/api/load/sample` | 加载内置示例 |
| GET | `/api/status` / `/messages` / `/signals` / `/timeline` | 项目信息。`status` 额外给出 `log_message_count` / `log_unknown_ids` / `data_signal_count` / `decode_failures`（DBC 定义了什么 vs 这份日志录到了什么 vs 哪些录到了却解不开）；`messages` / `signals` 每项带 `has_data` |
| GET | `/api/functions` | 功能清单。每个功能的引用信号分三档：`available`（有数据）/ `nodata`（DBC 里有、本日志没录到）/ `missing`（DBC 里没有） |
| GET | `/api/series?signals=a,b&start=&end=` | 信号时序（绘图） |
| GET | `/api/trace?start=&end=` | 报文跟踪窗口 |
| GET | `/api/analysis` | 功能分析结果（事件 + 阻止 + 区间，含 evidence 证据树）。`fast=1` 走快速求值，见下 |
| GET | `/api/events?types=&functions=&q=&limit=&fast=` | 关键事件列表（下边框面板用）：只回时间/类型/功能/摘要，不含证据树；`functions` 计数按未筛选全集统计并保留 count=0 的功能 |
| POST | `/api/evaluate` | 对任意条件表达式求时间线（监视） |
| GET/POST | `/api/spec` | 读取/设置功能规格文本 |
| GET | `/api/diag/report` | 通信诊断报告（负载/周期/离线/静默/错误帧/状态机/物理推断 + 能力清单） |
| GET | `/api/diag/export?fmt=md` | 导出诊断报告（`md` Markdown / `json`） |
| GET | `/api/diag/config` | 当前诊断配置（由 DBC 自动播种的周期表与节点） |

## 大日志的性能

一份 132 MB / 498 万帧的 BLF（本机实测）：

| 环节 | 说明 |
|---|---|
| 上传 → 坐标轴可交互 | 约 6 s。BLF 走 `blf_fast.py` 的向量化读取（扫 `LOBJ` 签名 + 批量 gather 字段），不再逐帧构造 `Message`/`Frame` 对象 |
| 点功能按钮 → 15 条曲线 | 约 4 s。信号解码走 `fast_decode.py` 的向量化抽位，单条报文从 1.6 s 降到约 0.1 s |
| 回放光标 | 约 60 fps。光标是 DOM 叠加层，不再每帧对整张图 `setOption` |
| 关键事件面板 | 惰性计算，不阻塞加载；面板自己显示"正在分析…" |

**快速求值**（事件面板右上角的开关，默认关）：分析引擎默认对日志里**每一个唯一
时间戳**求值一次，498 万帧就是 498 万拍 × 每个功能，规格里带 `code:` 的功能尤其
慢（分钟级）。打开后只在功能引用的信号真正变化的时刻求值，实测快十几倍。

- 对纯电平 / 边沿逻辑，结果与精确模式一致；
- 对按 tick 计数、或依赖固定步长 `dt` 的 Python `code`，结果可能不同；
- 两种模式的结果分开缓存，来回切不会互相覆盖。

声明式功能（`enter`/`exit`/`trigger`）**不需要**这个开关：引擎会把时间轴切成
"取值恒定"的分段，段内只求值一次再重放状态机，事件与 attempts 的条数、时刻
与逐拍求值逐字节一致。

## 测试

```bash
python3 -m pytest -q
```

其中 `tests/test_blf_fast.py` 和 `tests/test_perf_equivalence.py` 是性能优化的
等价性防线：前者把向量化 BLF 读取的每一帧与 `python-can` 逐字节对拍，后者把
向量化抽位与 `cantools`、分段重放与朴素逐拍求值分别对拍。
`tests/_golden_dump.py` 可以把整条链路的输出 dump 成 JSON 做前后对比：

```bash
python3 tests/_golden_dump.py before.json      # 改动前
python3 tests/_golden_dump.py after.json       # 改动后
python3 tests/_golden_dump.py --diff before.json after.json
```

## 说明与边界

- 下边框是"关键事件"面板。原先放在这里的报文跟踪表已移除——它的主要内容是
  逐帧信号值，与右侧信号值面板重复；`/api/trace` 接口仍保留可用。
- 界面上的时间一律按**相对日志起点**显示。BLF / MF4 的时间戳是绝对纪元秒
  （1787079711.019 这种），直接摊在界面上没法读；CSV 示例 start=0，显示不变。
- 事件面板 DOM 上限 600 行、ribbon 刻度上限 400 个，超出会在列表底部提示还有
  多少条未显示——用功能 chip 或搜索框收窄，而不是静默截断。
- 信号取值采用零阶保持（该信号最近一次报文的值），匹配 ECU 实际采样语义。
- 枚举信号在曲线中以选择索引阶梯显示，tooltip / 轴标签显示枚举名。
- 超大日志：文本格式流式解析、帧以紧凑 NumPy 数组存储、信号按需惰性解码；
  `/api/upload/log` 可带 `start`/`end`/`max_frames` 只加载一段。绘图默认
  20000 点/信号降采样。
- `.blf` 实测通过（`python-can` 往返）；`.mf4` 以真实 Vector 总线日志实测
  （`samples/20260731_EP35_*.mf4`，含 CAN-FD）。注意 MF4 的
  `CAN_DataFrame.DLC` 存的是 **4 bit DLC 编码**不是字节数（9→12、10→16、
  12→24、13→32、15→64），直接拿它切片会把每条 FD 报文截断；解析器优先取
  `CAN_DataFrame.DataLength`，没有该通道时按编码表展开。
- **"有数据"的判据是「解得开」而不是「ID 出现过」。** 报文录到了但按当前 DBC
  解不开（最常见的是长度不符），它下面的信号一个值都拿不到——若仍标成有数据，
  界面会显示 `15/15 有数据`、曲线却全空、事件 0 条，"没查到"就被读成"没问题"。
  `/api/status` 的 `decode_failures` 列出这类报文（报文名 + 日志实际字节数 vs
  DBC 字节数 + cantools 的原始错误），与 `log_unknown_ids`（DBC 根本不认识的
  ID，即装错 DBC）分开报。
- Python `code` 片段以本机权限执行（`exec`），仅应加载你信任的规格文件。
