# eNSP .topo 格式与踩坑

本文记录 `parse_topo.py` / `make_topo.py` 依据的真实文件格式，以及在各款设备上实测出来的行为。

## 文件与目录结构

**工程 = 一个文件夹**：

```
<项目名>/
├── <项目名>.topo          ← XML，设备 + 连线 + 画布文字
├── <GUID 文件夹>/         ← 每台路由器/交换机一个，名字等于 <dev id="GUID">
│   └── vrpcfg.zip         ← eNSP 执行 save 后才写入（内含 vrpcfg.cfg）
└── <GUID 文件夹>/...      ← PC 没有文件夹，com_port=0
```

- `<dev id="...">` 的 GUID **必须**等于对应子文件夹名，否则设备起不来。
- 反复重新生成会留下不再被引用的 GUID 文件夹；eNSP 会忽略它们，但目录会很乱。`parse_topo.py` 会把这类**孤儿文件夹**列进 warnings。
- `make_topo.py` 用 `uuid5("ensp-lab:"+设备名)` 生成 GUID：同名设备永远得到同一个 GUID，重复生成不会产生孤儿目录。

## XML 骨架

```xml
<?xml version="1.0" encoding="UNICODE" ?>     ← 注意：除非标准编码
<topo version="1.3.00.100">
    <devices>
        <dev id="GUID" name="AR1" poe="0" model="AR2220" settings=""
             system_mac="00-E0-FC-.." com_port="2000" bootmode="0"
             cx="161.0" cy="460.0" edit_left="188" edit_top="514">
            <slot number="slot17" isMainBoard="1">
                <interface sztype="Ethernet" interfacename="GE" count="1" />
                <interface sztype="Ethernet" interfacename="GE" count="2" />
            </slot>
        </dev>
    </devices>
    <lines>
        <line srcDeviceID="GUID-A" destDeviceID="GUID-B">
            <interfacePair lineName="Copper" srcIndex="0" ... tarIndex="1" ... />
        </line>
    </lines>
    <txttips>
        <txttip left="100" top="120" content="192.168.10.0/24" ... />
    </txttips>
</topo>
```

### 两个编码坑

1. **文件头声明 `encoding="UNICODE"`**。`UNICODE` 不是合法编码名，任何 XML 解析器都会拒绝。
   - 实测这些文件在**磁盘上是 UTF-8**；`parse_topo.py` 先按 utf-8 解码，解析前把声明改写成 `encoding="utf-8"`。
   - 不要按 utf-16 读：会得到一堆乱码汉字。
2. 写回文件时沿用 UTF-8 与 CRLF 即可，eNSP 仍能打开。

## 接口索引（`srcIndex` / `tarIndex`）

索引按 `<slot17>` 里的 `<interface>` **声明顺序**逐个累加，每个声明重复 `count` 次。索引不是"接口号"，是连杆用的内部序号。

| 型号 | slot17 声明 | 索引 → 接口 |
|---|---|---|
| AR2220 | GE count=1 + GE count=2 | 0→GE0/0/0, 1→GE0/0/1, 2→GE0/0/2 |
| AR2220 + 串口卡 | 上面 + Serial count=2，并有 `<slot isMainBoard="0" id="0" type="521"/>` | 3→Serial1/0/0, 4→Serial1/0/1 |
| AR1220 | GE count=2 + Ethernet count=8 | 0→GE0/0/0, 1→GE0/0/1, 2…9→Eth0/0/1…8 |
| S3700 | Ethernet count=22 + GE count=2 | 0…21→Eth0/0/1…22, 22→GE0/0/**1**, 23→GE0/0/**2** |
| PC | Ethernet count=1 | 0→Eth0/0/1 |

### 两处极易写错的起始编号

- **S3700 的千兆上行口是 GE0/0/1 和 GE0/0/2，不是从 0 开始。** 路由器才从 GE0/0/0 起。实测 `10.topo` 里 AR5→LSW1 走的就是 `GigabitEthernet0/0/1`。
- **AR2220 的 GE 声明是拆成两条写的**（count=1 再 count=2）。第二条是**接着**编号的（GE0/0/1、GE0/0/2），不是各自从 0 重来。

两条都写错过一次，表现是 `ValueError: ... interface 'GigabitEthernet0/0/2' not present`，或者链路全挤到同一个口、整片链路 down。`parse_topo.interface_index()` 现在把可用接口名一起报出来，便于快速定位。

## 设备属性

| 属性 | 说明 |
|---|---|
| `com_port` | console TCP 端口。路由器/交换机从 **2000** 起递增；PC 恒为 `0` |
| `bootmode` | 实测保存值均为 `0`；打开工程后仍需在 GUI 里启动设备 |
| `settings` | PC 的地址全在这里：`-simpc_ip/-simpc_mask/-simpc_gateway/-simpc_mac`，进 GUI 不用配 |
| `cx/cy` | 画布坐标；`edit_left/edit_top` 一般是 `+27/+54` 的偏移，不起作用 |
| `system_mac` | 设备 MAC，PC 上它同时作为 `-simpc_mac` |

PC 的 `settings` 字段很长（还带多播、DHCP、IPv6 等大量默认项），`make_topo.py` 里有完整模板可直接套用。

## 连线

```xml
<interfacePair lineName="Copper" srcIndex="0" srcBoundRectIsMoved="0"
    srcBoundRect_X=".." srcBoundRect_Y=".." srcOffset_X="0.0" srcOffset_Y="0.0"
    tarIndex="22" tarBoundRectIsMoved="0" tarBoundRect_X=".." tarBoundRect_Y=".."
    tarOffset_X="0.0" tarOffset_Y="0.0" />
```

- `lineName` 实测有 `Copper`（双绞线）和 `Serial`（串口线）两种。
- 串口线在真实工程里是 `srcBoundRectIsMoved="1"`，且 **src/tar 各自的 offset 不同**：
  `srcOffset_X="-14" srcOffset_Y="-20"`、`tarOffset_X="-39" tarOffset_Y="8"`。
  把两份复制成同一份会触发 XML duplicate attribute 报错。
- `srcBoundRect_*` 存的是该设备画布上的接口位置，被移动过才非 0；`make_topo.py` 写 0，eNSP 打开时自己重算。

## vrpcfg.zip 机制（最容易白干的一点）

把 `vrpcfg.zip`（内含 `vrpcfg.cfg`）手工塞进 GUID 文件夹，**eNSP 启动设备时不读它**，设备照样是空配置。

正确流程：**生成拓扑 → 用户在 GUI 启动设备 → console 实时下发配置 → `save`**。`save` 之后 eNSP 自己会把 `vrpcfg.zip` 写正确，此后正常关闭再打开工程能保留配置。

`vrpcfg.cfg` 的格式就是普通 VRP 配置（`sysname`、`interface GigabitEthernet0/0/0` / `ip address ...`、`ip route-static ...`），可以直接当"期望配置"对照。

## 打开工程

- 双击 `.topo` 或靠文件关联打开；`eNSP_Client.exe <path>.topo` 传参**会被忽略**，只会开空白工程。
- 改了磁盘上的 `.topo`，必须**关闭旧工程再重开**（内存里还是旧的）；旧设备在运行时其文件被占用，清理目录前要先在 GUI 停设备。
- **一次只开一个工程**：各工程都用 2000+ 作 console 端口，同时开会端口冲突。

## 校验

`make_topo.validate_xml()` 用标准库 `ElementTree` 校验生成的文档（先改写 `encoding`）。另外用它检查：

- 每台路由器/交换机都有对应 GUID 文件夹；
- 没有孤儿 GUID 文件夹；
- 生成的 `.topo` 用 `parse_topo.py` 再解析一遍能拿回同样的设备/连线——生成器与解析器互为对照，任一侧写错都会立刻暴露。

## eNSP 接受 .topo 的四个硬条件（2026-11 实测，违反是「静默降级」）

| 条件 | 违反现象 |
|---|---|
| 顶层四段 `<devices> <lines> <shapes> <txttips>` 齐全（可以为空 `<shapes>`） | 打开工程画布空白、状态栏「总数：0」，**不报错** |
| 文本按 **GBK**（系统 ANSI）写 | 中文标签乱码；英文数字正常，容易误判成"标注写错" |
| 每个 `system_mac` 是合法 **6 段** MAC | 该 `<dev>` 被丢弃（少几台，其余正常） |
| `<topo version>` 不必等于安装版本 | 实测 eNSP 1.2.00.510 能正常打开 `version="1.3.00.100"` 的工程 |

`.topo` 文件名建议与工程文件夹同名（eNSP 自己的例子都同名）。

## cx/cy 不是图标中心

`<dev cx cy>` 是 eNSP 的锚点，渲染出的图标中心会偏：

| 型号 | 图标中心相对 cx/cy |
|---|---|
| PC | (+27, +42) |
| S3700 | (+3, +41) |

所以「从截图上量出图标中心 → 直接填 cx/cy」会让整套图整体往右下偏。
`make_topo.py --calibrate` 会自动扣掉这两组偏差。

## 配置持久化：save ≠ 重开还在

- `save` 只写设备自己的 flash；**关掉工程再打开，设备常回默认配置**（实测 VLAN 全丢）。
- eNSP 启动设备时有一步「把工程目录里的 `<GUID>/vrpcfg.zip` 装进设备」，
  缺文件时 Conserver 日志出现 `Failed to put vrpcfg.zip`（这是提示，不是致命错误）。
- 正确做法：配完 `save` 后再把 `display current-configuration` 导出成
  `<GUID>/vrpcfg.zip`（`scripts/persist_cfg.py`），然后"关→开→启动"实测确认。
