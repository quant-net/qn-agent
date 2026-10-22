import asyncio
import os
import signal
import logging
import threading
import uvloop
import importlib
import json
from types import FrameType
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from quantnet_agent.common.config import Config
from quantnet_agent.service.register import Register
from quantnet_agent.scheduler.scheduler import AgentScheduler
from quantnet_agent.hal.link_state_table import LinkStateTable
from quantnet_agent.service.link_adjacency import LinkAdjacencyManager
from quantnet_agent.service.repl import AgentREPL
from quantnet_mq.schema.models import Schema
from quantnet_mq.msgclient import MsgClient

log = logging.getLogger(__name__)


class QuantnetAgent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.started = False
        self.should_exit = False
        self.force_exit = False
        self.threads = config.threads
        self.msgclient = MsgClient(self.config.cid, host=self.config.mq_broker_host, port=self.config.mq_broker_port)
        self._tpool = ThreadPoolExecutor(self.threads)
        self.scheduler = AgentScheduler(config.cid, self.msgclient)
        self._sreg = None
        self._link_state_table = None
        self._link_mgr = None
        self._repl = None
        self._startup_done = threading.Event()  # signals main thread when startup is complete

    async def handle_exit(self, sig: int, frame: Optional[FrameType]) -> None:
        if self.should_exit:
            return  # idempotent — REPL sends SIGTERM; handle_exit may fire twice
        self.should_exit = True
        await self._sreg.stop()
        if hasattr(self, "_link_mgr") and self._link_mgr is not None:
            await self._link_mgr.stop()

    def run(self) -> None:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

        if self.config.no_repl:
            # No REPL: run event loop in the main thread as before.
            # SIGINT and SIGTERM both trigger clean shutdown.
            asyncio.run(self._serve_no_repl())
            return

        # With REPL: event loop runs in a background thread; REPL owns the main thread.
        # add_signal_handler() requires the main thread, so we use signal.signal()
        # here and bridge into the event loop with call_soon_threadsafe.
        loop = uvloop.new_event_loop()
        asyncio.set_event_loop(loop)

        def _sigterm_handler(sig, frame):
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.handle_exit(sig, frame), loop=loop)
            )

        signal.signal(signal.SIGTERM, _sigterm_handler)

        loop_thread = threading.Thread(
            target=loop.run_until_complete,
            args=(self.serve(),),
            daemon=True,
            name="agent-event-loop",
        )
        loop_thread.start()

        # Wait for startup to complete before handing control to the REPL
        self._startup_done.wait()

        if self._repl is not None:
            self._repl.run_sync(loop)

        loop_thread.join()

    async def _serve_no_repl(self) -> None:
        process_id = os.getpid()
        loop = asyncio.get_event_loop()
        try:
            for signame in ("SIGINT", "SIGTERM"):
                sig = getattr(signal, signame)
                loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self.handle_exit(s, None))
                )
        except NotImplementedError:
            return
        log.info(f"Started agent process [{process_id}]")
        await self.startup()
        if self.should_exit:
            return
        await self.main_loop()
        await self.shutdown()
        log.info(f"Finished agent process [{process_id}]")

    async def serve(self) -> None:
        process_id = os.getpid()
        # SIGTERM is wired in run() via signal.signal() in the main thread
        # (add_signal_handler requires the main thread and can't be used here).
        log.info(f"Started agent process [{process_id}]")

        await self.startup()
        if self.should_exit:
            return
        await self.main_loop()
        await self.shutdown()

        log.info(f"Finished agent process [{process_id}]")

    def load_schema(self, path):
        if not path:
            log.warning("No additional schema path specified, proceeding with defaults")
            return
        Schema.load_schema(path)

    async def startup(self) -> None:
        self.load_schema(self.config.schema_path)
        log.info(f"Agent started with protocol namespaces:\n{Schema()}")
        self.node = self.get_node(json.load(open(self.config.node_file))["systemSettings"]["type"])

        # Create and wire link state table
        self._link_state_table = LinkStateTable()
        self.node.hal.link_state_table = self._link_state_table

        # Create link adjacency manager (also attached to HAL for link.control RPC)
        self._link_mgr = LinkAdjacencyManager(
            cid=self.config.cid,
            node_config=self.config.node_file,
            mqhost=self.config.mq_broker_host,
            mqport=self.config.mq_broker_port,
            link_state_table=self._link_state_table,
            hello_interval=self.config.link_hello_interval,
            hold_time=self.config.link_hold_time,
            node_status_fn=lambda: self.node.status,
        )
        self.node.hal.link_mgr = self._link_mgr
        asyncio.create_task(self._link_mgr.start())

        self._sreg = Register(
            self.config.cid, self.config.node_file, self.config.mq_broker_host, self.config.mq_broker_port,
            self.node._msgclient
        )
        asyncio.create_task(self._sreg.start())

        # Watch for registration and set link manager's registered flag
        async def _watch_registration():
            while not self._sreg.registered:
                await asyncio.sleep(1)
            self._link_mgr.registered = True

        asyncio.create_task(_watch_registration())

        await self.scheduler.start()
        await self.node.start()
        self.started = True

        # Build REPL object so main thread can call run_sync() after startup_done is set.
        if not self.config.no_repl:
            self._repl = AgentREPL(self.config.cid, self._link_mgr, self._link_state_table, config=self.config)

        # Signal main thread that startup is complete
        self._startup_done.set()

    async def main_loop(self) -> None:
        counter = 0
        should_exit = self.should_exit
        while not should_exit:
            counter += 1
            counter = counter % 864000
            await asyncio.sleep(0.1)
            should_exit = self.should_exit

    async def shutdown(self) -> None:
        log.info("Shutting down")

    def get_node(self, node_type):
        nodes_module = importlib.import_module("quantnet_agent.hal.node")
        return getattr(nodes_module, node_type)(self.config, self.scheduler, self.msgclient)
