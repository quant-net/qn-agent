import logging
import math
import asyncio
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


class Allocation:
    def __init__(
        self,
        name,
        operation,
        start_time: datetime,
        duration: timedelta,
        interval: timedelta = None,
        exp_id=None,
        parameters=None,
        result_handler=None,
        status=None,
        checking_param=None,
    ):
        self.name = name
        self.operation = operation
        self.start_time = start_time
        self.duration = duration
        self.interval = interval
        self.last_allocation = None
        self.last_exec = None
        self.parameters = parameters if parameters is not None else []
        self.exp_id = exp_id
        self.result_handler = result_handler
        self.status = status
        self.checking_param = checking_param if checking_param is not None else []
        self.failed = False
        self.job_ids = []
        self._slot_indices: set = set()

    def __str__(self):
        return self.name


class AgentScheduler:
    def __init__(self, cid, msgclient):
        logging.getLogger("apscheduler").setLevel(logging.CRITICAL)
        self._scheduler = AsyncIOScheduler()
        self.base = None
        self.timeslots = [None] * Constants.MAX_TIMESLOTS
        self.local_allocations = []
        self.remote_allocations = []
        # O(1) lookups — maintained in sync with the two lists above
        self._alloc_by_name: dict = {}    # name  -> Allocation
        self._alloc_by_job_id: dict = {}  # job_id -> Allocation
        self.lock = asyncio.Lock()
        self.is_started = False
        self.cmd_handler = {}
        self.cid = cid
        self.msgclient = msgclient
        self._exp_start_times: dict = {}  # exp_id -> datetime when first block started

        def job_listener(event):
            # Fires on EVENT_JOB_EXECUTED and EVENT_JOB_ERROR for both local and remote jobs.
            allocation = self._alloc_by_job_id.get(event.job_id)
            if allocation is None:
                return
            if event.exception:
                allocation.failed = True
                log.error(
                    f"Job {event.job_id} for allocation {allocation.name} failed: "
                    f"{event.exception}\n{event.traceback}"
                )
            else:
                log.debug(f"Job {event.job_id} for allocation {allocation.name} executed successfully.")
                # TODO: wait for result, then decide between LIGHT and FULL re-calibration based on the check.
                # Only publish results for local FULL calibrations; remote results are fetched on demand.
                if allocation in self.local_allocations and allocation.status == Calibration_status.FULL:
                    asyncio.create_task(self.publish_result(allocation))

        def missing_job_listener(event):
            # APScheduler fires EVENT_JOB_MISSED when a job's run_date passes without execution
            # (e.g. the scheduler was paused or overloaded). Reschedule local tasks only —
            # remote experiments are managed by the caller and not retried automatically.
            allocation = self._alloc_by_job_id.get(event.job_id)
            if allocation is not None and allocation in self.local_allocations:
                log.error(f"Job {event.job_id} for allocation {allocation.name} missed. Reallocating now")
                asyncio.create_task(self._handle_missed_allocation(allocation))

        self._scheduler.add_listener(job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        self._scheduler.add_listener(missing_job_listener, EVENT_JOB_MISSED)

    async def publish_result(self, allocation):
        result = await allocation.result_handler(allocation.exp_id, allocation.checking_param)
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
        return self._scheduler.get_jobs()

    async def get_free_timeslot(self, start_time: datetime, num_slots: int):
        def convert_to_bitmask(lst):
            return hex(int("".join(["1" if x is None else "0" for x in lst]), 2))

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

        slots = self.timeslots[start_time_base: start_time_base + num_slots]
        log.info(f"Reporting free timeslots [{start_time_base}, {start_time_base + num_slots}]")
        return {"code": Code.OK, "value": convert_to_bitmask(slots)}

    async def delete_allocation(self, allocation):
        log.debug(f"Deleting allocation {allocation.name}")
        for job_id in allocation.job_ids:
            try:
                self._scheduler.remove_job(job_id)
            except JobLookupError:
                pass
            self._alloc_by_job_id.pop(job_id, None)
        for index in allocation._slot_indices:
            if index < len(self.timeslots) and self.timeslots[index] is allocation:
                self.timeslots[index] = None
        allocation._slot_indices.clear()
        allocation.job_ids = []
        allocation.last_allocation = None

    async def run_immediately(self, allocation):
        if allocation is None:
            log.warning("run_immediately called with no allocation — task not yet registered, skipping.")
            return
        log.debug(f"Running {allocation.name} immediately")
        await self.delete_allocation(allocation)
        basetime_diff = math.ceil((datetime.now(timezone.utc) - self.base) / Constants.SLOTSIZE)
        # Set last_allocation one full interval before now so that schedule_allocations()
        # computes next_allocation = last + interval ≈ now and books the first run immediately.
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
            shift = int((new_base - self.base) / Constants.SLOTSIZE)

            if shift > 0:
                self.timeslots = self.timeslots[shift:] + [None] * shift
                self.base += shift * Constants.SLOTSIZE
                log.debug(f"Shifted timeslot by {shift}")

                # Adjust tracked slot indices for all allocations
                for allocation in self.local_allocations + self.remote_allocations:
                    allocation._slot_indices = {
                        idx - shift for idx in allocation._slot_indices if idx >= shift
                    }

            for allocation in self.local_allocations:
                if allocation.last_allocation is None:
                    # First time this allocation is being scheduled.
                    await self.run_immediately(allocation)
                elif shift == 0:
                    # Window did not move — nothing to reschedule.
                    pass
                else:
                    log.debug(f"last timeslot = {allocation.last_allocation}")
                    allocation.last_allocation -= shift
                    log.debug(f"updated last timeslot = {allocation.last_allocation}")

                    # Next run is still beyond the current window — nothing to schedule yet.
                    if allocation.last_allocation[0] > len(self.timeslots):
                        continue

                    # Next run fell into the past — run it now and let schedule_allocations
                    # fill in the following intervals.
                    if (allocation.last_allocation + int(allocation.interval / Constants.SLOTSIZE))[0] < 0:
                        await self.run_immediately(allocation)
                        continue

                    self.schedule_allocations(allocation)

            still_pending = []
            for alloc in self.remote_allocations:
                if hasattr(alloc, "job") and alloc.job.pending:
                    still_pending.append(alloc)
                else:
                    # Job has fired — clear its timeslot entries so future requests can use them
                    for idx in alloc._slot_indices:
                        if idx < len(self.timeslots) and self.timeslots[idx] is alloc:
                            self.timeslots[idx] = None
                    self._alloc_by_name.pop(alloc.name, None)
            self.remote_allocations = still_pending

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
            allocation.failed = False
            allocation.last_exec = [datetime.now(timezone.utc), start_time]
            if allocation.exp_id and allocation.exp_id not in self._exp_start_times:
                self._exp_start_times[allocation.exp_id] = allocation.last_exec[0]
            try:
                await allocation.operation(allocation.parameters, exp_id=allocation.exp_id)
            finally:
                await pub_job

        log.debug(f"Trying to allocate {allocation.name} to {indices}")
        for index in indices:
            self.timeslots[index] = allocation
            allocation._slot_indices.add(index)
        start_time = self.base + (indices[0] * Constants.SLOTSIZE)
        trigger = DateTrigger(run_date=start_time)
        job = self._scheduler.add_job(run_and_update, args=[allocation, start_time], trigger=trigger)
        log.debug(
            f"Adding a job {allocation} id {job} at indices {indices}, "
            f"start_time={start_time}, now = {datetime.now(timezone.utc)}"
        )
        allocation.job_ids.append(job.id)
        self._alloc_by_job_id[job.id] = allocation
        allocation.last_allocation = indices

    def _get_free_slots(self, indices):
        while indices[-1] < Constants.MAX_TIMESLOTS:
            if self._are_slots_empty(indices):
                return indices
            indices += 1
        return None

    def _are_slots_empty(self, indices):
        for index in indices:
            if self.timeslots[index] is not None:
                return False
        return True

    def _get_timeslot_indices(self, start_time: datetime, duration: timedelta, interval: timedelta):
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
        num_slots = math.ceil(duration / Constants.SLOTSIZE)
        current_time = start_time
        while True:
            log.debug(f"Looking for timeslots starting {current_time} for {duration}")
            start_index = int((current_time - self.base) / Constants.SLOTSIZE)
            if start_index + num_slots >= Constants.MAX_TIMESLOTS:
                return None
            indices = list(range(start_index, start_index + num_slots))
            if self._are_slots_empty(indices):
                log.debug(f"Found empty slots: {indices}")
                return indices
            log.debug(
                f"Slots {indices} are already occupied. "
                f"Finding next available slots from {current_time + duration}"
            )
            current_time += duration

    async def preallocate(self, allocation: Allocation):
        if allocation in self.local_allocations:
            raise Exception("Cannot allocate the same allocation object")
        if allocation.interval <= Constants.SLOTSIZE:
            raise Exception(
                f"Allocation {allocation.name} interval is too short (compared to the schedulers timeslot size)"
            )
        self.local_allocations.append(allocation)
        self._alloc_by_name[allocation.name] = allocation
        async with self.lock:
            await self.run_immediately(allocation)

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
                if not timeslot_indices:
                    continue
                if not self._are_slots_empty(timeslot_indices):
                    log.error(f"Cannot allocate experiment {exp_id}. Slots are already occupied")
                    return response_obj(expid=exp_id, status=Status(code=6, value=Code(6).name))

                start_time = self.base + (timeslot_indices[0] * Constants.SLOTSIZE)
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
                self._alloc_by_name[allocation_obj.name] = allocation_obj
                self.show_schedule()

            return response_obj(expid=exp_id, status=Status(code=0, value=Code(0).name))

    async def handle_update_result(self, request):
        log.info(f"Received getResult request : {request.serialize()}")
        getResult_task = self.cmd_handler[request.cmd][0]
        response_obj = self.cmd_handler[request.cmd][2]
        exp_id = request.payload.expid._value

        # Find any pending allocation OR check if exp already started via _exp_start_times.
        # remote_allocations only keeps pending (not-yet-fired) jobs, so completed blocks
        # are pruned. We must also look at _exp_start_times for already-started experiments.
        matching_allocation = next((alloc for alloc in self.remote_allocations if alloc.exp_id == exp_id), None)

        if exp_id not in self._exp_start_times and matching_allocation is None:
            log.error(f"No allocation found for exp_id {exp_id}")
            return response_obj(status=Status(
                code=Code.FAILED, value=Code.FAILED.name,
                reason=f"No allocation found for exp_id {exp_id}",
            ))

        if exp_id not in self._exp_start_times:
            # Experiment has not started yet — wait until its first block fires.
            # Use the earliest pending allocation's start_time as the expected start,
            # with a generous wall-clock deadline (start_time + 60s buffer).
            deadline = matching_allocation.start_time + timedelta(seconds=60)
            while exp_id not in self._exp_start_times:
                if datetime.now(timezone.utc) > deadline:
                    log.error(f"Timed out waiting for experiment {exp_id} to start.")
                    return response_obj(status=Status(
                        code=Code.FAILED, value=Code.FAILED.name,
                        reason=f"Experiment {exp_id} did not start within its allocated duration",
                    ))
                await asyncio.sleep(0.1)

        log.debug(f"Processing result for allocation with exp_id {exp_id}")
        try:
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
                to_cancel = [a for a in self.remote_allocations if a.exp_id == exp_id]
                for allocation in to_cancel:
                    await self.delete_allocation(allocation)
                    self._alloc_by_name.pop(allocation.name, None)
                self.remote_allocations = [a for a in self.remote_allocations if a.exp_id != exp_id]
                self._exp_start_times.pop(exp_id, None)
            return response_obj(status=Status(code=0, value=Code(0).name))
        except JobLookupError:
            return response_obj(status=Status(code=0, value=Code(0).name))
        except Exception as e:
            log.error(f"Failed to cancel experiment {exp_id}: {e}")
            return response_obj(status=Status(code=6, value=Code(6).name))

    def get_allocation(self, task_name):
        allocation = self._alloc_by_name.get(task_name)
        if allocation is None:
            log.warning(f"No allocation found for task {task_name}.")
        return allocation

    def register_command(self, ns, interpreter, rpcserver):
        # Each schedulable command is dispatched to a method named handle_<function_name>
        # on this class (e.g. experiment.submit → handle_submit).
        for cmd, interpreter_map in interpreter.get_schedulable_commands().items():
            self.cmd_handler[cmd] = interpreter_map
            target_handler = getattr(self, f"handle_{interpreter_map[0].__name__}")
            rpcserver.set_handler(cmd, target_handler, interpreter_map[1])

    def show_schedule(self):
        empty_slot_counter = 0
        occupied_slots = {}

        log.debug("Current schedule summary")
        schedule_output = "\n-------------------------------------------------\n["

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

        for alloc in self.local_allocations:
            slots = [str(i) for i, v in occupied_slots.items() if v == alloc.name]
            log.debug(f"{alloc.name} is allocated at indices\n[{', '.join(slots)}]\n")

        for alloc in self.remote_allocations:
            slots = [str(i) for i, v in occupied_slots.items() if v == alloc.name]
            log.debug(f"{alloc.name} is allocated at indices\n[{', '.join(slots)}]\n")
