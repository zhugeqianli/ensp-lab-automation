#!/usr/bin/env python3
"""Parse an eNSP project (.topo) and print devices, console ports, PC addressing
and full link wiring with resolved interface names.

Standard library only. Runs on Python 3.8+ and any OS.

Usage:
    python parse_topo.py <project.topo> [--json]

Exit codes: 0 ok, 2 file/parse error.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Model tables: how slot17 declarations map onto interface indices.
#
# Index order follows the slot17 declaration order in the .topo file, with each
# <interface> repeating count times. Verified against real eNSP projects:
#   AR1220: GE count=2 + Ethernet count=8 -> 0,1 = GE0/0/0-1 ; 2..9 = Eth0/0/1-8
#   S3700 : Ethernet count=22 + GE count=2 -> 0..21 = Eth0/0/1-22 ; 22,23 = GE
#   AR2220 + Serial card: GE 1 + GE 2 + Serial 2 -> 0..2 GE ; 3,4 = Serial1/0/0-1
# ---------------------------------------------------------------------------

DEFAULT_SLOTS = {
    "AR2220": [("Ethernet", "GE", 1), ("Ethernet", "GE", 2)],
    "AR1220": [("Ethernet", "GE", 2), ("Ethernet", "Ethernet", 8)],
    "S3700": [("Ethernet", "Ethernet", 22), ("Ethernet", "GE", 2)],
    "PC": [("Ethernet", "Ethernet", 1)],
}


def _is_serial(tag):
    return str(tag).lower().startswith("serial")


def _slot_spec(dev):
    """Return [(sztype, interfacename, count), ...] for a <dev> element."""
    spec = []
    for slot in dev.findall("slot"):
        if slot.get("isMainBoard") != "1":
            continue
        for itf in slot.findall("interface"):
            try:
                count = int(itf.get("count", "0"))
            except ValueError:
                count = 0
            spec.append((itf.get("sztype", ""), itf.get("interfacename", ""), count))
    if spec:
        return spec
    return list(DEFAULT_SLOTS.get(dev.get("model", ""), []))


def _iface_name(sztype, interfacename, ordinal):
    """Human VRP name for one interface within its family.

    `ordinal` is the running position inside the declaration order, which is
    how eNSP itself numbers: AR2220 declares GE twice (count=1 then count=2)
    and the second declaration continues at GE0/0/1, not GE0/0/0. Serial
    cards always start at Serial1/0/0."""
    if _is_serial(sztype) or _is_serial(interfacename):
        return "Serial1/0/%d" % ordinal
    if interfacename.startswith(("GE", "GigabitEthernet")):
        return "GigabitEthernet0/0/%d" % ordinal
    if interfacename.startswith("Ethernet"):
        return "Ethernet0/0/%d" % (ordinal + 1)
    return "%s%d" % (interfacename, ordinal)


def _iter_interfaces(dev):
    """Yield (index, vrp_name) in slot17 declaration order.

    Each family keeps its own counter, and the first number differs by model:
    the S3700 (a switch) exposes its uplinks as GE0/0/1-2, while routers start
    at GE0/0/0. Ethernet ports always start at 1. Serial cards always start at
    Serial1/0/0."""
    model = (dev.get("model") or "").upper()
    ge_first = 1 if model.startswith("S") else 0
    idx = 0
    ge_seen = 0
    eth_seen = 0
    serial_base = _serial_base(dev)
    for sztype, ifname, count in _slot_spec(dev):
        for _ in range(count):
            if _is_serial(sztype) or _is_serial(ifname):
                yield idx, "Serial1/0/%d" % (idx - serial_base)
            elif ifname.startswith(("GE", "GigabitEthernet")):
                yield idx, "GigabitEthernet0/0/%d" % (ge_first + ge_seen)
                ge_seen += 1
            elif ifname.startswith("Ethernet"):
                yield idx, "Ethernet0/0/%d" % (eth_seen + 1)
                eth_seen += 1
            else:
                yield idx, "%s%d" % (ifname, idx)
            idx += 1


def _serial_base(dev):
    """Index of the first Serial interface on this device (0 when absent)."""
    idx = 0
    for sztype, ifname, count in _slot_spec(dev):
        for _ in range(count):
            if _is_serial(sztype) or _is_serial(ifname):
                return idx
            idx += 1
    return 0


def _normalize(name):
    name = name.strip().replace(" ", "")
    low = name.lower()
    low = low.replace("gigabitethernet", "ge")
    low = low.replace("ethernet", "eth")
    return low


def index_to_name(dev, index):
    """Reverse mapping: interface index -> VRP name."""
    for idx, name in _iter_interfaces(dev):
        if idx == index:
            return name
    return None


def interface_index(dev, interface_name):
    """Map 'GigabitEthernet0/0/2' / 'GE0/0/2' / 'Eth0/0/3' / 'Serial1/0/1' -> index."""
    want = _normalize(interface_name)
    known = []
    for idx, name in _iter_interfaces(dev):
        known.append(name)
        if _normalize(name) == want:
            return idx
    raise ValueError(
        "%s on %s: interface %r not present (available: %s)"
        % (dev.get("name"), dev.get("model"), interface_name, ", ".join(known))
    )


def _pc_settings(settings):
    """Parse '-simpc_ip 1.2.3.4 -simpc_mask ...' into a dict."""
    out = {}
    if not settings:
        return out
    for key, val in re.findall(r"-(sim(?:pc)?\w*)\s+(\S+)", settings):
        out[key] = val
    return {
        "ip": out.get("simpc_ip", ""),
        "mask": out.get("simpc_mask", ""),
        "gateway": out.get("simpc_gateway", ""),
        "mac": out.get("simpc_mac", ""),
    }


def read_topo_text(path):
    """Read a .topo file. eNSP declares encoding='UNICODE' but writes UTF-8."""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if "<topo" in text:
            return text
    raise ValueError("cannot decode %s as a .topo XML document" % path)


def _clean_xml(text):
    """eNSP writes encoding='UNICODE', which XML parsers reject as unknown."""
    return re.sub(
        r'<\?xml[^>]*encoding="[^"]*"[^>]*\?>',
        '<?xml version="1.0" encoding="utf-8"?>',
        text,
        count=1,
    )


def parse_topo(path):
    text = _clean_xml(read_topo_text(path))
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("invalid .topo XML (%s): %s" % (path, exc))

    dev_els = {}
    devices = []
    for dev in root.iter("dev"):
        did = dev.get("id", "")
        model = dev.get("model", "")
        info = {
            "id": did,
            "name": dev.get("name", ""),
            "model": model,
            "com_port": int(dev.get("com_port", "0") or 0),
            "system_mac": dev.get("system_mac", ""),
            "bootmode": dev.get("bootmode", ""),
            "x": dev.get("cx", ""),
            "y": dev.get("cy", ""),
            "pc": _pc_settings(dev.get("settings", "")) if model.upper() == "PC" else {},
            "serial_card": any(s.get("isMainBoard") == "0" for s in dev.findall("slot")),
        }
        dev_els[did] = dev
        devices.append(info)

    links = []
    for line in root.iter("line"):
        src = line.get("srcDeviceID", "")
        dst = line.get("destDeviceID", "")
        for pair in line.findall("interfacePair"):
            try:
                src_idx = int(pair.get("srcIndex", "-1"))
                tar_idx = int(pair.get("tarIndex", "-1"))
            except ValueError:
                src_idx = tar_idx = -1
            links.append({
                "src_id": src,
                "src_name": next((d["name"] for d in devices if d["id"] == src), "?"),
                "src_index": src_idx,
                "src_interface": index_to_name(dev_els[src], src_idx) if src in dev_els else None,
                "dst_id": dst,
                "dst_name": next((d["name"] for d in devices if d["id"] == dst), "?"),
                "dst_index": tar_idx,
                "dst_interface": index_to_name(dev_els[dst], tar_idx) if dst in dev_els else None,
                "media": pair.get("lineName", ""),
            })

    txttips = [
        {"content": t.get("content", ""), "left": t.get("left", ""), "top": t.get("top", "")}
        for t in root.iter("txttip")
    ]

    project_dir = os.path.dirname(os.path.abspath(path))
    warnings = []
    known = {d["id"] for d in devices}
    try:
        present = {
            n for n in os.listdir(project_dir)
            if os.path.isdir(os.path.join(project_dir, n))
            and re.match(r"^[0-9A-Fa-f-]{36}$", n)
        }
    except OSError:
        present = set()
    for d in devices:
        if d["model"].upper() == "PC":
            continue
        if d["id"] not in present:
            warnings.append("%s: GUID folder missing (%s)" % (d["name"], d["id"]))
    for orphan in sorted(present - known):
        warnings.append("orphan GUID folder not referenced by .topo: %s" % orphan)

    return {
        "path": os.path.abspath(path),
        "devices": devices,
        "links": links,
        "txttips": txttips,
        "warnings": warnings,
    }


def format_table(data):
    out = ["project: %s" % data["path"], "", "DEVICES",
           "  %-8s %-8s %-7s %-16s %s" % ("name", "model", "console", "pc-ip", "note")]
    for d in data["devices"]:
        pc = d["pc"] or {}
        note = []
        if pc.get("gateway"):
            note.append("gw=%s" % pc["gateway"])
        if d["serial_card"]:
            note.append("serial-card")
        out.append("  %-8s %-8s %-7s %-16s %s" % (
            d["name"], d["model"], d["com_port"] or "-", pc.get("ip", "-"), " ".join(note)))
    out += ["", "LINKS"]
    for l in data["links"]:
        out.append("  %-8s %-20s<-- %-6s -->%-20s%-8s" % (
            l["src_name"], l["src_interface"] or "?", l["media"][:6],
            l["dst_interface"] or "?", l["dst_name"]))
    if data["txttips"]:
        out += ["", "DRAWING LABELS"]
        out += ["  %s" % t["content"] for t in data["txttips"]]
    if data["warnings"]:
        out += ["", "WARNINGS"]
        out += ["  ! %s" % w for w in data["warnings"]]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parse an eNSP .topo project.")
    ap.add_argument("topo", help="path to the .topo file")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)
    try:
        data = parse_topo(args.topo)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(format_table(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
