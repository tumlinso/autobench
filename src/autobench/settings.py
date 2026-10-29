from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def host_limits() -> dict[str, Any]:
    defaults = {"retained_cap_bytes": 4 * 1024**3, "max_kill_seconds": 600,
                "scratch_limit_bytes": 128 * 1024**2, "output_limit_bytes": 64 * 1024**2,
                "poll_seconds": 5, "settle_seconds": 2, "scratch_poll_ms": 500}
    path = config_home() / "autobench" / "host.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        for key in defaults:
            if key in raw:
                defaults[key] = int(raw[key])
    for key, env_name in (("retained_cap_bytes", "AUTOBENCH_HOST_RETAINED_CAP_BYTES"),
                          ("max_kill_seconds", "AUTOBENCH_HOST_MAX_KILL_SECONDS"),
                          ("scratch_limit_bytes", "AUTOBENCH_HOST_SCRATCH_LIMIT_BYTES"),
                          ("output_limit_bytes", "AUTOBENCH_HOST_OUTPUT_LIMIT_BYTES")):
        if os.environ.get(env_name):
            defaults[key] = int(os.environ[env_name])
    if any(not isinstance(value, int) or value <= 0 for value in defaults.values()):
        raise ValueError("all Autobench host limits must be positive integers")
    return defaults

