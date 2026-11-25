---
name: ensp-lab-automation
slug: ensp-lab-automation
displayName: eNSP 实验自动化（华为网络实验）
version: 1.0.1
summary: 让 AI 在华为 eNSP 里自动完成网络实验：生成/校验 .topo 工程、console 批量下发 VRP 配置并 save、ping 与接口计数断言、txt/json/md 三份报告与可判定的退出码。
description: 在华为 eNSP 中自动完成网络实验：生成/解析 .topo 工程、console 批量下发 VRP 配置并 save、ping/接口计数验证、排障、按试卷拓扑图复现画布。覆盖静态/默认/聚合/浮动路由、串口、S3700、PC 取证、拓扑图 OCR。用户要求用 eNSP 组网/路由/交换实验、批量配置或排连通性时使用。
license: MIT
author: zhuge
tags: [ensp, huawei, vrp, network-lab, topology, router, switch, mcp, automation]
metadata: {"openclaw":{"emoji":"🧪","requires":{"bins":["python3"],"optionalBins":["numpy","Pillow"]},"env":[],"optionalEnv":["ENSP_LAB_REGISTRY","ENSP_HOME"],"stateDirs":["%LOCALAPPDATA%/ensp-lab-automation"],"persistence":"仅在本机写一个工程登记表 registry.json（记录 .topo 与 console 端口的对应关系），可用 ENSP_LAB_REGISTRY 改位置；不联网、不上传任何实验数据。","os":["windows"]}}
---

# eNSP 实验自动化

流程固定三步：**生成/校验 .topo → 用户打开工程并启动设备 → console 批量下发 + save + 验证**。
设备操作全部走 eNSP 给每台设备开的 console TCP（127.0.0.1:2000+），不碰任何私有 API。
脚本只依赖 Python3 标准库（`figure_probe.py` 的 OCR 走 Windows 自带接口，模板匹配才需要 numpy+Pillow）。

## 开工第一件事：跑 preflight（省掉十几轮试探）

```bash
python scripts/preflight.py
```
一次给出：eNSP 是否在跑/版本、**能否注入鼠标键盘**、GUI 里是否开着 CLI 窗口、
哪些 console 口在线、每台设备的 提示符/主机名/视图/分页/接口形态、日志里真正的错误行、
以及"下一步该干嘛"。**不要再用截图猜状态。**

判断分叉：
- `可注入输入 = 不行`（eNSP 常以管理员运行）→ 打开工程、启动设备、双击进 CLI 都必须**让用户做**，
  你只做 console 部分。别再花时间试点击。
- `没有 console 口在线` → 工程没打开或设备没启动，让用户操作（见下面「双击打不开」）。
- `GUI CLI 窗口 = PC1,LSW2...` → 它们和你的 TCP console 抢同一个控制台，必须让用户关掉再下发。

## 三条「静默毁工程」的硬规则（今天全部踩过）

| 规则 | 违反后的现象 |
|---|---|
| **顶层必须四段** `<devices> <lines> <shapes> <txttips>`，空也要写 | eNSP 不报错，直接当空工程打开：画布空白、状态栏「总数：0」 |
| **.topo 必须按 GBK 写**（eNSP 按系统 ANSI 读文本） | 中文标签全变乱码（英文/数字正常，所以很容易误判成"标注写错"） |
| **system_mac 必须 6 段合法 MAC** | 该 `<dev>` 被丢弃，少几台设备、其余正常 |

`make_topo.py` 现在默认 GBK、自动补 `<shapes>`、MAC 用 md5 取 3 字节；写盘前 `validate_xml()`
会把这三条再校验一遍。手工改过 .topo 后，用 `parse_topo.py` 回读确认设备数没少。

## 工作流

1. **建工程**
   ```bash
   python scripts/make_topo.py <dir> --name 实验 --base-port 2000
   python scripts/make_topo.py <dir> --calibrate      # 坐标是从截图上量的「图标中心」时加这个
   ```
   有现成工程先 `parse_topo.py <x.topo>` 看清现状；多工程用 `--base-port 2012` 分段，
   避免端口串台。**一次只开一个工程**。

   **新实验优先写一份 `lab.json`**（拓扑 + 配置 + 断言），脚本只读数据，不再改源码：
   ```bash
   python scripts/lab_spec.py validate lab.json
   python scripts/make_topo.py --spec lab.json
   python scripts/apply_plan.py --spec lab.json --md 报告.md
   ```
   格式和字段见 `references/lab-spec.md`。console 端口按 `devices` 顺序从 `base_port` 分配，
   不用手写 `port`。
2. **让用户启动**（你点不了）：eNSP 里 **文件 → 打开** 选 .topo，框选设备点绿色三角。
   启动 20~90s；console 通了 ≠ VRP 就绪，`wait_prompt()` 会等到 `<主机名>` 提示符。
3. **下发配置**
   ```bash
   python scripts/apply_plan.py plan.json --report r.txt --json r.json --md r.md
   python scripts/apply_plan.py --spec lab.json --md r.md     # lab.json 里已含 config
   ```
   计划文件就是一个 JSON（结构见脚本头注释）：整段 `paste` 一把发、失败行单独重放、
   自动 `return_to_user_view()`、自动 `save`，并把结果出成文本 / JSON / Markdown 三份报告。
   `expect` 才是判定「做没做对」的地方，`verify` 只是把原始回显抄进报告：

   ```json
   {"devices": [{"name": "LSW1", "lines": ["system-view", "sysname LSW1", "vlan batch 10"],
                 "expect": [{"name": "VLAN10 存在", "cmd": "display vlan", "contains": "10"},
                            {"name": "PC1 通", "ping": {"target": "192.168.10.10", "max_loss": 0}}]}],
    "save": true}
   ```
   退出码 0 = 所有设备的下发、save、断言全过；1 = 有失败（看报告里的 `[FAIL]`）。
   **只有在需要逐条判断结果时才用 `con.cmd()`**；40 行配置 `paste` ≈1~2s，逐条 ≈25s。
4. **验证**：`iface_brief()` → `ping(source=...)` → 区分"没收到/没转出"用 `count_and_ping()`。
   浮动/备份路由必须用**业务网段地址**做源；首包丢包是 ARP 假象，再 ping 一次。
5. **让配置活过重开**（必做，否则前功尽弃）
   ```bash
   python scripts/persist_cfg.py <x.topo>
   ```
   `save` 只写进设备 flash；实测**关工程再开，设备会回默认配置**。而 eNSP 启动设备时
   有一步"把工程目录里的 `vrpcfg.zip` 装回设备"（缺文件时 Conserver 日志报
   `Failed to put vrpcfg.zip`）。这个脚本把 `display current-configuration` 导出成
   `<GUID>/vrpcfg.zip`，补上这一步。做完可以"关→开→启动"实测一次确认。

## console 的三个怪癖（不处理就是一小时白干）

1. **分页吞掉下一个命令的首字符**：日志里表现为 `<Huawei>ystem-view` + `Unrecognized command`。
   `cmd()`/`paste()` 已经先发一个裸 `\r` 把提示符刷新干净；自己写 socket 时也要这么做。
2. **设备可能停在 `[Huawei]` 或子视图**：直接发 `system-view` 会报错。
   下发前一律 `con.return_to_user_view()`。
3. **`screen-length 0 temporary` 在 console 口常被拒**（"must be VTY"）。此时分页靠
   `read_until()` 里补空格解决；要真关掉得走
   `system-view → user-interface con 0 → screen-length 0 → quit → return`。

另外：`reboot` 在 eNSP 里会掉进引导壳（`##` 提示符），整机复位只能用 GUI 停止/启动，重启前先 `save`。
设备刚起来时 console 可能回 `##`/空行，属于引导中，等 30~60s 再看。

## 按试卷拓扑图复现画布

1. 量文字和图标位置（不依赖视觉模型）：
   ```bash
   python scripts/figure_probe.py 图.png --ocr-scale 2 --template 496,576,568,628
   ```
   OCR 走 Windows 自带接口（`scripts/ocr_win.ps1`，零安装）；`--template` 给一个已知交换机
   图标的 bbox，会把同型号设备全部匹配出来。输出「文字+像素框」「图标中心」「墨迹密度图」。
2. 把图标中心写进 `lab.json` 的 `devices`（`"calibrate": true`）或 `make_topo.py` 的 DEVICES，
   **加 `--calibrate`**：
   eNSP 的 `cx/cy` 不是图标中心，实测渲染后中心偏移 **PC (+27,+42) / S3700 (+3,+41)**。
   不加校准，整套图会整体往右下偏。
3. 标注位置（部门/VLAN/IP/接口号）用 OCR 给的像素框直接填，不要凭感觉摆；
   摆完用几十行脚本检查"标注框是否压住设备框"，重叠就沿链路方向推远。

## 工具表

| 文件 | 用途 |
|---|---|
| `scripts/preflight.py` | **开工先跑**：环境+设备+日志一次说清，给出下一步 |
| `scripts/vrp_console.py` | 会话库：`wait_prompt` / `cmd` / `paste` / `save` / `return_to_user_view` / `ping` / `iface_brief` / `count_and_ping` |
| `scripts/parse_topo.py` | 解析工程（设备/console/PC 地址/连线索引），`--json` |
| `scripts/make_topo.py` | 生成 .topo（GBK、四段、合法 MAC、`--calibrate`、`--base-port`、`--spec`） |
| `scripts/lab_spec.py` | 校验 / 编译 lab.json（拓扑+配置+断言一份数据） |
| `references/lab-spec.md` | lab.json 字段表、端口分配规则、断言写法 |
| `scripts/apply_plan.py` | 按 JSON 计划批量下发 + save + `expect` 断言 + 文本/JSON/Markdown 报告 |
| `scripts/persist_cfg.py` | 把 running-config 导出成 `<GUID>/vrpcfg.zip`，让配置活过重开 |
| `scripts/figure_probe.py` / `ocr_win.ps1` | 从拓扑图量文字/图标坐标 |
| `scripts/ocr_docx.py` | （可选，需 `rapidocr-onnxruntime`）OCR 实验报告 docx 里嵌的拓扑图 |
| `scripts/validate_topo.py` / `topo_export.py` | 语义校验 / 导出 Mermaid·CSV |
| `scripts/project_registry.py` | 工程↔console 端口登记（多工程） |
| `tools/headless_probe.py` | 实验性：探测能否不启 eNSP GUI 直接跑设备（需先关闭 eNSP） |
| `scripts/test_skill.py` | 离线回归自测（含冒烟 + 假设备端到端），改脚本后先跑 |
| `scripts/test_smoke.py` | 每个脚本能做到 import 和 `--help` 的最小冒烟（可单独跑） |
| `references/topo-format.md` | .topo 格式、接口索引、编码与锚点坑 |
| `references/troubleshooting.md` | 排障路径、伪故障、PC 取证 |
| `references/routing-recipes.md` | 各实验类型配什么 |
| `mcp/` | 可选 MCP 封装（stdio，危险命令默认拒绝） |

## 省 token 的做法

- **先 `preflight.py` 再动手**：它把"eNSP 在不在、设备起没起、我能不能点"一次答完，
  省掉用截图/端口/窗口反复试探的十几轮。
- **不要用截图猜界面状态**；要看状态就读 console（`preflight` 已给）。
- 一次 `paste` 一批命令，别 40 次 `cmd`。
- 报错只取**最后一行**做判断（`cmd()` 已这么抛）。
- 只在需要时读 references，不要整篇塞上下文。
- 多设备并行探测（`scan_ports` 已并行），不要串行 sleep。
- 改动脚本后先 `python scripts/test_skill.py`（离线，全绿再上真机；内含 `test_smoke.py` 的 import/--help 冒烟和 `apply_plan` 的假设备端到端）。改技能本身见 `DEVELOPING.md`。

## 运行

- 先设 `PYTHONDONTWRITEBYTECODE=1`，别让 Python 在技能目录写 `__pycache__`。
- 控制台若是 GBK，`print` 里避免 `▶` 这类符号（脚本已 `stdout.reconfigure(errors="replace")`）。
- 动 eNSP 前说明要改什么、要用户开/停哪些设备；每台配完 `save`，最后统一验证并汇报。