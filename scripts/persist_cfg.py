#!/usr/bin/env python3
"""把设备里已经配好的 running-config 导出成 <GUID>/vrpcfg.zip —— 让配置能扛住"重开工程"。

为什么需要它：`save` 只写进设备自己的 flash；在 eNSP 里关工程/重开时配置**不一定**
被写回工程目录（实测关掉再开、设备回默认，所有 VLAN 消失）。而 eNSP 启动设备时
有一步「把工程目录里的 vrpcfg.zip 装进设备」，缺文件就在 Conserver 日志里报
"Failed to put vrpcfg.zip"。把 current-configuration 塞进这个 zip 里就能补上这一步。

用法：
    python persist_cfg.py <topo>                     # 按 topo 里的 com_port 自动配对
    python persist_cfg.py <topo> --map 2000:LSW1,2001:LSW2
    python persist_cfg.py <topo> --dry-run           # 只看会写什么
"""
import argparse
import io
import os
import re
import sys
import zipfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from vrp_console import VRPConsole, VRPError
except Exception:  # pragma: no cover
    VRPConsole = None
    VRPError = Exception


def load_topo(path):
    """{name: (guid, com_port)} —— 只依赖 <dev ...> 那一行。"""
    raw = io.open(path, encoding="gbk", errors="replace").read()
    out = {}
    for m in re.finditer(r'<dev id="([^"]+)" name="([^"]+)"[^>]*com_port="(\d+)"', raw):
        out[m.group(2)] = (m.group(1), int(m.group(3)))
    return out


def clean_cfg(text):
    """从 display current-configuration 的回显里去掉提示符/分页符，得到 vrpcfg.cfg。"""
    text = re.sub(r"----\s*More\s*----", "", text)
    lines = text.replace("\r\n", "\n").split("\n")
    out, started = [], False
    for ln in lines:
        s = ln.rstrip()
        if not started:
            if s.startswith("#"):
                started = True
            else:
                continue
        if re.match(r"^[<\[][^\s<\[\]]+[>\]]\s*$", s):
            continue
        out.append(s)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def identify(port):
    """连上去读主机名；连不上返回 None。"""
    if VRPConsole is None:
        return None
    con = VRPConsole(port)
    try:
        con.wait_ready(timeout=25)
        return con.hostname()
    except Exception:
        return None
    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="导出设备配置为 vrpcfg.zip")
    ap.add_argument("topo", help="工程 .topo 路径")
    ap.add_argument("--map", default="", help="强制端口映射，如 2000:LSW1,2001:LSW2")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args(argv)

    devices = load_topo(args.topo)
    folder = os.path.dirname(os.path.abspath(args.topo))
    pairs = []
    if args.map:
        for item in args.map.split(","):
            port, name = item.split(":")
            pairs.append((int(port), name.strip()))
    else:
        for name, (guid, port) in devices.items():
            if port:
                pairs.append((port, name))

    ok = 0
    for port, guessed in sorted(pairs):
        real = identify(port)
        name = real if real in devices else guessed
        if name not in devices:
            print("[skip] console %d 主机名 %r 与工程里的设备名都对不上" % (port, real))
            continue
        guid, _ = devices[name]
        con = VRPConsole(port)
        try:
            con.wait_ready(timeout=25)
            con.return_to_user_view()
            raw = con.cmd("display current-configuration", timeout=60)
        finally:
            con.close()
        cfg = clean_cfg(raw)
        if "sysname" not in cfg:
            print("[warn] %s 抓到的配置里没有 sysname，可能没抓全，跳过" % name)
            continue
        target = os.path.join(folder, guid)
        zip_path = os.path.join(target, "vrpcfg.zip")
        n = len(cfg.splitlines())
        if args.dry_run:
            print("[dry] %s (console %d) -> %s  %d 行" % (name, port, zip_path, n))
            continue
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("vrpcfg.cfg", cfg)
        print("[ok] %s (console %d) -> %s  %d 行 / %d 字节"
              % (name, port, os.path.relpath(zip_path, folder), n, os.path.getsize(zip_path)))
        ok += 1
    print("\n完成：%d 台" % ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())