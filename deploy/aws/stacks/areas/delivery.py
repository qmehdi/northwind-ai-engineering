"""Delivery: CodePipeline V2 from GitHub through CodeConnections, CodeBuild builds the two
serving images on arm64 and pushes them by digest, a second CodeBuild project builds the
`nw-pipelines` image on x86_64 (SageMaker Processing runs amd64) beside it, a manual approval,
then CodeDeploy for the Lambda canary and the runtime update (`delivery/deploy.sh`).

One owner for image versions: after a successful promotion `deploy.sh` writes the digest to
the SSM parameters `/<environment>/images/policy` and `/<environment>/images/agent`, and the
stack reads the same parameters for the Lambda's image and the live runtime's container (they
say `asset` until the first promotion, which `scripts/deploy_aws.sh` sets). A later
`cdk deploy` therefore keeps what the pipeline promoted. The runtime update reads the runtime's
whole configuration and passes it back with only the container changed, because
UpdateAgentRuntime replaces the optional fields it is not given (network, protocol, environment,
lifecycle, authorizer, request headers). The policy repository's policy lets Lambda pull
(`lambda.amazonaws.com`, conditioned on this account's functions), which `UpdateFunctionCode`
needs for an image outside the CDK asset repository.

Supply chain: the repositories are tag-immutable (a commit tag always names the same bytes), no
image is pushed as `latest`, and the training image is referenced by digest through
`/<environment>/images/pipelines` (output `PipelineImage` names the parameter). CodeBuild signs
every pushed digest with AWS Signer through Notation (the stack's signing profile, output
`SigningProfileArn`, platform `Notation-OCI-SHA384-ECDSA`), and the Deploy stage verifies both signatures against
that profile before it touches the Lambda or the runtime; an unsigned or foreign digest stops
the deploy. The
pipelines image is pushed under the commit tag and `latest`; `PipelineImage` names `latest`,
which is what `nw.platform.aws` puts in a tenant's pipeline definition, and the Build stage's
`pipelines.json` records the digest it resolved to. Pull-request checks stay in GitHub Actions (ADR
0012); this pipeline is the promotion path.

The cross-account pattern is present but not exercised: `northwind-deployer` is the role a
pipeline in a lower environment assumes to deploy here (its trust is this account; a higher
environment would trust the lower account's pipeline role instead), the artifact bucket is KMS
encrypted with the platform key so a second account can be granted the key, and the Deploy
stage assumes the role before it touches anything, which is the same STS hop drawn on the
delivery figure.

The CodeConnections connection is created by hand once (the GitHub app handshake is a console
step: Developer Tools, Connections, then "Update pending connection"); its ARN comes in as
`-c connectionArn=...`. Without it the stack still creates the repositories and the deployer
role and skips the pipeline, so a solo learner without GitHub access can deploy the platform
and push the images from a laptop with `make images-aws`.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codedeploy as cd
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as actions
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_signer as signer
from aws_cdk import aws_sns as sns
from constructs import Construct

from stacks.areas.data import RETENTION
from stacks.common import SERVICES, image_parameter

IMAGES = ("policy", "agent")
# The training image: every step of both pipelines, no artifact baked in (ADR 0011).
PIPELINES_IMAGE = "pipelines"
NOTATION_PLUGIN = "com.amazonaws.signer.notation.plugin"
# AWS Signer developer guide, image-signing-prerequisites (fetched 2026-09-30): the installer
# ships Notation, the Signer plugin and the `aws-signer-ts` trust store.
NOTATION_RPM = (
    "https://d2hvyiie56hcat.cloudfront.net/linux/{arch}/installer/rpm/latest/"
    "aws-signer-notation-cli_{arch}.rpm"
)
PIPELINES_BUILD = (
    '--build-arg APP=pipelines --build-arg ARTIFACTS="" '
    '--build-arg EXTRAS="--extra dl --extra mlops --extra pipelines"'
)


class Delivery(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        key: kms.IKey,
        logs_bucket: s3.IBucket,
        topic: sns.ITopic,
        policy_fn: lam.IFunction,
        deploy_app: cd.ILambdaApplication,
        deploy_group: cd.ILambdaDeploymentGroup,
        agent_runtime_arn: str,
        agent_runtime_id: str,
        runtime_role: iam.IRole,
        connection_arn: str | None,
        github_owner: str,
        github_repo: str,
        github_branch: str,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)

        self.repos: dict[str, ecr.Repository] = {}
        for name in (*IMAGES, PIPELINES_IMAGE):
            self.repos[name] = ecr.Repository(
                self,
                f"Repo{name.title()}",
                repository_name=f"{prefix}-{name}",
                image_scan_on_push=True,
                encryption=ecr.RepositoryEncryption.KMS,
                encryption_key=key,
                image_tag_mutability=ecr.TagMutability.IMMUTABLE,
                removal_policy=RemovalPolicy.DESTROY,
                empty_on_delete=True,
                lifecycle_rules=[ecr.LifecycleRule(max_image_count=20)],
            )
        # No fixed name: Signer only cancels a deleted profile and never frees its name, so
        # a fixed name would block the next deploy after a destroy.
        self.signing_profile = signer.SigningProfile(
            self,
            "SigningProfile",
            platform=signer.Platform.NOTATION_OCI_SHA384_ECDSA,
            signature_validity=Duration.days(365),
        )

        # The role a deploy assumes: here the pipeline's own account, in a higher environment
        # the lower account's pipeline role.
        self.deployer = iam.Role(
            self,
            "Deployer",
            role_name=f"{prefix}-deployer",
            assumed_by=iam.AccountPrincipal(stack.account),
            description="Assumed by the delivery pipeline to promote images; a higher environment trusts the lower one's pipeline",
            max_session_duration=Duration.hours(1),
        )
        self._grant_deploy(
            self.deployer,
            prefix,
            policy_fn,
            deploy_app,
            deploy_group,
            agent_runtime_arn,
            runtime_role,
        )
        for repo in self.repos.values():
            repo.grant_pull(self.deployer)
        # Lambda pulls a container image with the service principal (Lambda developer guide,
        # "Amazon ECR permissions"); scoped to this account's functions.
        self.repos["policy"].add_to_resource_policy(
            iam.PolicyStatement(
                sid="LambdaECRImageRetrievalPolicy",
                principals=[iam.ServicePrincipal("lambda.amazonaws.com")],
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                conditions={
                    "StringLike": {
                        "aws:sourceArn": f"arn:aws:lambda:{stack.region}:{stack.account}:function:*"
                    }
                },
            )
        )
        self.deployer.add_to_policy(
            iam.PolicyStatement(
                sid="VerifySignatures",
                actions=["signer:GetRevocationStatus"],
                resources=["*"],
            )
        )
        self.deployer.add_to_policy(
            iam.PolicyStatement(
                sid="ImageParameters",
                actions=["ssm:GetParameter", "ssm:PutParameter"],
                resources=[
                    f"arn:aws:ssm:{stack.region}:{stack.account}:parameter{image_parameter(prefix, n)}"
                    for n in IMAGES
                ],
            )
        )

        # The training image by digest: written by the pipelines build (or images_aws.sh).
        self.pipeline_image = image_parameter(prefix, PIPELINES_IMAGE)
        CfnOutput(self, "OutPipelineImage", value=self.pipeline_image).override_logical_id(
            "PipelineImage"
        )
        CfnOutput(
            self, "OutSigningProfileArn", value=self.signing_profile.signing_profile_arn
        ).override_logical_id("SigningProfileArn")
        CfnOutput(
            self,
            "OutImageRepositories",
            value=",".join(f"{n}={r.repository_uri}" for n, r in self.repos.items()),
        ).override_logical_id("ImageRepositories")

        self.pipeline = None
        if not connection_arn:
            CfnOutput(self, "OutDeployerRoleArn", value=self.deployer.role_arn).override_logical_id(
                "DeployerRoleArn"
            )
            return

        artifacts = s3.Bucket(
            self,
            "PipelineArtifacts",
            bucket_name=f"{prefix}-pipeline-{stack.account}-{stack.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            server_access_logs_bucket=logs_bucket,
            server_access_logs_prefix="pipeline/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="operational",
                    expiration=Duration.days(RETENTION["operational"]),
                    noncurrent_version_expiration=Duration.days(RETENTION["noncurrent"]),
                )
            ],
        )
        source = codepipeline.Artifact("source")
        built = codepipeline.Artifact("build")
        built_pipelines = codepipeline.Artifact("pipelines")

        build = codebuild.PipelineProject(
            self,
            "Build",
            project_name=f"{prefix}-build",
            description="Build the policy and agent images on arm64 and push them by digest",
            encryption_key=key,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,
            ),
            environment_variables={
                "ACCOUNT": codebuild.BuildEnvironmentVariable(value=stack.account),
                "REGION": codebuild.BuildEnvironmentVariable(value=stack.region),
                "SIGNING_PROFILE_ARN": codebuild.BuildEnvironmentVariable(
                    value=self.signing_profile.signing_profile_arn
                ),
                **{
                    f"REPO_{n.upper()}": codebuild.BuildEnvironmentVariable(value=r.repository_uri)
                    for n, r in self.repos.items()
                    if n in IMAGES
                },
            },
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(
                    log_group=logs.LogGroup(
                        self,
                        "BuildLogs",
                        log_group_name=f"/{prefix}/codebuild/build",
                        retention=logs.RetentionDays.ONE_MONTH,
                        removal_policy=RemovalPolicy.DESTROY,
                    )
                )
            ),
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "pre_build": {
                            "commands": [
                                "aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com",
                                "export TAG=${CODEBUILD_RESOLVED_SOURCE_VERSION:0:12}",
                                f"curl -fsSL -o /tmp/notation.rpm {NOTATION_RPM.format(arch='arm64')} && rpm -U /tmp/notation.rpm",
                            ]
                        },
                        "build": {
                            "commands": [
                                self._docker_build("policy"),
                                self._docker_build("agent"),
                            ]
                        },
                        "post_build": {
                            "commands": [
                                "docker push $REPO_POLICY:$TAG",
                                "docker push $REPO_AGENT:$TAG",
                                'P=$(aws ecr describe-images --repository-name $(basename $REPO_POLICY) --image-ids imageTag=$TAG --query "imageDetails[0].imageDigest" --output text)',
                                'A=$(aws ecr describe-images --repository-name $(basename $REPO_AGENT) --image-ids imageTag=$TAG --query "imageDetails[0].imageDigest" --output text)',
                                f'notation sign --plugin {NOTATION_PLUGIN} --id "$SIGNING_PROFILE_ARN" "$REPO_POLICY@$P"',
                                f'notation sign --plugin {NOTATION_PLUGIN} --id "$SIGNING_PROFILE_ARN" "$REPO_AGENT@$A"',
                                'printf \'{"policy":"%s@%s","agent":"%s@%s","tag":"%s"}\' "$REPO_POLICY" "$P" "$REPO_AGENT" "$A" "$TAG" > images.json',
                                "cat images.json",
                            ]
                        },
                    },
                    "artifacts": {"files": ["images.json"]},
                }
            ),
        )
        for name in IMAGES:
            self.repos[name].grant_pull_push(build)
        self._grant_sign(build)
        build.add_to_role_policy(
            iam.PolicyStatement(
                sid="DescribeImages",
                actions=["ecr:DescribeImages"],
                resources=[self.repos[n].repository_arn for n in IMAGES],
            )
        )
        build_pipelines = self._pipelines_project(prefix, key)

        deploy = codebuild.PipelineProject(
            self,
            "DeployProject",
            project_name=f"{prefix}-deploy",
            description="Promote the built digests: Lambda canary through CodeDeploy, runtime update",
            encryption_key=key,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=codebuild.ComputeType.SMALL,
            ),
            environment_variables={
                "POLICY_FUNCTION": codebuild.BuildEnvironmentVariable(
                    value=policy_fn.function_name
                ),
                "CODEDEPLOY_APP": codebuild.BuildEnvironmentVariable(
                    value=deploy_app.application_name
                ),
                "CODEDEPLOY_GROUP": codebuild.BuildEnvironmentVariable(
                    value=deploy_group.deployment_group_name
                ),
                "AGENT_RUNTIME_ID": codebuild.BuildEnvironmentVariable(value=agent_runtime_id),
                "IMAGE_PARAM_POLICY": codebuild.BuildEnvironmentVariable(
                    value=image_parameter(prefix, "policy")
                ),
                "IMAGE_PARAM_AGENT": codebuild.BuildEnvironmentVariable(
                    value=image_parameter(prefix, "agent")
                ),
                "DEPLOYER_ROLE_ARN": codebuild.BuildEnvironmentVariable(
                    value=self.deployer.role_arn
                ),
                "SIGNING_PROFILE_ARN": codebuild.BuildEnvironmentVariable(
                    value=self.signing_profile.signing_profile_arn
                ),
                "NOTATION_RPM": codebuild.BuildEnvironmentVariable(
                    value=NOTATION_RPM.format(arch="arm64")
                ),
            },
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(
                    log_group=logs.LogGroup(
                        self,
                        "DeployLogs",
                        log_group_name=f"/{prefix}/codebuild/deploy",
                        retention=logs.RetentionDays.ONE_MONTH,
                        removal_policy=RemovalPolicy.DESTROY,
                    )
                )
            ),
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {"build": {"commands": ["bash deploy/aws/delivery/deploy.sh"]}},
                }
            ),
        )
        deploy.add_to_role_policy(
            iam.PolicyStatement(
                sid="AssumeDeployer", actions=["sts:AssumeRole"], resources=[self.deployer.role_arn]
            )
        )
        self.deployer.assume_role_policy.add_statements(  # type: ignore[union-attr]
            iam.PolicyStatement(
                actions=["sts:AssumeRole"], principals=[iam.ArnPrincipal(deploy.role.role_arn)]
            )  # type: ignore[union-attr]
        )

        self.pipeline = codepipeline.Pipeline(
            self,
            "Pipeline",
            pipeline_name=f"{prefix}-delivery",
            pipeline_type=codepipeline.PipelineType.V2,
            artifact_bucket=artifacts,
            cross_account_keys=False,
            restart_execution_on_update=False,
            stages=[
                codepipeline.StageProps(
                    stage_name="Source",
                    actions=[
                        actions.CodeStarConnectionsSourceAction(
                            action_name="GitHub",
                            connection_arn=connection_arn,
                            owner=github_owner,
                            repo=github_repo,
                            branch=github_branch,
                            output=source,
                            trigger_on_push=True,
                        )
                    ],
                ),
                codepipeline.StageProps(
                    stage_name="Build",
                    actions=[
                        actions.CodeBuildAction(
                            action_name="Images", project=build, input=source, outputs=[built]
                        ),
                        actions.CodeBuildAction(
                            action_name="PipelinesImage",
                            project=build_pipelines,
                            input=source,
                            outputs=[built_pipelines],
                        ),
                    ],
                ),
                codepipeline.StageProps(
                    stage_name="Approve",
                    actions=[
                        actions.ManualApprovalAction(
                            action_name="Promote",
                            notification_topic=topic,
                            additional_information="Review the pull-request checks and the eval gates, then approve to promote by digest",
                        )
                    ],
                ),
                codepipeline.StageProps(
                    stage_name="Deploy",
                    actions=[
                        actions.CodeBuildAction(
                            action_name="Canary", project=deploy, input=source, extra_inputs=[built]
                        )
                    ],
                ),
            ],
        )
        CfnOutput(self, "OutDeployerRoleArn", value=self.deployer.role_arn).override_logical_id(
            "DeployerRoleArn"
        )
        CfnOutput(self, "OutPipeline", value=self.pipeline.pipeline_name).override_logical_id(
            "Pipeline"
        )

    def _pipelines_project(self, prefix: str, key: kms.IKey) -> codebuild.PipelineProject:
        """The `nw-pipelines` image on x86_64, pushed under the commit tag and `latest`, its
        digest written to `pipelines.json`. SageMaker Processing runs amd64 only, so this is
        its own project on an x86 build image rather than an emulated build on the arm one."""
        stack = Stack.of(self)
        repo = self.repos[PIPELINES_IMAGE]
        project = codebuild.PipelineProject(
            self,
            "BuildPipelines",
            project_name=f"{prefix}-build-pipelines",
            description="Build the nw-pipelines training image on x86_64 and push it by digest",
            encryption_key=key,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.AMAZON_LINUX_2023_5,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,
            ),
            environment_variables={
                "ACCOUNT": codebuild.BuildEnvironmentVariable(value=stack.account),
                "REGION": codebuild.BuildEnvironmentVariable(value=stack.region),
                "REPO_PIPELINES": codebuild.BuildEnvironmentVariable(value=repo.repository_uri),
                "SIGNING_PROFILE_ARN": codebuild.BuildEnvironmentVariable(
                    value=self.signing_profile.signing_profile_arn
                ),
                "IMAGE_PARAM_PIPELINES": codebuild.BuildEnvironmentVariable(
                    value=image_parameter(prefix, PIPELINES_IMAGE)
                ),
            },
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(
                    log_group=logs.LogGroup(
                        self,
                        "BuildPipelinesLogs",
                        log_group_name=f"/{prefix}/codebuild/build-pipelines",
                        retention=logs.RetentionDays.ONE_MONTH,
                        removal_policy=RemovalPolicy.DESTROY,
                    )
                )
            ),
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "pre_build": {
                            "commands": [
                                "aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com",
                                "export TAG=${CODEBUILD_RESOLVED_SOURCE_VERSION:0:12}",
                                f"curl -fsSL -o /tmp/notation.rpm {NOTATION_RPM.format(arch='amd64')} && rpm -U /tmp/notation.rpm",
                            ]
                        },
                        "build": {
                            "commands": [
                                f"docker build --platform linux/amd64 {PIPELINES_BUILD} "
                                "-t $REPO_PIPELINES:$TAG ."
                            ]
                        },
                        "post_build": {
                            "commands": [
                                "docker push $REPO_PIPELINES:$TAG",
                                'D=$(aws ecr describe-images --repository-name $(basename $REPO_PIPELINES) --image-ids imageTag=$TAG --query "imageDetails[0].imageDigest" --output text)',
                                f'notation sign --plugin {NOTATION_PLUGIN} --id "$SIGNING_PROFILE_ARN" "$REPO_PIPELINES@$D"',
                                'aws ssm put-parameter --name "$IMAGE_PARAM_PIPELINES" --type String --overwrite --value "$REPO_PIPELINES@$D"',
                                'printf \'{"pipelines":"%s@%s","tag":"%s"}\' "$REPO_PIPELINES" "$D" "$TAG" > pipelines.json',
                                "cat pipelines.json",
                            ]
                        },
                    },
                    "artifacts": {"files": ["pipelines.json"]},
                }
            ),
        )
        repo.grant_pull_push(project)
        self._grant_sign(project)
        project.add_to_role_policy(
            iam.PolicyStatement(
                sid="PipelinesImageParameter",
                actions=["ssm:PutParameter"],
                resources=[
                    f"arn:aws:ssm:{stack.region}:{stack.account}:parameter{image_parameter(prefix, PIPELINES_IMAGE)}"
                ],
            )
        )
        project.add_to_role_policy(
            iam.PolicyStatement(
                sid="DescribeImages",
                actions=["ecr:DescribeImages"],
                resources=[repo.repository_arn],
            )
        )
        return project

    def _grant_sign(self, project: codebuild.PipelineProject) -> None:
        project.add_to_role_policy(
            iam.PolicyStatement(
                sid="SignImages",
                actions=["signer:SignPayload", "signer:GetSigningProfile"],
                resources=[self.signing_profile.signing_profile_arn],
            )
        )
        project.add_to_role_policy(
            iam.PolicyStatement(
                sid="SignatureArtifacts",
                actions=["ecr:DescribeRepositories", "ecr:ListImages"],
                resources=[r.repository_arn for r in self.repos.values()],
            )
        )

    @staticmethod
    def _docker_build(name: str) -> str:
        app, artifacts, hf, _cpu, _mem = SERVICES[name]
        lambda_arg = " --build-arg LAMBDA=1 --build-arg PORT=8000" if name == "policy" else ""
        return (
            f"docker build --platform linux/arm64 --build-arg APP={app} --build-arg ARTIFACTS='{artifacts}' "
            f"--build-arg HF_MODELS={hf}{lambda_arg} -t $REPO_{name.upper()}:$TAG ."
        )

    @staticmethod
    def _grant_deploy(
        role, prefix, policy_fn, deploy_app, deploy_group, agent_runtime_arn, runtime_role
    ) -> None:
        stack = Stack.of(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="LambdaCanary",
                actions=[
                    "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration",  # NW_IMAGE_DIGEST beside the code
                    "lambda:PublishVersion",
                    "lambda:GetFunction",
                    "lambda:GetFunctionConfiguration",
                    "lambda:GetAlias",
                    "lambda:UpdateAlias",
                ],
                resources=[policy_fn.function_arn, f"{policy_fn.function_arn}:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="CodeDeploy",
                actions=[
                    "codedeploy:CreateDeployment",
                    "codedeploy:GetDeployment",
                    "codedeploy:GetDeploymentConfig",
                    "codedeploy:GetDeploymentGroup",
                    "codedeploy:RegisterApplicationRevision",
                    "codedeploy:GetApplicationRevision",
                ],
                resources=[
                    f"arn:aws:codedeploy:{stack.region}:{stack.account}:application:{deploy_app.application_name}",
                    f"arn:aws:codedeploy:{stack.region}:{stack.account}:deploymentgroup:{deploy_app.application_name}/{deploy_group.deployment_group_name}",
                    f"arn:aws:codedeploy:{stack.region}:{stack.account}:deploymentconfig:*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RuntimeUpdate",
                actions=[
                    "bedrock-agentcore:UpdateAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntime",
                ],
                resources=[agent_runtime_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassRuntimeRole",
                actions=["iam:PassRole"],
                resources=[runtime_role.role_arn],
                conditions={
                    "StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}
                },
            )
        )
