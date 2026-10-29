# Autobench

Autobench maintains source-aware benchmark and profiling evidence using spare
capacity admitted by Device Scheduler. It is a standalone engine with a CLI,
project-local registries and bounded evidence storage. Project Control hosting
is planned separately.

Implementation is in progress. The [design](DESIGN.md) records product boundaries;
the [implementation contract](IMPLEMENTATION.md) records the accepted first delivery.
Do not infer working runtime capabilities from design text alone.

Each participating workspace uses `.autobench/` for registrations, build recipes,
generated state and selected artifacts. Live worktree contents are authoritative.
Registered commands run as finite Scheduler jobs; interactive priority demand
preempts flexible background work after verified cleanup.

## License

Apache License 2.0. External tools and dependencies retain their own licenses.

