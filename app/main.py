"""
TriageFlow - priority-aware, containment-first clinical data agents on Vultr.

Control layer (this FastAPI app, on a Vultr Cloud Compute VM):
  request -> Triage agent (Vultr Serverless Inference) -> thesis scheduler
  (priority + Local/Edge/Cloud ECT routing) -> Coder agent -> isolated Docker
  sandbox -> real output -> self-healing retry on error -> Verifier agent.
"""

import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import llm, sandbox
from .scheduler import DEPARTMENTS, POOL_CAPACITY, Scheduler, deadline_for

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MAX_ATTEMPTS = 3
MAX_UPLOAD_BYTES = 2_000_000

app = FastAPI(title="TriageFlow")
sched = Scheduler()
jobs: dict = {}
jobs_lock = threading.Lock()
stop_event = threading.Event()


def _event(job: dict, stage: str, detail: str = ""):
    job["events"].append({"t": round(time.time() - job["submitted"], 2), "stage": stage, "detail": detail})
    job["status"] = stage


def _public(job: dict) -> dict:
    j = {k: v for k, v in job.items() if k != "files"}
    j["deadline_in_s"] = round(job["deadline"] - time.time(), 1) if job.get("deadline") else None
    return j


# ----------------------------------------------------------------- pipeline
def _triage_and_route(job_id: str):
    job = jobs[job_id]
    try:
        if job["kind"] == "containment_test":
            tri = {"priority": "Routine", "size": "small", "reason": "red-team containment test"}
        else:
            _event(job, "triaging", "triage agent (Vultr Serverless Inference)")
            tri = llm.triage(job["task"], job["department"], job["data_info"])
        job.update(priority=tri["priority"], size=tri["size"], triage_reason=tri["reason"])
        job["deadline"] = deadline_for(tri["priority"], job["submitted"])
        tier, pool, est, why = sched.route(job["department"], tri["size"])
        job.update(tier=tier, pool=pool, route_estimates=est, route_reason=why)
        _event(job, "queued", f"{tri['priority']} / {tri['size']} -> {tier} pool '{pool}' ({why})")
        sched.submit(job_id, pool, tri["priority"], job["deadline"], tri["size"])
    except Exception as e:  # noqa: BLE001
        _event(job, "failed", f"triage error: {e}")


def _execute(job_id: str):
    job = jobs[job_id]
    job["started"] = time.time()
    _event(job, "running", f"picked up by '{job['pool']}' pool")
    previous = []
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if job["kind"] == "containment_test":
                code = CONTAINMENT_PROBE
            else:
                _event(job, "coding", f"coder agent, attempt {attempt}")
                code = llm.write_code(job["task"], job["data_info"], previous)
            _event(job, "sandbox", f"attempt {attempt}: fresh container, {job['tier']} limits")
            res = sandbox.run_code(code, job["tier"], job["files"],
                                   timeout_s=8 if job["kind"] == "containment_test" else None)
            job["attempts"].append({"attempt": attempt, "code": code, "result": res.to_dict()})
            _event(job, "sandbox_done",
                    f"exit {res.exit_code}, {res.duration_s}s, timed_out={res.timed_out}, container destroyed={res.container_destroyed}")
            if res.ok or job["kind"] == "containment_test":
                break
            previous.append({"code": code, "error": (res.stderr or res.stdout)[-2500:]})
            if attempt < MAX_ATTEMPTS:
                _event(job, "self_healing", "error fed back to the coder agent")
        last = job["attempts"][-1]["result"]
        if job["kind"] == "containment_test":
            job["explanation"] = _containment_summary(last)
        elif last["ok"]:
            _event(job, "verifying", "verifier agent reads the real sandbox output")
            job["explanation"] = llm.explain(job["task"], job["attempts"][-1]["code"], last["stdout"], bool(last["artifacts"]))
        else:
            job["explanation"] = f"Failed after {MAX_ATTEMPTS} sandboxed attempts. Last error:\n{last['stderr'][-800:]}"
        job["finished"] = time.time()
        job["met_deadline"] = job["finished"] <= job["deadline"]
        _event(job, "done" if (last["ok"] or job["kind"] == "containment_test") else "failed",
                f"deadline {'met' if job['met_deadline'] else 'MISSED'}")
    except Exception as e:  # noqa: BLE001
        _event(job, "failed", f"error: {e}")
    finally:
        job["files"] = {}  # drop uploaded data from memory
        sched.finish(job["pool"], job_id)


def _worker(pool: str):
    while not stop_event.is_set():
        job_id = sched.next_job(pool, stop_event)
        if job_id:
            _execute(job_id)


@app.on_event("startup")
def _start_workers():
    for pool, cap in POOL_CAPACITY.items():
        for _ in range(cap):
            threading.Thread(target=_worker, args=(pool,), daemon=True).start()


def _new_job(kind: str, task: str, department: str, files: dict) -> dict:
    job_id = uuid.uuid4().hex[:10]
    def _describe(name, blob):
        header = blob.split(b"\n", 1)[0][:200].decode(errors="replace")
        return f"{name} ({len(blob) // 1024 or 1} KB, header: {header})"
    info = ", ".join(_describe(n, b) for n, b in files.items()) or "no files"
    job = {"id": job_id, "kind": kind, "task": task, "department": department, "files": files,
           "data_info": info, "submitted": time.time(), "events": [], "attempts": [], "status": "submitted",
           "llm_mode": llm.mode(), "model": llm.MODEL}
    with jobs_lock:
        jobs[job_id] = job
    threading.Thread(target=_triage_and_route, args=(job_id,), daemon=True).start()
    return job


# ----------------------------------------------------------------- API
@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "llm_mode": llm.mode(), "model": llm.MODEL, "docker": sandbox.docker_available(),
            "sandbox_image": sandbox.SANDBOX_IMAGE, "runtime": sandbox.SANDBOX_RUNTIME or "runc",
            "tier_limits": {k: v.__dict__ for k, v in sandbox.TIER_LIMITS.items()}}


@app.get("/api/samples")
def samples():
    return sorted(p.name for p in DATA_DIR.glob("*.csv"))


@app.get("/api/models")
def models():
    try:
        return {"models": llm.list_models()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/jobs")
async def create_job(task: str = Form(...), department: str = Form(...), sample: str = Form(""),
                     file: UploadFile | None = File(None)):
    if department not in DEPARTMENTS:
        raise HTTPException(400, f"department must be one of {DEPARTMENTS}")
    task = task.strip()[:2000]
    if not task:
        raise HTTPException(400, "task is empty")
    files = {}
    if sample:
        p = DATA_DIR / Path(sample).name
        if not p.is_file():
            raise HTTPException(400, "unknown sample")
        files[p.name] = p.read_bytes()
    if file is not None and file.filename:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large (2 MB max)")
        files[Path(file.filename).name] = content
    return _public(_new_job("analysis", task, department, files))


@app.post("/api/containment-test")
def containment_test():
    return _public(_new_job("containment_test",
                            "RED TEAM: steal the inference API key, reach the internet, write to the host, "
                            "fork-bomb, exhaust memory, then hang forever.", "Emergency", {}))


@app.get("/api/jobs")
def list_jobs():
    with jobs_lock:
        items = sorted(jobs.values(), key=lambda j: -j["submitted"])[:50]
    return [{k: _public(j)[k] for k in ("id", "kind", "task", "department", "status", "priority", "tier", "pool",
                                        "met_deadline", "deadline_in_s", "submitted") if k in j} for j in items]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return _public(job)


@app.get("/api/pools")
def pools():
    return sched.snapshot()


# ----------------------------------------------------------------- red team
CONTAINMENT_PROBE = r'''
import os, socket, subprocess, sys
def report(name, blocked, detail):
    print(("BLOCKED " if blocked else "LEAKED  ") + name + ": " + detail, flush=True)

# 1. Secret hygiene: is the Vultr inference key (or any key) visible?
keys = [k for k in os.environ if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "VULTR", "PASSWORD"))]
report("secret theft", not keys, "env vars visible to sandbox = " + ", ".join(sorted(os.environ)))

# 2. Network egress (exfiltration / downloads)
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3); report("network egress", False, "reached 1.1.1.1:443")
except OSError as e:
    report("network egress", True, type(e).__name__ + ": " + str(e))
try:
    socket.gethostbyname("api.vultrinference.com"); report("DNS lookup", False, "resolved")
except OSError as e:
    report("DNS lookup", True, str(e))

# 3. Host / filesystem tampering
for path in ("/etc/pwned", "/work/main.py", "/usr/local/bin/backdoor"):
    try:
        open(path, "w").write("x"); report("write " + path, False, "write succeeded")
    except OSError as e:
        report("write " + path, True, type(e).__name__)

# 4. Privilege
report("root user", os.getuid() != 0, "uid=%d" % os.getuid())

# 5. Fork bomb (PID limit)
children = []
try:
    for _ in range(500):
        children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]))
    report("fork bomb", False, "spawned 500 processes")
except (OSError, BlockingIOError) as e:
    report("fork bomb", True, "stopped after %d processes (%s)" % (len(children), type(e).__name__))
for c in children:
    c.kill()
for c in children:
    c.wait()

# 6. Memory exhaustion (cgroup limit) in a child process
r = subprocess.run([sys.executable, "-c", "b = bytearray(2 * 1024**3); print(len(b))"], capture_output=True)
report("memory bomb (2 GB)", r.returncode != 0, "child exit code %d (137 = OOM-killed)" % r.returncode)

# 7. Hang forever -> must be killed by the wall-clock limit
print("now hanging forever...", flush=True)
while True:
    pass
'''


def _containment_summary(res: dict) -> str:
    lines = [l for l in res["stdout"].splitlines() if l.startswith(("BLOCKED", "LEAKED"))]
    blocked = sum(l.startswith("BLOCKED") for l in lines)
    leaked = [l for l in lines if l.startswith("LEAKED")]
    s = f"{blocked}/{len(lines)} attacks blocked inside the sandbox."
    s += " Infinite loop killed by the wall-clock limit." if res["timed_out"] else " (loop not killed!)"
    s += f" Container destroyed afterwards: {res['container_destroyed']}."
    if leaked:
        s += "\nLEAKS: " + "; ".join(leaked)
    return s
