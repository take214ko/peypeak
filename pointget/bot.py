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
BACKLOG_HISTORY_LIMIT = 500  # 100件 → 500件に増加
API_DELAY_SECONDS = 2  # Gemini API呼び出し間のディレイ（秒）

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
                    model="gemini-2.0-flash",
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

        # リスト型（複数案件）またはオブジェクト型（単一案件）の両方に対応
        items = data if isinstance(data, list) else [data]
        
        db_statuses = []
        for item in items:
            site_name = item.get("site_name")
            project_name = item.get("project_name")
            points = item.get("points", 0)

            if site_name and project_name:
                is_updated, old_points = await asyncio.to_thread(upsert_project, site_name, project_name, points)
                if is_updated:
                    if old_points == 0:
                        status = f"✨ **【新規案件登録！】** {site_name}の{project_name}を **{points}pt** でデータベースに追加しました！"
                    else:
                        status = f"🚀 **【過去最高値更新！】** {old_points}pt ➡ **{points}pt** にアップデートされました！"

                    await asyncio.to_thread(export_db_to_json)
                    status += "\n📁 フロントエンド用 data.json をローカルで自動更新しました。"

                    github_status = await push_json_to_github(JSON_PATH)
                    if github_status:
                        status += "\n🌐 Webサイトへの自動同期をリクエストしました（数分以内に反映されます）。"
                    else:
                        status += "\n⚠️ Webサイトへの自動同期に失敗しました。Botのシステムログを確認してください。"
                else:
                    status = f"💤 【記録維持】過去最高値（{old_points}pt）の方が高いため、データは更新されませんでした（今回: {points}pt）。"
            else:
                status = "⚠️ データの構造化が不十分なため、DBへの保存をスキップしました。"
            
            db_statuses.append(status)

        db_status = "\n".join(db_statuses)

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


async def mark_all_current_messages_as_processed():
    """
    起動時に、現時点で存在するすべてのメッセージを処理済みとしてマーク
    （これにより、その後新規に送られるメッセージだけが処理される）
    """
    print("🔖 現在のすべてのメッセージを処理済みとしてマーク中...")
    marked_count = 0
    
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                bot_member = guild.me
                if not bot_member:
                    continue
                permissions = channel.permissions_for(bot_member)
                if not permissions.read_message_history:
                    continue
                
                async for message in channel.history(limit=BACKLOG_HISTORY_LIMIT, oldest_first=True):
                    if message.content and message.content.strip():
                        processed_message_ids.add(str(message.id))
                        marked_count += 1
            except Exception:
                continue
    
    save_processed_message_ids()
    print(f"✅ {marked_count}件のメッセージを処理済みとしてマークしました。\n")


async def process_backlog_messages():
    print("🕘 オフライン時に溜まっていたメッセージを確認しています...")

    processed_count = 0
    for guild in bot.guilds:
        print(f"📍 サーバー '{guild.name}' をスキャン中...")
        
        bot_member = guild.me
        if not bot_member:
            print(f"  ❌ サーバー内でボットメンバーを特定できません")
            continue
        
        text_channels = guild.text_channels
        print(f"  ├─ テキストチャンネル数: {len(text_channels)}件")
        
        for channel in text_channels:
            try:
                permissions = channel.permissions_for(bot_member)
                print(f"  ├─ #{channel.name}: ", end="")
                
                if not permissions.read_messages:
                    print("❌ メッセージ読取権限なし")
                    continue
                if not permissions.read_message_history:
                    print("⏭️  メッセージ履歴読取権限なし")
                    continue
                
                print("✅ 権限あり（スキャン開始）")
                
            except Exception as e:
                print(f"❌ 権限確認エラー: {e}")
                continue

            try:
                message_count = 0
                async for history_message in channel.history(limit=BACKLOG_HISTORY_LIMIT, oldest_first=True):
                    message_count += 1
                    
                    if history_message.author == bot.user:
                        continue
                    if not history_message.content or not history_message.content.strip():
                        continue
                    if str(history_message.id) in processed_message_ids:
                        continue

                    print(f"    📨 未処理メッセージ: '{history_message.content[:40]}...'")
                    success = await process_single_message(history_message)
                    if success:
                        processed_count += 1
                        try:
                            await history_message.add_reaction("✅")
                        except Exception:
                            pass
                    
                    # Gemini API呼び出し間のディレイ（レート制限回避）
                    await asyncio.sleep(API_DELAY_SECONDS)
                
                if message_count > 0:
                    print(f"  └─ #{channel.name}: {message_count}件スキャン完了")
            except Exception as e:
                print(f"  ❌ チャンネル {channel.name} エラー: {e}")

    if processed_count > 0:
        print(f"\n✅ オフライン時のメッセージ処理完了！（{processed_count}件処理）")
    else:
        print("\n⚠️ 未処理のメッセージはありません。")


@bot.event
async def on_ready():
    print(f"実験システム（Gemini ✕ SQLite ✕ SSG連動版）起動成功: {bot.user.name}")
    await mark_all_current_messages_as_processed()
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
