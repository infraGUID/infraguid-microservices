# CS-02 Kubernetes Log Intelligence Agent (Lambda)

Event-triggered AWS Lambda that analyzes Kubernetes pod-log anomalies after
deployments and notifies the on-call engineer via SNS. Provisioned by
`terraform/modules/log-intel-lambda`.

It is a **LangGraph ReAct agent** (Bedrock Amazon Nova Pro via `ChatBedrockConverse`):
for each detected anomaly it reasons and calls read-only tools to investigate,
then emits an alert that **proposes a remediation for a human to approve**. The
agent never mutates the cluster.

## Flow

```
Pods → Fluent Bit (DaemonSet) → CloudWatch Logs (/infraguid/prod/pod-logs)
     → subscription filter (anomaly pattern) → THIS Lambda
     → LangGraph ReAct agent ──tools──▶ CloudWatch logs / K8s API / metrics / ArgoCD
     → typed IncidentReport → SNS → admin email (root cause + suggested action)
```

The Lambda runs **only** when a matching anomaly line is logged — it is not a
background process and does not touch the application's request path.

## Agent tools (all read-only)

| Tool | Source |
|---|---|
| `search_pod_logs`, `get_log_context` | CloudWatch Logs (pod-log group) |
| `k8s_get_pod`, `k8s_list_events`, `k8s_list_pods`, `k8s_describe_node` | live Kubernetes API (EKS access entry → `log-intel-readers` RBAC) |
| `get_metric` | CloudWatch Container Insights |
| `get_argocd_history` | ArgoCD REST API (only when `ENABLE_ARGOCD=true`) |

## Detected anomalies

A curated ~30-signature set across image, container-config, scheduling/capacity,
volumes, node/eviction pressure, network/CNI sandbox, probes and app-level
failures (panics, OOM, timeouts). The authoritative classifier is
`ANOMALY_PATTERNS` in `collect.py`; it is kept in sync with the CloudWatch
subscription-filter pattern (`var.anomaly_filter_pattern`).

> **Note — Kubernetes Events shipping.** Most failure *reasons*
> (`FailedScheduling`, `Evicted`, `ImagePullBackOff`, `OOMKilled`, …) are
> Kubernetes Events / pod status, not container stdout. They only reach
> `/infraguid/prod/pod-logs` if Fluent Bit (or a k8s-event-exporter) is shipping
> Events into that group. Verify that is enabled so the broadened trigger fires;
> regardless, the agent's `k8s_list_events` tool is the authoritative live source.

## Files

| File | Responsibility |
|---|---|
| `handler.py` | Decode CloudWatch Logs event, orchestrate per incident |
| `collect.py` | Parse Fluent Bit records → incidents; enrich from CloudWatch |
| `agent.py` | Build/run the LangGraph ReAct agent; `IncidentReport`; heuristic fallback |
| `tools.py` | LangChain `@tool` wrappers the agent calls |
| `k8s.py` | EKS token signing + Kubernetes API GETs |
| `argocd.py` | ArgoCD deployment correlation |
| `summarize.py` | Evidence formatting + heuristic fallback |
| `notify.py` | `sns:Publish` the formatted alert |

## Environment (set by Terraform)

`SNS_TOPIC_ARN`, `BEDROCK_MODEL_ID`, `LOG_GROUP_NAME`, `EKS_CLUSTER_NAME`,
`ARGOCD_SECRET_ARN`, `ARGOCD_SERVER_URL`, `ENABLE_ARGOCD`, `MAX_AGENT_ITERATIONS`.

## Packaging

Unlike earlier versions, the agent has **real dependencies** (`langgraph`,
`langchain-core`, `langchain-aws`, `pydantic`) — see `requirements.txt`. They are
shipped as a **Lambda layer**: Terraform builds them with Linux wheels via a
`null_resource` and publishes `aws_lambda_layer_version`; CI (`.github/workflows/
log-intel-lambda.yml`) does the same and runs `update-function-code` +
`update-function-configuration --layers` on changes. The function zip stays
source-only (`archive_file` over this directory). `boto3` is provided by the
runtime.
