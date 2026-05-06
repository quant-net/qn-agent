import logging
from sipyco.pc_rpc import Client
from sipyco.broadcast import Receiver
from quantnet_agent.hal.hwclasses import ExpFramework
import asyncio
import numpy

log = logging.getLogger(__name__)
TIMEOUT = 10


class ArtiqClient(ExpFramework):
    def __init__(self, property, node_config, mqhost, mqport):
        self.expid_to_rid = {}
        self.logs = {}
        if "version" in property and int(property["version"]) == 7:
            self.schedule, self.exps, self.datasets = [
                Client(property["host"], property["port"], i)
                for i in "master_schedule master_experiment_db master_dataset_db".split()
            ]
        else:
            self.schedule, self.exps, self.datasets = [
                Client(property["host"], property["port"], i) for i in "schedule experiment_db dataset_db".split()
            ]

        asyncio.create_task(self._connect_and_receive_logs(property["host"], int(property["port"])))

        log.info(f"Initializing ARTIQ with {property['host']} : {property['port']}")

    async def _connect_and_receive_logs(self, host, port):
        # Create the receiver instance
        log_receiver = Receiver("log", [], None)

        # Connect to the log service at the specified address and port
        await log_receiver.connect(host, port)

        # Register the callback function to receive log messages
        log_receiver.notify_cbs.append(self._append_message)

        while True:
            # This loop keeps running to listen for incoming messages
            await asyncio.sleep(1)

    def _append_message(self, msg):
        log.debug(f"Received a message from artiq: {msg}")
        try:
            if msg[1] == "master":
                rid = "master"
            else:
                rid = int(msg[1][msg[1].find("(") + 1 : msg[1].find(",")])

            if rid not in self.logs:
                self.logs[rid] = ""
            self.logs[rid] = self.logs[rid] + msg[3]
        except Exception as e:
            log.error(f"Error reading ARTIQ log: {e}")
            return

    def reset(self):
        log.info("Force deleting scheduled experiments")
        for rid in self.schedule.get_status():
            log.info(f"exp : {rid}")
            self.schedule.delete(rid)

    def stop(self):
        log.info("Request for stopping experiments")
        for rid in self.schedule.get_status():
            log.info(f"exp : {rid}")
            self.schedule.request_termination(rid)

    def cleanUp(self):
        self.stop()

    def logs(self):
        pass

    async def receive(self, *args, **kwargs):
        exp_id = args[0]
        rid = self.expid_to_rid[exp_id]
        await self._wait_for_completion(rid)
        if len(args) > 1:
            params = args[1]
            try:
                results = {}
                for param in params:
                    result = self.datasets.get(param)
                    if type(result) == numpy.ndarray:
                        result = result.tolist()
                    results[param] = result
                return {f"status for rid {rid}": "done", "results": results}
            except Exception as e:
                logging.error(f"Failed to get result for exp {exp_id}: {e}")
                return {f"status for rid {rid}": "done"}
        else:
            return {f"status for rid {rid}": "done"}

    async def submit(self, exp_id, expName, classname, args=dict()):
        args["use_db"] = True
        expid = dict(
            file=expName,
            class_name=classname,
            log_level=logging.WARNING,
            arguments=args,
        )
        log.info(f"Submitting an experiment {expName}")
        rid = self.schedule.submit(pipeline_name="main", expid=expid, priority=0, due_date=None, flush=False)
        self.expid_to_rid[exp_id] = rid
        if self._check_rid(rid):
            log.info(f"experiment with id {rid} scheduled")
            return 0
        else:
            log.error(f"submitted exp with rid {rid} not found")
            raise Exception

    async def _wait_for_completion(self, rid):
        """Wait for the experiment to complete."""
        log.debug(f"Waiting for experiment {rid} to finish...")
        while rid in self.schedule.get_status():
            await asyncio.sleep(0.1)
        self.logs
        return 0

    def _check_rid(self, rid):
        return rid in self.schedule.get_status()
