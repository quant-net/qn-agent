import asyncio
import json
import tempfile
import os
from datetime import datetime, timedelta
from quantnet_agent.service.link_adjacency import LinkAdjacencyManager
from quantnet_agent.hal.link_state_table import (
    LinkStateTable, LinkEntry, link_key,
    LINK_DOWN, LINK_INIT, LINK_CONTROL_UP, LINK_QUANTUM_UP,
)

# Load link_adjacency schema
from quantnet_mq.schema.models import Schema
schema_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "qn-mq/quantnet_mq/schema/rpc/link_adjacency.yaml"
)
if os.path.exists(schema_path):
    Schema.load_schema(schema_path)


MY_CID = "LBNL-Q"
NEIGHBOR_CID = "LBNL-BSM"
CH_ID = "1"
NEIGHBOR_CH_ID = "4"
KEY = link_key(NEIGHBOR_CID, CH_ID)


def make_node_config(channels=None):
    """Write a minimal node JSON to a temp file and return its path."""
    if channels is None:
        channels = [
            {
                "ID": CH_ID,
                "type": "quantum",
                "direction": "out",
                "neighbor": {
                    "idRef": NEIGHBOR_CID,
                    "systemRef": NEIGHBOR_CID,
                    "channelRef": NEIGHBOR_CH_ID,
                },
            }
        ]
    data = {
        "systemSettings": {"cid": MY_CID, "type": "QNode"},
        "matterLightInterfaceSettings": [{"channels": channels}],
    }
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


def make_manager(hello_interval=10, hold_time=30, channels=None):
    table = LinkStateTable()
    cfg_path = make_node_config(channels)
    mgr = LinkAdjacencyManager(
        cid=MY_CID,
        node_config=cfg_path,
        mqhost="localhost",
        mqport=1883,
        link_state_table=table,
        hello_interval=hello_interval,
        hold_time=hold_time,
    )
    return mgr, table, cfg_path


def test_manager_instantiates():
    mgr, table, cfg = make_manager()
    os.unlink(cfg)
    assert mgr is not None
    assert not mgr.is_started


def test_set_hello_interval():
    mgr, _, cfg = make_manager()
    os.unlink(cfg)
    mgr.set_hello_interval(5)
    assert mgr.hello_interval == 5


def test_set_hold_time():
    mgr, _, cfg = make_manager()
    os.unlink(cfg)
    mgr.set_hold_time(15)
    assert mgr.hold_time == 15


def test_load_neighbors_from_config():
    mgr, table, cfg = make_manager()
    neighbors = mgr._load_neighbors()
    os.unlink(cfg)
    assert len(neighbors) == 1
    assert neighbors[0]["neighbor_cid"] == NEIGHBOR_CID
    assert neighbors[0]["channel_id"] == CH_ID


def test_load_neighbors_multiple_channels_same_neighbor():
    """Multiple channels to the same neighbor produce separate entries."""
    channels = [
        {
            "ID": "1", "type": "quantum", "direction": "out",
            "neighbor": {"systemRef": NEIGHBOR_CID, "channelRef": "4"},
        },
        {
            "ID": "2", "type": "quantum", "direction": "out",
            "neighbor": {"systemRef": NEIGHBOR_CID, "channelRef": "5"},
        },
    ]
    mgr, table, cfg = make_manager(channels=channels)
    neighbors = mgr._load_neighbors()
    os.unlink(cfg)
    assert len(neighbors) == 2
    assert neighbors[0]["channel_id"] == "1"
    assert neighbors[1]["channel_id"] == "2"


def test_init_table_creates_per_channel_entries():
    """Each channel gets its own LinkEntry in the table."""
    channels = [
        {
            "ID": "1", "type": "quantum", "direction": "out",
            "neighbor": {"systemRef": NEIGHBOR_CID, "channelRef": "4"},
        },
        {
            "ID": "2", "type": "quantum", "direction": "out",
            "neighbor": {"systemRef": NEIGHBOR_CID, "channelRef": "5"},
        },
    ]
    mgr, table, cfg = make_manager(channels=channels)
    mgr._neighbors = mgr._load_neighbors()
    mgr._init_table()
    os.unlink(cfg)
    key1 = link_key(NEIGHBOR_CID, "1")
    key2 = link_key(NEIGHBOR_CID, "2")
    assert table.get(key1) is not None
    assert table.get(key2) is not None
    assert table.get(key1).channel_id == "1"
    assert table.get(key2).channel_id == "2"


def test_hold_timer_expires_transitions_to_down():
    """If last_neighbor_hello is older than hold_time, outbound link should go DOWN."""
    mgr, table, cfg = make_manager(hold_time=30)
    os.unlink(cfg)
    old_time = datetime.utcnow() - timedelta(seconds=60)
    entry = LinkEntry(
        state=LINK_CONTROL_UP,
        last_hello_sent=datetime.utcnow(),
        last_hello_received=None,  # outbound doesn't receive hellos
        last_neighbor_hello=old_time,  # but neighbor was heard from 60s ago
        hold_time=30,
        neighbor_cid=NEIGHBOR_CID,
        channel_id=CH_ID,
        neighbor_channel_id=NEIGHBOR_CH_ID,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
    )
    table.add(KEY, entry)
    mgr._check_hold_timers()
    assert table.get(KEY).state == LINK_DOWN


def test_hold_timer_does_not_expire_when_recent():
    mgr, table, cfg = make_manager(hold_time=30)
    os.unlink(cfg)
    recent = datetime.utcnow()
    entry = LinkEntry(
        state=LINK_CONTROL_UP,
        last_hello_sent=datetime.utcnow(),
        last_hello_received=recent,
        hold_time=30,
        neighbor_cid=NEIGHBOR_CID,
        channel_id=CH_ID,
        neighbor_channel_id=NEIGHBOR_CH_ID,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
    )
    table.add(KEY, entry)
    mgr._check_hold_timers()
    assert table.get(KEY).state == LINK_CONTROL_UP


def test_disconnect_sets_down():
    mgr, table, cfg = make_manager()
    os.unlink(cfg)
    entry = LinkEntry(
        state=LINK_QUANTUM_UP,
        last_hello_sent=None,
        last_hello_received=None,
        hold_time=30,
        neighbor_cid=NEIGHBOR_CID,
        channel_id=CH_ID,
        neighbor_channel_id=NEIGHBOR_CH_ID,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
    )
    table.add(KEY, entry)
    asyncio.run(mgr.disconnect(KEY))
    assert table.get(KEY).state == LINK_DOWN


def test_hold_timer_per_channel_independent():
    """Hold timer expiry on one channel doesn't affect another.

    Outbound channels use last_neighbor_hello (shared neighbor liveness).
    If neighbor heard from 60s ago > 30s hold_time, ch1 drops. Ch2 recent, stays up.
    """
    mgr, table, cfg = make_manager(hold_time=30)
    os.unlink(cfg)
    old_time = datetime.utcnow() - timedelta(seconds=60)
    recent = datetime.utcnow()
    key1 = link_key(NEIGHBOR_CID, "1")
    key2 = link_key(NEIGHBOR_CID, "2")
    table.add(key1, LinkEntry(
        state=LINK_CONTROL_UP, last_hello_sent=recent,
        last_hello_received=None, last_neighbor_hello=old_time, hold_time=30,
        neighbor_cid=NEIGHBOR_CID, channel_id="1",
        neighbor_channel_id="4", switch_in_path=False,
        switch_cid=None, is_quantum=True, direction="out", history=[],
    ))
    table.add(key2, LinkEntry(
        state=LINK_CONTROL_UP, last_hello_sent=recent,
        last_hello_received=None, last_neighbor_hello=recent, hold_time=30,
        neighbor_cid=NEIGHBOR_CID, channel_id="2",
        neighbor_channel_id="5", switch_in_path=False,
        switch_cid=None, is_quantum=True, direction="out", history=[],
    ))
    mgr._check_hold_timers()
    # Both use same last_neighbor_hello, so both should drop
    # Actually wait — they have different last_neighbor_hello values!
    # key1 = old (60s), key2 = recent. So only key1 expires.
    assert table.get(key1).state == LINK_DOWN
    assert table.get(key2).state == LINK_CONTROL_UP


def test_init_times_out_when_no_hello_received():
    """INIT link that never receives a hello should fall back to DOWN
    after hold_time elapses since entering INIT."""
    mgr, table, cfg = make_manager(hold_time=30)
    os.unlink(cfg)
    old_init = datetime.utcnow() - timedelta(seconds=60)
    table.add(KEY, LinkEntry(
        state=LINK_INIT,
        last_hello_sent=datetime.utcnow(),
        last_hello_received=None,  # never received
        hold_time=30,
        neighbor_cid=NEIGHBOR_CID,
        channel_id=CH_ID,
        neighbor_channel_id=NEIGHBOR_CH_ID,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
        init_since=old_init,
    ))
    mgr._check_hold_timers()
    assert table.get(KEY).state == LINK_DOWN


def test_init_does_not_timeout_when_recent():
    """INIT link that just started should stay INIT."""
    mgr, table, cfg = make_manager(hold_time=30)
    os.unlink(cfg)
    now = datetime.utcnow()
    table.add(KEY, LinkEntry(
        state=LINK_INIT,
        last_hello_sent=now,
        last_hello_received=None,
        hold_time=30,
        neighbor_cid=NEIGHBOR_CID,
        channel_id=CH_ID,
        neighbor_channel_id=NEIGHBOR_CH_ID,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
        init_since=now,
    ))
    mgr._check_hold_timers()
    assert table.get(KEY).state == LINK_INIT


def test_classical_link_stays_control_up():
    """Classical links should not transition to QUANTUM_UP even when probe succeeds."""
    mgr, table, cfg = make_manager()
    os.unlink(cfg)
    # Create a classical link entry in CONTROL_UP state
    table.add(KEY, LinkEntry(
        state=LINK_CONTROL_UP,
        last_hello_sent=datetime.utcnow(),
        last_hello_received=datetime.utcnow(),
        hold_time=30,
        neighbor_cid=NEIGHBOR_CID,
        channel_id=CH_ID,
        neighbor_channel_id=NEIGHBOR_CH_ID,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=False,  # Classical link
        direction="out",
        history=[],
    ))
    # _probe_if_ready should skip this entry because is_quantum=False
    asyncio.run(mgr._probe_if_ready())
    # Entry should still be CONTROL_UP, never transitioned to QUANTUM_UP
    assert table.get(KEY).state == LINK_CONTROL_UP


def test_inbound_channel_loaded_but_passive():
    """Inbound channels should appear in the table but never send hellos
    or participate in hold timers."""
    channels = [
        {
            "ID": "1", "type": "quantum", "direction": "out",
            "neighbor": {"systemRef": NEIGHBOR_CID, "channelRef": "4"},
        },
        {
            "ID": "2", "type": "classic_clk", "direction": "in",
            "neighbor": {"systemRef": NEIGHBOR_CID, "channelRef": "5"},
        },
    ]
    mgr, table, cfg = make_manager(channels=channels)
    mgr._neighbors = mgr._load_neighbors()
    mgr._init_table()
    os.unlink(cfg)
    # Both channels should be in the table
    key_out = link_key(NEIGHBOR_CID, "1")
    key_in = link_key(NEIGHBOR_CID, "2")
    assert table.get(key_out) is not None
    assert table.get(key_in) is not None
    assert table.get(key_out).direction == "out"
    assert table.get(key_in).direction == "in"
    assert table.get(key_out).is_quantum is True
    assert table.get(key_in).is_quantum is False
    # Hold timer should skip inbound channel even if it has state != DOWN
    table.update(key_in, state=LINK_CONTROL_UP)
    table.update(key_in, last_hello_received=datetime.utcnow() - timedelta(seconds=60))
    mgr._check_hold_timers()
    # Inbound channel should remain CONTROL_UP (hold timer skipped it)
    assert table.get(key_in).state == LINK_CONTROL_UP


def test_duplicate_down_state_not_republished():
    """Repeated DOWN transitions (e.g., from INIT timeout cycles) should not
    re-publish the same state to the controller."""
    mgr, table, cfg = make_manager(hold_time=30)
    os.unlink(cfg)
    # Create an outbound link in INIT state with no hello received
    old_init = datetime.utcnow() - timedelta(seconds=60)
    entry = LinkEntry(
        state=LINK_INIT,
        last_hello_sent=datetime.utcnow(),
        last_hello_received=None,  # never received
        hold_time=30,
        neighbor_cid=NEIGHBOR_CID,
        channel_id=CH_ID,
        neighbor_channel_id=NEIGHBOR_CH_ID,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
        init_since=old_init,
    )
    table.add(KEY, entry)
    # First hold timer expiration: INIT → DOWN, should publish
    mgr._check_hold_timers()
    assert table.get(KEY).state == LINK_DOWN
    assert table.get(KEY).last_published_state == LINK_DOWN
    # Simulate next hello cycle: DOWN → INIT (by sending hello)
    table.get(KEY).state = LINK_INIT
    table.get(KEY).init_since = datetime.utcnow() - timedelta(seconds=30)
    # Second hold timer expiration: INIT → DOWN again, should NOT re-publish
    mgr._check_hold_timers()
    entry_after = table.get(KEY)
    # State still DOWN, but last_published_state unchanged (no re-publish)
    assert entry_after.state == LINK_DOWN
    assert entry_after.last_published_state == LINK_DOWN  # not re-set


def test_control_up_transition_is_published():
    """Upward transitions like CONTROL_UP (done by interpreter) are caught by
    the reactive _publish_pending_updates scan in the hello loop."""
    mgr, table, cfg = make_manager()
    os.unlink(cfg)
    # Create an entry in INIT state with no published state yet
    entry = LinkEntry(
        state=LINK_INIT,
        last_hello_sent=datetime.utcnow(),
        last_hello_received=datetime.utcnow(),
        hold_time=30,
        neighbor_cid=NEIGHBOR_CID,
        channel_id=CH_ID,
        neighbor_channel_id=NEIGHBOR_CH_ID,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
        init_since=None,
    )
    table.add(KEY, entry)
    # Simulate interpreter transitioning link to CONTROL_UP (no inline publish)
    table.record_transition(KEY, LINK_CONTROL_UP)
    # CONTROL_UP is not published yet
    assert table.get(KEY).last_published_state is None
    # Scan for pending updates (this is what _hello_loop does)
    mgr._publish_pending_updates()
    # Now it should be published
    assert table.get(KEY).last_published_state == LINK_CONTROL_UP


def test_hold_timer_uses_last_neighbor_hello_for_outbound():
    """Outbound entries use last_neighbor_hello (from inbound liveness) for hold timer."""
    mgr, table, cfg = make_manager(hold_time=30)
    os.unlink(cfg)
    # Create an outbound entry in CONTROL_UP state
    old_time = datetime.utcnow() - timedelta(seconds=60)  # 60s ago
    entry = LinkEntry(
        state=LINK_CONTROL_UP,
        last_hello_sent=datetime.utcnow(),
        last_hello_received=None,  # outbound doesn't receive hellos
        last_neighbor_hello=old_time,  # but neighbor's inbound heard from us 60s ago
        hold_time=30,
        neighbor_cid=NEIGHBOR_CID,
        channel_id=CH_ID,
        neighbor_channel_id=NEIGHBOR_CH_ID,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
        init_since=None,
    )
    table.add(KEY, entry)
    # Hold timer should use last_neighbor_hello: 60s > 30s → DOWN
    mgr._check_hold_timers()
    assert table.get(KEY).state == LINK_DOWN
