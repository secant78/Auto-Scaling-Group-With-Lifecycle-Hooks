# Assignment 10: Auto Scaling Group with Lifecycle Hooks

An Auto Scaling Group that automatically installs and configures a web server on every new instance — using EC2 Lifecycle Hooks, Lambda, and SSM Run Command — with CloudWatch-driven CPU-based scale-out.

## Architecture

```
User traffic (HTTP :80)
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│  Auto Scaling Group  (min 1 / max 3, us-east-1 subnets)      │
│                                                               │
│  ┌─────────────────────────────┐                             │
│  │  New EC2 Instance           │                             │
│  │  Amazon Linux 2023 / t3.micro│                            │
│  │  State: Pending:Wait ───────┼──► EventBridge              │
│  └─────────────────────────────┘        │                    │
│                                         ▼                    │
│                              Lambda: lifecycle-handler        │
│                                         │                    │
│                              SSM Run Command                  │
│                                         │                    │
│                              dnf install httpd               │
│                              write index.html                │
│                              systemctl start httpd           │
│                                         │                    │
│                              CompleteLifecycleAction         │
│                              (CONTINUE → InService)          │
│                                                               │
│  CloudWatch Alarm: CPUUtilization > 60% → SimpleScaling +1   │
└───────────────────────────────────────────────────────────────┘
```

## Lifecycle Hook Flow

1. New instance starts → enters **Pending:Wait** state
2. ASG lifecycle hook emits event to **EventBridge** (automatic, no SNS needed)
3. EventBridge rule triggers **Lambda** (`asg-lifecycle-handler-sean`)
4. Lambda waits for **SSM agent** to register on the instance (~2 min)
5. Lambda sends **SSM Run Command** (`AWS-RunShellScript`):
   - Fetches instance metadata via **IMDSv2** (ID, AZ, region, IP)
   - `dnf install -y httpd`
   - Writes `/var/www/html/index.html` with live metadata
   - `systemctl start httpd && systemctl enable httpd`
6. Lambda calls `CompleteLifecycleAction` with **CONTINUE** → instance enters **InService**
7. If any step fails → Lambda sends **ABANDON** → ASG terminates the instance

## Resources Created

| Resource | Name |
|---|---|
| Auto Scaling Group | `web-asg-sean` |
| Launch Template | `web-lt-sean` |
| Lifecycle Hook | `web-launch-hook-sean` |
| Lambda Function | `asg-lifecycle-handler-sean` |
| EventBridge Rule | `asg-lifecycle-rule-sean` |
| EC2 IAM Role | `asg-ec2-role-sean` |
| Lambda IAM Role | `asg-lambda-role-sean` |
| Security Group | `web-asg-sg-sean` |
| CloudWatch Alarm | `web-asg-cpu-high-sean` |

## Authentication: OIDC (No Long-Lived Secrets)

GitHub Actions authenticates to AWS via **OpenID Connect** — no IAM access keys stored as secrets.

## CI/CD Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `1 - Deploy Infrastructure` | Push to `main` / manual | Creates all AWS resources |
| `2 - Test Scaling` | Manual | Runs CPU stress, monitors scale-out, prints web URLs |
| `3 - Teardown` | Manual (confirm='yes') | Destroys all resources |

---

## Setup Guide

### Prerequisites
- AWS CLI configured with admin credentials (local machine only, for one-time OIDC setup)
- Python 3.10+
- `pip install boto3`

### Step 1: One-time OIDC setup (run locally)

```bash
python setup_oidc_role.py
```

Copy the printed role ARN and add it as a GitHub repository secret:
- **Secret name:** `AWS_ROLE_ARN`
- GitHub → Settings → Secrets and variables → Actions → New repository secret

### Step 2: Deploy

Push to `main` or manually trigger **Workflow 1**.

Watch the deployment logs to confirm all 10 resources are created.

### Step 3: Test scaling

Trigger **Workflow 2** (Test Scaling). It will:
1. Show current ASG instances
2. Run `stress --cpu 4` on the first instance for 300 s via SSM
3. Monitor the ASG until a new instance appears (CPU alarm fires at 60%)
4. Print the public IP of every `InService` instance

Open each printed URL in a browser — you'll see a unique page per instance showing its metadata.

### Step 4: Manual stress test (optional)

SSH is available if you need it, but SSM is simpler:

```bash
# Get instance IDs
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names web-asg-sean \
  --query 'AutoScalingGroups[0].Instances[*].InstanceId' \
  --output text

# Run stress via SSM (no SSH key needed)
aws ssm send-command \
  --instance-ids i-XXXXXXXXXXXXXXXXX \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["dnf install -y stress", "stress --cpu 4 --timeout 300 &"]' \
  --region us-east-1
```

### Step 5: Verify web server on new instance

```bash
# Get public IP of any instance
aws ec2 describe-instances \
  --instance-ids i-XXXXXXXXXXXXXXXXX \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text

# Check the web page
curl http://<PUBLIC_IP>
```

### Step 6: Monitor Lambda logs

```bash
aws logs tail /aws/lambda/asg-lifecycle-handler-sean --follow
```

### Step 7: Teardown

Trigger **Workflow 3** with `confirm = yes` — or run locally:

```bash
python teardown.py
```

---

## Success Criteria

| Criterion | How to verify |
|---|---|
| New instances auto-configured with working web server | Open `http://<public-ip>` — see metadata page |
| Scale-out within 5 min of CPU > 60% | Workflow 2 timer output shows < 5 min |
| All instances show unique metadata | Each IP returns a different Instance ID / AZ |
