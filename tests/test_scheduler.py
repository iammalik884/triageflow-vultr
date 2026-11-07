import threading
import time

from app.scheduler import Scheduler, deadline_for


def test_critical_served_before_routine_in_same_pool():
    s = Scheduler()
    now = time.time()
    s.submit("routine", "General_Ward", "Routine", deadline_for("Routine", now), "small")
    s.submit("high", "General_Ward", "High", deadline_for("High", now), "small")
    s.submit("critical", "General_Ward", "Critical", deadline_for("Critical", now), "small")
    stop = threading.Event()
    assert s.next_job("General_Ward", stop) == "critical"   # capacity 1
    s.finish("General_Ward", "critical")
    assert s.next_job("General_Ward", stop) == "high"
    s.finish("General_Ward", "high")
    assert s.next_job("General_Ward", stop) == "routine"


def test_idle_system_routes_local():
    tier, pool, _, _ = Scheduler().route("ICU", "small")
    assert (tier, pool) == ("Local", "ICU")


def test_large_job_goes_to_cloud():
    tier, pool, _, _ = Scheduler().route("Emergency", "large")
    assert (tier, pool) == ("Cloud", "Cloud")


def test_busy_department_offloads():
    s = Scheduler()
    for i in range(6):
        s.submit(f"j{i}", "General_Ward", "Routine", deadline_for("Routine"), "medium")
    tier, pool, est, _ = s.route("General_Ward", "medium")
    assert tier in ("Edge", "Cloud") and pool != "General_Ward"
    assert est[tier]["est_s"] <= est["Local"]["est_s"]
