"""
Priority-aware Local / Edge / Cloud scheduler for sandboxed agent jobs.

This is the production port of the MS-thesis scheduling model
("Privacy-Preserving SplitFed-MADRL for Priority-Aware Task Scheduling in
Healthcare Edge Computing", Steps 1-10):

  * three hospital departments (Emergency, ICU, General_Ward), each with its
    own edge sandbox pool, plus one shared Cloud sandbox pool;
  * three priority classes (Critical / High / Routine) with per-class
    deadlines;
  * the same three actions: Local (own department pool), Edge (least-loaded
    neighbour pool), Cloud (shared pool, bigger limits, extra start-up delay);
  * routing by the thesis' Estimated-Completion-Time rule (agents/heuristics.py)
        est(a) = delay(a) + (backlog(a) + demand) / capacity(a)
    measured here in seconds of sandbox work instead of simulator steps;
  * inside every pool, jobs are served Critical-first, then earliest deadline
    (the Step-10 priority-aware objective), not FCFS.

The thesis showed (results/step10_final) that priority-aware MAPPO cut
Critical deadline violations from 64.8% (FCFS) to 0.0% under overload. The
trained MAPPO actors take 10-value simulator observations, so here the
ECT + priority rule is used live and the MAPPO numbers are reported as the
offline evidence (see README).
"""

import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field

DEPARTMENTS = ("Emergency", "ICU", "General_Ward")
PRIORITIES = ("Critical", "High", "Routine")
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
# Wall-clock deadlines (seconds after submission) for an interactive service.
DEADLINE_S = {"Critical": 60, "High": 180, "Routine": 600}
# Concurrent sandboxes per pool (thesis capacities 5/4/3 and cloud 50, scaled
# down to what a single small Vultr VM can safely run at once).
POOL_CAPACITY = {"Emergency": 2, "ICU": 2, "General_Ward": 1, "Cloud": 3}
# Extra start-up delay (s) when leaving the source department (thesis: Edge 1
# step, Cloud 2 steps).
NETWORK_DELAY_S = {"Local": 0.0, "Edge": 2.0, "Cloud": 4.0}
# Expected sandbox seconds per job size (estimated by the triage agent).
SIZE_SECONDS = {"small": 4.0, "medium": 10.0, "large": 25.0}
CLOUD_SIZE_THRESHOLD = "large"  # thesis cloud_threshold: big jobs -> Cloud


@dataclass(order=True)
class _QueueItem:
    sort_key: tuple
    job_id: str = field(compare=False)


class Pool:
    def __init__(self, name: str, capacity: int):
        self.name, self.capacity = name, capacity
        self.queue: list = []
        self.running: dict = {}   # job_id -> expected seconds
        self.queued_s: dict = {}  # job_id -> expected seconds

    @property
    def backlog_s(self) -> float:
        return sum(self.running.values()) + sum(self.queued_s.values())


class Scheduler:
    def __init__(self):
        self.lock = threading.Condition()
        self.pools = {n: Pool(n, c) for n, c in POOL_CAPACITY.items()}
        self._counter = itertools.count()

    # ---- routing (thesis ECT rule) ---------------------------------------
    def estimates(self, department: str, size: str) -> dict:
        demand = SIZE_SECONDS[size]
        neighbours = [d for d in DEPARTMENTS if d != department]
        neighbour = min(neighbours, key=lambda d: (self.pools[d].backlog_s / self.pools[d].capacity, d))
        cand = {
            "Local": (department, NETWORK_DELAY_S["Local"]),
            "Edge": (neighbour, NETWORK_DELAY_S["Edge"]),
            "Cloud": ("Cloud", NETWORK_DELAY_S["Cloud"]),
        }
        return {
            tier: {"pool": pool, "est_s": round(delay + (self.pools[pool].backlog_s + demand) / self.pools[pool].capacity, 2)}
            for tier, (pool, delay) in cand.items()
        }

    def route(self, department: str, size: str) -> tuple:
        """Returns (tier, pool_name, estimates, reason)."""
        with self.lock:
            est = self.estimates(department, size)
            if size == CLOUD_SIZE_THRESHOLD:
                return "Cloud", "Cloud", est, "large job -> Cloud (thesis cloud_threshold rule)"
            order = {"Local": 0, "Edge": 1, "Cloud": 2}
            tier = min(est, key=lambda t: (est[t]["est_s"], order[t]))
            return tier, est[tier]["pool"], est, f"lowest estimated completion time ({est[tier]['est_s']}s)"

    # ---- queueing (priority + earliest deadline inside each pool) --------
    def submit(self, job_id: str, pool: str, priority: str, deadline_ts: float, size: str):
        with self.lock:
            p = self.pools[pool]
            heapq.heappush(p.queue, _QueueItem((PRIORITY_RANK[priority], deadline_ts, next(self._counter)), job_id))
            p.queued_s[job_id] = SIZE_SECONDS[size]
            self.lock.notify_all()

    def next_job(self, pool: str, stop: threading.Event):
        """Blocks until this pool has a free slot and a queued job."""
        with self.lock:
            p = self.pools[pool]
            while not stop.is_set():
                if p.queue and len(p.running) < p.capacity:
                    item = heapq.heappop(p.queue)
                    p.running[item.job_id] = p.queued_s.pop(item.job_id)
                    return item.job_id
                self.lock.wait(timeout=1.0)
        return None

    def finish(self, pool: str, job_id: str):
        with self.lock:
            self.pools[pool].running.pop(job_id, None)
            self.lock.notify_all()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                n: {"capacity": p.capacity, "running": len(p.running), "queued": len(p.queue),
                    "backlog_s": round(p.backlog_s, 1)}
                for n, p in self.pools.items()
            }


def deadline_for(priority: str, submitted: float | None = None) -> float:
    return (submitted or time.time()) + DEADLINE_S[priority]
