# eNSP MCP 封装（可选）

把技能里的 console / 拓扑脚本包成 **MCP server**（stdio，标准库，零依赖），
让 Trae / Claude Code / Cursor / DSH 等 MCP 客户端直接调用工具，而不是让 AI 现写 Python。

## 工具列表

| 工具 | 作用 |
|---|---|
| `ensp_scan_devices` | 扫 2000-2010 console 端口，`identify=true` 顺带读主机名 |
| `ensp_connect` | 建会话并等 VRP 提示符；`expected_name` 防配错设备 |
| `ensp_run_command` | 下发一条/多条命令；≥3 条自动整段粘贴（`fast` 强制），超长输出按 `max_chars` 截断；危险命令需 `confirm_destructive` |
| `ensp_save_config` | `save`（自动应答 y/n）；预置 vrpcfg.zip 不会被加载 |
| `ensp_ping` | `ping -a <source>`；验证路由必须带源 |
| `ensp_interface_brief` | 解析 `display ip interface brief` |
| `ensp_interface_counters` | 接口计数；给 `ping_target` 则返回 ping 前后差分 |
| `ensp_parse_topo` | 解析 `.topo`（设备/console/PC 地址/连线） |
| `ensp_build_topo` | 生成 `.topo`（接口名→索引按真实 slot17 换算） |
| `ensp_list_projects` | 已登记工程的端口块与设备→端口映射（多工程定位） |
| `ensp_validate_topo` | 语义校验（重名/端口冲突/索引越界），开 eNSP 前先跑 |
| `ensp_export_topo` | 导出 Mermaid / CSV / JSON，写报告用 |
| `ensp_backup` | 读 running-config 存档，改配置前先做 |
| `ensp_wait_for` | 轮询条件（AP 上线、OSPF Full），替代 sleep 盲读 |
| `ensp_list_sessions` / `ensp_disconnect` | 查看/关闭会话 |

设计取舍：设备操作全部走 eNSP 给每台设备开的 console TCP（127.0.0.1:2000+），
不依赖任何 eNSP 私有 API；危险命令（`reboot` / `reset saved-configuration` /
`format` / `erase` 等）默认拒绝，因为 VRP 在 eNSP 里 `reboot` 会掉进引导壳。

## 配置

客户端配置示例见同目录 `mcp.json`（把路径换成你的技能目录）：

```json
{
  "mcpServers": {
    "ensp": {
      "command": "python",
      "args": ["<skill>/mcp/ensp_mcp_server.py"]
    }
  }
}
```

Claude Code：`claude mcp add ensp -- python <skill>/mcp/ensp_mcp_server.py`
DSH：在插件的 MCP 客户端配置里指向同一命令。
Trae：设置 → MCP → 填 `python` + 脚本绝对路径。

## 自测（不需要 eNSP）

```bash
python <skill>/mcp/selftest_mcp.py
```

它会真起一个 stdio 服务，跑完 initialize → tools/list → build_topo → parse_topo，
并检查「无 eNSP 时 console 调用干净报错」。全部 PASS 才算接线正确。

## 注意

- stdout 是协议流，日志一律走 stderr；不要在这个脚本里 `print()`。
- 一个 server 进程持有会话表，客户端断开后会话自动释放；端口 2000+ 一次只对应
  一个 eNSP 工程（多开工程会端口冲突）。
- 生成的 `.topo` 仍要用户在 eNSP 里打开并启动设备后，才能 `ensp_connect`。