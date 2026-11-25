#!/usr/bin/env python3
"""一条命令把环境查清楚，替代"边猜边试"的十几轮工具调用。

输出分 5 块：
  1) eNSP 进程 / 版本 / 是否可用 GUI 注入输入（决定"能不能自己点按钮"）
  2) 当前有哪些 console 口在线（工程是否已启动）
  3) 每个在线设备的 提示符 / 主机名 / 视图 / 分页支持 / 接口形态
  4) eNSP 日志里今天的错误行（过滤掉每次启动都会有的噪声）
  5) 一句话结论 + 建议下一步

用法:
    python preflight.py                  # 全查
    python preflight.py --no-gui         # 跳过窗口/输入注入检查（非 Windows 或远程）
    python preflight.py --json           # 机读输出
"""
import argparse
import datetime
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
try:                      # 控制台是 GBK 时，别让某个符号把整个体检打崩
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from vrp_console import VRPConsole, VRPError  # noqa: E402
except Exception:                                # pragma: no cover
    VRPConsole = None
    VRPError = Exception

CONSOLE_MIN, CONSOLE_MAX = 2000, 2010
BENIGN_LOG = (
    "Failed to upload resource cfg file",
    "Failed to put vrpcfg.zip",
    "Avaliable bytes can be read from pipe",
    "CInterface::WorkerService - Failed to enumerate interface event",
    "CAgent::WaitMe - Failed to wait for processes to exit",
    "CTunnel::ClientService - Failed to receive from client socket",
    "CTunnel::DeviceService - Failed to receive from device socket",
)


# ---------------------------------------------------------------- 1. eNSP 本体
def find_ensp_dir():
    cands = [
        os.environ.get("ENSP_HOME", ""),
        r"D:\ENSP\eNSP",
        r"C:\Program Files\Huawei\eNSP",
        r"C:\Program Files (x86)\Huawei\eNSP",
    ]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "eNSP_Client.exe")):
            return c
    return None


def ensp_processes():
    """(pid, exe) 列表；不依赖 psutil。"""
    out = []
    if os.name != "nt":
        return out
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/FI", "IMAGENAME eq eNSP_Client.exe"],
                           capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines()[1:]:
            parts = [p.strip('"') for p in line.split('","')]
            if parts and parts[0].lower().startswith("ensp_client"):
                out.append((parts[1], parts[0]))
    except Exception:
        pass
    return out


def ensp_version(ensp_dir):
    if not ensp_dir or os.name != "nt":
        return None
    exe = os.path.join(ensp_dir, "eNSP_Client.exe")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Item '%s').VersionInfo.FileVersion" % exe],
            capture_output=True, text=True, timeout=20)
        v = r.stdout.strip()
        return v or None
    except Exception:
        return None


def can_inject_input():
    """真·键盘鼠标注入能力测试：SetCursorPos 成功与否。

    eNSP 以管理员身份运行时，普通权限进程的注入会被系统拦掉——
    这条信息决定"agent 能不能自己点。现在结论通常是：不能，必须让用户点。
    """
    if os.name != "nt":
        return None, "非 Windows"
    try:
        import ctypes
        ok = ctypes.windll.user32.SetCursorPos(10, 10)
        return bool(ok), "SetCursorPos 成功" if ok else "SetCursorPos 被拒（权限/沙箱限制）"
    except Exception as exc:
        return None, str(exc)


def gui_cli_windows():
    """eNSP 里打开着的设备 CLI 窗口标题（这些窗口和 TCP console 抢同一个控制台）。"""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        titles = []
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value
                if re.match(r"^(PC\d+|AR\d+|LSW\d+|S\d+|R\d+)$", t):
                    titles.append(t)
            return True

        ctypes.windll.user32.EnumWindows(EnumProc(cb), 0)
        return sorted(set(titles))
    except Exception:
        return []


def log_errors():
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "eNSP", "Log")
    today = datetime.date.today().strftime("%Y-%m-%d")
    hits = []
    for kind in ("VBoxServer", "Conserver"):
        path = os.path.join(base, "%s_%s.log" % (kind, today))
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-400:]
        except OSError:
            continue
        for ln in lines:
            if "[ERROR]" not in ln:
                continue
            if any(b in ln for b in BENIGN_LOG):
                continue
            hits.append("%s: %s" % (kind, ln.strip()))
    return hits[-8:]


# ------------------------------------------------------- 2/3. console 口与设备
def open_ports():
    res = []
    for p in range(CONSOLE_MIN, CONSOLE_MAX + 1):
        s = socket.socket()
        s.settimeout(0.35)
        try:
            s.connect(("127.0.0.1", p))
            res.append(p)
        except OSError:
            pass
        finally:
            s.close()
    return res


def probe_device(port, timeout=25):
    info = {"port": port}
    if VRPConsole is None:
        info["error"] = "vrp_console 不可用"
        return info
    con = VRPConsole(port)
    try:
        con.wait_ready(timeout=timeout)
        info["prompt"] = con.prompt()
        info["hostname"] = con.hostname()
        info["view"] = "user" if (info["prompt"] or "").endswith(">") else "config"
        info["paging_off"] = con.disable_paging()
        con.quiet_logs()
        brief = con.cmd("display interface brief", timeout=30, allow_error=True)
        info["ethernet_up"] = len(re.findall(r"^Ethernet\S+\s+up", brief, re.M))
        info["ge_up"] = len(re.findall(r"^GigabitEthernet\S+\s+up", brief, re.M))
        info["iface_kind"] = "switch" if "Ethernet" in brief else "router"
        cfg = con.cmd("display current-configuration", timeout=30, allow_error=True)
        # 默认配置里也有 "sysname HUAWEI"，所以不能只看关键字：主机名还是 HUAWEI、
        # 且没有任何 vlan/路由/接口配置，才算"没配过"
        info["configured"] = bool(
            (info["hostname"] or "").lower() not in ("huawei", "")
            or re.search(r"vlan batch|ip route-static|ospf \d", cfg))
    except Exception as exc:
        info["error"] = str(exc)
    finally:
        con.close()
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description="eNSP 环境一条命令体检")
    ap.add_argument("--no-gui", action="store_true", help="跳过窗口/输入注入检查")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--device-timeout", type=int, default=25)
    args = ap.parse_args(argv)

    ensp_dir = find_ensp_dir()
    procs = ensp_processes()
    report = {
        "ensp_dir": ensp_dir,
        "ensp_version": ensp_version(ensp_dir),
        "ensp_pids": [p[0] for p in procs],
        "ensp_running": bool(procs),
        "input_injection": None,
        "input_note": "",
        "gui_cli_windows": [],
        "console_ports": [],
        "devices": [],
        "log_errors": log_errors(),
        "advice": [],
    }
    if not args.no_gui:
        ok, note = can_inject_input()
        report["input_injection"] = ok
        report["input_note"] = note
        report["gui_cli_windows"] = gui_cli_windows()

    ports = open_ports()
    report["console_ports"] = ports
    for p in ports:
        report["devices"].append(probe_device(p, timeout=args.device_timeout))

    # ---- 结论与建议
    adv = report["advice"]
    if not report["ensp_running"]:
        adv.append("eNSP 没在运行：让用户启动 eNSP（或你起，但注入输入通常不可用）")
    if report["input_injection"] is False:
        adv.append("无法注入鼠标键盘：所有 GUI 动作（打开工程/启动设备/双击进 CLI）都必须让用户做")
    if report["gui_cli_windows"]:
        adv.append("GUI 里开着 CLI 窗口 %s：它们和 TCP console 抢同一个控制台，"
                   "会让命令被吃掉/串台，配置前请用户关掉" % ",".join(report["gui_cli_windows"]))
    if not ports:
        adv.append("没有 console 口在线：工程未打开或设备未启动；让用户在 eNSP 里"
                   "【文件→打开】工程后框选设备点绿色三角（双击 .topo 不会在已运行的 eNSP 里打开）")
    else:
        for d in report["devices"]:
            if d.get("error"):
                adv.append("console %d 连上了但没就绪（%s）：可能还在引导，等 30~60s 再看"
                           % (d["port"], d["error"][:60]))
            elif d.get("view") == "config":
                adv.append("console %d (%s) 停在配置视图：下发前先 return_to_user_view()"
                           % (d["port"], d.get("hostname")))
    if not adv:
        adv.append("环境就绪：可以直接 scan → 配置 → save → 验证")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    def line(k, v):
        print("  %-16s %s" % (k, v))

    print("=" * 64)
    print("eNSP 环境体检")
    print("=" * 64)
    line("eNSP 目录", ensp_dir or "未找到")
    line("eNSP 版本", report["ensp_version"] or "未取到")
    line("进程", ",".join(report["ensp_pids"]) or "未运行")
    if not args.no_gui:
        line("可注入输入", {True: "可以", False: "不行（必须让用户点）", None: "未测到"}[
            report["input_injection"]] + "  " + report["input_note"])
        line("GUI CLI 窗口", ",".join(report["gui_cli_windows"]) or "无")
    line("在线 console", ",".join(map(str, ports)) or "无")
    for d in report["devices"]:
        if d.get("error"):
            print("  - %d: 未就绪 %s" % (d["port"], d["error"][:80]))
        else:
            print("  - %d: 提示符=%s 主机名=%s 视图=%s 分页可关=%s 接口形态=%s"
                  " up(Eth/GE)=%d/%d 已配置=%s"
                  % (d["port"], d.get("prompt"), d.get("hostname"), d.get("view"),
                     d.get("paging_off"), d.get("iface_kind"),
                     d.get("ethernet_up", 0), d.get("ge_up", 0), d.get("configured")))
    print("  日志异常行:")
    if report["log_errors"]:
        for e in report["log_errors"]:
            print("    " + e)
    else:
        print("    （无；'Failed to upload resource cfg file' 之类是每次启动的噪声，已过滤）")
    print("\n结论 / 下一步:")
    for a in adv:
        print("  * " + a)
    return 0


if __name__ == "__main__":
    sys.exit(main())