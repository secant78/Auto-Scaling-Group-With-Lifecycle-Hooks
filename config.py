REGION = "us-east-1"
SUFFIX = "sean"

# ASG / Launch Template
ASG_NAME = f"web-asg-{SUFFIX}"
LAUNCH_TEMPLATE_NAME = f"web-lt-{SUFFIX}"
INSTANCE_TYPE = "t3.micro"
MIN_SIZE = 1
MAX_SIZE = 3
DESIRED_CAPACITY = 1

# Lifecycle Hook
LIFECYCLE_HOOK_NAME = f"web-launch-hook-{SUFFIX}"
HEARTBEAT_TIMEOUT = 900  # 15 minutes — Lambda completes action before this

# Lambda
LAMBDA_FUNCTION_NAME = f"asg-lifecycle-handler-{SUFFIX}"
LAMBDA_ROLE_NAME = f"asg-lambda-role-{SUFFIX}"
LAMBDA_TIMEOUT = 600  # 10 minutes

# IAM (EC2 instance profile)
EC2_ROLE_NAME = f"asg-ec2-role-{SUFFIX}"
EC2_INSTANCE_PROFILE_NAME = f"asg-ec2-profile-{SUFFIX}"

# Networking
SECURITY_GROUP_NAME = f"web-asg-sg-{SUFFIX}"

# EventBridge
EVENTBRIDGE_RULE_NAME = f"asg-lifecycle-rule-{SUFFIX}"

# CloudWatch / Scaling
CPU_ALARM_NAME = f"web-asg-cpu-high-{SUFFIX}"
CPU_THRESHOLD = 60.0
SCALING_POLICY_NAME = f"web-asg-scale-out-{SUFFIX}"
