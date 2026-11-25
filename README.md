# eNSP 实验自动化 · AI Skill

让 AI 助手（DeepSeek Harness / Claude Code / Cursor / Trae / WorkBuddy …）在装有**华为 eNSP** 的机器上
把一套网络实验真正做完：

```
生成 / 校验 .topo  →  你打开工程并启动设备  →  console 批量下发 VRP 配置
   →  自动 save  →  ping / 接口计数验证  →  文本 + JSON + Markdown 三份报告
```

设备操作全部走 eNSP 给每台设备开的 console TCP（`127.0.0.1:2000+`），**不碰任何私有 API、不装驱动、
不做 GUI 注入**。核心脚本只用 Python 标准库，开箱即用。

---

## 先说清楚前置条件（不满足就别装）

| 要求 | 说明 |
|---|---|
| **华为 eNSP 装在要跑实验的那台机器上** | 技能通过 `127.0.0.1:2000+` 的 console 口和设备通信，**必须和 eNSP 同一台电脑**，不能远程控另一台 |
| **Python 3.8+** | 核心脚本零第三方依赖 |
| **Windows** | 核心下发能力跨平台可用，但环境探测 / 截图量坐标 / OCR 走的是 Windows 自带接口 |
| 可选 `numpy` + `Pillow` | 只有 `figure_probe.py --template`（模板匹配图标）、`ink_grid`（墨迹密度图）用到 |
| 可选 `rapidocr-onnxruntime` | 只有 `ocr_docx.py`（OCR 实验报告里的拓扑图）用到 |

没装 eNSP 的人装了也用不了——这条写在最前面，避免装完再说「怎么没反应」。

---

## 装

三种客户端都是「把整个 `ensp-lab-automation/` 目录放进技能目录，然后**新开一个会话**」。
技能列表是会话开始时确定的，不重开会话看不到新技能。

| 客户端 | 放哪里 |
|---|---|
| DeepSeek Harness (DSH) | `%USERPROFILE%\.dsh\skills\ensp-lab-automation\` |
| WorkBuddy（用户级 / 项目级） | `%USERPROFILE%\.workbuddy\skills\...` / `<项目根>\.workbuddy\skills\...` |
| Claude Code | `.claude/skills/ensp-lab-automation/` |
| 其他 AI | 解压到任意目录，把 `SKILL.md` 挂成系统提示 / 项目规则 |

细节、校验方式、各客户端的差异见 [INSTALL.md](INSTALL.md)。

```powershell
# 校验下载的包（可选，防传输损坏）
Get-FileHash .\ensp-lab-automation.zip -Algorithm SHA256
Expand-Archive .\ensp-lab-automation.zip -DestinationPath .
```

---

## 装完先跑这个（不需要 eNSP 也能跑）

```powershell
python scripts\test_skill.py     # 离线回归：应输出 all checks passed
python scripts\selftest_mcp.py   # MCP 协议自测（不用 MCP 可以跳过）
```

真机开工第一条命令永远是：

```powershell
python scripts\preflight.py
```

一次给出：eNSP 在不在跑 / 版本、能否注入鼠标键盘、GUI 里是否开着 CLI 窗口、哪些 console 口在线、
每台设备的提示符 / 主机名 / 视图 / 分页状态 / 接口形态、日志里的真实错误行、以及下一步该干嘛。
**不要用截图猜界面状态。**

---

## 怎么用（AI 侧的动作）

1. **写一份 `lab.json`** 描述整个实验（拓扑 + 配置 + 断言，一份数据）
   ```powershell
   python scripts\lab_spec.py validate lab.json
   python scripts\make_topo.py --spec lab.json        # 生成 .topo
   ```
2. **你**在 eNSP 里「文件 → 打开」这个 `.topo`，框选设备点绿色三角启动（20~90 秒）。
   AI 点不了这一步，也不需要点。
3. **AI 下发并验证**
   ```powershell
   python scripts\apply_plan.py --spec lab.json --md 报告.md
   ```
   退出码 `0` = 下发、save、全部断言通过；`1` = 有失败（看报告里的 `[FAIL]`）。可直接接 CI。
4. **让配置活过「关工程再打开」**
   ```powershell
   python scripts\persist_cfg.py <x.topo>
   ```
   `save` 只写设备 flash；实测关掉工程再开，设备会回默认配置。这个脚本把
   `display current-configuration` 导出成 `<GUID>/vrpcfg.zip`，补上 eNSP 启动时要装回的那一步。

字段与断言写法见 [references/lab-spec.md](references/lab-spec.md)，排障见
[references/troubleshooting.md](references/troubleshooting.md)。

---

## 它有什么

| 能力 | 在哪 |
|---|---|
| 生成 `.topo`（默认 GBK 写盘、四段结构、合法 MAC、坐标校准、端口分段） | `scripts/make_topo.py` |
| 解析 `.topo`（设备 / console 端口 / PC 地址 / 连线索引） | `scripts/parse_topo.py` |
| 语义校验：重名、端口冲突、索引越界 | `scripts/validate_topo.py` |
| 批量下发 + `save` + 断言（`contains` / `not_contains` / `regex` / `ping`）+ 三份报告 + 退出码 | `scripts/apply_plan.py` |
| console 会话库：`wait_prompt` / `cmd` / `paste` / `save` / `ping` / `iface_brief` / 接口计数差分 | `scripts/vrp_console.py` |
| 环境体检：eNSP 状态、注入能力、GUI CLI 窗口、在线 console、提示符视图、日志真错误 | `scripts/preflight.py` |
| 配置持久化 `<GUID>/vrpcfg.zip` | `scripts/persist_cfg.py` |
| 从试卷拓扑截图量文字 / 图标坐标（Windows 自带 OCR，零安装） | `scripts/figure_probe.py` |
| 多工程 ↔ console 端口登记，防端口串台 | `scripts/project_registry.py` |
| 导出 Mermaid / CSV / JSON | `scripts/topo_export.py` |
| 可选 MCP 封装（stdio，16 个工具，危险命令默认拒绝） | `mcp/` |

覆盖的实验类型：静态 / 默认 / 聚合 / 浮动路由、串口、S3700 交换、VLAN、PC 侧取证、拓扑图复现。
配方见 [references/routing-recipes.md](references/routing-recipes.md)。

---

## 它**不做**的事（避免误会）

- **不会帮你启动设备**。eNSP 的设备启动与进 CLI 需要 GUI 操作；以管理员运行时普通进程还受 UIPI 限制，
  点不动就是点不动（`preflight.py` 会先告诉你「可注入输入 = 不行」，那就交给你点）。
- **不能远程控另一台机器的 eNSP**。console 只听 `127.0.0.1`。
- **没有自动重试、没有配置 diff、没有一键回滚**。下发失败会在报告里按行列出，回退要你自己写
  `undo` 序列。
- **不替代你的判断**。断言只判定你写下的那几条，`verify` 只是把原始回显抄进报告。
- **只覆盖 `SKILL.md` 里列出的实验类型**。没写进去的协议（BGP、MPLS、WLAN…）不保证能用。

---

## 可选：MCP 模式

不想让 AI 写 Python，只想让它调工具？把 `mcp/` 挂成 MCP server：

```powershell
python mcp\selftest_mcp.py        # 接线前先自测，不需要 eNSP
```

把 [mcp/mcp.json](mcp/mcp.json) 里的 `<SKILL_DIR>` 换成技能的本机绝对路径，再按客户端要求引入；
Claude Code 一行即可：

```powershell
claude mcp add ensp -- python "<技能目录>\mcp\ensp_mcp_server.py"
```

---

## 开发与反馈

改脚本前读 [DEVELOPING.md](DEVELOPING.md)：目录约定、改 → 测 → 同步 → 打包、加脚本清单、
以及 `.topo` 的三条「静默毁工程」铁律。

```powershell
python scripts\test_smoke.py     # 秒级：每个脚本 import + --help
python scripts\test_skill.py     # 全量离线回归
```

Issue / PR 欢迎。改动请附带 `test_skill.py` 全绿的结果；涉及真机行为的改动请说明实测环境
（eNSP 版本 + 机型）。

## 许可

[MIT](LICENSE.md)。随便用、改、商用；改完不用回馈，但请保留版权声明。

---

> 免责：本技能会向 eNSP 里的设备下发配置并 `save`。请先在虚拟机或非生产环境验证，
> 不要在真实设备上直接跑生成的计划文件。