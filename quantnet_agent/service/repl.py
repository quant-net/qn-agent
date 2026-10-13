import asyncio
import ctypes
import ctypes.util
import curses
import logging
import os
import readline
import signal
import sys
import threading
import time
from datetime import datetime

from quantnet_agent.hal.link_state_table import (
    LINK_DOWN, LINK_INIT, LINK_CONTROL_UP, LINK_QUANTUM_UP,
)

log = logging.getLogger(__name__)

COMMANDS = [
    "show link", "show logging", "show config", "show switch-check",
    "connect", "disconnect", "probe",
    "set hello-interval", "set hold-time",
    "debug link", "no debug link",
    "logging console", "no logging console",
    "monitor", "help", "exit", "quit",
]


def _make_completer(neighbor_cids, commands):
    def completer(text, state):
        options = [c for c in commands + neighbor_cids if c.startswith(text)]
        return options[state] if state < len(options) else None
    return completer


def _load_readline_lib():
    """Load the native readline/libedit shared library for rl_forced_update_display."""
    try:
        lib_name = ctypes.util.find_library("readline") or ctypes.util.find_library("edit")
        if lib_name:
            return ctypes.cdll.LoadLibrary(lib_name)
    except OSError:
        pass
    return None


_rl_lib = _load_readline_lib()


class REPLLogHandler(logging.Handler):
    """Log handler that prints above the readline prompt without corrupting it.

    When a log record arrives while the user is typing, this handler:
    1. Clears the current terminal line
    2. Prints the formatted log record
    3. Asks readline to redraw the prompt and the user's in-progress input

    The ``waiting`` flag must be set to True while the REPL is blocked on
    ``input()`` and False at all other times.  When False the handler prints
    the log line normally without redrawing a prompt.
    """

    def __init__(self, prompt_fn, fmt=None):
        super().__init__()
        self._prompt_fn = prompt_fn
        self.waiting = False
        if fmt:
            self.setFormatter(fmt)

    def emit(self, record):
        try:
            msg = self.format(record)
            if self.waiting:
                # Clear the current line and print the log message
                sys.stdout.write(f"\r\033[K{msg}\n")
                sys.stdout.flush()
                # Ask readline to redraw the prompt + current input buffer
                if _rl_lib and hasattr(_rl_lib, "rl_forced_update_display"):
                    _rl_lib.rl_on_new_line()
                    _rl_lib.rl_forced_update_display()
                else:
                    # Fallback: redraw bare prompt (input reappears on next keypress)
                    sys.stdout.write(self._prompt_fn())
                    sys.stdout.flush()
            else:
                sys.stdout.write(f"{msg}\n")
                sys.stdout.flush()
        except Exception:
            self.handleError(record)


class AgentREPL:
    def __init__(self, cid: str, link_mgr, link_state_table, config=None):
        self._cid = cid
        self._link_mgr = link_mgr
        self._table = link_state_table
        self._config = config
        self._debug = False
        self._logging_console_enabled = False  # Hide logging by default (like iOS)
        self._node_name = cid.split(":")[-2] if ":" in cid else cid
        self._repl_handler = None
        self._stop_event = threading.Event()  # set to unblock input() on shutdown
        self._last_sigint_time: float = 0.0   # timestamp of last Ctrl+C for double-Ctrl+C detection

    def _setup_readline(self):
        neighbor_cids = list(self._table.all().keys())
        readline.set_completer(
            _make_completer(neighbor_cids, COMMANDS)
        )
        readline.parse_and_bind("tab: complete")

    def _prompt(self):
        return f"quantnet-agent [{self._node_name}]> "

    def _install_log_handler(self):
        """Replace the root logger's stdout handler with a REPL-aware one.

        By default, logging is hidden (CRITICAL level) to keep the REPL clean.
        Users can enable it with 'logging console' command.
        """
        root = logging.getLogger()
        # Find the existing formatter from the current handler
        fmt = None
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout:
                fmt = h.formatter
                break
        self._repl_handler = REPLLogHandler(self._prompt, fmt=fmt)
        # Hide logging by default (like iOS): set to CRITICAL to hide all logs
        self._repl_handler.setLevel(logging.CRITICAL)
        # Remove stdout handlers, add ours
        self._original_handlers = []
        for h in list(root.handlers):
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout:
                self._original_handlers.append(h)
                root.removeHandler(h)
        root.addHandler(self._repl_handler)

    def _uninstall_log_handler(self):
        """Restore the original log handlers."""
        if self._repl_handler is None:
            return
        root = logging.getLogger()
        root.removeHandler(self._repl_handler)
        for h in self._original_handlers:
            root.addHandler(h)
        self._repl_handler = None
        self._original_handlers = []

    # Sentinel values returned by _read_line to distinguish outcomes
    _EOF = object()       # Ctrl+D on empty line → exit cleanly
    _INTERRUPT = object()  # Ctrl+C → cancel current input, stay alive

    def _read_line(self):
        """Read one line from stdin in a background thread.

        Returns:
            str        — the line the user typed
            _EOF       — Ctrl+D on an empty line (clean exit)
            _INTERRUPT — Ctrl+C (cancel current input, stay alive)
            None       — _stop_event was set externally (agent shutdown)
        """
        result = [None]
        done = threading.Event()

        def _reader():
            try:
                result[0] = input(self._prompt())
            except EOFError:
                result[0] = AgentREPL._EOF
            except KeyboardInterrupt:
                result[0] = AgentREPL._INTERRUPT
            finally:
                done.set()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        while not done.is_set():
            if self._stop_event.is_set():
                # Agent is shutting down — unblock input() with a newline
                try:
                    os.write(sys.stdin.fileno(), b"\n")
                except OSError:
                    pass
                done.wait(timeout=1.0)
                break
            done.wait(timeout=0.1)

        return result[0]

    def stop(self):
        """Unblock the input loop on external shutdown (e.g. SIGTERM from another source)."""
        self._stop_event.set()

    async def start(self):
        from quantnet_agent.common.logging import clear_buffered_logs
        self._setup_readline()
        self._install_log_handler()
        clear_buffered_logs()
        loop = asyncio.get_event_loop()
        print("\nAgent ready. Type 'help' for commands.\n")

        try:
            while not self._stop_event.is_set():
                self._repl_handler.waiting = True
                result = await loop.run_in_executor(None, self._read_line)
                self._repl_handler.waiting = False

                if result is None:
                    # External shutdown (stop_event set by handle_exit)
                    break

                if result is AgentREPL._INTERRUPT:
                    # Ctrl+C: cancel current input and stay alive, like every other REPL.
                    # Second Ctrl+C within 1 second: force-exit immediately.
                    now = time.monotonic()
                    if now - self._last_sigint_time < 1.0:
                        print("\nForce exit.")
                        os.kill(os.getpid(), signal.SIGKILL)
                    self._last_sigint_time = now
                    print("^C")
                    continue

                if result is AgentREPL._EOF:
                    # Ctrl+D on an empty line: clean exit with deregister
                    print()
                    break

                line = result.strip()
                if not line:
                    continue
                if await self._dispatch(line):
                    break  # exit/quit command
        finally:
            self._uninstall_log_handler()
            # Send SIGTERM so handle_exit runs deregister and main_loop exits.
            # Safe even if already shutting down — handle_exit is idempotent.
            os.kill(os.getpid(), signal.SIGTERM)

    async def _dispatch(self, line: str) -> bool:
        parts = line.split()
        if not parts:
            return False
        cmd = parts[0]
        args = parts[1:]

        if cmd in ("exit", "quit"):
            print("Shutting down agent...")
            return True

        if cmd == "help" or cmd == "?":
            self._print_help()
        elif cmd == "show":
            if not args or args[0] == "link":
                self._cmd_link_show(args[1:])
            elif args[0] == "logging":
                self._cmd_show_logging()
            elif args[0] == "config":
                self._cmd_show_config()
            elif args[0] == "switch-check" and len(args) >= 2:
                await self._cmd_link_switch_check(args[1])
            else:
                print(f"Unknown show target: {args[0]}")
        elif cmd == "connect" and args:
            await self._cmd_link_connect(args[0])
        elif cmd == "disconnect" and args:
            await self._cmd_link_disconnect(args[0])
        elif cmd == "probe" and args:
            await self._cmd_link_probe(args[0])
        elif cmd == "set" and len(args) >= 2:
            self._cmd_link_set(args[0], args[1])
        elif cmd == "debug" and args and args[0] == "link":
            self._cmd_link_debug("on")
        elif cmd == "no" and len(args) >= 2 and args[0] == "debug" and args[1] == "link":
            self._cmd_link_debug("off")
        elif cmd == "logging" and args and args[0] == "console":
            self._cmd_logging_console("on")
        elif cmd == "no" and len(args) >= 2 and args[0] == "logging" and args[1] == "console":
            self._cmd_logging_console("off")
        elif cmd == "monitor":
            self._cmd_link_monitor()
        else:
            print(f"Unknown command: {line}")

        return False

    def _cmd_link_show(self, args):
        entries = self._table.all()
        if not entries:
            print("No links configured.")
            return
        if args:
            key = args[0]
            entry = self._table.get(key)
            if entry is None:
                print(f"Unknown link: {key}")
                return
            link_type = "quantum" if entry.is_quantum else "classical"
            if entry.direction == "out":
                arrow = f"ch {entry.channel_id} (TX) → ch {entry.neighbor_channel_id} (RX)"
            else:
                arrow = f"ch {entry.channel_id} (RX) ← ch {entry.neighbor_channel_id} (TX)"
            print(f"\nLink: {key}")
            if entry.direction == "out":
                print(f"  State:               {entry.state}")
            print(f"  Neighbor:            {entry.neighbor_cid}")
            print(f"  Direction:           {arrow}")
            print(f"  Link type:           {link_type}")
            if entry.direction == "out":
                print(f"  Switch in path:      {entry.switch_in_path}")
                print(f"  Hold time:           {entry.hold_time}s")
                print(f"  Last hello RX:       {self._age(entry.last_hello_received)}")
                print(f"  Last hello TX:       {self._age(entry.last_hello_sent)}")
                if entry.history:
                    print("  Recent transitions:")
                    for ts, state in entry.history[-10:]:
                        print(f"    {ts.strftime('%H:%M:%S')}  {state}")
            print()
        else:
            print(f"\n{'Link':<35} {'State':<14} {'Type':<11} {'Direction':<17} {'Last RX'}")
            print("-" * 90)
            for key, entry in entries.items():
                link_type = "quantum" if entry.is_quantum else "classical"
                if entry.direction == "out":
                    direction = f"ch{entry.channel_id}→ch{entry.neighbor_channel_id}"
                    state = entry.state
                    last_rx = "—"  # Outbound doesn't receive hellos
                else:
                    direction = f"ch{entry.channel_id}←ch{entry.neighbor_channel_id}"
                    state = "—"
                    last_rx = self._age(entry.last_hello_received)  # Show when last hello arrived
                print(f"{key:<35} {state:<14} {link_type:<11} {direction:<17} {last_rx}")
            print()

    def _age(self, dt) -> str:
        if dt is None:
            return "never"
        secs = (datetime.utcnow() - dt).total_seconds()
        return f"{int(secs)}s ago"

    async def _cmd_link_connect(self, target: str):
        print(f"Triggering adjacency on {target}...")
        await self._link_mgr.connect(target)

    async def _cmd_link_disconnect(self, target: str):
        print(f"Disconnecting {target}...")
        await self._link_mgr.disconnect(target)

    async def _cmd_link_probe(self, target: str):
        entry = self._table.get(target)
        if entry is None:
            print(f"Unknown link: {target}")
            return
        print(f"Running probe on {target}...")
        await self._link_mgr._send_probe(target, entry)

    def _cmd_link_set(self, param: str, value: str):
        try:
            n = int(value)
        except ValueError:
            print(f"Invalid value: {value}")
            return
        if param == "hello-interval":
            self._link_mgr.set_hello_interval(n)
            print(f"Hello interval set to {n}s")
        elif param == "hold-time":
            self._link_mgr.set_hold_time(n)
            print(f"Hold time set to {n}s")
        else:
            print(f"Unknown parameter: {param}. Use hello-interval or hold-time.")

    async def _cmd_link_switch_check(self, target: str):
        entry = self._table.get(target)
        if entry is None:
            print(f"Unknown link: {target}")
            return
        if not entry.switch_in_path or not entry.switch_cid:
            print("No switch in path for this link.")
            return
        ok = await self._link_mgr._check_switch(
            entry.switch_cid, entry.channel_id, entry.neighbor_channel_id
        )
        print(f"Switch port check: {'OK' if ok else 'FAILED'}")

    def _cmd_link_debug(self, onoff: str):
        if onoff == "on":
            self._debug = True
            logging.getLogger("quantnet_agent.service.link_adjacency").setLevel(logging.DEBUG)
            logging.getLogger("quantnet_agent.hal.interpreter.link").setLevel(logging.DEBUG)
            print("Link debug logging enabled.")
        elif onoff == "off":
            self._debug = False
            logging.getLogger("quantnet_agent.service.link_adjacency").setLevel(logging.INFO)
            logging.getLogger("quantnet_agent.hal.interpreter.link").setLevel(logging.INFO)
            print("Link debug logging disabled.")
        else:
            print("Usage: debug link / no debug link")

    def _cmd_show_logging(self):
        """Show current logging console status and buffered logs."""
        from quantnet_agent.common.logging import get_buffered_logs
        status = "enabled" if self._logging_console_enabled else "disabled"
        print(f"Logging to console is {status}.")
        logs = get_buffered_logs(50)  # Show last 50 logs
        if logs:
            print(f"\nRecent logs (last {len(logs)}):")
            print("-" * 80)
            for line in logs:
                print(line)
            print("-" * 80)
        else:
            print("\nNo logs in buffer.")

    def _cmd_show_config(self):
        """Show agent configuration state."""
        import json
        cid = self._config.cid if self._config else "N/A"

        print(f"\nAgent Configuration ({cid}):")
        print("-" * 60)

        # Node info
        print(f"  Node ID                {cid}")
        if self._config:
            print(f"  Worker Threads         {self._config.threads}")
            broker = f"{self._config.mq_broker_host}:{self._config.mq_broker_port}"
            print(f"  Message Broker         {broker}")

        # Load node config for qubits and links
        if self._config and self._config.node_file:
            try:
                with open(self._config.node_file) as f:
                    node_config = json.load(f)

                # Qubits
                qubit_settings = node_config.get("qubitSettings", {})
                qubits = qubit_settings.get("qubits", [])
                if qubits:
                    print(f"\nQubits ({len(qubits)} total):")
                    for qubit in qubits:
                        qubit_id = qubit.get('ID')
                        qubit_type = qubit.get('quantumObject', 'N/A')
                        t1 = qubit.get('T1', {}).get('value', 'N/A')
                        t2 = qubit.get('T2', {}).get('value', 'N/A')
                        print(f"    Qubit {qubit_id}")
                        print(f"      Type               {qubit_type}")
                        print(f"      T1 (decay)         {t1} s")
                        print(f"      T2 (dephasing)     {t2} s")

                # Channels
                channels = node_config.get("channels", [])
                for iface in node_config.get("matterLightInterfaceSettings", []):
                    channels = channels + iface.get("channels", [])
                if channels:
                    print(f"\nChannels ({len(channels)} total):")
                    print("  " + "-" * 56)
                    header = f"  {'Ch':<3} {'Type':<14} {'Dir':<5} {'Neighbor':<30}"
                    print(header)
                    print("  " + "-" * 56)
                    for ch in channels:
                        ch_id = ch.get('ID')
                        ch_type = ch.get("type", "N/A")
                        direction = ch.get("direction", "N/A")[:3]
                        neighbor = ch.get("neighbor", {})
                        neighbor_sys = neighbor.get("systemRef", "?")
                        neighbor_ch = neighbor.get("channelRef", "?")
                        neighbor_str = f"{neighbor_sys}:{neighbor_ch}"
                        line = f"  {ch_id:<3} {ch_type:<14} {direction:<5} {neighbor_str:<30}"
                        print(line)

            except (FileNotFoundError, json.JSONDecodeError) as e:
                print(f"\n  Error loading node config: {e}")

        # Protocol Configuration
        print("\nLink Adjacency Protocol:")
        print(f"  Hello Interval         {self._link_mgr.hello_interval} seconds")
        print(f"  Hold Time              {self._link_mgr.hold_time} seconds")

        # Runtime State
        print("\nRuntime State:")
        debug_status = "Enabled" if self._debug else "Disabled"
        logging_status = "Enabled" if self._logging_console_enabled else "Disabled"
        print(f"  Debug Mode             {debug_status}")
        print(f"  Console Logging        {logging_status}")
        print()

    def _cmd_logging_console(self, onoff: str):
        """Enable or disable logging output to console (like iOS 'logging console')."""
        if onoff == "on":
            self._logging_console_enabled = True
            self._repl_handler.setLevel(logging.INFO)
            print("Logging to console enabled.")
        elif onoff == "off":
            self._logging_console_enabled = False
            self._repl_handler.setLevel(logging.CRITICAL)
            print("Logging to console disabled.")
        else:
            print("Usage: logging console / no logging console")

    def _cmd_link_monitor(self):
        """Enter curses live dashboard mode."""
        try:
            curses.wrapper(self._monitor_dashboard)
        except KeyboardInterrupt:
            pass
        print("Returned to prompt.")

    def _monitor_dashboard(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_CYAN, curses.COLOR_BLACK)

        STATE_COLOR = {
            LINK_QUANTUM_UP: 1,
            LINK_CONTROL_UP: 2,
            LINK_INIT: 4,
            LINK_DOWN: 3,
        }

        while True:
            key = stdscr.getch()
            if key == ord("q"):
                break

            stdscr.clear()
            h, w = stdscr.getmaxyx()
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            title = f" Link Adjacency Monitor ── {self._node_name} ── {now} "
            stdscr.addstr(0, 0, title[:w-1], curses.A_BOLD)
            stdscr.addstr(1, 0, "─" * (w-1))

            # Header row
            stdscr.addstr(2, 0, f"{'Neighbor':<44} {'State':<14} {'Last RX':<12}", curses.A_BOLD)
            stdscr.addstr(3, 0, "─" * (w-1))

            entries = self._table.all()
            row = 4
            split = max(h - 10, row + 2)
            for cid, entry in entries.items():
                if row >= split:
                    break
                color = curses.color_pair(STATE_COLOR.get(entry.state, 0))
                line = f"{cid[:43]:<44} {entry.state:<14} {self._age(entry.last_hello_received):<12}"
                stdscr.addstr(row, 0, line[:w-1], color)
                row += 1

            stdscr.addstr(split, 0, "─" * (w-1))
            stdscr.addstr(split+1, 0, "Recent Events", curses.A_BOLD)

            # Collect recent history from all entries
            events = []
            for cid, entry in entries.items():
                for ts, state in entry.history:
                    events.append((ts, cid, state))
            events.sort(key=lambda x: x[0], reverse=True)

            for i, (ts, cid, state) in enumerate(events[:h - split - 4]):
                color = curses.color_pair(STATE_COLOR.get(state, 0))
                short = cid.split(":")[-2] if ":" in cid else cid
                line = f"  {ts.strftime('%H:%M:%S')}  {short:<20} {state}"
                stdscr.addstr(split + 2 + i, 0, line[:w-1], color)

            stdscr.addstr(h-2, 0, "─" * (w-1))
            stdscr.addstr(h-1, 0, " q: back to prompt ", curses.A_REVERSE)
            stdscr.refresh()

            import time
            time.sleep(1)

    def _print_help(self):
        print("""
Commands:
  show link                      Show all links and their state
  show link <neighbor>           Detailed view of one link
  show logging                   Show console logging status
  show switch-check <neighbor>   Query switch port for a link
  connect <neighbor>             Trigger link adjacency
  disconnect <neighbor>          Bring a link to DOWN
  probe <neighbor>               Trigger quantum probe
  set hello-interval <n>         Set hello interval (seconds)
  set hold-time <n>              Set hold time (seconds)
  debug link                     Enable link debug logging
  no debug link                  Disable link debug logging
  logging console                Enable logging to console
  no logging console             Disable logging to console
  monitor                        Live dashboard (q to exit)
  help                           This message
  exit / quit                    Shut down agent
""")
