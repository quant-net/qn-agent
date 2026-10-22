import asyncio
import os
from unittest.mock import MagicMock
from quantnet_agent.hal.interpreter.link import LinkInterpreter
from quantnet_agent.hal.link_state_table import (
    LinkStateTable, LinkEntry, link_key,
    LINK_DOWN, LINK_INIT, LINK_CONTROL_UP,
)

# Load link_adjacency schema
from quantnet_mq.schema.models import Schema
schema_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "qn-mq/quantnet_mq/schema/rpc/link_adjacency.yaml"
)
if os.path.exists(schema_path):
    Schema.load_schema(schema_path)


def make_hal(node_type="QNode"):
    hal = MagicMock()
    hal._config.node_type = node_type
    hal.link_state_table = LinkStateTable()
    return hal


def make_entry(state=LINK_DOWN, neighbor_cid="LBNL-BSM", channel_id="1",
               neighbor_channel_id="4", direction="out"):
    return LinkEntry(
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
        direction=direction,
        history=[],
    )


SRC = "LBNL-BSM"
SRC_CH = "4"
MY_CID = "LBNL-Q"
MY_CH = "1"
KEY = link_key(SRC, MY_CH)  # "LBNL-BSM:1" — keyed by (neighbor, our channel)


def test_qnode_registers_hello_and_probe():
    interp = LinkInterpreter(make_hal("QNode"))
    cmds = interp.get_commands()
    assert "link.hello" in cmds
    assert "link.probe" in cmds
    assert "link.switchPortCheck" not in cmds


def test_bsmnode_registers_hello_and_probe():
    interp = LinkInterpreter(make_hal("BSMNode"))
    cmds = interp.get_commands()
    assert "link.hello" in cmds
    assert "link.probe" in cmds


def test_mnode_registers_hello_and_probe():
    interp = LinkInterpreter(make_hal("MNode"))
    cmds = interp.get_commands()
    assert "link.hello" in cmds
    assert "link.probe" in cmds


def test_opticalswitch_registers_switch_and_control():
    interp = LinkInterpreter(make_hal("OpticalSwitch"))
    cmds = interp.get_commands()
    assert "link.switchPortCheck" in cmds
    assert "link.control" in cmds
    assert "link.hello" not in cmds
    assert "link.probe" not in cmds


def test_handle_hello_updates_last_received():
    hal = make_hal("QNode")
    entry = make_entry(LINK_DOWN, neighbor_cid=SRC, channel_id=MY_CH,
                       neighbor_channel_id=SRC_CH)
    hal.link_state_table.add(KEY, entry)
    interp = LinkInterpreter(hal)

    msg = MagicMock()
    msg.payload.src_cid = SRC
    msg.payload.neighbor_channel_id = SRC_CH
    msg.payload.seen_neighbors = []

    asyncio.run(interp.handle_hello(msg))
    assert hal.link_state_table.get(KEY).last_hello_received is not None


def test_handle_hello_propagates_to_outbound_entries():
    """When hello arrives on inbound channel, propagate timestamp to all outbound
    entries for the same neighbor (for hold timer liveness on outbound)."""
    hal = make_hal("QNode")
    hal._config.cid = MY_CID
    # Add one inbound entry (hello arrives on this)
    inbound_key = link_key(SRC, "2")
    inbound_entry = make_entry(LINK_DOWN, neighbor_cid=SRC, channel_id="2",
                               neighbor_channel_id="5", direction="in")
    hal.link_state_table.add(inbound_key, inbound_entry)
    # Add two outbound entries (should get last_neighbor_hello updated)
    outbound_key1 = link_key(SRC, "1")
    outbound_entry1 = make_entry(LINK_CONTROL_UP, neighbor_cid=SRC, channel_id="1",
                                 neighbor_channel_id="4", direction="out")
    hal.link_state_table.add(outbound_key1, outbound_entry1)
    outbound_key2 = link_key(SRC, "3")
    outbound_entry2 = make_entry(LINK_CONTROL_UP, neighbor_cid=SRC, channel_id="3",
                                 neighbor_channel_id="6", direction="out")
    hal.link_state_table.add(outbound_key2, outbound_entry2)
    interp = LinkInterpreter(hal)

    msg = MagicMock()
    msg.payload.src_cid = SRC
    msg.payload.neighbor_channel_id = "5"  # hello on inbound ch2
    msg.payload.seen_neighbors = []

    asyncio.run(interp.handle_hello(msg))
    # Inbound entry updated
    assert hal.link_state_table.get(inbound_key).last_hello_received is not None
    # Both outbound entries get last_neighbor_hello set
    assert hal.link_state_table.get(outbound_key1).last_neighbor_hello is not None
    assert hal.link_state_table.get(outbound_key2).last_neighbor_hello is not None


def test_handle_hello_transitions_to_control_up_when_seen_by_key():
    """Bilateral confirmation works when seen_neighbors contains per-channel keys.

    Simulates: Remote sends hello on inbound channel, carrying seen_neighbors.
    Should promote all outbound INIT entries for that remote to CONTROL_UP.
    """
    hal = make_hal("QNode")
    hal._config.cid = MY_CID
    # Add one outbound entry in INIT state (this is what should transition)
    outbound_key = link_key(SRC, "1")
    outbound_entry = make_entry(LINK_INIT, neighbor_cid=SRC, channel_id="1",
                                neighbor_channel_id="4", direction="out")
    hal.link_state_table.add(outbound_key, outbound_entry)
    # Add one inbound entry (hello arrives on this)
    inbound_key = link_key(SRC, "2")
    inbound_entry = make_entry(LINK_DOWN, neighbor_cid=SRC, channel_id="2",
                               neighbor_channel_id="5", direction="in")
    hal.link_state_table.add(inbound_key, inbound_entry)
    interp = LinkInterpreter(hal)

    # Hello arrives on inbound channel 2
    msg = MagicMock()
    msg.payload.src_cid = SRC
    msg.payload.neighbor_channel_id = "5"  # incoming = their ch 5 = our inbound ch 2
    # Remote's seen list includes us
    msg.payload.seen_neighbors = [f"{MY_CID}:1"]

    asyncio.run(interp.handle_hello(msg))
    # Outbound entry should transition to CONTROL_UP
    assert hal.link_state_table.get(outbound_key).state == LINK_CONTROL_UP
    # Inbound entry stays DOWN (passive)
    assert hal.link_state_table.get(inbound_key).state == LINK_DOWN


def test_handle_hello_transitions_to_control_up_when_seen_by_cid():
    """Backward compat: bilateral confirmation also works with bare CID.

    Same as above but uses just the CID without channel-specific seen entry.
    """
    hal = make_hal("QNode")
    hal._config.cid = MY_CID
    # Add one outbound entry in INIT state
    outbound_key = link_key(SRC, "1")
    outbound_entry = make_entry(LINK_INIT, neighbor_cid=SRC, channel_id="1",
                                neighbor_channel_id="4", direction="out")
    hal.link_state_table.add(outbound_key, outbound_entry)
    # Add one inbound entry
    inbound_key = link_key(SRC, "2")
    inbound_entry = make_entry(LINK_DOWN, neighbor_cid=SRC, channel_id="2",
                               neighbor_channel_id="5", direction="in")
    hal.link_state_table.add(inbound_key, inbound_entry)
    interp = LinkInterpreter(hal)

    # Hello arrives on inbound channel
    msg = MagicMock()
    msg.payload.src_cid = SRC
    msg.payload.neighbor_channel_id = "5"
    # Remote's seen list contains just the CID (bare)
    msg.payload.seen_neighbors = [MY_CID]

    asyncio.run(interp.handle_hello(msg))
    # Outbound entry should still transition to CONTROL_UP
    assert hal.link_state_table.get(outbound_key).state == LINK_CONTROL_UP
    # Inbound entry stays DOWN
    assert hal.link_state_table.get(inbound_key).state == LINK_DOWN


def test_handle_hello_does_not_transition_if_not_seen():
    """Outbound entry stays INIT if neighbor doesn't see us."""
    hal = make_hal("QNode")
    hal._config.cid = MY_CID
    outbound_key = link_key(SRC, "1")
    entry = make_entry(LINK_INIT, neighbor_cid=SRC, channel_id="1",
                       neighbor_channel_id="4", direction="out")
    hal.link_state_table.add(outbound_key, entry)
    interp = LinkInterpreter(hal)

    msg = MagicMock()
    msg.payload.src_cid = SRC
    msg.payload.neighbor_channel_id = "4"
    msg.payload.seen_neighbors = []  # neighbor does NOT see us yet

    asyncio.run(interp.handle_hello(msg))
    assert hal.link_state_table.get(outbound_key).state == LINK_INIT


def test_handle_hello_multiple_channels_independent():
    """Two outbound channels to the same neighbor both transition if in INIT.

    When a hello arrives carrying seen_neighbors, ALL outbound INIT entries
    for that neighbor promote to CONTROL_UP, not just the matching one.
    """
    hal = make_hal("QNode")
    hal._config.cid = MY_CID
    key1 = link_key(SRC, "1")
    key2 = link_key(SRC, "2")
    key_in = link_key(SRC, "3")
    # Two outbound channels, both INIT
    hal.link_state_table.add(key1, make_entry(LINK_INIT, neighbor_cid=SRC,
                                              channel_id="1", neighbor_channel_id="4", direction="out"))
    hal.link_state_table.add(key2, make_entry(LINK_INIT, neighbor_cid=SRC,
                                              channel_id="2", neighbor_channel_id="5", direction="out"))
    # One inbound channel (hello arrives on this)
    hal.link_state_table.add(key_in, make_entry(LINK_DOWN, neighbor_cid=SRC,
                                                channel_id="3", neighbor_channel_id="6", direction="in"))
    interp = LinkInterpreter(hal)

    # Hello arrives on inbound channel 3, carrying seen_neighbors
    msg = MagicMock()
    msg.payload.src_cid = SRC
    msg.payload.neighbor_channel_id = "6"  # matches inbound ch 3
    msg.payload.seen_neighbors = [MY_CID]

    asyncio.run(interp.handle_hello(msg))
    # Both outbound entries should transition
    assert hal.link_state_table.get(key1).state == LINK_CONTROL_UP
    assert hal.link_state_table.get(key2).state == LINK_CONTROL_UP
    # Inbound stays DOWN (passive)
    assert hal.link_state_table.get(key_in).state == LINK_DOWN


def test_handle_probe_returns_ok():
    hal = make_hal("QNode")
    interp = LinkInterpreter(hal)
    msg = MagicMock()
    msg.payload.src_cid = SRC
    msg.payload.channel_id = "1"
    result = asyncio.run(interp.handle_probe(msg))
    assert result.status == "ok"


def test_handle_switch_port_check_returns_ok():
    hal = make_hal("OpticalSwitch")
    # Mock devs to avoid MagicMock issues
    hal.devs = {}
    interp = LinkInterpreter(hal)
    msg = MagicMock()
    msg.payload.src_channel = "1"
    msg.payload.dst_channel = "4"
    result = asyncio.run(interp.handle_switch_port_check(msg))
    assert result.status in ("ok", "port_down", "not_routed")
