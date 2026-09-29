import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

from .config import (
    DISK_USAGE_CRIT_PERCENT,
    DISK_USAGE_WARN_PERCENT,
    LOG_IGNORE_PATTERNS,
    LOG_SCAN_HOURS,
    MEM_AVAILABLE_MIN_MB,
    SWAP_USED_MAX_MB,
)

logger = logging.getLogger("auto_healing.diagnostics")


def get_host_metrics() -> dict[str, Any]:
    """Inspects host disk, memory, swap, and systemd units."""
    # Disk
    total, used, free = shutil.disk_usage("/")
    disk_used_pct = round((used / total) * 100, 1)
    disk_free_gb = round(free / (1024**3), 1)

    # Memory from /proc/meminfo
    mem_info = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    mem_info[key] = int(val) // 1024  # Convert kB to MB
    except Exception as e:
        logger.error(f"Error reading /proc/meminfo: {e}")

    mem_total = mem_info.get("MemTotal", 0)
    mem_avail = mem_info.get("MemAvailable", 0)
    swap_total = mem_info.get("SwapTotal", 0)
    swap_free = mem_info.get("SwapFree", 0)
    swap_used = swap_total - swap_free

    # Failed systemd units
    failed_units = []
    try:
        res = subprocess.run(
            ["systemctl", "--failed", "--plain", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in res.stdout.strip().splitlines():
            parts = line.split()
            if parts:
                failed_units.append(parts[0])
    except Exception as e:
        logger.warning(f"Failed to check systemd units: {e}")

    issues = []
    if disk_used_pct >= DISK_USAGE_CRIT_PERCENT:
        issues.append(f"DISK_CRITICAL: Root filesystem usage is {disk_used_pct}%")
    elif disk_used_pct >= DISK_USAGE_WARN_PERCENT:
        issues.append(f"DISK_WARNING: Root filesystem usage is {disk_used_pct}%")

    if mem_avail < MEM_AVAILABLE_MIN_MB:
        issues.append(f"MEMORY_LOW: Available RAM is only {mem_avail} MB")

    if swap_used > SWAP_USED_MAX_MB:
        issues.append(f"SWAP_HIGH: Swap used is {swap_used} MB")

    if failed_units:
        issues.append(f"SYSTEMD_FAILED: Units {failed_units} have failed")

    return {
        "disk": {
            "used_pct": disk_used_pct,
            "free_gb": disk_free_gb,
            "status": (
                "CRIT"
                if disk_used_pct >= DISK_USAGE_CRIT_PERCENT
                else ("WARN" if disk_used_pct >= DISK_USAGE_WARN_PERCENT else "OK")
            ),
        },
        "memory": {
            "total_mb": mem_total,
            "available_mb": mem_avail,
            "swap_used_mb": swap_used,
            "status": ("WARN" if (mem_avail < MEM_AVAILABLE_MIN_MB or swap_used > SWAP_USED_MAX_MB) else "OK"),
        },
        "failed_systemd_units": failed_units,
        "issues": issues,
    }


def get_docker_containers() -> list[dict[str, Any]]:
    """Inspects all Docker containers on the host."""
    containers = []
    try:
        cmd = ["docker", "ps", "-a", "--format", "{{json .}}"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            logger.error(f"Docker ps failed: {res.stderr}")
            return []

        for line in res.stdout.strip().splitlines():
            if not line:
                continue
            data = json.loads(line)
            # Fetch deeper inspect
            name = data.get("Names")
            inspect_cmd = ["docker", "inspect", name]
            insp_res = subprocess.run(inspect_cmd, capture_output=True, text=True, timeout=5)
            insp_data = json.loads(insp_res.stdout)[0] if insp_res.returncode == 0 else {}

            state = insp_data.get("State", {})
            status = state.get("Status", "unknown")
            health = state.get("Health", {}).get("Status", "none")
            restart_count = insp_data.get("RestartCount", 0)

            containers.append(
                {
                    "name": name,
                    "status": status,
                    "health": health,
                    "restarting": state.get("Restarting", False),
                    "restart_count": restart_count,
                    "created": insp_data.get("Created"),
                    "image": data.get("Image"),
                }
            )
    except Exception as e:
        logger.error(f"Error inspecting docker containers: {e}")

    return containers


def is_error_line(line: str) -> bool:
    """Evaluates if a log line represents a real error/critical severity."""
    lower = line.lower()
    if "[info]" in lower or "[debug]" in lower:
        return False
    if any(k in line for k in ["[ERROR]", "[CRITICAL]", "[EXCEPTION]", "[FATAL]", "[PANIC]"]):
        return True
    if any(
        k in lower
        for k in [
            "error:",
            "critical:",
            "fatal:",
            "panic:",
            "level=error",
            "level=critical",
            "level=fatal",
        ]
    ):
        return True
    return False


def scan_container_logs(container_name: str, hours: int = LOG_SCAN_HOURS) -> dict[str, Any]:
    """Scans container logs for errors in the past X hours."""
    errors = []
    try:
        cmd = ["docker", "logs", "--since", f"{hours}h", container_name]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, errors="ignore")
        combined_logs = (res.stdout + "\n" + res.stderr).splitlines()

        for line in combined_logs:
            lower = line.lower()
            if is_error_line(line):
                # Check ignores
                if any(ign.lower() in lower for ign in LOG_IGNORE_PATTERNS):
                    continue
                errors.append(line.strip()[:200])
    except Exception as e:
        logger.warning(f"Could not read logs for {container_name}: {e}")

    return {
        "container": container_name,
        "error_count": len(errors),
        "sample_errors": errors[-5:] if errors else [],
    }


def run_full_diagnostics() -> dict[str, Any]:
    """Runs all checks and returns consolidated diagnostic report."""
    host_metrics = get_host_metrics()
    containers = get_docker_containers()

    unhealthy_containers = []
    degraded_containers = []
    log_anomalies = []

    for c in containers:
        if c["health"] == "unhealthy":
            unhealthy_containers.append(c["name"])
        elif c["status"] != "running" and not c["status"].startswith("exited (0)"):
            degraded_containers.append(c["name"])

        # Scan logs for running containers
        if c["status"] == "running":
            log_res = scan_container_logs(c["name"], hours=LOG_SCAN_HOURS)
            if log_res["error_count"] > 10:  # Threshold for noticeable log noise
                log_anomalies.append(log_res)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host": host_metrics,
        "containers_total": len(containers),
        "containers_running": len([c for c in containers if c["status"] == "running"]),
        "unhealthy_containers": unhealthy_containers,
        "degraded_containers": degraded_containers,
        "log_anomalies": log_anomalies,
    }
