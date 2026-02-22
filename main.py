import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import aiohttp
from datetime import datetime
import asyncio
import json
load_dotenv()

# env keys
token = os.getenv("DISCORD_TOKEN")
SERVER_ID = os.getenv("MINEHUT_SERVERID1")  # make sure .env has this exact name
MINEHUT_TOKEN = os.getenv("MINEHUT_TOKEN")  # full "Bearer ey..." string

STATUS_CHANNEL_ID = 1475054751581343915  # your target channel id
STATUS_FILE = "status_message.json"       # stores message id so it persists across restarts
_status_lock = asyncio.Lock()
# basic checks early so you see clear errors
def _load_status_msg_id():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return int(d.get("message_id"))
    except Exception:
        return None

def _save_status_msg_id(msg_id: int):
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"message_id": int(msg_id)}, f)

def _format_status_embed(state: str, last_action: str = None, by: str = None):
    """
    Returns (content, embed) where:
    - content is short text (here None)
    - embed is a Discord Embed showing server status
    state: "running", "stopped", "starting", "stopping", "unknown"
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # emoji + title
    emoji = {
        "running": "🟢",
        "stopped": "🔴",
        "starting": "🟡",
        "stopping": "🟠",
        "unknown": "⚪"
    }.get(state.lower(), "⚪")

    title = f"{emoji} Server Status: {state.capitalize()}"

    desc = f"Last updated: {now}"
    if last_action:
        desc += f"\nLast action: {last_action}"
    if by:
        desc += f" by {by}"

    embed = discord.Embed(title=title, description=desc, color={
        "running": discord.Color.green(),
        "stopped": discord.Color.red(),
        "starting": discord.Color.gold(),
        "stopping": discord.Color.orange(),
        "unknown": discord.Color.light_grey()
    }.get(state.lower(), discord.Color.light_grey()))

    embed.set_footer(text="Use !serverstatus to refresh the status.")

    return None, embed

async def _ensure_status_message():
    """
    ensure a single status message exists in channel, return discord.Message
    """
    channel = bot.get_channel(STATUS_CHANNEL_ID) or await bot.fetch_channel(STATUS_CHANNEL_ID)
    if channel is None:
        raise RuntimeError(f"cannot find channel {STATUS_CHANNEL_ID}")

    msg_id = _load_status_msg_id()
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            return msg
        except Exception:
            # message deleted or invalid -> recreate
            pass

    # create a new message and save id
    content, embed = _format_status_embed("unknown", "initializing", bot.user.name)
    msg = await channel.send(content=content, embed=embed)
    _save_status_msg_id(msg.id)
    return msg

async def update_status_message(state: str, last_action: str=None, by: str=None):
    """
    call this to update the status message.
    state: running/stopped/starting/stopping/unknown
    last_action: e.g. "start requested"
    by: username or mention who triggered it
    """
    async with _status_lock:
        try:
            msg = await _ensure_status_message()
            content, embed = _format_status_embed(state, last_action, by)
            # edit embed (and keep content None so it looks clean)
            await msg.edit(content=content, embed=embed)
        except Exception as e:
            print("update_status_message error:", repr(e))

# helper to call after commands: tries to get real state then update message
async def refresh_and_update(trigger_by: str=None, action_hint: str=None, immediate_state: str=None, wait_seconds: int=3):
    """
    - immediate_state: optional quick state to show instantly ("starting","stopping")
    - wait_seconds: how long to wait then re-check real state
    """
    if immediate_state:
        await update_status_message(immediate_state, last_action=action_hint, by=trigger_by)
    # wait then check real state
    try:
        await asyncio.sleep(wait_seconds)
        real = await get_minehut_status()
        if real is True:
            s = "running"
        elif real is False:
            s = "stopped"
        else:
            s = "unknown"
        await update_status_message(s, last_action=action_hint, by=trigger_by)
    except Exception as e:
        print("refresh_and_update error:", repr(e))
        await update_status_message("unknown", last_action="refresh failed", by=trigger_by)


# Override with transition-aware polling so start/stop states do not get flipped by short API lag.
async def refresh_and_update(
    trigger_by: str = None,
    action_hint: str = None,
    immediate_state: str = None,
    wait_seconds: int = 3,
    expected_final: str = None,
    timeout_seconds: int = None,
    poll_interval: int = 3,
):
    if immediate_state:
        await update_status_message(immediate_state, last_action=action_hint, by=trigger_by)

    if poll_interval < 1:
        poll_interval = 1
    if timeout_seconds is None:
        timeout_seconds = wait_seconds
    if timeout_seconds < 0:
        timeout_seconds = 0

    try:
        elapsed = 0
        expected = expected_final.lower() if expected_final else None
        last_seen = None

        while True:
            if elapsed > 0:
                await asyncio.sleep(poll_interval)
            elif wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            real = await get_minehut_status()
            if real is True:
                current = "running"
            elif real is False:
                current = "stopped"
            else:
                current = "unknown"

            last_seen = current

            if expected is None:
                await update_status_message(current, last_action=action_hint, by=trigger_by)
                return

            if current == expected:
                await update_status_message(current, last_action=action_hint, by=trigger_by)
                return

            elapsed += poll_interval
            if elapsed >= timeout_seconds:
                if last_seen in {"running", "stopped"}:
                    await update_status_message(last_seen, last_action=action_hint, by=trigger_by)
                elif immediate_state:
                    await update_status_message(immediate_state, last_action=action_hint, by=trigger_by)
                else:
                    await update_status_message("unknown", last_action=action_hint, by=trigger_by)
                return
    except Exception as e:
        print("refresh_and_update error:", repr(e))
        await update_status_message("unknown", last_action="refresh failed", by=trigger_by)
if not token:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")
if not SERVER_ID:
    raise RuntimeError("MINEHUT_SERVERID1 is missing from .env")
if not MINEHUT_TOKEN:
    raise RuntimeError("MINEHUT_TOKEN is missing from .env (put full 'Bearer ...')")

# ensure bearer prefix
if not MINEHUT_TOKEN.startswith("Bearer "):
    MINEHUT_TOKEN = "Bearer " + MINEHUT_TOKEN

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
async def get_minehut_status():
    url = f"https://api.minehut.com/server/{SERVER_ID}"
    headers = {
        "authorization": MINEHUT_TOKEN,
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://app.minehut.com",
        "referer": "https://app.minehut.com/",
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url, headers=headers) as resp:
                text = await resp.text()
                print("get_minehut_status response:", resp.status, text[:500])  # print first 500 chars
                if resp.status != 200:
                    return None
                data = await resp.json()
                return bool(data.get("server", {}).get("online"))
    except Exception as e:
        print("get_minehut_status exception:", repr(e))
        return None
async def minehut_power(action):
    url = f"https://api.minehut.com/server/{SERVER_ID}/{action}"
    headers = {
    "authorization": MINEHUT_TOKEN,
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://app.minehut.com",
    "referer": "https://app.minehut.com/",
    "user-agent": "Mozilla/5.0",
    "x-profile-id": "fcb6722f-b1ec-4b87-b5ed-cf8a0cab8c31",
    "x-session-id": "97e35639-7207-4e54-ad25-c14c17488292"
    }

    # debug
    print(">>> minehut url:", url)
    print(">>> headers keys/types:", [(k, type(k).__name__) for k in headers.keys()])

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers) as resp:
                text = await resp.text()
                print("minehut response:", resp.status, text)
                return resp.status
    except Exception as e:
        print("error sending request to minehut:", repr(e))
        return None

@bot.event
async def on_ready():
    print(f"bot is ready and running, {bot.user.name}")

@bot.command()
async def hello(ctx):
    await ctx.send("nobody gonna say hello back to you broksi")

@bot.command()
async def assign(ctx):
    role = discord.utils.get(ctx.guild.roles, name="has no dih")
    if role:
        await ctx.author.remove_roles(role)
        await ctx.send(f"{ctx.author.mention} got his dih back :)")
    else:
        await ctx.send("role doesn't exist bro")


# minehut control commands
@bot.command()
@commands.has_role("Server Admin")
async def startserver(ctx):
    await ctx.reply("Starting server...")
    # immediate feedback + try to start
    res = await minehut_power("start_service")
    if res == 200:
        await ctx.reply("The server is starting. Please wait a few seconds to join.")
        await refresh_and_update(
            trigger_by=str(ctx.author),
            action_hint="Start requested",
            immediate_state="starting",
            wait_seconds=3,
            expected_final="running",
            timeout_seconds=45,
            poll_interval=5,
        )
    elif res is None:
        await ctx.reply("Error contacting Minehut. Please check the bot console.")
        await refresh_and_update(trigger_by=str(ctx.author), action_hint="Start failed", immediate_state="Unknown")
    else:
        await ctx.reply(f"Failed to start the server (Status {res}).")
        await refresh_and_update(trigger_by=str(ctx.author), action_hint=f"Start failed ({res})", immediate_state="Unknown")
@bot.command()
@commands.has_role("Server Admin")
async def goodboy(ctx):
    await ctx.reply("thanks daddy :3")
@bot.command()
@commands.has_role("Server Admin")
async def stopserver(ctx):
    await ctx.reply("Stopping server...")
    res = await minehut_power("shutdown")
    if res == 200:
        await ctx.reply("The server has been stopped.")
        await refresh_and_update(
            trigger_by=str(ctx.author),
            action_hint="Shutdown requested",
            immediate_state="stopping",
            wait_seconds=3,
            expected_final="stopped",
            timeout_seconds=45,
            poll_interval=5,
        )
    elif res is None:
        await ctx.reply("Error contacting Minehut. Please check the bot console.")
        await refresh_and_update(trigger_by=str(ctx.author), action_hint="Shutdown failed", immediate_state="Unknown")
    else:
        await ctx.reply(f"Failed to stop the server (Status {res}).")
        await refresh_and_update(trigger_by=str(ctx.author), action_hint=f"Stop failed ({res})", immediate_state="Unknown")

@bot.command()
async def serverstatus(ctx):
    await ctx.reply("Checking server status...")
    result = await get_minehut_status()
    if result is True:
        await ctx.reply("🟢 The server is currently running.")
        await refresh_and_update(trigger_by=str(ctx.author), action_hint="Server status checked", immediate_state="Running", wait_seconds=0)
    elif result is False:
        await ctx.reply("🔴 The server is currently stopped.")
        await refresh_and_update(trigger_by=str(ctx.author), action_hint="Server status checked", immediate_state="Stopped", wait_seconds=0)
    else:
        await ctx.reply("⚠️ Could not retrieve the server status. Please check the bot console.")
        await refresh_and_update(trigger_by=str(ctx.author), action_hint="Server status checked", immediate_state="Unknown", wait_seconds=0)
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.reply("🚫 You need the **Senior Admin** role to use this command.")
    else:
        raise error
bot.run(token, log_handler=handler, log_level=logging.DEBUG)
