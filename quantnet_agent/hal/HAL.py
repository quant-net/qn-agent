import logging
import ast
import os
import importlib
import sys
from abc import ABC, abstractmethod
import traceback
from pydantic import TypeAdapter

log = logging.getLogger(__name__)
driver_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver")


def get_driver_module(classname):
    for filename in os.listdir(driver_path):
        f_path = os.path.join(driver_path, filename)
        if not os.path.isfile(f_path):
            continue
        with open(f_path) as file:
            node = ast.parse(file.read())
            classes = [n for n in node.body if isinstance(n, ast.ClassDef)]
            for class_ in classes:
                if classname == class_.name:
                    try:
                        spec = importlib.util.spec_from_file_location(
                            os.path.splitext(filename)[0], os.path.join(driver_path, filename)
                        )
                        module = importlib.util.module_from_spec(spec)
                        sys.modules["module.name"] = module
                        spec.loader.exec_module(module)
                        mod = getattr(module, classname)
                        return mod
                    except Exception:
                        log.error(f"Cannot load driver class {class_.name}")
                        log.error(traceback.format_exc())
                        return
    log.error(f"Cannot find class {classname} from plugin dir")


class HardwareAbstractionLayer:
    def __init__(self, config,  msgclient):

        self.devs = {}
        self._msgclient = msgclient
        self._config = config

        for device, property in config.devices.items():
            enabled = property.get("enabled")
            if enabled and not TypeAdapter(bool).validate_python(enabled):
                continue
            if device in self.devs:
                log.error(f"Duplicated Device {device}")
                continue
            dev_cls = get_driver_module(property["driver"])
            if dev_cls is None:
                continue
            try:
                dev = dev_cls(property,
                              config.node_file,
                              config.mq_broker_host,
                              config.mq_broker_port)
            except Exception as e:
                log.error(f"Cannot instantiate driver class {dev_cls}: {e}")
                continue
            self.devs[device] = dev

        failed = [
            name for name, prop in config.devices.items()
            if TypeAdapter(bool).validate_python(prop.get("enabled", True))
            and name not in self.devs
        ]
        if failed:
            raise RuntimeError(
                f"Failed to load required device driver(s): {', '.join(failed)}. "
                f"Check the 'driver' class names in agent.cfg [devices] section."
            )


class Interpreter(ABC):
    """Abstract base class for the Agent Interpreter module.

    Example::

        i = Interpreter(hal)

    :param hal: :doc:`HAL </hal>` object created by :class:`~quantnet_agent.hal.node`.
    """

    def __init__(self, hal):
        self.hal = hal


class CoreInterpreter(Interpreter):
    """Abstract base class for the built-in Agent Interpreter module (e.g.,
    :class:`~quantnet_agent.hal.interpreter.scheduler`).

    Example::

        i = CoreInterpreter(node)

    :param node: :class:`~quantnet_agent.hal.node` object created by :class:`~quantnet_agent.service.node`.
    """

    def __init__(self, node) -> None:
        self.node = node

    @abstractmethod
    def get_commands(self):
        """Returns a dictionary of RPC handlers for a built-in interpreter to register with the RPC server
        in the controller. The dictionary keys are the names of RPC endpoints, and the values are lists composed
        of an RPC handler pointer and a schema model corresponding to the RPC.

        Example::

            def get_commands(self):
                commands = {
                    "scheduler.getSchedule": [self.get_schedule, "quantnet_mq.schema.models.scheduler.getSchedule"]
                }
                return commands
        """
        pass


class CMDInterpreter(Interpreter):
    """Abstract base class for the Agent command Interpreter module (e.g., Link calibration module
    :class:`~quantnet_agent.hal.interpreter.calibration`).

    Example::

        i = CMDInterpreter(hal)

    :param hal: :doc:`HAL </hal>` object created by :class:`~quantnet_agent.hal.node`.
    """

    def __init__(self, hal) -> None:
        super().__init__(hal)

    @abstractmethod
    def get_commands(self):
        """Returns a dictionary of RPC handlers for a command interpreter to register with the RPC server
        in the controller. The dictionary keys are the names of RPC endpoints, and the values are lists
        composed of an RPC handler pointer and a schema model corresponding to the RPC.

        Example::

            def get_commands(self):
                commands = {
                    "calibration.calibration": [self.measure, "quantnet_mq.schema.models.calibration.calibration"]
                }
                return commands
        """
        pass


class ScheduleableInterpreter(CMDInterpreter):
    """Abstract base class for a schedulable Agent command Interpreter module (e.g.,
    :class:`~quantnet_agent.hal.interpreter.ExperimentFramework`).

    Example::

        i = ScheduleableInterpreter(hal)

    :param hal: :doc:`HAL </hal>` object created by :class:`~quantnet_agent.hal.node`.
    """

    def __init__(self, hal) -> None:
        super().__init__(hal)

    @abstractmethod
    def get_commands(self):
        """Returns a dictionary of RPC handlers for a schedulable command interpreter to register with the RPC server
        in the controller. The dictionary keys are the names of RPC endpoints, and the values are lists composed
        of an RPC handler pointer and a schema model corresponding to the RPC.

        Example::

            def get_commands(self):
                commands = {
                    "experiment.getState": [self.get_state, "quantnet_mq.schema.models.experiment.getState"]
                }
                return commands
        """
        pass

    @abstractmethod
    def get_schedulable_commands(self):
        """Returns a dictionary of RPC handlers for a schedulable command interpreter to register with the RPC server
        in the two-level scheduler. The RPC endpoints returned by this function are used by the agent scheduler
        instead of the agent node, enabling the scheduler to allocate a handler to an available timeslot when
        requested by the global scheduler.

        Keys in the dictionary are the names of the RPC endpoints, and the values are lists composed of
        an RPC handler pointer, a schema model corresponding to the RPC, and a schema model for the response.

        Example::

            def get_schedulable_commands(self):
                commands = {
                    "experiment.submit": [
                        self.submit,
                        "quantnet_mq.schema.models.experiment.submit",
                        experiment.submitResponse,
                    ]
                }
                return commands
        """
        pass


class LocalTaskInterpreter(Interpreter):
    """Abstract base class for a local Agent command Interpreter module (e.g., local device calibration).

    Example::

        i = LocalTaskInterpreter(hal)

    :param hal: :doc:`HAL </hal>` object created by :class:`~quantnet_agent.hal.node`.
    """

    def __init__(self, hal):
        super().__init__(hal)

    @abstractmethod
    def run(self, *args, **kwargs):
        """Execute a local task (e.g., device calibration).

        :param args: Parameters to use for running the local task.
        :type args: list
        :param kwargs: Keyword parameter to use for running the local task.
        :type kwargs: dict
        """
        pass

    @abstractmethod
    def stop(self):
        """Stops a local task started with the `run` method."""
        pass

    @abstractmethod
    async def receive(self, *args, **kwargs):
        """Receive result of the `run` method."""
        pass
