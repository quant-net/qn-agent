import logging
import networkx as nx

from quantnet_agent.scheduler.scheduler import Allocation
from datetime import datetime, timezone
from quantnet_agent.common.constants import Constants
from quantnet_mq.schema.models import monitor
from quantnet_agent.common.calibration_status import Calibration_status
import asyncio
from enum import Enum
from collections import deque
from datetime import timedelta
from uuid import uuid4 as uuid

log = logging.getLogger(__name__)


def generate_uuid():
    return str(uuid()).replace("-", "").lower()


def get_nodes_with_attribute(graph, attribute):
    selected_data = {}
    for n, d in graph.nodes().items():
        if attribute in d:
            selected_data[n] = d
    return selected_data


def kahn_topological_sort(graph):
    # Step 1: Calculate in-degree for each node
    in_degree = {node: 0 for node in graph.nodes()}
    for u, v in graph.edges():
        in_degree[v] += 1

    # Step 2: Initialize the queue with nodes having in-degree 0
    zero_in_degree_queue = deque([node for node in graph.nodes() if in_degree[node] == 0])

    topological_order = []

    # Step 3: Process nodes with in-degree 0
    while zero_in_degree_queue:
        node = zero_in_degree_queue.popleft()  # Get a node with in-degree 0
        topological_order.append(node)

        # Step 4: Decrease the in-degree of its neighbors
        for neighbor in graph.successors(node):
            in_degree[neighbor] -= 1  # Decrease the in-degree
            if in_degree[neighbor] == 0:
                zero_in_degree_queue.append(neighbor)  # If in-degree becomes 0, add to queue

    # Step 5: Check if we have a valid topological order (i.e., no cycles)
    if len(topological_order) == len(graph.nodes()):
        return topological_order
    else:
        raise ValueError("Graph has at least one cycle, topological sort is not possible.")


class NodeState(Enum):
    out_of_spec = "OUT_OF_SPEC"
    partial_out_of_spec = "PARTIAL_OUT_OF_SPEC"
    in_spec = "IN_SPEC"


class LocalTaskManager:
    def __init__(self, cid, scheduler, msgclient, delay=5):
        self.cid = cid
        self.G = nx.DiGraph()
        self.tasks = {}
        self._scheduler = scheduler
        log.info("Generating DAG")
        self.G.add_node(0, id="root", state=NodeState.out_of_spec)
        self.is_started = False
        self._msgclient = msgclient
        self.delay = delay

    @property
    def status(self):
        log.debug("Checking root node status in DAG")
        return self.G.nodes(0)["state"]

    async def start(self):
        self.add_dependency()
        await self._msgclient.start()
        await self.start_tasks()
        self.is_started = True
        asyncio.create_task(self.serve())

    async def serve(self):
        prev_state = None
        log.info("Periodically checking state of DAG node")
        while self.is_started:
            await self.check_state()
            current_state = self.G.nodes()[0]["state"]
            if current_state != prev_state:
                await self.report_status("agentState", current_state.value)
                prev_state = current_state
            await asyncio.sleep(Constants.DAG_CHECK_INTERVAL.total_seconds())

    async def stop(self):
        self.is_started = False
        pass

    async def report_status(self, eventType, value):
        msg = monitor.MonitorEvent(
            rid=self.cid, ts=datetime.now(timezone.utc).timestamp(), eventType=eventType, value=value
        )
        await self._msgclient.publish("monitor", msg.as_dict())

    async def check_state(self):
        now = datetime.now(timezone.utc)
        topo_order = kahn_topological_sort(self.G)

        # --- Pass 1: evaluate each node's state top-down ---
        # Walking in topological order means every parent is evaluated before
        # its children, so a failed parent automatically blocks descendants.
        for node in topo_order:
            node_data = self.G.nodes[node]

            if node == 0:
                node_data["state"] = NodeState.in_spec
                if "prev_state" not in node_data:
                    node_data["prev_state"] = NodeState.out_of_spec
                continue

            # If any direct predecessor is not in_spec, inherit that failure.
            if any(
                self.G.nodes[p]["state"] is not NodeState.in_spec
                for p in self.G.predecessors(node)
            ):
                node_data["state"] = NodeState.out_of_spec
                continue

            task_name = node_data["Name"]
            await self.report_status("agentTaskSchedulerPhase", {
                "dag_traversal": "inspect",
                "task_name": task_name,
                "state": node_data["state"].value,
            })

            allocation = self._scheduler.get_allocation(task_name)

            if allocation is not None and allocation.last_exec is not None:
                last_execution = allocation.last_exec[0]
                if last_execution + allocation.interval > now:
                    log.debug(f"task {allocation.name} is in-spec (interval {allocation.interval}).")
                    node_data["state"] = NodeState.in_spec
                    if (
                        allocation.status in (Calibration_status.FULL, Calibration_status.LIGHT)
                        and node_data["Status_check"] is not None
                    ):
                        allocation.status = Calibration_status.CHECK
                        allocation.duration = timedelta(seconds=node_data["Status_check"]["Maximum_duration"])
                        allocation.parameters = [
                            node_data["Status_check"]["Function"],
                            node_data["Status_check"]["Class"],
                            node_data["Status_check"]["Experiment_parameters"],
                            node_data["Status_check"]["Scanning_parameters"],
                        ]
                else:
                    log.warning(
                        f"task {allocation.name} exceeded its interval {allocation.interval}. "
                        f"Last exec: {allocation.last_exec[0]}, scheduled: {allocation.last_exec[1]}. "
                        f"Elapsed since exec: {(now - last_execution).total_seconds():.1f}s"
                    )
                    node_data["state"] = NodeState.out_of_spec
            else:
                log.debug(f"Task {task_name} has no completed execution.")
                node_data["state"] = NodeState.out_of_spec

        # --- Pass 2: reschedule frontier out-of-spec nodes ---
        # A node is a "frontier" when it is out_of_spec but all its parents are
        # in_spec.  That makes it the earliest recoverable node in its chain —
        # reschedule it and leave its descendants blocked until it succeeds.
        for node in topo_order:
            if node == 0:
                continue
            node_data = self.G.nodes[node]
            if node_data["state"] is not NodeState.out_of_spec:
                continue
            if any(
                self.G.nodes[p]["state"] is not NodeState.in_spec
                for p in self.G.predecessors(node)
            ):
                continue

            task_name = node_data["Name"]
            allocation = self._scheduler.get_allocation(task_name)
            log.debug(f"Rescheduling frontier task {task_name}.")
            await self.report_status("agentTaskSchedulerPhase", {
                "dag_traversal": "reschedule",
                "task_name": task_name,
            })
            async with self._scheduler.lock:
                await self._scheduler.run_immediately(allocation)

        # --- Update root state from all non-root nodes ---
        # Using the full topo_order (not just direct children) means a failure
        # anywhere in the tree — not only at depth 1 — is reflected on root.
        all_states = [self.G.nodes[n]["state"] for n in topo_order if n != 0]
        if not all_states or all(s is NodeState.in_spec for s in all_states):
            self.G.nodes[0]["state"] = NodeState.in_spec
        elif all(s is NodeState.out_of_spec for s in all_states):
            self.G.nodes[0]["state"] = NodeState.out_of_spec
        else:
            self.G.nodes[0]["state"] = NodeState.partial_out_of_spec

        await asyncio.sleep(self.delay)

    async def start_tasks(self):
        nodes = kahn_topological_sort(self.G)
        for nodeId in nodes:
            if nodeId == 0:
                continue
            interpreter = self.tasks[nodeId]
            node = self.G.nodes[nodeId]
            # TODO: structure parameters for local task
            parameters = [
                node["Full_scale_calibration"]["Function"],
                node["Full_scale_calibration"]["Class"],
                node["Full_scale_calibration"]["Experiment_parameters"],
                node["Full_scale_calibration"]["Scanning_parameters"],
            ]

            allocation = Allocation(
                node["Name"],
                interpreter.run,
                datetime.now(timezone.utc) + Constants.SCHEDULER_GRACE_PERIOD,
                timedelta(seconds=node["Full_scale_calibration"]["Maximum_duration"]),
                timedelta(seconds=node["Periodicity"]),
                parameters=parameters,
                exp_id=generate_uuid(),
                status=Calibration_status.FULL,
                result_handler=interpreter.receive,
                checking_param=(
                    node["Full_scale_calibration"]["Result_parameters"]
                    if (
                        node["Full_scale_calibration"] is not None
                        and node["Full_scale_calibration"]["Result_parameters"] is not None
                        and type(node["Full_scale_calibration"]["Result_parameters"]) is list
                    )
                    else []
                ),
            )
            await self._scheduler.preallocate(allocation)

    def add_task(self, task, interpreter):
        id = task.pop("id")
        logging.info(f"Adding a local task: {id}")
        if id in [i for i in self.G.nodes()]:
            log.error(f"{task['id']} already exists in local calibration tasks. Ignoring")
            return
        self.G.add_node(id)
        nx.set_node_attributes(self.G, {id: task})
        self.tasks[id] = interpreter
        self.G.nodes[id]["state"] = NodeState.out_of_spec
        self.G.nodes[id]["prev_state"] = None

    def add_dependency(self):
        log.info("Adding dependency in DAG")
        nodes = get_nodes_with_attribute(self.G, "Dependency")

        for id, attr in nodes.items():
            if attr["Dependency"] is None:
                dependents = [0]
            elif isinstance(attr["Dependency"], str):
                dependents = [attr["Dependency"]]
            else:
                dependents = attr["Dependency"]

            for dependent in dependents:
                try:
                    log.debug(f"Adding dependency from {dependent} to {id}")
                    self.G.add_edge(dependent, id)
                except KeyError:
                    log.warning(f"No dependency found for node {id}: {dependent} does not exist")
