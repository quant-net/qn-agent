import os
import sys
import logging
import logging.config


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
