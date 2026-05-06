import socket
import click
import sys
import logging

from quantnet_agent.service import QuantnetAgent
from quantnet_agent.common import Config
from quantnet_agent.common.logging import setup_logging

log = logging.getLogger(__name__)


@click.command("Quantnet Agent")
@click.option("-c", "--config", default=None, help="Main configuration file", show_default=True)
@click.option("-n", "--node-config", default=None, help="Node configuration file", show_default=True)
@click.option("-a", "--agent_id", default=socket.getfqdn(), help="Specify an agent identifier", show_default=True)
@click.option("--mq-broker-host", "mq_broker_host", type=str, help="Message queue broker host", show_default=True)
@click.option("--mq-broker-port", "mq_broker_port", type=int, help="Message queue broker port", show_default=True)
@click.option("-d", "--debug", is_flag=True, show_default=True, default=False, help="Enable debug logging")
@click.option(
    "--interpreter-path",
    "interpreter_path",
    type=str,
    help="Location of additional command interpreters",
    show_default=True,
)
@click.option(
    "--schema-path",
    "schema_path",
    type=str,
    help="Specify a path containing additional schema files",
    show_default=True,
)
def main(config, agent_id, node_config, mq_broker_host, mq_broker_port, debug, interpreter_path, schema_path):
    cobj = Config(config, node_config, debug, agent_id, mq_broker_host, mq_broker_port, interpreter_path, schema_path)

    setup_logging(cobj)

    if cobj.node_file is None:
        log.error("No node configuration file specified. Use --node-config (-n) to provide one.")
        sys.exit(1)

    if cobj.config_file:
        log.info(f"Loaded agent configuration from {cobj.config_file}")
    else:
        log.warning("No agent configuration file found, continuing with defaults")

    agent = QuantnetAgent(cobj)
    agent.run()


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
