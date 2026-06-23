#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# CS-02 Log Intelligence Lambda — manual deploy (NOT managed by Terraform or CD).
#
# This Lambda is owned out-of-band on purpose: run this script (or use the
# console / aws CLI) whenever you want to create or update it. It is idempotent
# — safe to re-run. Terraform owns nothing here, so a CI apply can never delete
# the function again.
#
# Easiest way to run: AWS CloudShell (has aws, python3, pip, zip and your creds).
#   1) open CloudShell in us-east-1
#   2) clone this repo, then:  bash services/log-intel-lambda/deploy.sh
#
# Prereqs if running locally: awscli v2, python3.12, pip, zip, and AWS creds with
# permission to manage IAM / Lambda / Logs / EC2-SG / EKS in account 901607650789.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Static config ────────────────────────────────────────────────────────────
REGION="${AWS_REGION:-us-east-1}"
PROJECT="infraguidai"
ENVIRONMENT="prod"
PREFIX="${PROJECT}-${ENVIRONMENT}"

FUNCTION_NAME="${PREFIX}-log-intel"
ROLE_NAME="${PREFIX}-logintel-role"
LAYER_NAME="${PREFIX}-log-intel-deps"
SG_NAME="${PREFIX}-logintel-sg"
CLUSTER_NAME="${PREFIX}"                 # infraguidai-prod
LOG_GROUP="/infraguid/prod/pod-logs"
ARTIFACTS_BUCKET="${PREFIX}-lambda-artifacts-901607650789"

RUNTIME="python3.12"
HANDLER="handler.lambda_handler"
TIMEOUT=300
MEMORY=1024
BEDROCK_MODEL_ID="amazon.nova-pro-v1:0"
MAX_AGENT_ITERATIONS=6

# Coarse CloudWatch pre-filter (subset of collect.py ANOMALY_PATTERNS; <1024 chars).
ANOMALY_PATTERN='?OOMKilled ?"Out of memory" ?CrashLoopBackOff ?RunContainerError ?"Back-off restarting failed container" ?CreateContainerConfigError ?CreateContainerError ?ImagePullBackOff ?ErrImageNeverPull ?ErrImagePull ?InvalidImageName ?FailedScheduling ?"Insufficient cpu" ?"Insufficient memory" ?"exceeded quota" ?"MountVolume.SetUp failed" ?FailedMount ?FailedAttachVolume ?"no space left on device" ?Evicted ?DiskPressure ?MemoryPressure ?PIDPressure ?NodeNotReady ?FailedCreatePodSandBox ?NetworkNotReady ?"Liveness probe failed" ?"Readiness probe failed" ?"panic:"'

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
LOG_GROUP_ARN="arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:${LOG_GROUP}"

echo "==> Account ${ACCOUNT_ID}, region ${REGION}, function ${FUNCTION_NAME}"

# ── Discover networking + dependencies ───────────────────────────────────────
echo "==> Discovering VPC / subnets / cluster SG / SNS / secret / KMS ..."
read -r VPC_ID CLUSTER_SG < <(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query 'cluster.resourcesVpcConfig.[vpcId,clusterSecurityGroupId]' --output text)

SUBNET_CSV="$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=${PREFIX}-private-*" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')"

SNS_TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:${PREFIX}-alerts"
SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "${PROJECT}/${ENVIRONMENT}/app-secrets" \
  --region "$REGION" --query ARN --output text)"
KMS_KEY_ARN="$(aws kms describe-key --key-id "alias/${PREFIX}" --region "$REGION" \
  --query KeyMetadata.Arn --output text)"

echo "    VPC=$VPC_ID  clusterSG=$CLUSTER_SG  subnets=$SUBNET_CSV"
[ -n "$SUBNET_CSV" ] || { echo "ERROR: no private subnets found"; exit 1; }

# ── 1. IAM execution role ────────────────────────────────────────────────────
ROLE_CREATED=false
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "==> Creating IAM role $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENVIRONMENT}" "Key=ManagedBy,Value=manual-script" >/dev/null
  ROLE_CREATED=true
else
  echo "==> IAM role $ROLE_NAME already exists"
fi
ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"

aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole

cat >/tmp/logintel-permissions.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "Bedrock", "Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": ["arn:aws:bedrock:*::foundation-model/*"] },
    { "Sid": "PublishAlerts", "Effect": "Allow", "Action": ["sns:Publish"], "Resource": "${SNS_TOPIC_ARN}" },
    { "Sid": "ReadLogs", "Effect": "Allow", "Action": ["logs:FilterLogEvents","logs:GetLogEvents","logs:DescribeLogStreams"],
      "Resource": ["${LOG_GROUP_ARN}:*","arn:aws:logs:${REGION}:*:log-group:/aws/eks/${CLUSTER_NAME}/cluster:*"] },
    { "Sid": "ReadMetrics", "Effect": "Allow", "Action": ["cloudwatch:GetMetricData","cloudwatch:ListMetrics","cloudwatch:GetMetricStatistics"], "Resource": "*" },
    { "Sid": "DescribeCluster", "Effect": "Allow", "Action": ["eks:DescribeCluster","eks:ListNodegroups","eks:DescribeNodegroup"], "Resource": "*" },
    { "Sid": "K8sAuth", "Effect": "Allow", "Action": ["sts:GetCallerIdentity"], "Resource": "*" },
    { "Sid": "ReadSecret", "Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": "${SECRET_ARN}" },
    { "Sid": "KmsDecrypt", "Effect": "Allow", "Action": ["kms:Decrypt","kms:GenerateDataKey"], "Resource": "${KMS_KEY_ARN}" }
  ]
}
JSON
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name logintel-permissions \
  --policy-document file:///tmp/logintel-permissions.json
[ "$ROLE_CREATED" = true ] && { echo "    waiting for role to propagate ..."; sleep 12; }

# ── 2. Security group (egress only) ──────────────────────────────────────────
LAMBDA_SG="$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$SG_NAME" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [ -z "$LAMBDA_SG" ] || [ "$LAMBDA_SG" = "None" ]; then
  echo "==> Creating security group $SG_NAME"
  LAMBDA_SG="$(aws ec2 create-security-group --region "$REGION" \
    --group-name "$SG_NAME" --description "Log Intelligence Lambda - egress only" \
    --vpc-id "$VPC_ID" --query GroupId --output text)"
  aws ec2 create-tags --region "$REGION" --resources "$LAMBDA_SG" \
    --tags "Key=Name,Value=$SG_NAME" "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENVIRONMENT}"
  # default egress (allow all) is present on a new SG; nothing else to add.
else
  echo "==> Security group $SG_NAME already exists ($LAMBDA_SG)"
fi

# ── 3. Build + publish the dependencies layer ────────────────────────────────
echo "==> Building deps layer (Linux wheels) and publishing"
cd "$SRC_DIR"
rm -rf build/layer && mkdir -p build/layer/python
pip install -r requirements.txt -t build/layer/python \
  --platform manylinux2014_x86_64 --python-version 3.12 \
  --implementation cp --only-binary=:all: --upgrade --quiet
( cd build/layer && zip -qr /tmp/log-intel-deps-layer.zip python )
LAYER_KEY="log-intel-lambda/layer-manual-$(date +%Y%m%d%H%M%S).zip"
aws s3 cp /tmp/log-intel-deps-layer.zip "s3://${ARTIFACTS_BUCKET}/${LAYER_KEY}" --region "$REGION"
LAYER_ARN="$(aws lambda publish-layer-version --layer-name "$LAYER_NAME" \
  --compatible-runtimes "$RUNTIME" \
  --content "S3Bucket=${ARTIFACTS_BUCKET},S3Key=${LAYER_KEY}" \
  --region "$REGION" --query LayerVersionArn --output text)"
echo "    layer: $LAYER_ARN"

# ── 4. Package the function source (deps live in the layer) ──────────────────
echo "==> Packaging function source"
rm -f /tmp/log-intel-lambda.zip
zip -qr /tmp/log-intel-lambda.zip . \
  -x '*.pyc' '__pycache__/*' 'README.md' 'build/*' 'requirements.txt' 'deploy.sh'

ENV_VARS="Variables={SNS_TOPIC_ARN=${SNS_TOPIC_ARN},BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID},LOG_GROUP_NAME=${LOG_GROUP},EKS_CLUSTER_NAME=${CLUSTER_NAME},ARGOCD_SECRET_ARN=${SECRET_ARN},ARGOCD_SERVER_URL=,ENABLE_ARGOCD=false,MAX_AGENT_ITERATIONS=${MAX_AGENT_ITERATIONS}}"
VPC_CONFIG="SubnetIds=${SUBNET_CSV},SecurityGroupIds=${LAMBDA_SG}"

# ── 5. Create or update the function ─────────────────────────────────────────
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "==> Updating existing function code + config"
  aws lambda update-function-code --function-name "$FUNCTION_NAME" --region "$REGION" \
    --zip-file fileb:///tmp/log-intel-lambda.zip >/dev/null
  aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FUNCTION_NAME" --region "$REGION" \
    --role "$ROLE_ARN" --runtime "$RUNTIME" --handler "$HANDLER" \
    --timeout "$TIMEOUT" --memory-size "$MEMORY" --layers "$LAYER_ARN" \
    --vpc-config "$VPC_CONFIG" --environment "$ENV_VARS" >/dev/null
  aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$REGION"
else
  echo "==> Creating function"
  aws lambda create-function --function-name "$FUNCTION_NAME" --region "$REGION" \
    --runtime "$RUNTIME" --role "$ROLE_ARN" --handler "$HANDLER" \
    --zip-file fileb:///tmp/log-intel-lambda.zip \
    --timeout "$TIMEOUT" --memory-size "$MEMORY" --layers "$LAYER_ARN" \
    --vpc-config "$VPC_CONFIG" --environment "$ENV_VARS" \
    --tags "Project=${PROJECT},Environment=${ENVIRONMENT},ManagedBy=manual-script" >/dev/null
  aws lambda wait function-active --function-name "$FUNCTION_NAME" --region "$REGION"
fi
FUNCTION_ARN="$(aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" \
  --query Configuration.FunctionArn --output text)"
echo "    function: $FUNCTION_ARN"

# ── 6. Allow CloudWatch Logs to invoke the function ──────────────────────────
echo "==> Granting CloudWatch Logs invoke permission"
aws lambda remove-permission --function-name "$FUNCTION_NAME" --region "$REGION" \
  --statement-id AllowCloudWatchLogsInvoke 2>/dev/null || true
aws lambda add-permission --function-name "$FUNCTION_NAME" --region "$REGION" \
  --statement-id AllowCloudWatchLogsInvoke --action lambda:InvokeFunction \
  --principal "logs.${REGION}.amazonaws.com" --source-arn "${LOG_GROUP_ARN}:*" >/dev/null

# ── 7. Open EKS API (443) from the Lambda SG so its k8s tools work ────────────
echo "==> Ensuring EKS API ingress from Lambda SG"
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$CLUSTER_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,UserIdGroupPairs=[{GroupId=${LAMBDA_SG},Description=HTTPS from log-intel Lambda}]" \
  2>/dev/null || echo "    (ingress already present)"

# ── 8. EKS access entry (role -> read-only group; RBAC binding ships via ArgoCD)
echo "==> Ensuring EKS access entry"
aws eks create-access-entry --region "$REGION" --cluster-name "$CLUSTER_NAME" \
  --principal-arn "$ROLE_ARN" --kubernetes-groups log-intel-readers --type STANDARD \
  2>/dev/null || echo "    (access entry already present)"

# ── 9. Subscription filter: pod-logs -> Lambda on anomaly lines ──────────────
echo "==> Wiring log subscription filter on $LOG_GROUP"
aws logs put-subscription-filter --region "$REGION" \
  --log-group-name "$LOG_GROUP" --filter-name "${PREFIX}-anomaly-filter" \
  --filter-pattern "$ANOMALY_PATTERN" --destination-arn "$FUNCTION_ARN"

echo ""
echo "✅ Done. $FUNCTION_NAME is deployed and wired to $LOG_GROUP."
echo "   Update later with:  bash $0   (or edit the function directly in the console)."
