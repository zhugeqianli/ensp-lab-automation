# 实验类型速查（配什么命令）

## 地址规划

- 按主机数选前缀：100 台 → /25，50 → /26，25 → /27；互联链路用 /30。
- /30 的 `.3` 是广播地址，不能配在接口上（只能 .1/.2）。
- 写掩码时用点分十进制（`255.255.255.0`），别用 `ip address x 24`。

## 静态 / 默认路由

```
ip route-static 192.168.2.0 255.255.255.0 12.1.1.2      # 广播网段：必须写下一跳 IP
ip route-static 0.0.0.0 0 12.1.1.2                      # 默认路由
ip route-static 0.0.0.0 0 Serial1/0/0                   # 点对点/串口：可只写出接口
```

- 多路访问（以太网/广播）网段**只写出接口会 ARP 错**，必须给下一跳 IP。
- 串口是点对点链路，只写出接口即可。

## 浮动（备份）路由

```
ip route-static 192.168.2.0 255.255.255.0 12.1.1.2              # 主，默认 preference 60
ip route-static 192.168.2.0 255.255.255.0 13.1.1.2 preference 80 # 备，值大=备份
```

主链路断 → 主路由撤出路由表 → 备路由（preference 80）顶上。
验证时源地址要用**主机网段地址**（见 troubleshooting.md）。

## 路由聚合

连续网段取共同前缀合成超网，例如 172.16.0.0/16 ~ 172.18.0.0/16 → 172.16.0.0/14。
配在**汇聚/边界路由器**上指向 Null0 或下一跳，可显著缩小下游路由表。
注意聚合后明细路由仍可存在（最长匹配优先），题目要求「只剩聚合路由」时需 `undo` 明细。

## 主机路由 → 网络路由

把逐条 `/32` 合并成一条 `/24`：路由表大幅精简，新增主机免改路由。
前提是这些主机确实同网段。

## 交换机（S3700）

- 默认**透明桥接，无需任何配置**即可让同 VLAN PC 互通。
- 需要划分 VLAN 时才配：

```
vlan 10
 quit
interface Ethernet0/0/1
 port link-type access
 port default vlan 10
 quit
interface GigabitEthernet0/0/1
 port link-type trunk
 port trunk allow-pass vlan all
```

- S3700 上行口是 `GE0/0/1`、`GE0/0/2`（**不是 GE0/0/0**）。

## 串口（Serial）链路

- eNSP 自动提供时钟，**不需要** `clock rate` / `undo shutdown` 之外的封装配置。
- 两端配同网段 IP（如 10.0.13.1/24 与 10.0.13.2/24）即 up。
- 需要串口卡：AR2220 加 `Serial count=2` + `<slot type="521">`（make_topo 自动处理）。

## 验证清单（配完必做）

1. 每台 `con.save()`。
2. `con.iface_brief()` 看接口 up 与地址。
3. `con.ping(目标, source=源网段地址)` 端到端。
4. 需要证明路径切换时：关主链路 → 再 ping → 看路由表 preference。