#!/usr/bin/env python3
"""Generate an eNSP .topo project from three declarative tables.

Edit DEVICES, LINKS and PC_SETTINGS below (or import build_project() from your
own script), then run:

    python make_topo.py <outdir> [--name lab]

The generator only lays out the topology -- it writes no device configuration,
because eNSP ignores a vrpcfg.zip placed by hand. Configure over the console
after the user starts the devices (see the skill's workflow).

Interface-name-to-index conversion is the part that silently breaks eNSP
projects when hand-written: 'GigabitEthernet0/0/2' must map to index 2, and
'GE' is not a prefix of 'GigabitEthernet'. All conversions go through
parse_topo.interface_index(), which reads the real slot17 declaration.

Standard library only.
"""

import argparse
import hashlib
import os
import re
import sys
import uuid
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parse_topo import DEFAULT_SLOTS, interface_index  # noqa: E402

# ---------------------------------------------------------------------------
# EDIT HERE
# ---------------------------------------------------------------------------

DEVICES = [
    # (name, model, x, y)
    ("AR1", "AR2220", 200, 300),
    ("AR2", "AR2220", 700, 300),
    ("LSW1", "S3700", 450, 450),
    ("PC1", "PC", 150, 450),
    ("PC2", "PC", 750, 450),
]

# (device_a, interface_a, device_b, interface_b, media)
#
# `media` is "Copper" (default twisted pair) or "Serial" (needs a serial card,
# which is requested automatically by placing any Serial link on that device).
LINKS = [
    ("AR1", "GigabitEthernet0/0/0", "AR2", "GigabitEthernet0/0/0", "Copper"),
    ("AR1", "GigabitEthernet0/0/1", "LSW1", "GigabitEthernet0/0/1", "Copper"),
    ("LSW1", "Ethernet0/0/1", "PC1", "Ethernet0/0/1", "Copper"),
    ("LSW1", "Ethernet0/0/2", "PC2", "Ethernet0/0/1", "Copper"),
]

# PC addressing is embedded in the .topo, so a PC needs no manual setup.
PC_SETTINGS = {
    "PC1": {"ip": "192.168.10.10", "mask": "255.255.255.0", "gateway": "192.168.10.1"},
    "PC2": {"ip": "192.168.20.10", "mask": "255.255.255.0", "gateway": "192.168.20.1"},
}

# Drawing labels (the little text notes eNSP shows on the canvas).
LABELS = [
    ("192.168.10.0/24", 100, 120),
    ("192.168.20.0/24", 700, 120),
]


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

TOPO_HEADER = '<?xml version="1.0" encoding="UNICODE" ?>\r\n<topo version="1.3.00.100">\r\n'

PC_SETTINGS_TEMPLATE = (
    " -simpc_ip {ip}  -simpc_mask {mask}  -simpc_gateway {gateway}"
    "  -simpc_mac {mac}  -simpc_mc_dstip 0.0.0.0  -simpc_mc_dstmac 00-00-00-00-00-00"
    "  -simpc_dns1 0.0.0.0  -simpc_dns2 0.0.0.0  -simpc_ipv6 ::  -simpc_prefix 128"
    "  -simpc_gatewayv6 ::  -simpc_dhcp_state 0  -simpc_dhcpv6_state 0"
    "  -simpc_dns_auto_state 0  -simpc_igmp_version 1"
    "  -simpc_group_ip_start 0.0.0.0  -simpc_src_ip_start 0.0.0.0"
    "  -simpc_group_num 0  -simpc_group_step 0  -simpc_src_num 0  -simpc_src_step 0"
    "  -simpc_type MODE_IS_INCLUDE "
)


def _mac(seed, kind):
    """Deterministic system_mac, ALWAYS 6 octets.

    必须是 6 段：eNSP 会校验 system_mac，写错（例如早期版本拼成
    00-E0-FC-aa-bb-cc-dd 这种 7 段）时它**不报错、直接把该 <dev> 丢掉**，
    打开工程就是空画布、状态栏「总数：0」。
    """
    digest = hashlib.md5(("mac:" + str(seed)).encode("utf-8")).hexdigest().upper()
    prefix = "54-89-98" if kind.upper() == "PC" else "00-E0-FC"
    return "-".join([prefix, digest[0:2], digest[2:4], digest[4:6]])


def _stable_id(name):
    """Same device name always yields the same GUID, so regenerating an
    existing project keeps GUID folders stable instead of orphaning them."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "ensp-lab:" + name)).upper()


def _uses_serial(name):
    return any(
        a == name and m.lower() == "serial" or b == name and m.lower() == "serial"
        for a, _, b, _, m in LINKS
    )


def _dev_skeleton(name, model):
    """A tiny <dev> element used only for interface-name -> index conversion."""
    el = ET.Element("dev")
    el.set("name", name)
    el.set("model", model)
    slot = ET.SubElement(el, "slot")
    slot.set("number", "slot17")
    slot.set("isMainBoard", "1")
    for sztype, ifname, count in DEFAULT_SLOTS.get(model, []):
        itf = ET.SubElement(slot, "interface")
        itf.set("sztype", sztype)
        itf.set("interfacename", ifname)
        itf.set("count", str(count))
    if _uses_serial(name) and model.upper().startswith("AR22"):
        itf = ET.SubElement(slot, "interface")
        itf.set("sztype", "Serial")
        itf.set("interfacename", "Serial")
        itf.set("count", "2")
        card = ET.SubElement(el, "slot")
        card.set("isMainBoard", "0")
        card.set("id", "0")
        card.set("type", "521")
    return el


def _render_dev(name, model, x, y, port):
    did = _stable_id(name)
    el = _dev_skeleton(name, model)
    settings = ""
    mac = _mac(name, model)
    if model.upper() == "PC":
        pc = PC_SETTINGS.get(name, {})
        settings = PC_SETTINGS_TEMPLATE.format(
            ip=pc.get("ip", "0.0.0.0"),
            mask=pc.get("mask", "0.0.0.0"),
            gateway=pc.get("gateway", "0.0.0.0"),
            mac=mac,
        )
    lines = [
        '        <dev id="%s" name="%s" poe="0" model="%s" settings="%s"'
        ' system_mac="%s" com_port="%d" bootmode="0" cx="%.6f" cy="%.6f"'
        ' edit_left="%d" edit_top="%d">'
        % (did, name, model, settings, mac, port, x, y, x + 27, y + 54)
    ]
    main = el.find("slot")
    lines.append('            <slot number="slot17" isMainBoard="1">')
    for itf in main.findall("interface"):
        lines.append('                <interface sztype="%s" interfacename="%s" count="%s" />'
                     % (itf.get("sztype"), itf.get("interfacename"), itf.get("count")))
    lines.append("            </slot>")
    for slot in el.findall("slot"):
        if slot.get("isMainBoard") == "0":
            lines.append('            <slot isMainBoard="0" id="%s" type="%s" />'
                         % (slot.get("id"), slot.get("type")))
    lines.append("        </dev>")
    return "\n".join(lines)


def _render_link(dev_a, itf_a, dev_b, itf_b, media, skel):
    ia = interface_index(skel[dev_a], itf_a)
    ib = interface_index(skel[dev_b], itf_b)
    if media.lower() == "serial":
        # Serial endpoints in real eNSP saves carry a moved bound rect and
        # split offsets: src and tar each keep their own, never one copy.
        body = ('            <interfacePair lineName="Serial" srcIndex="%d"'
                ' srcBoundRectIsMoved="1" srcBoundRect_X="0.000000" srcBoundRect_Y="0.000000"'
                ' srcOffset_X="-14.000000" srcOffset_Y="-20.000000"'
                ' tarIndex="%d" tarBoundRectIsMoved="1" tarBoundRect_X="0.000000"'
                ' tarBoundRect_Y="0.000000" tarOffset_X="-39.000000" tarOffset_Y="8.000000" />'
                % (ia, ib))
    else:
        body = ('            <interfacePair lineName="Copper" srcIndex="%d"'
                ' srcBoundRectIsMoved="0" srcBoundRect_X="0.000000" srcBoundRect_Y="0.000000"'
                ' srcOffset_X="0.000000" srcOffset_Y="0.000000"'
                ' tarIndex="%d" tarBoundRectIsMoved="0" tarBoundRect_X="0.000000"'
                ' tarBoundRect_Y="0.000000" tarOffset_X="0.000000" tarOffset_Y="0.000000" />'
                % (ia, ib))
    return ('        <line srcDeviceID="%s" destDeviceID="%s">\n%s\n        </line>'
            % (_stable_id(dev_a), _stable_id(dev_b), body))


def _validate():
    names = [d[0] for d in DEVICES]
    if len(names) != len(set(names)):
        raise ValueError("duplicate device names in DEVICES")
    unknown = {n for n, *_ in LINKS if n not in names} | {b for _, _, b, _, _ in LINKS if b not in names}
    if unknown:
        raise ValueError("LINKS reference unknown devices: %s" % ", ".join(sorted(unknown)))
    if not any(m["model"].upper() == "PC" for m in [{"model": d[1]} for d in DEVICES]):
        pass  # a PC-less lab is legitimate
    # Roles: only routers/switches get a console port.
    console_devs = [d[0] for d in DEVICES if d[1].upper() != "PC"]
    if len(console_devs) > 11:
        raise ValueError(
            "%d console devices exceed the 2000-2010 range eNSP conventionally uses; "
            "split the lab" % len(console_devs)
        )


def build_text(base_port=2000):
    _validate()
    skel = {name: _dev_skeleton(name, model) for name, model, _, _ in DEVICES}
    parts = [TOPO_HEADER, "    <devices>\n"]
    port = int(base_port)
    for name, model, x, y in DEVICES:
        if model.upper() != "PC":
            assigned, port = port, port + 1
        else:
            assigned = 0
        parts.append(_render_dev(name, model, x, y, assigned) + "\n")
    parts.append("    </devices>\n    <lines>\n")
    for dev_a, itf_a, dev_b, itf_b, media in LINKS:
        parts.append(_render_link(dev_a, itf_a, dev_b, itf_b, media, skel) + "\n")
    parts.append("    </lines>\n")
    # 顶层必须是 devices / lines / shapes / txttips 四段：eNSP 自己存盘就是四段，
    # 缺 <shapes> 时它会静默当空工程打开（画布空白、总数 0），无论 devices 里写了什么。
    parts.append("    <shapes>\n    </shapes>\n")
    parts.append("    <txttips>\n")
    for content, left, top in LABELS:
        width = sum(13 if ord(c) > 127 else 7 for c in content) + 24
        parts.append(
            '        <txttip left="%d" top="%d" right="%d" bottom="%d" content="%s"'
            ' fontname="Consolas" fontstyle="0" editsize="100" txtcolor="-16777216"'
            ' txtbkcolor="-7278960" charset="1" />'
            % (left, top, left + width, top + 30, content)
        )
    parts.append("\n    </txttips>\n")
    parts.append("</topo>\r\n")
    return "".join(parts)


def validate_xml(text):
    """生成后自检：XML 合法 + eNSP 的三个硬性要求。

    eNSP 对不合规的 .topo 是「静默降级」——不报错、直接当空工程打开，
    所以这些检查必须在写盘前做掉：
      1. 顶层四段 devices / lines / shapes / txttips 齐全（缺 shapes 会 0 设备）
      2. 每个 system_mac 必须是合法 6 段 MAC（否则该设备被丢弃）
      3. 不能被 XML 解析器拒绝（声明里的 UNICODE 不是合法编码名）
    """
    fixed = re.sub(r'encoding="[^"]*"', 'encoding="utf-8"', text, count=1)
    root = ET.fromstring(fixed)
    for tag in ("devices", "lines", "shapes", "txttips"):
        if root.find(tag) is None:
            raise ValueError("生成的 .topo 缺少 <%s> 段：eNSP 会把它当空工程打开" % tag)
    mac_re = re.compile(r"^(?:[0-9A-F]{2}-){5}[0-9A-F]{2}$")
    for dev in root.findall("./devices/dev"):
        mac = dev.get("system_mac")
        if mac and not mac_re.match(mac):
            raise ValueError("设备 %s 的 system_mac=%r 不是 6 段 MAC"
                             % (dev.get("name"), mac))


def build_project(outdir, name="lab", devices=None, links=None, pc=None, labels=None,
                  base_port=2000, encoding="gbk", calibrate=False):
    """Write <outdir>/<name>.topo plus one GUID folder per router/switch.

    Pass devices/links/pc/labels to override the module tables, and base_port to
    start console ports somewhere other than 2000 (useful when several lab
    projects share one machine: give each project its own 12-port block so a
    scan can tell them apart). Returns the .topo path."""
    global DEVICES, LINKS, PC_SETTINGS, LABELS
    if devices is not None:
        DEVICES = devices
    if links is not None:
        LINKS = links
    if pc is not None:
        PC_SETTINGS = pc
    if labels is not None:
        LABELS = labels
    if calibrate:
        # 实测：渲染后图标中心 = (cx + dx, cy + dy)。把量到的图标中心换算成该填的 cx/cy。
        # 必须放在 devices 赋值之后：先赋值再校准，否则传进来的 devices 会把校准覆盖掉。
        off = {"PC": (27, 42), "S3700": (3, 41)}
        DEVICES = [(n, m, x - off.get(m.upper(), (0, 0))[0], y - off.get(m.upper(), (0, 0))[1])
                   for (n, m, x, y) in DEVICES]

    text = build_text(base_port)
    validate_xml(text)
    os.makedirs(outdir, exist_ok=True)
    topo_path = os.path.join(outdir, name + ".topo")
    # 编码：eNSP 按系统 ANSI（简体中文 = GBK）读 .topo 文本，UTF-8 写中文标签会变乱码。
    # 纯 ASCII 时两种编码等价，所以默认 GBK 是安全的。
    with open(topo_path, "w", encoding=encoding, errors="replace", newline="") as fh:
        fh.write(text)
    for dev_name, model, _, _ in DEVICES:
        if model.upper() == "PC":
            continue  # PCs get no folder; com_port stays 0
        os.makedirs(os.path.join(outdir, _stable_id(dev_name)), exist_ok=True)
    return topo_path


def build_from_spec(spec, outdir=None):
    """按 lab.json 生成工程；只需一份数据文件，不用改脚本常量。"""
    import lab_spec
    devices, links, pc, labels = lab_spec.topology(spec)
    return build_project(
        outdir or os.path.dirname(os.path.abspath(spec.get("_path", "."))),
        spec.get("name", "lab"),
        devices, links, pc, labels,
        base_port=int(spec.get("base_port", 2000)),
        encoding=spec.get("encoding", "gbk"),
        calibrate=bool(spec.get("calibrate", False)),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate an eNSP .topo project.")
    ap.add_argument("outdir", nargs="?", default="",
                    help="folder to create the project in（--spec 时可省略）")
    ap.add_argument("--spec", default="", help="lab.json：拓扑+配置+断言一份数据")
    ap.add_argument("--name", default=None, help="project name (default: lab)")
    ap.add_argument("--base-port", type=int, default=None,
                    help="first console port (default 2000; use a distinct block per lab)")
    ap.add_argument("--encoding", default=None,
                    help="topo 文本编码（默认 gbk：eNSP 按 ANSI 读，UTF-8 中文会乱码）")
    ap.add_argument("--calibrate", action="store_true",
                    help="坐标是从截图量来的图标中心时加这个（自动扣 eNSP 的锚点偏差）")
    args = ap.parse_args(argv)

    if args.spec:
        try:
            import lab_spec
            spec = lab_spec.load(args.spec)
        except (OSError, ValueError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        spec["_path"] = args.spec
        if args.name:
            spec["name"] = args.name
        if args.base_port is not None:
            spec["base_port"] = args.base_port
        if args.encoding:
            spec["encoding"] = args.encoding
        if args.calibrate:
            spec["calibrate"] = True
        try:
            path = build_from_spec(spec, args.outdir or os.path.dirname(os.path.abspath(args.spec)))
        except (OSError, ValueError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        print("wrote %s" % path)
        print("from spec: %s" % args.spec)
        return 0

    if not args.outdir:
        print("error: 没有 --spec 时必须给输出目录", file=sys.stderr)
        return 2
    try:
        path = build_project(args.outdir, args.name or "lab",
                             base_port=args.base_port or 2000,
                             encoding=args.encoding or "gbk",
                             calibrate=args.calibrate)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print("wrote %s" % path)
    print("devices: %d, links: %d" % (len(DEVICES), len(LINKS)))
    print("next: open the .topo in eNSP, start the devices, then configure over console")
    return 0


if __name__ == "__main__":
    sys.exit(main())
