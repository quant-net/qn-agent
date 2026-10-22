"""Standalone REPL client for remote QNCP agent control.

Connects to the MQTT broker and sends link.control RPCs to a target agent.
Multiple users can connect simultaneously — each gets an independent session.

Usage:
    quantnet_repl -a QPU-1 --mq-host 10.0.0.1
    quantnet_repl -a BSM-1_2 --mq-host controller.local --mq-port 1883
"""

import asyncio
import curses
import json
import logging
import readline
import uuid
from datetime import datetime

import click
import uvloop

from quantnet_agent.common.constants import Constants
from quantnet_mq.rpcclient import RPCClient

log = logging.getLogger(__name__)

COMMANDS = [
    "show link", "show link ",
    "connect ", "disconnect ", "probe ",
    "set hello-interval ", "set hold-time ",
    "show switch-check ",
    "debug link", "no debug link",
    "monitor",
    "help", "exit", "quit",
]


def _make_completer(commands, neighbor_cids):
    all_options = commands + neighbor_cids

    def completer(text, state):
        # Get the full line buffer for context-aware completion
        buf = readline.get_line_buffer()
        options = []
        if any(buf.rstrip().endswith(prefix) for prefix in
               ("connect", "disconnect", "probe", "show link", "show switch-check")):
            # Complete with neighbor CIDs
            options = [c for c in neighbor_cids if c.startswith(text)]
        else:
            options = [c for c in all_options if c.startswith(text)]
        return options[state] if state < len(options) else None

    return completer


class RemoteREPL:
    """Standalone REPL that controls a remote agent via link.control RPC."""

    def __init__(self, agent_id: str, mq_host: str, mq_port: int):
        self._agent_id = agent_id
        self._mq_host = mq_host
        self._mq_port = mq_port
        self._client_id = f"repl-{uuid.uuid4().hex[:8]}"
        self._client = RPCClient(
            self._client_id, host=mq_host, port=mq_port
        )
        self._client.set_handler(
            "link.control", None,
            "quantnet_mq.schema.models.link_adjacency.linkControl",
        )
        self._neighbor_cids: list[str] = []

    def _prompt(self):
        return f"quantnet-agent [{self._agent_id}]> "

    def _setup_readline(self):
        readline.set_completer(_make_completer(COMMANDS, self._neighbor_cids))
        readline.parse_and_bind("tab: complete")
        readline.set_completer_delims(" ")

    async def _call_control(self, action, target=None, param=None, value=None):
        """Send a link.control RPC to the target agent and return parsed response."""
        payload = {"action": action}
        if target is not None:
            payload["target"] = target
        if param is not None:
            payload["param"] = param
        if value is not None:
            payload["value"] = value

        try:
            resp = await self._client.call(
                "link.control",
                payload,
                topic=f"{Constants.LINK_TOPIC_PREFIX}/{self._agent_id}",
                timeout=10.0,
            )
            if isinstance(resp, (str, bytes)):
                return json.loads(resp)
            return resp
        except TimeoutError:
            return {"status": "error", "data": {"message": "RPC timeout — is the agent running?"}}
        except Exception as e:
            return {"status": "error", "data": {"message": str(e)}}

    async def _refresh_neighbors(self):
        """Fetch current neighbor list for tab completion."""
        resp = await self._call_control("show")
        if resp.get("status") == "ok":
            links = resp.get("data", {}).get("links", {})
            self._neighbor_cids = list(links.keys())
            self._setup_readline()

    async def start(self):
        """Connect to MQTT and run the REPL loop."""
        await self._client.start()
        print(f"\nConnected to MQTT broker at {self._mq_host}:{self._mq_port}")
        print(f"Target agent: {self._agent_id}")

        # Try to fetch initial neighbor list (non-fatal if agent isn't running yet)
        # Suppress RPC timeout log noise during initial probe
        rpc_logger = logging.getLogger("quantnet_mq.rpcclient")
        prev_level = rpc_logger.level
        rpc_logger.setLevel(logging.CRITICAL)
        resp = await self._call_control("show")
        rpc_logger.setLevel(prev_level)
        if resp.get("status") == "ok":
            links = resp.get("data", {}).get("links", {})
            self._neighbor_cids = list(links.keys())
            self._setup_readline()
            if self._neighbor_cids:
                print(f"Neighbors: {', '.join(self._neighbor_cids)}")
        else:
            print("Agent not responding yet (will retry on first command)")

        print("\nType 'help' for commands.\n")

        self._setup_readline()
        loop = asyncio.get_event_loop()

        try:
            while True:
                try:
                    line = await loop.run_in_executor(
                        None, lambda: input(self._prompt())
                    )
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                line = line.strip()
                if not line:
                    continue
                if await self._dispatch(line):
                    break
        finally:
            await self._client.stop()

    async def _dispatch(self, line: str) -> bool:
        parts = line.split()
        if not parts:
            return False
        cmd = parts[0]
        args = parts[1:]

        if cmd in ("exit", "quit"):
            print("Disconnecting...")
            return True

        if cmd == "help" or cmd == "?":
            self._print_help()
        elif cmd == "show":
            if not args or args[0] == "link":
                if len(args) >= 2:
                    await self._cmd_show_detail(args[1])
                else:
                    await self._cmd_show()
            elif args[0] == "switch-check" and len(args) >= 2:
                await self._cmd_switch_check(args[1])
            else:
                print(f"Unknown show target: {args[0]}")
        elif cmd == "connect" and args:
            await self._cmd_connect(args[0])
        elif cmd == "disconnect" and args:
            await self._cmd_disconnect(args[0])
        elif cmd == "probe" and args:
            await self._cmd_probe(args[0])
        elif cmd == "set" and len(args) >= 2:
            await self._cmd_set(args[0], args[1])
        elif cmd == "debug" and args and args[0] == "link":
            print("Debug mode is only available on the local agent REPL.")
        elif cmd == "no" and len(args) >= 2 and args[0] == "debug":
            print("Debug mode is only available on the local agent REPL.")
        elif cmd == "monitor":
            await self._cmd_monitor()
        else:
            print(f"Unknown command: {line}")

        return False

    async def _cmd_show(self):
        resp = await self._call_control("show")
        if resp.get("status") != "ok":
            print(f"Error: {resp.get('data', {}).get('message', 'unknown')}")
            return
        links = resp.get("data", {}).get("links", {})
        if not links:
            print("No links configured.")
            return
        # Update tab completion
        self._neighbor_cids = list(links.keys())
        self._setup_readline()

        print(f"\n{'Link':<35} {'State':<14} {'Last RX'}")
        print("-" * 65)
        for key, info in links.items():
            last_rx = self._format_age(info.get("last_hello_received"))
            print(f"{key:<35} {info['state']:<14} {last_rx}")
        print()

    async def _cmd_show_detail(self, target):
        resp = await self._call_control("show_detail", target=target)
        if resp.get("status") != "ok":
            print(f"Error: {resp.get('data', {}).get('message', 'unknown')}")
            return
        data = resp.get("data", {})
        print(f"\nLink: {data.get('key', target)}")
        print(f"  State:               {data.get('state')}")
        print(f"  Neighbor:            {data.get('neighbor')}")
        print(f"  Channel:             {data.get('channel_id')} → {data.get('neighbor_channel_id')}")
        print(f"  Switch in path:      {data.get('switch_in_path')}")
        print(f"  Hold time:           {data.get('hold_time')}s")
        print(f"  Last hello RX:       {self._format_age(data.get('last_hello_received'))}")
        print(f"  Last hello TX:       {self._format_age(data.get('last_hello_sent'))}")
        history = data.get("history", [])
        if history:
            print("  Recent transitions:")
            for h in history[-10:]:
                print(f"    {h['timestamp']}  {h['state']}")
        print()

    async def _cmd_connect(self, target):
        print(f"Triggering adjacency with {target}...")
        resp = await self._call_control("connect", target=target)
        print("OK" if resp.get("status") == "ok" else f"Error: {resp.get('data', {}).get('message')}")

    async def _cmd_disconnect(self, target):
        print(f"Disconnecting {target}...")
        resp = await self._call_control("disconnect", target=target)
        print("OK" if resp.get("status") == "ok" else f"Error: {resp.get('data', {}).get('message')}")

    async def _cmd_probe(self, target):
        print(f"Running probe to {target}...")
        resp = await self._call_control("probe", target=target)
        print("OK" if resp.get("status") == "ok" else f"Error: {resp.get('data', {}).get('message')}")

    async def _cmd_set(self, param, value_str):
        try:
            value = int(value_str)
        except ValueError:
            print(f"Invalid value: {value_str}")
            return
        resp = await self._call_control("set", param=param, value=value)
        if resp.get("status") == "ok":
            print(f"{param} set to {value}")
        else:
            print(f"Error: {resp.get('data', {}).get('message')}")

    async def _cmd_switch_check(self, target):
        resp = await self._call_control("switch_check", target=target)
        if resp.get("status") == "ok":
            ok = resp.get("data", {}).get("switch_ok", False)
            print(f"Switch port check: {'OK' if ok else 'FAILED'}")
        else:
            print(f"Error: {resp.get('data', {}).get('message')}")

    async def _cmd_monitor(self):
        """Live monitor — polls link state every second and displays in curses."""
        try:
            curses.wrapper(lambda stdscr: asyncio.get_event_loop().run_until_complete(
                self._monitor_loop(stdscr)
            ))
        except KeyboardInterrupt:
            pass
        print("Returned to prompt.")

    async def _monitor_loop(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_CYAN, curses.COLOR_BLACK)

        STATE_COLOR = {
            "QUANTUM_UP": 1,
            "CONTROL_UP": 2,
            "INIT": 4,
            "DOWN": 3,
        }

        while True:
            key = stdscr.getch()
            if key == ord("q"):
                break

            # Fetch current state
            resp = await self._call_control("show")
            links = resp.get("data", {}).get("links", {}) if resp.get("status") == "ok" else {}

            stdscr.clear()
            h, w = stdscr.getmaxyx()
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            title = f" Link Adjacency Monitor ── {self._agent_id} ── {now} "
            stdscr.addstr(0, 0, title[:w - 1], curses.A_BOLD)
            stdscr.addstr(1, 0, "─" * (w - 1))

            stdscr.addstr(2, 0, f"{'Neighbor':<44} {'State':<14} {'Last RX':<12}", curses.A_BOLD)
            stdscr.addstr(3, 0, "─" * (w - 1))

            row = 4
            for cid, info in links.items():
                if row >= h - 3:
                    break
                state = info.get("state", "?")
                color = curses.color_pair(STATE_COLOR.get(state, 0))
                last_rx = self._format_age(info.get("last_hello_received"))
                line = f"{cid[:43]:<44} {state:<14} {last_rx:<12}"
                stdscr.addstr(row, 0, line[:w - 1], color)
                row += 1

            stdscr.addstr(h - 2, 0, "─" * (w - 1))
            stdscr.addstr(h - 1, 0, " q: back to prompt ", curses.A_REVERSE)
            stdscr.refresh()

            await asyncio.sleep(1)

    @staticmethod
    def _format_age(iso_str):
        if iso_str is None:
            return "never"
        try:
            dt = datetime.fromisoformat(iso_str)
            secs = (datetime.utcnow() - dt).total_seconds()
            return f"{int(secs)}s ago"
        except (ValueError, TypeError):
            return "?"

    def _print_help(self):
        print(f"""
Remote REPL for agent: {self._agent_id}

Commands:
  show link                      Show all links and their state
  show link <neighbor>           Detailed view of one link
  show switch-check <neighbor>   Query switch port for a link
  connect <neighbor>             Trigger link adjacency
  disconnect <neighbor>          Bring a link to DOWN
  probe <neighbor>               Trigger quantum probe
  set hello-interval <n>         Set hello interval (seconds)
  set hold-time <n>              Set hold time (seconds)
  monitor                        Live dashboard (q to exit)
  help                           This message
  exit / quit                    Disconnect
""")


@click.command("quantnet-repl")
@click.option("-a", "--agent-id", required=True, help="Target agent ID (e.g., QPU-1)")
@click.option("--mq-host", default="127.0.0.1", help="MQTT broker host", show_default=True)
@click.option("--mq-port", default=1883, type=int, help="MQTT broker port", show_default=True)
def main(agent_id, mq_host, mq_port):
    """Connect to a running QNCP agent and control it interactively."""
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    repl = RemoteREPL(agent_id, mq_host, mq_port)
    try:
        asyncio.run(repl.start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
