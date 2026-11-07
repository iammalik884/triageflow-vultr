# lablab.ai submission — copy/paste material

## Project title
TriageFlow — Priority-Aware, Containment-First Clinical Data Agents

## Short description (≤ 255 chars)
Hospital departments ask their data questions in plain English. TriageFlow's agents triage by clinical priority, schedule across edge/cloud sandbox pools, run real code in throwaway Vultr containers, self-heal and verify the output.

## Long description
When an ICU, an emergency department and a general ward all hand work to AI agents, two things break: a Critical request waits behind a Routine report, and one bad piece of generated code can damage the host or leak credentials. TriageFlow fixes both: who runs first, and what a run can touch.

A clinician picks their department, attaches a de-identified CSV (or a synthetic sample) and asks a question. A triage agent on Vultr Serverless Inference assigns Critical, High or Routine priority. A scheduler taken from my MS thesis on priority-aware healthcare edge scheduling routes the job to the department's own sandbox pool, the least-loaded neighbour pool, or a shared cloud pool using an estimated-completion-time rule, and serves Critical jobs first with earliest-deadline ordering. A coder agent writes Python, which runs in a fresh Docker container on a Vultr VM with no network, no secrets, a non-root user, a read-only filesystem and memory, CPU, PID and time caps. Errors go back to the agent for up to three self-healing attempts, each in a new container. A verifier agent explains only numbers that appear in the real output and marks the result VERIFIED or NOT VERIFIED. Every container is destroyed afterwards, and the UI shows the full timeline, the code, the real stdout, charts and the exact docker flags.

The containment moment: a red-team job tries to steal the API key, reach the internet, write to the host, fork-bomb, allocate 2 GB and hang forever. All nine attacks are blocked and the loop is killed by the time limit.

The research behind it: in the thesis simulator, priority-aware MAPPO cut Critical deadline violations under overload from 64.8% (FCFS) to 0.0%. TriageFlow brings that scheduling model to real, sandboxed agent jobs on Vultr.

## Technology tags
Vultr, Vultr Serverless Inference, Vultr Cloud Compute, Docker, gVisor, FastAPI, Python, AI Agents, Multi-Agent, Code Execution, Sandboxing, Healthcare

## Category tags
Healthcare, AI Agents, Security, Edge Computing

## Challenge
Challenge 1 — Blast Radius Zero (Pattern A: sandboxed code execution)

---

## Demo video script (≈ 3 minutes)

1. **0:00–0:20 — Problem.** "Three hospital departments hand work to AI agents. Two risks: Critical work waits behind Routine work, and generated code can escape. TriageFlow solves both on Vultr."
2. **0:20–0:40 — Architecture.** Show the README diagram: browser → FastAPI control layer on a Vultr VM → Vultr Serverless Inference agents → priority scheduler → throwaway Docker sandboxes.
3. **0:40–1:40 — Real work.** Open the public URL. Click the "ICU deterioration" example → submit. Point at: triage badge **Critical**, scheduler table (Local / Edge / Cloud estimates), timeline (coding → sandbox → verifying), the code the agent wrote, the **real** stdout, the chart, the verifier's VERDICT, "container destroyed: true".
4. **1:40–2:00 — Priority.** Submit a Routine "Ward weekly report" and the Critical ICU job together; show the Critical one runs first / gets the free slot.
5. **2:00–2:40 — CONTAINMENT MOMENT.** Click **Run red-team containment test**. Read the green BLOCKED lines: no secrets in env, network unreachable, read-only filesystem, uid 65534, fork bomb stopped at 63 processes, 2 GB allocation OOM-killed, infinite loop killed at 8 s, container destroyed. Open the attempt and show the exact `docker run` flags.
6. **2:40–3:00 — Research + close.** Show the thesis results table (FCFS 64.8% → MAPPO 0.0% Critical violations). "TriageFlow: agents that execute real work — safely, and in the right order."

## Slides outline (6 slides)
1. TriageFlow — title, team EdgeMind Agents, challenge 1
2. Problem — priority inversion + blast radius of generated code
3. Solution & architecture diagram
4. Containment — the 5 guarantees + red-team result (9/9 blocked)
5. Thesis-backed scheduler — ECT routing + results table
6. Business value & next steps — hospitals/labs running agent jobs safely; MAPPO actors live, auth, multi-VM pools
