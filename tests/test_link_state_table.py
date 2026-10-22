from datetime import datetime
from quantnet_agent.hal.link_state_table import (
    LinkEntry, LinkStateTable, link_key,
    LINK_DOWN, LINK_INIT, LINK_CONTROL_UP, LINK_QUANTUM_UP,
)

NEIGHBOR = "LBNL-BSM"
CH = "4"
KEY = link_key(NEIGHBOR, CH)


def make_entry(**kwargs):
    defaults = dict(
        state=LINK_DOWN,
        last_hello_sent=None,
        last_hello_received=None,
        hold_time=30,
        neighbor_cid=NEIGHBOR,
        channel_id="1",
        neighbor_channel_id=CH,
        switch_in_path=False,
        switch_cid=None,
        is_quantum=True,
        direction="out",
        history=[],
    )
    defaults.update(kwargs)
    return LinkEntry(**defaults)


def test_link_key():
    assert link_key("A", "1") == "A:1"


def test_add_and_get():
    t = LinkStateTable()
    entry = make_entry()
    t.add(KEY, entry)
    assert t.get(KEY) is entry


def test_get_missing_returns_none():
    t = LinkStateTable()
    assert t.get("unknown:0") is None


def test_update_state():
    t = LinkStateTable()
    t.add(KEY, make_entry())
    t.update(KEY, state=LINK_INIT)
    assert t.get(KEY).state == LINK_INIT


def test_update_multiple_fields():
    t = LinkStateTable()
    t.add(KEY, make_entry())
    now = datetime.utcnow()
    t.update(KEY, state=LINK_CONTROL_UP, last_hello_received=now)
    e = t.get(KEY)
    assert e.state == LINK_CONTROL_UP
    assert e.last_hello_received == now


def test_all_returns_all_entries():
    t = LinkStateTable()
    t.add("n1:1", make_entry(neighbor_cid="n1", channel_id="1"))
    t.add("n1:2", make_entry(neighbor_cid="n1", channel_id="2"))
    t.add("n2:1", make_entry(neighbor_cid="n2", channel_id="1"))
    assert set(t.all().keys()) == {"n1:1", "n1:2", "n2:1"}


def test_record_transition_updates_state_and_history():
    t = LinkStateTable()
    t.add(KEY, make_entry(state=LINK_DOWN))
    t.record_transition(KEY, LINK_INIT)
    e = t.get(KEY)
    assert e.state == LINK_INIT
    assert len(e.history) == 1
    ts, state = e.history[0]
    assert state == LINK_INIT
    assert isinstance(ts, datetime)


def test_record_transition_accumulates_history():
    t = LinkStateTable()
    t.add(KEY, make_entry(state=LINK_DOWN))
    t.record_transition(KEY, LINK_INIT)
    t.record_transition(KEY, LINK_CONTROL_UP)
    t.record_transition(KEY, LINK_QUANTUM_UP)
    e = t.get(KEY)
    assert e.state == LINK_QUANTUM_UP
    assert [s for _, s in e.history] == [LINK_INIT, LINK_CONTROL_UP, LINK_QUANTUM_UP]


def test_keys_for_neighbor():
    t = LinkStateTable()
    t.add("N1:1", make_entry(neighbor_cid="N1", channel_id="1"))
    t.add("N1:2", make_entry(neighbor_cid="N1", channel_id="2"))
    t.add("N2:1", make_entry(neighbor_cid="N2", channel_id="1"))
    assert sorted(t.keys_for_neighbor("N1")) == ["N1:1", "N1:2"]
    assert t.keys_for_neighbor("N2") == ["N2:1"]
    assert t.keys_for_neighbor("N3") == []
