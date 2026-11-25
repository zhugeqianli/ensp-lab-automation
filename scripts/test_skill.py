#!/usr/bin/env python3
"""Regression checks for the eNSP skill scripts.

Runs without eNSP and without network access:

  1. vrp_console protocol behaviour against test_offline.FakeVRP
  2. parse_topo against every .topo file passed on the command line
  3. make_topo generation, then parse_topo round-trip of the result
  4. interface index tables for each supported model

Usage:
    python test_skill.py [--topos DIR]

Exit 0 when everything passes.
"""

import argparse
import glob
import os
import shutil
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import make_topo  # noqa: E402
import parse_topo  # noqa: E402
import test_offline  # noqa: E402
from vrp_console import VRPConsole  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  (%s)" % detail))
    if not cond:
        FAILED.append(name)
    return bool(cond)


def section(title):
    print()
    print(title)


def console_checks():
    section("[vrp_console] protocol behaviour (fake VRP)")
    fake = test_offline.FakeVRP()
    fake.start()
    time.sleep(0.2)
    with VRPConsole(fake.port) as con:
        check("wait_ready returns the prompt", con.wait_ready(timeout=10).rstrip() == "R1>")
        check("hostname parsing", con.hostname() == "R1")
        text = con.cmd("display version")
        check("paged output reassembled", "line-1" in text and "line-3" in text, repr(text[:80]))
        check("paging marker removed", "More" not in text)
        try:
            con.cmd("bogus-command")
            check("command error raises", False)
        except Exception:
            check("command error raises", True)
        brief = con.iface_brief()
        check("iface_brief row count", len(brief) == 3, str(sorted(brief)))
        check("iface_brief address", brief.get("GigabitEthernet0/0/0", {}).get("ip") == "192.168.1.1/24")
        check("iface_counters", con.iface_counters("GigabitEthernet0/0/0") == (1234, 5678))
        ok, loss, rtt = con.ping("192.168.2.10", count=4)
        check("ping ok/loss/rtt", ok and loss == 25.0 and rtt == 30.0, "%s %s %s" % (ok, loss, rtt))
        check("save()", con.save() is True)
        check("device recorded save", fake.saved is True)

        out = con.paste(["system-view", "sysname R2", "screen-length 0 temporary"])
        check("paste applies a command block", con.hostname() == "R2", out[:80])
        check("paste strips echo/prompt noise",
              "system-view" not in out and "R2]" not in out, repr(out[:120]))
        try:
            con.paste(["bogus-command"])
            check("paste raises on a rejected command", False)
        except Exception:
            check("paste raises on a rejected command", True)

        con.cmd("system-view")
        check("view() reports the config view", con.view() == "config", con.view())
        before = con.return_to_user_view()
        check("return_to_user_view leaves the user view", con.view() == "user", before)
        check("backup() returns the running config", "sysname" in con.backup())
        ok, text = con.wait_for("192.168.1.1", command="display ip interface brief", timeout=6)
        check("wait_for matches a condition", ok, text[:80])

    # A switch rejects `screen-length` (VTY-only); paging must still work in-band.
    fake2 = test_offline.FakeVRP()
    fake2.reject_screen_length = True
    fake2.start()
    time.sleep(0.2)
    with VRPConsole(fake2.port) as con2:
        check("screen-length rejection is detected", con2.disable_paging() is False)
        text = con2.cmd("display version")
        check("paging reassembled in-band after rejection",
              "line-3" in text and "More" not in text, repr(text[:100]))


def smoke_checks():
    """每个脚本都能 import、能 --help（挡住 apply_plan 那种 import 期崩溃）。"""
    section("[smoke] every script imports and answers --help")
    import test_smoke
    for name in test_smoke.run_all():
        FAILED.append("smoke: " + name)


def apply_plan_checks():
    """apply_plan 端到端：假 VRP 上跑通「下发 → save → 断言 → 报告」。"""
    section("[apply_plan] end-to-end against fake VRP")
    import contextlib
    import io
    import json
    import apply_plan as ap

    tmp = workdir()
    try:
        fake = test_offline.FakeVRP()
        fake.start()
        time.sleep(0.2)
        plan = {"devices": [{
            "name": "R1", "port": fake.port,
            "lines": ["system-view", "sysname R1", "return"],
            "expect": [
                {"name": "sysname 生效", "cmd": "display current-configuration",
                 "contains": "sysname R1"},
                {"name": "没有 Error 行", "cmd": "display current-configuration",
                 "not_contains": "Error:"},
            ],
        }], "save": True}
        plan_path = os.path.join(tmp, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        jpath = os.path.join(tmp, "report.json")
        mpath = os.path.join(tmp, "report.md")
        def run(args):
            """跑一次 main() 并把控制台输出收起来（测试日志里别刷整份报告）。"""
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ap.main(args)
            return code, buf.getvalue()

        rc, console = run([plan_path, "--json", jpath, "--md", mpath,
                           "--report", os.path.join(tmp, "report.txt")])
        check("apply_plan exits 0 on a good plan", rc == 0, "rc=%s" % rc)
        check("text report says 1/1 passed", "1/1" in console, console[-160:])
        with open(jpath, encoding="utf-8") as fh:
            data = json.load(fh)
        res = data["results"][0]
        check("JSON report names the device", res["device"] == "R1", str(res.get("device")))
        check("JSON report: both checks pass",
              len(res["expects"]) == 2 and all(e["ok"] for e in res["expects"]),
              str(res["expects"]))
        check("JSON report: save recorded",
              res["save"] is True and fake.saved is True, "%s / %s" % (res["save"], fake.saved))
        check("device really got the sysname", fake.sysname == "R1", fake.sysname)
        with open(mpath, encoding="utf-8") as fh:
            md = fh.read()
        check("markdown report written", "\u2705" in md and "sysname" in md, md[:80])

        # 断言不成立时必须退 1，而不是"发了就算成功"
        fake2 = test_offline.FakeVRP()
        fake2.start()
        time.sleep(0.2)
        bad = {"devices": [{
            "name": "R1", "port": fake2.port,
            "lines": ["system-view", "sysname R1", "return"],
            "expect": [{"name": "VLAN10 存在", "cmd": "display current-configuration",
                        "contains": "vlan batch 10"}],
        }], "save": False}
        bad_path = os.path.join(tmp, "bad.json")
        with open(bad_path, "w", encoding="utf-8") as fh:
            json.dump(bad, fh)
        rc2, console2 = run([bad_path])
        check("apply_plan exits 1 when an assertion fails", rc2 == 1, "rc=%s" % rc2)
        check("failed assertion shows up in the report", "[FAIL]" in console2,
              console2[-160:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def apply_plan_regression_checks():
    """P0 回归：不重放命令 / ping 缺 target 退 2 / 报告写不了退 2 / 主机名不一致不下发。"""
    section("[apply_plan] P0 regressions")
    import contextlib
    import io
    import json
    import apply_plan as ap

    tmp = workdir()
    try:
        def run(args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ap.main(args)
            return code, out.getvalue(), err.getvalue()

        def write_plan(name, plan):
            path = os.path.join(tmp, name)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(plan, fh)
            return path

        def new_fake(sysname=None):
            fake = test_offline.FakeVRP()
            if sysname:
                fake.sysname = sysname
            fake.start()
            time.sleep(0.2)
            return fake

        # --- P0-1：整段下发报错后不许重放（旧行为会把全部 lines 再发一遍）
        fake = new_fake()
        path = write_plan("p0-1.json", {"devices": [{
            "name": "R1", "port": fake.port,
            "lines": ["system-view", "bogus-command-xyz", "return"],
        }], "save": False})
        rc, console, _ = run([path])
        check("P0-1 malformed line: exits 1", rc == 1, "rc=%s" % rc)
        count = fake.history.count("bogus-command-xyz")
        check("P0-1 malformed line sent exactly once (no replay)", count == 1,
              "sent %d times; history=%s" % (count, fake.history[:10]))
        check("P0-1 replay is off: system-view not re-sent",
              fake.history.count("system-view") <= 1,
              "system-view x%d" % fake.history.count("system-view"))
        check("P0-1 error still reported", "[FAIL]" in console, console[-200:])

        # --- P0-2：ping 断言缺 target 属于计划问题 → 退 2，且不许碰设备
        fake2 = new_fake()
        path2 = write_plan("p0-2.json", {"devices": [{
            "name": "R1", "port": fake2.port,
            "lines": ["system-view", "return"],
            "expect": [{"name": "PC1 通", "ping": {"max_loss": 0}}],
        }], "save": False})
        rc2, console2, _ = run([path2])
        check("P0-2 ping without target: exits 2", rc2 == 2, "rc=%s" % rc2)
        check("P0-2 ping without target: says why", "target" in console2, console2[-200:])
        check("P0-2 ping without target: device untouched",
              "system-view" not in fake2.history, str(fake2.history[:6]))

        # --- P0-3：报告写不进磁盘 → 退 2（以前静默返回 0/1）
        fake3 = new_fake()
        path3 = write_plan("p0-3.json", {"devices": [{
            "name": "R1", "port": fake3.port, "lines": ["system-view", "return"],
        }], "save": False})
        bad_report = os.path.join(tmp, "no-such-dir", "report.txt")
        rc3, console3, err3 = run([path3, "--report", bad_report])
        check("P0-3 unwritable report: exits 2", rc3 == 2, "rc=%s" % rc3)
        check("P0-3 unwritable report: announced on stderr", "写不了" in err3, err3[-200:])
        check("P0-3 unwritable report: no false '已写入' success claim",
              "文本报告已写入 %s" % bad_report not in console3, console3[-160:])

        # --- P0-4：提示符主机名与计划名不一致 → 默认跳过，不下发、不 save
        fake4 = new_fake(sysname="R9")
        path4 = write_plan("p0-4.json", {"devices": [{
            "name": "R1", "port": fake4.port,
            "lines": ["system-view", "sysname R1", "return"],
        }], "save": True})
        rc4, console4, _ = run([path4])
        check("P0-4 hostname mismatch: exits 1", rc4 == 1, "rc=%s" % rc4)
        check("P0-4 hostname mismatch: nothing pushed",
              "sysname R1" not in fake4.history, str(fake4.history[:8]))
        check("P0-4 hostname mismatch: not saved", fake4.saved is False, str(fake4.saved))
        check("P0-4 hostname mismatch: explains the skip",
              "--allow-name-mismatch" in console4, console4[-200:])

        # --- 逃生开关确实有效（否则那个 flag 就是个安慰剂）
        fake5 = new_fake(sysname="R9")
        path5 = write_plan("p0-4b.json", {"devices": [{
            "name": "R1", "port": fake5.port,
            "lines": ["system-view", "sysname R1", "return"],
        }], "save": False})
        rc5, _, _ = run([path5, "--allow-name-mismatch"])
        check("P0-4 --allow-name-mismatch does push", "sysname R1" in fake5.history,
              str(fake5.history[:8]))
        check("P0-4 --allow-name-mismatch exits 0", rc5 == 0, "rc=%s" % rc5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def lab_spec_checks():
    """lab.json：校验、编译成计划、直接生成工程。"""
    section("[lab_spec] spec -> topology / plan / topo")
    import lab_spec

    spec = {
        "name": "spec-lab",
        "base_port": 2012,
        "devices": [
            {"name": "R1", "model": "AR2220", "x": 100, "y": 100},
            {"name": "PC1", "model": "PC", "x": 300, "y": 100,
             "ip": "192.168.1.10", "mask": "255.255.255.0", "gateway": "192.168.1.1"},
        ],
        "links": [{"a": "R1", "a_if": "GigabitEthernet0/0/0",
                   "b": "PC1", "b_if": "Ethernet0/0/1"}],
        "labels": [{"text": "LAN", "x": 10, "y": 10}],
        "config": {"R1": {"lines": ["system-view", "sysname R1"],
                          "expect": [{"name": "sysname 生效",
                                      "cmd": "display current-configuration",
                                      "contains": "sysname R1"}]}},
        "save": True,
    }
    try:
        lab_spec.validate(spec)
        check("validate accepts a good spec", True)
    except ValueError as exc:
        check("validate accepts a good spec", False, str(exc))

    devices, links, pc, labels = lab_spec.topology(spec)
    check("topology: devices/links/pc/labels",
          (len(devices), len(links), len(pc), len(labels)) == (2, 1, 1, 1),
          "%s %s %s %s" % (len(devices), len(links), len(pc), len(labels)))
    check("topology: PC address carried over", pc["PC1"]["ip"] == "192.168.1.10", str(pc))

    struct = lab_spec.plan(spec)
    check("plan: only non-PC devices get a console", len(struct["devices"]) == 1,
          str(struct["devices"]))
    check("plan: port follows base_port", struct["devices"][0]["port"] == 2012,
          str(struct["devices"][0]["port"]))
    check("plan: lines and expect compiled",
          struct["devices"][0]["lines"][-1] == "sysname R1"
          and struct["devices"][0]["expect"][0]["contains"] == "sysname R1",
          str(struct["devices"][0]))

    tmp = workdir()
    try:
        path = make_topo.build_from_spec(spec, tmp)
        data = parse_topo.parse_topo(path)
        check("spec -> .topo round-trips",
              len(data["devices"]) == 2 and len(data["links"]) == 1,
              "%s devices %s links" % (len(data["devices"]), len(data["links"])))
        check("spec -> .topo keeps PC address",
              next(d for d in data["devices"] if d["name"] == "PC1")["pc"]["ip"]
              == "192.168.1.10")
        check("spec -> .topo uses base_port",
              next(d for d in data["devices"] if d["name"] == "R1")["com_port"] == 2012)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [
        ("duplicate device name", dict(spec, devices=spec["devices"] + [dict(spec["devices"][0])])),
        ("link to unknown device", dict(spec, links=[{"a": "R1", "a_if": "GE0/0/0",
                                                      "b": "NOPE", "b_if": "Ethernet0/0/1"}])),
        ("unsupported model", dict(spec, devices=[{"name": "X", "model": "NE40", "x": 0, "y": 0}])),
        ("PC without ip", dict(spec, devices=[{"name": "P", "model": "PC", "x": 0, "y": 0}])),
        ("config for a PC", dict(spec, config={"PC1": {"lines": ["x"]}})),
    ]
    for label, candidate in bad:
        try:
            lab_spec.validate(candidate)
            check("rejects: %s" % label, False, "没抛异常")
        except ValueError:
            check("rejects: %s" % label, True)


def index_checks():
    section("[parse_topo] interface index tables")
    expect = {
        "AR2220": ["GigabitEthernet0/0/0", "GigabitEthernet0/0/1", "GigabitEthernet0/0/2"],
        "AR1220": ["GigabitEthernet0/0/0", "GigabitEthernet0/0/1"]
                  + ["Ethernet0/0/%d" % i for i in range(1, 9)],
        "S3700": ["Ethernet0/0/%d" % i for i in range(1, 23)]
                 + ["GigabitEthernet0/0/1", "GigabitEthernet0/0/2"],
        "PC": ["Ethernet0/0/1"],
    }
    for model, names in expect.items():
        dev = make_topo._dev_skeleton("T", model)
        got = [name for _, name in parse_topo._iter_interfaces(dev)]
        check("%s interfaces" % model, got == names,
              "got %s" % got[:4] + ("..." if len(got) > 4 else ""))

    # A serial card is added only when a Serial link actually terminates on the
    # device, so drive it through the generator's own link table.
    saved_links = make_topo.LINKS
    try:
        make_topo.LINKS = [("S", "Serial1/0/0", "T", "Serial1/0/0", "Serial"),
                           ("S", "Serial1/0/1", "T", "Serial1/0/1", "Serial")]
        dev = make_topo._dev_skeleton("S", "AR2220")
    finally:
        make_topo.LINKS = saved_links
    names = [name for _, name in parse_topo._iter_interfaces(dev)]
    check("AR2220 + serial card", names[3:5] == ["Serial1/0/0", "Serial1/0/1"], str(names))

    for model, iface, idx in [("AR2220", "GE0/0/2", 2), ("S3700", "GE0/0/2", 23),
                              ("AR1220", "Eth0/0/8", 9), ("PC", "Ethernet0/0/1", 0)]:
        dev = make_topo._dev_skeleton("T", model)
        check("%s %s -> index %d" % (model, iface, idx),
              parse_topo.interface_index(dev, iface) == idx)

    dev = make_topo._dev_skeleton("T", "S3700")
    try:
        parse_topo.interface_index(dev, "GigabitEthernet0/0/0")
        check("S3700 GE0/0/0 rejected (starts at 1)", False)
    except ValueError:
        check("S3700 GE0/0/0 rejected (starts at 1)", True)


def parse_real(topos_dir):
    section("[parse_topo] real projects in %s" % topos_dir)
    files = sorted(glob.glob(os.path.join(topos_dir, "*.topo")))
    if not files:
        print("  (none found -- skipped)")
        return
    for path in files:
        try:
            data = parse_topo.parse_topo(path)
        except Exception as exc:
            check(os.path.basename(path), False, str(exc))
            continue
        routers = [d for d in data["devices"] if d["model"].upper() != "PC"]
        good = (len(data["devices"]) > 0
                and len(data["links"]) > 0
                and all(l["src_interface"] and l["dst_interface"] for l in data["links"]))
        check("%s: %d devices, %d links" % (os.path.basename(path), len(data["devices"]),
                                            len(data["links"])), good,
              "unresolved interface on some link")
        check("%s: console ports assigned" % os.path.basename(path),
              all(d["com_port"] >= 2000 for d in routers))


def workdir():
    """Scratch dir next to the scripts.

    tempfile is avoided on purpose: under a Windows sandbox the temp root can be
    read-only, which made the round-trip test fail with PermissionError while
    the code itself was fine.
    """
    d = os.path.join(HERE, ".test-work")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


def roundtrip():
    section("[make_topo] generate + parse round-trip")
    devices = [
        ("AR1", "AR2220", 100, 100), ("AR2", "AR2220", 400, 100),
        ("R1", "AR1220", 700, 100), ("SW", "S3700", 400, 300),
        ("PC1", "PC", 100, 300),
    ]
    links = [
        ("AR1", "Serial1/0/0", "AR2", "Serial1/0/0", "Serial"),
        ("AR1", "GigabitEthernet0/0/2", "SW", "GigabitEthernet0/0/2", "Copper"),
        ("R1", "Ethernet0/0/8", "SW", "Ethernet0/0/22", "Copper"),
        ("SW", "Ethernet0/0/1", "PC1", "Ethernet0/0/1", "Copper"),
    ]
    pc = {"PC1": {"ip": "192.168.10.10", "mask": "255.255.255.0", "gateway": "192.168.10.1"}}
    tmp = workdir()
    try:
        path = make_topo.build_project(tmp, "lab", devices, links, pc, [])
        check("wrote .topo", os.path.isfile(path))
        data = parse_topo.parse_topo(path)
        check("device count round-trips", len(data["devices"]) == len(devices),
              str(len(data["devices"])))
        check("link count round-trips", len(data["links"]) == len(links),
              str(len(data["links"])))
        pairs = {(l["src_interface"], l["dst_interface"]) for l in data["links"]}
        check("serial link resolved", ("Serial1/0/0", "Serial1/0/0") in pairs, str(pairs))
        check("GE0/0/2 resolved", ("GigabitEthernet0/0/2", "GigabitEthernet0/0/2") in pairs)
        check("S3700 Eth0/0/22 resolved", ("Ethernet0/0/8", "Ethernet0/0/22") in pairs)
        pc_dev = next(d for d in data["devices"] if d["model"] == "PC")
        check("PC address round-trips", pc_dev["pc"]["ip"] == "192.168.10.10", str(pc_dev["pc"]))
        check("no warnings", not data["warnings"], str(data["warnings"]))
        # GUID folders exist for routers/switches, not for PCs.
        folders = [n for n in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, n))]
        check("one folder per non-PC device", len(folders) == 4, str(len(folders)))

        # Regenerating must not orphan folders (stable GUIDs).
        make_topo.build_project(tmp, "lab", devices, links, pc, [])
        folders2 = [n for n in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, n))]
        check("regeneration is idempotent", sorted(folders) == sorted(folders2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --calibrate 必须作用在「传进来的 devices」上：曾经被后面的赋值整体覆盖。
    tmp = workdir()
    try:
        path = make_topo.build_project(tmp, "cal", devices=[("PC1", "PC", 100, 100)],
                                       links=[], pc={}, labels=[], calibrate=True)
        text = open(path, encoding="utf-8").read()
        check("calibrate subtracts the PC anchor offset",
              'cx="73.000000"' in text and 'cy="58.000000"' in text,
              text[text.find("<dev "):][:160])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def registry_checks():
    section("[project_registry] port blocks for several labs")
    import project_registry as proj
    tmp = workdir()
    try:
        reg = os.path.join(tmp, "registry.json")
        first = os.path.join(tmp, "lab-a")
        make_topo.build_project(first, "lab-a", base_port=2000,
                                devices=[("R1", "AR2220", 100, 100), ("R2", "AR2220", 300, 100)],
                                links=[("R1", "GigabitEthernet0/0/0", "R2",
                                        "GigabitEthernet0/0/0", "Copper")],
                                pc={}, labels=[])
        data, entry = proj.register(os.path.join(first, "lab-a.topo"), proj.load(reg),
                                    base_port=2000, name="lab-a")
        proj.save(data, reg)
        check("register keeps base port", entry["base_port"] == 2000, str(entry["base_port"]))
        check("port 2001 resolves to lab-a",
              (proj.by_port(2001, proj.load(reg)) or {}).get("name") == "lab-a")
        check("next free block skips the used one", proj.next_base(proj.load(reg)) == 2012,
              str(proj.next_base(proj.load(reg))))
        second = os.path.join(tmp, "lab-b")
        make_topo.build_project(second, "lab-b", base_port=2012,
                                devices=[("R3", "AR2220", 100, 100), ("R4", "AR2220", 300, 100)],
                                links=[("R3", "GigabitEthernet0/0/0", "R4",
                                        "GigabitEthernet0/0/0", "Copper")],
                                pc={}, labels=[])
        parsed = parse_topo.parse_topo(os.path.join(second, "lab-b.topo"))
        check("--base-port lands in the .topo",
              parsed["devices"][0]["com_port"] == 2012, str(parsed["devices"][0]["com_port"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def topo_tool_checks():
    section("[validate_topo / topo_export]")
    import validate_topo
    import topo_export
    tmp = workdir()
    try:
        path = make_topo.build_project(
            tmp, "lab",
            devices=[("R1", "AR2220", 100, 100), ("R2", "AR2220", 300, 100),
                     ("PC1", "PC", 100, 300)],
            links=[("R1", "GigabitEthernet0/0/0", "R2", "GigabitEthernet0/0/0", "Copper"),
                   ("R2", "GigabitEthernet0/0/1", "PC1", "Ethernet0/0/1", "Copper")],
            pc={"PC1": {"ip": "192.168.1.10", "mask": "255.255.255.0",
                        "gateway": "192.168.1.1"}},
            labels=[])
        report = validate_topo.validate(path)
        check("validate: clean lab has no errors", not report["errors"], str(report["errors"]))
        check("validate: counts devices and links",
              report["devices"] == 3 and report["links"] == 2, str(report))

        text = open(path, "r", encoding="utf-8").read().replace('com_port="2001"', 'com_port="2000"')
        broken = os.path.join(tmp, "broken.topo")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write(text)
        bad = validate_topo.validate(broken)
        check("validate: duplicate console port is an error",
              any("console port 2000" in e for e in bad["errors"]), str(bad["errors"]))

        parsed = parse_topo.parse_topo(path)
        mermaid = topo_export.to_mermaid(parsed)
        check("export: mermaid has a graph and both routers",
              mermaid.startswith("graph LR") and "R1" in mermaid and "R2" in mermaid,
              mermaid[:80])
        check("export: mermaid draws every link",
              sum(1 for line in mermaid.splitlines() if "--->" in line) >= 0
              and mermaid.count("---|") == 2, mermaid)
        csv_text = topo_export.to_csv(parsed)
        check("export: csv has device and link sections",
              "section,name,model" in csv_text and "section,src" in csv_text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def corrupted_guard():
    section("[parse_topo] rejects bad input cleanly")
    tmp = workdir()
    try:
        bad = os.path.join(tmp, "bad.topo")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("<topo><devices><dev id=")
        try:
            parse_topo.parse_topo(bad)
            check("malformed XML raises ValueError", False)
        except ValueError:
            check("malformed XML raises ValueError", True)
        except Exception as exc:
            check("malformed XML raises ValueError", False, type(exc).__name__)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--topos", default="", help="directory of real .topo files")
    args = ap.parse_args(argv)

    smoke_checks()
    apply_plan_checks()
    apply_plan_regression_checks()
    console_checks()
    index_checks()
    if args.topos:
        parse_real(args.topos)
    roundtrip()
    lab_spec_checks()
    registry_checks()
    topo_tool_checks()
    corrupted_guard()

    print()
    if FAILED:
        print("FAILED: %d check(s): %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
