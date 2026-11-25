#!/usr/bin/env python3
"""eNSP MCP server -- exposes the skill's console/topology scripts as MCP tools.

Transport: stdio, one JSON-RPC 2.0 message per line (MCP standard).
Dependencies: Python 3.8+ standard library only. No eNSP API is used; every
device operation goes through the per-device VRP console TCP port that eNSP
opens on 127.0.0.1:2000+.

Wire it into a client with the mcp.json next to this file, e.g.

    {
      "mcpServers": {
        "ensp": {"command": "python", "args": ["<skill>/mcp/ensp_mcp_server.py"]}
      }
    }

Safety: `reboot`, `reset saved-configuration`, `format`, `erase` and friends
are refused unless the caller passes confirm_destructive=true, because VRP
reboot inside eNSP drops into the boot loader and the device has to be
stopped/started from the GUI afterwards.

The stdout stream is the protocol -- all logging goes to stderr.
"""

import json
import os
import re
import sys
import traceback

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

import make_topo  # noqa: E402
import parse_topo  # noqa: E402
import project_registry  # noqa: E402
import topo_export  # noqa: E402
import validate_topo  # noqa: E402
from vrp_console import VRPConsole, VRPError, probe, scan_ports, wait_prompt  # noqa: E402

SERVER_NAME = "ensp-lab-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

# port -> VRPConsole, kept for the lifetime of the server process
SESSIONS = {}

DANGEROUS = re.compile(
    r"(?:^|\s)(reboot|reset\s+saved-configuration|reset\s+recycle-bin|format|"
    r"erase|delete\s+/force|startup\s+saved-configuration|undo\s+startup)\b",
    re.IGNORECASE,
)

DEFAULT_HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# tool implementations
# ---------------------------------------------------------------------------

def _session(port, host=DEFAULT_HOST, timeout=30.0):
    con = SESSIONS.get(port)
    if con is not None:
        return con
    con = wait_prompt(port, host=host, timeout=timeout)
    con.disable_paging()
    SESSIONS[port] = con
    return con


def tool_scan_devices(args):
    host = args.get("host") or DEFAULT_HOST
    start = int(args.get("start", 2000))
    end = int(args.get("end", 2010))
    ports = scan_ports(host=host, start=start, end=end)
    rows = []
    for port in ports:
        row = {"port": port, "ready": False, "hostname": None}
        if args.get("identify"):
            try:
                con = VRPConsole(port, host=host, connect_timeout=2.0)
                try:
                    row["hostname"] = con.hostname()
                    row["ready"] = True
                finally:
                    con.close()
            except (VRPError, OSError) as exc:
                row["error"] = str(exc)
        rows.append(row)
    return json.dumps(
        {"host": host, "range": [start, end], "ports": ports, "devices": rows},
        ensure_ascii=False,
        indent=2,
    )


def tool_connect(args):
    port = int(args["port"])
    expected = args.get("expected_name")
    if port in SESSIONS:
        con = SESSIONS[port]
        return "already connected: port %d, prompt %r" % (port, con.prompt())
    con = wait_prompt(port, name=expected, host=args.get("host") or DEFAULT_HOST,
                      timeout=float(args.get("timeout", 180)))
    con.disable_paging()
    SESSIONS[port] = con
    return json.dumps(
        {"port": port, "hostname": con.hostname(), "prompt": con.prompt()},
        ensure_ascii=False,
        indent=2,
    )


def tool_run_command(args):
    port = int(args["port"])
    commands = args.get("commands")
    if commands is None:
        command = args.get("command")
        if not command:
            raise ValueError("pass either command or commands")
        commands = [command]
    commands = [str(c) for c in commands]

    if not args.get("confirm_destructive"):
        blocked = [c for c in commands if DANGEROUS.search(c)]
        if blocked:
            raise ValueError(
                "refused destructive command(s) %s: pass confirm_destructive=true "
                "and tell the user that VRP reboot drops into the boot loader "
                "(stop/start from the eNSP GUI instead)" % blocked
            )

    con = _session(port, timeout=float(args.get("timeout", 30)))
    limit = args.get("max_chars", 6000)

    # A console keeps the view a previous caller (or the eNSP GUI) left it in.
    # Starting a batch in interface view would configure the wrong node.
    if args.get("to_user_view", True):
        try:
            con.return_to_user_view()
        except VRPError:
            pass

    # Pasting many lines at once is the fast path: one write, one read instead
    # of one idle timeout per command.
    if args.get("fast") or len(commands) > 2:
        text = con.paste(commands,
                         timeout=float(args.get("paste_timeout", 90)),
                         allow_error=bool(args.get("allow_error", False)))
        return _trim(text, limit)

    outputs = []
    for command in commands:
        body = con.cmd(command, timeout=float(args.get("command_timeout", 30)),
                       allow_error=bool(args.get("allow_error", False)))
        outputs.append({"command": command, "output": body})
    if len(outputs) == 1:
        return _trim(outputs[0]["output"], limit)
    return _trim(json.dumps(outputs, ensure_ascii=False, indent=2), limit)


def _trim(text, limit):
    """Keep a long console dump from eating the caller's context."""
    limit = int(limit or 0)
    if limit <= 0 or len(text) <= limit:
        return text
    half = limit // 2
    return (text[:half] + "\n... (%d chars omitted) ...\n" % (len(text) - limit)
            + text[-half:])


def tool_save_config(args):
    con = _session(int(args["port"]))
    ok = con.save()
    return json.dumps({"port": int(args["port"]), "saved": ok}, ensure_ascii=False)


def tool_ping(args):
    con = _session(int(args["port"]))
    ok, loss, rtt = con.ping(
        args["target"],
        source=args.get("source"),
        count=int(args.get("count", 4)),
        timeout=float(args.get("timeout", 60)),
    )
    return json.dumps(
        {"target": args["target"], "source": args.get("source"), "ok": ok,
         "loss_percent": loss, "rtt_ms": rtt},
        ensure_ascii=False,
    )


def tool_interface_brief(args):
    con = _session(int(args["port"]))
    return json.dumps(con.iface_brief(), ensure_ascii=False, indent=2)


def tool_interface_counters(args):
    con = _session(int(args["port"]))
    iface = args["interface"]
    if args.get("ping_target"):
        return json.dumps(
            con.count_and_ping(iface, args["ping_target"], source=args.get("source"),
                               count=int(args.get("count", 4))),
            ensure_ascii=False,
            indent=2,
        )
    rx, tx = con.iface_counters(iface)
    return json.dumps({"interface": iface, "rx_packets": rx, "tx_packets": tx},
                      ensure_ascii=False)


def _device_tuples(devices):
    out = []
    for d in devices:
        model = str(d["model"])
        out.append((str(d["name"]), model, float(d.get("x", 100)), float(d.get("y", 100))))
    return out


def _link_tuples(links):
    out = []
    for l in links:
        out.append((
            str(l["src"]), str(l["src_interface"]),
            str(l["dst"]), str(l["dst_interface"]),
            str(l.get("media", "Copper")),
        ))
    return out


def tool_build_topo(args):
    devices = _device_tuples(args["devices"])
    links = _link_tuples(args["links"])
    pc = args.get("pc") or {}
    labels = [(str(t["text"]), int(t.get("x", 100)), int(t.get("y", 80)))
              for t in (args.get("labels") or [])]
    name = str(args.get("name", "lab"))
    outdir = os.path.abspath(args["outdir"])
    path = make_topo.build_project(outdir, name, devices, links, pc, labels)
    data = parse_topo.parse_topo(path)
    return json.dumps(
        {"topo": path, "devices": len(data["devices"]), "links": len(data["links"]),
         "console_ports": {d["name"]: d["com_port"] for d in data["devices"]},
         "warnings": data["warnings"]},
        ensure_ascii=False,
        indent=2,
    )


def tool_parse_topo(args):
    data = parse_topo.parse_topo(args["path"])
    return json.dumps(data, ensure_ascii=False, indent=2)


def tool_list_sessions(args):
    rows = []
    for port, con in sorted(SESSIONS.items()):
        row = {"port": port}
        try:
            row["prompt"] = con.prompt(timeout=2.0)
        except (VRPError, OSError) as exc:
            row["error"] = str(exc)
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False, indent=2)


def tool_disconnect(args):
    if args.get("all"):
        for con in SESSIONS.values():
            con.close()
        SESSIONS.clear()
        return "closed all console sessions"
    port = int(args["port"])
    con = SESSIONS.pop(port, None)
    if con is None:
        return "no session on port %d" % port
    con.close()
    return "closed console %d" % port


def tool_list_projects(args):
    """Registered lab projects and the console-port block each one owns."""
    return json.dumps(project_registry.list_projects(), ensure_ascii=False, indent=2)


def tool_validate_topo(args):
    """Static checks that catch what makes eNSP open a lab incorrectly."""
    return json.dumps(validate_topo.validate(args["path"]), ensure_ascii=False, indent=2)


def tool_export_topo(args):
    data = parse_topo.parse_topo(args["path"])
    fmt = str(args.get("format", "mermaid")).lower()
    if fmt == "mermaid":
        return topo_export.to_mermaid(data)
    if fmt == "csv":
        return topo_export.to_csv(data)
    return json.dumps(data, ensure_ascii=False, indent=2)


def tool_backup(args):
    """Running config of one device, for before/after comparison or rollback."""
    con = _session(int(args["port"]))
    con.return_to_user_view()
    return _trim(con.backup(), args.get("max_chars", 6000))


def tool_wait_for(args):
    """Poll a display command until a pattern appears (no fixed sleeps)."""
    con = _session(int(args["port"]))
    ok, text = con.wait_for(
        args["pattern"],
        command=args.get("command"),
        timeout=float(args.get("timeout", 60)),
        interval=float(args.get("interval", 2)),
    )
    return json.dumps({"matched": ok, "pattern": args["pattern"],
                       "output_tail": _trim(text, 2000)},
                      ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# tool table
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "ensp_scan_devices",
        "description": "Scan 127.0.0.1 console ports 2000-2010 (the range eNSP assigns) "
                       "and report which ones have a listener. identify=true also nudges "
                       "each console for its VRP hostname (slow if a device is booting).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "default 127.0.0.1"},
                "start": {"type": "integer", "default": 2000},
                "end": {"type": "integer", "default": 2010},
                "identify": {"type": "boolean", "default": False,
                             "description": "read the VRP hostname from each live console"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_connect",
        "description": "Open a console session and wait for the VRP prompt (a running AR "
                       "takes 1-2 minutes to boot). expected_name aborts instead of "
                       "configuring the wrong device when the prompt says another name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer"},
                "expected_name": {"type": "string"},
                "host": {"type": "string"},
                "timeout": {"type": "number", "default": 180},
            },
            "required": ["port"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_run_command",
        "description": "Run one command or a list of VRP commands on a console (auto-connects "
                       "when needed) and return the output with echo and prompt stripped. "
                       "Three or more commands are pasted in one write (fast=true forces it): "
                       "one idle timeout instead of one per command. Output longer than "
                       "max_chars is head/tail trimmed to save context. Destructive commands "
                       "need confirm_destructive=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer"},
                "command": {"type": "string"},
                "commands": {"type": "array", "items": {"type": "string"}},
                "fast": {"type": "boolean", "default": False,
                         "description": "paste the whole list in one write and read once"},
                "allow_error": {"type": "boolean", "default": False,
                                "description": "return output instead of raising on VRP errors"},
                "confirm_destructive": {"type": "boolean", "default": False},
                "command_timeout": {"type": "number", "default": 30},
                "paste_timeout": {"type": "number", "default": 90},
                "max_chars": {"type": "integer", "default": 6000,
                              "description": "trim longer output to head+tail (0 = no trim)"},
            },
            "required": ["port"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_save_config",
        "description": "Run `save`, answering the file-name/y-n prompts. A hand-planted "
                       "vrpcfg.zip is never loaded by eNSP, so a lab must be configured live "
                       "and saved this way.",
        "inputSchema": {
            "type": "object",
            "properties": {"port": {"type": "integer"}},
            "required": ["port"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_ping",
        "description": "Ping from the device. Always pass source (ping -a) when verifying "
                       "routing: the default source is the outgoing interface address and a "
                       "network that does not cover the transit link returns a false 100% loss.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer"},
                "target": {"type": "string"},
                "source": {"type": "string"},
                "count": {"type": "integer", "default": 4},
                "timeout": {"type": "number", "default": 60},
            },
            "required": ["port", "target"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_interface_brief",
        "description": "Parsed `display ip interface brief`: ip/mask/physical/protocol per interface.",
        "inputSchema": {
            "type": "object",
            "properties": {"port": {"type": "integer"}},
            "required": ["port"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_interface_counters",
        "description": "Input/Output packet counters for one interface. With ping_target it "
                       "returns the counter deltas around a ping, which separates 'frame never "
                       "arrived' (rx 0) from 'arrived but not forwarded' (rx>0, tx 0).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer"},
                "interface": {"type": "string"},
                "ping_target": {"type": "string"},
                "source": {"type": "string"},
                "count": {"type": "integer", "default": 4},
            },
            "required": ["port", "interface"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_parse_topo",
        "description": "Parse a .topo project into devices, console ports, PC addressing and "
                       "resolved link interfaces, plus GUID-folder warnings.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_build_topo",
        "description": "Write a .topo project (no device config: eNSP ignores a pre-planted "
                       "vrpcfg.zip) and return the assigned console ports. Interface names are "
                       "converted to eNSP indices with the model's real slot17 declaration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outdir": {"type": "string"},
                "name": {"type": "string", "default": "lab"},
                "devices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "model": {"type": "string",
                                      "enum": ["AR2220", "AR1220", "S3700", "PC"]},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                        },
                        "required": ["name", "model"],
                        "additionalProperties": False,
                    },
                },
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "src": {"type": "string"},
                            "src_interface": {"type": "string"},
                            "dst": {"type": "string"},
                            "dst_interface": {"type": "string"},
                            "media": {"type": "string", "enum": ["Copper", "Serial"]},
                        },
                        "required": ["src", "src_interface", "dst", "dst_interface"],
                        "additionalProperties": False,
                    },
                },
                "pc": {
                    "type": "object",
                    "description": "{PC name: {ip, mask, gateway}}",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "ip": {"type": "string"},
                            "mask": {"type": "string"},
                            "gateway": {"type": "string"},
                        },
                        "required": ["ip", "mask", "gateway"],
                        "additionalProperties": False,
                    },
                },
                "labels": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                        },
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["outdir", "devices", "links"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_list_sessions",
        "description": "List console sessions this server process still holds.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "ensp_disconnect",
        "description": "Close one console session (port) or all of them (all=true).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer"},
                "all": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_list_projects",
        "description": "Registered lab projects: name, .topo path, console-port block and "
                       "device->port map. Use it to tell several labs apart without scanning "
                       "ports or opening eNSP.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "ensp_validate_topo",
        "description": "Semantic checks for a .topo project: duplicate names, clashing "
                       "console ports, link indexes no model exposes, PC without address, "
                       "orphan GUID folders. Returns errors and warnings.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_export_topo",
        "description": "Export a .topo for documentation: Mermaid diagram (renders in "
                       "GitHub/VS Code/Typora), CSV tables, or the parser JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "format": {"type": "string", "enum": ["mermaid", "csv", "json"],
                           "default": "mermaid"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_backup",
        "description": "Read `display current-configuration` from a device. Do it before a "
                       "configuration batch so the change can be compared or rolled back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer"},
                "max_chars": {"type": "integer", "default": 6000},
            },
            "required": ["port"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ensp_wait_for",
        "description": "Poll a display command until a pattern appears (AP joining, OSPF "
                       "full, BGP up) instead of sleeping and re-reading by hand.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer"},
                "pattern": {"type": "string", "description": "regular expression, case-insensitive"},
                "command": {"type": "string", "description": "display command to poll"},
                "timeout": {"type": "number", "default": 60},
                "interval": {"type": "number", "default": 2},
            },
            "required": ["port", "pattern"],
            "additionalProperties": False,
        },
    },
]

HANDLERS = {
    "ensp_scan_devices": tool_scan_devices,
    "ensp_connect": tool_connect,
    "ensp_run_command": tool_run_command,
    "ensp_save_config": tool_save_config,
    "ensp_ping": tool_ping,
    "ensp_interface_brief": tool_interface_brief,
    "ensp_interface_counters": tool_interface_counters,
    "ensp_parse_topo": tool_parse_topo,
    "ensp_build_topo": tool_build_topo,
    "ensp_list_projects": tool_list_projects,
    "ensp_validate_topo": tool_validate_topo,
    "ensp_export_topo": tool_export_topo,
    "ensp_backup": tool_backup,
    "ensp_wait_for": tool_wait_for,
    "ensp_list_sessions": tool_list_sessions,
    "ensp_disconnect": tool_disconnect,
}


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------

def _write(message):
    data = json.dumps(message, ensure_ascii=True, separators=(",", ":"))
    sys.stdout.buffer.write(data.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _result(msg_id, result):
    _write({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id, code, message):
    _write({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        _result(msg_id, {
            "protocolVersion": requested or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Huawei eNSP automation. Start with ensp_scan_devices, connect with "
                "ensp_connect (wait for the VRP prompt), configure with ensp_run_command, "
                "then ensp_save_config. Verify with ensp_ping using an explicit source "
                "address inside the destination subnet, and ensp_interface_counters. "
                "A pre-planted vrpcfg.zip is never loaded by eNSP."
            ),
        })
        return

    if method in ("notifications/initialized", "initialized"):
        return

    if method == "ping":
        _result(msg_id, {})
        return

    if method == "tools/list":
        _result(msg_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            _error(msg_id, -32602, "unknown tool: %r" % name)
            return
        try:
            text = handler(arguments)
        except (VRPError, ValueError, OSError, KeyError) as exc:
            _result(msg_id, _text_result("%s: %s" % (type(exc).__name__, exc), True))
            return
        except Exception as exc:  # keep the protocol alive, report the bug
            traceback.print_exc(file=sys.stderr)
            _result(msg_id, _text_result("internal error: %s: %s" % (type(exc).__name__, exc), True))
            return
        _result(msg_id, _text_result(text))
        return

    if msg_id is not None:
        _error(msg_id, -32601, "method not found: %r" % method)


def main():
    print("ensp-lab-mcp %s ready on stdio" % SERVER_VERSION, file=sys.stderr)
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _error(None, -32700, "parse error: %s" % exc)
            continue
        if isinstance(msg, list):  # JSON-RPC batch
            for item in msg:
                handle(item)
        else:
            handle(msg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass