#!/usr/bin/env python3
"""按 JSON 计划批量下发配置 + 断言 + 出报告：一遍到位，成功与否有据可查。

为什么不用「一条条 cmd」：每行都要等一次空闲超时（≈0.5s），40 行就 20s+；
而 paste 一把写完再读只要 1~2s。整段 paste 的代价是报错时定位不到具体行 ——
`_clean_bulk()` 已经把回显剥掉了，剩下的只有输出。

**这里刻意不做「失败行重放」**：重放会把已经成功执行过的非幂等命令再跑一遍
（成对的 `undo` / `vlan batch` / 先 undo 再配的写法），最终状态取决于重放顺序，
不可预测；比"少一条定位信息"贵得多。报错行原样进报告（标 `(整段下发)`），
要精确到行就把那一段拆小重发。

计划文件格式：
{
  "devices": [
    {"name": "LSW1",                 # 与设备提示符里的主机名一致（可省略 port）
     "port": 2000,                   # 可选：省掉按主机名扫描；给了就跳过扫描
     "lines": ["system-view", "sysname LSW1", "vlan batch 10 20 30", "..."],
     "verify": ["display vlan"],     # 可选：把原始输出写进报告，不做判定
     "expect": [                     # 可选：真正判定通过/不通过
       {"name": "VLAN10 存在", "cmd": "display vlan", "contains": "10"},
       {"name": "没有 Error 行", "cmd": "display vlan", "not_contains": "Error:"},
       {"name": "有 OSPF 邻居", "cmd": "display ospf peer brief", "regex": "Full"},
       {"name": "PC1 通", "ping": {"target": "192.168.10.10", "count": 4, "max_loss": 0}}
     ]}
  ],
  "save": true
}

`expect` 的判定方式（四选一）：
  contains / not_contains / regex —— 对 `cmd` 的输出做文本匹配
  ping —— 目标 + 可选 source/count/max_loss，丢包率不超过 max_loss 才算过

用法：
    python apply_plan.py plan.json                  # 执行并打印报告
    python apply_plan.py --spec lab.json            # 直接用 lab.json 的 config（推荐）
    python apply_plan.py plan.json --dry-run        # 只打印要发什么、要断言什么
    python apply_plan.py plan.json --report r.txt   # 文本报告另存
    python apply_plan.py plan.json --json r.json    # 结构化报告（机读/回归对比）
    python apply_plan.py plan.json --md r.md        # Markdown 报告（贴进实验报告）

退出码：0 全部通过；1 有设备/断言失败；2 环境或计划有问题。
"""
import argparse
import io
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from vrp_console import VRPConsole, VRPError, scan_ports  # noqa: E402

ERROR_RE = re.compile(r"(Error:|Unrecognized command|Incomplete command|"
                      r"Wrong parameter|Too many parameters|Ambiguous command)", re.I)


# ---------------------------------------------------------------- 通道定位
def find_port(name, ports):
    """按主机名找端口；找不到再看提示符。"""
    for p in ports:
        con = VRPConsole(p)
        try:
            con.wait_ready(timeout=15)
            if (con.hostname() or "").lower() == name.lower():
                return p
        except Exception:
            pass
        finally:
            con.close()
    return None


# ---------------------------------------------------------------- 断言
def _expect_label(exp):
    if "ping" in exp:
        return "ping %s" % (exp["ping"].get("target") or "?")
    return str(exp.get("name") or exp.get("cmd") or "check")


def run_expect(con, expects):
    """逐条跑 expect，返回 [{name, ok, detail}]；断言失败不抛异常。"""
    results = []
    for exp in expects or []:
        name = _expect_label(exp)
        try:
            if "ping" in exp:
                p = exp.get("ping") or {}
                target = p.get("target")
                if not target:
                    results.append({"name": name, "ok": False,
                                    "detail": "ping 断言缺少 target"})
                    continue
                ok, loss, rtt = con.ping(str(target), source=p.get("source"),
                                         count=int(p.get("count", 4)))
                max_loss = float(p.get("max_loss", 0))
                passed = bool(ok) and loss <= max_loss
                detail = "丢包 %s%%，rtt %s" % (loss, rtt)
            else:
                cmd = exp.get("cmd")
                if not cmd:
                    results.append({"name": name, "ok": False,
                                    "detail": "expect 缺少 cmd 或 ping"})
                    continue
                out = con.cmd(cmd, timeout=float(exp.get("timeout", 60)))
                if "contains" in exp:
                    passed = str(exp["contains"]) in out
                    detail = "" if passed else "输出里没有 %r" % exp["contains"]
                elif "not_contains" in exp:
                    passed = str(exp["not_contains"]) not in out
                    detail = "" if passed else "输出里出现了 %r" % exp["not_contains"]
                elif "regex" in exp:
                    passed = re.search(exp["regex"], out, re.I | re.M) is not None
                    detail = "" if passed else "输出不匹配 /%s/" % exp["regex"]
                else:
                    passed, detail = False, "expect 需要 contains/not_contains/regex/ping"
                if not passed:
                    first = (out.strip().splitlines() or ["(空输出)"])[0]
                    detail += " | 输出首行: %s" % first
        except Exception as exc:  # 断言本身出问题（缺字段、类型错）也不能崩掉整轮
            passed, detail = False, "命令失败: %s: %s" % (type(exc).__name__, exc)
        results.append({"name": name, "ok": passed, "detail": "" if passed else detail})
    return results


# ---------------------------------------------------------------- 单台
def apply_one(item, ports, save=True, dry=False, allow_name_mismatch=False):
    """下发一台设备，返回结构化结果（不抛异常，失败写进结果里）。"""
    name = item.get("name") or "(unnamed)"
    result = {
        "device": name, "port": item.get("port"), "ok": False, "dry_run": dry,
        "lines": 0, "error_lines": [], "save": None, "expects": [], "verify": [],
        "notes": [],
    }
    port = item.get("port") or find_port(name, ports)
    if not port:
        result["notes"].append("找不到对应的 console 口（设备没启动或名字不对）")
        return result
    result["port"] = port

    lines = [str(l).rstrip() for l in item.get("lines", []) if str(l).strip()]
    result["lines"] = len(lines)
    expects = item.get("expect", [])

    if dry:
        result["ok"] = True
        result["expects"] = [{"name": _expect_label(e), "ok": None, "detail": "dry-run"}
                             for e in expects]
        return result

    con = VRPConsole(port)
    try:
        con.wait_ready(timeout=180)
        seen = con.hostname()
        if seen and seen.lower() != name.lower():
            # 默认严格：主机名对不上就不碰这台设备。多工程并行（--base-port 分段）
            # 或 eNSP 复用端口时，继续下发等于把 A 实验的配置写进 B 实验并 save。
            result["notes"].append(
                "提示符主机名 %r 与计划名 %r 不一致：已跳过该设备，未下发、未 save"
                "（多半是端口串台；确认无误确实要强推请加 --allow-name-mismatch）"
                % (seen, name))
            if not allow_name_mismatch:
                return result
            result["notes"].append("--allow-name-mismatch 已开启，按计划名继续下发")
        con.return_to_user_view()
        con.quiet_logs()
        con.disable_paging()

        # 一段发完；只看这段回显里有没有报错行。
        # 注意：这里绝不逐行重放 —— 重放会把非幂等命令（成对 undo / vlan batch /
        # 先 undo 再配）再执行一遍。宁可丢失"哪一行错"的定位，也不动第二次设备。
        body = con.paste(lines, timeout=120, allow_error=True)
        bad = [l.strip() for l in body.splitlines() if ERROR_RE.search(l)]
        if bad:
            result["notes"].append(
                "整段下发有 %d 行报错；未重放任何命令（重放会重复执行非幂等命令）"
                "，报错原文见下面的 [FAIL] 行" % len(bad))
            for err in bad:
                result["error_lines"].append({"line": "(整段下发)",
                                              "error": err[:200]})

        con.return_to_user_view()
        if save:
            result["save"] = con.save(timeout=90)

        result["expects"] = run_expect(con, expects)
        for cmd in item.get("verify", []):
            try:
                result["verify"].append({"cmd": cmd, "output": con.cmd(cmd, timeout=90)})
            except VRPError as exc:
                result["verify"].append({"cmd": cmd, "output": "<失败: %s>" % exc})

        result["ok"] = (not result["error_lines"]
                        and all(c["ok"] for c in result["expects"])
                        and result["save"] is not False)
    finally:
        con.close()
    return result


# ---------------------------------------------------------------- 报告
def _save_text(save):
    if save is None:
        return "跳过"
    return "成功" if save else "失败"


def device_text(r):
    """一台设备在控制台/文本报告里的一段。"""
    out = ["### %s (console %s)%s" % (r["device"], r["port"],
                                      "  [dry-run]" if r["dry_run"] else "")]
    for n in r["notes"]:
        out.append("    注意: %s" % n)
    if r["dry_run"]:
        out.append("    将下发 %d 行，%d 条断言" % (r["lines"], len(r["expects"])))
        for e in r["expects"]:
            out.append("    断言: %s" % e["name"])
        return out
    out.append("    下发 %d 行，报错行 %d" % (r["lines"], len(r["error_lines"])))
    for e in r["error_lines"]:
        out.append("    [FAIL] %-42s %s" % (str(e["line"])[:42], e["error"]))
    out.append("    save: %s" % _save_text(r["save"]))
    for e in r["expects"]:
        out.append("    [%s] %s%s" % ("OK" if e["ok"] else "FAIL", e["name"],
                                     "" if e["ok"] else "  (%s)" % e["detail"]))
    for v in r["verify"]:
        out.append("\n----- %s : %s -----\n%s"
                   % (r["device"], v["cmd"], v["output"].strip()))
    return out


def render_text(results, plan_path):
    head = ["=" * 64, "eNSP 配置报告", "=" * 64,
            "计划: %s" % plan_path,
            "时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"), ""]
    for r in results:
        head.extend(device_text(r))
        head.append("")
    passed = sum(1 for r in results if r["ok"])
    head.append("结论: %d/%d 台通过%s" % (passed, len(results),
                                      "" if passed == len(results) else "，看上面的 FAIL"))
    return "\n".join(head)


def render_md(results, plan_path):
    passed = sum(1 for r in results if r["ok"])
    out = ["# eNSP 配置报告", "",
           "- 计划: `%s`" % plan_path,
           "- 时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
           "- 结论: **%d/%d 台通过**" % (passed, len(results)), "",
           "| 设备 | console | 下发行 | 报错行 | save | 断言 |",
           "|---|---|---|---|---|---|"]
    for r in results:
        ok_checks = sum(1 for e in r["expects"] if e["ok"])
        out.append("| %s | %s | %d | %d | %s | %d/%d |"
                   % (r["device"], r["port"], r["lines"], len(r["error_lines"]),
                      _save_text(r["save"]), ok_checks, len(r["expects"])))
    for r in results:
        out += ["", "## %s (console %s)" % (r["device"], r["port"])]
        if r["dry_run"]:
            out.append("dry-run：将下发 %d 行，断言 %d 条。" % (r["lines"], len(r["expects"])))
            continue
        if r["expects"]:
            out += ["", "| 断言 | 结果 | 详情 |", "|---|---|---|"]
            for e in r["expects"]:
                out.append("| %s | %s | %s |" % (e["name"], "✅" if e["ok"] else "❌",
                                                 e["detail"] or ""))
        if r["error_lines"]:
            out += ["", "**下发报错**", ""]
            for e in r["error_lines"]:
                out.append("- `%s` → %s" % (e["line"], e["error"]))
        for v in r["verify"]:
            out += ["", "<details><summary>%s</summary>" % v["cmd"], "",
                    "```", v["output"].strip()[:2000], "```", "", "</details>"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 入口
def _write_report(path, content, label):
    """写一份报告；失败要报出来，不能像以前那样打印"已写入"其实没写。"""
    try:
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        print("写不了%s %s: %s" % (label, path, exc), file=sys.stderr)
        return False
    print("%s已写入 %s" % (label, path))
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="按计划批量下发配置 + 断言 + 报告")
    ap.add_argument("plan", nargs="?", default="", help="计划 JSON（或用 --spec lab.json）")
    ap.add_argument("--spec", default="", help="lab.json：直接用它里面的 config 编译计划")
    ap.add_argument("--report", default="", help="文本报告输出文件")
    ap.add_argument("--json", default="", help="结构化报告 (JSON) 输出文件")
    ap.add_argument("--md", default="", help="Markdown 报告输出文件")
    ap.add_argument("--dry-run", action="store_true", help="只打印要下发/断言的内容")
    ap.add_argument("--no-save", action="store_true", help="下发后不 save")
    ap.add_argument("--allow-name-mismatch", action="store_true",
                    help="提示符主机名与计划名不一致时仍然下发（默认跳过该设备）")
    args = ap.parse_args(argv)

    if args.spec:
        try:
            import lab_spec
            plan = lab_spec.plan(lab_spec.load(args.spec))
        except (OSError, ValueError) as exc:
            print("读不了 lab.json %s: %s" % (args.spec, exc))
            return 2
        plan_label = args.spec
    else:
        if not args.plan:
            print("需要 plan.json 或 --spec lab.json")
            return 2
        try:
            with io.open(args.plan, encoding="utf-8") as fh:
                plan = json.load(fh)
        except (OSError, ValueError) as exc:
            print("读不了计划文件 %s: %s" % (args.plan, exc))
            return 2
        plan_label = args.plan
    devices = plan.get("devices") or []
    if not devices:
        print("计划里没有 devices。")
        return 2

    # 计划静态校验：ping 断言缺 target 属于"计划有问题"，先拦在动设备之前。
    # 以前会走到 run_expect 里抛 KeyError，整轮崩掉并且一份报告都不写。
    broken = []
    for item in devices:
        for exp in item.get("expect") or []:
            if "ping" in exp and not (exp.get("ping") or {}).get("target"):
                broken.append("%s: %s" % (item.get("name", "?"), _expect_label(exp)))
    if broken:
        print("计划里的 ping 断言缺少 target：")
        for b in broken:
            print("  " + b)
        return 2

    save = plan.get("save", True) and not args.no_save
    explicit = all(d.get("port") for d in devices)
    ports = [] if explicit else scan_ports()
    if explicit:
        print("计划里都指定了 console 端口，跳过扫描")
    elif not ports:
        print("没有在线的 console 口：先在 eNSP 里打开工程并启动设备。")
        return 2
    else:
        print("在线 console: %s" % ports)

    results = []
    for item in devices:
        try:
            r = apply_one(item, ports, save=save, dry=args.dry_run,
                          allow_name_mismatch=args.allow_name_mismatch)
        except Exception as exc:  # 单台崩掉不影响其它设备，也不能让报告丢失
            r = {"device": item.get("name") or "(unnamed)", "port": item.get("port"),
                 "ok": False, "dry_run": args.dry_run, "lines": 0, "error_lines": [],
                 "save": None, "expects": [], "verify": [],
                 "notes": ["执行时抛出异常: %s: %s" % (type(exc).__name__, exc)]}
        results.append(r)
        print("\n".join(device_text(r)))
        print("")
        time.sleep(0.2)

    text = render_text(results, plan_label)
    write_failed = False
    if args.report:
        write_failed |= not _write_report(args.report, text + "\n", "文本报告")
    if args.json:
        write_failed |= not _write_report(
            args.json,
            json.dumps({"plan": plan_label, "results": results,
                        "ok": all(r["ok"] for r in results)},
                       ensure_ascii=False, indent=2) + "\n",
            "JSON 报告")
    if args.md:
        write_failed |= not _write_report(args.md, render_md(results, plan_label),
                                          "Markdown 报告")

    all_ok = all(r["ok"] for r in results)
    passed = sum(1 for r in results if r["ok"])
    print("结论: %d/%d 台通过%s" % (passed, len(results),
                                  "" if all_ok else "，看上面的 FAIL"))
    if write_failed:
        return 2  # 报告没落盘 = 证据丢了，不能报"成功"
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())