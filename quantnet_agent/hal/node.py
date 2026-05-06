import logging
import importlib.util
import os
import inspect

# import pprint
from quantnet_agent.common.constants import Constants
from abc import ABC
from quantnet_mq.rpcserver import RPCServer
from quantnet_agent.hal.HAL import (
    HardwareAbstractionLayer,
    CMDInterpreter,
    LocalTaskInterpreter,
    CoreInterpreter,
    ScheduleableInterpreter,
)
from quantnet_agent.hal.local_task_manager import LocalTaskManager

builtin_interpreter_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "interpreter")

log = logging.getLogger(__name__)


class Node(ABC):
    def __init__(self, config, scheduler, msgclient):
        self._cid = config.cid
        self._node = config.node_file
        self._mqhost = config.mq_broker_host
        self._mqport = config.mq_broker_port
        self.tasks = config.tasks
        self.devices = config.devices
        self.proto_plugins = config.proto_plugins
        self.interpreter_path = config.interpreter_path
        self._rpcserver = RPCServer(self._cid, topic=f"rpc/{self._cid}", host=self._mqhost, port=self._mqport)
        self._msgclient = msgclient
        self.local_parameters = {}
        self.plugins = {}
        self.core = {}
        self.scheduler = scheduler
        self.hal = HardwareAbstractionLayer(config, self._msgclient)
        self.local_task_manager = LocalTaskManager(
            self._cid, scheduler, self._msgclient, delay=int(config.task_properties.get("traverse_delay", "0"))
        )

        # Built-in interpreters
        for ns, interpreter in Constants.DEFAULT_INTERPRETERS.items():
            self.register_command_namespace(ns, os.path.join(builtin_interpreter_path, interpreter))

        # User-defined interpreters
        for ns, interpreter in self.proto_plugins.items():
            self.register_command_namespace(ns, os.path.join(self.interpreter_path, interpreter), builtin=False)

        # local task interpreters
        for task in self.tasks:
            self.register_command_namespace(Constants.DEFAULT_TASK_NS, Constants.DEFAULT_TASK_INTERPRETER, task=task)

        log.info(f"Agent {self._cid} will load the followings: \n{str(self)}")

    def __str__(self):
        ret = f"{'NAME':<30}{'TYPE':<30}RESOURCE\n"
        ret += f"{''.ljust(60,'-')}\n"
        for name, v in self.devices.items():
            ns = "driver"
            path = v.get("driver")
            ret += f"{name:<30}{ns:<30}{path}\n"
        for name, v in Constants.DEFAULT_INTERPRETERS.items():
            ns = "build-in interpreters"
            path = v
            ret += f"{name:<30}{ns:<30}{path}\n"
        for name, v in self.proto_plugins.items():
            ns = "user-defined interpreters"
            path = v
            ret += f"{name:<30}{ns:<30}{path}\n"
        for task in self.tasks:
            ns = "task"
            name = task["Name"]
            path = task.get("_path", "")
            ret += f"{name:<30}{ns:<30}{path}\n"
        return ret.strip()

    def reset(self):
        pass

    def stop(self):
        self.local_task_manager.stop()
        pass

    def update(self):
        pass

    @property
    def status(self):
        log.debug("Getting local node state")
        return self.local_task_manager.status

    def get_node(self, node_type):
        nodes_module = importlib.import_module("quantnet_agent.hal.node")
        return getattr(nodes_module, node_type)()

    def register_task(self, interpreter, task):
        self.local_task_manager.add_task(task, interpreter)

    def register_command(self, ns, interpreter, builtin, is_core):
        interpreter_obj = interpreter(self) if is_core else interpreter(self.hal)

        for cmd, interpreter_map in interpreter_obj.get_commands().items():

            if builtin:
                log.debug(f"Registering command {cmd} from built-in interpreter{interpreter} in namespace {ns}")
                if is_core:
                    if ns not in self.core:
                        self.core[ns] = {}
                    target = self.core[ns]
                else:
                    if ns not in self.plugins:
                        self.plugins[ns] = {}
                    target = self.plugins[ns]
                target[cmd] = interpreter_map
            else:
                log.debug(f"Registering command {cmd} from user interpreter{interpreter} in namespace {ns}")
                if ns in self.core:
                    if cmd in self.core[ns]:
                        log.error(f"Cannot overwrite Cmd {cmd} from core component {self.core[ns][cmd]}")
                        raise Exception("Duplicated command definition")
                elif ns in self.plugins:
                    log.error(f"Cmd {cmd} is already registered from {self.plugins[ns][cmd]}")
                    raise Exception("Duplicated command definition")
                else:
                    self.plugins[ns] = {}
                    self.plugins[ns][cmd] = interpreter_map
                    log.debug(f"Overwriting command {cmd} with interpreter{interpreter} in namespace {ns}")

            self._rpcserver.set_handler(cmd, interpreter_map[0], interpreter_map[1])

    def register_command_namespace(self, ns, cmd_interpreter, name=None, builtin=True, task=None):
        # TODO: check if module complies with spec

        log.debug(f"Registering namespace {ns}")
        try:
            module_spec = importlib.util.spec_from_file_location(ns, cmd_interpreter)
            plugin_modules = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(plugin_modules)
        except FileNotFoundError:
            log.error(f"Interpreter not found for namespace {ns} at {cmd_interpreter}")
            exit(1)
        for module_name in dir(plugin_modules):
            module = getattr(plugin_modules, module_name)
            if not inspect.isclass(module):
                continue
            if (
                issubclass(module, CMDInterpreter)
                and not module == CMDInterpreter
                and not module == ScheduleableInterpreter
            ):
                self.register_command(ns, module, builtin, False)
                if issubclass(module, ScheduleableInterpreter):
                    self.scheduler.register_command(ns, module(self.hal), self._rpcserver)
            elif issubclass(module, CoreInterpreter) and not module == CoreInterpreter:
                self.register_command(ns, module, builtin, True)
            elif issubclass(module, LocalTaskInterpreter) and not module == LocalTaskInterpreter:
                self.register_task(module(self.hal), task)

    async def start(self):
        await self._rpcserver.start()
        await self.local_task_manager.start()


class QNode(Node):
    def __init__(self, config, scheduler, msgclient):
        super().__init__(config, scheduler, msgclient)

    def sync(self):
        pass


class MNode(Node):
    def __init__(self, config, scheduler, msgclient):
        super().__init__(config, scheduler, msgclient)

    def sync(self):
        pass


class BSMNode(Node):
    def __init__(self, config, scheduler, msgclient):
        super().__init__(config, scheduler, msgclient)

        if "epc" not in config.devices:
            log.error("This agent has no EPC configuration")
            return
        if "polarimeter" not in config.devices:
            log.error("This agent has no polarimeter configuration")
            return

    def sync(self):
        pass


class OpticalSwitch(Node):
    def __init__(self, config, scheduler, msgclient):
        super().__init__(config, scheduler, msgclient)
        self._rpc_calibration_server_handlers = []

    def sync(self):
        pass
