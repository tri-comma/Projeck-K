import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy, # 【追加】デプロイ用モジュール
    aws_lambda as _lambda,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integ,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_wafv2 as wafv2,
)
from constructs import Construct
import os

class KaraokeStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        full_domain = f"{config['subdomain']}.{config['domain_name']}"

        # ---------------------------------------------------------
        # 1. Route53 & ACM (Domain & Certificate)
        # ---------------------------------------------------------
        # 既存のホストゾーンを参照
        hosted_zone = route53.HostedZone.from_lookup(self, "HostedZone",
            domain_name=config["domain_name"]
        )

        # SSL証明書の作成 (us-east-1必須。DNS検証も自動)
        certificate = acm.Certificate(self, "SiteCertificate",
            domain_name=full_domain,
            validation=acm.CertificateValidation.from_dns(hosted_zone)
        )

        # ---------------------------------------------------------
        # 2. S3 Bucket (Frontend)
        # ---------------------------------------------------------
        frontend_bucket = s3.Bucket(self, "FrontendBucket",
            bucket_name=config["s3_bucket_name"],
            # CloudFront経由でのみアクセス許可 (OACを使用)
            access_control=s3.BucketAccessControl.PRIVATE,
            removal_policy=RemovalPolicy.DESTROY, # スタック削除時にバケットも消す(本番ならRETAIN推奨)
            auto_delete_objects=True,             # 中身も空にする
        )

        # ---------------------------------------------------------
        # 3. Lambda (Backend API)
        # ---------------------------------------------------------
        # Dockerを使用して依存ライブラリ(fastapi, mangum, ytmusicapi)をインストール・同梱する
        backend_lambda = _lambda.Function(self, "BackendHandler",
            function_name=config["lambda_func_name"],
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="main.handler", # Mangumでラップしたエントリーポイント
            code=_lambda.Code.from_asset(
                path="backend", # backendフォルダを参照
                exclude=[
                    ".venv",
                    "venv",
                    "__pycache__",
                    "*.pyc",
                    ".git"
                ],
                bundling=cdk.BundlingOptions(  # ここを _lambda から cdk に変更
                    image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash", "-c",
                        "cp -R . /asset-output && pip install --platform manylinux2014_aarch64 --target /asset-output --implementation cp --python-version 3.12 --only-binary=:all: -r requirements.txt"
                    ],
                )
            ),
            timeout=Duration.seconds(30), # YouTube検索は少し時間がかかる場合があるため
            memory_size=256,
        )

        # ---------------------------------------------------------
        # 4. API Gateway (HTTP API)
        # ---------------------------------------------------------
        http_api = apigwv2.HttpApi(self, "KaraokeHttpApi",
            default_integration=apigwv2_integ.HttpLambdaIntegration(
                "LambdaIntegration", backend_lambda
            )
        )

        # ---------------------------------------------------------
        # 5. CloudFront (Distribution)
        # ---------------------------------------------------------
        # 【修正】OAC (最新機能) が環境起因で動かないため、
        # 安定板の OAI (Origin Access Identity) を使用します。
        
        # 1. OAIを作成
        origin_access_identity = cloudfront.OriginAccessIdentity(self, "MyOAI")
        
        # 2. S3バケットへの読み取り権限をOAIに付与
        frontend_bucket.grant_read(origin_access_identity)

        # ---------------------------------------------------------
        # WAF v2 (Access Control)
        # ---------------------------------------------------------
        
        # 1. IP Set for Whitelist (開発用IP等)
        ip_set = wafv2.CfnIPSet(self, "AllowedIPSet",
            name=f"{config['stack_name']}-allowed-ips",
            description="Allow access from these IPs regardless of other rules",
            addresses=config.get("allowed_ips", []),
            scope="CLOUDFRONT",
            ip_address_version="IPV4"
        )

        # 2. Web ACL
        web_acl = wafv2.CfnWebACL(self, "WebACL",
            name=f"{config['stack_name']}-web-acl",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            scope="CLOUDFRONT",
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="WebACL",
                sampled_requests_enabled=True
            ),
            rules=[
                # Rule 0: 指定IPを優先許可
                wafv2.CfnWebACL.RuleProperty(
                    name="AllowWhitelistIPs",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(allow={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        ip_set_reference_statement=wafv2.CfnWebACL.IPSetReferenceStatementProperty(
                            arn=ip_set.attr_arn
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AllowWhitelistIPs",
                        sampled_requests_enabled=True
                    )
                ),
                # Rule 1: 日本国外IPを拒否
                wafv2.CfnWebACL.RuleProperty(
                    name="DenyNonJPIPs",
                    priority=1,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        not_statement=wafv2.CfnWebACL.NotStatementProperty(
                            statement=wafv2.CfnWebACL.StatementProperty(
                                geo_match_statement=wafv2.CfnWebACL.GeoMatchStatementProperty(
                                    country_codes=["JP"]
                                )
                            )
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="DenyNonJPIPs",
                        sampled_requests_enabled=True
                    )
                ),
                # Rule 2: 特定のUA (Windows, Mac, iOS, Android) を拒否
                wafv2.CfnWebACL.RuleProperty(
                    name="DenyBlacklistedUA",
                    priority=2,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        or_statement=wafv2.CfnWebACL.OrStatementProperty(
                            statements=[
                                wafv2.CfnWebACL.StatementProperty(
                                    byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                                        field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(single_header={"name": "user-agent"}),
                                        positional_constraint="CONTAINS",
                                        search_string=ua,
                                        text_transformations=[wafv2.CfnWebACL.TextTransformationProperty(priority=0, type="LOWERCASE")]
                                    )
                                ) for ua in ["windows", "macintosh", "iphone", "ipad", "android"]
                            ]
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="DenyBlacklistedUA",
                        sampled_requests_enabled=True
                    )
                )
            ]
        )

        distribution = cloudfront.Distribution(self, "SiteDistribution",
            domain_names=[full_domain],
            certificate=certificate,
            default_root_object="index.html",
            
            # デフォルトの挙動: S3へ (Frontend)
            default_behavior=cloudfront.BehaviorOptions(
                # 【修正】origin_access_control ではなく origin_access_identity を指定
                origin=origins.S3Origin(frontend_bucket, origin_access_identity=origin_access_identity),
                
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            
            # WAF & 403設定
            web_acl_id=web_acl.attr_arn,
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=403,
                    response_page_path="/403.html",
                    ttl=Duration.seconds(0)
                )
            ],
            
            # /api/* の挙動: API Gatewayへ (Backend)
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(
                        f"{http_api.api_id}.execute-api.{self.region}.amazonaws.com",
                        origin_path="" 
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER
                )
            }
        )

        # ---------------------------------------------------------
        # 6. DNS Record (A Alias)
        # ---------------------------------------------------------
        route53.ARecord(self, "SiteAliasRecord",
            zone=hosted_zone,
            record_name=config["subdomain"],
            target=route53.RecordTarget.from_alias(targets.CloudFrontTarget(distribution))
        )

        # ---------------------------------------------------------
        # 7. S3 Deployment (Frontend Assets)
        # ---------------------------------------------------------
        # 【追加】frontendディレクトリの中身をS3にアップロードし、変更があればCloudFrontのキャッシュも削除
        s3deploy.BucketDeployment(self, "DeployWebsite",
            sources=[s3deploy.Source.asset("frontend")], # ローカルのfrontendフォルダ
            destination_bucket=frontend_bucket,
            distribution=distribution, # キャッシュ無効化のためDistributionを指定
            distribution_paths=["/*"], # 全ファイルのキャッシュをクリア
        )