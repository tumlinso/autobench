# Autobench repository guidance

## Ownership

Autobench is an independently usable benchmark engine. It owns registrations,
source/build identity, coverage, comparisons, and bounded evidence. Device
Scheduler owns physical admission, process supervision, preemption, and verified
cleanup. Project Control integration and automatic ctxpp maintenance are deferred.

Use DESIGN.md and IMPLEMENTATION.md for product decisions. Keep implementation
claims calibrated to actual tests and runtime receipts. Never treat an empty
source trace, job submission, or process exit alone as complete evidence.

## Engineering

Preserve unknown edits, workspaces, artifacts, credentials, task authorities and
histories. Do not reset, clean, hand-edit existing service databases, or delete
unrelated state. Use configured named Codex subagents for coherent engineering
objectives when useful; never Project Control local workers. Workers are not
alone and must accommodate concurrent edits. Root retains integration and final
acceptance. Only the user authorizes Astra.

Run focused meaningful checks and required cross-boundary regressions. Keep live
service changes and GPU qualification coordinated by root. For actual CUDA
profiling consult the KDA agent entrypoint and applicable CUDA execution guidance;
device visibility is not admission. All benchmark effects use Device Scheduler.

Registry commands are trusted executable configuration with argv and declared
inputs. Keep credentials and generated state out of commits. Retained data has
a hard cap; scratch is monitored and can briefly overshoot before cancellation.

