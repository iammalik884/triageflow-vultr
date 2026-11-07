"""
Agent LLM calls through Vultr Serverless Inference (OpenAI-compatible API,
https://api.vultrinference.com/v1). Three agent roles:

  1. Triage agent   - reads the request, assigns Critical/High/Routine
                      priority and a job size (drives the scheduler).
  2. Coder agent    - writes the Python that runs inside the sandbox; on a
                      failure it gets the sandbox error back and retries.
  3. Verifier agent - reads the REAL sandbox output and explains it; it is
                      told never to state numbers that are not in the output.

The API key lives only in this process (env var), never in the sandbox.
If no key is set, a deterministic offline mock is used so the UI still works
for local development; the UI shows which mode is active.
"""

import json
import os
import re
import urllib.error
import urllib.request

BASE_URL = os.environ.get("VULTR_INFERENCE_BASE_URL", "https://api.vultrinference.com/v1")
MODEL = os.environ.get("VULTR_MODEL", "kimi-k2-instruct")


def _api_key() -> str:
    return os.environ.get("VULTR_INFERENCE_API_KEY", "").strip()


def mode() -> str:
    return "vultr" if _api_key() else "mock"


def _chat(messages: list, max_tokens: int = 1200, temperature: float = 0.1) -> str:
    body = json.dumps({"model": MODEL, "messages": messages, "max_tokens": max_tokens,
                       "temperature": temperature}).encode()
    req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=body, method="POST", headers={
        "Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "TriageFlow/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Vultr Inference HTTP {e.code}: {e.read()[:300]!r}") from None
    return data["choices"][0]["message"]["content"] or ""


def list_models() -> list:
    if not _api_key():
        return []
    req = urllib.request.Request(f"{BASE_URL}/models", headers={
        "Authorization": f"Bearer {_api_key()}", "User-Agent": "TriageFlow/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return [m.get("id") for m in data.get("data", data if isinstance(data, list) else [])]


def _extract_code(text: str) -> str:
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    # Truncated reply: opening fence but no closing fence -> strip the fence line.
    text = re.sub(r"^\s*```(?:python|py)?\s*\n?", "", text)
    return text.replace("```", "").strip()


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


# ---------------------------------------------------------------- triage
TRIAGE_SYSTEM = """You are the triage agent of a hospital edge-computing platform.
Classify a data-analysis job requested by a hospital department.
priority: "Critical" = could affect an unstable patient right now (deteriorating vitals,
sepsis/arrhythmia alerts, ICU/ER emergencies); "High" = clinically relevant soon
(same-shift decisions); "Routine" = reporting, statistics, admin.
size: "small" (one quick computation), "medium" (several steps or a chart),
"large" (heavy simulation, many charts, big loops).
Reply with ONLY JSON: {"priority": "...", "size": "...", "reason": "<one short sentence>"}"""


def triage(task: str, department: str, data_info: str) -> dict:
    if mode() == "mock":
        t = task.lower()
        pri = ("Critical" if any(k in t for k in ("critical", "sepsis", "deteriorat", "alarm", "urgent", "arrest", "spo2"))
               else "High" if department in ("ICU", "Emergency") else "Routine")
        size = "large" if any(k in t for k in ("simulate", "monte carlo", "all charts")) else \
            "medium" if any(k in t for k in ("chart", "plot", "trend", "graph")) else "small"
        return {"priority": pri, "size": size, "reason": "offline keyword triage (mock mode)"}
    out = _extract_json(_chat([
        {"role": "system", "content": TRIAGE_SYSTEM},
        {"role": "user", "content": f"Department: {department}\nAttached data: {data_info}\nJob: {task}"}],
        max_tokens=200))
    pri = out.get("priority") if out.get("priority") in ("Critical", "High", "Routine") else "High"
    size = out.get("size") if out.get("size") in ("small", "medium", "large") else "medium"
    return {"priority": pri, "size": size, "reason": str(out.get("reason", ""))[:200]}


# ---------------------------------------------------------------- coder
CODER_SYSTEM = """You are the coder agent. Write ONE complete Python 3 script that does the job.
It runs in an isolated sandbox: NO network, NO pip installs, read-only filesystem except /out and /tmp.
Available libraries: Python standard library, pandas, numpy, matplotlib (Agg backend).
Input files are in /work/data/ (read-only). Save any chart as a PNG in /out/ (e.g. /out/chart.png).
Print the key results clearly to stdout (they will be checked and explained to a clinician).
Do not invent data: compute everything from the files. Keep it short and robust.
Use the column types shown in the file description: integer columns such as `day` or `hour`
are counters (day 1, day 2, ...), NOT dates - never pass them to pd.to_datetime.
Reply with ONLY the code in one ```python block."""


def write_code(task: str, data_info: str, previous: list) -> str:
    """previous: list of {"code", "error"} from failed attempts (self-healing)."""
    if mode() == "mock":
        return _mock_code(task, data_info, previous)
    messages = [{"role": "system", "content": CODER_SYSTEM},
                {"role": "user", "content": f"Files: {data_info}\nJob: {task}"}]
    for p in previous:
        messages.append({"role": "assistant", "content": f"```python\n{p['code']}\n```"})
        messages.append({"role": "user", "content": f"The sandbox run FAILED:\n{p['error'][-2500:]}\nFix the script. Reply with the full corrected code only."})
    # Reasoning models can spend the whole budget before writing code -> empty reply.
    # Give a bigger budget and ask once more if the reply is still empty.
    for _ in range(2):
        code = _extract_code(_chat(messages, max_tokens=4000))
        if code:
            return code
    return ""


# ---------------------------------------------------------------- verifier
VERIFIER_SYSTEM = """You are the verifier agent. You get a job, the code that ran in the sandbox
and its REAL stdout. In 3-5 short sentences explain the result for a clinician.
Only use numbers that literally appear in the output; if the output does not answer the job,
say so plainly. End with one line: VERDICT: VERIFIED or VERDICT: NOT VERIFIED."""


def explain(task: str, code: str, stdout: str, has_chart: bool) -> str:
    if mode() == "mock":
        lines = [l for l in stdout.strip().splitlines() if l.strip()][:6]
        body = " ".join(lines) if lines else "The script produced no output."
        return (f"[mock verifier] Sandbox output: {body}" + (" A chart was produced." if has_chart else "")
                + "\nVERDICT: " + ("VERIFIED" if lines else "NOT VERIFIED"))
    return _chat([{"role": "system", "content": VERIFIER_SYSTEM},
                  {"role": "user", "content": f"Job: {task}\n\nCode:\n{code[:3000]}\n\nStdout:\n{stdout[:3500]}\n\nChart produced: {has_chart}"}],
                 max_tokens=400, temperature=0.0).strip()


# ---------------------------------------------------------------- offline mock
def _mock_code(task: str, data_info: str, previous: list) -> str:
    first = data_info.split(",")[0].split(" ")[0].strip() or "vitals.csv"
    chart = any(k in task.lower() for k in ("chart", "plot", "trend", "graph"))
    code = f'''import pandas as pd
df = pd.read_csv("/work/data/{first}")
print("rows:", len(df), "columns:", list(df.columns))
num = df.select_dtypes("number")
print(num.describe().round(2).to_string())
'''
    if "patient_id" in task.lower() or "news" in task.lower() or "sepsis" in task.lower():
        code += '''flags = df[(df.get("heart_rate", 0) > 110) | (df.get("spo2", 100) < 92) | (df.get("resp_rate", 0) > 24)]
print("patients with warning vitals:", sorted(flags["patient_id"].unique().tolist()) if "patient_id" in df else len(flags))
'''
    if chart:
        code += '''import matplotlib.pyplot as plt
ax = num.iloc[:, :3].plot(figsize=(7, 3.5), title="Vitals trend")
plt.tight_layout(); plt.savefig("/out/chart.png", dpi=110)
print("chart saved")
'''
    return code
