"""
Regression tests for dependency ordering in LocalTaskManager.check_state().

Bug: when a parent calibration fails (is out-of-spec), child calibrations that
depend on it are still scheduled via run_immediately() because check_state()
only propagates the out-of-spec state upward through ancestors but does not
prevent the child from being run when its parent is not in_spec.

The tests below reproduce that scenario without any real hardware or message
broker by stubbing out the scheduler and msgclient.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, call

from quantnet_agent.hal.local_task_manager import LocalTaskManager, NodeState
from quantnet_agent.scheduler.scheduler import Allocation
from quantnet_agent.common.calibration_status import Calibration_status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_task(node_id, name, dependency=None):
    return {
        "id": node_id,
        "Name": name,
        "Dependency": dependency,
        "Periodicity": 300,
        "Status_check": None,
        "Lightweight_calibration": None,
        "Full_scale_calibration": {
            "Function": "dummy.py",
            "Class": "DummyCal",
            "Scanning_parameters": "scan",
            "Result_parameters": None,
            "Experiment_parameters": {},
            "Maximum_duration": 10,
            "Analysis_function": None,
        },
    }


def _make_allocation(name, last_exec_offset=None, interval=timedelta(seconds=300)):
    alloc = MagicMock(spec=Allocation)
    alloc.name = name
    alloc.interval = interval
    alloc.status = Calibration_status.FULL
    alloc.job_ids = []
    if last_exec_offset is not None:
        now = datetime.now(timezone.utc)
        exec_time = now + last_exec_offset
        alloc.last_exec = [exec_time, exec_time]
    else:
        alloc.last_exec = None
    return alloc


def _make_ltm(scheduler, msgclient):
    return LocalTaskManager(cid="test-agent", scheduler=scheduler, msgclient=msgclient, delay=0)


def _add_tasks(ltm, tasks):
    for task in tasks:
        ltm.add_task(dict(task), MagicMock())
    ltm.add_dependency()


def _make_mocks():
    sched = MagicMock()
    sched.run_immediately = AsyncMock()
    client = MagicMock()
    client.publish = AsyncMock()
    return sched, client


# ---------------------------------------------------------------------------
# Test 1 – happy path: parent in-spec, child is checked normally
# ---------------------------------------------------------------------------

def test_child_checked_when_parent_in_spec():
    """When the parent task is in-spec, the child's allocation is inspected."""
    sched, client = _make_mocks()
    parent_alloc = _make_allocation("ParentCal", last_exec_offset=timedelta(seconds=-10))
    child_alloc = _make_allocation("ChildCal", last_exec_offset=timedelta(seconds=-10))
    sched.get_allocation.side_effect = lambda name: {
        "ParentCal": parent_alloc,
        "ChildCal": child_alloc,
    }.get(name)

    ltm = _make_ltm(sched, client)
    _add_tasks(ltm, [
        _make_task("parent_id", "ParentCal", dependency=None),
        _make_task("child_id", "ChildCal", dependency=["parent_id"]),
    ])

    _run(ltm.check_state())

    assert sched.get_allocation.call_count >= 2
    sched.run_immediately.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2 – regression: parent fails, child must NOT be run immediately
# ---------------------------------------------------------------------------

def test_child_not_run_when_parent_out_of_spec():
    """
    Regression test for the dependency-violation bug.

    Setup:
      - Parent: last_exec is stale (exceeded its interval → out-of-spec).
      - Child: depends on parent, never ran (last_exec=None).

    Expected (correct) behaviour:
      run_immediately() must NOT be called for the child because its parent
      is not in-spec.

    Observed (buggy) behaviour before fix:
      check_state() iterates via BFS and calls run_immediately() for the child
      because it has no last_exec — without first checking that its parent
      node is already out-of-spec.  This violates the dependency ordering.
    """
    sched, client = _make_mocks()
    parent_alloc = _make_allocation("ParentCal", last_exec_offset=timedelta(seconds=-1000))
    child_alloc = _make_allocation("ChildCal", last_exec_offset=None)
    sched.get_allocation.side_effect = lambda name: {
        "ParentCal": parent_alloc,
        "ChildCal": child_alloc,
    }.get(name)

    ltm = _make_ltm(sched, client)
    _add_tasks(ltm, [
        _make_task("parent_id", "ParentCal", dependency=None),
        _make_task("child_id", "ChildCal", dependency=["parent_id"]),
    ])

    _run(ltm.check_state())

    # Parent node must be marked out-of-spec in the DAG.
    parent_node_id = next(n for n, d in ltm.G.nodes(data=True) if d.get("Name") == "ParentCal")
    assert ltm.G.nodes[parent_node_id]["state"] == NodeState.out_of_spec, (
        "Parent should be out_of_spec after its interval is exceeded"
    )

    # Child must NOT be run while its parent is out-of-spec.
    child_scheduled = any(
        c == call(child_alloc)
        for c in sched.run_immediately.await_args_list
    )
    assert not child_scheduled, (
        "Child was scheduled despite parent being out-of-spec — dependency not honoured"
    )


# ---------------------------------------------------------------------------
# Test 3 – regression: parent never ran, child must NOT be run immediately
# ---------------------------------------------------------------------------

def test_child_not_run_when_parent_never_ran():
    """
    Variant: parent has no allocation at all (get_allocation returns None —
    i.e. the allocation was never pre-registered in the scheduler).
    The child should still be blocked.

    Buggy behaviour: check_state() visits the child via BFS, finds it also has
    no allocation, and calls run_immediately() on it despite the parent being
    uninitialized.
    """
    sched, client = _make_mocks()
    child_alloc = _make_allocation("ChildCal", last_exec_offset=None)
    sched.get_allocation.side_effect = lambda name: {
        "ParentCal": None,
        "ChildCal": child_alloc,
    }.get(name)

    ltm = _make_ltm(sched, client)
    _add_tasks(ltm, [
        _make_task("parent_id", "ParentCal", dependency=None),
        _make_task("child_id", "ChildCal", dependency=["parent_id"]),
    ])

    _run(ltm.check_state())

    child_scheduled = any(
        c == call(child_alloc)
        for c in sched.run_immediately.await_args_list
    )
    assert not child_scheduled, (
        "Child was scheduled even though parent has no allocation — dependency not honoured"
    )


# ---------------------------------------------------------------------------
# Test 4 – three-level chain: grandparent fails, neither child nor grandchild run
# ---------------------------------------------------------------------------

def test_deep_chain_dependency_not_violated():
    """
    A → B → C chain.  A is out-of-spec (stale last_exec).
    Neither B nor C should be scheduled.

    Buggy behaviour: BFS visits B after A, sees B has no allocation, and calls
    run_immediately(B).  Then visits C, calls run_immediately(C).  Both B and C
    are scheduled without A ever completing — clearly wrong.
    The log output of the failing test confirms:
        scheduled_names == ['TaskB', 'TaskC']   (TaskA is NOT in the list)
    """
    sched, client = _make_mocks()
    a_alloc = _make_allocation("TaskA", last_exec_offset=timedelta(seconds=-1000))
    b_alloc = _make_allocation("TaskB", last_exec_offset=None)
    c_alloc = _make_allocation("TaskC", last_exec_offset=None)
    sched.get_allocation.side_effect = lambda name: {
        "TaskA": a_alloc,
        "TaskB": b_alloc,
        "TaskC": c_alloc,
    }.get(name)

    ltm = _make_ltm(sched, client)
    _add_tasks(ltm, [
        _make_task("a", "TaskA", dependency=None),
        _make_task("b", "TaskB", dependency=["a"]),
        _make_task("c", "TaskC", dependency=["b"]),
    ])

    _run(ltm.check_state())

    scheduled_names = [
        c.args[0].name
        for c in sched.run_immediately.await_args_list
        if c.args
    ]
    assert "TaskB" not in scheduled_names, "TaskB should be blocked by out-of-spec TaskA"
    assert "TaskC" not in scheduled_names, "TaskC should be blocked by out-of-spec TaskA"


# ---------------------------------------------------------------------------
# Test 5 – sibling independence: one sibling fails, the other still runs
# ---------------------------------------------------------------------------

def test_sibling_failure_does_not_block_independent_sibling():
    """
    Both TaskX and TaskY depend only on root (no dependency on each other).
    TaskX is out-of-spec; TaskY is in-spec.
    TaskY should not be affected by TaskX's failure.
    """
    sched, client = _make_mocks()
    x_alloc = _make_allocation("TaskX", last_exec_offset=timedelta(seconds=-1000))
    y_alloc = _make_allocation("TaskY", last_exec_offset=timedelta(seconds=-10))
    sched.get_allocation.side_effect = lambda name: {
        "TaskX": x_alloc,
        "TaskY": y_alloc,
    }.get(name)

    ltm = _make_ltm(sched, client)
    _add_tasks(ltm, [
        _make_task("x", "TaskX", dependency=None),
        _make_task("y", "TaskY", dependency=None),
    ])

    _run(ltm.check_state())

    y_node_id = next(n for n, d in ltm.G.nodes(data=True) if d.get("Name") == "TaskY")
    assert ltm.G.nodes[y_node_id]["state"] == NodeState.in_spec, (
        "TaskY should be in_spec even though sibling TaskX failed"
    )


# ---------------------------------------------------------------------------
# Test 6 – rescheduling order: when all tasks fail, parent must be rescheduled
#           before child (topological order, not reversed)
# ---------------------------------------------------------------------------

def test_reschedule_order_respects_dependency_when_all_fail():
    """
    Regression test for reversed rescheduling order.

    Setup: A → B chain; both have stale last_exec (all calibrations failed).

    Expected (correct) behaviour:
      When the fix is applied, run_immediately() should be called for A before B,
      matching topological order so that A has a chance to succeed before B is
      attempted.

    Observed (buggy) behaviour before fix:
      check_state() iterates via BFS (root → A → B).  A is stale so it is
      marked out-of-spec but run_immediately is NOT called for it (the stale
      path, lines 160-172, only sets state and logs).  B has no allocation so
      run_immediately IS called for B (the None path, lines 173-179).
      Result: B is rescheduled while A is not — inverted priority.
    """
    sched, client = _make_mocks()
    a_alloc = _make_allocation("TaskA", last_exec_offset=timedelta(seconds=-1000))
    b_alloc = _make_allocation("TaskB", last_exec_offset=timedelta(seconds=-1000))
    sched.get_allocation.side_effect = lambda name: {
        "TaskA": a_alloc,
        "TaskB": b_alloc,
    }.get(name)

    ltm = _make_ltm(sched, client)
    _add_tasks(ltm, [
        _make_task("a", "TaskA", dependency=None),
        _make_task("b", "TaskB", dependency=["a"]),
    ])

    _run(ltm.check_state())

    scheduled_names = [
        c.args[0].name
        for c in sched.run_immediately.await_args_list
        if c.args
    ]

    # Child must not be rescheduled while parent is out-of-spec.
    assert "TaskB" not in scheduled_names, (
        "TaskB rescheduled before TaskA completed — dependency ordering violated"
    )

    # Parent must be rescheduled so the chain can eventually recover.
    assert "TaskA" in scheduled_names, (
        "TaskA (out-of-spec) was not rescheduled — stale path skips run_immediately"
    )


# ---------------------------------------------------------------------------
# Test 7 – rescheduling order: all-fail with None allocations (no last_exec)
#           must also respect topological order
# ---------------------------------------------------------------------------

def test_reschedule_order_with_no_allocations_respects_dependency():
    """
    Variant: A → B chain; both have allocation=None (never started at all).

    Pass 1 marks A out-of-spec (no allocation). Because A is out-of-spec,
    pass 1 immediately marks B out-of-spec via parent-inheritance without
    even checking its allocation.

    Pass 2 reschedules only A (the frontier node). B is blocked because its
    predecessor A is still out-of-spec.

    run_immediately() is called with None (since get_allocation returns None)
    for TaskA — callers are responsible for passing the correct object; that
    is intentional and matches the existing contract.
    """
    sched, client = _make_mocks()
    sched.get_allocation.side_effect = lambda name: None

    ltm = _make_ltm(sched, client)
    _add_tasks(ltm, [
        _make_task("a", "TaskA", dependency=None),
        _make_task("b", "TaskB", dependency=["a"]),
    ])

    _run(ltm.check_state())

    # run_immediately is mocked here, so the null-guard inside the real implementation
    # is not exercised by this test — what is verified is that Pass 2 calls it exactly
    # once (for TaskA, the frontier) and does not call it for TaskB (blocked by TaskA).
    call_count = sched.run_immediately.await_count
    assert call_count == 1, (
        f"Expected exactly 1 run_immediately call (TaskA only), got {call_count}"
    )
