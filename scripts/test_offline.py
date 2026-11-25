#!/usr/bin/env python3
"""Offline self-test for vrp_console.py using a fake VRP console.

The fake device speaks enough VRP to exercise every quirk the library is
supposed to absorb: bare-CR command submission, paged long output
(``---- More ----``), an errored command, the ``save`` y/n prompt, ``ping``
statistics, and ``display ip interface brief``.

Run:  python test_offline.py
Exit: 0 all checks passed, 1 a check failed.
"""

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True

from vrp_console import VRPConsole, VRPError  # noqa: E402

HOSTNAME = "R1"


class FakeVRP(threading.Thread):
    """Minimal VRP-ish console on an ephemeral port."""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.view = "user"
        self.sysname = HOSTNAME
        self.screen_length = 512
        self.reject_screen_length = False
        self.saved = False
        self.pending_save = False
        self.paging_sent = False
        self.history = []  # every non-empty command line received (回放/重复执行自证用)

    def prompt(self):
        return "%s%s" % (self.sysname, ">" if self.view == "user" else "]")

    def send(self, conn, text):
        conn.sendall(text.encode("utf-8"))

    def run(self):
        conn, _ = self.srv.accept()
        conn.settimeout(30)
        self.send(conn, "\r\nPress ENTER to get started.\r\n")
        buf = ""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk.decode("utf-8", "replace")
                # Consume complete lines. A bare CR ends a line; a following LF
                # is the leftover newline that must NOT answer y/n prompts.
                while True:
                    idx_cr = buf.find("\r")
                    idx_lf = buf.find("\n")
                    if idx_lf != -1 and (idx_cr == -1 or idx_lf < idx_cr):
                        buf = buf[idx_lf + 1:]  # stray LF: swallow, never answer a prompt
                        continue
                    if idx_cr == -1:
                        # A lone space is the paging continue keystroke.
                        if buf.strip() == "" and buf and self.paging_sent:
                            buf = ""
                            self.send(conn, " line-%d\r\n" % 3)
                            self.send(conn, self.prompt())
                            self.paging_sent = False
                        break
                    line = buf[:idx_cr]
                    buf = buf[idx_cr + 1:]
                    self.handle(conn, line)
        finally:
            conn.close()
            self.srv.close()

    def handle(self, conn, line):
        cmd = line.strip()
        if cmd:
            self.history.append(cmd)
        if self.pending_save:
            if cmd.lower() in ("y", "yes"):
                self.saved = True
                self.pending_save = False
                self.send(conn, "\r\n  Configuration file has been successfully saved.\r\n")
            elif cmd == "":
                pass
            self.send(conn, self.prompt())
            return
        if cmd == "":
            self.send(conn, "\r\n" + self.prompt())
            return

        self.send(conn, cmd + "\r\n")
        low = cmd.lower()

        if low == "system-view":
            self.view = "config"
            self.send(conn, "[%s]\r\n%s]" % (self.sysname, self.sysname))
        elif low.startswith("sysname "):
            self.sysname = cmd.split(None, 1)[1].strip()
            self.send(conn, "[%s]" % self.sysname)
        elif low == "quit":
            self.view = "user"
            self.send(conn, "<%s>" % self.sysname)
        elif low == "return":
            self.view = "user"
            self.send(conn, "<%s>" % self.sysname)
        elif low.startswith("screen-length 0"):
            if self.reject_screen_length:
                self.send(conn, "\r\nError: The type of the included user interface"
                                " must be VTY.\r\n")
                self.send(conn, self.prompt())
            else:
                self.screen_length = 0
                self.send(conn, self.prompt())
        elif low.startswith("display current-configuration"):
            self.send(conn, "\r\nsysname %s\r\ninterface GigabitEthernet0/0/0\r\n"
                            " ip address 192.168.1.1 255.255.255.0\r\nreturn\r\n" % self.sysname)
            self.send(conn, self.prompt())
        elif low == "save":
            self.send(conn, "\r\nThe current configuration will be written to the device.\r\n"
                            "Are you sure to continue?[Y/N]")
            self.pending_save = True
        elif low.startswith("ping"):
            self.send(conn, "\r\n  PING 192.168.2.10: 56  data bytes, press CTRL_C to break\r\n"
                            "    Reply from 192.168.2.10: bytes=56 Sequence=1 ttl=254 time=30 ms\r\n"
                            "\r\n  --- 192.168.2.10 ping statistics ---\r\n"
                            "    4 packet(s) transmitted\r\n"
                            "    3 packet(s) received\r\n"
                            "    25.00% packet loss\r\n"
                            "    round-trip min/avg/max = 20/30/40 ms\r\n\r\n")
            self.send(conn, self.prompt())
        elif low.startswith("display ip interface brief"):
            # One leading space per field: real VRP pads the IP column as
            # "192.168.1.1/24", and the last column may be truncated.
            rows = ["Interface                         IP Address/Mask      Physical   Protocol",
                    "GigabitEthernet0/0/0             192.168.1.1/24       up         up",
                    "GigabitEthernet0/0/1             unassigned           down       down",
                    "Serial1/0/0                      10.0.13.1/24         up         up"]
            self.send(conn, "\r\n" + "\r\n".join(rows) + "\r\n\r\n")
            self.send(conn, self.prompt())
        elif low.startswith("display interface"):
            self.send(conn, "\r\nGigabitEthernet0/0/0 current state : UP\r\n"
                            "    Input: 1234 packets\r\n"
                            "    Output: 5678 packets\r\n\r\n")
            self.send(conn, self.prompt())
        elif low.startswith("display"):
            # A paged dump: page 1 + '---- More ----', page 2 after a keystroke.
            if self.screen_length == 0:
                self.send(conn, "\r\nline-1\r\nline-2\r\nline-3\r\n\r\n" + self.prompt())
            else:
                self.paging_sent = True
                self.send(conn, "\r\nline-1\r\nline-2\r\n  ---- More ----")
        else:
            self.send(conn, "^\r\nError: Unrecognized command found at '^' position.\r\n")
            self.send(conn, self.prompt())


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  (%s)" % detail) if detail and not cond else ""))
    return bool(cond)


def main():
    fake = FakeVRP()
    fake.start()
    time.sleep(0.2)
    results = []

    with VRPConsole(fake.port) as con:
        results.append(check("wait_ready finds a prompt",
                             con.wait_ready(timeout=10).rstrip() == "R1>"))
        results.append(check("hostname()", con.hostname() == "R1"))

        text = con.cmd("display version")
        results.append(check("paged output reassembled",
                             "line-1" in text and "line-3" in text,
                             repr(text[:120])))
        results.append(check("paging marker consumed", "More" not in text))

        con.cmd("system-view")
        con.cmd("sysname R1")
        results.append(check("sysname applied", con.hostname() == "R1"))

        try:
            con.cmd("bogus-command")
            results.append(check("error command raises", False))
        except VRPError:
            results.append(check("error command raises", True))

        itfs = con.iface_brief()
        results.append(check("iface_brief parses 3 rows", len(itfs) == 3, str(itfs.keys())))
        results.append(check("iface_brief reads address",
                             itfs.get("GigabitEthernet0/0/0", {}).get("ip") == "192.168.1.1/24",
                             str(itfs.get("GigabitEthernet0/0/0"))))

        counters = con.iface_counters("GigabitEthernet0/0/0")
        results.append(check("iface_counters (in,out)", counters == (1234, 5678), str(counters)))

        ok, loss, rtt = con.ping("192.168.2.10", count=4)
        results.append(check("ping ok/loss/rtt", ok and loss == 25.0 and rtt == 30.0,
                             "%s %s %s" % (ok, loss, rtt)))

        results.append(check("save() reaches the y/n prompt", con.save() is True))
        results.append(check("device recorded the save", fake.saved is True))

    print()
    failed = results.count(False)
    print("%d/%d checks passed" % (len(results) - failed, len(results)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
