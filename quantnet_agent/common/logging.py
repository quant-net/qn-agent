import os
import sys
import logging
import logging.config


class CircularBufferHandler(logging.Handler):
    """A handler that keeps the last N log records in memory.

    This allows showing recent logs via `show logging` command in REPL.
    """
    def __init__(self, capacity=1000):
        super().__init__()
        self.capacity = capacity
        self.buffer = []

    def emit(self, record):
        try:
            msg = self.format(record)
            self.buffer.append(msg)
            if len(self.buffer) > self.capacity:
                self.buffer.pop(0)
        except Exception:
            self.handleError(record)

    def get_logs(self, count=None):
        """Return the last `count` logs, or all if count is None."""
        if count is None:
            return list(self.buffer)
        return list(self.buffer[-count:])


# Global circular buffer handler (always active)
_global_log_buffer = CircularBufferHandler(capacity=1000)


def get_buffered_logs(count=None):
    """Retrieve buffered logs. If count is None, return all; else last count."""
    return _global_log_buffer.get_logs(count)


def clear_buffered_logs():
    """Clear the log buffer (typically called when REPL starts)."""
    _global_log_buffer.buffer.clear()


def quantnet_log_formatter(cobj=None):
    config_logformat = cobj.get(
        "common",
        "logformat",
        default="{asctime} {name:<29} {process} {levelname:>8} {message}",
    )
    return logging.Formatter(fmt=config_logformat, style="{")


def setup_default_logging(cobj=None):
    """
    Configures the logging by setting the output stream to stdout and
    configures log level and log format.
    """
    config_loglevel = getattr(logging, cobj.get("common", "loglevel", default="INFO").upper())

    stdouthandler = logging.StreamHandler(stream=sys.stdout)
    stdouthandler.setFormatter(quantnet_log_formatter(cobj))
    stdouthandler.setLevel(config_loglevel)
    logging.basicConfig(level=config_loglevel, handlers=[stdouthandler])


def setup_logging(cobj=None):
    """
    Configures the logging by setting the output stream to stdout and
    configures log level and log format.
    """

    configfiles = list()

    if cobj:
        logging_config_path = cobj.get("common", "logging_config", default=None)
        if logging_config_path:
            configfiles.append(logging_config_path)

    for i in ["QUANTNET_HOME", "VIRTUAL_ENV"]:
        if i in os.environ:
            configfiles.append(f"{os.environ[i]}/etc/logging.conf")
    configfiles.append("/opt/quantnet/etc/logging.conf")

    has_config = False
    for configfile in configfiles:
        try:
            logging.config.fileConfig(configfile, disable_existing_loggers=False)
            has_config = True
        except Exception:
            has_config = False
        if has_config:
            break

    if not has_config and cobj:
        setup_default_logging(cobj)
    logging.getLogger("gmqtt").setLevel(logging.WARNING)

    # Always add global circular buffer for 'show logging' command
    _global_log_buffer.setFormatter(
        logging.Formatter(fmt="{asctime} {name:<29} {process} {levelname:>8} {message}", style="{")
    )
    _global_log_buffer.setLevel(logging.DEBUG)  # Capture all levels
    logging.getLogger().addHandler(_global_log_buffer)
