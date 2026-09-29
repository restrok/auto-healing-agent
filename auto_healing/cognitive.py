"""Cognitive Brain with Resilient Circuit Breaker and Tier-1 Remediation Fallback.

Inspired by RRSI (Regularized Recursive Self-Improvement - arXiv:2609.24972).
Prevents circular dependencies on downstream AI infrastructure (LLM/DB/Brain)
and provides deterministic, ordered bootstrap recovery.
"""

import concurrent.futures
import enum
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger("auto_healing.cognitive")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
MODEL_NAME = os.getenv("LLM_MODEL", "deepseek-v4.1-flash")

CORE_DB_CONTAINER = os.getenv("CORE_DB_CONTAINER", "core-db-1")
CORE_LLM_CONTAINER = os.getenv("CORE_LLM_CONTAINER", "core-llm-1")
CORE_BRAIN_CONTAINER = os.getenv("CORE_BRAIN_CONTAINER", "core-brain-1")
CORE_SCHEDULER_CONTAINER = os.getenv("CORE_SCHEDULER_CONTAINER", "core-scheduler-1")

BOOTSTRAP_DEPENDENCY_ORDER: List[str] = [
    "docker.service",
    CORE_DB_CONTAINER,
    CORE_LLM_CONTAINER,
    CORE_BRAIN_CONTAINER,
    CORE_SCHEDULER_CONTAINER,
]

CORE_SERVICES: set[str] = {
    CORE_DB_CONTAINER,
    CORE_LLM_CONTAINER,
    CORE_BRAIN_CONTAINER,
    CORE_SCHEDULER_CONTAINER,
}


class CircuitState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerError(Exception):
    """Base exception for circuit breaker errors."""

    pass


class CircuitBreakerOpenError(CircuitBreakerError):
    """Raised when an operation is rejected because the circuit is OPEN."""

    pass


class CircuitBreaker:
    """Thread-safe stateful Circuit Breaker with call timeout and recovery cooldown."""

    def __init__(
        self,
        failure_threshold: int = 2,
        recovery_timeout: float = 60.0,
        call_timeout: float = 2.0,
    ) -> None:
        self.failure_threshold: int = failure_threshold
        self.recovery_timeout: float = recovery_timeout
        self.call_timeout: float = call_timeout
        self.state: CircuitState = CircuitState.CLOSED
        self.failure_count: int = 0
        self.last_failure_time: Optional[float] = None
        self._lock: threading.Lock = threading.Lock()
        self._executor: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="CircuitBreakerWorker"
        )

    def can_execute(self) -> bool:
        """Determines whether a call can proceed based on current circuit state and elapsed time."""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                now = time.time()
                if self.last_failure_time is not None and (now - self.last_failure_time) >= self.recovery_timeout:
                    logger.info(
                        f"Circuit breaker cooldown elapsed ({self.recovery_timeout}s). "
                        "State transitioning OPEN -> HALF_OPEN."
                    )
                    self.state = CircuitState.HALF_OPEN
                    return True
                return False
            if self.state == CircuitState.HALF_OPEN:
                return True
            return False

    def record_success(self) -> None:
        """Records a successful operation and resets state if in HALF_OPEN or resets failure count."""
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                logger.info("Circuit breaker trial call succeeded. State transitioning HALF_OPEN -> CLOSED.")
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.last_failure_time = None
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0

    def record_failure(self) -> None:
        """Records a failure. Trips circuit to OPEN if failure threshold is reached or if in HALF_OPEN."""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.state == CircuitState.HALF_OPEN:
                logger.warning(
                    "Circuit breaker trial call failed during HALF_OPEN. State transitioning HALF_OPEN -> OPEN."
                )
                self.state = CircuitState.OPEN
            elif self.state == CircuitState.CLOSED:
                if self.failure_count >= self.failure_threshold:
                    logger.warning(
                        f"Circuit breaker threshold reached ({self.failure_count}/{self.failure_threshold}). "
                        "State transitioning CLOSED -> OPEN."
                    )
                    self.state = CircuitState.OPEN

    def reset(self) -> None:
        """Explicitly resets circuit breaker to CLOSED state."""
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.last_failure_time = None

    def execute(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Executes func enforcing circuit state and strict call timeout."""
        if not self.can_execute():
            raise CircuitBreakerOpenError(
                f"Circuit breaker is OPEN. Calls blocked until recovery timeout ({self.recovery_timeout}s)."
            )

        try:
            if self.call_timeout and self.call_timeout > 0:
                future = self._executor.submit(func, *args, **kwargs)
                try:
                    result = future.result(timeout=self.call_timeout)
                except concurrent.futures.TimeoutError as te:
                    raise TimeoutError(f"Call timed out after {self.call_timeout}s") from te
            else:
                result = func(*args, **kwargs)

            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    def close(self) -> None:
        """Shuts down internal thread pool without blocking."""
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False, cancel_futures=True)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# Module-level default circuit breaker
default_circuit_breaker = CircuitBreaker(
    failure_threshold=2,
    recovery_timeout=60.0,
    call_timeout=2.0,
)


def get_tier1_remediation(target_name: str, issue_description: str = "") -> Dict[str, Any]:
    """Returns deterministic safe-mode remediation adhering to bootstrap dependency order:
    docker.service -> core-db-1 -> core-llm-1 -> core-brain-1 / core-scheduler-1
    """
    clean_target = (target_name or "").strip().lower()

    # 1. Docker daemon recovery
    if clean_target in ("docker", "docker.service", "dockerd") or (
        "docker" in clean_target and "core" not in clean_target
    ):
        return {
            "title": "Tier 1: Recuperación de Docker Daemon",
            "root_cause": f"Falla de servicio Docker o socket daemon inaccesible. {issue_description}".strip(),
            "recommended_fix": "Reiniciar el servicio docker.service mediante systemctl en el host.",
            "worker_task": "sudo systemctl restart docker.service",
            "target_project": "homelab",
            "tier": "tier1",
        }

    # 2. Database service recovery (root dependency)
    if clean_target == CORE_DB_CONTAINER.lower() or "neo4j" in clean_target or "core-db" in clean_target:
        return {
            "title": "Tier 1: Bootstrap Graph Database",
            "root_cause": f"Base de datos inaccesible o no saludable (dependencia raíz del sistema). {issue_description}".strip(),
            "recommended_fix": f"Reiniciar contenedor {CORE_DB_CONTAINER} y verificar disponibilidad en puerto 7474.",
            "worker_task": f"docker restart {CORE_DB_CONTAINER}",
            "target_project": "core-services",
            "tier": "tier1",
        }

    # 3. LLM / inference service recovery
    if clean_target == CORE_LLM_CONTAINER.lower() or "ollama" in clean_target or "core-llm" in clean_target:
        return {
            "title": "Tier 1: Bootstrap LLM Service",
            "root_cause": f"Servicio LLM inaccesible o saturado; falla de inferencia o embeddings locales. {issue_description}".strip(),
            "recommended_fix": f"Reiniciar contenedor {CORE_LLM_CONTAINER} tras validar servicio base Docker.",
            "worker_task": f"docker restart {CORE_LLM_CONTAINER}",
            "target_project": "core-services",
            "tier": "tier1",
        }

    # 4. Core Brain recovery
    if clean_target == CORE_BRAIN_CONTAINER.lower() or "brain" in clean_target:
        return {
            "title": "Tier 1: Bootstrap Brain Engine",
            "root_cause": f"Servicio Brain Engine degradado o no responde; requiere validar {CORE_DB_CONTAINER}. {issue_description}".strip(),
            "recommended_fix": f"Verificar salud de {CORE_DB_CONTAINER} y reiniciar {CORE_BRAIN_CONTAINER}.",
            "worker_task": f"docker restart {CORE_BRAIN_CONTAINER}",
            "target_project": "core-services",
            "tier": "tier1",
        }

    # 5. Core Scheduler recovery
    if clean_target == CORE_SCHEDULER_CONTAINER.lower() or "scheduler" in clean_target:
        return {
            "title": "Tier 1: Bootstrap Scheduler",
            "root_cause": f"Scheduler degradado; depende de {CORE_DB_CONTAINER} y {CORE_BRAIN_CONTAINER} para tareas periódicas. {issue_description}".strip(),
            "recommended_fix": f"Reiniciar contenedor {CORE_SCHEDULER_CONTAINER} tras validar base de datos y Brain.",
            "worker_task": f"docker restart {CORE_SCHEDULER_CONTAINER}",
            "target_project": "core-services",
            "tier": "tier1",
        }

    # 6. Safe fallback for other containers
    target_orig = target_name or "contenedor"
    if clean_target.startswith("app"):
        project = "app-platform"
    elif clean_target.startswith("gateway") or "orchestrator" in clean_target:
        project = "orchestrator"
    elif clean_target.startswith("core"):
        project = "core-services"
    else:
        project = "homelab"

    if "inspect" in issue_description.lower() or "status" in issue_description.lower():
        task = f"docker inspect {target_orig}"
        fix = f"Inspeccionar configuración y estado de red del contenedor {target_orig}."
    else:
        task = f"docker restart {target_orig}"
        fix = f"Reiniciar contenedor {target_orig} y auditar estado post-reinicio."

    return {
        "title": f"Tier 1: Remediación estándar para {target_orig}",
        "root_cause": f"Anomalía persistente detectada en {target_orig}: {issue_description or 'Contenedor en estado degradado o errores en logs.'}".strip(),
        "recommended_fix": fix,
        "worker_task": task,
        "target_project": project,
        "tier": "tier1",
    }


def analyze_log_anomaly(
    container_name: str,
    log_snippet: Union[str, List[str]],
    discarded_hypotheses: Optional[List[Any]] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> Dict[str, Any]:
    """Uses LLM with strict circuit breaker and negative constraints to diagnose errors.

    Falls back deterministically to get_tier1_remediation on:
    - Anti-circular dependency trigger (container is LLM/Core service).
    - Circuit breaker OPEN (fast-fail).
    - HTTP timeout / connection error / non-zero code.
    - LLM output proposing a discarded hypothesis (post-filter veto).
    """
    cb = circuit_breaker or default_circuit_breaker

    # 1. Anti-circular dependency guard
    if container_name in CORE_SERVICES:
        logger.warning(
            f"Anti-circular dependency triggered for {container_name}: bypassing LLM and invoking Tier 1 remediation."
        )
        return get_tier1_remediation(container_name, f"Circular dependency safeguard for {container_name}")

    # 2. Fast-fail if circuit breaker is already OPEN
    if not cb.can_execute():
        logger.warning(f"Circuit breaker is OPEN for cognitive analysis of {container_name}. Fast-failing to Tier 1.")
        return get_tier1_remediation(
            container_name,
            f"Circuit breaker is OPEN (fast-fail, recovery timeout {cb.recovery_timeout}s)",
        )

    # 3. Format input logs
    if isinstance(log_snippet, list):
        formatted_logs = "\n".join(str(line) for line in log_snippet[:10])
        num_lines = len(log_snippet)
    else:
        raw_lines = [line for line in str(log_snippet).splitlines() if line.strip()]
        formatted_logs = "\n".join(raw_lines[:10]) if raw_lines else str(log_snippet)
        num_lines = len(raw_lines) if raw_lines else 1

    logger.info(f"🧠 Asking LLM ({MODEL_NAME}) to diagnose {container_name} ({num_lines} sample errors)...")

    # 4. Format negative constraints from discarded hypotheses
    negative_constraints = ""
    discarded_text_list: List[str] = []
    if discarded_hypotheses:
        for h in discarded_hypotheses:
            if isinstance(h, dict):
                val = h.get("worker_task") or h.get("action") or h.get("recommended_fix") or h.get("title")
                if val:
                    discarded_text_list.append(str(val))
            elif isinstance(h, str):
                discarded_text_list.append(h)

        if discarded_text_list:
            negative_constraints = (
                "\nNEGATIVE CONSTRAINTS (DO NOT PROPOSE THE FOLLOWING DISCARDED HYPOTHESES):\n"
                "The following remediations/hypotheses have already failed or were rejected by the operator:\n"
                + "\n".join(f"- {d}" for d in discarded_text_list)
                + "\nYou MUST propose a DIFFERENT, alternative root cause and remediation.\n"
            )

    prompt = f"""You are a Senior Infrastructure and DevOps AI Engineer auditing a Linux homelab server.
Container '{container_name}' has produced the following error logs in the last 24 hours:

```
{formatted_logs}
```
{negative_constraints}
Task:
1. Determine the root cause (concise explanation).
2. Recommend the exact fix (configuration change, service restart, or code fix).
3. Draft the exact prompt/task for the Antigravity Worker agent to execute on the host to fix it.
4. Specify target_project (one of: 'homelab', 'app-platform', 'orchestrator', 'core-services', or 'host').

Respond strictly in valid JSON format with the following keys:
{{
  "title": "Short title for approval plan",
  "root_cause": "1-2 sentence root cause explanation in Spanish",
  "recommended_fix": "Clear fix description in Spanish",
  "worker_task": "Exact technical prompt in Spanish for the Antigravity Worker to execute",
  "target_project": "project directory name"
}}
"""

    def _do_http_call() -> Dict[str, Any]:
        url = f"{OLLAMA_BASE_URL}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OLLAMA_API_KEY}",
        }
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert SRE and cloud platform architect. Output ONLY valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=cb.call_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()

            # Clean markdown code blocks if present
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            parsed = json.loads(content.strip())
            return parsed

    try:
        parsed = cb.execute(_do_http_call)
    except Exception as exc:
        logger.error(f"Cognitive analysis failed through circuit breaker for {container_name}: {exc}")
        return get_tier1_remediation(container_name, f"Cognitive service unavailable or failed: {exc}")

    required_keys = {
        "title",
        "root_cause",
        "recommended_fix",
        "worker_task",
        "target_project",
    }
    if not isinstance(parsed, dict) or not required_keys.issubset(parsed.keys()):
        logger.warning(f"Cognitive analysis returned incomplete JSON schema for {container_name}: {parsed}")
        return get_tier1_remediation(container_name, "Incomplete diagnosis format from LLM")

    # Post-filter validation against discarded hypotheses
    if discarded_text_list:
        proposed_task = (parsed.get("worker_task") or "").lower().strip()
        proposed_fix = (parsed.get("recommended_fix") or "").lower().strip()
        for d in discarded_text_list:
            d_lower = d.lower().strip()
            if d_lower and (d_lower in proposed_task or d_lower in proposed_fix or proposed_task in d_lower):
                logger.warning(
                    f"LLM proposed a discarded hypothesis ('{d}') for {container_name}. Vetoing and falling back to Tier 1."
                )
                return get_tier1_remediation(
                    container_name,
                    f"Vetoed discarded hypothesis: {d}",
                )

    logger.info(f"✅ Diagnosis generated for {container_name}: {parsed.get('title')}")
    return parsed
