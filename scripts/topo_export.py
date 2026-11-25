#!/usr/bin/env python3
"""Turn a .topo project into something a report can embed.

eNSP has no export; a lab report usually needs a topology picture and a
device/link table. This emits Mermaid (renders on GitHub, GitLab, Typora,
VS Code), CSV (paste into Excel/Word) or the raw parser JSON.

Standard library only. No eNSP needed.

    python topo_export.py <project.topo> [--format mermaid|csv|json]

Exit codes: 0 ok, 2 unreadable file.
"""

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import parse_topo  # noqa: E402


def _node_id(index):
    return "n%d" % index


def to_mermaid(data):
    lines = ["graph LR"]
    for i, dev in enumerate(data["devices"]):
        label = "%s<br/>%s" % (dev["name"], dev["model"])
        if dev["model"].upper() == "PC":
            pc = dev["pc"] or {}
            label += "<br/>%s" % (pc.get("ip") or "no-ip")
        lines.append('    %s["%s"]' % (_node_id(i), label))
    index = {dev["id"]: i for i, dev in enumerate(data["devices"])}
    for link in data["links"]:
        a = index.get(link["src_id"])
        b = index.get(link["dst_id"])
        if a is None or b is None:
            continue
        label = "%s <-> %s" % (link["src_interface"] or "?", link["dst_interface"] or "?")
        lines.append('    %s ---|"%s"| %s' % (_node_id(a), label, _node_id(b)))
    for tip in data["txttips"]:
        lines.append("    %%%% label: %s" % tip["content"])
    return "\n".join(lines)


def to_csv(data):
    rows = ["section,name,model,console_port,pc_ip,pc_mask,pc_gateway"]
    for dev in data["devices"]:
        pc = dev["pc"] or {}
        rows.append("device,%s,%s,%s,%s,%s,%s" % (
            dev["name"], dev["model"], dev["com_port"] or "",
            pc.get("ip", ""), pc.get("mask", ""), pc.get("gateway", "")))
    rows.append("")
    rows.append("section,src,src_interface,dst,dst_interface,media")
    for link in data["links"]:
        rows.append("link,%s,%s,%s,%s,%s" % (
            link["src_name"], link["src_interface"] or "",
            link["dst_name"], link["dst_interface"] or "",
            link["media"]))
    return "\n".join(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export an eNSP .topo for documentation")
    ap.add_argument("topo")
    ap.add_argument("--format", default="mermaid",
                    choices=["mermaid", "csv", "json"])
    args = ap.parse_args(argv)
    try:
        data = parse_topo.parse_topo(args.topo)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.format == "mermaid":
        print(to_mermaid(data))
    elif args.format == "csv":
        print(to_csv(data))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())