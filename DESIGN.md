# Autobench design

**Status:** accepted direction; implementation in progress  
**Updated:** 2026-10-29

This document captures the direction discussed so far. It distinguishes user-confirmed requirements, observations from inspecting neighboring source checkouts, and design proposals. The accepted first implementation and subsequent user choices are recorded in [IMPLEMENTATION.md](IMPLEMENTATION.md); runtime capability claims require separate qualification evidence.

## Purpose

Autobench continuously keeps useful benchmark and profiling evidence current for registered code and test data. A developer or agent declares what should be measured and how. Autobench notices when relevant inputs change, prepares the required executable, and uses spare device availability to run small pieces of that work. The developer can continue foreground implementation while useful background evidence accumulates.

For example, an agent can use KDA to improve a Cellerator kernel and use a device immediately. A registered Cellerator mechanism benchmark can then detect the source change, rebuild the larger program that includes the kernel, and profile the real mechanism when suitable capacity becomes available. The agent should be able to retrieve that evidence later with its source, build, input, hardware, and profiling provenance intact.

The primary model is continuous coverage from standing benchmark registrations. A registration describes an objective and a repeatable way to evaluate it; it does not require a large one-time campaign. A large mechanism can be covered by multiple short, independently useful packets.

## Decision status

### User-confirmed direction

- Autobench continuously fills missing or stale benchmark and profiling coverage for registered objectives.
- The live worktree, including uncommitted changes, is the default source authority. Project Control may eventually supply richer branch/worktree tracking, but standalone Autobench must identify its workspace and exact source contents independently.
- Autobench has a separate, independently usable engine, following the Todo Orchestrator model. Project Control can host a richer interface to that engine later; Device Scheduler remains the physical scheduling authority.
- Every packet registration declares both an **expected duration** and a **hard kill time**. Work should be bounded and generally short enough to fit around interactive development. Proposed starting values appear below; they are tunable and not measured or qualified.
- Benchmarks must be able to exercise and profile the real program or mechanism. Kernel-only microbenchmarks can be useful, but they do not substitute for larger mechanism evidence when that is the registered objective.
- An interactive high-priority device request should automatically preempt flexible background work, including Autobench packets, subject to safe cancellation and verified cleanup. Autobench should require no manual intervention for that preemption.
- Every project workspace where Autobench is active gets a `.autobench/` directory for project benchmark registries, build command lists, and artifacts. Its size must be bounded; it must not become a full-detail history of every run.
- C++ Context Compiler (ctxpp) may eventually help determine source freshness. It is undergoing substantial changes, and not all project code is C++ or CUDA. Autobench therefore needs a language-neutral freshness and rebuild path; ctxpp should be an optional source of stronger C++ dependency information.
- Project Control integration is a future direction and is deferred. Project-specific registrations and evidence should be usable without Project Control.
- Standalone initialization is one command that prepares the project, enrolls it with the configured service, and activates reconciliation. There is no second required enable command. A hosting application may suppress Autobench's agent instructions and provide its own route to the engine.

### Source observations (checkout inspection only)

These describe inspected source and documentation, not a fresh test or guarantee about the currently deployed service.

- Device Scheduler supports durable supervised jobs, resource admission, background priority, restart-safe cancellation, and ordinary current-capacity backfill. A job has a hard timeout; the inspected job contract has no separate expected-duration estimate or general future-capacity planner. Its recurring schedules and current-fit backfill do not combine benchmark packets around forecast availability. Relevant source: `device-scheduler/packages/device-scheduler-core/src/device_scheduler_core/models.py`, `policy.py`, `application.py`, and `device-scheduler/docs/using-v1.md`.
- Current preemption is tied to queued jobs. `leases.request` directly requests a lease; it does not queue a high-priority demand or invoke background-job preemption. A caller-owned supervised reservation is protected from policy preemption. This is a gap against the desired interactive lease behavior. Relevant source: `device-scheduler/packages/device-scheduler-core/src/device_scheduler_core/application.py`, `device-scheduler/packages/device-scheduler-core/src/device_scheduler_core/policy.py`, and `device-scheduler/packages/device-scheduler-mcp/src/device_scheduler_mcp/kda_bridge.py`.
- Scheduler cancellation and resource cleanup are verified before capacity is reused, but the inspected configuration and qualification evidence do not establish a universal preemption-latency guarantee. Checkpoint/resume is not a general job capability. GPU memory limits are accounting, and topology/profiler isolation has documented qualification limits. Relevant source: `device-scheduler/packages/device-scheduler-adapters/src/device_scheduler_adapters/systemd.py`, `device-scheduler/docs/using-v1.md`, and `device-scheduler/planning/capability-matrix.md`.
- Project Foundations provides standalone `project-source` and `project-evidence` packages. Source indexing supplies file identities/content hashes and some structural information; its structural support varies by language and it does not provide a general build dependency closure or change event service. Its ctxpp adapter reads exported information and does not run or refresh ctxpp. `project-evidence` can retain immutable observations, source references, and content-addressed artifacts. These packages can inform an eventual Project Control integration without making Project Control a runtime requirement. Relevant source: `project-foundations/docs/API.md` and the package READMEs.
- KDA provides useful examples of source/build fingerprints and cached builds. Its experiment engine is candidate-comparison machinery, not a continuous project-wide registry or scheduler. KDA and its NCU reporting guidance support profiling real program entry points with representative inputs; full Nsight Compute profiles can replay a short kernel for seconds or a long kernel for minutes. Profile replay timing must remain distinct from ordinary performance timing. Relevant source: `kda/kda_experiments/engine.py`, `kda/docs/experiment-engine.md`, and `kda/skills/ncu-report-skill/reference/03-collection.md`.

## Project workspace and bounded storage

### Agreed location, proposed organization

The project-local `.autobench/` directory is part of the design. Its internal layout and format are open. One possible layout is:

```text
.autobench/
  registry/       # benchmark, dataset, and build recipe registrations
  state/          # freshness, coverage, pending work, and compact indexes
  builds/         # reusable build outputs
  evidence/       # summaries, measurements, and selected profiler reports
  scratch/        # temporary inputs and outputs for active packets
```

The registration is declarative project configuration. Generated state, builds, evidence, and scratch are managed artifacts and should ordinarily be excluded from version control. Whether registration files are tracked by default, and how shared registrations are reviewed, remain open questions. Automatic cache eviction must preserve authored registrations, build recipes, and the compact state required to reconcile active work and determine current coverage. Exact data should normally be referenced in place rather than copied into `.autobench/`. Avoid copying a complete repository or duplicating project-source/ctxpp indexes where a file manifest, content identities, or an existing source utility will do.

### Retention intent

**Agreed:** storage is bounded and Autobench does not keep full detail for every execution forever.

**Proposed retention roles:** preserve compact current coverage state, a comparable baseline, and the last successful result. Multiple roles may point to the same artifacts. Retain selected recent raw profiler captures when they are especially useful; summarize and discard other raw reports when permitted. Bound metadata, logs, source/build caches, and indexes as well as obvious large reports.

Every byte under `.autobench/`, including scratch and generated summaries, should count toward a configured total budget. Per-category budgets, maximum packet outputs, and active-work headroom should prevent one workload from crowding out all others. Before admitting a packet, storage pressure should trigger eligible cleanup; if there is not enough safe space, defer the packet with an inspectable reason. Never remove artifacts belonging to active work. A quota also needs an enforcement mechanism during active work: reserving a declared output allowance, limiting output, or another bounded write path should prevent a process from exceeding its allocation before retrospective cleanup can run. How to handle an artifact that exceeds its allowance without corrupting or mislabeling evidence remains open. This is a bound on Autobench-managed storage, not a guarantee about total filesystem free space or other host users.

Evicting a raw report must not make coverage unsatisfied by itself or cause the same profile to run forever. Evidence completion and raw-artifact retention are separate states. A compact record can say that a report existed, what it concluded, and that its raw bytes were evicted; a digest must not imply the bytes remain available. An explicit pin or keep request could override ordinary retention but would still count against the budget and may block new work. Pin semantics and quota behavior are undecided.

The project-local budget does not automatically bound disk used by a global Autobench coordinator or Device Scheduler. Their ledgers, captured job output/logs, receipts, temporary files, and artifacts may be stored outside `.autobench/`. End-to-end storage limits therefore require an ownership and retention agreement across those components; hidden duplicate histories must not bypass a project's limit. The inspected `project-evidence` API describes append-only observations and content-addressed blobs. This review did not establish a supported pruning or garbage-collection API, and Scheduler ownership/retention is not established here. Immutable evidence records need not require infinite physical retention, but deletion or compaction must use an explicit supported mechanism, such as owner-scoped retention or export/segmentation, rather than direct database edits. The implementation and migration implications are open.

### Proposed starting limits

These values are engineering defaults to make a bounded first implementation concrete. They are not user-provided targets, hardware measurements, or claims of optimality. Templates can be tuned from observed complete runs; host policy can lower or raise project limits within administrator-configured ceilings.

| Limit or packet template | Starting value | Behavior |
|---|---:|---|
| `.autobench/` managed storage per workspace | 1 GiB | Project registrations may choose a different limit within the host policy ceiling. The initial host ceiling is 4 GiB per workspace; an administrator can change it. |
| Cleanup target | 80% of project limit | When admission needs room, reclaim eligible cache and old detail toward this level before reserving packet output. |
| Scratch allowance per active GPU packet | 128 MiB | Count it once against the same workspace budget and reserve it before launch. It can be raised within remaining project and host limits. |
| Raw profiler output per packet | 128 MiB | Default cap included in the packet's scratch allowance. Larger Nsight Compute captures need explicit registration and a larger bounded reservation. Do not silently discard a partial report or count a shared artifact twice. |
| Active GPU packets per workspace | 1 | A packet may request multiple GPUs when the workload requires them. Obtain fresh Scheduler admission at packet boundaries and hold no project GPU reservation between packets; host-wide admission still applies. |
| Correctness or ordinary timing packet | 5 s expected; 30 s hard kill | Include startup and warmup in runtime; planning adds a separate termination/cleanup allowance. A template, not a correctness intensity or scientific validity requirement. |
| Targeted profiling packet | 30 s expected; 120 s hard kill | Focused capture template. Full captures that exceed it need an explicit larger limit. |
| CPU build packet | 60 s expected; 300 s hard kill | Build before requesting GPU capacity; verify inputs and output identity. |
| CPU analysis packet | 10 s expected; 60 s hard kill | Run after releasing GPU resources when possible. |
| Host default hard-runtime ceiling | 600 s | Administrator-configurable. A project packet may use a larger value only when host policy permits it. |

Registrations must declare or explicitly accept expected and kill times; templates supply convenient starting values. Rolling conservative observations may adjust packing estimates and should include startup, warmup, and capture overhead. A missing Nsight estimate is the cost of the profiling packet, not an estimate of kernel runtime. Estimates never extend the registered hard limit. If a requested output reservation does not fit the remaining workspace budget, defer the packet instead of bypassing the quota. Actual output enforcement, truncation semantics, and Scheduler/global-store charging need qualification.

These initial numbers are tunable templates for packet size, not lower bounds or prescribed correctness intensity. A standalone capture can explicitly request a larger report/runtime reservation within project and host ceilings. A shared artifact or build is charged once to its owner budget, even when several packets reference it.

## Registrations and freshness

### Standing benchmark registration

A registration is a durable request to maintain a defined kind of evidence for declared inputs and source scope. The following fields are a starting point for discussion, not a settled schema:

- Stable project-local identity and registration revision.
- Benchmark purpose, mechanism or workload, parameter set, and expected measurement names/units.
- Repository and worktree selection, watched paths, and relevant source/build inputs.
- Dataset identities, versions or content identities, location, and access requirements.
- Build command (or another trusted builder), working directory, environment/toolchain identity, outputs, and whether a verified build may be reused.
- Validation commands and required correctness checks.
- Timing, timeline, and hardware-counter profiling procedures, with representative cases and required artifacts.
- Scheduler resource needs, exclusivity/isolation requirements, expected duration, hard timeout, and declared cancellation/restart behavior for each packet type.
- Evidence currentness, refresh policy, urgency, resource budget, and retention class.

Build commands and benchmark executables are trusted executable configuration, not arbitrary content to discover and run from plans, messages, or model output. The registration interface should make the execution identity and scope inspectable.

### Three freshness questions

Autobench must keep distinct:

1. **Source freshness:** have inputs relevant to this registration changed?
2. **Build freshness:** does a verified executable correspond to those exact inputs, build command, toolchain, and dependencies?
3. **Evidence freshness:** does existing evidence refer to the same source/build, data, hardware target, and measurement protocol now requested?

A project-source file index or ctxpp index can help answer the first question, but an index alone does not prove a build is current. A content-addressed build fingerprint, such as the pattern in KDA, is a useful proposal. Registrations must declare build outputs and important environment inputs. Autobench should verify output identities after a successful build.

The initial language-neutral path can use explicit file/path scopes, content digests, configured rebuild commands, and a manifest of known build inputs. Where dependency coverage is incomplete, Autobench should conservatively mark evidence stale when in-scope inputs change and clearly report the coverage gap. It must not claim that a source index found the full transitive build closure unless a qualified provider supports that claim.

Ctxpp can later be plugged in as an optional structural provider for opted-in C++ projects. Autobench should qualify the exact export generation and source identity it consumes, handle stale or partial exports as incomplete coverage, and fall back to the general path. The current design does not depend on ctxpp behavior or interfaces while its development is active.

### Live worktree authority and source snapshots

**Agreed default:** a registration follows the live worktree bytes, including dirty changes. Each registration and its evidence also bind to a stable project/workspace identity and exact content identities. Project Control may later track branching and multiple worktrees; the independent Autobench engine must still distinguish which workspace and content produced each result.

The stable snapshot mechanism remains a technical question. Each build and packet must see a consistent input set; an edit during execution must not silently mix source revisions. Coalesce changes per registration so an agent can keep working and the latest settled content is queued, not one job per save. A content manifest identifies files but does not itself freeze them: the implementation must materialize the declared inputs, read an immutable revision, or detect concurrent changes and reject a mixed-input build. Materialization should be scoped and reusable rather than copying a full repository per execution. A separate committed/integrated registration mode may be useful later, but it is not the live-worktree default.

### Reusing Project Control's trace internals

Project Control's public `trace()` currently routes through its `InformationService` to the standalone `project_source.SourceIndex.trace` API. That library is the reuse seam: Autobench can call it directly without importing Project Control or invoking a PC MCP server. PC also constructs a separate private `TraceService` for its impact path; that PC-coupled implementation is not the public trace tool's portable dependency.

Incoming traversal can suggest consumers of changed files or symbols and help explain which benchmark is relevant. Autobench must supply its own association between registration revisions, source targets/paths, and build inputs; the source graph does not itself register benchmarks. The implementation refreshes current file observations before traversal, but relation coverage still depends on the language and structural providers. It supplies neither a general build dependency closure nor a continuous watcher.

Treat trace results as additional invalidation evidence. A complete trace within the available indexed graph, or an empty edge list, does not prove that no unobserved dependency exists. Autobench must not skip declared relevant work solely because a trace returned no relation. Keep declared scope authoritative and invalidate conservatively when coverage is incomplete. Reuse a workspace index and share bounded traversal results across a refresh cycle rather than scanning or building a persistent source graph for each benchmark or packet. Ctxpp provider generations and source identities still need validation when used.

## Continuous coverage and packet scheduling

Autobench, as a standalone engine, should maintain a queue of unmet registered objectives and reconcile it against source snapshots, builds, Scheduler jobs, and evidence. Device Scheduler is the host-wide physical planner and executor, not the owner of Autobench's registry, source coverage, build recipes, or scientific evidence. Project Control may later host a richer registered interface to the same engine; it is not a requirement to implement the engine inside Scheduler or Project Control. The work loop is proposed as:

1. Observe source and registration changes; coalesce changes to the newest eligible snapshot.
2. Reconcile existing jobs and uncertain outcomes before submitting anything new.
3. Determine which build and evidence outputs are missing, stale, or failed.
4. Form bounded packets from useful ready work, respecting dependencies such as build then correctness then measurement/profile.
5. Ask Device Scheduler to pack compatible work against present and anticipated availability.
6. Submit only authorized packets, retain their durable job/attempt identity, and inspect until execution and cleanup are resolved.
7. Validate result provenance and content, update current coverage, and apply bounded retention.

Packets should be short, cancellable, and useful on their own. A large mechanism may take many packets, but an agent can keep coding while that work proceeds. Autobench can supply dependency and grouping hints for shared verified builds, data, and setup; Scheduler uses generic hints, expected durations, and available or anticipated windows to place compatible packets. Within a shared-device group, execute packets sequentially with independent outcomes and reconsider placement after each job. Independent resources may run work concurrently where interference is excluded. Expected runtime plus cleanup margin must fit a proposed window, but is not a completion guarantee. Do not overlap work that interferes with timing or profiler isolation. Autobench owns benchmark/data dependency formation and scientific result interpretation. Scheduler owns global resource placement, execution priority, cancellation, and physical cleanup.

No single expected-duration value should be used as the kill limit. Expected duration estimates the time a packet is likely to need; hard timeout bounds the launched process. Each packet class (build, correctness, ordinary timing, Nsight Systems, Nsight Compute, analysis) may need a different estimate and maximum. A packet interrupted by preemption is incomplete. It can be retried as a new attempt only after Scheduler verifies the old process is stopped and resources are clean. There is no general checkpoint/resume assumption.

Retries and failed rebuilds also need bounded backoff: a broken or repeatedly interrupted registration must not consume every newly available window. Current Scheduler preemption requires a background job to declare cancellation and restart safe and to have an attempt remaining. A retry limit must not accidentally turn a previously preemptible benchmark into work that blocks foreground demand. Whether the Scheduler can cancel and abandon such a packet, or Autobench must stop submitting it before that state, needs an explicit contract.

Availability forecasts are estimates, not reservations. Scheduler must recheck real capacity when work starts. The planner should account for packet duration, cleanup margin, future known demand and resource compatibility, while exposing forecast confidence or limits. No design should promise a packet will finish merely because its expected duration fits an estimated idle window.

## Device Scheduler contract changes

Autobench depends on Scheduler as the host-wide capacity and execution authority. The following are gaps in the inspected Scheduler checkout; exact APIs and release ordering remain undecided.

### Core extensions needed for this model

- **Separate expected duration from hard timeout.** Accept an optional duration estimate on schedulable work. Preserve the existing timeout as the hard runtime bound. Use the estimate for planning/backfill only; report observed durations so estimates can improve without silently rewriting a registration's maximum.
- **Anticipated-availability planning and duration-aware backfill.** Provide a planner that considers known queued work, resource releases or constraints, compatibility, and durations. It should form or admit small independent packet groups into plausible windows, protect high-priority interactive demand, return reasons and uncertainty, and recheck admission against live capacity. Current-fit `safe_backfill`, `not_before`, deadlines, and recurring schedules do not provide this behavior.
- **A foreground preemption-demand route for high-priority leases.** Direct high-priority lease requests currently are not queued or preemptive, while queued job submission can cancel eligible background jobs. Extend lease demand so a declared interactive request automatically preempts flexible background jobs, including Autobench packets, until enough requested capacity is reclaimed. Keep caller-owned or otherwise protected grants out of the preemption set. Issue the interactive grant only after cancellation and cleanup are verified; unknown cleanup still blocks capacity, so no zero-latency promise is possible.
- **Strict foreground ordering.** Queue aging can make an old background job outrank a new interactive job in current policy. Interactive demand must remain ahead of background work regardless of background age. Retain fairness within non-foreground work without allowing coverage jobs to delay foreground demand.
- **Cancellation and cleanup bounds with evidence.** Define the packet interruption contract and a maximum or qualified range for termination/cleanup. Include cancellation grace and cleanup margin in planning. A timeout or lost connection must not imply that the process stopped; unknown cleanup must keep capacity unavailable and require inspection/reconciliation before retry.

### Related qualification needs

- Qualify profiling resource isolation, including the GPU UUID and any PCIe/NVLink domains that can affect the capture. Current Scheduler evidence records gaps for profiler/topology isolation on a suitable domain.
- Qualify background-job preemption through real profiling wrappers, including partial-report preservation and cleanup after termination.
- Qualify the high-priority lease-demand path, strict foreground ordering, preemption of Autobench packets, protected-grant exclusions, and busy/unknown cleanup behavior before promising immediate resource availability.
- Keep model eviction or managed-service handoff out of ordinary benchmark scheduling unless explicitly authorized through its separate finite handoff contract. Autobench must not infer permission to unload or suspend unrelated services.

Coordinate exact operation schemas, forecast format, and cleanup signals with Device Scheduler maintainers. Recurring schedules and the managed-model handoff batch are not an availability planner or generic benchmark packet API.

## Profiling and evidence quality

Profiling should run the registered real program path with representative registered data. It must preserve which executable, source/build identity, invocation parameters, workload shape, device, profiler/tool version, and capture recipe produced each artifact.

Correctness, ordinary performance measurement, Nsight Systems timeline capture, Nsight Compute counters, and sanitizer results answer different questions. Registrations should declare which are required. Profile replay time is diagnostic and must not be presented as ordinary throughput. A profile report that is truncated, incomplete, mismatched to the binary, or from an interrupted attempt cannot satisfy a complete evidence requirement.

Longer full profiles should be optional packets with explicit expected time, hard timeout, resource isolation, and retention policy. A concise summary may remain after raw report eviction. Analysis that does not need a GPU should run after releasing the device. The first registration examples should include at least a short real mechanism timing packet and a focused profile packet, while showing that full captures may require minutes rather than seconds.

## Result retrieval and Project Control

Autobench should provide project-scoped queries for current evidence, historical comparable baselines, freshness reasons, and pending/failed work. A result lookup should be able to name a benchmark and input case, then return evidence bound to workspace and live source content, build identity, data identity, hardware/software environment, and protocol. It should distinguish:

- current complete evidence;
- older evidence that remains useful but is stale for the current source;
- missing or partial evidence;
- failed correctness/build/profile results;
- interrupted attempts and unresolved Scheduler cleanup;
- compact summaries whose raw report was evicted.

The shared Project Evidence records look useful for storing attributed observations and artifact references; the Project Source records can identify exact source bytes. Autobench still needs its own registration, coverage, comparison, and query model. Project Control can later expose a richer registered interface to the same engine. The engine itself remains independently useful without Project Control. Service connection details, authentication, and the future PC adapter are deferred technical work; they do not change the engine's ownership boundary.

## Initialization and agent instructions

The CLI convention is agreed:

```text
autobench init [tool-specific options]
autobench init [tool-specific options] --agent-instructions=skip
```

`--agent-instructions=write` is the default. On a standalone project, initialization creates or updates only Autobench's identifiable block in the repository-root `AGENTS.md`:

```markdown
<!-- agent-interface:autobench:start -->
This repository has a canonical Autobench interface.
Route benchmark registration, background measurement, profiling, and benchmark evidence requests through `autobench`.
Consider installing the [Autobench skill](https://github.com/tumlinso/autobench)
to simplify development; if installed, read its `SKILL.md` before use.
<!-- agent-interface:autobench:end -->
```

The marker identity is stable regardless of installation location. The content uses portable command names and routing guidance, not machine-specific paths. Initialization creates `AGENTS.md` if needed; otherwise it inserts or updates the one owned block, prevents duplicates, and preserves surrounding text. Skill suggestion does not install a skill or change global agent configuration. Initialization of `.autobench/` must follow Autobench authority and preservation rules; it must not replace existing state implicitly.

A hosting application can pass `--agent-instructions=skip` and supply its own instructions. In the intended Project Control arrangement, the Project Control initializer suppresses Autobench's standalone block and routes agents through Project Control's registered interface to the Autobench engine. Explicit host takeover may replace the known Autobench block with Project Control's block while preserving other instructions and Autobench state. Project Control's repository initializer is still being developed, and this integration is deferred. No shared database or common setup framework follows from the marker convention.

`init` should prepare `.autobench/`, enroll the project with the configured service, and activate reconciliation so there is no second mandatory enable command. A workspace with no registrations remains idle. A standing registration authorizes its declared bounded commands and resource envelopes, subject to host grants; that should be enough for the intended automatic build-and-profile refresh. If the required service, credentials, or host grant is missing, report incomplete readiness and what is needed. Do not claim execution is active or silently install/configure a service. `--agent-instructions=skip` suppresses only the agent block; it does not disable engine setup or reconciliation. Connection, credentials, service enrollment, and readiness are technical implementation details.

## Remaining implementation questions

The product direction is set: live worktree freshness, a standalone Autobench engine, automatic enrollment/reconciliation from `init`, bounded packet templates, and high-priority lease demand that preempts eligible background work. The following are technical details to resolve while implementing that direction.

| Technical question | Implementation concern |
|---|---|
| How does Autobench freeze declared live-worktree bytes into a stable, reusable input set and detect edits during a build? | A path manifest identifies bytes but does not make them immutable. Prefer scoped capture and reuse over copying a whole repository. |
| How are registration revisions mapped to build inputs and existing source relations? | Keep explicitly declared scope authoritative. `project-source` trace can suggest affected consumers, but complete-within-index or empty trace results do not prove absent dependency. |
| What correctness, timing, and profile protocols make evidence comparable per registration? | Specify representative inputs, validation, warmups/repetitions, selected profiler capture, output limits, and protocol revision. Templates are not scientific requirements. |
| How does Scheduler expose estimated availability and confidence, and what cleanup margin is required? | Re-evaluate actual resources at launch; treat forecasts as advisory. Expected duration and known cleanup inform packing without guaranteeing completion. |
| How are artifact budgets charged across Autobench, Scheduler, and the append-only `project-evidence` API? | Implement output reservation/enforcement and supported retention/compaction with each storage owner. Never hand-edit shared authorities or hide duplicate bytes outside the project limit. |
| Where does the global reconciliation process run, and how are enrollment, credentials, and host grants configured? | Keep Autobench standalone and Scheduler as physical authority. Surface missing service/grants as incomplete readiness; do not silently provision host services. |
| Which Scheduler schemas represent high-priority lease demand, forecast packing, and cleanup receipts? | Preserve protected-grant exclusions, strict foreground ordering, and verified cleanup. This is API design for a required extension, not optional foreground behavior. |
| How are registration files versioned/reviewed while generated state stays bounded? | Keep authored recipes safe from cache eviction while retaining compact state needed to reconcile active jobs and current coverage. |

## Suggested implementation sequence

1. Implement live-worktree identity, scoped stable input capture, and registration records.
2. Establish bounded `.autobench/` accounting and supported retention across artifact owners.
3. Extend Device Scheduler with expected duration, availability-aware grouping, high-priority lease demand, strict foreground ordering, and verified preemption/cleanup evidence. Qualify profiler isolation on the host.
4. Build one standalone vertical path for a registered real mechanism: detect an input change, rebuild, validate correctness, collect timing/profile packets, persist compact evidence, and answer an evidence query.
5. Add continuous reconciliation, coalescing, duplicate suppression, error backoff, recovery, and storage eviction.
6. Add registrations for other projects/build systems and optional ctxpp support.
7. Integrate through Project Control after its initializer and registered-tool interface are ready.

This sequence is a proposal for implementation work; it does not reopen the agreed direction. Input capture, service connection, Scheduler schemas, and artifact-retention interfaces can be resolved in their implementation workstreams.

## Future acceptance checks

Implementation should eventually demonstrate, with source and runtime evidence, that:

- A relevant live-worktree change creates at most one pending refresh per registration for the latest consistent source snapshot; unrelated changes and a negative source trace do not suppress declared benchmark work.
- The rebuild is tied to recorded inputs and its output identity; failed or stale builds cannot feed completed evidence.
- Correctness, performance timing, and profiler artifacts remain distinct and queryable with their full provenance.
- A high-priority lease request preempts eligible background profiling and cannot be aged behind by a background job; protected grants remain protected and the foreground grant waits for verified cleanup.
- Expected-duration estimates influence packet grouping without overriding hard kill times or guaranteeing forecast availability; packet boundaries release resources and no workspace retains a GPU reservation between packets.
- Project, coordinator, and Scheduler storage stays within agreed configured bounds under repeated runs; proposed per-workspace limits are applied, output limits are enforced while work is active, shared artifacts are charged once, active work is protected, evictions use supported owner APIs and are inspectable, and artifact eviction alone does not regenerate satisfied evidence.
- Standalone `init` prepares state and activates configured reconciliation in one command; no registrations leaves the service idle, while missing service/grants report incomplete readiness. It writes/updates the Autobench block idempotently, preserves surrounding `AGENTS.md` content and existing state, and `--agent-instructions=skip` changes only host guidance.
- Agent retrieval distinguishes current, stale, incomplete, evicted, and unresolved results without requiring Project Control.

No implementation, Scheduler change, initialization, experiment, or runtime qualification has been performed as part of this design.

## References inspected

These are sibling-checkout references, not runtime dependencies or portable initialization guidance. Their contents may change independently; recheck decisive contracts before implementation. Device Scheduler qualification records distinguish repository behavior from live evidence and remaining gaps.

- [Device Scheduler job and interruption models](../device-scheduler/packages/device-scheduler-core/src/device_scheduler_core/models.py), [admission and lease operations](../device-scheduler/packages/device-scheduler-core/src/device_scheduler_core/application.py), and [weighted scheduling policy](../device-scheduler/packages/device-scheduler-core/src/device_scheduler_core/policy.py).
- [Device Scheduler usage](../device-scheduler/docs/using-v1.md), [systemd execution adapter](../device-scheduler/packages/device-scheduler-adapters/src/device_scheduler_adapters/systemd.py), and [capability and evidence matrix](../device-scheduler/planning/capability-matrix.md).
- [KDA Scheduler bridge](../device-scheduler/packages/device-scheduler-mcp/src/device_scheduler_mcp/kda_bridge.py) and [Project Control Scheduler adapter](../project-control/src/project_control/device_scheduler.py).
- [Project Control architecture](../project-control/docs/architecture.md), [Project Foundations public API](../project-foundations/docs/API.md), [source index](../project-foundations/packages/project-source/src/project_source/index.py), and [evidence store](../project-foundations/packages/project-evidence/src/project_evidence/store.py).
- [Project Control public trace routing](../project-control/src/project_control/as1_context.py) and [separate private impact-provider composition](../project-control/src/project_control/as1_surface.py), distinguishing the shared trace library from PC-specific implementation.
- [KDA experiment engine](../kda/docs/experiment-engine.md), [build fingerprint implementation](../kda/kda_experiments/engine.py), [real-program profiling guidance](../kda/skills/ncu-report-skill/reference/02-harness-guide.md), and [NCU collection recipes](../kda/skills/ncu-report-skill/reference/03-collection.md).
- [Todo Orchestrator standalone and hosted interfaces](../todo-orchestrator/README.md) and [Project-Lab experiment execution](../project-lab/README.md), as neighboring patterns rather than required Autobench engines.
