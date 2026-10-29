---
name: autobench
description: Register bounded project benchmarks and retrieve current Scheduler-backed timing and profiling evidence with Autobench.
---

# Autobench skill

Use the standalone `autobench` CLI when a project has initialized Autobench.
Project Control may later provide its own registered route to the same engine.

## Typical workflow

1. Check `autobench status` and `autobench doctor --project <workspace>`.
2. Register a small, meaningful packet with an argv array, declared source/data
   scope, expected duration, and hard kill time. Keep expensive profiling in a
   separate packet with a larger explicit envelope.
3. Keep harnesses deterministic and include them in the captured source scope,
   or use an installed Autobench recipe. Use `{snapshot}`, `{build}`, `{data}`,
   `{output}`, and `{artifact.<packet>}` placeholders instead of relying on a
   mutable current checkout during execution.
4. Give dependent packets `depends_on` entries so correctness and preparation
   happen before measurement, and CPU analysis runs after profiling jobs release
   the device.
5. Read `autobench results <benchmark>`, `autobench compare <benchmark>`, and
   `autobench explain <benchmark>` before drawing conclusions. Distinguish stale,
   incomplete, correctness, timing, and profiler evidence.

All benchmark commands execute through the configured Device Scheduler. Do not
run the registered GPU command locally or bypass Scheduler admission. Interactive
GPU work uses the Scheduler's high-priority lease demand; it can preempt flexible
Autobench work after verified cleanup.

`autobench init --agent-instructions=skip` is for a host such as Project Control
that supplies its own repository guidance. The option suppresses only the
Autobench-owned root `AGENTS.md` block.
