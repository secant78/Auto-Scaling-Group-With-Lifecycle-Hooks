"""
Assignment 10: Auto Scaling Group with Lifecycle Hooks — infrastructure setup.

Creates (idempotently):
  1. Security group        — allow HTTP (80) inbound from anywhere
  2. EC2 IAM role          — AmazonSSMManagedInstanceCore so SSM can reach instances
  3. Lambda IAM role       — SSM, autoscaling, CloudWatch Logs
  4. Lambda function       — asg-lifecycle-handler (packaged from lambda/)
  5. EventBridge rule      — triggers Lambda on lifecycle-hook launch events
  6. Launch Template       — Amazon Linux 2023, t3.micro, public IP, SSM profile
  7. Auto Scaling Group    — min 1 / max 3 across all default-VPC subnets
  8. Lifecycle hook        — Pending:Wait → EventBridge → Lambda → CONTINUE/ABANDON
  9. Scale-out policy      — SimpleScaling +1 with 300 s cooldown
 10. CloudWatch alarm      — CPU > 60 % for 1 period → fire scale-out policy
"""

import io
import json
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

import config

ec2_client = boto3.client("ec2", region_name=config.REGION)
iam = boto3.client("iam")
lambda_client = boto3.client("lambda", region_name=config.REGION)
autoscaling = boto3.client("autoscaling", region_name=config.REGION)
events = boto3.client("events", region_name=config.REGION)
cloudwatch = boto3.client("cloudwatch", region_name=config.REGION)
sts = boto3.client("sts")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account_id():
    return sts.get_caller_identity()["Account"]


def _tag(name):
    return [{"Key": "Name", "Value": name}, {"Key": "Project", "Value": "ASG-Assignment10"}]


def _wait(seconds, msg=""):
    print(f"  Waiting {seconds}s{': ' + msg if msg else ''}…")
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# 1. Security Group
# ---------------------------------------------------------------------------

def create_security_group():
    print("\n[1/10] Security group…")
    vpc_id = ec2_client.describe_vpcs(
        Filters=[{"Name": "isDefault", "Values": ["true"]}]
    )["Vpcs"][0]["VpcId"]

    # Check if it already exists
    existing = ec2_client.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [config.SECURITY_GROUP_NAME]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]

    if existing:
        sg_id = existing[0]["GroupId"]
        print(f"  Already exists: {sg_id}")
        return sg_id

    sg = ec2_client.create_security_group(
        GroupName=config.SECURITY_GROUP_NAME,
        Description="ASG lifecycle hooks demo - HTTP inbound",
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": _tag(config.SECURITY_GROUP_NAME)}],
    )
    sg_id = sg["GroupId"]

    ec2_client.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 80,
                "ToPort": 80,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP"}],
            },
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH (debug)"}],
            },
        ],
    )
    print(f"  Created: {sg_id}")
    return sg_id


# ---------------------------------------------------------------------------
# 2. EC2 IAM role + instance profile (for SSM)
# ---------------------------------------------------------------------------

def create_ec2_role():
    print("\n[2/10] EC2 IAM role + instance profile…")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    # Role
    try:
        role = iam.create_role(
            RoleName=config.EC2_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="EC2 role for ASG instances - allows SSM",
        )
        print(f"  Created role: {config.EC2_ROLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            print(f"  Role already exists: {config.EC2_ROLE_NAME}")
        else:
            raise

    # Attach AmazonSSMManagedInstanceCore so SSM can reach instances
    try:
        iam.attach_role_policy(
            RoleName=config.EC2_ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        )
    except ClientError as e:
        if "already attached" not in str(e).lower():
            raise

    # Instance profile
    try:
        iam.create_instance_profile(InstanceProfileName=config.EC2_INSTANCE_PROFILE_NAME)
        print(f"  Created instance profile: {config.EC2_INSTANCE_PROFILE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            print(f"  Instance profile already exists: {config.EC2_INSTANCE_PROFILE_NAME}")
        else:
            raise

    try:
        iam.add_role_to_instance_profile(
            InstanceProfileName=config.EC2_INSTANCE_PROFILE_NAME,
            RoleName=config.EC2_ROLE_NAME,
        )
    except ClientError as e:
        if "already associated" not in str(e).lower() and "LimitExceeded" not in str(e):
            pass  # Already associated — fine

    profile_arn = iam.get_instance_profile(
        InstanceProfileName=config.EC2_INSTANCE_PROFILE_NAME
    )["InstanceProfile"]["Arn"]
    print(f"  Profile ARN: {profile_arn}")
    return profile_arn


# ---------------------------------------------------------------------------
# 3. Lambda IAM role
# ---------------------------------------------------------------------------

def create_lambda_role():
    print("\n[3/10] Lambda IAM role…")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    inline_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ssm:SendCommand",
                    "ssm:GetCommandInvocation",
                    "ssm:DescribeInstanceInformation",
                    "ssm:ListCommandInvocations",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "autoscaling:CompleteLifecycleAction",
                    "autoscaling:DescribeAutoScalingInstances",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["ec2:DescribeInstances"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "arn:aws:logs:*:*:*",
            },
        ],
    }

    try:
        iam.create_role(
            RoleName=config.LAMBDA_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Lambda execution role for ASG lifecycle hook handler",
        )
        print(f"  Created role: {config.LAMBDA_ROLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            print(f"  Role already exists: {config.LAMBDA_ROLE_NAME}")
        else:
            raise

    iam.put_role_policy(
        RoleName=config.LAMBDA_ROLE_NAME,
        PolicyName="ASGLifecycleLambdaPolicy",
        PolicyDocument=json.dumps(inline_policy),
    )

    role_arn = iam.get_role(RoleName=config.LAMBDA_ROLE_NAME)["Role"]["Arn"]
    print(f"  Role ARN: {role_arn}")
    return role_arn


# ---------------------------------------------------------------------------
# 4. Package and deploy Lambda function
# ---------------------------------------------------------------------------

def _zip_lambda():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write("lambda/lifecycle_handler.py", "lifecycle_handler.py")
    buf.seek(0)
    return buf.read()


def create_lambda_function(lambda_role_arn):
    print("\n[4/10] Lambda function…")

    code = _zip_lambda()

    # IAM role propagation can take up to ~30s; retry until Lambda accepts it.
    fn_arn = None
    for attempt in range(1, 13):  # up to ~60s
        try:
            resp = lambda_client.create_function(
                FunctionName=config.LAMBDA_FUNCTION_NAME,
                Runtime="python3.12",
                Role=lambda_role_arn,
                Handler="lifecycle_handler.lambda_handler",
                Code={"ZipFile": code},
                Timeout=config.LAMBDA_TIMEOUT,
                MemorySize=256,
                Description="ASG lifecycle hook: installs httpd via SSM on new EC2 instances",
            )
            fn_arn = resp["FunctionArn"]
            print(f"  Created: {fn_arn}")
            break
        except lambda_client.exceptions.InvalidParameterValueException as e:
            if "cannot be assumed" in str(e):
                print(f"  IAM role not ready yet (attempt {attempt}/12), waiting 5s…")
                time.sleep(5)
            else:
                raise
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceConflictException":
                # Already exists — update code + config, waiting between calls
                lambda_client.update_function_code(
                    FunctionName=config.LAMBDA_FUNCTION_NAME,
                    ZipFile=code,
                )
                # Wait for the code update to complete before updating config
                lambda_client.get_waiter("function_updated").wait(
                    FunctionName=config.LAMBDA_FUNCTION_NAME
                )
                lambda_client.update_function_configuration(
                    FunctionName=config.LAMBDA_FUNCTION_NAME,
                    Role=lambda_role_arn,
                    Timeout=config.LAMBDA_TIMEOUT,
                    MemorySize=256,
                )
                fn_arn = lambda_client.get_function_configuration(
                    FunctionName=config.LAMBDA_FUNCTION_NAME
                )["FunctionArn"]
                print(f"  Updated existing function: {fn_arn}")
                break
            else:
                raise

    if fn_arn is None:
        raise RuntimeError("Lambda creation failed: IAM role never became assumable after 60s")

    return fn_arn


# ---------------------------------------------------------------------------
# 5. EventBridge rule → Lambda
# ---------------------------------------------------------------------------

def create_eventbridge_rule(fn_arn):
    print("\n[5/10] EventBridge rule…")
    pattern = json.dumps(
        {
            "source": ["aws.autoscaling"],
            "detail-type": ["EC2 Instance-launch Lifecycle Action"],
            "detail": {
                "AutoScalingGroupName": [config.ASG_NAME],
                "LifecycleHookName": [config.LIFECYCLE_HOOK_NAME],
            },
        }
    )

    rule_resp = events.put_rule(
        Name=config.EVENTBRIDGE_RULE_NAME,
        EventPattern=pattern,
        State="ENABLED",
        Description="Trigger Lambda on ASG instance-launch lifecycle hook",
    )
    rule_arn = rule_resp["RuleArn"]
    print(f"  Rule ARN: {rule_arn}")

    # Grant EventBridge permission to invoke Lambda
    try:
        lambda_client.add_permission(
            FunctionName=config.LAMBDA_FUNCTION_NAME,
            StatementId="EventBridgeInvoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            pass  # Permission already exists

    events.put_targets(
        Rule=config.EVENTBRIDGE_RULE_NAME,
        Targets=[{"Id": "LambdaTarget", "Arn": fn_arn}],
    )
    print(f"  Lambda target added.")
    return rule_arn


# ---------------------------------------------------------------------------
# 6. Launch Template
# ---------------------------------------------------------------------------

def get_latest_al2023_ami():
    resp = ec2_client.describe_images(
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-*-x86_64"]},
            {"Name": "owner-alias", "Values": ["amazon"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
        Owners=["amazon"],
    )
    images = sorted(resp["Images"], key=lambda x: x["CreationDate"], reverse=True)
    ami_id = images[0]["ImageId"]
    print(f"  Latest AL2023 AMI: {ami_id}  ({images[0]['Name']})")
    return ami_id


def create_launch_template(sg_id, instance_profile_arn):
    print("\n[6/10] Launch Template…")

    # Check if already exists
    existing = ec2_client.describe_launch_templates(
        Filters=[{"Name": "launch-template-name", "Values": [config.LAUNCH_TEMPLATE_NAME]}]
    )["LaunchTemplates"]
    if existing:
        lt_id = existing[0]["LaunchTemplateId"]
        print(f"  Already exists: {lt_id}")
        return lt_id

    ami_id = get_latest_al2023_ami()

    resp = ec2_client.create_launch_template(
        LaunchTemplateName=config.LAUNCH_TEMPLATE_NAME,
        VersionDescription="v1",
        LaunchTemplateData={
            "ImageId": ami_id,
            "InstanceType": config.INSTANCE_TYPE,
            "IamInstanceProfile": {"Arn": instance_profile_arn},
            "NetworkInterfaces": [
                {
                    "DeviceIndex": 0,
                    "AssociatePublicIpAddress": True,
                    "Groups": [sg_id],
                    "DeleteOnTermination": True,
                }
            ],
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": _tag("asg-web-instance"),
                }
            ],
        },
        TagSpecifications=[
            {"ResourceType": "launch-template", "Tags": _tag(config.LAUNCH_TEMPLATE_NAME)}
        ],
    )
    lt_id = resp["LaunchTemplate"]["LaunchTemplateId"]
    print(f"  Created: {lt_id}")
    return lt_id


# ---------------------------------------------------------------------------
# 7. Auto Scaling Group
# ---------------------------------------------------------------------------

def get_default_subnet_ids():
    resp = ec2_client.describe_subnets(
        Filters=[{"Name": "defaultForAz", "Values": ["true"]}]
    )
    return [s["SubnetId"] for s in resp["Subnets"]]


def create_asg(lt_id):
    print("\n[7/10] Auto Scaling Group…")
    existing = autoscaling.describe_auto_scaling_groups(
        AutoScalingGroupNames=[config.ASG_NAME]
    )["AutoScalingGroups"]

    if existing:
        print(f"  Already exists: {config.ASG_NAME}")
        return

    subnet_ids = get_default_subnet_ids()
    print(f"  Subnets: {subnet_ids}")

    autoscaling.create_auto_scaling_group(
        AutoScalingGroupName=config.ASG_NAME,
        LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"},
        MinSize=config.MIN_SIZE,
        MaxSize=config.MAX_SIZE,
        DesiredCapacity=config.DESIRED_CAPACITY,
        VPCZoneIdentifier=",".join(subnet_ids),
        Tags=[
            {
                "Key": "Name",
                "Value": "asg-web-instance",
                "PropagateAtLaunch": True,
                "ResourceId": config.ASG_NAME,
                "ResourceType": "auto-scaling-group",
            }
        ],
        HealthCheckType="EC2",
        HealthCheckGracePeriod=300,
    )
    print(f"  Created: {config.ASG_NAME}")


# ---------------------------------------------------------------------------
# 8. Lifecycle Hook
# ---------------------------------------------------------------------------

def create_lifecycle_hook():
    print("\n[8/10] Lifecycle hook…")
    autoscaling.put_lifecycle_hook(
        LifecycleHookName=config.LIFECYCLE_HOOK_NAME,
        AutoScalingGroupName=config.ASG_NAME,
        LifecycleTransition="autoscaling:EC2_INSTANCE_LAUNCHING",
        HeartbeatTimeout=config.HEARTBEAT_TIMEOUT,
        DefaultResult="ABANDON",
    )
    print(
        f"  Hook '{config.LIFECYCLE_HOOK_NAME}' on ASG '{config.ASG_NAME}' "
        f"(timeout={config.HEARTBEAT_TIMEOUT}s, default=ABANDON)"
    )


# ---------------------------------------------------------------------------
# 9. Scale-out policy
# ---------------------------------------------------------------------------

def create_scaling_policy():
    print("\n[9/10] Scale-out policy…")
    resp = autoscaling.put_scaling_policy(
        AutoScalingGroupName=config.ASG_NAME,
        PolicyName=config.SCALING_POLICY_NAME,
        PolicyType="SimpleScaling",
        AdjustmentType="ChangeInCapacity",
        ScalingAdjustment=1,
        Cooldown=300,
    )
    policy_arn = resp["PolicyARN"]
    print(f"  Policy ARN: {policy_arn}")
    return policy_arn


# ---------------------------------------------------------------------------
# 10. CloudWatch CPU alarm
# ---------------------------------------------------------------------------

def create_cloudwatch_alarm(policy_arn):
    print("\n[10/10] CloudWatch CPU alarm…")
    cloudwatch.put_metric_alarm(
        AlarmName=config.CPU_ALARM_NAME,
        AlarmDescription=f"Scale out {config.ASG_NAME} when CPU > {config.CPU_THRESHOLD}%",
        MetricName="CPUUtilization",
        Namespace="AWS/EC2",
        Statistic="Average",
        Dimensions=[{"Name": "AutoScalingGroupName", "Value": config.ASG_NAME}],
        Period=60,
        EvaluationPeriods=1,
        Threshold=config.CPU_THRESHOLD,
        ComparisonOperator="GreaterThanThreshold",
        AlarmActions=[policy_arn],
        TreatMissingData="notBreaching",
    )
    print(
        f"  Alarm '{config.CPU_ALARM_NAME}': CPU > {config.CPU_THRESHOLD}% "
        f"for 1 × 60s → {config.SCALING_POLICY_NAME}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Assignment 10: ASG with Lifecycle Hooks — Infrastructure Setup")
    print("=" * 60)

    sg_id = create_security_group()
    instance_profile_arn = create_ec2_role()
    lambda_role_arn = create_lambda_role()
    fn_arn = create_lambda_function(lambda_role_arn)
    create_eventbridge_rule(fn_arn)
    lt_id = create_launch_template(sg_id, instance_profile_arn)
    create_asg(lt_id)
    create_lifecycle_hook()
    policy_arn = create_scaling_policy()
    create_cloudwatch_alarm(policy_arn)

    print("\n" + "=" * 60)
    print("DEPLOYMENT COMPLETE")
    print("=" * 60)
    print(f"\nASG Name      : {config.ASG_NAME}")
    print(f"Lambda        : {config.LAMBDA_FUNCTION_NAME}")
    print(f"Lifecycle Hook: {config.LIFECYCLE_HOOK_NAME}")
    print(f"CPU Alarm     : {config.CPU_ALARM_NAME} (threshold: {config.CPU_THRESHOLD}%)")
    print(
        "\nMonitor the lifecycle hook execution:"
        f"\n  aws logs tail /aws/lambda/{config.LAMBDA_FUNCTION_NAME} --follow"
    )
    print(
        "\nWatch ASG activity:"
        f"\n  aws autoscaling describe-scaling-activities --auto-scaling-group-name {config.ASG_NAME}"
    )


if __name__ == "__main__":
    main()
