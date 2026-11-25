#!/usr/bin/env python3
"""Protocol self-test for ensp_mcp_server.py -- no eNSP required.

Starts the server over stdio, performs the MCP handshake, lists tools, builds a
small .topo project through the protocol and parses it back through the
protocol. Offline, standard library only.

    python selftest_mcp.py
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "ensp_mcp_server.py")
WORK = os.path.join(os.path.dirname(HERE), ".mcp_selftest")


class Client:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.path.dirname(HERE),
        )
        self.next_id = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        line = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()
        if notify:
            return None
        raw = self.proc.stdout.readline()
        if not raw:
            err = self.proc.stderr.read().decode("utf-8", "replace")
            raise RuntimeError("server closed stdout; stderr:\n%s" % err)
        return json.loads(raw.decode("utf-8"))

    def call(self, name, arguments):
        reply = self.send("tools/call", {"name": name, "arguments": arguments})
        result = reply.get("result") or {}
        items = result.get("content") or []
        text = items[0]["text"] if items else ""
        return text, bool(result.get("isError"))

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


FAILED = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(label)


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    client = Client()
    try:
        init = client.send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "0"},
        })
        info = init.get("result") or {}
        check("initialize returns serverInfo", info.get("serverInfo", {}).get("name") == "ensp-lab-mcp", info)
        check("initialize advertises tools", "tools" in (info.get("capabilities") or {}), info)

        client.send("notifications/initialized", {}, notify=True)

        listing = client.send("tools/list", {})
        tools = {t["name"] for t in (listing.get("result") or {}).get("tools", [])}
        expected = {
            "ensp_scan_devices", "ensp_connect", "ensp_run_command", "ensp_save_config",
            "ensp_ping", "ensp_interface_brief", "ensp_interface_counters",
            "ensp_parse_topo", "ensp_build_topo", "ensp_list_projects",
            "ensp_validate_topo", "ensp_export_topo", "ensp_backup", "ensp_wait_for",
            "ensp_list_sessions", "ensp_disconnect",
        }
        check("tools/list exposes all tools", expected <= tools, sorted(expected - tools))

        text, is_error = client.call("ensp_list_projects", {})
        check("ensp_list_projects returns a JSON list",
              not is_error and isinstance(json.loads(text), list), text[:120])

        spec = {
            "outdir": os.path.join(WORK, "lab"),
            "name": "lab",
            "devices": [
                {"name": "R1", "model": "AR2220", "x": 100, "y": 100},
                {"name": "R2", "model": "AR2220", "x": 400, "y": 100},
                {"name": "SW1", "model": "S3700", "x": 250, "y": 300},
                {"name": "PC1", "model": "PC", "x": 100, "y": 300},
            ],
            "links": [
                {"src": "R1", "src_interface": "Serial1/0/0",
                 "dst": "R2", "dst_interface": "Serial1/0/0", "media": "Serial"},
                {"src": "R1", "src_interface": "GigabitEthernet0/0/2",
                 "dst": "SW1", "dst_interface": "GigabitEthernet0/0/1"},
                {"src": "SW1", "src_interface": "Ethernet0/0/1",
                 "dst": "PC1", "dst_interface": "Ethernet0/0/1"},
            ],
            "pc": {"PC1": {"ip": "192.168.10.10", "mask": "255.255.255.0",
                           "gateway": "192.168.10.1"}},
            "labels": [{"text": "192.168.10.0/24", "x": 80, "y": 60}],
        }
        text, is_error = client.call("ensp_build_topo", spec)
        check("ensp_build_topo succeeds", not is_error, text)
        built = json.loads(text) if not is_error else {}
        check("build reports 4 devices / 3 links", built.get("devices") == 4 and built.get("links") == 3, built)
        check("console ports assigned from 2000",
              list(built.get("console_ports", {}).values())[:1] == [2000], built.get("console_ports"))

        text, is_error = client.call("ensp_parse_topo", {"path": built.get("topo", "")})
        check("ensp_parse_topo succeeds", not is_error, text)
        data = json.loads(text) if not is_error else {}
        pairs = {(l["src_interface"], l["dst_interface"]) for l in data.get("links", [])}
        check("serial link resolves", ("Serial1/0/0", "Serial1/0/0") in pairs, pairs)
        check("S3700 GE0/0/1 resolves to index 22",
              any(l["dst_interface"] == "GigabitEthernet0/0/1" and l["dst_index"] == 22
                  for l in data.get("links", [])), data.get("links"))
        check("PC address round-trips",
              any(d["model"] == "PC" and d["pc"]["ip"] == "192.168.10.10" for d in data.get("devices", [])),
              data.get("devices"))
        check("no parser warnings", not data.get("warnings"), data.get("warnings"))

        text, is_error = client.call("ensp_validate_topo", {"path": built.get("topo", "")})
        check("ensp_validate_topo: generated lab is clean",
              not is_error and not json.loads(text)["errors"], text[:160])

        text, is_error = client.call("ensp_export_topo",
                                     {"path": built.get("topo", ""), "format": "mermaid"})
        check("ensp_export_topo returns a Mermaid graph",
              not is_error and text.startswith("graph LR"), text[:80])

        text, is_error = client.call("ensp_run_command", {
            "port": 2999,
            "command": "ping 8.8.8.8",
            "confirm_destructive": False,
        })
        check("offline console call fails cleanly (no eNSP running)",
              is_error, text)

        text, is_error = client.call("ensp_run_command", {
            "port": 2999,
            "commands": ["system-view", "sysname X", "quit"],
            "fast": True,
        })
        check("offline paste path fails cleanly", is_error, text)

        text, is_error = client.call("ensp_disconnect", {"all": True})
        check("ensp_disconnect all", not is_error, text)
    finally:
        client.close()
        shutil.rmtree(WORK, ignore_errors=True)

    print()
    if FAILED:
        print("FAILED: %d check(s): %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("all MCP checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())