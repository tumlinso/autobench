from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def activate_existing_user_unit() -> tuple[bool, str]:
    """Start only a preinstalled Autobench user unit; never install host files."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "systemd user services are unavailable"
    template = subprocess.run([systemctl, "--user", "cat", "autobench-coordinator.service"], capture_output=True, text=True)
    if template.returncode != 0:
        return False, "Autobench coordinator user service is not installed"
    started = subprocess.run([systemctl, "--user", "start", "autobench-coordinator.service"], capture_output=True, text=True)
    if started.returncode != 0:
        detail = started.stderr.strip() or started.stdout.strip() or "service start failed"
        return False, detail
    return True, "existing Autobench user service started"

