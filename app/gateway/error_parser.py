"""Domain-aware error parsing: extract error type, source, and context.

Instead of just normalizing raw text, parse errors into structured form:
  {
    "source": "spark" | "flink" | "k8s" | "docker" | "generic",
    "error_type": "OutOfMemoryError" | "FetchFailedException" | ...,
    "context": {"executor": "3", "stage": "12", ...},
    "normalized_message": "...",
  }

This allows the knowledge base to store and match on error *semantics*, not just
text similarity. The agent can then write back structured resolutions that apply
to all variants of an error class.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedError:
    source: str  # "spark", "flink", "k8s", "docker", "generic"
    error_type: str  # e.g., "OutOfMemoryError", "ImagePullBackOff"
    context: dict  # e.g., {"executor": "3", "stage": "12"}
    normalized_message: str  # the full normalized text


class ErrorParser:
    """Parse raw error messages into structured form."""

    def parse(self, message: str) -> ParsedError:
        """Detect source and extract error type + context."""
        msg = message.strip()

        # Try each parser in order
        for parser_fn in [
            self._parse_spark,
            self._parse_flink,
            self._parse_k8s,
            self._parse_docker,
        ]:
            result = parser_fn(msg)
            if result:
                return result

        # Fallback: generic error
        return ParsedError(
            source="generic",
            error_type="UnknownError",
            context={},
            normalized_message=msg.lower(),
        )

    def _parse_spark(self, msg: str) -> Optional[ParsedError]:
        """Parse Spark driver/executor errors."""
        if "spark" not in msg.lower() and not any(
            x in msg.lower() for x in ["executor", "driver", "stage", "task"]
        ):
            return None

        context = {}
        error_type = "SparkError"

        # Extract executor/driver
        if match := re.search(r"executor[:\s]+(\d+)", msg, re.I):
            context["executor"] = match.group(1)
        if "driver" in msg.lower():
            context["component"] = "driver"

        # Extract stage/task
        if match := re.search(r"stage[:\s]+(\d+)", msg, re.I):
            context["stage"] = match.group(1)
        if match := re.search(r"task[:\s]+(\d+)", msg, re.I):
            context["task"] = match.group(1)

        # Detect specific error types
        if "outofmemoryerror" in msg.lower():
            error_type = "OutOfMemoryError"
        elif "fetchfailedexception" in msg.lower():
            error_type = "FetchFailedException"
        elif "analysisexception" in msg.lower():
            error_type = "AnalysisException"
        elif "heartbeat" in msg.lower():
            error_type = "HeartbeatTimeout"

        return ParsedError(
            source="spark",
            error_type=error_type,
            context=context,
            normalized_message=msg.lower(),
        )

    def _parse_flink(self, msg: str) -> Optional[ParsedError]:
        """Parse Flink JobManager/TaskManager errors."""
        if "flink" not in msg.lower() and not any(
            x in msg.lower() for x in ["jobmanager", "taskmanager", "checkpoint"]
        ):
            return None

        context = {}
        error_type = "FlinkError"

        # Extract component
        if match := re.search(r"(jobmanager|taskmanager)", msg, re.I):
            context["component"] = match.group(1).lower()

        # Extract checkpoint number
        if match := re.search(r"checkpoint[:\s]+(\d+)", msg, re.I):
            context["checkpoint"] = match.group(1)

        # Detect specific error types
        if "checkpoint" in msg.lower() and "expired" in msg.lower():
            error_type = "CheckpointTimeout"
        elif "could not acquire" in msg.lower() and "slot" in msg.lower():
            error_type = "InsufficientSlots"
        elif "lost leadership" in msg.lower():
            error_type = "LeadershipLoss"
        elif "backpressure" in msg.lower():
            error_type = "Backpressure"

        return ParsedError(
            source="flink",
            error_type=error_type,
            context=context,
            normalized_message=msg.lower(),
        )

    def _parse_k8s(self, msg: str) -> Optional[ParsedError]:
        """Parse Kubernetes pod/container errors."""
        if not any(
            x in msg.lower() for x in ["pod", "container", "crashloop", "imagepull", "oomkilled"]
        ):
            return None

        context = {}
        error_type = "K8sError"

        # Extract pod name (normalize the random suffix)
        if match := re.search(r"pod\s+([a-z0-9-]+)", msg, re.I):
            pod = match.group(1)
            # Strip the random suffix: api-7d9c4f6b9-x2vkd -> api
            pod_base = re.sub(r"-[0-9a-f]+-[0-9a-z]+$", "", pod, flags=re.I)
            context["pod"] = pod_base

        # Extract namespace
        if match := re.search(r"namespace[:\s]+([a-z0-9-]+)", msg, re.I):
            context["namespace"] = match.group(1)

        # Detect specific error types
        if "crashloopbackoff" in msg.lower():
            error_type = "CrashLoopBackOff"
        elif "imagepullbackoff" in msg.lower():
            error_type = "ImagePullBackOff"
        elif "oomkilled" in msg.lower():
            error_type = "OOMKilled"
        elif "pending" in msg.lower():
            error_type = "Pending"

        return ParsedError(
            source="k8s",
            error_type=error_type,
            context=context,
            normalized_message=msg.lower(),
        )

    def _parse_docker(self, msg: str) -> Optional[ParsedError]:
        """Parse Docker daemon/container errors."""
        if "docker" not in msg.lower() and not any(
            x in msg.lower() for x in ["container", "daemon", "image"]
        ):
            return None

        context = {}
        error_type = "DockerError"

        # Extract container name
        if match := re.search(r"container\s+([a-z0-9-]+)", msg, re.I):
            context["container"] = match.group(1)

        # Detect specific error types
        if "cannot connect" in msg.lower():
            error_type = "DaemonNotRunning"
        elif "port" in msg.lower() and "allocated" in msg.lower():
            error_type = "PortConflict"
        elif "no space" in msg.lower():
            error_type = "DiskFull"
        elif "imagepull" in msg.lower():
            error_type = "ImagePullError"

        return ParsedError(
            source="docker",
            error_type=error_type,
            context=context,
            normalized_message=msg.lower(),
        )
