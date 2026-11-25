#!/usr/bin/env python3
"""EXPERIMENTAL: can an eNSP device be booted without the eNSP GUI?

What this verifies
------------------
eNSP runs every router as a VirtualBox VM cloned from ``vboxserver/AR_Base``.
The console is a named pipe ``\\\\.\\pipe\\config`` attached to UART2
(IOBase 0x2f8, IRQ3). VirtualBox can present a UART as a TCP server instead, so
if a linked clone boots and speaks VRP on that TCP port, a lab can run headless
(no eNSP process, multiple labs in parallel, own port blocks).

What it does (and undoes)
-------------------------
1. refuses to run while eNSP_Client/VBoxHeadless is running (never disturb a lab)
2. uses an isolated VBOX_USER_HOME, so the eNSP VirtualBox registry is untouched
3. registers AR_Base there, makes a *linked* clone, points UART2 at a TCP port
4. starts the clone headless and waits for a VRP prompt on 127.0.0.1:<port>
5. powers the clone off, unregisters and deletes it

It stops VBoxSVC once: VBOX_USER_HOME is only honoured when no VBoxSVC is
already serving the session. That is safe while no VM is running (checked
first), and eNSP restarts its own VBoxSVC when it next opens a project.

Usage:
    python headless_probe.py --yes [--ensp-dir D:\\ENSP\\eNSP] [--port 22000]

Verdicts:
    HEADLESS-OK      VRP answered over plain TCP: the rest of eNSP is unnecessary
    NO-BOOT          VM started but never spoke (image/VBox incompatibility)
    REFUSED          eNSP or a VM is running, or prerequisites are missing

Standard library only.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

VBOX_CANDIDATES = [
    os.environ.get("VBOXMANAGE", ""),
    r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    r"C:\Program Files (x86)\Oracle\VirtualBox\VBoxManage.exe",
]
ENSP_CANDIDATES = [
    os.environ.get("ENSP_DIR", ""),
    r"D:\ENSP\eNSP",
    r"C:\Program Files\Huawei\eNSP",
    r"C:\Program Files (x86)\Huawei\eNSP",
]


def find_first(candidates, suffix):
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, suffix) if os.path.isdir(c) else c):
            return c
    return None


def vboxmanage():
    for c in VBOX_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    found = shutil.which("VBoxManage")
    if found:
        return found
    raise SystemExit("REFUSED: VBoxManage.exe not found (set VBOXMANAGE)")


def tasklist_names():
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.lower()


def run_vbox(vb, args, env, timeout=120):
    proc = subprocess.run([vb] + args, capture_output=True, text=True,
                          timeout=timeout, env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def wait_console(port, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                s.settimeout(4)
                time.sleep(0.7)
                try:
                    data = s.recv(8192)
                except socket.timeout:
                    data = b""
                if data:
                    return data.decode("utf-8", "replace")
        except OSError:
            pass
        time.sleep(4)
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Probe headless eNSP device boot")
    ap.add_argument("--yes", action="store_true", help="required: confirms the probe may start a VM")
    ap.add_argument("--ensp-dir", default=None)
    ap.add_argument("--base-vm", default="AR_Base")
    ap.add_argument("--snapshot", default="AR_Base_Link")
    ap.add_argument("--port", type=int, default=22000)
    ap.add_argument("--wait", type=int, default=120)
    ap.add_argument("--keep", action="store_true", help="keep the clone after the probe")
    args = ap.parse_args(argv)

    if not args.yes:
        print("REFUSED: pass --yes to allow starting a temporary VirtualBox VM")
        return 2

    ensp = args.ensp_dir or find_first(ENSP_CANDIDATES, "")
    if not ensp or not os.path.isdir(ensp):
        print("REFUSED: eNSP install dir not found (set ENSP_DIR / --ensp-dir)")
        return 2
    base_vbox = os.path.join(ensp, "vboxserver", args.base_vm, args.base_vm + ".vbox")
    if not os.path.isfile(base_vbox):
        print("REFUSED: base VM missing: %s" % base_vbox)
        return 2

    names = tasklist_names()
    if "ensp_client.exe" in names or "vboxheadless.exe" in names:
        print("REFUSED: eNSP or a VirtualBox VM is running; close the lab first")
        return 2

    vb = vboxmanage()
    work = tempfile.mkdtemp(prefix="ensp-headless-")
    home = os.path.join(work, "home")
    vms = os.path.join(work, "vms")
    os.makedirs(home)
    os.makedirs(vms)
    env = dict(os.environ, VBOX_USER_HOME=home)
    name = "headless_probe"

    print("base VM : %s" % base_vbox)
    print("work dir: %s" % work)
    print("vbox home isolated: %s" % home)

    # VBOX_USER_HOME is ignored while a VBoxSVC serves this session.
    rc, out = run_vbox(vb, ["list", "runningvms"], env)
    if out.strip():
        print("REFUSED: VMs are running:\n%s" % out.strip())
        return 2
    subprocess.run(["taskkill", "/IM", "VBoxSVC.exe", "/F"],
                   capture_output=True, timeout=20)
    time.sleep(2)
    print("VBoxSVC stopped (no VMs were running)")

    try:
        rc, out = run_vbox(vb, ["registervm", base_vbox], env)
        print("register: rc=%d %s" % (rc, out.strip()[:200]))

        rc, out = run_vbox(vb, ["clonevm", args.base_vm, "--snapshot", args.snapshot,
                                "--options", "link", "--name", name,
                                "--basefolder", vms], env, timeout=300)
        print("clone   : rc=%d %s" % (rc, out.strip()[-300:]))
        if rc != 0:
            return 1

        rc, out = run_vbox(vb, ["modifyvm", name, "--nic1", "none",
                                "--uart1", "0x2f8", "3",
                                "--uartmode1", "tcpserver", str(args.port)], env)
        print("uart    : rc=%d %s" % (rc, out.strip()[:200]))

        rc, out = run_vbox(vb, ["startvm", name, "--type", "headless"], env, timeout=180)
        print("start   : rc=%d %s" % (rc, out.strip()[:200]))

        print("waiting up to %ds for VRP on 127.0.0.1:%d ..." % (args.wait, args.port))
        banner = wait_console(args.port, args.wait)
        if banner:
            print("\n=== CONSOLE BANNER ===")
            print(banner[:1500])
            print("=== VERDICT: HEADLESS-OK ===")
            print("A UART-as-TCP console works. Next step: clone one VM per device,")
            print("wire links with internal networks, and map ports to the .topo.")
            return 0

        print("\n=== VERDICT: NO-BOOT ===")
        print("The VM started but never spoke on the TCP UART.")
        log = os.path.join(vms, name, "Logs", "VBox.log")
        if os.path.isfile(log):
            with open(log, "r", encoding="utf-8", errors="replace") as fh:
                print("\n--- VBox.log tail ---")
                print("".join(fh.readlines()[-20:]))
        return 1
    finally:
        if args.keep:
            print("kept clone: %s (unregister: VBoxManage unregistervm %s --delete)"
                  % (os.path.join(vms, name), name))
        else:
            run_vbox(vb, ["controlvm", name, "poweroff"], env, timeout=60)
            run_vbox(vb, ["unregistervm", name, "--delete"], env, timeout=120)
            shutil.rmtree(work, ignore_errors=True)
            print("cleaned up %s" % work)


if __name__ == "__main__":
    sys.exit(main())