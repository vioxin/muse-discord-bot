"""みゅーちゃん Discord Bot (Render 常駐版)

- Discord Gateway に常駐して、参加サーバーの全テキストチャンネルの
  メッセージをメモリに集める。
- 小さな HTTP サーバーを内蔵し、Muse からの中継リクエストを受ける。
    GET  /health   : 生存確認 (認証なし、GAS の wake 用)
    GET  /messages : 集めたメッセージを返す (要 X-Relay-Key)
                     ?clear=1 で返した分を消す
    POST /send     : {"channel_id": "...", "content": "..."} を投稿
                     (要 X-Relay-Key)
- 環境変数: DISCORD_BOT_TOKEN, RELAY_KEY, PORT (Render が自動設定)
"""

import asyncio
import os
from collections import OrderedDict

import discord
from aiohttp import web

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
RELAY_KEY = os.environ.get("RELAY_KEY", "")
PORT = int(os.environ.get("PORT", "10000"))

MAX_MESSAGES = 5000
CATCHUP_LIMIT = 50

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True

client = discord.Client(intents=intents)

# message_id -> dict (挿入順 = おおよそ時系列)
messages_by_id: "OrderedDict[str, dict]" = OrderedDict()
catchup_done = False


def store_message(message: discord.Message) -> None:
    if message.id in messages_by_id:
        return
    messages_by_id[str(message.id)] = {
        "id": str(message.id),
        "guild": message.guild.name if message.guild else "(DM)",
        "channel": message.channel.name if hasattr(message.channel, "name") else "(unknown)",
        "channel_id": str(message.channel.id),
        "author": str(message.author),
        "is_bot": message.author.bot,
        "content": message.content or "",
        "timestamp": message.created_at.isoformat(),
    }
    while len(messages_by_id) > MAX_MESSAGES:
        messages_by_id.popitem(last=False)


async def catchup() -> None:
    """起動時に各チャンネルの直近メッセージを取り込む (再接続時は再実行しない)。"""
    global catchup_done
    if catchup_done:
        return
    catchup_done = True
    for guild in client.guilds:
        for channel in guild.text_channels:
            try:
                async for msg in channel.history(limit=CATCHUP_LIMIT):
                    store_message(msg)
            except (discord.Forbidden, discord.HTTPException):
                continue


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    await catchup()


@client.event
async def on_message(message: discord.Message):
    store_message(message)


def authorized(request: web.Request) -> bool:
    return bool(RELAY_KEY) and request.headers.get("X-Relay-Key") == RELAY_KEY


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "bot": str(client.user) if client.user else None})


async def get_messages(request: web.Request) -> web.Response:
    if not authorized(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=403)
    items = list(messages_by_id.values())
    if request.query.get("clear") == "1":
        messages_by_id.clear()
    return web.json_response({"ok": True, "count": len(items), "messages": items})


async def send_message(request: web.Request) -> web.Response:
    if not authorized(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=403)
    try:
        data = await request.json()
        channel_id = int(data["channel_id"])
        content = str(data["content"])[:2000]
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"channel not found: {exc}"}, status=404)
    try:
        sent = await channel.send(content)
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"send failed: {exc}"}, status=502)
    return web.json_response({"ok": True, "message_id": str(sent.id)})


async def main() -> None:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/messages", get_messages)
    app.router.add_post("/send", send_message)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"HTTP server listening on {PORT}")
    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
