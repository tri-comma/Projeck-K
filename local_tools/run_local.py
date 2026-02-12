import sys
import os
from pathlib import Path
import uvicorn
from fastapi.staticfiles import StaticFiles

# --- パス解決のロジック ---
# このファイル (run_local.py) のあるディレクトリ
current_dir = Path(__file__).resolve().parent
# プロジェクトルート (my-karaoke-app/) ※1つ上の階層
project_root = current_dir.parent

# backend フォルダをモジュール検索パスに追加
# これにより "from backend.main import ..." ではなく、直接 "from main import ..." が可能になる
# (Lambda環境での動作に合わせるため、backend直下をルートに見立てるのがコツです)
sys.path.append(str(project_root / "backend"))

# backend/main.py から app をインポート
from main import app

# --- 静的ファイルの設定 ---
# frontend フォルダの絶対パスを取得
frontend_dir = project_root / "frontend"

# マウント
app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

if __name__ == "__main__":
    print(f"Project Root: {project_root}")
    print(f"Frontend Dir: {frontend_dir}")
    print("Starting Local Server...")
    
    # ローカル開発フラグをセット
    os.environ["LOCAL_DEV"] = "true"
    
    # 起動
    uvicorn.run(app, host="0.0.0.0", port=8000)