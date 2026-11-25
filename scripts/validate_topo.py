#!/usr/bin/env python3
"""Semantic checks for an eNSP .topo project, before it is opened in eNSP.

parse_topo tells you what is in the file; this tells you what is wrong with it:
duplicate names, clashing console ports, link indexes that no model exposes,
orphan GUID folders, PCs without an address. Errors stop eNSP from wiring the
lab correctly; warnings are worth a look.

Standard library only.

    python validate_topo.py <project.topo> [--json]
    python validate_topo.py --selftest

Exit codes: 0 clean (warnings allowed), 1 errors found, 2 unreadable file.
"""

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import make_topo  # noqa: E402
import parse_topo  # noqa: E402


def _dups(values):
    seen, dup = set(), set()
    for v in values:
        if v in seen:
            dup.add(v)
        seen.add(v)
    return dup


def validate(path):
    data = parse_topo.parse_topo(path)
    devices = data["devices"]
    links = data["links"]
    errors, warnings = [], []

    names = [d["name"] for d in devices]
    if not devices:
        errors.append("no devices in the project")
    for name in sorted(_dups(names)):
        errors.append("duplicate device name: %s" % name)

    console = [d for d in devices if d["model"].upper() != "PC"]
    ports = [d["com_port"] for d in console if d["com_port"]]
    for port in sorted(_dups(ports)):
        errors.append("console port %d is used by more than one device" % port)
    if len(console) > 11:
        errors.append("%d console devices exceed the 2000-2010 port range"
                      % len(console))

    for d in devices:
        if d["model"].upper() == "PC":
            if d["com_port"]:
                warnings.append("%s is a PC but has com_port=%d" % (d["name"], d["com_port"]))
            pc = d["pc"] or {}
            if not pc.get("ip"):
                warnings.append("%s has no IP configured" % d["name"])
            if not pc.get("gateway"):
                warnings.append("%s has no gateway configured" % d["name"])
        else:
            if not d["com_port"]:
                errors.append("%s has no console port" % d["name"])
            elif not 2000 <= d["com_port"] <= 2100:
                warnings.append("%s console port %d is outside the usual 2000-2100"
                                % (d["name"], d["com_port"]))

    for link in links:
        pair = "%s <-> %s" % (link["src_name"], link["dst_name"])
        if link["src_id"] == link["dst_id"]:
            errors.append("self-link on %s" % link["src_name"])
        for side in ("src", "dst"):
            if link[side + "_index"] < 0:
                errors.append("%s: negative interface index on the %s side" % (pair, side))
            if not link[side + "_interface"]:
                errors.append("%s: interface index %s does not exist on %s"
                              % (pair, link[side + "_index"], link[side + "_name"]))

    warnings.extend(data["warnings"])
    return {
        "path": data["path"],
        "devices": len(devices),
        "links": len(links),
        "errors": errors,
        "warnings": warnings,
    }


def selftest():
    import shutil
    import tempfile
    work = os.path.join(HERE, ".validate-selftest")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    ok = []
    try:
        path = make_topo.build_project(
            work, "lab",
            devices=[("R1", "AR2220", 100, 100), ("R2", "AR2220", 300, 100),
                     ("PC1", "PC", 100, 300)],
            links=[("R1", "GigabitEthernet0/0/0", "R2", "GigabitEthernet0/0/0", "Copper"),
                   ("R2", "GigabitEthernet0/0/1", "PC1", "Ethernet0/0/1", "Copper")],
            pc={"PC1": {"ip": "192.168.1.10", "mask": "255.255.255.0",
                        "gateway": "192.168.1.1"}},
            labels=[])
        clean = validate(path)
        ok.append(("clean lab has no errors", not clean["errors"]))
        ok.append(("clean lab reports devices/links",
                   clean["devices"] == 3 and clean["links"] == 2))

        # Clash two console ports and expect a hard error.
        text = open(path, "r", encoding="utf-8").read().replace('com_port="2001"', 'com_port="2000"')
        broken = os.path.join(work, "broken.topo")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write(text)
        report = validate(broken)
        ok.append(("duplicate console port is an error",
                   any("console port 2000" in e for e in report["errors"]),
                   str(report["errors"])))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    failed = 0
    for item in ok:
        label, cond = item[0], item[1]
        print(("  PASS  " if cond else "  FAIL  ") + label)
        failed += 0 if cond else 1
    print("\n%d/%d checks passed" % (len(ok) - failed, len(ok)))
    return 0 if failed == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate an eNSP .topo project")
    ap.add_argument("topo", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.topo:
        ap.print_help()
        return 2
    try:
        report = validate(args.topo)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("%s: %d devices, %d links" % (report["path"], report["devices"], report["links"]))
        for err in report["errors"]:
            print("  ERROR   %s" % err)
        for warn in report["warnings"]:
            print("  WARN    %s" % warn)
        if not report["errors"] and not report["warnings"]:
            print("  clean")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())