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
- **报文跟踪表**：与光标联动，显示原始帧与解析后的信号值。
- **在线编辑规格**：随时修改功能条件并重新分析。
- **零配置启动**：首次打开自动加载内置示例（定速巡航 + 过温保护场景）。

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
`functions.yaml`）。也可以手动：顶部 **加载示例** / 拖拽 DBC、日志、规格文件到页面。

## 目录结构

```
canprobe/
├── run.py                 # 入口
├── canprobe/              # 后端 (FastAPI)
│   ├── main.py            #   API + 静态服务
│   ├── dbc_loader.py      #   DBC 加载与信号元数据
│   ├── log_parser.py      #   asc/csv/json/trc/blf/mf4 解析 + 流式 iter_frames
│   ├── decoder.py         #   帧 → 信号时序 (紧凑存储 + 惰性解码)
│   ├── analyzer.py        #   功能状态机 + 证据引擎（声明式）
│   ├── executor.py        #   Python 参考功能代码执行
│   └── store.py           #   内存项目状态
├── web/                   # 前端 (原生 JS + ECharts)
│   ├── index.html / app.js / style.css
│   └── vendor/echarts.min.js
├── samples/               # 示例数据 + 生成脚本
│   ├── generate_samples.py
│   ├── cruise.dbc / cruise.csv / functions.yaml
└── tests/                 # pytest
```

## 功能分析规格（functions.yaml）

条件表达式语法：

| 形式 | 含义 |
|---|---|
| `{signal: 名, op: 操作符, value: 阈值}` | 数值/枚举比较，`op ∈ >,<,>=,<=,==,!=` |
| `{rising: 信号}` / `{falling: 信号}` / `{changed: 信号}` | 边沿触发（0→1 / 1→0 / 任意变化） |
| `{all: [...]}` / `{any: [...]}` / `{not: {...}}` | 逻辑与 / 或 / 非 |

每个功能字段：

| 字段 | 说明 |
|---|---|
| `id` / `name` / `description` | 标识、名称、描述 |
| `enter` | 进入条件（满足即进入） |
| `exit` | 退出条件（可选，满足即退出） |
| `trigger` | "尝试"条件：触发它但 `enter` 未满足 → 记为**被阻止的进入**（回答"为什么没进入"）。缺省时自动取 `enter` 中的边沿条件 |
| `initial` | 初始是否已激活（默认 false） |

示例（`samples/functions.yaml`）：

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
  - id: cruise_py
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
`samples/functions_py.yaml`（含"温度>120 持续 0.5s 才进入"的防抖计时）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload/dbc` / `/upload/log` / `/upload/spec` | 上传文件；`/upload/log` 可带 `start`/`end`/`max_frames` 增量加载窗口 |
| POST | `/api/load/sample` | 加载内置示例 |
| GET | `/api/status` / `/messages` / `/signals` / `/timeline` | 项目信息 |
| GET | `/api/series?signals=a,b&start=&end=` | 信号时序（绘图） |
| GET | `/api/trace?start=&end=` | 报文跟踪窗口 |
| GET | `/api/analysis` | 功能分析结果（事件 + 阻止 + 区间） |
| POST | `/api/evaluate` | 对任意条件表达式求时间线（监视） |
| GET/POST | `/api/spec` | 读取/设置功能规格文本 |

## 测试

```bash
python3 -m pytest -q
```

## 说明与边界

- 底部"功能分析证据"文本面板当前暂不显示（后续通过工况自动解析 + 自动关联信号
  再放回）；分析引擎本身仍运行，功能状态以泳道图 + 侧栏激活区间呈现。
- 信号取值采用零阶保持（该信号最近一次报文的值），匹配 ECU 实际采样语义。
- 枚举信号在曲线中以选择索引阶梯显示，tooltip / 轴标签显示枚举名。
- 超大日志：文本格式流式解析、帧以紧凑 NumPy 数组存储、信号按需惰性解码；
  `/api/upload/log` 可带 `start`/`end`/`max_frames` 只加载一段。绘图默认
  20000 点/信号降采样。
- `.blf` 实测通过（`python-can` 往返）；`.mf4` 解析器按 asammdf 自身提取逻辑
  实现，完整实测需一个真实 Vector 总线日志 `.mf4`（放入 `samples/can_sample.mf4`
  后 `pytest tests/test_parsers.py` 会自动跑对应用例）。
- Python `code` 片段以本机权限执行（`exec`），仅应加载你信任的规格文件。
