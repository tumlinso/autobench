# Autobench runtime

## Project layout and registration

`autobench init` creates `.autobench/registry/`, compact SQLite coordination state,
build and evidence caches, and bounded scratch locations. Registry TOML and the
`config.json` are authored project settings. State, builds, evidence, snapshots,
and scratch are ignored by Git. Initialization preserves or creates
`.autobench/.gitignore`, which keeps machine-local/generated state out of Git
while leaving registry TOML trackable. `config.json` and `enrolled.json` are
generated workspace-local activation state, not shared registration settings.
The default retained-data cap is 1 GiB; hosts may
set a cap up to 4 GiB. Before admission, the coordinator reserves the configured
128 MiB scratch budget against the retained-data cap. The Scheduler-launched
worker monitors the full managed `.autobench` tree every 500 ms and terminates
its owned child if a packet exceeds its scratch budget or the retained cap; this
also applies when the global coordinator is between workspace polls. Device
Scheduler still controls job cleanup and capacity release. The worker limits
each output file to 64 MiB; a write can briefly exceed the reserved budget
before the next monitor check. Registration and project data are not eviction
targets.

Register one or more bounded packets in `.autobench/registry/<name>.toml`:

```toml
[benchmark]
name = "example"
revision = 1

[source]
include = ["src/**", "bench/example/**"]

[[packet]]
name = "correctness"
kind = "correctness"
argv = ["python3", "{snapshot}/bench/example/run.py", "--output", "{output}"]
expected_seconds = 5
kill_seconds = 30
resources = { cpu_cores = 1, host_memory_bytes = 1073741824 }
```

Commands are argv arrays and never run through a shell. `source.include` and
optional `data.paths` are relative project globs. Autobench snapshots only those
paths and omits `.git`, `.autobench`, symlinks, virtual environments, and common
dependency caches. `{snapshot}`, `{data}`, `{project}`, `{build}`, `{output}`,
`{autobench}`, and `{artifact.<packet-name>}` are substituted in argv and
environment values. Keep harnesses in declared source scope, or ship them as
Autobench package recipe resources. Packet dependencies use
`depends_on = ["packet-name"]`; downstream packets wait for current successful
dependencies. Put compact numeric measurements in `metrics.json` beneath
`{output}`. Raw reports and any other outputs are retained with the packet.

The worker routes `TMPDIR`, `TMP`, `TEMP`, `XDG_CACHE_HOME`, and
`CUDA_CACHE_PATH` into monitored per-attempt scratch and removes those temporary
directories after the command exits. A command may set these variables
explicitly in its `environment` table; the requested paths must remain inside
that attempt's `{output}` directory. Other registered environment values retain
their configured precedence.

Import a full TOML definition through the CLI so it receives the same validation
as the runtime registry:

```sh
autobench register --project /path/to/workspace --file /path/to/benchmark.toml
```

Existing registrations are preserved. An explicit replacement requires a
higher `[benchmark].revision` and `--replace-revision`.

Each packet sets `expected_seconds` for scheduling estimates and `kill_seconds`
for its hard command deadline. The default host ceiling is 600 seconds.
Expected duration does not extend the kill limit. A registration authorizes its
declared argv and resource request; it does not grant Scheduler permissions.
Successful durations from the most recent 16 compatible attempts inform the
next forecast as `max(expected_seconds, p90 × 1.2)`, capped to leave the
Scheduler's five-second termination grace when the registered envelope permits.
Compatibility includes benchmark packet, protocol, data, and selected device.
The estimate never changes the hard kill bound.

The compact resource form uses `cpu_cores`, `host_memory_bytes`, and
`gpu_memory_bytes`; Autobench translates these to Scheduler `cpu/cores`,
`memory/bytes`, and `gpu/memory_bytes` requirements. GPU packets are matched to
forecast-selected UUID resource IDs, request exclusive access by default, and
include the Scheduler `gpu.quiescence` precondition. Registrations that require
additional isolation domains may declare Scheduler `resources.scheduler`
requirements or `resources.topology_constraints` explicitly.

## Scheduler boundary and recovery

The coordinator uses the authenticated Device Scheduler Python client and
submits the internal worker as a durable job. The worker runs registered argv
only inside its Scheduler job attempt, limits stdout/stderr and output files,
and stages results under `.autobench/scratch/`. The coordinator imports a result
only after a terminal Scheduler job reports `cleanup_verified`. A missing
cleanup receipt is unresolved and blocks later submissions for that workspace.

Submission intent is committed to SQLite before the network call. Job keys are
stable per attempt; an interrupted/unknown submission is not blindly retried.
An operator can inspect `autobench status` and Scheduler's own job ledger, then
leave the same attempt unresolved until the Scheduler can be queried again.
Autobench does not locally kill GPU work or infer cleanup from a timeout.

Jobs are background and `flexible=true`, with preemption action `abandon` so an
interactive lease demand can cancel them and the coordinator can submit a fresh
attempt after verified cleanup. The coordinator keeps one active packet per
workspace. Scheduler remains responsible for host-wide resource placement and
foreground preemption.

## User service

Autobench does not install user services or modify global agent configuration.
If a user systemd unit is already installed, `autobench init` enrolls the project
in the user-level catalog and starts the single `autobench-coordinator.service`,
which reconciles all enrolled workspaces. The shipped unit is
`deploy/autobench-coordinator.service`; a host administrator/user may install it
and run `systemctl --user daemon-reload` separately. Without the unit or
Scheduler credentials, initialization reports incomplete readiness while
leaving the project registry usable.

`autobench doctor --project <workspace>` reports missing Scheduler URL/token
configuration, coordinator unit availability, and missing `nsys`/`ncu` tools
required by registered profiling packets. The user unit expects the release
entrypoint at `~/.local/bin/autobench`; the worker prepends the active release's
Python executable directory to registered command PATH so `python3` resolves to
the same isolated runtime.

The catalog in the XDG user state directory stores workspace paths and activation
state only. Registration and execution data remain project-local. The coordinator
polls registrations every five seconds and settles source
changes for two seconds before creating a snapshot. Optional
`project-source.SourceIndex.trace` data is diagnostic only; declared input scope
always controls freshness, including when a trace is empty or incomplete. The
optional index is built from the immutable declared-source snapshot, excludes
declared data and generated state, and is skipped when file/byte or retained-cap
budgets are exceeded; it does not claim a transitive source closure.

## Evidence and retention

SQLite stores compact attempts, current coverage, and up to 16 recent attempt
summaries per benchmark. The artifact tree keeps current and previous baseline
outputs per packet; older reports are eligible for cleanup. Registration files,
current evidence, active outputs, and unresolved attempt records are preserved.
`autobench results`, `compare`, and `explain` read project-local Autobench
evidence and do not append to a shared evidence store.
