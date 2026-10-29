# Single-host qualification

This record describes checks performed on 2026-10-29. It is separate from the
architecture contract and does not assert a kernel speedup or biological validity.

## Isolated checks

Autobench's tests cover registration/init preservation, consistent source capture,
submission recovery, concurrent coordinator exclusion, quotas, eviction stability,
provenance comparisons, retry backoff, readiness grants, bounded failure diagnostics,
and the actual Scheduler dispatch-to-engine forecast contract. The integrated
suite passed 55 tests and three subtests.

Device Scheduler passed 208 consolidated core, adapter, policy, CLI, contract,
systemd, authentication and NVIDIA tests, plus four selected SDK tests. The SDK
checks include a transport deadline long enough for bounded GPU lease observations. The broad ASGI bridge and
GPU-qualification fixture runs timed out; the same hangs reproduced on the
unchanged Scheduler base. They are not reported as passing.

## Source refresh through the installed service

A CPU-only fixture was initialized through the installed CLI beneath Autobench's
ignored build directory. The global coordinator admitted it through Scheduler.

| Observation | Scheduler job | Result |
| --- | --- | --- |
| Initial declared input | `c59c3dff-144c-4140-ae7f-f084e92fd835` | sum 6, count 3 |
| Two quick edits, final input `4 5 6` | `3ce3561c-c9ed-4e5b-9a39-0f9df24fbb1a` | sum 15, count 3 |

The source fingerprints changed from
`d15f705580cc0bad5317ce8ec9f628065cc723efa9facd57fb38c2ae672abcd3` to
`3d5394b7b512b6c1c574dd914ecb2c923b681b81c79da1e067d8f413892322e7`.
Exactly one refreshed attempt was submitted for the final input; the next idle
check retained the same successful attempts. Both leases were released after
verified cleanup. The temporary workspace was paused through the supported CLI.

## Real Cellerator pipeline

The standing registration built captured production support/indexing/gating code
for SM70 with line information and a deterministic synthetic fixture:
2,176 edges, 256 entities, 64 cells, 16 modules, seed 20261027. The fixture
contains 571,446 unique array bytes. Source identity is
`a0b85e822c0aaeaed476ab8cf44057d81b4e8d9c1dc8a7a4f9c7667ad6e1d816`;
build identity is
`c436d855f6eb5cfcb04a761d65c71caf0bb6f0b5b7560930b098a5d78a8aa35d`;
the executable SHA-256 is
`89e3151542b7645ec7b2ecfeea9888ea95b8eddd0ee66ae7db67cc72d2970fde`.

| Packet | Scheduler job | Cleanup |
| --- | --- | --- |
| prepare-fixture | `42a31d21-e49e-4715-9461-a257a816c55d` | verified |
| host-oracle | `4b5f1617-8d81-4323-9afb-27b81f9133d0` | verified |
| cuda-oracle | `f55a09e9-0dec-4175-bedc-acfb045345b9` | verified |
| mechanism-timing | `3dfce866-5fa9-44ca-8a6d-f02ce33d2647` | verified |
| profile-nsys | `c3cd0ce0-a044-4288-97fa-a28413d4965e` | verified |
| profile-ncu | `9307a066-e28d-4b67-bb67-8885c3f8d3f2` | verified |
| analyze-profiles | `6b335869-80d9-407f-a1e5-82ea7f58b5c9` | verified |

Every packet is current under the final measurement-protocol identity. GPU
packets used V100 UUID `GPU-cf22c41f-5b58-77b1-3535-8fadd1ca6505`. The native
harness reported `exact_host_oracle_pass`. Nsys captured the complete mechanism;
NCU captured one actual `stage_worksets_kernel` launch. CPU analysis used the
NCU report API and read 1,207 available metrics plus 17 rule results. This is
profiling evidence for the synthetic fixture, not a speedup or biological claim.

Current pointers and retained reports live beneath the participating workspace's
`.autobench/evidence/cellerator-sparse-support-indexing/`. Use `autobench results`
for compact measurements, `explain` for live freshness, `compare` for compatible
baselines, and `results --details` only when full receipts are needed. The result
query returns at most 16 compact records by default. Current-role evidence and
live source freshness are separate facts. Measurement compatibility includes the
canonical command, resources, hashed environment, registered protocol and recipe
identity. Compact results are attributed to the actual successful attempt; a
failed retry cannot inherit measurements from an earlier success.

A coordinator release was changed while an admitted CPU build was running. The
same Scheduler job completed and was imported after restart; no duplicate build
was admitted for that attempt.

## Live command deadline

The CPU-only `cpu-deadline` packet asked a child to sleep for 30 seconds with
`expected_seconds=1` and `kill_seconds=3`. Autobench recorded `timed_out`, return
code 124, after 3.00568 seconds. Scheduler job
`0fb12f4a-d281-40df-86f2-7e291ba9202f` and attempt
`0bcc52bc-602f-4f53-a2aa-43c22397b776` have verified cleanup and a released lease.
The unit's recorded start/exit times were 17:07:09 and 17:07:12 UTC; its cgroup
was empty and no benchmark child remained. The workspace was paused before a
retry. This candidate used the host's 15-second stop grace; final Autobench jobs
explicitly declare five seconds, matching their availability-planning allowance.
RuntimeMaxSec starts unit termination at the command bound, with final systemd
kill allowed that stop grace; the worker also kills its owned command at its
deadline.

## Real priority preemption

A separate qualification registration ran a finite Nsys CUDA trace over the same
production mechanism and fixture. It completed a native mechanism invocation
before an interactive external GPU lease was requested at priority 70. The
background job was flexible, with abandon-on-preempt semantics.

| Event | Recorded UTC time |
| --- | --- |
| Durable priority demand requested | 18:41:59.622692 |
| Background job stopped | 18:42:14.139131 |
| Foreground lease granted after verified cleanup and idle observations | 18:42:40.814357 |

The victim was job `e7610c13-c1fd-4e21-b0fe-0f6d252693bc`, Autobench attempt
`2e303ffe-9fb2-4450-bf57-42f62be0c38d`. Scheduler recorded cancellation with
verified cleanup and released the background lease. Autobench recorded
`preempted` and did not promote the interrupted fingerprint to successful
coverage. The real systemd unit had a 75-second runtime bound and a five-second
stop grace. The qualification workspace was then paused through the public API.

Demand `autobench-qualification-dff62f56-5bc9-4c1d-9280-f44f691504fc` received
lease `1b88ee2a-9b82-4e12-968a-5b99a6c40c9c`. Its final release has a verified
reclamation receipt, fencing token 1171. No foreground GPU workload was launched:
the qualification tests admission and cleanup of the lease itself. Grant latency
was about 41 seconds on this host, including termination and repeated idle
observations. Interactive demand takes precedence, but verified resource release
is not instantaneous.

The test exposed the need to allow more than the SDK's former 30-second HTTP
deadline for external lease observations. Request/release now allow at least
60 seconds; callers can configure a longer bound up to 300 seconds. No idle
requirement or cleanup proof was relaxed.

## Live scratch budget

A separate finite Nsys packet exceeded its 128 MiB scratch allowance. The worker
observed 135,348,528 bytes against 134,217,728 allowed and recorded
`storage_over_cap`, return code 125. Job
`1e2ff1f9-8031-490a-9187-6c619810efab` ultimately has verified cleanup and a
released lease; the attempt produced no successful benchmark evidence.

This exercised two corrections: the coordinator requests cancellation from a
persisted overrun result even if files have already been unlinked, and Scheduler
stops a verified unit whose main process exited while a profiler child still
occupies its cgroup. Recovery of an owned stopped attempt requires exact
execution identity, fencing and fresh cleanup evidence. Historical uncertainty
is retained in the audit record. Monitored limits permit brief overshoot and do
not provide a filesystem quota.

## Scope-enumeration check

The Cellerator registration enumerated 92 intended files totaling 922,186 bytes.
An independent full-walk root-relative oracle returned the same path set and
content manifest (`f745e0556769e9a75d32ea91df83939e0eedd06d32ec9b52e74acf0ce0e2ae15`).
A single pruned enumeration took 10.7 ms. The legacy full walk took 10.15 s and
also selected 13 unintended historical source copies through suffix matching.
These are single-run measurements, not a service throughput guarantee. The new
root-relative contract intentionally invalidates evidence from the old scope.

## Live integration corrections

The first admitted builds exposed an existing Scheduler executable-path problem:
resolving `venv/bin/python` to `/usr/bin/python3.12` loses virtual environment
imports. Scheduler now validates the resolved target while preserving the
invocation path. Autobench's worker had the corresponding child-PATH issue and
now preserves the virtual environment's lexical bin directory. Real venv import
regressions cover both boundaries.

Failed packets retain bounded diagnostic tails before scratch deletion. Repeated
failures use fingerprint-specific backoff; a changed registration can supersede
a failed measurement without discarding historical authority.

## Deployment and preservation

The host uses separate immutable Python 3.12 releases, pinned dependency
constraints and wheel digests. `~/.local/bin/autobench` routes through
`~/.local/state/autobench/current`. The enabled user unit is
`autobench-coordinator.service`. Each live registration includes explicit compiler
and profiler paths; no interactive shell PATH is assumed. The qualified engine
release is `4f4ee30`; the final Scheduler release is `c345b15` (including the
SDK timeout correction). `~/.local/bin/device-scheduler` routes through its own
`~/.local/state/device-scheduler/current` release link. Both services are active.

The temporary priority-qualification grant was revoked after its final lease was
released. A fresh request with that credential returned HTTP 401, then the
temporary token file was removed. The standing background principal remains
scoped to its workspaces with maximum priority 50.

The final owner-scoped history query reported 951,455 logical payload bytes and
550 rows against its 64 MiB / 10,000-row limits. The aggregate existing Scheduler
database is separate from this owner budget; no global history was purged.

Before the Scheduler cutover, its consistent ledger, configuration and effective
unit/drop-ins were backed up under its private state directory. The original
release and tested predecessors remain available. Rollback changes the release
entrypoint and restarts the service; it does not restore or rewind a live ledger.

The pre-existing ExpertPooling job
`581d60e0-5e8b-46ae-a3cc-171a8e9738f1` and reclaiming lease
`ea127c92-bcad-460d-9692-4ca1d3da3480` were preserved. Their unresolved state makes
global Scheduler readiness degraded independently of Autobench's available
resources. No foreign job, reservation or unknown-cleanup record was cleared. Final status
showed zero held leases, one unresolved job and one unresolved lease (the two
pre-existing records above), plus eight retained unknown historical operations.
Five of those operation records came from failed qualification observations or
cleanup attempts whose owned jobs/leases were subsequently verified and released;
the audit records were not rewritten to manufacture an entirely green status.

Project Control hosting and ctxpp integration remain deferred. Scratch enforcement
is monitored rather than a filesystem quota; arbitrary command writes outside
managed paths are outside its storage accounting contract.
