import asyncio
import json
import logging
import uuid
from datetime import datetime

from quantnet_mq.rpcclient import RPCClient
from quantnet_agent.common.constants import Constants
from quantnet_agent.hal.link_state_table import (
    LinkStateTable, LinkEntry, link_key,
    LINK_DOWN, LINK_INIT, LINK_CONTROL_UP, LINK_QUANTUM_UP,
)

LINK_TOPIC = Constants.LINK_TOPIC_PREFIX

# RPC client handler definitions (cmd, callback, classpath)
_LINK_RPC_HANDLERS = [
    ("link.hello", None, "quantnet_mq.schema.models.link_adjacency.linkHello"),
    ("link.probe", None, "quantnet_mq.schema.models.link_adjacency.linkProbe"),
    ("link.switchPortCheck", None, "quantnet_mq.schema.models.link_adjacency.linkSwitchPortCheck"),
]

log = logging.getLogger(__name__)


class LinkAdjacencyManager:
    def __init__(
        self,
        cid: str,
        node_config: str,
        mqhost: str,
        mqport: int,
        link_state_table: LinkStateTable,
        hello_interval: int = 10,
        hold_time: int = 30,
        node_status_fn=None,
    ):
        self._cid = cid
        self._node_config = node_config
        self._mqhost = mqhost
        self._mqport = mqport
        self._table = link_state_table
        self.hello_interval = hello_interval
        self.hold_time = hold_time
        self.is_started = False
        self.registered = False
        self._node_status_fn = node_status_fn
        self._client = RPCClient(f"{cid}-link", host=mqhost, port=mqport)
        self._neighbors: list[dict] = []

    def set_hello_interval(self, n: int) -> None:
        self.hello_interval = n

    def set_hold_time(self, n: int) -> None:
        self.hold_time = n
        # propagate to all existing entries
        for entry in self._table.all().values():
            entry.hold_time = n

    def _load_neighbors(self) -> list[dict]:
        """Parse node config JSON and return one entry per channel (in and out)."""
        with open(self._node_config) as f:
            data = json.load(f)

        raw_channels: list[dict] = []
        for iface in data.get("matterLightInterfaceSettings", []):
            for ch in iface.get("channels", []):
                raw_channels.append(ch)
        for ch in data.get("channels", []):
            raw_channels.append(ch)

        # Channel types that support quantum transmission
        QUANTUM_TYPES = {"quantum", "heraldedconnection", "quantumconnection"}

        neighbors = []
        for ch in raw_channels:
            n = ch.get("neighbor", {})
            ref = n.get("systemRef") or n.get("idRef")
            if not ref:
                continue
            ch_type = ch.get("type", "quantum").lower()
            neighbors.append({
                "neighbor_cid": ref,
                "channel_id": ch.get("ID", ""),
                "neighbor_channel_id": n.get("channelRef", ""),
                "switch_in_path": False,
                "switch_cid": None,
                "is_quantum": ch_type in QUANTUM_TYPES,
                "direction": ch.get("direction", "out"),
            })
        return neighbors

    def _init_table(self) -> None:
        for nb in self._neighbors:
            key = link_key(nb["neighbor_cid"], nb["channel_id"])
            if self._table.get(key) is None:
                self._table.add(
                    key,
                    LinkEntry(
                        state=LINK_DOWN,
                        last_hello_sent=None,
                        last_hello_received=None,
                        hold_time=self.hold_time,
                        neighbor_cid=nb["neighbor_cid"],
                        channel_id=nb["channel_id"],
                        neighbor_channel_id=nb["neighbor_channel_id"],
                        switch_in_path=nb["switch_in_path"],
                        switch_cid=nb["switch_cid"],
                        is_quantum=nb["is_quantum"],
                        direction=nb["direction"],
                        history=[],
                    ),
                )

    def _check_hold_timers(self) -> None:
        now = datetime.utcnow()
        for key, entry in self._table.all().items():
            if entry.direction == "in":
                continue  # Inbound channels are passive — no hold timer
            if entry.state == LINK_DOWN:
                continue
            # Outbound entries: check last_neighbor_hello (liveness from any inbound)
            if entry.last_neighbor_hello is not None:
                elapsed = (now - entry.last_neighbor_hello).total_seconds()
            elif entry.init_since is not None:
                # INIT with no neighbor hello: measure from when we entered INIT
                elapsed = (now - entry.init_since).total_seconds()
            else:
                continue
            if elapsed > entry.hold_time:
                log.warning(
                    "Hold timer expired for %s (%.1fs > %ds) → DOWN",
                    key, elapsed, entry.hold_time,
                )
                self._table.record_transition(key, LINK_DOWN)
                self._table.update(key, init_since=None)
                self._publish_state_update(key, entry, LINK_DOWN)

    def _publish_state_update(self, key: str, entry: LinkEntry, state: str) -> None:
        # Suppress duplicate state updates (e.g., repeated DOWN from INIT timeout cycles)
        if state == entry.last_published_state:
            return
        # Fire-and-forget publish to monitor topic using MonitorEvent format
        try:
            from quantnet_mq.schema.models import monitor
            event = monitor.MonitorEvent(
                rid=self._cid,
                ts=datetime.utcnow().timestamp(),
                eventType="linkStateUpdate",
                value={
                    "src_cid": self._cid,
                    "dst_cid": entry.neighbor_cid,
                    "channel_id": entry.channel_id,
                    "state": state,
                },
            )
            mqtt = self._client._mqttclient
            if mqtt:
                mqtt.publish("monitor", event.serialize(), qos=1)
            # Update last published state after successful publish attempt
            entry.last_published_state = state
        except Exception as e:
            log.debug("Could not publish link_state_update: %s", e)

    def _publish_pending_updates(self) -> None:
        """Scan table for state changes that haven't been published yet."""
        for key, entry in self._table.all().items():
            if entry.direction == "in":
                continue
            if entry.state != entry.last_published_state:
                self._publish_state_update(key, entry, entry.state)

    async def _send_hello(self, key: str) -> None:
        entry = self._table.get(key)
        if entry is None:
            return
        if entry.direction == "in":
            return  # Inbound channels are passive — never send hellos
        seen = [
            k for k, e in self._table.all().items()
            if e.last_hello_received is not None
        ]
        try:
            payload = {
                "src_cid": self._cid,
                "dst_cid": entry.neighbor_cid,
                "channel_id": entry.channel_id,
                "neighbor_channel_id": entry.neighbor_channel_id,
                "seen_neighbors": seen,
                "hello_interval": self.hello_interval,
                "hold_time": self.hold_time,
                "timestamp": datetime.utcnow().isoformat(),
            }
            resp = await self._client.call(
                "link.hello", payload, topic=f"{LINK_TOPIC}/{entry.neighbor_cid}"
            )
            resp = json.loads(resp) if isinstance(resp, (str, bytes)) else resp
            self._table.update(key, last_hello_sent=datetime.utcnow())
            if entry.state == LINK_DOWN:
                self._table.record_transition(key, LINK_INIT)
                self._table.update(key, init_since=datetime.utcnow())
        except Exception as e:
            log.warning("Hello to %s failed: %s", key, e)

    async def _probe_if_ready(self) -> None:
        """After each hello loop tick, check if any link is CONTROL_UP and probe it.

        Quantum probes are only attempted when the local agent is IN_SPEC,
        meaning all calibration tasks have passed.
        """
        if self._node_status_fn is not None:
            from quantnet_agent.hal.local_task_manager import NodeState
            try:
                status = self._node_status_fn()
            except Exception:
                log.debug("Skipping quantum probe — could not read agent status")
                return
            if status is not NodeState.in_spec:
                log.debug("Skipping quantum probe — agent not IN_SPEC (current: %s)", status.value)
                return
        for key, entry in self._table.all().items():
            if entry.direction == "in":
                continue  # Inbound channels are passive
            if entry.state != LINK_CONTROL_UP:
                continue
            # Classical links stay at CONTROL_UP; only quantum links attempt probe
            if not entry.is_quantum:
                log.debug("Skipping quantum probe for classical link %s", key)
                continue
            if entry.switch_in_path and entry.switch_cid:
                ok = await self._check_switch(entry.switch_cid, entry.channel_id, entry.neighbor_channel_id)
                if not ok:
                    log.info("Switch port check failed for %s, staying CONTROL_UP", key)
                    continue
            await self._send_probe(key, entry)

    async def _check_switch(self, switch_cid: str, src_ch: str, dst_ch: str) -> bool:
        try:
            payload = {
                "src_channel": src_ch,
                "dst_channel": dst_ch,
            }
            resp = await self._client.call(
                "link.switchPortCheck", payload, topic=f"{LINK_TOPIC}/{switch_cid}"
            )
            resp = json.loads(resp) if isinstance(resp, (str, bytes)) else resp
            return resp.get("status") == "ok"
        except Exception as e:
            log.warning("Switch check failed: %s", e)
            return False

    async def _send_probe(self, key: str, entry: LinkEntry) -> None:
        try:
            payload = {
                "src_cid": self._cid,
                "dst_cid": entry.neighbor_cid,
                "channel_id": entry.channel_id,
                "probe_id": str(uuid.uuid4()),
            }
            resp = await self._client.call(
                "link.probe", payload, topic=f"{LINK_TOPIC}/{entry.neighbor_cid}"
            )
            resp = json.loads(resp) if isinstance(resp, (str, bytes)) else resp
            if resp and resp.get("status") == "ok":
                log.info("Probe passed for %s → QUANTUM_UP", key)
                self._table.record_transition(key, LINK_QUANTUM_UP)
                self._publish_state_update(key, entry, LINK_QUANTUM_UP)
        except Exception as e:
            log.warning("Probe to %s failed: %s", key, e)

    async def _hello_loop(self) -> None:
        while self.is_started:
            keys = list(self._table.all().keys())
            await asyncio.gather(*(self._send_hello(k) for k in keys))
            self._check_hold_timers()
            await self._probe_if_ready()
            self._publish_pending_updates()
            await asyncio.sleep(self.hello_interval)

    async def start(self) -> None:
        self._neighbors = self._load_neighbors()
        self._init_table()
        for cmd, cb, classpath in _LINK_RPC_HANDLERS:
            self._client.set_handler(cmd, cb, classpath)
        await self._client.start()
        # Wait for registration before sending first hello
        while not self.registered:
            await asyncio.sleep(1)
        self.is_started = True
        log.info("LinkAdjacencyManager started, %d link(s)", len(self._neighbors))
        asyncio.create_task(self._hello_loop())

    async def stop(self) -> None:
        self.is_started = False
        log.info("LinkAdjacencyManager stopped")

    async def connect(self, key: str) -> None:
        """Manually trigger adjacency on one link."""
        entry = self._table.get(key)
        if entry and entry.state == LINK_DOWN:
            self._table.record_transition(key, LINK_INIT)
            self._table.update(key, init_since=datetime.utcnow())
        await self._send_hello(key)

    async def disconnect(self, key: str) -> None:
        """Manually bring one link to DOWN."""
        entry = self._table.get(key)
        if entry:
            self._table.record_transition(key, LINK_DOWN)
            self._table.update(key, init_since=None)
            self._publish_state_update(key, entry, LINK_DOWN)
