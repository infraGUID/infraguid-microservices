"""Evidence formatting and heuristic fallback for the Log Intelligence Agent.

The LangGraph ReAct agent (`agent.py`) is the primary analysis path. This module
provides two pieces it depends on:
  * `build_evidence`    — flatten an incident's captured log lines into a prompt block.
  * `heuristic_summary` — a deterministic, error-type-aware report used when the agent
                          is unavailable, so an alert is always produced.
"""
from __future__ import annotations

def build_evidence(incident: dict) -> str:
    """Flatten captured + context log lines into a de-duped, capped evidence block."""
    lines = incident.get("samples", []) + incident.get("context_lines", [])
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(f"- {line}")
        if len(out) >= 25:
            break
    return "\n".join(out) or "(no log lines captured)"

def heuristic_summary(incident: dict) -> dict:
    """Deterministic, error-type-aware report used when the agent cannot run."""
    error_type = incident["error_type"]
    pod = incident["pod"]
    ns = incident["namespace"]
    ctr = incident["container"]
    severity = incident["default_severity"]

    et = error_type.lower()

    if "readiness probe" in et:
        root_cause = (
            f"The readiness probe on container '{ctr}' is repeatedly failing, causing "
            "Kubernetes to remove the pod from the Service endpoint list so it receives "
            "no traffic. Likely causes: the health-check endpoint is returning a non-2xx "
            "status, a required dependency (database, cache, or external API) is "
            "unreachable, the probe port or path is misconfigured, or the application "
            "is taking longer to initialise than the probe's initialDelaySeconds allows."
        )
        summary = (
            f"Pod {pod} (namespace {ns}) is failing its readiness probe on container "
            f"'{ctr}' and has been pulled out of service traffic rotation. The container "
            "process is alive but Kubernetes does not consider it ready. This commonly "
            "happens when the /health endpoint returns an error because a downstream "
            "dependency (DB, Redis, external API) is unavailable, or when a recent "
            "deployment introduced a misconfiguration that prevents the app from "
            "completing its startup sequence within the allowed time."
        )
        suggested_fix = (
            "1. Inspect the pod state and readiness probe failure events:\n"
            f"     kubectl describe pod -n {ns} {pod}\n\n"
            "2. Check live and previous container logs for dependency or startup errors:\n"
            f"     kubectl logs -n {ns} {pod} -c {ctr} --tail=100\n"
            f"     kubectl logs -n {ns} {pod} -c {ctr} --previous --tail=100\n\n"
            "3. Verify the probe path and port in the Deployment spec match the actual\n"
            "   health endpoint exposed by the application.\n\n"
            "4. Confirm all required dependencies (DB, Redis, external services) are\n"
            "   reachable from within the pod network.\n\n"
            "5. If a recent deployment caused this, roll back:\n"
            f"     kubectl rollout undo deployment/<name> -n {ns}"
        )
        suggested_command = f"kubectl describe pod -n {ns} {pod}"
        investigation_trail = (
            f"Heuristic diagnosis — 'Readiness probe failed' on {pod} in {ns}.\n\n"
            "Run these diagnostic commands in order:\n\n"
            f"  kubectl describe pod -n {ns} {pod}\n"
            f"    → Look for 'Readiness probe failed' events, the probe config\n"
            f"      (path, port, initialDelaySeconds, failureThreshold), and\n"
            f"      the container's last exit code.\n\n"
            f"  kubectl logs -n {ns} {pod} -c {ctr} --tail=100\n"
            f"    → Scan for connection refused, timeout, or HTTP 5xx errors\n"
            f"      that would cause the health endpoint to fail.\n\n"
            f"  kubectl logs -n {ns} {pod} -c {ctr} --previous --tail=100\n"
            f"    → Logs from the previous container instance if it was restarted.\n\n"
            f"  kubectl get events -n {ns} --sort-by='.lastTimestamp' | tail -30\n"
            f"    → Review scheduling, image-pull, volume-mount, or OOM events\n"
            f"      in the namespace that may precede the probe failure.\n\n"
            f"  kubectl rollout history deployment -n {ns}\n"
            f"    → Identify whether a recent rollout introduced the regression;\n"
            f"      if so, run: kubectl rollout undo deployment/<name> -n {ns}\n\n"
            "Common root causes: slow startup exceeding initialDelaySeconds, health-\n"
            "check path or port mismatch in the probe spec, OOMKilled container, or\n"
            "a downstream dependency (DB / cache / external API) is unavailable."
        )

    elif "liveness probe" in et:
        root_cause = (
            f"The liveness probe on container '{ctr}' is failing, causing Kubernetes "
            "to restart the container. The application process is running but appears "
            "unresponsive or deadlocked — its health endpoint is not answering within "
            "the probe timeout."
        )
        summary = (
            f"Pod {pod} (namespace {ns}) liveness probe is failing on container '{ctr}', "
            "triggering repeated container restarts. The container is alive at the OS "
            "level but Kubernetes considers it unhealthy and kills it. This typically "
            "indicates an application deadlock, resource exhaustion causing the event "
            "loop to stall, or the health endpoint becoming blocked under load."
        )
        suggested_fix = (
            "1. Check restart count and last termination reason:\n"
            f"     kubectl describe pod -n {ns} {pod}\n\n"
            "2. Review logs from the previous (terminated) container:\n"
            f"     kubectl logs -n {ns} {pod} -c {ctr} --previous --tail=200\n\n"
            "3. Monitor current resource usage (CPU / memory throttling):\n"
            f"     kubectl top pod {pod} -n {ns}\n\n"
            "4. Increase the liveness probe failureThreshold or initialDelaySeconds\n"
            "   in the Deployment spec if the application needs more warm-up time.\n\n"
            "5. If a recent deploy caused this, roll back:\n"
            f"     kubectl rollout undo deployment/<name> -n {ns}"
        )
        suggested_command = f"kubectl logs -n {ns} {pod} -c {ctr} --previous --tail=200"
        investigation_trail = (
            f"Heuristic diagnosis — 'Liveness probe failed' on {pod} in {ns}.\n\n"
            "Run these diagnostic commands in order:\n\n"
            f"  kubectl describe pod -n {ns} {pod}\n"
            f"    → Check restart count, last termination reason/exit code,\n"
            f"      and liveness probe configuration.\n\n"
            f"  kubectl logs -n {ns} {pod} -c {ctr} --previous --tail=200\n"
            f"    → Logs from the last crashed container instance.\n\n"
            f"  kubectl top pod {pod} -n {ns}\n"
            f"    → Current CPU / memory consumption; throttling causes probe timeouts.\n\n"
            f"  kubectl get events -n {ns} --sort-by='.lastTimestamp' | tail -20\n"
            f"    → Look for OOMKilled or BackOff events in the namespace.\n\n"
            "Common root causes: application deadlock, memory leak causing OOMKill,\n"
            "or the health endpoint blocking under load."
        )

    elif "oomkill" in et or "oom killed" in et or "out of memory" in et:
        root_cause = (
            f"Container '{ctr}' was OOMKilled — the Linux kernel terminated the process "
            "because it exceeded its configured memory limit. The container is restarting."
        )
        summary = (
            f"Pod {pod} (namespace {ns}) container '{ctr}' was terminated by the OOM "
            "killer after exceeding its memory limit. This may be a one-time spike or "
            "an ongoing memory leak. The container will keep restarting until the root "
            "cause (insufficient limit or a leak) is addressed."
        )
        suggested_fix = (
            "1. Confirm OOMKill and check current limits:\n"
            f"     kubectl describe pod -n {ns} {pod}\n"
            "     → Look for lastState.terminated.reason: OOMKilled\n\n"
            "2. Review logs before the kill:\n"
            f"     kubectl logs -n {ns} {pod} -c {ctr} --previous --tail=200\n\n"
            "3. Check live memory usage:\n"
            f"     kubectl top pod -n {ns}\n\n"
            "4. Increase the container memory limit in the Deployment spec and redeploy.\n\n"
            "5. Profile the application for memory leaks if usage grows over time."
        )
        suggested_command = f"kubectl describe pod -n {ns} {pod}"
        investigation_trail = (
            f"Heuristic diagnosis — OOMKilled on {pod} in {ns}.\n\n"
            f"  kubectl describe pod -n {ns} {pod}\n"
            f"    → Confirm lastState.terminated.reason = OOMKilled and review limits.\n\n"
            f"  kubectl logs -n {ns} {pod} -c {ctr} --previous --tail=200\n"
            f"    → Application state immediately before the kill.\n\n"
            f"  kubectl top pod -n {ns}\n"
            f"    → Current memory consumption across all pods in the namespace.\n\n"
            "Common root causes: memory limit set too low for the workload, memory leak\n"
            "in application code, or an unexpected traffic spike."
        )

    elif "imagepullbackoff" in et or "errimagepull" in et or "image pull" in et:
        root_cause = (
            f"Kubernetes cannot pull the container image for '{ctr}'. The image tag "
            "may not exist in the registry, the registry credentials (imagePullSecret) "
            "are missing or expired, or the registry endpoint is unreachable from the cluster."
        )
        summary = (
            f"Pod {pod} (namespace {ns}) is stuck in ImagePullBackOff for container "
            f"'{ctr}'. Kubernetes cannot fetch the container image. This blocks the pod "
            "from starting entirely. Check that the image tag exists, that a valid "
            "imagePullSecret is attached to the pod's service account, and that the "
            "container registry is accessible from the cluster network."
        )
        suggested_fix = (
            "1. Check the exact image name and pull error:\n"
            f"     kubectl describe pod -n {ns} {pod}\n"
            "     → Events section will show the exact failure message.\n\n"
            "2. Verify the image tag exists in the registry.\n\n"
            "3. Inspect imagePullSecrets on the pod and its service account:\n"
            f"     kubectl get pod {pod} -n {ns} -o yaml | grep -i imagesecret\n"
            f"     kubectl get serviceaccount -n {ns} <sa-name> -o yaml\n\n"
            "4. If credentials are expired, rotate the registry secret:\n"
            f"     kubectl delete secret <registry-secret> -n {ns}\n"
            "     kubectl create secret docker-registry <registry-secret> \\\n"
            "       --docker-server=<registry> --docker-username=<user> \\\n"
            f"       --docker-password=<token> -n {ns}"
        )
        suggested_command = f"kubectl describe pod -n {ns} {pod}"
        investigation_trail = (
            f"Heuristic diagnosis — ImagePullBackOff on {pod} in {ns}.\n\n"
            f"  kubectl describe pod -n {ns} {pod}\n"
            f"    → Events will show the exact image name and pull error\n"
            f"      (e.g. 'unauthorized', 'not found', 'connection refused').\n\n"
            f"  kubectl get events -n {ns} --sort-by='.lastTimestamp' | tail -20\n"
            f"    → 'Failed to pull image' and 'ErrImagePull' events.\n\n"
            f"  kubectl get serviceaccount -n {ns} default -o yaml\n"
            f"    → Verify imagePullSecrets are configured and named correctly.\n\n"
            "Common root causes: image tag does not exist (bad CI/CD push), ECR auth\n"
            "token expired, or imagePullSecret not attached to the service account."
        )

    elif "crashloopbackoff" in et or "crash loop" in et:
        root_cause = (
            f"Container '{ctr}' is crash-looping: it starts and immediately exits with "
            "a non-zero exit code. Kubernetes is applying exponential back-off before "
            "each restart attempt."
        )
        summary = (
            f"Pod {pod} (namespace {ns}) container '{ctr}' is in CrashLoopBackOff. "
            "The container repeatedly exits shortly after starting, indicating a fatal "
            "startup error. Common causes include a missing environment variable or "
            "mounted secret, a failed database connection on startup, an application "
            "panic, or a code regression introduced by a recent deployment."
        )
        suggested_fix = (
            "1. Read logs from the last crashed container:\n"
            f"     kubectl logs -n {ns} {pod} -c {ctr} --previous\n\n"
            "2. Check the exit code and restart reason:\n"
            f"     kubectl describe pod -n {ns} {pod}\n"
            "     → Exit code 1 = app error, 137 = OOMKill, 139 = segfault.\n\n"
            "3. Verify all required environment variables and mounted secrets:\n"
            f"     kubectl get pod -n {ns} {pod} -o yaml | grep -A 20 env\n\n"
            "4. If a recent deployment caused this, roll back immediately:\n"
            f"     kubectl rollout undo deployment/<name> -n {ns}"
        )
        suggested_command = f"kubectl logs -n {ns} {pod} -c {ctr} --previous"
        investigation_trail = (
            f"Heuristic diagnosis — CrashLoopBackOff on {pod} in {ns}.\n\n"
            f"  kubectl logs -n {ns} {pod} -c {ctr} --previous\n"
            f"    → The fatal error is almost always in the last few log lines.\n\n"
            f"  kubectl describe pod -n {ns} {pod}\n"
            f"    → Exit code: 1=app error, 137=OOMKill, 139=segfault, 143=SIGTERM.\n"
            f"      Also check restart count and back-off delay.\n\n"
            f"  kubectl get events -n {ns} --sort-by='.lastTimestamp' | tail -20\n"
            f"    → Mount failures or scheduling issues that precede the crash.\n\n"
            f"  kubectl rollout history deployment -n {ns}\n"
            f"    → If a recent rollout introduced the regression, run:\n"
            f"      kubectl rollout undo deployment/<name> -n {ns}\n\n"
            "Common root causes: missing env var / secret, failed DB migration on\n"
            "startup, incorrect CMD/ENTRYPOINT, or a recent code regression."
        )

    elif "pending" in et or "failedscheduling" in et or "insufficient" in et:
        root_cause = (
            f"Pod {pod} cannot be scheduled: the cluster has insufficient CPU or "
            "memory, or no available node satisfies the pod's affinity rules, "
            "tolerations, or node-selector constraints."
        )
        summary = (
            f"Pod {pod} (namespace {ns}) is stuck in Pending state. Kubernetes cannot "
            "find a suitable node to run it. This is typically caused by insufficient "
            "cluster capacity (all nodes are resource-saturated), node taints without "
            "matching tolerations on the pod, or unsatisfied node affinity / selector "
            "rules. Check whether the cluster autoscaler or Karpenter is responding."
        )
        suggested_fix = (
            "1. Inspect the scheduling failure message:\n"
            f"     kubectl describe pod -n {ns} {pod}\n"
            "     → Events section: 'FailedScheduling' message explains the constraint.\n\n"
            "2. Review node capacity:\n"
            "     kubectl describe nodes | grep -A 5 'Allocated resources'\n\n"
            "3. Verify cluster autoscaler or Karpenter is active and not blocked.\n\n"
            "4. Check pod disruption budgets that may prevent evictions:\n"
            f"     kubectl get pdb -n {ns}\n\n"
            "5. Confirm the pod's tolerations match any node taints:\n"
            f"     kubectl get pod {pod} -n {ns} -o yaml | grep -A 10 toleration"
        )
        suggested_command = f"kubectl describe pod -n {ns} {pod}"
        investigation_trail = (
            f"Heuristic diagnosis — scheduling failure for {pod} in {ns}.\n\n"
            f"  kubectl describe pod -n {ns} {pod}\n"
            f"    → 'FailedScheduling' events state the exact constraint\n"
            f"      (e.g. 'Insufficient memory', 'node(s) had taint', 'no nodes\n"
            f"      matched node affinity').\n\n"
            f"  kubectl get nodes\n"
            f"    → Node count and Ready/NotReady status.\n\n"
            f"  kubectl describe nodes | grep -A 5 'Allocated resources'\n"
            f"    → Identify which nodes are resource-saturated.\n\n"
            "Common root causes: node group at capacity with autoscaler not triggering,\n"
            "taint / toleration mismatch, or PodDisruptionBudget blocking evictions."
        )

    else:
        root_cause = (
            f"{error_type} detected on container '{ctr}' in pod {pod}. "
            "Manual investigation is required to determine the exact cause."
        )
        summary = (
            f"An anomaly of type '{error_type}' was detected in pod {pod} "
            f"(namespace {ns}, container '{ctr}'). Review the pod's events, logs, "
            "and recent deployment history to identify the root cause."
        )
        suggested_fix = (
            "1. Inspect the pod state and events:\n"
            f"     kubectl describe pod -n {ns} {pod}\n\n"
            "2. Check live and previous container logs:\n"
            f"     kubectl logs -n {ns} {pod} -c {ctr} --tail=100\n"
            f"     kubectl logs -n {ns} {pod} -c {ctr} --previous --tail=100\n\n"
            "3. Review namespace-wide recent events:\n"
            f"     kubectl get events -n {ns} --sort-by='.lastTimestamp' | tail -30\n\n"
            "4. Review recent rollout history:\n"
            f"     kubectl rollout history deployment -n {ns}"
        )
        suggested_command = f"kubectl describe pod -n {ns} {pod}"
        investigation_trail = (
            f"Heuristic diagnosis — '{error_type}' on {pod} in {ns}.\n\n"
            "Run these diagnostic commands in order:\n\n"
            f"  kubectl describe pod -n {ns} {pod}\n"
            f"    → Pod state, container statuses, events, and probe config.\n\n"
            f"  kubectl logs -n {ns} {pod} -c {ctr} --tail=100\n"
            f"    → Live container logs.\n\n"
            f"  kubectl logs -n {ns} {pod} -c {ctr} --previous --tail=100\n"
            f"    → Logs from the previous (terminated) container instance.\n\n"
            f"  kubectl get events -n {ns} --sort-by='.lastTimestamp' | tail -30\n"
            f"    → Namespace-wide recent events.\n\n"
            f"  kubectl rollout history deployment -n {ns}\n"
            f"    → Recent deployments that may have introduced this issue."
        )

    return {
        "severity": severity,
        "root_cause": root_cause,
        "summary": summary,
        "suggested_fix": suggested_fix,
        "suggested_command": suggested_command,
        "investigation_trail": investigation_trail,
    }
