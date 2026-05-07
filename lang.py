import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import logging

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger(__name__)
USERS_FILE   = os.path.join(SCRIPT_DIR, "users.json")
LOCALES_DIR  = os.path.join(SCRIPT_DIR, "locales")
DEFAULT_LANG = "en"

_VALID_LANGS = frozenset(
    f[:-5] for f in os.listdir(LOCALES_DIR) if f.endswith(".json")
)

def _load_locales() -> dict:
    locales = {}
    for fname in os.listdir(LOCALES_DIR):
        if fname.endswith(".json"):
            with open(os.path.join(LOCALES_DIR, fname), "r", encoding="utf-8") as f:
                locales[fname[:-5]] = json.load(f)
    return locales

_LOCALES = _load_locales()

def _t(lang: str, key: str, **kwargs) -> str:
    text = _LOCALES.get(lang, _LOCALES.get(DEFAULT_LANG, {})).get(key, key)
    return text.format(**kwargs) if kwargs else text

# ─────────────────────────────────────────────
#  USER LANGUAGE HELPERS
# ─────────────────────────────────────────────

def _get_user_lang(user_id: str) -> str | None:
    """Returns cached or explicit lang (used where interaction is unavailable, e.g. on_voice_state_update)."""
    if not os.path.exists(USERS_FILE):
        return None
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get(user_id, {}).get("lang")

def _get_explicit_lang(user_id: str) -> str | None:
    """Returns lang only if the user explicitly set it — None means auto mode."""
    if not os.path.exists(USERS_FILE):
        return None
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        entry = json.load(f).get(user_id, {})
    lang = entry.get("lang")
    return lang if entry.get("lang_explicit") and lang in _VALID_LANGS else None

def _save_user_lang(user_id: str, lang: str, explicit: bool = False) -> None:
    data = {}
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    entry = data.setdefault(user_id, {})
    entry["lang"] = lang
    if explicit:
        entry["lang_explicit"] = True
    else:
        entry.pop("lang_explicit", None)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def clear_user_lang(user_id: str) -> None:
    """Remove explicit lang override — keeps the cached value so panels still have a fallback."""
    if not os.path.exists(USERS_FILE):
        return
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if user_id in data:
        data[user_id].pop("lang_explicit", None)
        if not data[user_id]:
            del data[user_id]
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def detect_lang(interaction: discord.Interaction) -> str:
    user_id = str(interaction.user.id)
    code    = str(interaction.locale).split("-")[0]
    discord_lang = code if code in _VALID_LANGS else DEFAULT_LANG

    if not os.path.exists(USERS_FILE):
        _save_user_lang(user_id, discord_lang, explicit=False)
        return discord_lang

    with open(USERS_FILE, "r", encoding="utf-8") as f:
        entry = json.load(f).get(user_id, {})

    saved = entry.get("lang")
    if entry.get("lang_explicit") and saved in _VALID_LANGS:
        return saved

    if saved != discord_lang:
        _save_user_lang(user_id, discord_lang, explicit=False)
    return discord_lang

# ─────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────

class LangCog(commands.Cog):
    @app_commands.command(
        name="lang",
        description=app_commands.locale_str("Change bot language", key="cmd_lang"),
    )
    async def lang_cmd(self, interaction: discord.Interaction):
        from voice import LangView
        lang         = detect_lang(interaction)
        current_lang = _get_explicit_lang(str(interaction.user.id))
        view         = LangView(current_lang, lang)
        await interaction.response.send_message(_t(lang, "voice_prompt_lang"), view=view, ephemeral=True)
        log.info(f"/lang opened by {interaction.user} (current: {current_lang or 'auto'})")


async def setup(bot: commands.Bot):
    await bot.add_cog(LangCog(bot))
