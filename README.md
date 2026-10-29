# Autobench

Autobench maintains source-aware benchmark and profiling evidence using spare
capacity admitted by Device Scheduler. It is a standalone engine with a CLI,
project-local registries and bounded evidence storage. Project Control hosting
is planned separately.

The [design](DESIGN.md) records product boundaries and the
[implementation contract](IMPLEMENTATION.md) records the accepted first delivery.
See [runtime guidance](docs/runtime.md) for configuration, recovery, and evidence
retention. [Host qualification](docs/qualification.md) records actual profiling,
source refresh, deadline, quota, and preemption checks separately from unit tests.

Each participating workspace uses `.autobench/` for registrations, build recipes,
generated state and selected artifacts. Live worktree contents are authoritative.
Registered commands run as finite Scheduler jobs; interactive priority demand
preempts flexible background work after verified cleanup.

## Use

Python 3.11+ is required. The base engine has no mandatory runtime dependencies.
Execution needs the separately installed Device Scheduler SDK and a compatible
Scheduler service; source tracing and the Cellerator example additionally use
`project-source` and NumPy. Extras are `scheduler`, `source-trace`, `examples`, and
`test`. Install shared packages from their own supported distributions before
enabling those extras; Autobench does not install other services implicitly.

With the user service and scoped Scheduler access already configured:

```sh
autobench init --project /path/to/project \
  --scheduler-url http://127.0.0.1:8770 \
  --token-file /private/path/to/autobench.token
autobench register --project /path/to/project --file benchmark.toml
autobench doctor --project /path/to/project
autobench status --project /path/to/project
autobench results --project /path/to/project
```

`init` enrolls the workspace and starts the configured coordinator in one command.
It writes only its owned root `AGENTS.md` block by default; a hosting application
can use `--agent-instructions=skip`. It preserves existing state, and reports
incomplete readiness if the service or credentials are missing.

The same engine is available through `autobench.api` for initialization,
registration, and workspace operations. `autobench serve` reconciles all enrolled
workspaces; `pause`, `resume`, `compare`, and `explain` operate on project evidence.

## Bounded evidence

Defaults are 1 GiB retained data per workspace, a configurable 4 GiB host ceiling,
and 128 MiB reserved packet scratch. Temporary writes are monitored, so brief
scratch overshoot is possible; this is not a filesystem quota. Current results,
selected baselines, and compact summaries survive ordinary cache cleanup. Old raw
captures are evicted without automatically rerunning satisfied measurements.

Build, correctness, ordinary timing, Nsys, NCU, and CPU analysis remain separate
finite jobs. Priority acquisition preempts only flexible eligible background work,
and capacity is reused only after Scheduler cleanup verification. Profiler replay
timings are diagnostic, not ordinary throughput measurements.

## Example and checks

The [Cellerator example](examples/cellerator-sparse-support/README.md) builds actual
production sparse-support code and profiles the complete mechanism using an exact
oracle and deterministic **synthetic** inputs. It does not establish biological
validity or a kernel speedup.

```sh
python -m pytest -q
```

Tests use temporary workspaces and fake resource adapters; physical GPU and user
service qualification must run through Device Scheduler separately.

## License

Apache License 2.0. External tools and dependencies retain their own licenses.

