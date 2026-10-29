from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .errors import SchedulerUnavailable


class Scheduler(Protocol):
    async def call(self, operation: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]: ...


class McpScheduler:
    """Typed-operation seam over Device Scheduler's authenticated Python SDK."""

    def __init__(self, base_url: str, token_file: str):
        try:
            from device_scheduler_mcp.client import DeviceSchedulerClient
        except ImportError as exc:
            raise SchedulerUnavailable("install the Device Scheduler Python client to enable execution") from exc
        self.client = DeviceSchedulerClient(base_url, token_file)

    async def call(self, operation: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self.client.call(operation, arguments)


def job_object(response: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = response.get("job")
    return nested if isinstance(nested, Mapping) else response


def cleanup_verified(job: Mapping[str, Any]) -> bool:
    """Fail closed: only an explicit verified cleanup receipt releases a packet."""
    if job.get("cleanup_verified") is True:
        return True
    cleanup = job.get("cleanup")
    if isinstance(cleanup, Mapping):
        return cleanup.get("state") == "verified" or cleanup.get("verified") is True
    result = job.get("cleanup_receipt")
    return isinstance(result, Mapping) and result.get("verified") is True


def terminal(job: Mapping[str, Any]) -> bool:
    state = str(job.get("state", "")).lower()
    return state in {"succeeded", "failed", "cancelled", "canceled", "timed_out", "abandoned", "completed"}

