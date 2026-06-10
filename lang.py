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

# ─────────────────────────────────────────────
#  LOCALES — single source for all modules
# ─────────────────────────────────────────────

def _load_locales() -> dict:
    locales = {}
    for fname in os.listdir(LOCALES_DIR):
        if fname.endswith(".json"):
            with open(os.path.join(LOCALES_DIR, fname), "r", encoding="utf-8") as f:
                locales[fname[:-5]] = json.load(f)
    return locales

LOCALES = _load_locales()
_VALID_LANGS = frozenset(LOCALES)


def t(lang: str, key: str, **kwargs) -> str:
    text = LOCALES.get(lang, {}).get(key)
    if text is None:
        text = LOCALES.get(DEFAULT_LANG, {}).get(key, key)
    return text.format(**kwargs) if kwargs else text

_t = t


class LocaleTranslator(app_commands.Translator):
    async def translate(self, string: app_commands.locale_str, locale: discord.Locale, _context: app_commands.TranslationContext) -> str | None:
        key = string.extras.get("key")
        if not key:
            return None
        lang_code = str(locale).split("-")[0]
        return LOCALES.get(lang_code, {}).get(key)


def atomic_write_json(path: str, data, indent: int = 2, ensure_ascii: bool = True) -> None:
    """Write JSON to a temp file and swap it in with os.replace, so a crash
    mid-write can never leave a corrupted file behind."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
    os.replace(tmp, path)

# ─────────────────────────────────────────────
#  USER LANGUAGE HELPERS
# ─────────────────────────────────────────────

# In-memory cache of per-user language settings so detect_lang doesn't hit
# the disk on every interaction. users.json is also written by other modules,
# but lang/lang_explicit change only through the helpers below, so the cache
# stays consistent.
_lang_cache: dict[str, dict] | None = None

def _get_lang_cache() -> dict:
    global _lang_cache
    if _lang_cache is None:
        _lang_cache = {}
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _lang_cache = {
                uid: {"lang": entry.get("lang"), "lang_explicit": entry.get("lang_explicit", False)}
                for uid, entry in data.items()
            }
            log.debug(f"Lang cache loaded ({len(_lang_cache)} users).")
    return _lang_cache

def _get_user_lang(user_id: str) -> str | None:
    """Returns cached or explicit lang (used where interaction is unavailable, e.g. on_voice_state_update)."""
    return _get_lang_cache().get(user_id, {}).get("lang")

def _get_explicit_lang(user_id: str) -> str | None:
    """Returns lang only if the user explicitly set it — None means auto mode."""
    entry = _get_lang_cache().get(user_id, {})
    lang  = entry.get("lang")
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
    atomic_write_json(USERS_FILE, data)
    _get_lang_cache()[user_id] = {"lang": lang, "lang_explicit": explicit}
    log.debug(f"User lang saved: {user_id} -> {lang} (explicit={explicit})")

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
    atomic_write_json(USERS_FILE, data)
    cache = _get_lang_cache()
    if user_id in cache:
        cache[user_id]["lang_explicit"] = False
    log.debug(f"User lang reset to auto: {user_id}")

def detect_lang(interaction: discord.Interaction) -> str:
    user_id = str(interaction.user.id)
    code    = str(interaction.locale).split("-")[0]
    discord_lang = code if code in _VALID_LANGS else DEFAULT_LANG

    entry = _get_lang_cache().get(user_id, {})
    saved = entry.get("lang")
    if entry.get("lang_explicit") and saved in _VALID_LANGS:
        return saved

    if saved != discord_lang:
        _save_user_lang(user_id, discord_lang, explicit=False)
    return discord_lang

# ─────────────────────────────────────────────
#  SETUP
# ─────────────────────────────────────────────
# Language selection for users lives in the /menu (menu.py); this extension
# only provides the shared helpers and registers the command translator.

async def setup(bot: commands.Bot):
    await bot.tree.set_translator(LocaleTranslator())
