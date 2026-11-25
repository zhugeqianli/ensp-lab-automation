# lab.json：一份数据描述一个实验

`lab.json` 把 **拓扑 + 配置 + 断言** 放在一个文件里，脚本读它，不用再改 Python 常量：

```bash
python scripts/lab_spec.py validate lab.json     # 只校验，秒回
python scripts/make_topo.py --spec lab.json      # 生成 .topo（设备/连线/PC 地址/标注/端口段）
python scripts/apply_plan.py --spec lab.json --md 报告.md   # 下发 config 并按 expect 判定
```

## 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 否 | 工程名（默认 `lab`），同时是 `<name>.topo` 的文件名 |
| `base_port` | 否 | 第一个 console 口（默认 2000）。多工程共存时每个工程用独立段 |
| `encoding` | 否 | `.topo` 写盘编码，默认 `gbk`（eNSP 按 ANSI 读，别改成 utf-8） |
| `calibrate` | 否 | `true` 时把 `x/y` 当成「截图量出的图标中心」，自动扣 eNSP 锚点偏差 |
| `devices[]` | 是 | `name` / `model` / `x` / `y`；PC 还要 `ip`（`mask`、`gateway` 可选） |
| `links[]` | 否 | `a` / `a_if` / `b` / `b_if` / `media`（`Copper` 默认，或 `Serial`） |
| `labels[]` | 否 | `text` / `x` / `y`，画布上的文字标注（VLAN 号、部门、IP 段…） |
| `config` | 否 | 设备名 → `{lines, verify, expect}`，只允许非 PC 设备 |
| `save` | 否 | 下发后是否 `save`，默认 `true` |

`model` 目前支持：`AR1220` / `AR2220` / `S3700` / `PC`（见 `parse_topo.DEFAULT_SLOTS`）。
`links` 里的接口名用 VRP 全名（`GigabitEthernet0/0/1`、`Ethernet0/0/1`、`Serial1/0/0`），
脚本会按机型换算成 eNSP 的索引，写错会直接报错而不是生成坏工程。

## console 端口分配规则

按 `devices` 顺序，非 PC 设备从 `base_port` 起逐个 +1（PC 不占端口）。
所以 `devices` 的顺序决定 `2000/2001/...` 归谁；`apply_plan --spec` 用的是同一套规则，
不需要手写 `port`。要换顺序就调 `devices` 的顺序，别另外写端口。

## 断言（`expect`）

```json
"expect": [
  {"name": "VLAN10 存在",  "cmd": "display vlan",          "contains": "10"},
  {"name": "没有报错",      "cmd": "display vlan",          "not_contains": "Error:"},
  {"name": "OSPF 有 Full", "cmd": "display ospf peer brief", "regex": "Full"},
  {"name": "PC 通",        "ping": {"target": "192.168.10.10",
                                   "source": "192.168.1.1",
                                   "count": 4, "max_loss": 0}}
]
```

- `contains` / `not_contains` / `regex` 对 `cmd` 的输出做匹配，四选一。
- `ping` 只在**有 IP 的设备**（路由器 / 配了 Vlanif 的交换机）上有效；纯二层交换机没地址，
  要从 PC 侧验证得在 eNSP 里手工 ping，或用路由器当源。
- 断言失败时 `apply_plan` 退 `1`，报告里是 `[FAIL]`；全部通过才退 `0`。

## 最小例子

```json
{
  "name": "demo",
  "devices": [
    {"name": "R1", "model": "AR2220", "x": 200, "y": 200},
    {"name": "PC1", "model": "PC", "x": 500, "y": 200,
     "ip": "192.168.1.10", "mask": "255.255.255.0", "gateway": "192.168.1.1"}
  ],
  "links": [{"a": "R1", "a_if": "GigabitEthernet0/0/0",
             "b": "PC1", "b_if": "Ethernet0/0/1"}],
  "config": {
    "R1": {
      "lines": ["system-view", "sysname R1",
                "interface GigabitEthernet0/0/0",
                "ip address 192.168.1.1 255.255.255.0", "quit", "return"],
      "expect": [{"name": "接口地址生效", "cmd": "display ip interface brief",
                  "contains": "192.168.1.1"}]
    }
  },
  "save": true
}
```

## 常见校验错误

| 报错 | 原因 |
|---|---|
| `model='XXX' 不支持` | 机型不在 `DEFAULT_SLOTS`，先扩展 parse_topo 的槽位表 |
| `links[i] 的 a='X' 不在 devices 里` | 设备名拼错，或忘了加到 `devices` |
| `config.PC1: PC 没有 CLI` | 给 PC 写了配置；PC 地址应写在 `devices[].ip` |
| `设备名重复` | 两个设备同名（GUID 会冲突） |

改实验流程：改 `lab.json` → `lab_spec.py validate` → `make_topo.py --spec` →
eNSP 打开工程启动设备 → `apply_plan.py --spec`。全程不需要动脚本源码。