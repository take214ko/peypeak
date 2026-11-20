import os
import json
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from google import genai
from google.genai import types

from db_manager import upsert_project, export_db_to_json
from github_manager import push_json_to_github

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE_DIR, "data.json")
PROCESSED_MESSAGES_FILE = os.path.join(BASE_DIR, "processed_messages.json")
BACKLOG_HISTORY_LIMIT = 100

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

processed_message_ids = set()


def load_processed_message_ids():
    global processed_message_ids
    if not os.path.exists(PROCESSED_MESSAGES_FILE):
        return set()

    try:
        with open(PROCESSED_MESSAGES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            processed_message_ids = {str(item) for item in data}
        elif isinstance(data, dict):
            processed_message_ids = {str(item) for item in data.get("message_ids", [])}
        else:
            processed_message_ids = set()
    except Exception as e:
        print(f"⚠️ 処理済みメッセージ一覧の読み込みに失敗しました: {e}")
        processed_message_ids = set()

    return processed_message_ids


def save_processed_message_ids():
    try:
        with open(PROCESSED_MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(processed_message_ids), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 処理済みメッセージ一覧の保存に失敗しました: {e}")


def remember_processed_message(message):
    processed_message_ids.add(str(message.id))
    save_processed_message_ids()


load_processed_message_ids()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
gemini_client = genai.Client()


async def process_single_message(message):
    if message.author == bot.user:
        return False

    if not message.content or not message.content.strip():
        return False

    message_key = str(message.id)
    if message_key in processed_message_ids:
        return False

    remember_processed_message(message)

    print(f"\n[データ受信] Discord入力テキスト: {message.content}")
    processing_msg = await message.channel.send("⏳ Gemini API でテキストを解析中...")

    try:
        prompt = f"""
        以下のポイ活案件に関するテキストを解析し、指定されたJSONフォーマットで出力してください。

        【解析対象テキスト】
        {message.content}

        【サイト名（site_name）に関する絶対ルール】
        site_nameには、必ず以下のいずれかのカタカナ表記のみを適用してください。
        表記が英語だったり揺れていた場合は、最も近い以下のマスター名に変換（名寄せ）してください。
        リストにない、または判別できない場合は null にしてください。
        既定リスト: [モッピー, ハピタス, ちょびリッチ, ポイントインカム, ポイントタウン, A8ネット]

        【出力JSONフォーマット】
        {{
            "site_name": "上記の既定リストから選択（例：モッピー、ハピタス。不明なら null）",
            "project_name": "案件名・サービス名（例：楽天カード、SBI証券など）",
            "points": "獲得ポイント数（純粋な数値のみ。例：10000。取得できなければ 0）"
        }}
        """

        max_retries = 3
        response = None

        for attempt in range(max_retries):
            try:
                response = await gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
                break
            except Exception as api_e:
                if "503" in str(api_e) and attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"⚠️ 503エラー検知。{wait_time}秒後に再試行します... ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_time)
                else:
                    raise api_e

        raw_json_text = response.text
        print(f"[Geminiレスポンス]: {raw_json_text}")

        data = json.loads(raw_json_text)
        site_name = data.get("site_name")
        project_name = data.get("project_name")
        points = data.get("points", 0)

        if site_name and project_name:
            is_updated, old_points = await asyncio.to_thread(upsert_project, site_name, project_name, points)
            if is_updated:
                if old_points == 0:
                    db_status = f"✨ **【新規案件登録！】** {site_name}の{project_name}を **{points}pt** でデータベースに追加しました！"
                else:
                    db_status = f"🚀 **【過去最高値更新！】** {old_points}pt ➡ **{points}pt** にアップデートされました！"

                await asyncio.to_thread(export_db_to_json)
                db_status += "\n📁 フロントエンド用 data.json をローカルで自動更新しました。"

                github_status = await push_json_to_github(JSON_PATH)
                if github_status:
                    db_status += "\n🌐 Webサイトへの自動同期をリクエストしました（数分以内に反映されます）。"
                else:
                    db_status += "\n⚠️ Webサイトへの自動同期に失敗しました。Botのシステムログを確認してください。"
            else:
                db_status = f"💤 【記録維持】過去最高値（{old_points}pt）の方が高いため、データは更新されませんでした（今回: {points}pt）。"
        else:
            db_status = "⚠️ データの構造化が不十分なため、DBへの保存をスキップしました。"

        await processing_msg.delete()
        await message.channel.send(
            f"✅ 解析完了！\n"
            f"```json\n{raw_json_text}\n```\n"
            f"{db_status}"
        )
        return True

    except Exception as e:
        print(f"【例外発生】エラーログ: {e}")
        try:
            await processing_msg.edit(content=f"❌ エラーが発生しました:\n`{e}`")
        except Exception:
            await message.channel.send(f"❌ エラーが発生しました:\n`{e}`")
        return False


async def process_backlog_messages():
    print("🕘 オフライン時に溜まっていたメッセージを確認しています...")

    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                if not channel.permissions_for(bot.user).read_message_history:
                    continue
            except Exception:
                continue

            try:
                async for history_message in channel.history(limit=BACKLOG_HISTORY_LIMIT, oldest_first=False):
                    if history_message.author == bot.user:
                        continue
                    if str(history_message.id) in processed_message_ids:
                        continue
                    if not history_message.content or not history_message.content.strip():
                        continue

                    await process_single_message(history_message)
            except Exception as e:
                print(f"⚠️ チャンネル {channel.name} の履歴取得に失敗しました: {e}")

    print("✅ オフライン時の未処理メッセージの確認を完了しました。")


@bot.event
async def on_ready():
    print(f"実験システム（Gemini ✕ SQLite ✕ SSG連動版）起動成功: {bot.user.name}")
    await process_backlog_messages()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if not message.content or not message.content.strip():
        return

    if str(message.id) in processed_message_ids:
        return

    await process_single_message(message)


bot.run(TOKEN)
