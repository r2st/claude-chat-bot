"""
Resource limiter for subprocess execution — ported from auto-agent/fastcoder.

Enforces CPU, memory, disk, wall-time, and process-count limits on
subprocesses spawned by the coding agent. Prevents runaway builds or
tests from consuming the host machine.

Usage:
    from resource_limiter import ResourceLimiter, ResourceLimits

    limiter = ResourceLimiter()
    rc, stdout, stderr, usage = await limiter.execute("npm test", cwd="/app")

    if usage.limits_hit:
        print(f"Hit limits: {usage.limits_hit}")
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ─── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULT_CPU_SECONDS = 300
_DEFAULT_MEMORY_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB
_DEFAULT_DISK_BYTES = 10 * 1024 * 1024 * 1024     # 10 GB
_DEFAULT_MAX_PROCESSES = 50
_DEFAULT_WALL_TIME = 600                           # 10 min


@dataclass
class ResourceLimits:
    """Configurable resource limits for subprocess execution."""
    cpu_seconds: int = _DEFAULT_CPU_SECONDS
    memory_bytes: int = _DEFAULT_MEMORY_BYTES
    disk_bytes: int = _DEFAULT_DISK_BYTES
    max_processes: int = _DEFAULT_MAX_PROCESSES
    wall_time_seconds: int = _DEFAULT_WALL_TIME


@dataclass
class ResourceUsage:
    """Actual resource usage observed during execution."""
    cpu_time_seconds: float = 0.0
    memory_peak_bytes: int = 0
    wall_time_seconds: float = 0.0
    limits_hit: list[str] = field(default_factory=list)


# ─── Preset templates ────────────────────────────────────────────────────────

TEMPLATES: dict[str, ResourceLimits] = {
    "strict": ResourceLimits(
        cpu_seconds=60,
        memory_bytes=512 * 1024 * 1024,
        disk_bytes=1024 * 1024 * 1024,
        max_processes=30,
        wall_time_seconds=120,
    ),
    "standard": ResourceLimits(),
    "relaxed": ResourceLimits(
        cpu_seconds=600,
        memory_bytes=4 * 1024 * 1024 * 1024,
        disk_bytes=20 * 1024 * 1024 * 1024,
        max_processes=100,
        wall_time_seconds=1200,
    ),
    "test": ResourceLimits(
        cpu_seconds=30,
        memory_bytes=256 * 1024 * 1024,
        disk_bytes=500 * 1024 * 1024,
        max_processes=10,
        wall_time_seconds=60,
    ),
}


# ─── Main class ──────────────────────────────────────────────────────────────

class ResourceLimiter:
    """Enforce resource limits on subprocess execution.

    On Linux, sets OS-level limits via resource.setrlimit in the child
    process and monitors /proc/{pid}/ for violations.

    On macOS (where /proc doesn't exist), relies on wall-time timeout only.
    """

    def __init__(self, limits: Optional[ResourceLimits] = None):
        self.limits = limits or ResourceLimits()
        self._is_linux = platform.system() == "Linux"

    def _get_preexec_fn(self):
        """Return a preexec_fn that sets OS resource limits (Linux only)."""
        if not self._is_linux:
            return None

        import resource as _resource

        limits = self.limits

        def _set_limits():
            try:
                _resource.setrlimit(_resource.RLIMIT_CPU,
                                    (limits.cpu_seconds, limits.cpu_seconds))
                _resource.setrlimit(_resource.RLIMIT_AS,
                                    (limits.memory_bytes, limits.memory_bytes))
                _resource.setrlimit(_resource.RLIMIT_FSIZE,
                                    (limits.disk_bytes, limits.disk_bytes))
                _resource.setrlimit(_resource.RLIMIT_NPROC,
                                    (limits.max_processes, limits.max_processes))
            except (ValueError, OSError) as e:
                log.warning("Could not set resource limits: %s", e)

        return _set_limits

    async def _monitor_linux(self, process: asyncio.subprocess.Process,
                             limits: ResourceLimits) -> ResourceUsage:
        """Monitor a process via /proc on Linux."""
        usage = ResourceUsage()
        start = time.time()
        pid = process.pid

        if pid is None:
            return usage

        while process.returncode is None:
            try:
                # /proc/{pid}/stat → CPU time
                stat_path = f"/proc/{pid}/stat"
                if os.path.exists(stat_path):
                    with open(stat_path) as f:
                        fields = f.read().split()
                    if len(fields) > 14:
                        utime = int(fields[13])
                        stime = int(fields[14])
                        ticks = os.sysconf("SC_CLK_TCK")
                        usage.cpu_time_seconds = max(
                            usage.cpu_time_seconds, (utime + stime) / ticks
                        )

                # /proc/{pid}/status → RSS memory
                status_path = f"/proc/{pid}/status"
                if os.path.exists(status_path):
                    with open(status_path) as f:
                        for line in f:
                            if line.startswith("VmRSS:"):
                                rss_kb = int(line.split()[1])
                                usage.memory_peak_bytes = max(
                                    usage.memory_peak_bytes, rss_kb * 1024
                                )

                wall = time.time() - start
                usage.wall_time_seconds = wall

                # Check violations
                if usage.cpu_time_seconds > limits.cpu_seconds and "cpu" not in usage.limits_hit:
                    usage.limits_hit.append("cpu")
                    log.warning("CPU limit exceeded (%ss > %ss), killing pid %d",
                                usage.cpu_time_seconds, limits.cpu_seconds, pid)
                    process.kill()
                    break
                if usage.memory_peak_bytes > limits.memory_bytes and "memory" not in usage.limits_hit:
                    usage.limits_hit.append("memory")
                    log.warning("Memory limit exceeded, killing pid %d", pid)
                    process.kill()
                    break
                if wall > limits.wall_time_seconds and "wall_time" not in usage.limits_hit:
                    usage.limits_hit.append("wall_time")
                    log.warning("Wall-time limit exceeded (%ss), killing pid %d", wall, pid)
                    process.kill()
                    break

            except (FileNotFoundError, IOError, PermissionError):
                pass

            await asyncio.sleep(0.5)

        return usage

    async def execute(
        self,
        cmd: str | list[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        limits: Optional[ResourceLimits] = None,
    ) -> tuple[int, str, str, ResourceUsage]:
        """Execute a command with resource limits.

        Args:
            cmd: Command as a list of arguments (preferred) or shell string.
            cwd: Working directory.
            env: Environment variables.
            limits: Override limits for this call.

        Returns:
            (return_code, stdout, stderr, resource_usage)
        """
        limits = limits or self.limits
        start = time.time()

        try:
            if isinstance(cmd, str):
                import shlex
                cmd_args = shlex.split(cmd)
            else:
                cmd_args = list(cmd)
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or os.getcwd(),
                env=env,
                preexec_fn=self._get_preexec_fn(),
            )

            if self._is_linux:
                monitor_task = asyncio.create_task(
                    self._monitor_linux(process, limits)
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=limits.wall_time_seconds
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                    stdout, stderr = b"", b"Wall-time limit exceeded"

                try:
                    usage = await asyncio.wait_for(monitor_task, timeout=5)
                except asyncio.TimeoutError:
                    monitor_task.cancel()
                    usage = ResourceUsage()
            else:
                # macOS / non-Linux: wall-time only
                usage = ResourceUsage()
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=limits.wall_time_seconds
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                    stdout, stderr = b"", b"Wall-time limit exceeded"
                    usage.limits_hit.append("wall_time")

                usage.wall_time_seconds = time.time() - start

            return (
                process.returncode or 0,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                usage,
            )

        except Exception as e:
            log.error("execute error: %s", e)
            return 1, "", str(e), ResourceUsage()

    @staticmethod
    def from_template(name: str) -> "ResourceLimiter":
        """Create a ResourceLimiter from a preset template.

        Available: "strict", "standard", "relaxed", "test".
        """
        name = name.lower()
        if name not in TEMPLATES:
            raise ValueError(f"Unknown template: {name}. Available: {list(TEMPLATES)}")
        return ResourceLimiter(TEMPLATES[name])


def format_usage(usage: ResourceUsage) -> str:
    """Human-readable one-liner for resource usage."""
    parts = [f"⏱ {usage.wall_time_seconds:.1f}s"]
    if usage.cpu_time_seconds > 0:
        parts.append(f"CPU {usage.cpu_time_seconds:.1f}s")
    if usage.memory_peak_bytes > 0:
        mb = usage.memory_peak_bytes / (1024 * 1024)
        parts.append(f"Mem {mb:.0f}MB")
    if usage.limits_hit:
        parts.append(f"⚠️ limits hit: {', '.join(usage.limits_hit)}")
    return " · ".join(parts)
