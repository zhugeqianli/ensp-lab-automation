#!/usr/bin/env python3
"""VRP console session over eNSP's console TCP bridge.

Standard library only (socket/re/time). Any OS, Python 3.8+.

eNSP exposes the console of each running router/switch on 127.0.0.1:<com_port>
(typically 2000, 2001, ...). This module wraps that raw socket in a small
session object with VRP's quirks handled once and for all:

  * every line is terminated with a bare CR -- a stray LF left in the buffer
    instantly answers VRP's y/n prompts, which makes save/reboot confirmation
    fail forever;
  * reads end on an *idle* timeout, not a fixed sleep, because VRP echo
    arrives in bursts;
  * ``---- More ----`` paging is answered with a space while reading, and
    ``screen-length 0 temporary`` is issued before any long ``display``.

Typical use::

    import sys; sys.path.insert(0, "<skill>/scripts")
    from vrp_console import wait_prompt
    con = wait_prompt(2000, name="R1")
    con.cmd("system-view")
    con.cmd("sysname R1")
    con.cmd("interface GigabitEthernet0/0/0")
    con.cmd("ip address 192.168.1.1 255.255.255.0")
    con.cmd("quit")
    con.cmd("ip route-static 192.168.2.0 255.255.255.0 12.1.1.2")
    assert con.save()
    ok, loss, rtt = con.ping("192.168.2.10", source="192.168.1.254", count=4)
"""

import re
import socket
import time

DEFAULT_HOST = "127.0.0.1"
CONSOLE_PORT_MIN = 2000
CONSOLE_PORT_MAX = 2100

# A prompt line ("R1>", "[R1]", "[R1-GigabitEthernet0/0/0]"). The leading
# dots-plus avoids matching an empty buffer, which re.M's $ would otherwise
# accept at position 0 -- that made every read return "" on the first poll.
PROMPT_TAIL = re.compile(r"[^\r\n]+[<>#\]][ \t]*$", re.M)
# PROMPT_TAIL is for a raw buffer (lines end in \r\n, so $ only matches at the
# real end). For ONE stripped line use this anchored form instead: the tail
# class above allows zero characters, so .match() would accept any line.
PROMPT_LINE = re.compile(r"^[\[\(]?[A-Za-z0-9_.\-]+(?:\([^)]*\))?[\]>#][ \t]*$")
PROMPT_AT_START = re.compile(r"^[\[\(]?[A-Za-z0-9_.\-]+(?:\([^)]*\))?[\]>#][ \t]*")
PROMPT_RE = re.compile(r"[\[\(]?([A-Za-z0-9_.\-]+)(?:\([^)]*\))?[\]>#]")
MORE_RE = re.compile(r"----\s*More\s*----")
ERROR_RE = re.compile(
    r"(Error:|Unrecognized command|Incomplete command|Wrong parameter|"
    r"Too many parameters|Ambiguous command)",
    re.IGNORECASE,
)
CONFIRM_RE = re.compile(r"\[Y/N\]|\(y/n\)|\[Y\]|[Yy]/\s*[Nn]")
PING_RE = re.compile(
    r"(\d+)\s+packet\(s\)\s+transmitted\s+(\d+)\s+packet\(s\)\s+received", re.I
)
LOSS_RE = re.compile(r"([\d.]+)%\s*packet\s+loss")
RTT_RE = re.compile(r"round-trip\s+min/avg/max\s*=\s*[\d.]+/([\d.]+)/[\d.]+\s*ms", re.I)
IN_PKT_RE = re.compile(r"Input:\s+(\d+)\s+packets", re.I)
OUT_PKT_RE = re.compile(r"Output:\s+(\d+)\s+packets", re.I)
IFACE_NAME_RE = re.compile(
    r"^(GigabitEthernet|Ethernet|Serial|LoopBack|Vlanif|NULL)", re.I
)


class VRPError(RuntimeError):
    """A console operation failed (timeout, closed socket, command error)."""


def probe(port, host=DEFAULT_HOST, timeout=1.0):
    """True when something is listening on this console port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def scan_ports(host=DEFAULT_HOST, start=CONSOLE_PORT_MIN, end=CONSOLE_PORT_MAX):
    """Console ports with a listener, ascending.

    Probed in parallel: a sequential sweep of 11 ports costs up to 11s when
    nothing is running, and rapid connect/disconnect churn is known to be hard
    on eNSP's VBox server.
    """
    from concurrent.futures import ThreadPoolExecutor
    ports = list(range(int(start), int(end) + 1))
    with ThreadPoolExecutor(max_workers=min(16, len(ports) or 1)) as pool:
        found = [p for p, ok in zip(ports, pool.map(lambda p: probe(p, host), ports)) if ok]
    return sorted(found)


class VRPConsole:
    """One VRP console session over a TCP socket."""

    def __init__(self, port, host=DEFAULT_HOST, connect_timeout=5.0,
                 idle=0.4, settle=0.2):
        self.port = port
        self.host = host
        self.idle = idle
        self.settle = settle
        self.buf = ""
        self.paging_disabled = False
        self.paging_supported = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(connect_timeout)
        self.sock.connect((host, port))
        self.sock.setblocking(False)

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- socket primitives -------------------------------------------------

    def _write(self, text):
        """Send raw text. Commands must end with a bare CR, never CRLF."""
        self.sock.sendall(text.encode("utf-8"))
        time.sleep(self.settle)

    def _pump(self, deadline):
        """Receive until nothing arrives for `idle` seconds or the deadline."""
        last = time.time()
        while True:
            now = time.time()
            if now >= deadline:
                return False
            try:
                chunk = self.sock.recv(65536)
            except BlockingIOError:
                if now - last >= self.idle:
                    return True
                time.sleep(0.02)
                continue
            except OSError as exc:
                raise VRPError("console %d closed: %s" % (self.port, exc))
            if not chunk:
                raise VRPError("console %d closed by eNSP" % self.port)
            self.buf += chunk.decode("utf-8", "replace")
            last = time.time()

    def _answer_paging(self, timeout=10.0):
        """Press space for every pending '---- More ----'. True if one was seen.

        Answered outside the receive loop so a page keystroke can never
        interleave with the command the caller is sending."""
        answered = False
        deadline = time.time() + timeout
        while MORE_RE.search(self.buf):
            self.buf = MORE_RE.sub("", self.buf, count=1)
            self.sock.sendall(b" ")
            answered = True
            self._pump(deadline)
        return answered

    def read_until(self, patterns, timeout=15.0):
        """Read until one of `patterns` matches. Returns (text, match|None).

        Paging prompts met along the way are answered automatically, so long
        displays arrive whole."""
        if isinstance(patterns, (str, re.Pattern)):
            patterns = [patterns]
        compiled = [p if isinstance(p, re.Pattern) else re.compile(p) for p in patterns]
        deadline = time.time() + timeout
        while True:
            for pat in compiled:
                m = pat.search(self.buf)
                if m:
                    text, self.buf = self.buf[:m.end()], self.buf[m.end():]
                    return text, m
            if MORE_RE.search(self.buf) and self._answer_paging():
                continue
            if not self._pump(deadline):
                text, self.buf = self.buf, ""
                return text, None

    def drain(self, timeout=3.0):
        """Collect whatever is still arriving, then clear the buffer."""
        self._pump(time.time() + timeout)
        text, self.buf = self.buf, ""
        return text

    # -- session -----------------------------------------------------------

    def wait_ready(self, timeout=180.0):
        """Wait until VRP answers with a prompt; return the prompt line.

        eNSP opens the TCP bridge as soon as the device starts, well before
        VRP is ready, so this nudges with CR and retries until a prompt
        appears."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._write("\r")
            _, m = self.read_until(
                [PROMPT_TAIL], timeout=min(10.0, max(1.0, deadline - time.time()))
            )
            if m is not None:
                return m.group(0).strip()
        raise VRPError("console %d: no VRP prompt within %.0fs" % (self.port, timeout))

    def prompt(self, timeout=15.0):
        """Return the current prompt line, e.g. 'R1>' or '[R1]'.

        An idle console says nothing, so this nudges it with a bare CR."""
        self._write("\r")
        _, m = self.read_until([PROMPT_TAIL], timeout=timeout)
        if m is None:
            raise VRPError("console %d: no prompt within %.0fs" % (self.port, timeout))
        self.buf = ""
        return m.group(0).strip()

    def hostname(self):
        """Device name as shown in its prompt, e.g. 'R1'."""
        line = self.prompt()
        m = PROMPT_RE.match(line)
        return m.group(1) if m else line

    def disable_paging(self):
        """Try to switch paging off; remember whether VRP accepted it.

        `screen-length` is a VTY-only command: over a console port a switch
        answers "Error: The type of the included user interface must be VTY."
        Paging then has to be answered in-band with a space, which read_until()
        already does, so a rejected screen-length is not an error.
        """
        if self.paging_supported is None:
            body = self.cmd("screen-length 0 temporary", allow_error=True, timeout=10.0)
            self.paging_supported = not ERROR_RE.search(body)
        self.paging_disabled = True
        return self.paging_supported

    def cmd(self, command, timeout=20.0, allow_error=False):
        """Send one command; return its output without echo or prompt."""
        if not self.paging_disabled and command.lower().startswith("display"):
            self.disable_paging()
        # eNSP/VRP 偶发把下一条命令的首字符当回显吃掉（"system-view" 变成 "ystem-view"）：
        # 先发一个裸 CR 把提示符刷新干净再发命令。
        self._write("\r")
        self._pump(time.time() + 0.25)
        self.buf = ""
        self._write(command + "\r")
        text, m = self.read_until([PROMPT_TAIL], timeout=timeout)
        if m is None:
            raise VRPError("console %d: %r timed out after %.0fs"
                           % (self.port, command, timeout))
        body = self._strip_echo(text)
        if not allow_error and ERROR_RE.search(body):
            last = body.strip().splitlines()[-1] if body.strip() else "(no output)"
            raise VRPError("%s -> %s" % (command, last))
        return body

    @staticmethod
    def _strip_echo(text):
        """Drop the echoed command line and the trailing prompt line."""
        lines = text.replace("\r\n", "\n").split("\n")
        if lines and lines[0].strip():
            lines = lines[1:]
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and PROMPT_LINE.match(lines[-1]):
            lines.pop()
        return "\n".join(lines)

    def config_lines(self, lines, timeout=30.0):
        """Send commands in order; raises on the first one VRP rejects."""
        out = []
        for command in lines:
            command = command.strip()
            if not command or command.startswith(("#", "!")):
                continue
            out.append(self.cmd(command, timeout=timeout))
        return out

    def config_block(self, block, timeout=30.0):
        """Send a multi-line config snippet (as it appears in vrpcfg.cfg)."""
        return self.config_lines(block.splitlines(), timeout=timeout)

    def paste(self, lines, timeout=90.0, allow_error=False):
        """Send many commands in ONE write and read once. Returns the output.

        This is the fast path: ``config_lines`` pays an idle timeout per
        command (>0.5s each), so a 40-line config costs ~25s of pure waiting.
        Pasting the whole block makes VRP echo all prompts back-to-back and one
        read collects everything, typically 1-2s for the same block.

        VRP applies the commands in order, so a later command can still fail
        (e.g. an interface that does not exist); the first VRP error line is
        raised unless allow_error is set.
        """
        commands = [str(line).strip() for line in lines]
        commands = [c for c in commands if c and not c.startswith(("#", "!"))]
        if not commands:
            return ""
        # 和 cmd() 同样的坑：分页/回显压缩会把这一批的首字符吃掉，先冲一个裸 CR
        self._write("\r")
        self._pump(time.time() + 0.25)
        self.buf = ""
        self._write("\r".join(commands) + "\r")
        deadline = time.time() + timeout
        started = time.time()
        need = len(commands)
        # VRP answers each pasted line with a prompt. Waiting for that many
        # prompts before treating the console as idle is what keeps a slow
        # device from having its last (or errored) reply read by the NEXT call.
        while time.time() < deadline:
            idle = self._pump(deadline)
            if MORE_RE.search(self.buf):
                self._answer_paging()
                continue
            have = len(PROMPT_RE.findall(self.buf))
            if have >= need and idle:
                self._pump(min(deadline, time.time() + 0.8))
                break
            if idle:
                if time.time() - started > 2.0:
                    break
                time.sleep(0.05)
        text, self.buf = self.buf, ""
        body = self._clean_bulk(text, commands)
        if not allow_error:
            m = ERROR_RE.search(body)
            if m:
                tail = [l for l in body.splitlines() if ERROR_RE.search(l)]
                raise VRPError("paste rejected at %r: %s"
                               % (commands[-1], tail[-1].strip() if tail else m.group(0)))
        return body

    @staticmethod
    def _clean_bulk(text, commands):
        """Drop echoed commands and prompt lines from a pasted block's output."""
        wanted = {c for c in commands}
        keep = []
        for line in text.replace("\r\n", "\n").split("\n"):
            stripped = line.strip()
            # A prompt and the next echoed command can share a line
            # ("R1>system-view") because a prompt carries no trailing newline.
            while True:
                pm = PROMPT_AT_START.match(stripped)
                if not pm:
                    break
                rest = stripped[pm.end():].strip()
                if rest and rest not in wanted and not PROMPT_AT_START.match(rest):
                    break
                stripped = rest
            if not stripped:
                continue
            if stripped in wanted or PROMPT_LINE.match(stripped):
                continue
            if MORE_RE.search(stripped):
                continue
            keep.append(stripped)
        return "\n".join(keep)

    def quiet_logs(self):
        """Stop console log messages so reads stay short and parseable."""
        try:
            self.cmd("undo terminal monitor", allow_error=True, timeout=10.0)
        except VRPError:
            pass

    def save(self, timeout=60.0):
        """`save`, answering the file-name and y/n prompts. True when saved."""
        self._write("save\r")
        text = ""
        for _ in range(4):
            chunk, m = self.read_until([CONFIRM_RE, PROMPT_TAIL], timeout=timeout)
            text += chunk
            if m is None or m.re is PROMPT_TAIL:
                break
            tail = chunk.strip().splitlines()[-1] if chunk.strip() else ""
            # A trailing ':' is the file-name prompt -> press Enter.
            self._write("\r" if tail.rstrip().endswith(":") else "y\r")
        low = text.lower()
        return "successfully" in low or "success" in low

    # -- view / safety -----------------------------------------------------

    def view(self, timeout=10.0):
        """'user' or 'config', classified from the current prompt."""
        return "user" if self.prompt(timeout=timeout).endswith(">") else "config"

    def return_to_user_view(self, hops=4):
        """Leave whatever configuration view the console was left in.

        The console keeps its view between connections and is shared with the
        eNSP GUI, so a batch that starts in interface view would configure the
        wrong node. Returns the prompt that was seen before normalising.
        """
        before = self.prompt()
        for _ in range(hops):
            if self.prompt().endswith(">"):
                break
            self.cmd("return", allow_error=True, timeout=15.0)
        return before

    def backup(self, timeout=60.0):
        """Running configuration as text, for rollback comparisons."""
        return self.cmd("display current-configuration", timeout=timeout)

    def wait_for(self, pattern, command=None, timeout=60.0, interval=2.0):
        """Poll until `pattern` appears in a display output (or in the prompt).

        Use it for asynchronous state (AP joining, OSPF full, BGP up) instead
        of sleeping and re-reading by hand. Returns (matched, last_text).
        """
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            text = self.cmd(command, timeout=60.0) if command else self.prompt()
            if re.search(pattern, text, re.I):
                return True, text
            time.sleep(interval)
        return False, text

    # -- verification ------------------------------------------------------

    def ping(self, target, source=None, count=4, timeout=60.0):
        """Return (ok, loss_percent, rtt_ms). ok means at least one reply."""
        command = "ping"
        if source:
            command += " -a %s" % source
        command += " -c %d %s" % (count, target)
        text = self.cmd(command, timeout=timeout)
        received = 0
        m = PING_RE.search(text)
        if m:
            received = int(m.group(2))
        loss_m = LOSS_RE.search(text)
        loss = float(loss_m.group(1)) if loss_m else (0.0 if received else 100.0)
        rtt_m = RTT_RE.search(text)
        rtt = float(rtt_m.group(1)) if rtt_m else None
        return received > 0, loss, rtt

    def iface_brief(self):
        """{interface: {'ip','mask','physical','protocol','raw'}} from
        `display ip interface brief`."""
        text = self.cmd("display ip interface brief", timeout=60.0)
        out = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 4 or not IFACE_NAME_RE.match(parts[0]):
                continue
            out[parts[0]] = {
                "ip": "" if parts[1].lower().startswith("un") else parts[1],
                "mask": parts[2],
                "physical": parts[3],
                "protocol": parts[4] if len(parts) > 4 else "",
                "raw": line.strip(),
            }
        return out

    def iface_counters(self, interface):
        """(input_packets, output_packets) for one interface."""
        text = self.cmd("display interface %s" % interface, timeout=60.0)
        inp = IN_PKT_RE.search(text)
        outp = OUT_PKT_RE.search(text)
        if not inp or not outp:
            raise VRPError("cannot read counters for %s" % interface)
        return int(inp.group(1)), int(outp.group(1))

    def count_and_ping(self, interface, target, source=None, count=4):
        """Interface counter deltas around one ping.

        Separates 'the frame never arrived' (rx_delta == 0) from 'it arrived
        but was not forwarded' (rx_delta > 0, tx_delta == 0)."""
        before = self.iface_counters(interface)
        ok, loss, rtt = self.ping(target, source=source, count=count)
        after = self.iface_counters(interface)
        return {
            "ok": ok,
            "loss": loss,
            "rtt": rtt,
            "rx_delta": after[0] - before[0],
            "tx_delta": after[1] - before[1],
        }


def wait_prompt(port, name=None, host=DEFAULT_HOST, timeout=180.0):
    """Connect to a console and block until VRP is ready.

    `name` is a safety check: when given, a prompt that names a different
    device raises instead of returning, so a script cannot configure the
    wrong router."""
    con = VRPConsole(port, host=host)
    try:
        con.wait_ready(timeout=timeout)
        if name:
            seen = con.hostname()
            if seen and seen.lower() != name.lower():
                raise VRPError(
                    "console %d answered as %r but %r was expected: wrong device or port"
                    % (port, seen, name)
                )
        con.quiet_logs()
    except Exception:
        con.close()
        raise
    return con
