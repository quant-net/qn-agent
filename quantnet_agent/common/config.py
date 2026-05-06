import os
import logging
import configobj
import json
import sys
from quantnet_agent.common.constants import Constants

log = logging.getLogger(__name__)


def find_config_file(config_file):
    config_files = []
    if config_file:
        if not os.path.exists(config_file):
            log.error(f"Specified configuration file '{config_file}' does not exist.")
            sys.exit(3)
        return config_file

    if "QUANTNET_HOME" in os.environ:
        config_files.append(f"{os.environ['QUANTNET_HOME']}/etc/agent.cfg")
    else:
        config_files.append("/etc/quantnet/agent.cfg")

    for cf in config_files:
        if os.path.exists(cf):
            return cf
    return None


class Config:
    def __init__(
        self,
        config_file: str = None,
        node_file: str = None,
        debug: bool = False,
        agent_id: str = None,
        mq_broker_host: str = None,
        mq_broker_port: int = None,
        interpreter_path: str = None,
        schema_path: str = None,
    ):
        self.config_file = find_config_file(config_file)

        self._parser = {}
        if self.config_file:
            try:
                self._parser = configobj.ConfigObj(self.config_file)
            except IOError:
                pass

        self.node_file = self._resolve(node_file, "agent", "node_file", None)
        self.mq_broker_host = self._resolve(mq_broker_host, "mq", "host", "127.0.0.1")
        self.mq_broker_port = self._resolve(mq_broker_port, "mq", "port", "1883")

        self.threads = int(self._resolve(None, "agent", "threads", 8))
        self.debug = self._resolve(debug if debug else None, "agent", "debug", False)

        self.cid = self._resolve(agent_id, "agent", "agent_id", None)
        self.interpreter_path = self._resolve(interpreter_path, "interpreters", "path", None)

        if self._parser and "protocols" in self._parser and len(self._parser["protocols"]) > 0:
            if self.interpreter_path is None:
                raise Exception("Interpreter location for protocols is not found")
            self.proto_plugins = self._parser["protocols"]
        else:
            self.proto_plugins = {}

        self.schema_path = self._resolve(schema_path, "schemas", "path", None)

        self.devices = self._parser.get("devices", {}) if self._parser else {}

        self.tasks = []
        self.task_properties = {}

        if self._parser and "tasks" in self._parser:
            for task, property in self._parser["tasks"].items():
                try:
                    if type(property) is configobj.Section:
                        with open(property["path"], "r") as file:
                            calibration_task = json.load(file)
                            calibration_task["_path"] = property["path"]
                            if float(calibration_task["Periodicity"]) <= Constants.SLOTSIZE.total_seconds():
                                raise Exception(
                                    f"Task {task} interval is too short."
                                    "It should be larger than the TDMA slot size "
                                    f"{Constants.SLOTSIZE.total_seconds() * 1e3 }"
                                )
                            self.tasks.append(calibration_task)
                    else:
                        self.task_properties[task] = property
                except Exception as e:
                    log.error(f"Cannot load local task {task} : {e}")

    def _resolve(self, cli_val, section, option, default):
        if cli_val is not None:
            return cli_val
        if not self._parser:
            return default
        try:
            return self._parser[section][option]
        except (configobj.ConfigObjError, KeyError):
            return default

    def get(self, section, option, default=None, **kwargs):
        return self._resolve(None, section, option, default)
