# 改这个技能：开发、回归、发布

这份文档给「要改脚本的人」看。用技能做实验看 `SKILL.md`，装到别的机器看 `INSTALL.md`。

## 目录约定（先记住，能省很多混乱）

| 位置 | 角色 | 能不能手改 |
|---|---|---|
| 本仓库根（git 仓库） | **唯一源码** | 改这里 |
| `%USERPROFILE%\.dsh\skills\ensp-lab-automation\` | DSH 实际加载的安装副本 | 不要手改，从仓库同步过去 |
| git tag / GitHub Release | 对外发布的版本 | 不再手工传 zip 当分发源 |

改完源码 → 跑测试 → 同步到安装副本 → 提交打 tag。这样装上跑的就是刚测过的那份。

## 只做一个新实验（不改代码）

写一份 `lab.json`，放在实验自己的目录里：

```powershell
python scripts\lab_spec.py validate lab.json        # 秒回，先发现拼写/机型错误
python scripts\make_topo.py --spec lab.json         # 生成同目录的 <name>.topo
# eNSP 打开工程 + 启动设备
python scripts\apply_plan.py --spec lab.json --md 报告.md
```

字段和端口分配规则见 `references/lab-spec.md`。只有要新增机型、新脚本、新断言类型时，
才需要动 Python 代码，并按下面「改一个脚本的最小循环」走。

## 「改 → 测 → 装 → 包」

```powershell
python scripts\test_skill.py                    # 1. 全量离线测试，必须全绿

robocopy . "$env:USERPROFILE\.dsh\skills\ensp-lab-automation" /MIR /XD .git   # 2. 同步到安装副本

git add -A                                       # 3. 提交
git commit -m "fix: ..."
git tag v1.0.1                                   # 4. 发版（tag 就是发布版本号）
git push; git push --tags

# 5. 打包：GitHub Release 用完整包，SkillHub 上传包必须去掉 .gitignore
#    （SkillHub 服务端会拒 .gitignore，报「提交失败 / 不允许的文件类型: .gitignore」）
#    另外：许可证文件必须叫 LICENSE.md —— 无扩展名的 LICENSE 也会被拒（同样报「不允许的文件类型」），
#    别改回 LICENSE。GitHub 认 LICENSE.md，能正常识别成 MIT。
git archive --format=zip --prefix=ensp-lab-automation/ -o ..\ensp-lab-automation.zip HEAD
git archive --format=zip --prefix=ensp-lab-automation/ -o ..\ensp-lab-automation-skillhub.zip HEAD ':(exclude).gitignore'
```

`robocopy /MIR` 会把目标目录里多余的文件删掉，所以**不要在仓库里留临时产物**（`__pycache__`、
`.test-work` 等，`.gitignore` 已挡）。发布后**新开一个 DSH 会话**，技能列表里才是新版本。

## 改一个脚本的最小循环

1. 改 `pkg/scripts/xxx.py`。
2. `python pkg\scripts\test_skill.py` —— 全绿才继续。
3. 想只跑某一类：
   - `python pkg\scripts\test_smoke.py` —— 所有脚本 import + `--help`（秒级）
   - `python pkg\scripts\test_offline.py` —— `vrp_console` 对着假 VRP 的协议行为
   - `python pkg\scripts\test_skill.py --topos <目录>` —— 顺手解析你真实的 .topo
4. 按上面「改 → 测 → 装 → 包」同步到安装副本。
5. 真机上还要过一遍 `preflight.py`（eNSP 在跑时）。

## 加一个新脚本的清单

- 文件顶部：`sys.dont_write_bytecode = True`，并把脚本目录插进 `sys.path`（抄现成的）。
- 有 CLI 就必须有 `argparse` 和 `--help`，参数风格跟现有脚本一致（`--json` / `--dry-run`）。
- 别在 import 期做重活（连设备、扫端口、读写文件），测试要靠 import 兜底。
- 能被离线测的逻辑就写进 `test_skill.py`；`test_smoke.py` 会自动发现新脚本，不用登记。
- 在 `SKILL.md` 的「工具表」补一行；对外行为变了，`INSTALL.md` 的更新记录也补一行。

## 写断言（`apply_plan` 的 `expect`）

判定「做对了」只有三种写法，别用「看回显像成功了」当结论：

```json
{"name": "VLAN10 存在",  "cmd": "display vlan",        "contains": "10"}
{"name": "没有报错",      "cmd": "display vlan",        "not_contains": "Error:"}
{"name": "OSPF 有 Full", "cmd": "display ospf peer brief", "regex": "Full"}
{"name": "PC1 通",       "ping": {"target": "192.168.10.10", "source": "192.168.10.1",
                                  "count": 4, "max_loss": 0}}
```

`verify` 只把原始输出抄进报告，不判定；报告同时出 `.txt` / `.json` / `.md` 三份。

## 三条「静默毁工程」的铁律（改生成器时必看）

1. `.topo` 顶层必须四段 `<devices> <lines> <shapes> <txttips>`，空也要写。
2. `.topo` 按 GBK 写盘（eNSP 按系统 ANSI 读）。
3. `system_mac` 必须 6 段合法 MAC。

`make_topo.validate_xml()` 会在写盘前挡这三条；改了生成逻辑后务必跑
`test_skill.py` 里的 round-trip 用例（生成 → `parse_topo` 回读 → 设备数/链路数/PC 地址）。

## 常见坑（都是本地实测过的）

- **Windows 控制台是 GBK**：脚本里 `print` 中文时，子进程要用
  `PYTHONIOENCODING=utf-8` + `encoding="utf-8"` 解码，否则 `subprocess` 会在读输出时抛
  `UnicodeDecodeError`（`test_smoke.py` 已经这么处理）。
- **别在技能目录留 `__pycache__` / `.test-work`**：跑测试时设 `PYTHONDONTWRITEBYTECODE=1`。
- **改 `preflight.py` 的判定逻辑要克制**：它的输出是 Agent 的「第一手状态」，误报比不报更贵。
- **假 VRP 不是真机**：`test_offline.FakeVRP` 只模拟协议怪癖，不模拟业务语义；
  真机上的 `display` 输出格式永远比假的更脏，新断言先在真机跑一次再收进测试。