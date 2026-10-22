from dataclasses import dataclass, field
from datetime import datetime

LINK_DOWN = "DOWN"
LINK_INIT = "INIT"
LINK_CONTROL_UP = "CONTROL_UP"
LINK_QUANTUM_UP = "QUANTUM_UP"


def link_key(neighbor_cid: str, channel_id: str) -> str:
    """Build the canonical table key for a per-channel link entry."""
    return f"{neighbor_cid}:{channel_id}"


@dataclass
class LinkEntry:
    state: str
    last_hello_sent: datetime | None
    last_hello_received: datetime | None
    hold_time: int
    neighbor_cid: str
    channel_id: str
    neighbor_channel_id: str
    switch_in_path: bool
    switch_cid: str | None
    is_quantum: bool  # False for classical channels (capped at CONTROL_UP)
    direction: str    # "out" or "in"; inbound channels are passive (no state)
    history: list = field(default_factory=list)   # list[tuple[datetime, str]]
    init_since: datetime | None = field(default=None)  # when link entered INIT
    last_published_state: str | None = field(default=None)  # suppress duplicate state updates
    last_neighbor_hello: datetime | None = field(default=None)  # last hello from neighbor (any inbound channel)


class LinkStateTable:
    def __init__(self):
        self._entries: dict[str, LinkEntry] = {}  # key = "neighbor_cid:channel_id"

    def add(self, key: str, entry: LinkEntry) -> None:
        self._entries[key] = entry

    def get(self, key: str) -> LinkEntry | None:
        return self._entries.get(key)

    def all(self) -> dict[str, LinkEntry]:
        return dict(self._entries)

    def update(self, key: str, **kwargs) -> None:
        entry = self._entries[key]
        for k, v in kwargs.items():
            setattr(entry, k, v)

    def record_transition(self, key: str, new_state: str) -> None:
        entry = self._entries[key]
        entry.state = new_state
        entry.history.append((datetime.utcnow(), new_state))

    def keys_for_neighbor(self, neighbor_cid: str) -> list[str]:
        """Return all table keys that belong to a given neighbor."""
        prefix = f"{neighbor_cid}:"
        return [k for k in self._entries if k.startswith(prefix)]
