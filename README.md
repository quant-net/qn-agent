# Quant-Net Agent

The QUANT-NET Control Plane (QNCP) runs as a distributed system. This Agent
registers resources, interfaces with devices, and interprets protocol commands
from a Controller instance. The Agent package includes the concept of extensible
command interpreters, which define the behavior of the Agents when processing
protocol messages and interacting with the underlying hardware or simulation
devices.

The Agent includes a hardware abstraction layer (HAL) that defines a base set of
hardware classes for managing the interaction with quantum devices and external
experiment control frameworks.

## Development Install

Pull requirements and install package in edit mode. 

```
pip3 install -e .
```

The `quantnet_agent` script will be available in your local path, or check `~/.local/bin`

```
$ quantnet_agent --help
Usage: quantnet_agent [OPTIONS]

Options:
  -c, --config TEXT         Main configuration file  [default:
                            ./config/agent.cfg]
  -n, --node-config TEXT    Node configuration file  [default: ~/.quant-
                            net/node.json]
  -a, --agent_id TEXT       Specify an agent identifier  [default: ]
  --mq-broker-host TEXT     Message queue broker host
  --mq-broker-port INTEGER  Message queue broker port
  -d, --debug               Enable debug logging
  --interpreter-path TEXT   Location of additional command interpreters
  --schema-path TEXT        Specify a path containing additional schema files
  --help                    Show this message and exit.
```

## Hardware driver usage

Some hardware drivers rely on optional, driver‑specific Python packages that are **not** installed by default.

Drivers under `quantnet_agent/hal/driver` currently requiring extra dependencies are:

- `artiq.py` → `sipyco` `python-usbtmc`
- `Thorlabs.py` → `ThorlabsPM100`

All such optional development/driver dependencies are listed in `requirements-dev.txt`. To install them all at once:

```bash
pip install -r requirements-dev.txt
```

If you only need a specific driver, install just its package(s), for example:

```bash
pip install sipyco ThorlabsPM100 python-usbtmc
```

## Example usage

An MQTT broker should be available for the agent to connect to. A development docker-compose file that starts an Eclipse Mosquitto instance is available in `quant-net-docker:docker-compose-backend.yml`

Testing node config files exist in `quant-net-mq:quant-net-mq/schemas/examples`

Running the agent:

```
quantnet_agent --mq-broker-host <broker> -n <path_to>/quant-net-mq/quantnet_mq/schema/examples/conf_qnode.json
