"""Runs the red-team probe in a real container. Needs Docker + the sandbox
image (SANDBOX_IMAGE). Skipped when Docker is not available."""
import os

import pytest

from app import sandbox
from app.main import CONTAINMENT_PROBE

pytestmark = pytest.mark.skipif(not sandbox.docker_available(), reason="docker not available")


def test_probe_is_fully_contained():
    os.environ["VULTR_INFERENCE_API_KEY"] = "sk-must-never-leak"
    res = sandbox.run_code(CONTAINMENT_PROBE, "Local", timeout_s=8)
    lines = [l for l in res.stdout.splitlines() if l.startswith(("BLOCKED", "LEAKED"))]
    assert lines and all(l.startswith("BLOCKED") for l in lines), res.stdout
    assert "sk-must-never-leak" not in res.stdout + res.stderr
    assert res.timed_out and res.container_destroyed


def test_normal_code_runs_and_returns_chart():
    code = ("import matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.savefig('/out/c.png')\nprint('sum', 1+2)")
    res = sandbox.run_code(code, "Local")
    assert res.ok and "sum 3" in res.stdout and len(res.artifacts) == 1
