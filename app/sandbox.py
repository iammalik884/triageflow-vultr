"""
Containment layer: every piece of model-written code runs in a fresh,
throwaway Docker container on the Vultr VM, never in the app process.

Containment guarantees (Vultr "Blast Radius Zero" checklist):
  * Process isolation  - separate container, non-root user (65534), all Linux
                         capabilities dropped, no-new-privileges, read-only
                         root filesystem, optional gVisor runtime (runsc).
  * Secret hygiene     - the container gets NO environment from the app: only
                         PATH/HOME/MPLBACKEND are set. The Vultr inference key
                         never enters the sandbox.
  * No network         - --network none (no exfiltration, no downloads).
  * Resource limits    - memory, CPU, PID count, /tmp size and wall-clock time
                         are capped per tier (Local edge pool vs Cloud pool).
  * Lifecycle          - --rm, plus a forced `docker rm -f` in `finally`; the
                         job's input/output folders are deleted afterwards.
"""

import base64
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict

SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "triageflow-sandbox:latest")
SANDBOX_RUNTIME = os.environ.get("SANDBOX_RUNTIME", "")  # e.g. "runsc" for gVisor
MAX_OUTPUT_CHARS = 8000


@dataclass(frozen=True)
class Limits:
    memory_mb: int
    cpus: float
    pids: int
    timeout_s: int
    tmp_mb: int = 32


# Thesis tiers mapped to sandbox pools: department edge pools are small and
# close (fast start), the shared cloud pool is bigger but "further away".
TIER_LIMITS = {
    "Local": Limits(memory_mb=256, cpus=0.5, pids=64, timeout_s=20),
    "Edge": Limits(memory_mb=256, cpus=0.5, pids=64, timeout_s=20),
    "Cloud": Limits(memory_mb=512, cpus=1.0, pids=128, timeout_s=45),
}


@dataclass
class RunResult:
    run_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    oom_killed: bool
    limits: dict
    docker_flags: list
    container_destroyed: bool
    artifacts: list = field(default_factory=list)  # [{"name", "b64"}] PNG charts

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self):
        d = asdict(self)
        d["ok"] = self.ok
        return d


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "\n...[truncated]"


def run_code(code: str, tier: str = "Local", data_files: dict | None = None, timeout_s: int | None = None) -> RunResult:
    """Run `code` (Python) in a fresh container. data_files: {name: bytes},
    mounted read-only at /work/data. Charts saved to /out are returned."""
    limits = TIER_LIMITS[tier]
    if timeout_s:
        limits = Limits(limits.memory_mb, limits.cpus, limits.pids, timeout_s, limits.tmp_mb)
    run_id = "tf-" + uuid.uuid4().hex[:12]
    root = tempfile.mkdtemp(prefix=run_id + "-")
    work, out = os.path.join(root, "work"), os.path.join(root, "out")
    os.makedirs(os.path.join(work, "data"))
    os.makedirs(out)
    os.chmod(out, 0o777)
    with open(os.path.join(work, "main.py"), "w", encoding="utf-8") as f:
        f.write(code)
    for name, content in (data_files or {}).items():
        with open(os.path.join(work, "data", os.path.basename(name)), "wb") as f:
            f.write(content)
    for dirpath, _, files in os.walk(work):
        os.chmod(dirpath, 0o755)
        for fn in files:
            os.chmod(os.path.join(dirpath, fn), 0o644)

    flags = [
        "--rm", "--name", run_id,
        "--network", "none",
        "--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m",
        "--cpus", str(limits.cpus),
        "--pids-limit", str(limits.pids),
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,size={limits.tmp_mb}m,mode=1777",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "-v", f"{work}:/work:ro",
        "-v", f"{out}:/out:rw",
        "-w", "/work",
        # The ONLY environment the sandbox sees (no secrets):
        "-e", "PATH=/usr/local/bin:/usr/bin:/bin", "-e", "HOME=/tmp",
        "-e", "MPLBACKEND=Agg", "-e", "MPLCONFIGDIR=/tmp",
    ]
    if SANDBOX_RUNTIME:
        flags += ["--runtime", SANDBOX_RUNTIME]
    cmd = ["docker", "run", *flags, SANDBOX_IMAGE, "python3", "-I", "/work/main.py"]
    shown_flags = [f for f in flags if not f.startswith(root)]

    start = time.time()
    timed_out, stdout, stderr, code_ = False, "", "", -1
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=limits.timeout_s + 5)
        stdout, stderr, code_ = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = f"Killed by TriageFlow: exceeded {limits.timeout_s}s wall-clock limit."
    finally:
        subprocess.run(["docker", "rm", "-f", run_id], capture_output=True)
    duration = time.time() - start
    # exit 137 = SIGKILL (memory cgroup OOM or our kill)
    oom = code_ == 137 and not timed_out

    artifacts = []
    for fn in sorted(os.listdir(out))[:4]:
        p = os.path.join(out, fn)
        if fn.lower().endswith(".png") and os.path.getsize(p) < 3_000_000:
            with open(p, "rb") as f:
                artifacts.append({"name": fn, "b64": base64.b64encode(f.read()).decode()})
    gone = subprocess.run(["docker", "inspect", run_id], capture_output=True).returncode != 0
    shutil.rmtree(root, ignore_errors=True)
    return RunResult(run_id, code_, _clip(stdout), _clip(stderr), round(duration, 2),
                     timed_out, oom, asdict(limits), shown_flags, gone, artifacts)
