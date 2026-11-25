# ensp-lab-automation 安装说明

本目录是一个 AI Skill（技能包），让 AI 助手在装有华为 eNSP 的机器上自动完成网络实验：
生成/解析 .topo 工程、通过 console TCP(127.0.0.1:2000+) 批量下发 VRP 配置、
save、ping/接口计数验证、排障报告。核心脚本只依赖 Python3 标准库。
另含可选的 MCP 封装，可供 Trae / Claude Code / Cursor / DSH 等 MCP 客户端直接调用。

## 先确认前置条件

| 要求 | 说明 |
|---|---|
| 目标机装华为 eNSP | console 只听 `127.0.0.1`，技能必须与 eNSP **同机运行**，不能远程控另一台 |
| Python 3.8+ | 核心脚本零第三方依赖，开箱即用 |
| Windows | 下发能力跨平台可用，但环境探测 / 截图量坐标 / OCR 走的是 Windows 自带接口 |
| 可选 `numpy` + `Pillow` | 只有 `figure_probe.py` 的 `--template`（模板匹配）与 `ink_grid`（墨迹密度图）用到 |
| 可选 `rapidocr-onnxruntime` | 只有 `ocr_docx.py`（OCR 报告里的拓扑图）用到 |

不满足前两条就别装——装了也用不起来。

## 目录结构

```
ensp-lab-automation/
├── SKILL.md               # 技能主文档（AI 的操作手册，最核心）
├── README.md              # 对外介绍：能力、门槛、明确不做的事
├── INSTALL.md             # 本说明（部署 / 分发）
├── DEVELOPING.md          # 改代码的人看：改 → 测 → 装 → 包
├── LICENSE.md             # MIT
├── scripts/
│   ├── vrp_console.py     # VRP console 会话库（cmd / paste / save / ping / 计数）
│   ├── preflight.py       # 开工先跑：环境 + 设备 + 日志一次说清
│   ├── apply_plan.py      # 按 JSON 计划下发 + save + expect 断言 + 三份报告
│   ├── persist_cfg.py     # running-config → <GUID>/vrpcfg.zip，扛住重开工程
│   ├── parse_topo.py      # 解析 .topo（设备 / console / PC 地址 / 连线）
│   ├── lab_spec.py        # lab.json 校验 / 编译（拓扑 + 配置 + 断言一份数据）
│   ├── make_topo.py       # 生成 .topo（GBK / 四段 / 合法 MAC / --calibrate / --base-port / --spec）
│   ├── validate_topo.py   # 语义校验：重名 / 端口冲突 / 索引越界
│   ├── topo_export.py     # 导出 Mermaid / CSV / JSON
│   ├── project_registry.py# 多工程 ↔ console 端口登记
│   ├── figure_probe.py    # 从拓扑图量文字 / 图标坐标（OCR 走 Windows 自带）
│   ├── ocr_docx.py        # （可选）OCR 实验报告 docx 里的拓扑图
│   ├── test_skill.py      # 离线回归自测（含冒烟 + 假设备端到端）
│   ├── test_smoke.py      # 每个脚本 import / --help 冒烟（秒级）
│   └── test_offline.py    # 假 VRP 设备实现（test_skill.py 复用）
├── mcp/
│   ├── ensp_mcp_server.py # 可选：stdio MCP 服务，把上面的能力暴露成工具
│   ├── mcp.json           # 客户端配置示例（把 <SKILL_DIR> 换成本机技能目录）
│   ├── selftest_mcp.py    # MCP 协议自测（不需要 eNSP）
│   └── README.md          # MCP 用法
├── tools/
│   └── headless_probe.py  # 实验性：探测能否不启 eNSP GUI 直接跑设备（需关闭 eNSP）
└── references/
    ├── topo-format.md     # .topo XML 格式与全部踩坑经验
    ├── lab-spec.md        # lab.json 字段表 / 端口分配 / 断言写法
    ├── troubleshooting.md # 排障路径、伪故障、PC 侧取证
    └── routing-recipes.md # 静态/默认/聚合/浮动路由、VLAN 配法
```

## 安装到 WorkBuddy（其他设备的 WorkBuddy）

1. 解压整个压缩包，得到 `ensp-lab-automation/` 文件夹。
2. 放到目标机器的技能目录（二选一）：
   - 用户级（所有项目可用）：`~/.workbuddy/skills/ensp-lab-automation/`
     （Windows 即 `C:\\Users\\<用户名>\\.workbuddy\\skills\\ensp-lab-automation\\`）
   - 项目级（仅单个项目）：`<项目根>/.workbuddy/skills/ensp-lab-automation/`
3. 重启 / 新开 WorkBuddy 会话，技能即可被发现。对 AI 说
   「用 eNSP 做组网/路由实验」或「帮我排查 eNSP 连通性」即可触发。

## 安装到 DeepSeek Harness（DSH）

把解压后的目录放进 `~/.dsh/skills/ensp-lab-automation/`（Windows 即
`C:\\Users\\<用户名>\\.dsh\\skills\\ensp-lab-automation\\`），新开会话即可在技能目录里看到。

## 安装到其他 AI（Claude Code / Cursor / 任意支持 Skill 的助手）

1. 解压到任意目录，例如 `~/skills/ensp-lab-automation/`。
2. 把 `SKILL.md` 的路径注册进你的 AI 的技能/规则机制：
   - Claude Code：放入 `.claude/skills/` 或在 CLAUDE.md 里引用 SKILL.md 路径；
   - 其他：将 SKILL.md 全文粘贴/挂载为系统提示或项目规则均可。
3. 脚本用 `python3 <技能目录>/scripts/xxx.py` 直接运行，无需安装依赖。

## 可选：MCP 模式

不需要 MCP 就跳过本节，AI 直接调 `scripts/` 里的库同样工作。
需要时把 `mcp/mcp.json` 里的 **`<SKILL_DIR>`** 换成技能的本机绝对路径（含 `ensp-lab-automation`），
再把整段加进客户端配置；也可以把它复制成 `mcp.local.json` 再改，避免下次升级覆盖：

```json
{
  "mcpServers": {
    "ensp": {"command": "python", "args": ["<技能目录>/mcp/ensp_mcp_server.py"]}
  }
}
```

Claude Code 可一行接入：`claude mcp add ensp -- python <技能目录>/mcp/ensp_mcp_server.py`
接线前先自测：`python <技能目录>/mcp/selftest_mcp.py`（不需要 eNSP）。

## 环境要求

- 目标机器装有华为 eNSP（eNSP_Client.exe）及 Python 3.8+。
- 除 `ocr_docx.py`（可选，需 `pip install rapidocr-onnxruntime`）外，
  所有脚本仅用 Python 标准库，开箱即用。
- AI 通过 127.0.0.1:2000+ 端口访问各设备的 VRP console，需在本机运行。
- 改脚本后先跑 `python scripts/test_skill.py`（离线，全绿再用）。

---

## 2026-11-25 更新（1.0.1：修 4 个「会毁实验」的 P0）

| 缺陷 | 现象 | 现在 |
|---|---|---|
| 整段下发报错后**重放全部命令** | 非幂等命令（成对的 `undo`、`vlan batch`、先 undo 再配）被重复执行，最终状态不可预测 | **不再重放**；报错行原样进报告并标 `(整段下发)`。要精确定位就把那段拆小重发——宁可少一条定位，不重复改一次设备 |
| `ping` 断言缺 `target` | `KeyError` 直接崩，**一份报告都不写** | 计划静态校验先拦下，退出码 2 并指名哪个设备哪条断言 |
| 报告写入失败被静默吞掉 | 打印「已写入 X」其实没写，证据丢了还以为有 | `with` + 捕获 `OSError`；写不进 → stderr 报错 + **退出码 2**，不再谎报成功 |
| 提示符主机名与计划名不一致 | 只记一条 note 就继续下发并 `save` → **多工程并行时把 A 实验的配置写进 B 实验的设备** | **默认跳过该设备**（不下发、不 save、判失败）；确认无误要强推加 `--allow-name-mismatch` |

回归测试 16 条已进 `test_skill.py` 的 `[apply_plan] P0 regressions`：同一条命令只到设备一次、
ping 缺字段退 2 且不碰设备、报告写不了退 2、主机名不一致零下发、逃生开关确实有效。

---

## 2026-11 更新（实测踩坑后补的工具）

| 新增 | 什么时候用 |
|---|---|
| `scripts/preflight.py` | **每次开工第一条命令**：一次给出 eNSP 是否在跑/版本、能否注入鼠标键盘、GUI 里是否开着 CLI 窗口、在线 console 口、每台设备的提示符/视图/分页/接口形态、日志真错误、下一步建议 |
| `scripts/apply_plan.py` | 按 JSON 计划整段下发 + save + 验证 + 出报告（比逐条 `cmd` 快一个数量级） |
| `scripts/persist_cfg.py` | 把 running-config 导出成 `<GUID>/vrpcfg.zip`，让配置活过"关工程再打开" |
| `scripts/figure_probe.py` + `scripts/ocr_win.ps1` | 按试卷拓扑图复现画布：量文字/图标坐标（OCR 用 Windows 自带，无需安装） |

`make_topo.py` 现在：默认 **GBK** 写盘、自动补 `<shapes>`、MAC 用 md5 取 3 字节（6 段）、
新增 `--calibrate`（坐标是截图量的图标中心时用）和 `--encoding`。

**依赖**：核心脚本仍是纯标准库。只有 `figure_probe.py --template`（模板匹配图标）需要
`numpy` + `Pillow`；OCR 依赖 Windows 自带的 OCR 语言包（简体中文系统默认就有）。

---

## 2026-11-12 更新（加断言、修回归）

| 变化 | 说明 |
|---|---|
| `apply_plan.py` 修复 | 补上缺失的 `import os`（此前一运行就 NameError）；去掉复查时向用户视图发 `return` 造成的「成功也判失败」 |
| `apply_plan.py` 新增 `expect` | 下发后按 `contains` / `not_contains` / `regex` / `ping` 逐条判定，退出码 0/1 可被脚本依赖 |
| `apply_plan.py` 新增报告 | `--json`（机读、可回归对比）/ `--md`（可直接贴进实验报告），`--report` 文本版保留 |
| `scripts/test_smoke.py` | 每个脚本 import + `--help` 冒烟，防止「工具已死但没人知道」 |
| `scripts/test_skill.py` | 接入冒烟 + `apply_plan` 对假 VRP 的端到端（成功路径 / 断言失败路径） |
| `DEVELOPING.md` | 新增：源码目录约定、改→测→装→包流程、加脚本清单、三条铁律 |
| `scripts/lab_spec.py` + `references/lab-spec.md` | 新增：`lab.json` 一份数据描述「拓扑+配置+断言」；`make_topo.py --spec` / `apply_plan.py --spec` 直接消费，做新实验不再改脚本常量 |

**分发**：源码在 git 仓库，版本看 tag。给别人的包直接用 GitHub 的 **Code → Download ZIP**，
或 `git archive --format=zip --prefix=ensp-lab-automation/ -o ensp-lab-automation.zip HEAD`。
不要再用历史上手工传的旧 zip 当分发源。

**已验证环境**：eNSP 1.2.00.510（`D:\ENSP\eNSP`）+ `ensp-vbox-shim`；工程 `version="1.3.00.100"`
在该版本下可正常打开。
