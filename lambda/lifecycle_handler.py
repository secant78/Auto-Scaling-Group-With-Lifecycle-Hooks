"""
ASG Lifecycle Hook Handler

Triggered by EventBridge when an EC2 instance enters the Pending:Wait state.
Uses SSM Run Command to install httpd, write a metadata-rich index.html,
and start the service. Completes the lifecycle action with CONTINUE on
success or ABANDON on failure so the ASG can react appropriately.
"""

import boto3
import json
import logging
import time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")
autoscaling = boto3.client("autoscaling")

# Shell script executed on the new instance via SSM Run Command.
# Amazon Linux 2023 uses dnf; IMDSv2 token is required for metadata queries.
CONFIGURE_SCRIPT = """#!/bin/bash
set -euo pipefail

echo "=== Fetching instance metadata (IMDSv2) ==="
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id)
AZ=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/placement/availability-zone)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/placement/region)
PRIVATE_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/local-ipv4)
INSTANCE_TYPE=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-type)
LAUNCH_TIME=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

echo "Instance: $INSTANCE_ID  AZ: $AZ  IP: $PRIVATE_IP"

echo "=== Installing httpd ==="
dnf install -y httpd

echo "=== Writing index.html ==="
cat > /var/www/html/index.html << HTMLEOF
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>ASG Instance – $INSTANCE_ID</title>
  <style>
    body { font-family: Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 40px; }
    .card { background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.15);
            max-width: 620px; margin: auto; padding: 32px; }
    h1 { color: #232f3e; margin-top: 0; }
    table { border-collapse: collapse; width: 100%; margin-top: 20px; }
    th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid #e0e0e0; }
    th { background: #f0f0f0; color: #555; width: 40%; }
    .badge { display: inline-block; background: #ff9900; color: #fff;
             border-radius: 4px; padding: 2px 10px; font-size: .85em; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Auto Scaling Group Demo</h1>
    <span class="badge">Configured via Lifecycle Hook + SSM</span>
    <table>
      <tr><th>Instance ID</th><td>$INSTANCE_ID</td></tr>
      <tr><th>Instance Type</th><td>$INSTANCE_TYPE</td></tr>
      <tr><th>Availability Zone</th><td>$AZ</td></tr>
      <tr><th>Region</th><td>$REGION</td></tr>
      <tr><th>Private IP</th><td>$PRIVATE_IP</td></tr>
      <tr><th>Configured At</th><td>$LAUNCH_TIME</td></tr>
    </table>
  </div>
</body>
</html>
HTMLEOF

echo "=== Starting httpd ==="
systemctl enable httpd
systemctl start httpd

systemctl is-active httpd && echo "httpd is running" || (echo "httpd failed to start" && exit 1)
echo "=== Configuration complete ==="
"""


def wait_for_ssm_ready(instance_id: str, max_attempts: int = 20, delay: int = 15) -> bool:
    """Poll SSM until the agent on the instance is registered."""
    logger.info(f"Waiting for SSM agent on {instance_id} (max {max_attempts * delay}s)")
    for attempt in range(1, max_attempts + 1):
        try:
            resp = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
            )
            if resp["InstanceInformationList"]:
                logger.info(f"SSM agent ready on {instance_id} (attempt {attempt})")
                return True
        except Exception as exc:
            logger.warning(f"SSM check attempt {attempt}: {exc}")
        time.sleep(delay)
    return False


def run_ssm_command(instance_id: str) -> bool:
    """Send and wait for the configure script to finish. Returns True on success."""
    logger.info(f"Sending SSM command to {instance_id}")
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [CONFIGURE_SCRIPT]},
        TimeoutSeconds=300,
        Comment="ASG lifecycle hook: install and configure web server",
    )
    command_id = resp["Command"]["CommandId"]
    logger.info(f"Command ID: {command_id}")

    terminal_states = {"Success", "Failed", "Cancelled", "TimedOut", "DeliveryTimedOut"}
    for attempt in range(1, 25):
        time.sleep(15)
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            logger.info(f"Waiting for invocation record (attempt {attempt})")
            continue

        status = result["Status"]
        logger.info(f"Command status (attempt {attempt}): {status}")

        if status == "Success":
            logger.info("Web server configured successfully")
            return True
        if status in terminal_states:
            logger.error(
                f"Command ended with: {status}\n"
                f"STDOUT: {result.get('StandardOutputContent', '')}\n"
                f"STDERR: {result.get('StandardErrorContent', '')}"
            )
            return False

    logger.error("Timed out waiting for SSM command to complete")
    return False


def complete_lifecycle(hook_name: str, asg_name: str, token: str, result: str) -> None:
    logger.info(f"Completing lifecycle action with result={result}")
    autoscaling.complete_lifecycle_action(
        LifecycleHookName=hook_name,
        AutoScalingGroupName=asg_name,
        LifecycleActionToken=token,
        LifecycleActionResult=result,
    )


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")

    detail = event.get("detail", {})
    instance_id = detail.get("EC2InstanceId")
    hook_name = detail.get("LifecycleHookName")
    asg_name = detail.get("AutoScalingGroupName")
    token = detail.get("LifecycleActionToken")

    if not all([instance_id, hook_name, asg_name, token]):
        logger.error(f"Missing required fields in event detail: {detail}")
        return

    logger.info(f"Processing instance {instance_id} | ASG: {asg_name} | Hook: {hook_name}")

    try:
        # Wait for SSM agent to register (instance may still be booting)
        if not wait_for_ssm_ready(instance_id):
            logger.error(f"SSM agent never became available on {instance_id}")
            complete_lifecycle(hook_name, asg_name, token, "ABANDON")
            return

        # Run configuration script
        success = run_ssm_command(instance_id)
        complete_lifecycle(hook_name, asg_name, token, "CONTINUE" if success else "ABANDON")

    except Exception as exc:
        logger.error(f"Unhandled error: {exc}", exc_info=True)
        try:
            complete_lifecycle(hook_name, asg_name, token, "ABANDON")
        except Exception as inner:
            logger.error(f"Failed to send ABANDON: {inner}")
