import os
from datetime import timedelta
import quantnet_agent


class Constants:
    DEFAULT_NODE_CONFIG_FILE = "./config/conf-alice.json"
    DEFAULT_LOGGING_CONFIG_FILE = "./config/logging.conf"
    DEFAULT_INTERPRETERS = {
        "scheduler": "scheduler.py",
        "calibration": "calibration.py",
        "experiment": "exp_framework.py",
        "link": "link.py",
    }
    DEFAULT_TASK_INTERPRETER = os.path.join(
        os.path.dirname(quantnet_agent.__file__), "hal/interpreter/calibration_interpreter.py"
    )
    DEFAULT_TASK_NS = "quantnet_agent.task"
    HEARTBEAT_INTERVAL = 10
    REGISTRATION_RETRY_INTERVAL = 10
    DEFAULT_LINK_HELLO_INTERVAL = 10
    DEFAULT_LINK_HOLD_TIME = 30
    LINK_TOPIC_PREFIX = "link"  # MQTT topic prefix for agent-to-agent link adjacency traffic
    MAX_TIMESLOTS = 20000
    SLOTSIZE = timedelta(milliseconds=100)
    SCHEDULER_GRACE_PERIOD = timedelta(milliseconds=50)
    # TODO: Check SCHEDULER_GRACE_PERIOD < SLOTSIZE * MAX_TIMESLOTS
    UPDATE_INTERVAL = SLOTSIZE * MAX_TIMESLOTS / 2
    # TODO: Check if update_interval is appropriate
    DAG_CHECK_INTERVAL = timedelta(milliseconds=1000)
