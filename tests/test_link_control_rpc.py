"""Tests for the link.control RPC handler in LinkInterpreter."""

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

from quantnet_agent.hal.interpreter.link import LinkInterpreter
from quantnet_agent.hal.link_state_table import (
    LinkStateTable, LinkEntry, link_key,
    LINK_DOWN, LINK_INIT, LINK_CONTROL_UP, LINK_QUANTUM_UP,
)


NEIGHBOR_A = "BSM-1_2"
NEIGHBOR_B = "QPU-2"
KEY_A = link_key(NEIGHBOR_A, "1")
KEY_B = link_key(NEIGHBOR_B, "3")


def make_hal(node_type="QNode"):
    hal = MagicMock()
    hal._config.node_type = node_type
    hal._config.cid = "QPU-1"
    hal.link_state_table = LinkStateTable()
    hal.link_mgr = MagicMock()
    hal.link_mgr.connect = AsyncMock()
    hal.link_mgr.disconnect = AsyncMock()
    hal.link_mgr._send_probe = AsyncMock()
    hal.link_mgr._check_switch = AsyncMock(return_value=True)
    hal.link_mgr.hello_interval = 10
    hal.link_mgr.hold_time = 30
    return hal


def make_entry(state=LINK_DOWN, neighbor_cid="BSM-1_2", channel_id="1",
               neighbor_channel_id="4", **kwargs):
    defaults = dict(
        state=state,
        last_hello_sent=None,
        last_hello_received=None,
        hold_time=30,
        neighbor_cid=neighbor_cid,
        channel_id=channel_id,
        neighbor_channel_id=neighbor_channel_id,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
    )
    defaults.update(kwargs)
    return LinkEntry(**defaults)


def make_control_msg(action, target=None, param=None, value=None):
    msg = MagicMock()
    msg.payload = MagicMock()
    msg.payload.action = action
    msg.payload.target = target
    msg.payload.param = param
    msg.payload.value = value
    return msg


class TestLinkControlRegistration:
    def test_control_registered_for_qnode(self):
        interp = LinkInterpreter(make_hal("QNode"))
        cmds = interp.get_commands()
        assert "link.control" in cmds

    def test_control_registered_for_bsmnode(self):
        interp = LinkInterpreter(make_hal("BSMNode"))
        cmds = interp.get_commands()
        assert "link.control" in cmds

    def test_control_registered_for_opticalswitch(self):
        interp = LinkInterpreter(make_hal("OpticalSwitch"))
        cmds = interp.get_commands()
        assert "link.control" in cmds


class TestLinkControlShow:
    def test_show_returns_all_entries(self):
        hal = make_hal()
        hal.link_state_table.add(KEY_A, make_entry(LINK_QUANTUM_UP,
                                                   neighbor_cid=NEIGHBOR_A, channel_id="1"))
        hal.link_state_table.add(KEY_B, make_entry(LINK_INIT,
                                                   neighbor_cid=NEIGHBOR_B, channel_id="3"))
        interp = LinkInterpreter(hal)

        msg = make_control_msg("show")
        resp = asyncio.run(interp.handle_control(msg))
        data = resp.as_dict()
        assert data["status"] == "ok"
        links = data["data"]["links"]
        assert KEY_A in links
        assert KEY_B in links
        assert links[KEY_A]["state"] == LINK_QUANTUM_UP
        assert links[KEY_B]["state"] == LINK_INIT

    def test_show_empty_table(self):
        hal = make_hal()
        interp = LinkInterpreter(hal)

        msg = make_control_msg("show")
        resp = asyncio.run(interp.handle_control(msg))
        data = resp.as_dict()
        assert data["status"] == "ok"
        assert data["data"]["links"] == {}

    def test_show_detail_returns_single_entry(self):
        hal = make_hal()
        now = datetime.utcnow()
        hal.link_state_table.add(
            KEY_A,
            make_entry(LINK_CONTROL_UP, neighbor_cid=NEIGHBOR_A,
                       channel_id="1", last_hello_received=now),
        )
        interp = LinkInterpreter(hal)

        msg = make_control_msg("show_detail", target=KEY_A)
        resp = asyncio.run(interp.handle_control(msg))
        data = resp.as_dict()
        assert data["status"] == "ok"
        assert data["data"]["state"] == LINK_CONTROL_UP
        assert data["data"]["channel_id"] == "1"
        assert data["data"]["neighbor"] == NEIGHBOR_A

    def test_show_detail_unknown_returns_error(self):
        hal = make_hal()
        interp = LinkInterpreter(hal)

        msg = make_control_msg("show_detail", target="UNKNOWN:0")
        resp = asyncio.run(interp.handle_control(msg))
        data = resp.as_dict()
        assert data["status"] == "error"


class TestLinkControlActions:
    def test_connect_calls_link_mgr(self):
        hal = make_hal()
        hal.link_state_table.add(KEY_A, make_entry(LINK_DOWN))
        interp = LinkInterpreter(hal)

        msg = make_control_msg("connect", target=KEY_A)
        resp = asyncio.run(interp.handle_control(msg))
        assert resp.as_dict()["status"] == "ok"
        hal.link_mgr.connect.assert_awaited_once_with(KEY_A)

    def test_disconnect_calls_link_mgr(self):
        hal = make_hal()
        hal.link_state_table.add(KEY_A, make_entry(LINK_QUANTUM_UP))
        interp = LinkInterpreter(hal)

        msg = make_control_msg("disconnect", target=KEY_A)
        resp = asyncio.run(interp.handle_control(msg))
        assert resp.as_dict()["status"] == "ok"
        hal.link_mgr.disconnect.assert_awaited_once_with(KEY_A)

    def test_probe_calls_link_mgr(self):
        hal = make_hal()
        entry = make_entry(LINK_CONTROL_UP)
        hal.link_state_table.add(KEY_A, entry)
        interp = LinkInterpreter(hal)

        msg = make_control_msg("probe", target=KEY_A)
        resp = asyncio.run(interp.handle_control(msg))
        assert resp.as_dict()["status"] == "ok"
        hal.link_mgr._send_probe.assert_awaited_once()

    def test_probe_unknown_target_returns_error(self):
        hal = make_hal()
        interp = LinkInterpreter(hal)

        msg = make_control_msg("probe", target="UNKNOWN:0")
        resp = asyncio.run(interp.handle_control(msg))
        assert resp.as_dict()["status"] == "error"


class TestLinkControlSet:
    def test_set_hello_interval(self):
        hal = make_hal()
        interp = LinkInterpreter(hal)

        msg = make_control_msg("set", param="hello-interval", value=5)
        resp = asyncio.run(interp.handle_control(msg))
        assert resp.as_dict()["status"] == "ok"
        hal.link_mgr.set_hello_interval.assert_called_once_with(5)

    def test_set_hold_time(self):
        hal = make_hal()
        interp = LinkInterpreter(hal)

        msg = make_control_msg("set", param="hold-time", value=60)
        resp = asyncio.run(interp.handle_control(msg))
        assert resp.as_dict()["status"] == "ok"
        hal.link_mgr.set_hold_time.assert_called_once_with(60)

    def test_set_unknown_param_returns_error(self):
        hal = make_hal()
        interp = LinkInterpreter(hal)

        msg = make_control_msg("set", param="unknown", value=5)
        resp = asyncio.run(interp.handle_control(msg))
        assert resp.as_dict()["status"] == "error"


class TestLinkControlUnknown:
    def test_unknown_action_returns_error(self):
        hal = make_hal()
        interp = LinkInterpreter(hal)

        msg = make_control_msg("bogus")
        resp = asyncio.run(interp.handle_control(msg))
        assert resp.as_dict()["status"] == "error"
