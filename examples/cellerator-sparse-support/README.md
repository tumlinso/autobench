# Cellerator sparse-support indexing example

This is the first real-program Autobench registration. It builds Cellerator's
existing sparse-support leaf libraries and CUDA benchmark from one immutable
source snapshot. It exercises the existing postings plus gated-worksets path;
it does not change the kernel or claim biological validity or a speedup.

## Activate in a Cellerator workspace

Copy `registration.toml` to
`<Cellerator workspace>/.autobench/registry/cellerator-sparse-support-indexing.toml`,
then run `autobench status` (or the host's registration validation command).
The registration invokes the recipes shipped with Autobench using the runtime
`{autobench}` placeholder. No absolute checkout path is stored in the
registration. The project source snapshot itself contains only the paths listed
in `[source].include`; it excludes the full Cellerator build, `external/`, and
historical `bench/sparse_support/evidence/` campaigns.

The build compiles these production sources and the existing benchmark:

- `src/support/{logical_support,adaptive_support,proposal_discovery}.cc`
- `src/indexing/{reverse_postings,temporal_occurrences}.cc`
- `src/gating/{condition_program,derivative_sampling}.cc`
- `src/gating/cuda_worksets.cu`
- `src/indexing/cuda_postings.cu`
- `bench/sparse_support/cuda_indexing_bench.cu`

The build declares CUDA architecture 70 and `-lineinfo`. Its metadata records
the exact source and Autobench recipe hashes, binary hash, compiler versions,
architecture, and line-info choice. It intentionally does not consume
prebuilt Cellerator libraries or configure the full root build.

## Packets and fixture

`prepare-fixture` creates a compact synthetic data bundle at the stable
project path `.autobench/data/cellerator-sparse-support-indexing`, shared across
source-fingerprint rebuilds. `[data].paths` makes that cache part of each
immutable source snapshot; all benchmark and profiler packets read the captured
copy, never the live mutable cache. Cellerator's existing `make_scaled_fixture` and
`write_raw_bundle` are called with 2,176 directed edges, 256 entities, 64 cells,
16 modules, seed `20261027`, and identity layout. The manifest and every unique
array are hashed. The source wrapper verifies that identity before and after
the native run. The data is synthetic engineering input, not biological data.
The fixture is regenerated only when absent, corrupt, or when the generator
hash changes. Regeneration removes only files named by its previous manifest.
Autobench accounts for this one project cache rather than copying it into every
packet artifact.

The registered packets are deliberately small and independent:

| Packet | Work | Expected / kill seconds |
| --- | --- | ---: |
| `host-oracle` | Existing host oracle on the frozen fixture | 5 / 30 |
| `cuda-oracle` | Actual CUDA mechanism and exact harness correctness checks | 5 / 30 |
| `mechanism-timing` | Ordinary mechanism timing with one warmup and three samples | 5 / 30 |
| `profile-nsys` | Nsys timeline of the full sparse indexing mechanism | 30 / 120 |
| `profile-ncu` | One `stage_worksets_kernel` launch, full NCU set | 30 / 120 |
| `analyze-profiles` | CPU-side NCU API extraction and Nsys stats after capture | 10 / 60 |

The build packet is 60 / 300 seconds. These are planning templates, not measured
runtime guarantees. They may be raised explicitly, subject to the configured
600-second host ceiling. Foreground interactive device demand must retain
priority over all three background GPU packets; each profile packet requests
an exclusive device and is independently preemptible.

GPU commands require an Autobench worker launched as an admitted Device
Scheduler job. The worker passes `AUTOBENCH_SCHEDULER_JOB_ID`; the recipe uses
that context only to satisfy the existing Cellerator harness's compatibility
sentinel. The Scheduler coordinator remains responsible for validating the
actual grant and cleanup receipt. A directly launched command without worker
context fails closed. Device visibility and the sentinel alone do not establish
admission.

For V100, configure `ncu` to resolve to Nsight Compute 2025.2.x. The recipe
checks the executable version and supported collection options before capture,
then uses `--set full`, source import, base clock control, the exact kernel
filter, and one launch. It writes the `.ncu-rep` and invokes the NCU Python
report API in the later CPU packet. Nsys writes a full `.nsys-rep`; its kernel
and memory summaries are computed by the later CPU packet. Captures, raw result
JSON, command lines, tool output, analysis JSON, and hashes are real outputs;
the recipe has no synthetic or fallback metrics.

The analyzed NCU report records the available metrics and rule results without
turning them into an unsupported bottleneck diagnosis. Nsys analysis is a
summary of the captured report. Source/build/data identity and the matched
Scheduler receipt must remain attached to the engine's packet record.

## Local CPU checks

From the Autobench checkout, run:

```sh
python3 tests/examples/test_cellerator_sparse_support.py -v
```

This validates the registration envelope and packet dependency paths, confirms
the source globs omit historical campaign data, parses all helper scripts, and
generates the registered CPU fixture twice to check its manifest identity and
size. It does not compile CUDA, run the GPU oracle, or collect Nsight reports;
those actions require Scheduler admission and are left for host qualification.
