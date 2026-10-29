# Accepted first implementation

Accepted 2026-10-29. This is an implementation contract, not execution evidence.

## Delivery

- Public repository `tumlinso/autobench`, created through the user's `gh` CLI;
  Apache License 2.0.
- Python 3.11+ standalone engine, Python application API, CLI, agent skill and
  same-user systemd coordinator. No Project Control or Todo runtime dependency.
- Project-local versioned registrations, live-worktree input capture, reusable
  verified builds, continuous coverage and compact provenance-aware queries.
- Device Scheduler extensions for priority lease demand, strict foreground
  ordering, expected duration, cancel-and-abandon packets, hard-kill semantics,
  advisory availability-aware packing and supported bounded own-work history.
- Installed working single-host service after isolated tests and controlled
  cutover, preserving existing records and unrelated work.

## Fixed behavior

`autobench init` prepares and enrolls a workspace and enables configured
reconciliation. `--agent-instructions=write` is the default; `skip` suppresses
only its owned portable AGENTS block. Missing grants/service are incomplete
readiness, not implicit installation or successful activation.

The CLI includes register, status, results, compare, explain, pause, resume,
doctor and serve. Public Python APIs support the same operations for future
hosts. Build, correctness, timing, Nsys, NCU and CPU analysis remain separate
finite Scheduler jobs. Persist submission intent before effects; reconcile
unknown outcomes instead of replaying. A preempted capture is incomplete.

Poll declared scopes every five seconds, settle for two seconds, and supersede
unstarted work with the latest consistent source capture. Do not capture mutable
live files by hardlink or copy whole repositories for every packet. Declared
source/build dependencies remain authoritative; shared project-source trace
adds consumers but a negative trace cannot suppress relevant declared work.

## Storage and scheduling defaults

- Workspace retained-data budget 1 GiB, administrator-configurable 4 GiB default
  ceiling; cleanup targets 80% when space is needed.
- Reserve 128 MiB default packet scratch; monitor every 500 ms and cancel on
  overrun. Brief scratch overshoot is accepted. This is not filesystem quota
  enforcement of arbitrary writes. Retained imports must satisfy the hard cap.
- One active GPU packet per workspace; packets can request multiple devices.
- Expected/hard-kill templates: correctness/timing 5/30 s, profiling 30/120 s,
  CPU build 60/300 s, CPU analysis 10/60 s. Default host runtime ceiling 600 s.
  Explicit registrations can override within host ceilings.
- At most 16 historical summaries per benchmark case; selected current/baseline
  raw captures. Current, baseline and last-good roles can share artifacts.
- Active/unresolved work and authored registrations are protected. Eviction of
  raw bytes does not itself regenerate satisfied evidence. Bound metadata and
  external service copies through supported owner APIs as well as local blobs.

## Real qualification

Use Cellerator's sparse-support production mechanism and exact oracle with a
registered deterministic synthetic fixture. Build captured production sources
with explicit architecture and line information rather than linking stale
prebuilt libraries. Collect ordinary timings, an actual Nsys timeline, a focused
NCU capture and CPU-side analysis with source/build/data/tool/device receipts.

Qualify source-change refresh, consistent captures, duplicate suppression,
restart recovery, real priority-lease preemption and verified cleanup, protected
grants, uncertain outcomes, quota pressure, eviction stability, init preservation
and independent evidence retrieval. No algorithm speedup is required.

## Release

Develop Scheduler changes in an isolated checkout. Integrate tested code and
build pinned artifacts; back up consistent ledgers/configuration before changing
installed services. Admit qualification through Scheduler, verify cleanup and
readiness, then publish validated source and operational documentation. Do not
publish credentials, generated captures or unrelated project state.
