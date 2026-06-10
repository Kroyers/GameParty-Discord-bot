import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
from lang import detect_lang, atomic_write_json, t
import json
import os
import logging
import re
import time
import xml.etree.ElementTree as ET
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
    atomic_write_json(CONFIG_FILE, data)

def load_noti() -> dict:
    if not os.path.exists(NOTI_FILE):
        return {"youtube": {}, "twitch": {}, "rss": {}}
    with open(NOTI_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("youtube", {})
    data.setdefault("twitch", {})
    data.setdefault("rss", {})
    return data

def save_noti(data: dict) -> None:
    atomic_write_json(NOTI_FILE, data)

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

    log.debug(f"YouTube API: channels lookup for {query!r}")
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
    log.debug(f"YouTube API: playlistItems for {channel_id}")
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
        log.debug("Twitch API: refreshing OAuth token")
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
            log.debug("Twitch API: token refreshed")
            return bool(self._token)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Client-Id": self.client_id}

    async def _get(self, session: aiohttp.ClientSession, endpoint: str, params: dict) -> dict | None:
        if not self._token and not await self._refresh(session):
            return None
        log.debug(f"Twitch API: GET {endpoint} {params}")
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
#  RSS
# ─────────────────────────────────────────────

_MEDIA_NS = "http://search.yahoo.com/mrss/"

def _strip_html(text: str) -> str:
    text = re.sub(r"<(strong|b)>(.*?)</(strong|b)>", r"**\2**", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|li|div|h\d)>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    for ent, char in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
        ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"),
    ):
        text = text.replace(ent, char)
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()

def _parse_rss(xml_text: str) -> tuple[str, list[dict]]:
    """Returns (feed_title, [items]) where each item is {guid, title, link, desc, image, pubdate}."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "", []

    # RSS 2.0
    channel = root.find("channel")
    if channel is not None:
        feed_title = (channel.findtext("title") or "").strip()
        items = []
        for item in channel.findall("item"):
            guid     = (item.findtext("guid") or item.findtext("link") or "").strip()
            title    = (item.findtext("title") or "").strip()
            link     = (item.findtext("link") or "").strip()
            pubdate  = (item.findtext("pubDate") or "").strip()
            desc_raw = item.findtext("description") or ""
            desc     = _strip_html(desc_raw)
            image    = None
            enc = item.find("enclosure")
            if enc is not None and "image" in (enc.get("type") or ""):
                image = enc.get("url")
            if not image:
                for tag in (f"{{{_MEDIA_NS}}}thumbnail", f"{{{_MEDIA_NS}}}content"):
                    el = item.find(tag)
                    if el is not None:
                        image = el.get("url")
                        break
            if not image:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc_raw)
                if m:
                    image = m.group(1)
            items.append({"guid": guid, "title": title, "link": link, "desc": desc, "image": image, "pubdate": pubdate})
        return feed_title, items

    # Atom
    ATOM = "http://www.w3.org/2005/Atom"
    feed_title = (root.findtext(f"{{{ATOM}}}title") or "").strip()
    items = []
    for entry in root.findall(f"{{{ATOM}}}entry"):
        guid     = (entry.findtext(f"{{{ATOM}}}id") or "").strip()
        title    = (entry.findtext(f"{{{ATOM}}}title") or "").strip()
        link_el  = entry.find(f"{{{ATOM}}}link")
        link     = link_el.get("href", "") if link_el is not None else ""
        pubdate  = (entry.findtext(f"{{{ATOM}}}updated") or entry.findtext(f"{{{ATOM}}}published") or "").strip()
        desc_raw = entry.findtext(f"{{{ATOM}}}summary") or entry.findtext(f"{{{ATOM}}}content") or ""
        desc     = _strip_html(desc_raw)
        image    = None
        for tag in (f"{{{_MEDIA_NS}}}thumbnail", f"{{{_MEDIA_NS}}}content"):
            el = entry.find(tag)
            if el is not None:
                image = el.get("url")
                break
        items.append({"guid": guid, "title": title, "link": link, "desc": desc, "image": image, "pubdate": pubdate})
    return feed_title, items

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


def rss_embed(feed_name: str, title: str, link: str, desc: str, image: str | None, color: int = 0x2196F3) -> discord.Embed:
    embed = discord.Embed(title=title, url=link or None, color=color)
    embed.set_author(name=feed_name)
    if desc:
        embed.description = desc[:500] + ("…" if len(desc) > 500 else "")
    if image:
        embed.set_thumbnail(url=image)
    embed.set_footer(text="News")
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
        self.check_rss.start()

    async def cog_unload(self):
        self.check_youtube.cancel()
        self.check_twitch.cancel()
        self.check_rss.cancel()
        if self._session:
            await self._session.close()

    # ── YouTube poll (every 10 min, 1–2 quota units/channel) ─────
    # Videos: 1 unit (playlistItems). Streams: +1 unit (videos.list batch).

    @tasks.loop(minutes=10)
    async def check_youtube(self):
        cfg          = load_config()
        video_ch_id  = cfg.get("noti_video_channel_id")
        yt_role_id   = cfg.get("noti_youtube_role_id")
        if not video_ch_id:
            return
        data         = load_noti()
        changed      = False
        log.debug(f"YouTube check: {len(data['youtube'])} channel(s).")

        for ch_id, info in data["youtube"].items():
            recent = await yt_fetch_recent(self._session, self._yt_key, ch_id, max_results=1)
            if not recent:
                continue

            video_id, title, thumb = recent[0]
            if video_id != info.get("last_video_id"):
                old = info.get("last_video_id")
                if old is None:
                    info["last_video_id"] = video_id
                    changed = True
                else:
                    channel = self.bot.get_channel(video_ch_id)
                    if channel:
                        try:
                            kwargs = {"embed": yt_video_embed(info["name"], ch_id, video_id, title, thumb)}
                            if yt_role_id:
                                kwargs["content"] = _role_mention(yt_role_id, channel.guild)
                                kwargs["allowed_mentions"] = discord.AllowedMentions(everyone=True, roles=True)
                            await channel.send(**kwargs)
                            info["last_video_id"] = video_id
                            changed = True
                            log.info(f"YT video notified: {title!r} ({ch_id})")
                        except Exception as e:
                            log.error(f"YT video send failed ({ch_id}): {e}")
                    else:
                        info["last_video_id"] = video_id
                        changed = True

        if changed:
            save_noti_section("youtube", data["youtube"])

    @check_youtube.before_loop
    async def _before_youtube(self):
        await self.bot.wait_until_ready()

    # ── Twitch poll (every 5 min) ─────────────

    @tasks.loop(minutes=5)
    async def check_twitch(self):
        cfg          = load_config()
        stream_ch_id = cfg.get("noti_stream_channel_id")
        tw_role_id   = cfg.get("noti_twitch_role_id")
        data         = load_noti()
        changed      = False

        if not stream_ch_id:
            return
        log.debug(f"Twitch check: {len(data['twitch'])} streamer(s).")

        for login, info in data["twitch"].items():
            stream    = await self._twitch.get_stream(self._session, login)
            stream_id = stream["id"] if stream else None
            if stream_id == info.get("stream_id"):
                if stream and stream.get("thumbnail_url"):
                    # Persist only on a real change — the live embed refresh
                    # below runs every tick either way.
                    if info.get("stream_thumb") != stream["thumbnail_url"]:
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

            old_msg_id  = info.get("msg_id")
            old_title   = info.get("stream_title", "")
            old_started = info.get("stream_started_at", "")

            channel = self.bot.get_channel(stream_ch_id)
            if not channel:
                info["stream_id"]         = stream_id
                info["msg_id"]            = None
                info["stream_title"]      = stream.get("title", "") if stream else old_title
                info["stream_started_at"] = stream.get("started_at", "") if stream else ""
                info["stream_thumb"]      = stream.get("thumbnail_url", "") if stream else info.get("stream_thumb", "")
                changed = True
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
                    info["stream_id"]         = stream_id
                    info["msg_id"]            = msg.id
                    info["stream_title"]      = stream.get("title", "")
                    info["stream_started_at"] = stream.get("started_at", "")
                    info["stream_thumb"]      = stream.get("thumbnail_url", "")
                    changed = True
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
                    info["stream_id"]         = stream_id
                    info["msg_id"]            = None
                    info["stream_title"]      = old_title
                    info["stream_started_at"] = ""
                    changed = True
                    log.info(f"Twitch stream ended: {login}")
                except Exception as e:
                    log.error(f"Twitch edit failed ({login}): {e}")
            else:
                info["stream_id"]         = stream_id
                info["msg_id"]            = None
                info["stream_started_at"] = ""
                changed = True

        if changed:
            save_noti_section("twitch", data["twitch"])

    @check_twitch.before_loop
    async def _before_twitch(self):
        await self.bot.wait_until_ready()

    # ── RSS poll (every 10 min) ───────────────

    @tasks.loop(minutes=10)
    async def check_rss(self):
        cfg     = load_config()
        ch_id   = cfg.get("noti_rss_channel_id")
        role_id = cfg.get("noti_rss_role_id")
        data    = load_noti()
        changed = False

        if not ch_id or not data["rss"]:
            return

        channel = self.bot.get_channel(ch_id)
        if not channel:
            return
        log.debug(f"RSS check: {len(data['rss'])} feed(s).")

        for url, info in data["rss"].items():
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        log.warning(f"RSS fetch failed ({url}): HTTP {r.status}")
                        continue
                    xml_text = await r.text()
            except Exception as e:
                log.warning(f"RSS fetch error ({url}): {e}")
                continue

            _, items = _parse_rss(xml_text)
            if not items:
                continue

            latest       = items[0]
            guid_changed = latest["guid"]    != info.get("last_guid")
            pub_changed  = latest["pubdate"] != info.get("last_pubdate") and latest["pubdate"]

            if not guid_changed and not pub_changed:
                continue

            first_run = info.get("last_guid") is None

            if first_run:
                info["last_guid"]    = latest["guid"]
                info["last_pubdate"] = latest["pubdate"]
                changed = True
                continue

            if guid_changed:
                info["msg_id"] = None

            embed   = rss_embed(info["name"], latest["title"], latest["link"], latest["desc"], latest["image"], info.get("color", 0x2196F3))
            sent_ok = False

            if not guid_changed and info.get("msg_id"):
                try:
                    msg = await channel.fetch_message(info["msg_id"])
                    await msg.edit(embed=embed)
                    log.info(f"RSS news updated (edit): {latest['title']!r} ({url})")
                    sent_ok = True
                except Exception as e:
                    log.warning(f"RSS edit failed, resending ({url}): {e}")

            if not sent_ok:
                try:
                    kwargs = {"embed": embed}
                    if role_id:
                        kwargs["content"] = _role_mention(role_id, channel.guild)
                        kwargs["allowed_mentions"] = discord.AllowedMentions(everyone=True, roles=True)
                    msg = await channel.send(**kwargs)
                    info["msg_id"] = msg.id
                    sent_ok = True
                    log.info(f"RSS news notified: {latest['title']!r} ({url})")
                except Exception as e:
                    log.error(f"RSS send failed ({url}): {e}")

            if sent_ok:
                info["last_guid"]    = latest["guid"]
                info["last_pubdate"] = latest["pubdate"]
                changed = True

        if changed:
            save_noti_section("rss", data["rss"])

    @check_rss.before_loop
    async def _before_rss(self):
        await self.bot.wait_until_ready()

    # ── Commands ──────────────────────────────

    @app_commands.command(name="noti-video", description=app_commands.locale_str("Set the Discord channel for all video notifications", key="cmd_noti_video"))
    @app_commands.describe(channel=app_commands.locale_str("Discord channel where new video notifications will be sent", key="cmd_noti_video_channel"))
    @app_commands.default_permissions(administrator=True)
    async def noti_video(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        target = channel or interaction.channel
        cfg = load_config()
        cfg["noti_video_channel_id"] = target.id
        save_config(cfg)
        await interaction.response.send_message(f"✅ {t(detect_lang(interaction), 'noti_video_set', channel=target.mention)}", ephemeral=True)
        log.info(f"Noti video channel set to #{target.name} ({target.id}) by {interaction.user}.")

    @app_commands.command(name="noti-stream", description=app_commands.locale_str("Set the Discord channel for all stream notifications", key="cmd_noti_stream"))
    @app_commands.describe(channel=app_commands.locale_str("Discord channel where live stream notifications will be sent", key="cmd_noti_stream_channel"))
    @app_commands.default_permissions(administrator=True)
    async def noti_stream(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        target = channel or interaction.channel
        cfg = load_config()
        cfg["noti_stream_channel_id"] = target.id
        save_config(cfg)
        await interaction.response.send_message(f"✅ {t(detect_lang(interaction), 'noti_stream_set', channel=target.mention)}", ephemeral=True)
        log.info(f"Noti stream channel set to #{target.name} ({target.id}) by {interaction.user}.")

    @app_commands.command(name="noti-rss-channel", description=app_commands.locale_str("Set the channel for RSS news notifications", key="cmd_noti_rss_channel"))
    @app_commands.describe(channel=app_commands.locale_str("Channel to post RSS notifications in", key="cmd_noti_rss_channel_param"))
    @app_commands.default_permissions(administrator=True)
    async def noti_rss_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        target = channel or interaction.channel
        cfg = load_config()
        cfg["noti_rss_channel_id"] = target.id
        save_config(cfg)
        await interaction.response.send_message(t(detect_lang(interaction), "noti_rss_channel_set", channel=target.mention), ephemeral=True)
        log.info(f"Noti RSS channel set to #{target.name} ({target.id}) by {interaction.user}.")

    @app_commands.command(name="noti-youtube-role", description=app_commands.locale_str("Set the role to mention for YouTube notifications", key="cmd_noti_youtube_role"))
    @app_commands.describe(role=app_commands.locale_str("Role to mention for YouTube videos and streams", key="cmd_noti_youtube_role_param"))
    @app_commands.default_permissions(administrator=True)
    async def noti_role_youtube(self, interaction: discord.Interaction, role: discord.Role):
        cfg = load_config()
        cfg["noti_youtube_role_id"] = role.id
        save_config(cfg)
        mention = "@everyone" if role.is_default() else role.mention
        await interaction.response.send_message(f"✅ {t(detect_lang(interaction), 'noti_youtube_role_set', role=mention)}", ephemeral=True)
        log.info(f"Noti YouTube role set to {role.name} ({role.id}) by {interaction.user}.")

    @app_commands.command(name="noti-twitch-role", description=app_commands.locale_str("Set the role to mention for Twitch notifications", key="cmd_noti_twitch_role"))
    @app_commands.describe(role=app_commands.locale_str("Role to mention for Twitch streams", key="cmd_noti_twitch_role_param"))
    @app_commands.default_permissions(administrator=True)
    async def noti_role_twitch(self, interaction: discord.Interaction, role: discord.Role):
        cfg = load_config()
        cfg["noti_twitch_role_id"] = role.id
        save_config(cfg)
        mention = "@everyone" if role.is_default() else role.mention
        await interaction.response.send_message(f"✅ {t(detect_lang(interaction), 'noti_twitch_role_set', role=mention)}", ephemeral=True)
        log.info(f"Noti Twitch role set to {role.name} ({role.id}) by {interaction.user}.")

    @app_commands.command(name="noti-rss-role", description=app_commands.locale_str("Set the role to mention for RSS notifications", key="cmd_noti_rss_role"))
    @app_commands.describe(role=app_commands.locale_str("Role to mention for RSS news", key="cmd_noti_rss_role_param"))
    @app_commands.default_permissions(administrator=True)
    async def noti_rss_role(self, interaction: discord.Interaction, role: discord.Role):
        cfg = load_config()
        cfg["noti_rss_role_id"] = role.id
        save_config(cfg)
        mention = "@everyone" if role.is_default() else role.mention
        await interaction.response.send_message(t(detect_lang(interaction), "noti_rss_role_set", role=mention), ephemeral=True)
        log.info(f"Noti RSS role set to {role.name} ({role.id}) by {interaction.user}.")

    @app_commands.command(name="noti-youtube-add", description=app_commands.locale_str("Add a YouTube channel to monitor", key="cmd_noti_youtube_add"))
    @app_commands.describe(channel=app_commands.locale_str("YouTube channel URL, @handle, or channel ID (UCxxxxxx)", key="cmd_noti_youtube_add_channel"))
    @app_commands.default_permissions(administrator=True)
    async def noti_youtube_add(self, interaction: discord.Interaction, channel: str):
        lang = detect_lang(interaction)
        if not self._yt_key:
            await interaction.response.send_message(f"❌ {t(lang, 'noti_err_no_yt_key')}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        resolved = await yt_resolve_channel(self._session, self._yt_key, channel)
        if not resolved:
            await interaction.followup.send(f"❌ {t(lang, 'noti_err_yt_not_found')}")
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

        await interaction.followup.send(f"✅ {t(lang, 'noti_added', name=name)}")
        log.info(f"YT channel added: {name} ({ch_id}) by {interaction.user}.")

    @app_commands.command(name="noti-twitch-add", description=app_commands.locale_str("Add a Twitch streamer to monitor", key="cmd_noti_twitch_add"))
    @app_commands.describe(username=app_commands.locale_str("Twitch username", key="cmd_noti_twitch_add_username"))
    @app_commands.default_permissions(administrator=True)
    async def noti_twitch_add(self, interaction: discord.Interaction, username: str):
        lang = detect_lang(interaction)
        if not self._twitch:
            await interaction.response.send_message(f"❌ {t(lang, 'noti_err_no_twitch')}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        login = username.lstrip("@").lower()
        user  = await self._twitch.get_user(self._session, login)
        if not user:
            await interaction.followup.send(f"❌ {t(lang, 'noti_err_twitch_not_found', login=login)}")
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

        await interaction.followup.send(f"✅ {t(lang, 'noti_added', name=user['display_name'])}")
        log.info(f"Twitch added: {user['display_name']} ({login}) by {interaction.user}.")

    @app_commands.command(name="noti-rss-add", description=app_commands.locale_str("Add an RSS feed to monitor", key="cmd_noti_rss_add"))
    @app_commands.describe(
        url=app_commands.locale_str("RSS feed URL", key="cmd_noti_rss_add_url"),
        name=app_commands.locale_str("Display name (leave empty to use feed title)", key="cmd_noti_rss_add_name"),
        color=app_commands.locale_str("Embed color as hex (e.g. 1B2838), default: 2196F3", key="cmd_noti_rss_add_color"),
    )
    @app_commands.default_permissions(administrator=True)
    async def noti_rss_add(self, interaction: discord.Interaction, url: str, name: str | None = None, color: str | None = None):
        lang = detect_lang(interaction)
        await interaction.response.defer(ephemeral=True)

        embed_color = 0x2196F3
        if color:
            try:
                embed_color = int(color.lstrip("#"), 16)
            except ValueError:
                await interaction.followup.send(t(lang, "noti_err_rss_color"))
                return

        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    await interaction.followup.send(t(lang, "noti_err_rss_invalid"))
                    return
                xml_text = await r.text()
        except Exception:
            await interaction.followup.send(t(lang, "noti_err_rss_invalid"))
            return

        feed_title, items = _parse_rss(xml_text)
        if not items:
            await interaction.followup.send(t(lang, "noti_err_rss_invalid"))
            return

        feed_name = name or feed_title or url
        latest    = items[0]

        data = load_noti()
        data["rss"][url] = {"name": feed_name, "last_guid": latest["guid"], "last_pubdate": latest["pubdate"], "color": embed_color, "msg_id": None}
        save_noti(data)

        cfg     = load_config()
        ch_id   = cfg.get("noti_rss_channel_id")
        role_id = cfg.get("noti_rss_role_id")
        ch = self.bot.get_channel(ch_id) if ch_id else None
        if ch:
            try:
                kwargs = {"embed": rss_embed(feed_name, latest["title"], latest["link"], latest["desc"], latest["image"], embed_color)}
                if role_id:
                    kwargs["content"] = _role_mention(role_id, ch.guild)
                    kwargs["allowed_mentions"] = discord.AllowedMentions(everyone=True, roles=True)
                msg = await ch.send(**kwargs)
                data["rss"][url]["msg_id"] = msg.id
                save_noti_section("rss", data["rss"])
                log.info(f"RSS latest item sent for {feed_name} ({url}).")
            except Exception as e:
                log.error(f"RSS latest send failed ({url}): {e}")

        await interaction.followup.send(f"✅ {t(lang, 'noti_added', name=feed_name)}")
        log.info(f"RSS feed added: {feed_name} ({url}) by {interaction.user}.")

    @app_commands.command(name="noti-youtube-remove", description=app_commands.locale_str("Remove a monitored YouTube channel", key="cmd_noti_youtube_remove"))
    @app_commands.describe(channel=app_commands.locale_str("YouTube channel (start typing to search)", key="cmd_noti_youtube_remove_ch"))
    @app_commands.default_permissions(administrator=True)
    async def noti_youtube_remove(self, interaction: discord.Interaction, channel: str):
        lang = detect_lang(interaction)
        data = load_noti()
        info = data["youtube"].pop(channel, None)
        if not info:
            await interaction.response.send_message(f"❌ {t(lang, 'noti_err_yt_not_in_list')}", ephemeral=True)
            return
        save_noti(data)
        await interaction.response.send_message(f"✅ {t(lang, 'noti_removed', name=info['name'])}", ephemeral=True)
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
            await interaction.response.send_message(f"❌ {t(lang, 'noti_err_twitch_not_in_list')}", ephemeral=True)
            return
        save_noti(data)
        await interaction.response.send_message(f"✅ {t(lang, 'noti_removed', name=info['name'])}", ephemeral=True)
        log.info(f"Twitch removed: {info['name']} ({username}) by {interaction.user}.")

    @noti_twitch_remove.autocomplete("username")
    async def _twitch_remove_ac(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        data = load_noti()
        return [
            app_commands.Choice(name=info["name"], value=login)
            for login, info in data["twitch"].items()
            if current.lower() in info["name"].lower() or current.lower() in login
        ][:25]

    @app_commands.command(name="noti-rss-remove", description=app_commands.locale_str("Remove a monitored RSS feed", key="cmd_noti_rss_remove"))
    @app_commands.describe(feed=app_commands.locale_str("RSS feed (start typing to search)", key="cmd_noti_rss_remove_feed"))
    @app_commands.default_permissions(administrator=True)
    async def noti_rss_remove(self, interaction: discord.Interaction, feed: str):
        lang = detect_lang(interaction)
        data = load_noti()
        info = data["rss"].pop(feed, None)
        if not info:
            await interaction.response.send_message(t(lang, "noti_err_rss_not_in_list"), ephemeral=True)
            return
        save_noti(data)
        await interaction.response.send_message(f"✅ {t(lang, 'noti_removed', name=info['name'])}", ephemeral=True)
        log.info(f"RSS feed removed: {info['name']} ({feed}) by {interaction.user}.")

    @noti_rss_remove.autocomplete("feed")
    async def _rss_remove_ac(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        data = load_noti()
        return [
            app_commands.Choice(name=info["name"][:100], value=url)
            for url, info in data["rss"].items()
            # Discord caps choice values at 100 chars — longer URLs can still
            # be removed by pasting the full URL manually.
            if len(url) <= 100
            and (current.lower() in info["name"].lower() or current.lower() in url.lower())
        ][:25]

    @app_commands.command(name="noti-list", description=app_commands.locale_str("List all monitored YouTube channels and Twitch streamers", key="cmd_noti_list"))
    @app_commands.default_permissions(administrator=True)
    async def noti_list(self, interaction: discord.Interaction):
        lang = detect_lang(interaction)
        cfg  = load_config()
        data = load_noti()

        video_ch  = f"<#{cfg['noti_video_channel_id']}>"  if cfg.get("noti_video_channel_id")  else "—"
        stream_ch = f"<#{cfg['noti_stream_channel_id']}>" if cfg.get("noti_stream_channel_id") else "—"
        rss_ch    = f"<#{cfg['noti_rss_channel_id']}>"    if cfg.get("noti_rss_channel_id")    else "—"
        yt_role   = f"<@&{cfg['noti_youtube_role_id']}>"  if cfg.get("noti_youtube_role_id")   else "—"
        tw_role   = f"<@&{cfg['noti_twitch_role_id']}>"   if cfg.get("noti_twitch_role_id")    else "—"
        rss_role  = f"<@&{cfg['noti_rss_role_id']}>"      if cfg.get("noti_rss_role_id")       else "—"

        yt_lines  = [f"**{info['name']}** `{ch_id}`" for ch_id, info in data["youtube"].items()]
        tw_lines  = [f"**{info['name']}** `{login}`" for login, info in data["twitch"].items()]
        rss_lines = [f"**{info['name']}** `{url}`"   for url,   info in data["rss"].items()]

        embed = discord.Embed(title=t(lang, "noti_list_title"), color=discord.Color.blurple())
        embed.add_field(name=t(lang, "noti_list_video_ch"),  value=video_ch,  inline=True)
        embed.add_field(name=t(lang, "noti_list_stream_ch"), value=stream_ch, inline=True)
        embed.add_field(name=t(lang, "noti_list_rss_ch"),    value=rss_ch,    inline=True)
        embed.add_field(name=t(lang, "noti_list_yt_role"),   value=yt_role,   inline=True)
        embed.add_field(name=t(lang, "noti_list_tw_role"),   value=tw_role,   inline=True)
        embed.add_field(name=t(lang, "noti_list_rss_role"),  value=rss_role,  inline=True)
        embed.add_field(name=t(lang, "noti_list_youtube", count=len(yt_lines)),  value="\n".join(yt_lines)  or "—", inline=False)
        embed.add_field(name=t(lang, "noti_list_twitch",  count=len(tw_lines)),  value="\n".join(tw_lines)  or "—", inline=False)
        embed.add_field(name=t(lang, "noti_list_rss",     count=len(rss_lines)), value="\n".join(rss_lines) or "—", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(NotiCog(bot))
