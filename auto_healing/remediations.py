import logging
import subprocess
from typing import Any

logger = logging.getLogger("auto_healing.remediations")


def restart_container(container_name: str) -> dict[str, Any]:
    """Restarts a specified Docker container safely."""
    logger.info(f"🔄 Executing remediation: restart_container on {container_name}")
    try:
        res = subprocess.run(
            ["docker", "restart", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode == 0:
            return {
                "action": "restart_container",
                "target": container_name,
                "status": "APPLIED",
                "message": f"Container {container_name} restarted successfully",
            }
        else:
            return {
                "action": "restart_container",
                "target": container_name,
                "status": "FAILED",
                "message": res.stderr.strip(),
            }
    except Exception as e:
        logger.error(f"Failed to restart {container_name}: {e}")
        return {
            "action": "restart_container",
            "target": container_name,
            "status": "ERROR",
            "message": str(e),
        }


def prune_docker_dangling_images() -> dict[str, Any]:
    """Prunes dangling images and unused build caches safely."""
    logger.info("🧹 Executing remediation: prune_docker_dangling_images")
    try:
        res = subprocess.run(
            ["docker", "image", "prune", "-f"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "action": "prune_docker_dangling_images",
            "target": "docker_images",
            "status": "APPLIED" if res.returncode == 0 else "FAILED",
            "message": res.stdout.strip() or res.stderr.strip(),
        }
    except Exception as e:
        logger.error(f"Failed to prune images: {e}")
        return {
            "action": "prune_docker_dangling_images",
            "target": "docker_images",
            "status": "ERROR",
            "message": str(e),
        }


def restart_systemd_unit(unit_name: str) -> dict[str, Any]:
    """Attempts to restart a failed systemd unit."""
    logger.info(f"🔄 Executing remediation: restart_systemd_unit on {unit_name}")
    try:
        subprocess.run(
            ["sudo", "systemctl", "reset-failed", unit_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        res_restart = subprocess.run(
            ["sudo", "systemctl", "restart", unit_name],
            capture_output=True,
            text=True,
            timeout=20,
        )
        success = res_restart.returncode == 0
        return {
            "action": "restart_systemd_unit",
            "target": unit_name,
            "status": "APPLIED" if success else "FAILED",
            "message": res_restart.stdout.strip() or res_restart.stderr.strip(),
        }
    except Exception as e:
        logger.error(f"Failed to restart systemd unit {unit_name}: {e}")
        return {
            "action": "restart_systemd_unit",
            "target": unit_name,
            "status": "ERROR",
            "message": str(e),
        }
