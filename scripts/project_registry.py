#!/usr/bin/env python3
"""Project registry: which eNSP lab owns which console-port block.

eNSP gives every router/switch a console port starting at 2000, so two labs on
one machine collide as soon as they are both open. Keeping a tiny registry lets
the agent answer three questions without starting eNSP:

  * which port block does this .topo already own?   -> show <topo>
  * which project is the device on port 2001 from?  -> which <port>
  * where can a new lab start without colliding?    -> next (allocate)

Storage: per-user data dir (Windows ``%LOCALAPPDATA%\\ensp-lab-automation\\registry.json``,
else ``$XDG_DATA_HOME/ensp-lab-automation/registry.json``), so one skill install shares its
registry across DSH / WorkBuddy / Claude Code. Override with ENSP_LAB_REGISTRY.
Standard library only.

Usage:
    python project_registry.py list
    python project_registry.py show <project.topo>
    python project_registry.py register <project.topo> [--base 2012]
    python project_registry.py which 2001
    python project_registry.py next
    python project_registry.py forget <project.topo>
    python project_registry.py --selftest
"""

import argparse
import json
import os
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SPAN = 12  # console ports reserved per project block


def _user_data_dir():
    """Per-user data dir, independent of which AI client the skill is installed in."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "ensp-lab-automation")


DEFAULT_REGISTRY = os.path.join(_user_data_dir(), "registry.json")
# 早期版本固定写 ~/.dsh/ensp-labs.json；已有登记表就继续读它，避免丢工程登记
LEGACY_REGISTRY = os.path.join(os.path.expanduser("~"), ".dsh", "ensp-labs.json")


def registry_path(path=None):
    if path:
        return path
    env = os.environ.get("ENSP_LAB_REGISTRY")
    if env:
        return env
    if not os.path.exists(DEFAULT_REGISTRY) and os.path.exists(LEGACY_REGISTRY):
        return LEGACY_REGISTRY
    return DEFAULT_REGISTRY


def normalize(path):
    return os.path.normcase(os.path.abspath(path))


def load(path=None):
    p = registry_path(path)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("span", SPAN)
    data.setdefault("projects", {})
    return data


def save(data, path=None):
    p = registry_path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _blocks(data):
    return [(int(p["base_port"]), int(p["base_port"]) + int(data.get("span", SPAN)))
            for p in data["projects"].values() if p.get("base_port") is not None]


def next_base(data, start=2000, span=None):
    """First port block of `span` ports that does not overlap a registered lab."""
    span = int(span or data.get("span", SPAN))
    used = _blocks(data)
    base = int(start)
    while True:
        clash = any(base < hi and lo < base + span for lo, hi in used)
        if not clash:
            return base
        base = max(hi for lo, hi in used if base < hi and lo < base + span)


def register(topo_path, data=None, base_port=None, name=None, hostnames=None):
    """Record a project. Returns its entry."""
    data = data if data is not None else load()
    topo_path = os.path.abspath(topo_path)
    key = normalize(topo_path)
    entry = data["projects"].get(key, {})

    ports = {}
    devices = []
    if os.path.isfile(topo_path):
        import parse_topo
        parsed = parse_topo.parse_topo(topo_path)
        devices = [{"name": d["name"], "model": d["model"], "com_port": d["com_port"]}
                   for d in parsed["devices"]]
        for d in parsed["devices"]:
            if d["com_port"]:
                ports[d["name"]] = d["com_port"]
    if hostnames:
        for dev, host in hostnames.items():
            if dev in ports:
                ports[host] = ports.pop(dev)

    if base_port is None:
        base_port = min(ports.values()) if ports else next_base(data)
    entry.update({
        "name": name or entry.get("name") or os.path.splitext(os.path.basename(topo_path))[0],
        "topo": topo_path,
        "base_port": int(base_port),
        "ports": ports,
        "devices": devices,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    data["projects"][key] = entry
    return data, entry


def lookup(topo_path, data=None):
    data = data if data is not None else load()
    return data["projects"].get(normalize(topo_path))


def by_port(port, data=None):
    data = data if data is not None else load()
    for entry in data["projects"].values():
        if int(port) in {int(p) for p in (entry.get("ports") or {}).values()}:
            return entry
    return None


def list_projects(data=None):
    data = data if data is not None else load()
    rows = []
    for entry in data["projects"].values():
        rows.append({
            "name": entry.get("name"),
            "topo": entry.get("topo"),
            "base_port": entry.get("base_port"),
            "ports": entry.get("ports") or {},
            "devices": len(entry.get("devices") or []),
            "updated": entry.get("updated"),
        })
    return sorted(rows, key=lambda r: (r["base_port"] or 0, r["name"] or ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description="eNSP project / console-port registry")
    ap.add_argument("--registry", default=None, help="registry JSON path")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list")
    p_show = sub.add_parser("show"); p_show.add_argument("topo")
    p_reg = sub.add_parser("register"); p_reg.add_argument("topo")
    p_reg.add_argument("--base", type=int, default=None)
    p_reg.add_argument("--name", default=None)
    p_which = sub.add_parser("which"); p_which.add_argument("port", type=int)
    sub.add_parser("next")
    p_forget = sub.add_parser("forget"); p_forget.add_argument("topo")

    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    data = load(args.registry)
    if args.cmd == "list":
        rows = list_projects(data)
        if not rows:
            print("no projects registered")
        for r in rows:
            print("%-20s base=%-5s devices=%-2d %s" % (r["name"], r["base_port"], r["devices"], r["topo"]))
            if r["ports"]:
                print("    ports: " + ", ".join("%s=%s" % kv for kv in sorted(r["ports"].items())))
    elif args.cmd == "show":
        entry = lookup(args.topo, data)
        print(json.dumps(entry, ensure_ascii=False, indent=2) if entry else "not registered")
        return 0 if entry else 1
    elif args.cmd == "register":
        data, entry = register(args.topo, data, base_port=args.base, name=args.name)
        save(data, args.registry)
        print("registered %s (base %d)" % (entry["name"], entry["base_port"]))
    elif args.cmd == "which":
        entry = by_port(args.port, data)
        if entry is None:
            print("no project registered for port %d" % args.port)
            return 1
        print("%s  %s  (%s)" % (entry["name"], entry["topo"], entry["base_port"]))
    elif args.cmd == "next":
        print(next_base(data))
    elif args.cmd == "forget":
        key = normalize(args.topo)
        if key in data["projects"]:
            del data["projects"][key]
            save(data, args.registry)
            print("forgotten %s" % args.topo)
        else:
            print("not registered")
            return 1
    else:
        ap.print_help()
    return 0


def selftest():
    import shutil
    work = os.path.join(HERE, ".registry-selftest")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    sample = os.path.join(work, "lab-a")
    import make_topo
    make_topo.build_project(sample, "lab-a",
                            devices=[("R1", "AR2220", 100, 100), ("R2", "AR2220", 300, 100)],
                            links=[("R1", "GigabitEthernet0/0/0", "R2", "GigabitEthernet0/0/0", "Copper")],
                            pc={}, labels=[])
    topo = os.path.join(sample, "lab-a.topo")
    reg = os.path.join(work, "registry.json")

    data = load(reg)
    data, entry = register(topo, data, base_port=2000, name="lab-a")
    save(data, reg)
    ok = [("register stores base 2000", entry["base_port"] == 2000)]
    ok.append(("lookup finds it", lookup(topo, load(reg)) is not None))
    ok.append(("port 2001 maps to lab-a", (by_port(2001, load(reg)) or {}).get("name") == "lab-a"))
    ok.append(("port 2003 is free", by_port(2003, load(reg)) is None))
    ok.append(("next block skips 2000-2012", next_base(load(reg)) == 2012))

    second = os.path.join(os.path.dirname(sample), "lab-b")
    make_topo.build_project(second, "lab-b",
                            devices=[("R3", "AR2220", 100, 100), ("R4", "AR2220", 300, 100)],
                            links=[("R3", "GigabitEthernet0/0/0", "R4", "GigabitEthernet0/0/0", "Copper")],
                            pc={}, labels=[], base_port=2012)
    data, entry = register(os.path.join(second, "lab-b.topo"), load(reg), base_port=2012, name="lab-b")
    save(data, reg)
    ok.append(("two labs, no overlap", by_port(2012, load(reg))["name"] == "lab-b"))
    ok.append(("base_port lands in the topo", any(
        d["com_port"] == 2012 for d in
        __import__("parse_topo").parse_topo(os.path.join(second, "lab-b.topo"))["devices"])))

    failed = 0
    for label, cond in ok:
        print(("  PASS  " if cond else "  FAIL  ") + label)
        failed += 0 if cond else 1
    print("\n%d/%d checks passed" % (len(ok) - failed, len(ok)))
    shutil.rmtree(work, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())