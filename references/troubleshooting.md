# 排障与取证（按序执行，别跳步）

## 0. 经实测的 console 事实（先看，能省一轮排查）

- **`screen-length` 是 VTY 命令**：交换机 console 口会回
  `Error: The type of the included user interface must be VTY.`。
  所以分页不能靠它关，只能遇到 `---- More ----` 补空格（`read_until` 已内置）。
  路由器上通常能成功；`disable_paging()` 会记住结果，`paging_supported=False` 不代表故障。
- **console 会保留上次的视图**：上次停在 interface 视图，这次 `ip address ...` 会配到那个接口上。
  批量前先 `con.return_to_user_view()`（MCP 的 `ensp_run_command` 默认已做）。
- **同一个 console 端口被 GUI 的 CLI 窗口共用**：批量执行时提醒用户别在 eNSP 里同时敲命令。
- 新启动设备可能先打印
  `Info: You are advised to change the password to ensure security.`，属正常，不用等输入。
- `save` / `reset saved-configuration` / `reboot` 会确认；`undo vlan` 之类 **undo 不保证**会问。
- **高频连断会让 eNSP 的 VBox 服务崩掉**，连带整拓扑设备掉线。扫描只扫 2000-2010，
  重复操作复用会话（MCP 里会话常驻），不要为"快一点"反复新建连接。

## 1. 标准排障路径

1. `display ip interface brief` — 接口物理/协议 up？IP 配上了？
   物理 down = 网线/对端没起；协议 down = 对端未配同网段地址或封装不一致。
2. `display ip routing-table protocol static` — 静态路由是否 `Active RD`？
   下一跳是否可达、出接口是否合理。
3. `display arp all` — 下一跳 ARP 是否解析（`00-00-00-00-00-00` = 没解析）。
4. **接口计数差分**：`con.count_and_ping(iface, target, source=...)` 或 ping 前后各取一次
   `iface_counters`。
   - rx 不变 → 帧没到本接口（上游/链路/对端问题）
   - rx 涨、tx 不涨 → 收到了没转发（路由/ACL/下一跳 ARP）
5. `ping -a <源地址>` 换源，排除「路由不覆盖互联段」的假故障。
6. 仍不通 → 在 eNSP GUI 停止/启动涉事设备（数据面偶发假死），重配前先 `save`。

**链路全 down 先查 .topo 的 srcIndex/tarIndex**：接口名→索引换算错会让所有线挤到同一个口。

## 2. 三个高频"假故障"

| 现象 | 真相 | 处理 |
|---|---|---|
| 路由器 ping 远端 PC 100% 丢包 | 默认源是出口互联地址，对端没有回互联段的路由 | `ping -a <目的网段内的本端地址>` |
| 多跳路径首 ping 丢 20%~50%，但 `ok=True` | 逐跳 ARP 解析吃掉头几个 ICMP | 再 ping 一次，通常 0% |
| 验证浮动路由时两端同时切备份 | 关主链路时对端接口也 down，两端 prio60 主路由同时失效 | 预期行为，双向都走 prio80 |

## 3. PC 侧取证（PC 没有 console TCP 口）

- **先读已存 .topo**：`parse_topo.parse_topo()` 一次拿到全部 PC 的 ip/mask/gateway 和
  「PC—交换机—路由器」连线。PC 网关填错表现为「只有这一台跨网段不通」。
- **网关是否生效的黄金测试**：从对端路由器 `ping -a <对端网段地址> <PC IP>`。
  同网段源通、跨网段源全不通 ⇒ PC 没经网关回包。
  所有 PC 对称失败 ⇒ 网关普遍没生效（对话框里有值 ≠ 生效），让用户在 PC 命令行 `ipconfig` 核对。
- 交换机 `display mac-address` 偶发空回显，重试 2~4 次再判定。

## 4. 无法连通时的设备侧顺序

```
con.iface_brief()                      # 接口状态一屏看完
con.cmd("display ip routing-table")    # 路由表
con.cmd("display arp all")             # 下一跳解析
con.count_and_ping("GigabitEthernet0/0/0", "192.168.2.10", source="192.168.1.254")
```

## 5. 报告拓扑图 OCR（用户说「按第X题/按图」）

- `python scripts/ocr_docx.py <报告.docx>` 先列「段落 ↔ 图片」顺序，某题的图紧跟其题干；
  再 `python scripts/ocr_docx.py <报告.docx> imageN.png crop` OCR 指定图。
- 图字小：放大 3~8 倍 + 提对比度，多尺度去重合并，筛 `\d+\.\d+` 找 IP。
- 环境看不了图时，也可让用户描述；图没标的互联段按惯例补 10.0.0.0/24 并明确告知。

## 6. 多工程并存的坑

- 所有工程默认从 2000 起分配 console 口，**同时打开两个工程必然端口冲突**。
- 用 `make_topo.py --base-port 2012` 给每个工程独立的 12 口段，并用
  `project_registry.py register <topo>` 登记；之后 `project_registry.py which 2001`
  就能回答「这个口是哪个工程的哪台设备」，不用扫全部端口。
- 未登记就扫描时，先看 `display ip interface brief`：有 Serial 口/几个 GE 可辨认型号，
  再用 `sysname` 或 ARP 表确认身份，避免配错设备。
## 打开工程后「总数：0」（2026-11 实测，三次踩同一类坑）

按顺序查，前三条都是 eNSP **不报错**的静默失败：

1. `.topo` 顶层缺 `<shapes>` 段 → 必须四段齐全（`make_topo.py` 已自动补）。
2. `system_mac` 不是 6 段 → 该设备被丢弃。
3. 文本不是 GBK → 中文标注乱码（设备本身还在，但标签全花）。
4. **双击 .topo 不是"打开"**：eNSP 单实例且忽略命令行参数，双击只会闪一下，
   屏幕上还是原来那个空白工程。必须 `文件 → 打开`。
5. 用 `parse_topo.py` 回读文件确认设备数，和 eNSP 状态栏「总数」对比，差在哪台就知道是哪条。

## 配置 save 了，重开工程却变回默认

`save` 只进设备 flash；工程目录里没有 `<GUID>/vrpcfg.zip` 时，重开就回默认。
跑 `python scripts/persist_cfg.py <x.topo>` 把 running-config 导出成 zip，再关→开验证一次。

## 命令被吃掉 / 出现没敲过的命令

- `<Huawei>ystem-view` + `Unrecognized command`：分页提示符吞了首字符 →
  发命令前先发一个裸 `\r` 并读干净（`cmd()`/`paste()` 已内置）。
- 出现你没发过的命令（例如主机名突然变成 `X`）：**同一个 console 上有第二个客户端**——
  eNSP 里开着这台设备的 CLI 窗口，或另一个脚本还连着。`preflight.py` 会列出 GUI CLI 窗口；
  让用户关掉后再下发。
- 直接发 `system-view` 报 `Unrecognized command`：设备停在配置/子视图，先 `return_to_user_view()`。

## console 回 `##` 或长时间没提示符

设备还在引导（S3700/AR 20~90s）。`wait_ready()` 会一直等到 `<主机名>`；
不要因为「端口通了」就判定就绪。`reboot` 会掉进引导壳，整机复位只能 GUI 停止/启动。

## 不要用左侧设备面板的项数判断 eNSP 是否正常

实测面板 listview `LVM_GETITEMCOUNT` 返回 0，但面板里型号是正常显示的。这条不可信。
