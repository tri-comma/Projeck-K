#!/usr/bin/env python3
import os
import aws_cdk as cdk
from infrastructure.karaoke_stack import KaraokeStack

app = cdk.App()

# --- 設定値 (Configuration) ---
CONFIG = {
    # 既存のドメイン情報
    "domain_name": "tri-comma.com",
    "subdomain": "k",
    
    # リソース識別子
    "s3_bucket_name": "k.tri-comma.com-frontend",
    "lambda_func_name": "k-tri-comma-com-backend",
    "stack_name": "k-tri-comma-com-stack",
    
    # 開発用フラグ
    "is_dev": True
}

# --- スタックの定義 ---
# us-east-1 (N. Virginia) に固定することで、ACM証明書周りの構成をシンプルにする
env_us_east_1 = cdk.Environment(account=os.getenv('CDK_DEFAULT_ACCOUNT'), region='us-east-1')

KaraokeStack(app, CONFIG["stack_name"],
    config=CONFIG,
    env=env_us_east_1
)

app.synth()