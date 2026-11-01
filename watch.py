import discord
from discord.ext import commands, tasks
from lang import atomic_write_json
import html
import json
import os
import logging
import re
import aiohttp

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(SCRIPT_DIR, "watch_state.json")

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

# Each watch: page URL + regexes extracting the texts that should change once the product is available.
# On any change of the extracted texts (incl. a text disappearing) the user is pinged in the channel.
WATCHES = {
    "babble_tracker_pro": {
        "name":       "Babble Mouth Tracker Pro",
        "url":        "https://babble.diy/store/babble-tracker-pro/",
        "channel_id": 1301917425071034388,
        "user_id":    131526344231223297,
        "fields": {
            "price":  r'<span class="text-4xl font-bold[^"]*">(.*?)</span>',
            "button": r'font-bold text-text-dark">.*?</div>\s*<button[^>]*>(.*?)</button>',
        },
    },
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GamePartyBot/1.0)"}

# ─────────────────────────────────────────────
#  STORAGE
# ─────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(data: dict) -> None:
    atomic_write_json(STATE_FILE, data)

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _clean(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    return " ".join(html.unescape(text).split())

def extract_fields(page: str, fields: dict) -> dict:
    out = {}
    for key, pattern in fields.items():
        m = re.search(pattern, page, re.S)
        out[key] = _clean(m.group(1)) if m else None
    return out

async def fetch_page(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                log.warning(f"Watch fetch {url} returned HTTP {resp.status}")
                return None
            return await resp.text()
    except Exception as e:
        log.warning(f"Watch fetch {url} failed: {e}")
        return None

# ─────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────

class WatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        self.check_pages.start()

    async def cog_unload(self):
        self.check_pages.cancel()
        if self._session:
            await self._session.close()

    @tasks.loop(minutes=1)
    async def check_pages(self):
        state   = load_state()
        changed = False

        for key, w in WATCHES.items():
            page = await fetch_page(self._session, w["url"])
            if page is None:
                continue

            current = extract_fields(page, w["fields"])
            old     = state.get(key)
            if old is None:
                state[key] = current
                changed = True
                log.info(f"Watch baseline for {key}: {current}")
                continue
            if current == old:
                continue

            channel = self.bot.get_channel(w["channel_id"])
            if not channel:
                log.error(f"Watch channel {w['channel_id']} not found ({key}).")
                continue

            embed = discord.Embed(
                title=f"🛒 {w['name']} — změna na stránce",
                url=w["url"],
                color=discord.Color.green(),
            )
            for field in w["fields"]:
                before = old.get(field) or "—"
                after  = current.get(field) or "—"
                if before != after:
                    embed.add_field(name=field, value=f"~~{before}~~\n**{after}**", inline=False)
            try:
                await channel.send(
                    content=f"<@{w['user_id']}>",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                state[key] = current
                changed = True
                log.info(f"Watch change notified for {key}: {old} -> {current}")
            except Exception as e:
                log.error(f"Watch send failed ({key}): {e}")

        if changed:
            save_state(state)

    @check_pages.before_loop
    async def _before_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(WatchCog(bot))
