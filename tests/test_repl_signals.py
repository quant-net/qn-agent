"""Tests for REPL signal handling and event loop threading model.

These tests verify:
1. Ctrl+C (SIGINT) does NOT trigger deregister — the REPL stays alive
2. Ctrl+D (EOFError) triggers clean shutdown via SIGTERM → deregister
3. 'exit'/'quit' commands trigger clean shutdown
4. Double Ctrl+C within 1 second sends SIGKILL (force exit)
5. The event loop runs in a background thread; REPL runs in the main thread
"""
import asyncio
import os
import signal
import threading
import unittest
from unittest.mock import MagicMock, patch

from quantnet_agent.service.repl import AgentREPL
from quantnet_agent.hal.link_state_table import LinkStateTable


def _make_repl():
    table = LinkStateTable()
    link_mgr = MagicMock()
    link_mgr.hello_interval = 10
    link_mgr.hold_time = 30
    return AgentREPL("test:LBNL-Q:default", link_mgr, table)


class TestReplSigintDoesNotDeregister(unittest.TestCase):
    """Ctrl+C must print ^C and return to the prompt — no shutdown, no deregister."""

    def test_keyboard_interrupt_stays_alive(self):
        """run_sync loop continues after KeyboardInterrupt from Ctrl+C."""
        repl = _make_repl()
        call_count = [0]

        def fake_input(prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                raise KeyboardInterrupt  # first call: Ctrl+C
            return "exit"               # second call: clean exit

        loop = asyncio.new_event_loop()

        shutdown_fn = MagicMock()

        with patch("builtins.input", side_effect=fake_input):
            with patch.object(os, "kill") as mock_kill:
                repl.run_sync(loop, on_shutdown=shutdown_fn)

        # SIGKILL must NOT have been sent (that's double Ctrl+C)
        sigkill_calls = [c for c in mock_kill.call_args_list
                         if c.args[1] == signal.SIGKILL]
        self.assertEqual(sigkill_calls, [], "SIGKILL should not be sent on single Ctrl+C")

        # SIGTERM must have been sent exactly once (from 'exit' path)
        sigterm_calls = [c for c in mock_kill.call_args_list
                         if c.args[1] == signal.SIGTERM]
        self.assertEqual(len(sigterm_calls), 1, "SIGTERM should be sent once on exit")

        # loop ran both inputs (stayed alive after first Ctrl+C)
        self.assertEqual(call_count[0], 2)
        loop.close()

    def test_double_ctrl_c_sends_sigkill(self):
        """Two Ctrl+C within 1 second sends SIGKILL."""
        repl = _make_repl()
        call_count = [0]
        sigkill_sent = [False]

        def fake_input(prompt):
            call_count[0] += 1
            raise KeyboardInterrupt

        def fake_kill(pid, sig):
            if sig == signal.SIGKILL:
                sigkill_sent[0] = True
                # Raise SystemExit so run_sync's finally block fires and the
                # test doesn't loop forever (the real process would die here)
                raise SystemExit(1)
            # SIGTERM from finally block: just record it

        loop = asyncio.new_event_loop()

        with patch("builtins.input", side_effect=fake_input):
            with patch.object(os, "kill", side_effect=fake_kill):
                try:
                    repl.run_sync(loop)
                except SystemExit:
                    pass

        self.assertTrue(sigkill_sent[0], "SIGKILL should fire on double Ctrl+C")
        loop.close()

    def test_ctrl_d_sends_sigterm_not_sigkill(self):
        """Ctrl+D triggers SIGTERM for clean shutdown, not SIGKILL."""
        repl = _make_repl()

        def fake_input(prompt):
            raise EOFError  # Ctrl+D

        loop = asyncio.new_event_loop()

        with patch("builtins.input", side_effect=fake_input):
            with patch.object(os, "kill") as mock_kill:
                repl.run_sync(loop)

        sigterm_calls = [c for c in mock_kill.call_args_list
                         if c.args[1] == signal.SIGTERM]
        sigkill_calls = [c for c in mock_kill.call_args_list
                         if c.args[1] == signal.SIGKILL]
        self.assertEqual(len(sigterm_calls), 1)
        self.assertEqual(sigkill_calls, [])
        loop.close()

    def test_exit_command_sends_sigterm(self):
        """'exit' command sends SIGTERM for clean shutdown."""
        repl = _make_repl()

        def fake_input(prompt):
            return "exit"

        loop = asyncio.new_event_loop()

        with patch("builtins.input", side_effect=fake_input):
            with patch.object(os, "kill") as mock_kill:
                repl.run_sync(loop)

        sigterm_calls = [c for c in mock_kill.call_args_list
                         if c.args[1] == signal.SIGTERM]
        self.assertEqual(len(sigterm_calls), 1)
        loop.close()


class TestReplEventLoopThread(unittest.TestCase):
    """The event loop must run in a thread; run_sync is for the main thread."""

    def test_run_sync_is_not_a_coroutine(self):
        """run_sync is a plain blocking method, not async."""
        repl = _make_repl()
        self.assertFalse(asyncio.iscoroutinefunction(repl.run_sync),
                         "run_sync must be synchronous")

    def test_async_commands_use_provided_loop(self):
        """Async commands (connect, disconnect) run on the provided event loop."""
        repl = _make_repl()
        commands = iter(["connect NEIGHBOR:CH", "exit"])

        def fake_input(prompt):
            return next(commands)

        loop = asyncio.new_event_loop()
        connect_loop = [None]

        async def fake_connect(target):
            connect_loop[0] = asyncio.get_running_loop()

        repl._link_mgr.connect = fake_connect

        # The loop must be running in a thread for run_coroutine_threadsafe to work
        stop_event = asyncio.Event()

        async def _keep_alive():
            await stop_event.wait()

        loop_thread = threading.Thread(
            target=loop.run_until_complete, args=(_keep_alive(),), daemon=True
        )
        loop_thread.start()

        with patch("builtins.input", side_effect=fake_input):
            with patch.object(os, "kill"):
                repl.run_sync(loop)

        # The connect coroutine ran on the provided loop
        self.assertIs(connect_loop[0], loop)
        loop.call_soon_threadsafe(stop_event.set)
        loop_thread.join(timeout=2)
