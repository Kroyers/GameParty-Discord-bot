import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
from lang import detect_lang
import json
import os
import logging
import time
import aiohttp

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
NOTI_FILE    = os.path.join(SCRIPT_DIR, "noti.json")
CONFIG_FILE  = os.path.join(SCRIPT_DIR, "config.json")
LOCALES_DIR  = os.path.join(SCRIPT_DIR, "locales")

log = logging.getLogger(__name__)

def _load_locales() -> dict:
    locales = {}
    for fname in os.listdir(LOCALES_DIR):
        if fname.endswith(".json"):
            with open(os.path.join(LOCALES_DIR, fname), "r", encoding="utf-8") as f:
                locales[fname[:-5]] = json.load(f)
    return locales

LOCALES = _load_locales()

def t(lang: str, key: str, **kwargs) -> str:
    text = LOCALES.get(lang, LOCALES.get("en", {})).get(key, key)
    return text.format(**kwargs) if kwargs else text

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

def save_noti_section(section: str, section_data: dict) -> None:
    """Reload file and update only one section to avoid overwriting concurrent task changes."""
    data = load_noti()
    data[section] = section_data
    save_noti(data)

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

    log.info(f"YouTube API: channels lookup for {query!r}")
    async with session.get("https://www.googleapis.com/youtube/v3/channels", params=params) as r:
        if r.status != 200:
            log.warning(f"YouTube API: channels {r.status} for {query!r}")
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
    log.info(f"YouTube API: playlistItems for {channel_id}")
    async with session.get("https://www.googleapis.com/youtube/v3/playlistItems", params=params) as r:
        if r.status != 200:
            log.warning(f"YouTube API: playlistItems {r.status} for {channel_id}")
            return []
        data = await r.json()

    results = []
    for item in data.get("items", []):
        snippet  = item["snippet"]
        video_id = snippet["resourceId"]["videoId"]
        title    = snippet["title"]
        if title not in ("Private video", "Deleted video"):
            thumbs = snippet.get("thumbnails", {})
            thumb  = next(
                (thumbs[q]["url"] for q in ("maxres", "standard", "high", "medium", "default") if q in thumbs),
                f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
            )
            results.append((video_id, title, thumb))
    return results


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
        log.info("Twitch API: refreshing OAuth token")
        async with session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
                "grant_type":    "client_credentials",
            },
        ) as r:
            if r.status != 200:
                log.warning(f"Twitch API: token refresh failed ({r.status})")
                return False
            self._token = (await r.json()).get("access_token")
            log.info("Twitch API: token refreshed")
            return bool(self._token)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Client-Id": self.client_id}

    async def _get(self, session: aiohttp.ClientSession, endpoint: str, params: dict) -> dict | None:
        if not self._token and not await self._refresh(session):
            return None
        log.info(f"Twitch API: GET {endpoint} {params}")
        async with session.get(f"{self.BASE}/{endpoint}", params=params, headers=self._headers()) as r:
            if r.status == 401:
                log.warning("Twitch API: 401, retrying after token refresh")
                self._token = None
                if not await self._refresh(session):
                    return None
                async with session.get(f"{self.BASE}/{endpoint}", params=params, headers=self._headers()) as r2:
                    if r2.status != 200:
                        log.warning(f"Twitch API: {endpoint} {r2.status} after retry")
                    return await r2.json() if r2.status == 200 else None
            if r.status != 200:
                log.warning(f"Twitch API: {endpoint} {r.status}")
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
#  HELPERS
# ─────────────────────────────────────────────

def _format_duration(started_at: str) -> str:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        total = int((datetime.now(timezone.utc) - start).total_seconds())
        h, rem = divmod(total, 3600)
        m = rem // 60
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return ""

def _role_mention(role_id: int, guild: discord.Guild) -> str:
    if role_id == guild.id:
        return "@everyone"
    role = guild.get_role(role_id)
    return role.mention if role else f"<@&{role_id}>"

# ─────────────────────────────────────────────
#  EMBEDS
# ─────────────────────────────────────────────

def yt_video_embed(name: str, ch_id: str, video_id: str, title: str, thumb: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        url=f"https://www.youtube.com/watch?v={video_id}",
        color=0xFF0000,
    )
    embed.set_author(name=name, url=f"https://www.youtube.com/channel/{ch_id}")
    embed.set_image(url=thumb)
    embed.set_footer(text="YouTube • New Video")
    return embed



def twitch_embed(login: str, name: str, title: str, game: str, thumbnail_url: str, viewers: int) -> discord.Embed:
    url   = f"https://www.twitch.tv/{login}"
    embed = discord.Embed(title=title or "Untitled stream", url=url, color=0x9146FF)
    embed.set_author(name=f"{name} is now live! 🔴", url=url)
    if game:
        embed.add_field(name="Category", value=game, inline=True)
    if viewers:
        embed.add_field(name="Viewers", value=f"{viewers:,}", inline=True)
    if thumbnail_url:
        thumb = thumbnail_url.replace("{width}", "640").replace("{height}", "360")
        embed.set_image(url=f"{thumb}?t={int(time.time())}")
    embed.set_footer(text="Twitch • Live")
    return embed


def twitch_ended_embed(login: str, name: str, title: str, avatar_url: str | None = None, duration: str = "") -> discord.Embed:
    url   = f"https://www.twitch.tv/{login}"
    embed = discord.Embed(title=title or "Untitled stream", url=url, color=0x808080)
    embed.set_author(name=f"{name} is now offline", url=url)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    footer = "Twitch • Stream ended"
    if duration:
        footer += f" • {duration}"
    embed.set_footer(text=footer)
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
        yt_role_id   = cfg.get("noti_youtube_role_id")
        data         = load_noti()
        changed      = False

        for ch_id, info in data["youtube"].items():
            if not video_ch_id:
                continue

            recent = await yt_fetch_recent(self._session, self._yt_key, ch_id, max_results=1)
            if not recent:
                continue

            video_id, title, thumb = recent[0]
            if video_id != info.get("last_video_id"):
                old = info.get("last_video_id")
                info["last_video_id"] = video_id
                changed = True
                if old is not None:
                    channel = self.bot.get_channel(video_ch_id)
                    if channel:
                        try:
                            kwargs = {"embed": yt_video_embed(info["name"], ch_id, video_id, title, thumb)}
                            if yt_role_id:
                                kwargs["content"] = _role_mention(yt_role_id, channel.guild)
                                kwargs["allowed_mentions"] = discord.AllowedMentions(everyone=True, roles=True)
                            await channel.send(**kwargs)
                            log.info(f"YT video notified: {title!r} ({ch_id})")
                        except Exception as e:
                            log.error(f"YT video send failed ({ch_id}): {e}")

        if changed:
            save_noti_section("youtube", data["youtube"])

    @check_youtube.before_loop
    async def _before_youtube(self):
        await self.bot.wait_until_ready()

    # ── Twitch poll (every 2 min) ─────────────

    @tasks.loop(minutes=5)
    async def check_twitch(self):
        cfg          = load_config()
        stream_ch_id = cfg.get("noti_stream_channel_id")
        tw_role_id   = cfg.get("noti_twitch_role_id")
        data         = load_noti()
        changed      = False

        if not stream_ch_id:
            return

        for login, info in data["twitch"].items():
            stream    = await self._twitch.get_stream(self._session, login)
            stream_id = stream["id"] if stream else None
            if stream_id == info.get("stream_id"):
                if stream and stream.get("thumbnail_url"):
                    info["stream_thumb"] = stream["thumbnail_url"]
                    changed = True
                    msg_id = info.get("msg_id")
                    if msg_id:
                        channel = self.bot.get_channel(stream_ch_id)
                        if channel:
                            try:
                                msg = await channel.fetch_message(msg_id)
                                await msg.edit(embed=twitch_embed(
                                    login=login,
                                    name=info["name"],
                                    title=stream.get("title", ""),
                                    game=stream.get("game_name", ""),
                                    thumbnail_url=stream["thumbnail_url"],
                                    viewers=stream.get("viewer_count", 0),
                                ))
                            except Exception as e:
                                log.warning(f"Twitch thumb update failed ({login}): {e}")
                continue

            old_msg_id    = info.get("msg_id")
            old_title     = info.get("stream_title", "")
            old_started   = info.get("stream_started_at", "")
            old_thumb     = info.get("stream_thumb", "")
            info["stream_id"]         = stream_id
            info["msg_id"]            = None
            info["stream_title"]      = stream.get("title", "") if stream else old_title
            info["stream_started_at"] = stream.get("started_at", "") if stream else ""
            info["stream_thumb"]      = stream.get("thumbnail_url", "") if stream else info.get("stream_thumb", "")
            changed = True

            channel = self.bot.get_channel(stream_ch_id)
            if not channel:
                continue

            if stream:
                try:
                    kwargs = {
                        "embed": twitch_embed(
                            login=login,
                            name=info["name"],
                            title=stream.get("title", ""),
                            game=stream.get("game_name", ""),
                            thumbnail_url=stream.get("thumbnail_url", ""),
                            viewers=stream.get("viewer_count", 0),
                        ),
                    }
                    if tw_role_id:
                        kwargs["content"] = _role_mention(tw_role_id, channel.guild)
                        kwargs["allowed_mentions"] = discord.AllowedMentions(everyone=True, roles=True)
                    msg = await channel.send(**kwargs)
                    info["msg_id"] = msg.id
                    log.info(f"Twitch stream notified: {login}")
                except Exception as e:
                    log.error(f"Twitch send failed ({login}): {e}")
            elif old_msg_id:
                try:
                    duration = _format_duration(old_started) if old_started else ""
                    msg = await channel.fetch_message(old_msg_id)
                    await msg.edit(
                        content=None,
                        embed=twitch_ended_embed(login, info["name"], old_title, info.get("avatar_url"), duration),
                    )
                    log.info(f"Twitch stream ended: {login}")
                except Exception as e:
                    log.error(f"Twitch edit failed ({login}): {e}")

        if changed:
            save_noti_section("twitch", data["twitch"])

    @check_twitch.before_loop
    async def _before_twitch(self):
        await self.bot.wait_until_ready()

    # ── Commands ──────────────────────────────

    @app_commands.command(name="noti-video", description=app_commands.locale_str("Set the Discord channel for all video notifications", key="cmd_noti_video"))
    @app_commands.describe(channel=app_commands.locale_str("Discord channel where new video notifications will be sent", key="cmd_noti_video_channel"))
    @app_commands.default_permissions(administrator=True)
    async def noti_video(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = load_config()
        cfg["noti_video_channel_id"] = channel.id
        save_config(cfg)
        await interaction.response.send_message(t(detect_lang(interaction), "noti_video_set", channel=channel.mention), ephemeral=True)
        log.info(f"Noti video channel set to #{channel.name} ({channel.id}) by {interaction.user}.")

    @app_commands.command(name="noti-stream", description=app_commands.locale_str("Set the Discord channel for all stream notifications", key="cmd_noti_stream"))
    @app_commands.describe(channel=app_commands.locale_str("Discord channel where live stream notifications will be sent", key="cmd_noti_stream_channel"))
    @app_commands.default_permissions(administrator=True)
    async def noti_stream(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = load_config()
        cfg["noti_stream_channel_id"] = channel.id
        save_config(cfg)
        await interaction.response.send_message(t(detect_lang(interaction), "noti_stream_set", channel=channel.mention), ephemeral=True)
        log.info(f"Noti stream channel set to #{channel.name} ({channel.id}) by {interaction.user}.")

    @app_commands.command(name="noti-youtube-role", description=app_commands.locale_str("Set the role to mention for YouTube notifications", key="cmd_noti_youtube_role"))
    @app_commands.describe(role=app_commands.locale_str("Role to mention for YouTube videos and streams", key="cmd_noti_youtube_role_param"))
    @app_commands.default_permissions(administrator=True)
    async def noti_role_youtube(self, interaction: discord.Interaction, role: discord.Role):
        cfg = load_config()
        cfg["noti_youtube_role_id"] = role.id
        save_config(cfg)
        mention = "@everyone" if role.is_default() else role.mention
        await interaction.response.send_message(t(detect_lang(interaction), "noti_youtube_role_set", role=mention), ephemeral=True)
        log.info(f"Noti YouTube role set to {role.name} ({role.id}) by {interaction.user}.")

    @app_commands.command(name="noti-twitch-role", description=app_commands.locale_str("Set the role to mention for Twitch notifications", key="cmd_noti_twitch_role"))
    @app_commands.describe(role=app_commands.locale_str("Role to mention for Twitch streams", key="cmd_noti_twitch_role_param"))
    @app_commands.default_permissions(administrator=True)
    async def noti_role_twitch(self, interaction: discord.Interaction, role: discord.Role):
        cfg = load_config()
        cfg["noti_twitch_role_id"] = role.id
        save_config(cfg)
        mention = "@everyone" if role.is_default() else role.mention
        await interaction.response.send_message(t(detect_lang(interaction), "noti_twitch_role_set", role=mention), ephemeral=True)
        log.info(f"Noti Twitch role set to {role.name} ({role.id}) by {interaction.user}.")

    @app_commands.command(name="noti-youtube-add", description=app_commands.locale_str("Add a YouTube channel to monitor", key="cmd_noti_youtube_add"))
    @app_commands.describe(channel=app_commands.locale_str("YouTube channel URL, @handle, or channel ID (UCxxxxxx)", key="cmd_noti_youtube_add_channel"))
    @app_commands.default_permissions(administrator=True)
    async def noti_youtube_add(self, interaction: discord.Interaction, channel: str):
        lang = detect_lang(interaction)
        if not self._yt_key:
            await interaction.response.send_message(t(lang, "noti_err_no_yt_key"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        resolved = await yt_resolve_channel(self._session, self._yt_key, channel)
        if not resolved:
            await interaction.followup.send(t(lang, "noti_err_yt_not_found"))
            return

        ch_id, name = resolved
        data        = load_noti()
        existing    = data["youtube"].get(ch_id, {})

        recent        = await yt_fetch_recent(self._session, self._yt_key, ch_id, max_results=1)
        last_video_id = recent[0][0] if recent else existing.get("last_video_id")

        data["youtube"][ch_id] = {
            "name":          name,
            "last_video_id": last_video_id,
        }
        save_noti(data)

        if recent:
            video_id, title, thumb = recent[0]
            cfg         = load_config()
            video_ch_id = cfg.get("noti_video_channel_id")
            yt_role_id  = cfg.get("noti_youtube_role_id")
            ch = self.bot.get_channel(video_ch_id)
            if ch:
                try:
                    kwargs = {"embed": yt_video_embed(name, ch_id, video_id, title, thumb)}
                    if yt_role_id:
                        kwargs["content"] = _role_mention(yt_role_id, ch.guild)
                        kwargs["allowed_mentions"] = discord.AllowedMentions(everyone=True, roles=True)
                    await ch.send(**kwargs)
                    log.info(f"YT latest video sent for {name} ({ch_id}).")
                except Exception as e:
                    log.error(f"YT latest video send failed ({ch_id}): {e}")

        await interaction.followup.send(t(lang, "noti_added", name=name))
        log.info(f"YT channel added: {name} ({ch_id}) by {interaction.user}.")

    @app_commands.command(name="noti-twitch-add", description=app_commands.locale_str("Add a Twitch streamer to monitor", key="cmd_noti_twitch_add"))
    @app_commands.describe(username=app_commands.locale_str("Twitch username", key="cmd_noti_twitch_add_username"))
    @app_commands.default_permissions(administrator=True)
    async def noti_twitch_add(self, interaction: discord.Interaction, username: str):
        lang = detect_lang(interaction)
        if not self._twitch:
            await interaction.response.send_message(t(lang, "noti_err_no_twitch"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        login = username.lstrip("@").lower()
        user  = await self._twitch.get_user(self._session, login)
        if not user:
            await interaction.followup.send(t(lang, "noti_err_twitch_not_found", login=login))
            return

        data     = load_noti()
        existing = data["twitch"].get(login, {})

        stream    = await self._twitch.get_stream(self._session, login)
        stream_id = stream["id"] if stream else existing.get("stream_id")

        data["twitch"][login] = {
            "name":       user["display_name"],
            "avatar_url": user.get("profile_image_url", ""),
            "stream_id":    stream_id,
            "msg_id":       existing.get("msg_id"),
            "stream_title": stream.get("title", "") if stream else existing.get("stream_title", ""),
            "stream_thumb": stream.get("thumbnail_url", "") if stream else existing.get("stream_thumb", ""),
            "stream_started_at": stream.get("started_at", "") if stream else "",
        }
        save_noti(data)

        if stream:
            cfg          = load_config()
            stream_ch_id = cfg.get("noti_stream_channel_id")
            tw_role_id   = cfg.get("noti_twitch_role_id")
            ch = self.bot.get_channel(stream_ch_id)
            if ch:
                try:
                    embed = twitch_embed(
                        login=login,
                        name=user["display_name"],
                        title=stream.get("title", ""),
                        game=stream.get("game_name", ""),
                        thumbnail_url=stream.get("thumbnail_url", ""),
                        viewers=stream.get("viewer_count", 0),
                    )
                    kwargs = {"embed": embed}
                    if tw_role_id:
                        kwargs["content"] = _role_mention(tw_role_id, ch.guild)
                        kwargs["allowed_mentions"] = discord.AllowedMentions(everyone=True, roles=True)
                    msg = await ch.send(**kwargs)
                    data["twitch"][login]["msg_id"] = msg.id
                    save_noti_section("twitch", data["twitch"])
                    log.info(f"Twitch live stream sent on add: {login}")
                except Exception as e:
                    log.error(f"Twitch live send on add failed ({login}): {e}")

        await interaction.followup.send(t(lang, "noti_added", name=user["display_name"]))
        log.info(f"Twitch added: {user['display_name']} ({login}) by {interaction.user}.")

    @app_commands.command(name="noti-youtube-remove", description=app_commands.locale_str("Remove a monitored YouTube channel", key="cmd_noti_youtube_remove"))
    @app_commands.describe(channel=app_commands.locale_str("YouTube channel (start typing to search)", key="cmd_noti_youtube_remove_ch"))
    @app_commands.default_permissions(administrator=True)
    async def noti_youtube_remove(self, interaction: discord.Interaction, channel: str):
        lang = detect_lang(interaction)
        data = load_noti()
        info = data["youtube"].pop(channel, None)
        if not info:
            await interaction.response.send_message(t(lang, "noti_err_yt_not_in_list"), ephemeral=True)
            return
        save_noti(data)
        await interaction.response.send_message(t(lang, "noti_removed", name=info["name"]), ephemeral=True)
        log.info(f"YT channel removed: {info['name']} ({channel}) by {interaction.user}.")

    @noti_youtube_remove.autocomplete("channel")
    async def _yt_remove_ac(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        data = load_noti()
        return [
            app_commands.Choice(name=info["name"], value=ch_id)
            for ch_id, info in data["youtube"].items()
            if current.lower() in info["name"].lower() or current in ch_id
        ][:25]

    @app_commands.command(name="noti-twitch-remove", description=app_commands.locale_str("Remove a monitored Twitch streamer", key="cmd_noti_twitch_remove"))
    @app_commands.describe(username=app_commands.locale_str("Twitch username (start typing to search)", key="cmd_noti_twitch_remove_user"))
    @app_commands.default_permissions(administrator=True)
    async def noti_twitch_remove(self, interaction: discord.Interaction, username: str):
        lang = detect_lang(interaction)
        data = load_noti()
        info = data["twitch"].pop(username, None)
        if not info:
            await interaction.response.send_message(t(lang, "noti_err_twitch_not_in_list"), ephemeral=True)
            return
        save_noti(data)
        await interaction.response.send_message(t(lang, "noti_removed", name=info["name"]), ephemeral=True)
        log.info(f"Twitch removed: {info['name']} ({username}) by {interaction.user}.")

    @noti_twitch_remove.autocomplete("username")
    async def _twitch_remove_ac(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        data = load_noti()
        return [
            app_commands.Choice(name=info["name"], value=login)
            for login, info in data["twitch"].items()
            if current.lower() in info["name"].lower() or current.lower() in login
        ][:25]

    @app_commands.command(name="noti-list", description=app_commands.locale_str("List all monitored YouTube channels and Twitch streamers", key="cmd_noti_list"))
    @app_commands.default_permissions(administrator=True)
    async def noti_list(self, interaction: discord.Interaction):
        lang = detect_lang(interaction)
        cfg  = load_config()
        data = load_noti()

        video_ch  = f"<#{cfg['noti_video_channel_id']}>"  if cfg.get("noti_video_channel_id")  else "—"
        stream_ch = f"<#{cfg['noti_stream_channel_id']}>" if cfg.get("noti_stream_channel_id") else "—"
        yt_role   = f"<@&{cfg['noti_youtube_role_id']}>"  if cfg.get("noti_youtube_role_id")   else "—"
        tw_role   = f"<@&{cfg['noti_twitch_role_id']}>"   if cfg.get("noti_twitch_role_id")    else "—"

        yt_lines = [f"**{info['name']}** `{ch_id}`" for ch_id, info in data["youtube"].items()]
        tw_lines = [f"**{info['name']}** `{login}`"  for login, info in data["twitch"].items()]

        embed = discord.Embed(title=t(lang, "noti_list_title"), color=discord.Color.blurple())
        embed.add_field(name=t(lang, "noti_list_video_ch"),  value=video_ch,  inline=True)
        embed.add_field(name=t(lang, "noti_list_stream_ch"), value=stream_ch, inline=True)
        embed.add_field(name=t(lang, "noti_list_yt_role"),   value=yt_role,   inline=True)
        embed.add_field(name=t(lang, "noti_list_tw_role"),   value=tw_role,   inline=True)
        embed.add_field(name=t(lang, "noti_list_youtube", count=len(yt_lines)), value="\n".join(yt_lines) or "—", inline=False)
        embed.add_field(name=t(lang, "noti_list_twitch",  count=len(tw_lines)), value="\n".join(tw_lines)  or "—", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(NotiCog(bot))
