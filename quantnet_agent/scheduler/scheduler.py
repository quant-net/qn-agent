import logging
import math
import asyncio
from collections import deque
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED
from datetime import datetime, timedelta, timezone
from quantnet_agent.common.constants import Constants
from quantnet_mq import Code
from quantnet_mq.schema.models import Status
from quantnet_agent.common.calibration_status import Calibration_status
import numpy as np
from quantnet_mq.schema.models import monitor


log = logging.getLogger(__name__)
logging.getLogger("apscheduler").setLevel(logging.CRITICAL)
logging.getLogger().setLevel(logging.DEBUG)


class Allocation:
    def __init__(
        self,
        name,
        operation,
        start_time: datetime,
        duration: timedelta,
        interval: timedelta = None,
        exp_id=None,
        parameters=[],
        result_handler=None,
        status=None,
        checking_param=[],
    ):
        self.name = name
        self.operation = operation
        self.start_time = start_time
        self.duration = duration
        self.interval = interval
        self.last_allocation = None
        self.last_exec = None
        self.parameters = parameters
        self.exp_id = exp_id
        self.result_handler = result_handler
        self.status = status
        self.checking_param = checking_param
        self.job_ids = []

    def __str__(self):
        return self.name


class AgentScheduler:
    def __init__(self, cid, msgclient):
        self._scheduler = AsyncIOScheduler()
        self.base = None
        self.timeslots = [None] * Constants.MAX_TIMESLOTS
        self.local_allocations = []
        self.remote_allocations = []
        self.lock = asyncio.Lock()
        self.is_started = False
        self.cmd_handler = {}
        self.cid = cid
        self.msgclient = msgclient

        def job_listener(event):
            """Listener for job events to handle updates to remote allocations."""
            job_id = event.job_id
            matching_remote_allocation = next(
                (alloc for alloc in self.remote_allocations if job_id in alloc.job_ids), None
            )
            matching_local_allocation = next(
                (alloc for alloc in self.local_allocations if job_id in alloc.job_ids), None
            )
            if matching_remote_allocation:
                if event.exception:
                    log.error(f"Job {job_id} for allocation {matching_remote_allocation.name} failed.")
                else:
                    log.debug(f"Job {job_id} for allocation {matching_remote_allocation.name} executed successfully.")
            elif matching_local_allocation:
                if event.exception:
                    log.error(f"Job {job_id} for allocation {matching_local_allocation.name} failed.")
                else:
                    log.debug(f"Job {job_id} for allocation {matching_local_allocation.name} executed successfully.")
                    if matching_local_allocation.status == Calibration_status.FULL:
                        # TODO: wait for it to finish and do half or full calibration based on the check
                        asyncio.create_task(self.publish_result(matching_local_allocation))

        def missing_job_listener(event):
            job_id = event.job_id
            matching_allocation = next((alloc for alloc in self.local_allocations if job_id in alloc.job_ids), None)
            if matching_allocation:
                log.error(f"Job {job_id} for allocation {matching_allocation.name} missed. Reallocating now")
                asyncio.create_task(self._handle_missed_allocation(matching_allocation))

        self._scheduler.add_listener(job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        self._scheduler.add_listener(missing_job_listener, EVENT_JOB_MISSED)

    async def publish_result(self, allocation):

        result = await allocation.result_handler(
                            allocation.exp_id, allocation.checking_param
                        )
        v = {"name": allocation.name, "exp_id": allocation.exp_id}
        if "results" in result:
            v["result"] = result["results"]
            msg = monitor.MonitorEvent(
                rid=self.cid,
                ts=datetime.now(timezone.utc).timestamp(),
                eventType="agentTaskResult",
                value=v,
            )
            await self.msgclient.publish("monitor", msg.as_dict())

    async def _handle_missed_allocation(self, allocation):
        """Handle missed allocation by reacquiring the lock and running it immediately."""
        async with self.lock:
            await self.run_immediately(allocation)

    async def start(self):
        log.info(f"Starting Scheduler at {datetime.now(timezone.utc)}")
        self._scheduler.start()
        self.is_started = True
        asyncio.create_task(self._handle_jobs())

    async def stop(self):
        log.info("Stopping Scheduler")
        self._scheduler.shutdown()
        self.is_started = False

    def get_jobs(self):
        jobs = self._scheduler.get_jobs()
        return jobs

    async def get_free_timeslot(self, start_time: datetime, num_slots: int):
        def convert_to_bitmask(lst):
            mask = hex(int("".join(["1" if x is None else "0" for x in lst]), 2))
            return mask

        log.info(
            f"\nGetting free timeslot from {start_time} for {num_slots} slots."
            f"\nCurrent timeslot base is {self.base} - {self.base + (Constants.MAX_TIMESLOTS * Constants.SLOTSIZE)}"
        )
        if start_time < datetime.now(timezone.utc):
            log.error("Free Timeslot request base is before the current time")
            return {"code": Code.INVALID_ARGUMENT, "value": "Free Timeslot request is before the current time"}

        start_time_base = math.ceil((start_time - self.base) / Constants.SLOTSIZE)
        if len(self.timeslots) < (start_time_base + num_slots):
            log.error("Free Timeslot request is too further ahead from current time slots")
            return {"code": Code.INVALID_ARGUMENT, "value": "Free Timeslot request is larger than current time slot"}

        indices_to_return = [
            start_time_base,
            start_time_base + num_slots,
        ]
        slots = self.timeslots[indices_to_return[0]: indices_to_return[1]]
        log.info(f"Reporting free timeslots {indices_to_return}")
        mask = convert_to_bitmask(slots)
        return {"code": Code.OK, "value": mask}

    async def delete_allocation(self, allocation):
        log.debug(f"Deleting allocation {allocation.name}")
        for job_id in allocation.job_ids:
            for i in self._scheduler.get_jobs():
                if i.id == job_id:
                    self._scheduler.remove_job(job_id)
        for index in range(len(self.timeslots)):
            if self.timeslots[index] == allocation:
                self.timeslots[index] = None
        allocation.job_ids = []
        allocation.last_allocation = None

    async def run_immediately(self, allocation):
        log.debug(f"Running {allocation.name} immediately")
        await self.delete_allocation(allocation)
        basetime_diff = math.ceil((datetime.now(timezone.utc) - self.base) / Constants.SLOTSIZE)
        allocation.last_allocation = (
            np.arange(0, math.ceil(allocation.duration / Constants.SLOTSIZE))
            - int(allocation.interval / Constants.SLOTSIZE)
        ) + basetime_diff
        log.debug(f"Setting last allocation to {allocation.last_allocation}")
        self.schedule_allocations(allocation)

    def schedule_allocations(self, allocation):
        next_allocation = allocation.last_allocation + int(allocation.interval / Constants.SLOTSIZE)
        while next_allocation[-1] < len(self.timeslots):
            if not self.schedule_next_allocation(allocation, next_allocation):
                break
            next_allocation = allocation.last_allocation + int(allocation.interval / Constants.SLOTSIZE)

    def schedule_next_allocation(self, allocation, indices):
        log.debug(f"New indices for allocation={allocation} is {indices}")

        if not self._are_slots_empty(indices):
            indices = self._get_free_slots(indices)
            if indices is None:
                log.warning("Cannot find an empty slot for a task within current time window")
                return False
        self._allocate(allocation, indices)
        return True

    async def update_schedule(self):
        async with self.lock:
            new_base = datetime.now(timezone.utc)
            timeslot_diff = (new_base - self.base) / Constants.SLOTSIZE
            new_timeslots = deque(self.timeslots)

            # Drops past timeslots and add new ones
            for _ in range(int(timeslot_diff)):
                new_timeslots.popleft()
                new_timeslots.extend([None])
            self.timeslots = list(new_timeslots)
            self.base += int(timeslot_diff) * Constants.SLOTSIZE
            log.debug(f"Shifted timeslot by {int(timeslot_diff)}")

            # Allocate jobs for existing recurring tasks to the new timeslots
            for allocation in self.local_allocations:
                if allocation.last_allocation is None:
                    # This is the first allocation
                    await self.run_immediately(allocation)
                else:
                    # Update timeslot index for next allocations
                    log.debug(f"last timeslot = {allocation.last_allocation}")
                    # allocation.last_allocation = [x - int(timeslot_diff) for x in allocation.last_allocation]
                    allocation.last_allocation -= int(timeslot_diff)
                    log.debug(f"updated last timeslot = {allocation.last_allocation}")

                    # Next run is still far head from the current window
                    if allocation.last_allocation[0] > len(self.timeslots):
                        continue

                    # If the next run is in the past, run it immediately
                    if (allocation.last_allocation + int(allocation.interval / Constants.SLOTSIZE))[0] < 0:
                        await self.run_immediately(allocation)
                        continue

                    self.schedule_allocations(allocation)

            self.remote_allocations = [i for i in self.remote_allocations if hasattr(i, "job") and i.job.pending]

    async def _handle_jobs(self):
        self.base = datetime.now(timezone.utc)
        while self.is_started:
            await asyncio.sleep(Constants.UPDATE_INTERVAL.total_seconds())
            asyncio.create_task(self.update_schedule())

    def _allocate(self, allocation, indices):
        async def run_and_update(allocation, start_time):
            msg = monitor.MonitorEvent(
                rid=self.cid,
                ts=datetime.now(timezone.utc).timestamp(),
                eventType="agentTaskSchedulerTask",
                value=f"Running task {allocation.name} at {start_time}",
            )
            pub_job = self.msgclient.publish("monitor", msg.as_dict())
            allocation.last_exec = [datetime.now(timezone.utc), start_time]
            await allocation.operation(allocation.parameters, exp_id=allocation.exp_id)
            await pub_job

        log.debug(f"Trying to allocate {allocation.name} to {indices}")

        for index in indices:
            self.timeslots[index] = allocation
        start_time = self.base + (indices[0] * Constants.SLOTSIZE)
        trigger = DateTrigger(run_date=start_time)
        job = self._scheduler.add_job(run_and_update, args=[allocation, start_time], trigger=trigger)
        log.debug(
            f"Adding a job {allocation} id {job} at indices {indices}, "
            f"start_time={start_time}, now = {datetime.now(timezone.utc)}"
        )
        allocation.job_ids.append(job.id)
        allocation.last_allocation = indices

    def _get_free_slots(self, indices):
        while indices[-1] < Constants.MAX_TIMESLOTS:
            if self._are_slots_empty(indices):
                return indices
            else:
                indices += 1
        return None

    def _are_slots_empty(self, indices):
        for index in indices:
            if self.timeslots[index] is not None:
                return False
        return True

    def _get_timeslot_indices(self, start_time: datetime, duration: timedelta, interval: timedelta):
        # Calculate how many iterations and their timeslot indices within current scheduable timeslots
        indices = []
        time_remaining = self.base + (Constants.MAX_TIMESLOTS * Constants.SLOTSIZE) - (start_time + duration)
        log.debug(
            f"Finding indices for a job starting at {start_time},"
            f"interval = {interval} with remaining time {time_remaining},"
            f"looping {math.ceil(time_remaining / interval)} times"
        )
        for i in range(math.ceil(time_remaining / interval)):
            indices_found = self._get_timeslot_index(start_time + (i * interval), duration)
            log.debug(f"Found indices {indices_found}")
            if indices_found is not None:
                indices.append(indices_found)
        return indices

    def _get_timeslot_index(self, start_time, duration):
        log.debug(f"Looking for timeslots starting {start_time} for {duration}")
        start_index = int((start_time - self.base) / Constants.SLOTSIZE)
        num_slots = math.ceil(duration / Constants.SLOTSIZE)
        if start_index + num_slots >= Constants.MAX_TIMESLOTS:
            return None
        indices = [start_index + i for i in range(num_slots)]
        if self._are_slots_empty(indices):
            log.debug(f"Found empty slots: {indices}")
            return indices
        else:
            # If timeslot is already occupied, find next available timeslots
            log.debug(
                f"Slots {indices} are already occupied." f"finding next available slots from {start_time + duration}"
            )
            return self._get_timeslot_index(start_time + duration, duration)

    async def preallocate(self, allocation: Allocation):
        if allocation in self.local_allocations:
            raise Exception("Cannot allocate the same allocation object")
        if allocation.interval <= Constants.SLOTSIZE:
            raise Exception(
                f"Allocation {allocation.name} interval is too short (compared to the schedulers timeslot size)"
            )
        self.local_allocations.append(allocation)
        async with self.lock:
            await self.run_immediately(allocation)
        # await self.show_schedule()

    def get_status(self):
        return self._scheduler.running

    async def handle_submit(self, request):
        async with self.lock:
            log.info(f"Received allocation request : {request.serialize()}")
            exp_id = request.payload.exp_id
            timeslotbase = datetime.fromtimestamp(request.payload.timeslotBase._value, tz=timezone.utc)
            base_diff = math.ceil((timeslotbase - self.base) / Constants.SLOTSIZE)
            log.info(f"Allocating tasks on basetime {timeslotbase}, base difference is {base_diff}")
            submit_task = self.cmd_handler[request.cmd][0]
            response_obj = self.cmd_handler[request.cmd][2]
            result_handler = self.cmd_handler[request.cmd][3]

            if base_diff < 0:
                log.error("Allocation request is already past the current time")
                return response_obj(expid=exp_id, status=Status(code=6, value=Code(6).name))
            elif base_diff > len(self.timeslots):
                log.error("Allocation request is too far into the future")
                return response_obj(expid=exp_id, status=Status(code=6, value=Code(6).name))
            for allocation in request.payload.allocations:
                timeslot_indices = [i + base_diff for i in allocation.timeSlot]

                if not self._are_slots_empty(timeslot_indices):
                    log.error(f"Cannot allocate experiment {exp_id}. Slots are already occupied")
                    return response_obj(expid=exp_id, status=Status(code=6, value=Code(6).name))

                start_time = self.base + (timeslot_indices[0] * Constants.SLOTSIZE)

                # Allocating experiment to the timeslots
                allocation_obj = Allocation(
                    allocation.expName._value,
                    submit_task,
                    start_time,
                    Constants.SLOTSIZE * len(allocation.timeSlot),
                    exp_id=exp_id,
                    parameters=allocation,
                    result_handler=result_handler,
                )
                self._allocate(allocation_obj, timeslot_indices)
                self.remote_allocations.append(allocation_obj)
                self.show_schedule()
            return response_obj(expid=exp_id, status=Status(code=0, value=Code(0).name))

    async def handle_update_result(self, request):
        log.info(f"Received getResult request : {request.serialize()}")
        getResult_task = self.cmd_handler[request.cmd][0]
        response_obj = self.cmd_handler[request.cmd][2]
        exp_id = request.payload.expid._value

        matching_allocation = next((alloc for alloc in self.remote_allocations if alloc.exp_id == exp_id), None)

        if not matching_allocation:
            log.error(f"No allocation found for exp_id {exp_id}")
            return response_obj(
                status=Status(Code.FAILED, value=Code.FAIELD.name, reason=f"No allocation found for exp_id {exp_id}")
            )

        while matching_allocation.last_exec is None:
            await asyncio.sleep(0.1)

        # Handle the result retrieval for the matching allocation
        log.debug(f"Processing result for allocation with exp_id {exp_id}")
        try:
            # Call the result handler with the matching allocation
            result = await getResult_task(exp_id)
            return response_obj(status=Status(code=Code.OK, value=Code.OK.name), result=result)
        except Exception as e:
            log.error(f"Error while processing result for allocation with exp_id {exp_id}: {e}")
            return response_obj(status=Status(code=Code.FAILED, value=Code.FAILED.name,
                                              reason="Error while processing result"))

    async def handle_cancel(self, request):
        log.info(f"Received cancelling request: {request.serialize()}")
        log.debug(f"Current jobs : {self._scheduler.get_jobs()}")
        exp_id = request.payload.exp_id
        response_obj = self.cmd_handler[request.cmd][2]
        try:
            async with self.lock:
                for experiment in self.remote_allocations:
                    if experiment.exp_id == exp_id:
                        for job_id in experiment.job_ids:
                            self._scheduler.remove_job(job_id)
                        for index in range(len(self.timeslots)):
                            if self.timeslots[index] == exp_id:
                                self.timeslots[index] = None
            return response_obj(status=Status(code=0, value=Code(0).name))
        except JobLookupError:
            return response_obj(status=Status(code=0, value=Code(0).name))
        except Exception as e:
            log.error(f"Failed to cancel experiment {exp_id}: {e}")
            return response_obj(status=Status(code=6, value=Code(6).name))

    def get_allocation(self, task_name):
        """
        Retrieves the allocation for a given task name from local or remote allocations.
        :param task_name: The name of the task to find the allocation for.
        :return: The Allocation object if found, else None.
        """
        # Search in local allocations
        for allocation in self.local_allocations:
            if allocation.name == task_name:
                return allocation

        # Search in remote allocations
        for allocation in self.remote_allocations:
            if allocation.name == task_name:
                return allocation

        # If not found, return None
        log.warning(f"No allocation found for task {task_name}.")
        return None

    def register_command(self, ns, interpreter, rpcserver):
        # NOTE: Scheduleable method in interpreter is handled by method with name handle_{method_name}
        for cmd, interpreter_map in interpreter.get_schedulable_commands().items():
            self.cmd_handler[cmd] = interpreter_map
            target_handler = getattr(self, f"handle_{interpreter_map[0].__name__}")
            rpcserver.set_handler(cmd, target_handler, interpreter_map[1])

    def show_schedule(self):
        empty_slot_counter = 0
        occupied_slots = {}

        log.debug("Current schedule summary")
        schedule_output = "\n-------------------------------------------------\n["

        # Build the timeslot display line
        for i in range(len(self.timeslots)):
            if self.timeslots[i] is not None:
                occupied_slots[i] = self.timeslots[i].name
                empty_slot_counter = 0
                schedule_output += f"{self.timeslots[i].name[0]}|"
            else:
                empty_slot_counter += 1
                if empty_slot_counter < 6:
                    schedule_output += "."

        schedule_output += "]\n-------------------------------------------------\n"
        log.debug(schedule_output)

        # Log allocations for local tasks
        for alloc in self.local_allocations:
            slots = [str(i) for i, v in occupied_slots.items() if v == alloc.name]
            log.debug(f"{alloc.name} is allocated at indices\n[{', '.join(slots)}]\n")

        # Log allocations for remote tasks
        for alloc in self.remote_allocations:
            slots = [str(i) for i, v in occupied_slots.items() if v == alloc.name]
            log.debug(f"{alloc.name} is allocated at indices\n[{', '.join(slots)}]\n")
