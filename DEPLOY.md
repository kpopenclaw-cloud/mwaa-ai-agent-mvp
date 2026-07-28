# Deployment: mwaa-agent-chat on AWS App Runner

The chat UI (`webapp.py` + `static/chat.html`) is deployed as a container
on AWS App Runner, in the same AWS account as the MWAA environments it
queries.

**Real values (account ID, live URL, resource ARNs, instance IDs) are
deliberately not in this file** - this repo is public. They live in
`DEPLOY.local.md` in this same directory, which is gitignored and never
leaves your machine. Placeholders below (`<ACCOUNT_ID>`, `<APP_RUNNER_SERVICE_ARN>`,
etc.) match the variable names `DEPLOY.local.md` fills in.

Login: HTTP Basic Auth, username `admin`, password in Secrets Manager
(`mwaa-agent-chat/chat-password` - never committed anywhere).

## What's running

| Resource | What it is |
|---|---|
| App Runner service | Runs the container, provisions the public HTTPS URL |
| ECR repository `mwaa-agent-chat` | Private image registry the service pulls from |
| Instance role `mwaa-agent-chat-instance-role` | What the running app is allowed to call in AWS |
| ECR access role `mwaa-agent-chat-ecr-access` | Build-time only - lets App Runner pull the image |
| Secret `mwaa-agent-chat/anthropic-api-key` | Anthropic API key, injected as an env var at container start |
| Secret `mwaa-agent-chat/chat-password` | Basic Auth password, same mechanism |

Instance configuration: 0.25 vCPU / 0.5 GB, `AutoDeploymentsEnabled: false`
(pushing a new image does **not** auto-redeploy - see below).

The instance role has no static AWS keys. It's scoped to exactly what the
app needs: `airflow:InvokeRestApi`/`GetEnvironment`/`ListEnvironments`,
`logs:DescribeLogStreams`/`GetLogEvents`, `ssm:SendCommand` (only against
the `AWS-RunShellScript` document and the specific proxy instance),
`ssm:GetCommandInvocation`, and `secretsmanager:GetSecretValue` on just
its own two secrets.

## Cost

Roughly **$10-20/month** while the service is running, mostly App Runner
compute - it does not scale to zero, this is the always-on cost. ECR
storage and the two Secrets Manager entries add a couple dollars at most.
Nothing here is pay-per-request; it accrues whether or not anyone's
asking it questions.

## Redeploying after a code change

```bash
cd mwaa-ai-agent-mvp
docker buildx build --platform linux/amd64 \
  -t <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/mwaa-agent-chat:latest --load .

aws ecr get-login-password --region <REGION> --profile <PROFILE> \
  | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com

docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/mwaa-agent-chat:latest

aws apprunner start-deployment \
  --service-arn <APP_RUNNER_SERVICE_ARN> \
  --region <REGION> --profile <PROFILE>
```

`--platform linux/amd64` matters if you're building on Apple Silicon - App
Runner failed to start from a plain `docker build` image on this account
until the platform was pinned explicitly (see "Known issues" below).

## Changing environment variables or secrets

Plain env vars (`MWAA_ENV_NAME`, `AWS_REGION`, `MWAA_SSM_PROXY_INSTANCE_ID`,
`CHAT_USERNAME`) and secret references live in the service's
`ImageConfiguration`. Update via:

```bash
aws apprunner update-service --service-arn <APP_RUNNER_SERVICE_ARN> \
  --source-configuration file://new-config.json \
  --region <REGION> --profile <PROFILE>
```

To rotate a secret's *value* without touching the service config:

```bash
aws secretsmanager put-secret-value \
  --secret-id mwaa-agent-chat/chat-password \
  --secret-string "NEW_PASSWORD" \
  --region <REGION> --profile <PROFILE>
```

New secret values are picked up on the next deployment, not live - run
`start-deployment` after rotating.

## Known issues hit during setup

- **First deployment failed (`CREATE_FAILED`).** Root cause: the IAM roles
  were created, then the App Runner service was created ~10 seconds later
  - not enough time for IAM's eventual consistency to propagate before
  App Runner tried to assume the instance role. Fixed by deleting the
  failed service and recreating it a few minutes later. If you see
  `CREATE_FAILED` on a fresh deploy with brand-new roles, wait a couple
  minutes and retry before assuming the permissions are wrong.
- **Architecture mismatch.** A plain `docker build` on Apple Silicon
  produces an `arm64` image; App Runner expects `x86_64`. Always build
  with `--platform linux/amd64` (see the redeploy command above) or the
  push will succeed but the service will fail to start.
- **Empty-string env vars break boto3.** Some deploy UIs can't fully
  "unset" a variable, only blank it - `AWS_PROFILE=""` makes boto3 look
  for a profile literally named `""` and crash. `webapp.py`/`main.py` treat
  `os.getenv(...) or None` for the optional AWS-related vars specifically
  to guard against this.

## Tearing down

Nothing here has been torn down - this section is for when you want to.
Real ARNs/names for these commands are in `DEPLOY.local.md`.

```bash
PROFILE=<PROFILE>
REGION=<REGION>

aws apprunner delete-service \
  --service-arn <APP_RUNNER_SERVICE_ARN> \
  --region "$REGION" --profile "$PROFILE"

aws ecr delete-repository --repository-name mwaa-agent-chat --force \
  --region "$REGION" --profile "$PROFILE"

aws iam delete-role-policy --role-name mwaa-agent-chat-instance-role \
  --policy-name mwaa-agent-chat-permissions --profile "$PROFILE"
aws iam delete-role --role-name mwaa-agent-chat-instance-role --profile "$PROFILE"

aws iam detach-role-policy --role-name mwaa-agent-chat-ecr-access \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess \
  --profile "$PROFILE"
aws iam delete-role --role-name mwaa-agent-chat-ecr-access --profile "$PROFILE"

aws secretsmanager delete-secret --secret-id mwaa-agent-chat/anthropic-api-key \
  --force-delete-without-recovery --region "$REGION" --profile "$PROFILE"
aws secretsmanager delete-secret --secret-id mwaa-agent-chat/chat-password \
  --force-delete-without-recovery --region "$REGION" --profile "$PROFILE"
```

Delete the App Runner service first - the other resources aren't
dependencies of it, but there's no reason to delete IAM roles or secrets
while the service that uses them is still up.

## How a question becomes an answer

See the "Live example" section in the README - same flow, this is just
where the compute actually runs now: App Runner (not your laptop) calling
Claude (Anthropic API) and AWS (MWAA via the SSM proxy instance), both
reached over the internet from App Runner's managed network, no VPC
connector involved.
