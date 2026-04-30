import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import logging
import aiohttp

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
NOTI_FILE    = os.path.join(SCRIPT_DIR, "noti.json")
CONFIG_FILE  = os.path.join(SCRIPT_DIR, "config.json")

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  STORAGE
# ─────────────────────────────────────────────

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(data: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_noti() -> dict:
    if not os.path.exists(NOTI_FILE):
        return {"youtube": {}, "twitch": {}}
    with open(NOTI_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("youtube", {})
    data.setdefault("twitch", {})
    return data

def save_noti(data: dict) -> None:
    with open(NOTI_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ─────────────────────────────────────────────
#  YOUTUBE
# ─────────────────────────────────────────────

async def yt_resolve_channel(session: aiohttp.ClientSession, api_key: str, query: str) -> tuple[str, str] | None:
    if "youtube.com" in query:
        if "/@" in query:
            query = "@" + query.split("/@")[1].split("/")[0].split("?")[0]
        elif "/channel/" in query:
            query = query.split("/channel/")[1].split("/")[0].split("?")[0]

    params = {"part": "snippet", "key": api_key}
    if query.startswith("UC"):
        params["id"] = query
    else:
        params["forHandle"] = query if query.startswith("@") else f"@{query}"

    async with session.get("https://www.googleapis.com/youtube/v3/channels", params=params) as r:
        if r.status != 200:
            return None
        data = await r.json()

    items = data.get("items", [])
    if not items:
        return None
    return items[0]["id"], items[0]["snippet"]["title"]


async def yt_fetch_recent(
    session: aiohttp.ClientSession, api_key: str, channel_id: str, max_results: int = 1
) -> list[tuple[str, str]]:
    uploads_id = "UU" + channel_id[2:]
    params = {"part": "snippet", "playlistId": uploads_id, "maxResults": max_results, "key": api_key}
    async with session.get("https://www.googleapis.com/youtube/v3/playlistItems", params=params) as r:
        if r.status != 200:
            log.warning(f"YT playlistItems {r.status} for {channel_id}")
            return []
        data = await r.json()

    results = []
    for item in data.get("items", []):
        snippet  = item["snippet"]
        video_id = snippet["resourceId"]["videoId"]
        title    = snippet["title"]
        if title not in ("Private video", "Deleted video"):
            results.append((video_id, title))
    return results


async def yt_find_live(
    session: aiohttp.ClientSession, api_key: str, video_ids: list[str]
) -> tuple[str, str] | None:
    if not video_ids:
        return None
    params = {"part": "snippet", "id": ",".join(video_ids), "key": api_key}
    async with session.get("https://www.googleapis.com/youtube/v3/videos", params=params) as r:
        if r.status != 200:
            return None
        data = await r.json()

    for video in data.get("items", []):
        if video["snippet"].get("liveBroadcastContent") == "live":
            return video["id"], video["snippet"]["title"]
    return None

# ─────────────────────────────────────────────
#  TWITCH
# ─────────────────────────────────────────────

class TwitchClient:
    BASE = "https://api.twitch.tv/helix"

    def __init__(self, client_id: str, client_secret: str):
        self.client_id     = client_id
        self.client_secret = client_secret
        self._token: str | None = None

    async def _refresh(self, session: aiohttp.ClientSession) -> bool:
        async with session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
                "grant_type":    "client_credentials",
            },
        ) as r:
            if r.status != 200:
                return False
            self._token = (await r.json()).get("access_token")
            return bool(self._token)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Client-Id": self.client_id}

    async def _get(self, session: aiohttp.ClientSession, endpoint: str, params: dict) -> dict | None:
        if not self._token and not await self._refresh(session):
            return None
        async with session.get(f"{self.BASE}/{endpoint}", params=params, headers=self._headers()) as r:
            if r.status == 401:
                self._token = None
                if not await self._refresh(session):
                    return None
                async with session.get(f"{self.BASE}/{endpoint}", params=params, headers=self._headers()) as r2:
                    return await r2.json() if r2.status == 200 else None
            return await r.json() if r.status == 200 else None

    async def get_user(self, session: aiohttp.ClientSession, login: str) -> dict | None:
        data  = await self._get(session, "users", {"login": login})
        items = (data or {}).get("data", [])
        return items[0] if items else None

    async def get_stream(self, session: aiohttp.ClientSession, user_login: str) -> dict | None:
        data  = await self._get(session, "streams", {"user_login": user_login})
        items = (data or {}).get("data", [])
        return items[0] if items else None

# ─────────────────────────────────────────────
#  EMBEDS
# ─────────────────────────────────────────────

def yt_video_embed(name: str, ch_id: str, video_id: str, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        url=f"https://www.youtube.com/watch?v={video_id}",
        color=0xFF0000,
    )
    embed.set_author(name=name, url=f"https://www.youtube.com/channel/{ch_id}")
    embed.set_image(url=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg")
    embed.set_footer(text="YouTube • New Video")
    return embed


def yt_stream_embed(name: str, ch_id: str, video_id: str, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        url=f"https://www.youtube.com/watch?v={video_id}",
        color=0xFF0000,
    )
    embed.set_author(name=name, url=f"https://www.youtube.com/channel/{ch_id}")
    embed.set_image(url=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg")
    embed.set_footer(text="YouTube • Live")
    return embed


def twitch_embed(login: str, name: str, title: str, game: str, thumbnail_url: str, viewers: int) -> discord.Embed:
    url   = f"https://www.twitch.tv/{login}"
    embed = discord.Embed(title=title or "Untitled stream", url=url, color=0x9146FF)
    embed.set_author(name=name, url=url)
    if game:
        embed.add_field(name="Category", value=game, inline=True)
    if viewers:
        embed.add_field(name="Viewers", value=f"{viewers:,}", inline=True)
    if thumbnail_url:
        embed.set_image(url=thumbnail_url.replace("{width}", "640").replace("{height}", "360"))
    embed.set_footer(text="Twitch • Live")
    return embed

# ─────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────

class NotiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot     = bot
        self._yt_key = os.getenv("YOUTUBE_API_KEY")
        twitch_id    = os.getenv("TWITCH_CLIENT_ID")
        twitch_sec   = os.getenv("TWITCH_CLIENT_SECRET")
        self._twitch = TwitchClient(twitch_id, twitch_sec) if twitch_id and twitch_sec else None
        self._session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        if self._yt_key:
            self.check_youtube.start()
        else:
            log.warning("YOUTUBE_API_KEY not set — YouTube notifications disabled.")
        if self._twitch:
            self.check_twitch.start()
        else:
            log.warning("TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET not set — Twitch notifications disabled.")

    async def cog_unload(self):
        self.check_youtube.cancel()
        self.check_twitch.cancel()
        if self._session:
            await self._session.close()

    # ── YouTube poll (every 10 min, 1–2 quota units/channel) ─────
    # Videos: 1 unit (playlistItems). Streams: +1 unit (videos.list batch).

    @tasks.loop(minutes=10)
    async def check_youtube(self):
        cfg          = load_config()
        video_ch_id  = cfg.get("noti_video_channel_id")
        stream_ch_id = cfg.get("noti_stream_channel_id")
        data         = load_noti()
        changed      = False

        for ch_id, info in data["youtube"].items():
            has_video  = bool(video_ch_id)
            has_stream = bool(stream_ch_id)

            recent = await yt_fetch_recent(self._session, self._yt_key, ch_id, max_results=5 if has_stream else 1)
            if not recent:
                continue

            if has_video:
                video_id, title = recent[0]
                if video_id != info.get("last_video_id"):
                    old = info.get("last_video_id")
                    info["last_video_id"] = video_id
                    changed = True
                    if old is not None:
                        channel = self.bot.get_channel(video_ch_id)
                        if channel:
                            try:
                                await channel.send(embed=yt_video_embed(info["name"], ch_id, video_id, title))
                                log.info(f"YT video notified: {title!r} ({ch_id})")
                            except Exception as e:
                                log.error(f"YT video send failed ({ch_id}): {e}")

            if has_stream:
                live      = await yt_find_live(self._session, self._yt_key, [vid for vid, _ in recent])
                stream_id = live[0] if live else None
                if stream_id != info.get("last_stream_id"):
                    info["last_stream_id"] = stream_id
                    changed = True
                    if stream_id:
                        channel = self.bot.get_channel(stream_ch_id)
                        if channel:
                            try:
                                await channel.send(embed=yt_stream_embed(info["name"], ch_id, stream_id, live[1]))
                                log.info(f"YT stream notified: {live[1]!r} ({ch_id})")
                            except Exception as e:
                                log.error(f"YT stream send failed ({ch_id}): {e}")

        if changed:
            save_noti(data)

    @check_youtube.before_loop
    async def _before_youtube(self):
        await self.bot.wait_until_ready()

    # ── Twitch poll (every 2 min) ─────────────

    @tasks.loop(minutes=2)
    async def check_twitch(self):
        stream_ch_id = load_config().get("noti_stream_channel_id")
        data         = load_noti()
        changed      = False

        if not stream_ch_id:
            return

        for login, info in data["twitch"].items():
            stream    = await self._twitch.get_stream(self._session, login)
            stream_id = stream["id"] if stream else None
            if stream_id == info.get("stream_id"):
                continue
            info["stream_id"] = stream_id
            changed = True
            if not stream:
                continue
            channel = self.bot.get_channel(stream_ch_id)
            if not channel:
                continue
            try:
                await channel.send(embed=twitch_embed(
                    login=login,
                    name=info["name"],
                    title=stream.get("title", ""),
                    game=stream.get("game_name", ""),
                    thumbnail_url=stream.get("thumbnail_url", ""),
                    viewers=stream.get("viewer_count", 0),
                ))
                log.info(f"Twitch stream notified: {login}")
            except Exception as e:
                log.error(f"Twitch send failed ({login}): {e}")

        if changed:
            save_noti(data)

    @check_twitch.before_loop
    async def _before_twitch(self):
        await self.bot.wait_until_ready()

    # ── Commands ──────────────────────────────

    @app_commands.command(name="noti-video", description="Set the Discord channel for all video notifications")
    @app_commands.describe(channel="Discord channel where new video notifications will be sent")
    @app_commands.default_permissions(administrator=True)
    async def noti_video(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = load_config()
        cfg["noti_video_channel_id"] = channel.id
        save_config(cfg)
        await interaction.response.send_message(f"✅ Video notifications -> {channel.mention}.", ephemeral=True)
        log.info(f"Noti video channel set to #{channel.name} ({channel.id}) by {interaction.user}.")

    @app_commands.command(name="noti-stream", description="Set the Discord channel for all stream notifications")
    @app_commands.describe(channel="Discord channel where live stream notifications will be sent")
    @app_commands.default_permissions(administrator=True)
    async def noti_stream(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = load_config()
        cfg["noti_stream_channel_id"] = channel.id
        save_config(cfg)
        await interaction.response.send_message(f"✅ Stream notifications -> {channel.mention}.", ephemeral=True)
        log.info(f"Noti stream channel set to #{channel.name} ({channel.id}) by {interaction.user}.")

    @app_commands.command(name="noti-youtube-add", description="Add a YouTube channel to monitor for new videos and/or live streams")
    @app_commands.describe(channel="YouTube channel URL, @handle, or channel ID (UCxxxxxx)")
    @app_commands.default_permissions(administrator=True)
    async def noti_youtube_add(self, interaction: discord.Interaction, channel: str):
        if not self._yt_key:
            await interaction.response.send_message("❌ `YOUTUBE_API_KEY` not set in .env.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        resolved = await yt_resolve_channel(self._session, self._yt_key, channel)
        if not resolved:
            await interaction.followup.send("❌ YouTube channel not found. Use the channel URL or `@handle`.")
            return

        ch_id, name = resolved
        data        = load_noti()
        existing    = data["youtube"].get(ch_id, {})

        last_video_id  = existing.get("last_video_id")
        last_stream_id = existing.get("last_stream_id")
        if last_video_id is None or "last_stream_id" not in existing:
            recent = await yt_fetch_recent(self._session, self._yt_key, ch_id, max_results=5)
            if recent and last_video_id is None:
                last_video_id = recent[0][0]
            if "last_stream_id" not in existing:
                live = await yt_find_live(self._session, self._yt_key, [vid for vid, _ in recent])
                last_stream_id = live[0] if live else None

        data["youtube"][ch_id] = {
            "name":          name,
            "last_video_id": last_video_id,
            "last_stream_id": last_stream_id,
        }
        save_noti(data)

        await interaction.followup.send(f"✅ **{name}** added.")
        log.info(f"YT channel added: {name} ({ch_id}) by {interaction.user}.")

    @app_commands.command(name="noti-twitch-add", description="Add a Twitch streamer to monitor for live streams")
    @app_commands.describe(username="Twitch username")
    @app_commands.default_permissions(administrator=True)
    async def noti_twitch_add(self, interaction: discord.Interaction, username: str):
        if not self._twitch:
            await interaction.response.send_message(
                "❌ `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` not set in .env.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        login = username.lstrip("@").lower()
        user  = await self._twitch.get_user(self._session, login)
        if not user:
            await interaction.followup.send(f"❌ Twitch user `{login}` not found.")
            return

        data     = load_noti()
        existing = data["twitch"].get(login, {})

        stream    = await self._twitch.get_stream(self._session, login)
        stream_id = stream["id"] if stream else existing.get("stream_id")

        data["twitch"][login] = {
            "name":      user["display_name"],
            "stream_id": stream_id,
        }
        save_noti(data)

        await interaction.followup.send(f"✅ **{user['display_name']}** added.")
        log.info(f"Twitch added: {user['display_name']} ({login}) by {interaction.user}.")

    @app_commands.command(name="noti-youtube-remove", description="Remove a monitored YouTube channel")
    @app_commands.describe(channel="YouTube channel (start typing to search)")
    @app_commands.default_permissions(administrator=True)
    async def noti_youtube_remove(self, interaction: discord.Interaction, channel: str):
        data = load_noti()
        info = data["youtube"].pop(channel, None)
        if not info:
            await interaction.response.send_message("❌ YouTube channel not in watchlist.", ephemeral=True)
            return
        save_noti(data)
        await interaction.response.send_message(f"✅ **{info['name']}** removed.", ephemeral=True)
        log.info(f"YT channel removed: {info['name']} ({channel}) by {interaction.user}.")

    @noti_youtube_remove.autocomplete("channel")
    async def _yt_remove_ac(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        data = load_noti()
        return [
            app_commands.Choice(name=info["name"], value=ch_id)
            for ch_id, info in data["youtube"].items()
            if current.lower() in info["name"].lower() or current in ch_id
        ][:25]

    @app_commands.command(name="noti-twitch-remove", description="Remove a monitored Twitch streamer")
    @app_commands.describe(username="Twitch username (start typing to search)")
    @app_commands.default_permissions(administrator=True)
    async def noti_twitch_remove(self, interaction: discord.Interaction, username: str):
        data = load_noti()
        info = data["twitch"].pop(username, None)
        if not info:
            await interaction.response.send_message("❌ Twitch streamer not in watchlist.", ephemeral=True)
            return
        save_noti(data)
        await interaction.response.send_message(f"✅ **{info['name']}** removed.", ephemeral=True)
        log.info(f"Twitch removed: {info['name']} ({username}) by {interaction.user}.")

    @noti_twitch_remove.autocomplete("username")
    async def _twitch_remove_ac(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        data = load_noti()
        return [
            app_commands.Choice(name=info["name"], value=login)
            for login, info in data["twitch"].items()
            if current.lower() in info["name"].lower() or current.lower() in login
        ][:25]

    @app_commands.command(name="noti-list", description="List all monitored YouTube channels and Twitch streamers")
    @app_commands.default_permissions(administrator=True)
    async def noti_list(self, interaction: discord.Interaction):
        cfg  = load_config()
        data = load_noti()

        video_ch  = f"<#{cfg['noti_video_channel_id']}>"  if cfg.get("noti_video_channel_id")  else "—"
        stream_ch = f"<#{cfg['noti_stream_channel_id']}>" if cfg.get("noti_stream_channel_id") else "—"

        yt_lines = [f"**{info['name']}** `{ch_id}`" for ch_id, info in data["youtube"].items()]
        tw_lines = [f"**{info['name']}** `{login}`"  for login, info in data["twitch"].items()]

        embed = discord.Embed(title="Notification Watchlist", color=discord.Color.blurple())
        embed.add_field(name="Video channel",  value=video_ch,  inline=True)
        embed.add_field(name="Stream channel", value=stream_ch, inline=True)
        embed.add_field(name=f"YouTube ({len(yt_lines)})", value="\n".join(yt_lines) or "—", inline=False)
        embed.add_field(name=f"Twitch ({len(tw_lines)})",  value="\n".join(tw_lines)  or "—", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(NotiCog(bot))
