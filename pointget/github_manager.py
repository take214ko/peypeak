# github_manager.py
import os
import json
import base64
import aiohttp
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OWNER = os.getenv("GITHUB_REPO_OWNER")
REPO = os.getenv("GITHUB_REPO_NAME")
PATH = "data.json"  # リポジトリ内の対象ファイルパス

async def push_json_to_github(local_json_path):
    """ローカルのjsonファイルをGitHubリポジトリへ非同期で直接プッシュする"""
    if not GITHUB_TOKEN or not OWNER or not REPO:
        print("【エラー】GitHub設定用の環境変数が不足しています。")
        return False

    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    # 1. ローカルの最新JSONデータを読み込んでBase64エンコード
    try:
        with open(local_json_path, "r", encoding="utf-8") as f:
            content = f.read()
        content_bytes = content.encode("utf-8")
        base64_content = base64.b64encode(content_bytes).decode("utf-8")
    except Exception as e:
        print(f"【エラー】ローカルファイルの読み込みに失敗: {e}")
        return False

    async with aiohttp.ClientSession() as session:
        # 2. GitHub上の現在のファイルのSHA（既存ファイル識別子）を取得
        sha = None
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                res_data = await resp.json()
                sha = res_data.get("sha")
            elif resp.status != 404:
                # 404以外のエラー（認証エラーなど）は中断
                print(f"【エラー】GitHubからのファイル情報取得失敗 (Status: {resp.status})")
                return False

        # 3. GitHubへアップデート（または新規作成）リクエストを送信
        payload = {
            "message": "🔄 Bot: data.json 自動最高値更新",
            "content": base64_content,
        }
        if sha:
            payload["sha"] = sha  # 上書きの際に必須

        async with session.put(url, headers=headers, json=payload) as resp:
            if resp.status in [200, 201]:
                print("🚀 GitHub上の data.json の自動更新に成功しました。")
                return True
            else:
                error_detail = await resp.text()
                print(f"❌ GitHub更新エラー (Status: {resp.status}): {error_detail}")
                return False