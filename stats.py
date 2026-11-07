import discord
from discord import app_commands
from discord.ext import commands
from lang import detect_lang
import asyncio
import os
import platform
import re
import logging
import aiohttp

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

# Usage statistics in Tianji (Application tracking). Disabled while either variable is missing.
TIANJI_URL    = os.getenv("TIANJI_URL", "").rstrip("/")
TIANJI_APP_ID = os.getenv("TIANJI_APP_ID", "")
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
VERSION_LIMIT = 35   # Tianji stores a session's version in VARCHAR(35)
OS_LIMIT      = 20   # ... and its OS in VARCHAR(20)
OS_RELEASE_FILE = "/etc/os-release"

_session: aiohttp.ClientSession | None = None
_pending: set[asyncio.Task] = set()   # keeps fire-and-forget sends alive until they finish

# ─────────────────────────────────────────────
#  SENDING
# ─────────────────────────────────────────────

def bot_version() -> str:
    # VERSION lives in main.py (the auto-updater depends on it being there),
    # and importing main would start the bot — so read it from the file.
    try:
        with open(os.path.join(SCRIPT_DIR, "main.py"), "r", encoding="utf-8") as f:
            match = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
        return match.group(1) if match else "?"
    except Exception:
        return "?"


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().replace("\x00", "").strip()
    except OSError:
        return ""


def _linux_name() -> str:
    """Short distribution name, e.g. "Raspbian 13" or "Debian 12"."""
    release = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', _read(OS_RELEASE_FILE), re.MULTILINE))
    name = " ".join(x for x in (release.get("ID", "").capitalize(), release.get("VERSION_ID", "")) if x)
    return name or "Linux"


def host_os() -> str:
    """Text for Tianji's OS chart: the system the bot runs on, e.g. "Windows 10" or "Raspbian 13"."""
    system = platform.system() or "Unknown"
    if system == "Windows":
        text = f"{system} {platform.release()}"
    elif system == "Linux":
        # The kernel release says little (e.g. "6.6.44-production+truenas"), the distribution says more.
        text = _linux_name()
    else:
        text = system
    return text[:OS_LIMIT]


def _reported_version(guild: discord.Guild | None) -> str:
    """Text for Tianji's version chart: bot version and the Discord server the event came from,
    e.g. "0.3.10 · GameParty"."""
    version = bot_version()
    if guild is not None:
        version = f"{version} · {guild.name}"
    return version if len(version) <= VERSION_LIMIT else version[:VERSION_LIMIT - 1] + "…"


async def _send(name: str, data: dict, guild: discord.Guild | None) -> None:
    version = _reported_version(guild)
    system  = host_os()
    payload = {"type": "event", "payload": {"application": TIANJI_APP_ID, "name": name[:50], "data": data,
                                            "version": version, "os": system}}
    # Tianji keys sessions by IP and User-Agent and stores version and OS on the session,
    # so every version, system and server gets a session of its own. Headers must stay ASCII,
    # hence the server ID instead of its name.
    agent = f"GamePartyBot/{bot_version()} ({system.encode('ascii', 'replace').decode()}"
    agent += f"; guild {guild.id})" if guild is not None else ")"
    try:
        async with _session.post(
            f"{TIANJI_URL}/api/application/send", json=payload,
            headers={"User-Agent": agent}, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 400:
                log.warning(f"Tianji rejected the event '{name}' ({resp.status}): {(await resp.text())[:200]}")
    except Exception as e:   # statistics must never break the bot
        log.warning(f"Tianji event '{name}' failed: {e}")


def track(name: str, interaction: discord.Interaction | None = None, **data) -> None:
    """Reports an event without waiting for it, so interaction responses are never delayed.
    No user IDs or names are sent, only the event, the user's language and the given data."""
    if _session is None:
        return
    guild = None
    if interaction is not None:
        data.setdefault("lang", detect_lang(interaction))
        guild = interaction.guild
    task = asyncio.get_running_loop().create_task(_send(name, data, guild))
    _pending.add(task)
    task.add_done_callback(_pending.discard)

# ─────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────

class StatsCog(commands.Cog):
    """Every finished slash command becomes an event named after it, e.g. "/poll"."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        global _session
        if TIANJI_URL and TIANJI_APP_ID:
            _session = aiohttp.ClientSession()
        else:
            log.warning("TIANJI_URL/TIANJI_APP_ID not set — usage statistics disabled.")

    async def cog_unload(self):
        global _session
        if _pending:
            await asyncio.gather(*_pending, return_exceptions=True)
        if _session:
            await _session.close()
            _session = None

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command: app_commands.Command):
        track(f"/{command.qualified_name}", interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
