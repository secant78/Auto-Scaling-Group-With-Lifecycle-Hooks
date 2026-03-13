"""
Assignment 10: Teardown — deletes all resources created by setup_infrastructure.py.

Safe to run multiple times; missing resources are silently skipped.
"""

import time
import boto3
from botocore.exceptions import ClientError

import config

ec2_client = boto3.client("ec2", region_name=config.REGION)
iam = boto3.client("iam")
lambda_client = boto3.client("lambda", region_name=config.REGION)
autoscaling = boto3.client("autoscaling", region_name=config.REGION)
events = boto3.client("events", region_name=config.REGION)
cloudwatch = boto3.client("cloudwatch", region_name=config.REGION)


def _swallow(fn, *args, codes=("NoSuchEntity", "ResourceNotFoundException",
                               "ValidationError", "InvalidGroup.NotFound",
                               "NoSuchEntityException"), **kwargs):
    """Call fn; silently ignore ClientErrors whose code is in `codes`."""
    try:
        fn(*args, **kwargs)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if any(c in code for c in codes):
            pass
        else:
            print(f"  Warning: {e}")
    except Exception as e:
        print(f"  Warning: {e}")


def delete_cloudwatch_alarm():
    print("\n[1/9] CloudWatch alarm…")
    cloudwatch.delete_alarms(AlarmNames=[config.CPU_ALARM_NAME])
    print(f"  Deleted alarm: {config.CPU_ALARM_NAME}")


def delete_asg():
    print("\n[2/9] Auto Scaling Group (terminating instances)…")
    try:
        autoscaling.delete_auto_scaling_group(
            AutoScalingGroupName=config.ASG_NAME,
            ForceDelete=True,
        )
        print(f"  Deletion initiated for: {config.ASG_NAME}")
    except ClientError as e:
        if "AutoScalingGroup name not found" in str(e) or "ValidationError" in str(e):
            print(f"  ASG not found, skipping.")
            return

    # Wait for ASG and its instances to terminate
    print("  Waiting for ASG termination (up to 5 min)…")
    for _ in range(30):
        time.sleep(10)
        groups = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[config.ASG_NAME]
        )["AutoScalingGroups"]
        if not groups:
            print("  ASG fully removed.")
            return
        state = groups[0].get("Status", "")
        count = len(groups[0]["Instances"])
        print(f"    Status={state or 'active'}  Instances={count}")
    print("  Timed out waiting — continuing teardown anyway.")


def delete_launch_template():
    print("\n[3/9] Launch Template…")
    try:
        ec2_client.delete_launch_template(
            LaunchTemplateName=config.LAUNCH_TEMPLATE_NAME
        )
        print(f"  Deleted: {config.LAUNCH_TEMPLATE_NAME}")
    except ClientError as e:
        if "InvalidLaunchTemplateName.NotFoundException" in str(e) or "does not exist" in str(e):
            print("  Not found, skipping.")
        else:
            print(f"  Warning: {e}")


def delete_eventbridge_rule():
    print("\n[4/9] EventBridge rule…")
    try:
        targets = events.list_targets_by_rule(Rule=config.EVENTBRIDGE_RULE_NAME)["Targets"]
        if targets:
            events.remove_targets(
                Rule=config.EVENTBRIDGE_RULE_NAME,
                Ids=[t["Id"] for t in targets],
            )
        events.delete_rule(Name=config.EVENTBRIDGE_RULE_NAME)
        print(f"  Deleted: {config.EVENTBRIDGE_RULE_NAME}")
    except ClientError as e:
        if "ResourceNotFoundException" in str(e):
            print("  Not found, skipping.")
        else:
            print(f"  Warning: {e}")


def delete_lambda():
    print("\n[5/9] Lambda function…")
    try:
        lambda_client.delete_function(FunctionName=config.LAMBDA_FUNCTION_NAME)
        print(f"  Deleted: {config.LAMBDA_FUNCTION_NAME}")
    except ClientError as e:
        if "ResourceNotFoundException" in str(e):
            print("  Not found, skipping.")
        else:
            print(f"  Warning: {e}")


def delete_lambda_role():
    print("\n[6/9] Lambda IAM role…")
    role = config.LAMBDA_ROLE_NAME
    try:
        policies = iam.list_role_policies(RoleName=role)["PolicyNames"]
        for p in policies:
            iam.delete_role_policy(RoleName=role, PolicyName=p)
        attached = iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]
        for p in attached:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role)
        print(f"  Deleted: {role}")
    except ClientError as e:
        if "NoSuchEntity" in str(e):
            print("  Not found, skipping.")
        else:
            print(f"  Warning: {e}")


def delete_ec2_role():
    print("\n[7/9] EC2 IAM role + instance profile…")
    profile = config.EC2_INSTANCE_PROFILE_NAME
    role = config.EC2_ROLE_NAME

    # Remove role from instance profile
    try:
        iam.remove_role_from_instance_profile(
            InstanceProfileName=profile, RoleName=role
        )
    except ClientError:
        pass

    # Delete instance profile
    try:
        iam.delete_instance_profile(InstanceProfileName=profile)
        print(f"  Deleted instance profile: {profile}")
    except ClientError as e:
        if "NoSuchEntity" not in str(e):
            print(f"  Warning (instance profile): {e}")

    # Detach managed policies and delete role
    try:
        attached = iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]
        for p in attached:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        inline = iam.list_role_policies(RoleName=role)["PolicyNames"]
        for p in inline:
            iam.delete_role_policy(RoleName=role, PolicyName=p)
        iam.delete_role(RoleName=role)
        print(f"  Deleted role: {role}")
    except ClientError as e:
        if "NoSuchEntity" not in str(e):
            print(f"  Warning (role): {e}")


def delete_security_group():
    print("\n[8/9] Security group…")
    # Brief wait in case terminating instances are still holding the SG
    print("  Waiting 15s for instance NICs to detach…")
    time.sleep(15)

    try:
        groups = ec2_client.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [config.SECURITY_GROUP_NAME]}]
        )["SecurityGroups"]
        if not groups:
            print("  Not found, skipping.")
            return
        sg_id = groups[0]["GroupId"]
        ec2_client.delete_security_group(GroupId=sg_id)
        print(f"  Deleted: {sg_id}")
    except ClientError as e:
        print(f"  Warning: {e}")


def delete_log_group():
    print("\n[9/9] CloudWatch Log Group…")
    logs = boto3.client("logs", region_name=config.REGION)
    log_group = f"/aws/lambda/{config.LAMBDA_FUNCTION_NAME}"
    try:
        logs.delete_log_group(logGroupName=log_group)
        print(f"  Deleted: {log_group}")
    except ClientError as e:
        if "ResourceNotFoundException" in str(e):
            print("  Not found, skipping.")
        else:
            print(f"  Warning: {e}")


def main():
    print("=" * 60)
    print("Assignment 10: ASG with Lifecycle Hooks — Teardown")
    print("=" * 60)

    delete_cloudwatch_alarm()
    delete_asg()
    delete_launch_template()
    delete_eventbridge_rule()
    delete_lambda()
    delete_lambda_role()
    delete_ec2_role()
    delete_security_group()
    delete_log_group()

    print("\n" + "=" * 60)
    print("TEARDOWN COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
