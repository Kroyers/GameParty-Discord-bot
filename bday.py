import discord
from discord import app_commands
from discord.ext import commands, tasks
from cryptography.fernet import Fernet
from lang import detect_lang, _save_user_lang, clear_user_lang, _get_user_lang, _get_explicit_lang
import json
import datetime
import os
import logging

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────
#  SETTINGS
# ─────────────────────────────────────────────

USERS_FILE   = os.path.join(SCRIPT_DIR, "users.json")
KEY_FILE     = os.path.join(SCRIPT_DIR, "bday.key")
CONFIG_FILE  = os.path.join(SCRIPT_DIR, "config.json")
LOCALES_DIR  = os.path.join(SCRIPT_DIR, "locales")
DEFAULT_LANG = "en"

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  LOCALIZATION
# ─────────────────────────────────────────────

def load_locales() -> dict:
    locales = {}
    for fname in os.listdir(LOCALES_DIR):
        if fname.endswith(".json"):
            code = fname[:-5]
            with open(os.path.join(LOCALES_DIR, fname), "r", encoding="utf-8") as f:
                locales[code] = json.load(f)
    return locales

LOCALES = load_locales()



def t(lang: str, key: str, **kwargs) -> str:
    text = LOCALES.get(lang, {}).get(key)
    if not text:
        if text is None:
            log.warning(f"Missing translation: [{lang}] '{key}', falling back to '{DEFAULT_LANG}'.")
        text = LOCALES.get(DEFAULT_LANG, {}).get(key, key)
    return text.format(**kwargs) if kwargs else text


class LocaleTranslator(app_commands.Translator):
    async def translate(self, string: app_commands.locale_str, locale: discord.Locale, _context: app_commands.TranslationContext) -> str | None:
        key = string.extras.get("key")
        if not key:
            return None
        lang_code = str(locale).split("-")[0]
        return LOCALES.get(lang_code, {}).get(key)

# ─────────────────────────────────────────────
#  ENCRYPTION
# ─────────────────────────────────────────────

def load_or_create_key() -> Fernet:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return Fernet(f.read())
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    log.info("New encryption key generated (bday.key). Back up this file!")
    return Fernet(key)

fernet = load_or_create_key()

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

BDAY_KEYS = {"day", "month", "year", "last_wished"}

def load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    result = {}
    for user_id, record in raw.items():
        entry = {"lang": record.get("lang"), "lang_explicit": record.get("lang_explicit", False), "active": record.get("active", True)}
        if "bday" in record:
            try:
                bday = json.loads(fernet.decrypt(record["bday"].encode()))
                entry.update(bday)
            except Exception:
                log.warning(f"Failed to decrypt bday for user {user_id}, skipping.")
        result[user_id] = entry
    return result


def save_users(data: dict) -> None:
    output = {}
    for user_id, entry in data.items():
        record = {}
        if entry.get("lang"):
            record["lang"] = entry["lang"]
        if entry.get("lang_explicit"):
            record["lang_explicit"] = True
        if not entry.get("active", True):
            record["active"] = False
        bday_data = {k: entry[k] for k in BDAY_KEYS if k in entry}
        if bday_data:
            record["bday"] = fernet.encrypt(json.dumps(bday_data).encode()).decode()
        output[user_id] = record
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

# ─────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────



class BdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bday_channel_id = load_config().get("bday_channel_id")

    async def cog_load(self):
        self.check_birthdays.start()

    async def cog_unload(self):
        self.check_birthdays.cancel()

    # ── Events ────────────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        user_id = str(member.id)
        users = load_users()
        if user_id in users:
            users[user_id]["active"] = False
            save_users(users)
            log.info(f"Birthday deactivated for user {user_id} (left server).")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        user_id = str(member.id)
        users = load_users()
        if user_id in users:
            users[user_id]["active"] = True
            save_users(users)
            log.info(f"Birthday reactivated for user {user_id} (rejoined server).")

    # ── Commands ──────────────────────────────

    @app_commands.command(
        name="bday",
        description=app_commands.locale_str("Register your birthday", key="cmd_bday"),
    )
    @app_commands.describe(
        day=app_commands.locale_str("Day of birth (1–31)",       key="cmd_bday_day"),
        month=app_commands.locale_str("Month of birth (1–12)",   key="cmd_bday_month"),
        year=app_commands.locale_str("Year of birth (optional)", key="cmd_bday_year"),
    )
    @app_commands.rename(
        day=app_commands.locale_str("day",   key="param_day"),
        month=app_commands.locale_str("month", key="param_month"),
        year=app_commands.locale_str("year",   key="param_year"),
    )
    async def bday(self, interaction: discord.Interaction, day: int, month: int, year: int | None = None):
        users = load_users()
        user_id = str(interaction.user.id)
        lang = detect_lang(interaction)

        if not (1 <= day <= 31):
            await interaction.response.send_message(f"❌ {t(lang, 'err_day')}", ephemeral=True)
            return
        if not (1 <= month <= 12):
            await interaction.response.send_message(f"❌ {t(lang, "err_month")}", ephemeral=True)
            return

        check_year = year or 2000
        try:
            datetime.date(check_year, month, day)
        except ValueError:
            await interaction.response.send_message(
                f"❌ {t(lang, 'err_date', day=day, month=month, year=check_year)}"
            )
            return

        if year is not None:
            if not (1900 <= year <= datetime.date.today().year):
                await interaction.response.send_message(
                    f"❌ {t(lang, "err_year", year=datetime.date.today().year)}", ephemeral=True
                )
                return

        users[user_id] = {
            "day": day,
            "month": month,
            "year": year,
            "lang": lang,
            "last_wished": users.get(user_id, {}).get("last_wished"),
        }
        save_users(users)

        year_str = f".{year}" if year else ""
        await interaction.response.send_message(
            f"✅ {t(lang, "saved", date=f"{day}.{month}.{year_str}")}", ephemeral=True
        )
        log.info(f"Birthday saved for {interaction.user} ({user_id}) [{lang}]")

    @app_commands.command(
        name="bday-remove",
        description=app_commands.locale_str("Remove your saved birthday", key="cmd_bday_remove"),
    )
    async def bday_remove(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        users = load_users()
        lang = detect_lang(interaction)

        if "day" not in users.get(user_id, {}):
            await interaction.response.send_message(f"❌ {t(lang, "bday_not_found")}", ephemeral=True)
            return

        for key in BDAY_KEYS:
            users[user_id].pop(key, None)

        if not any(users[user_id].values()):
            del users[user_id]

        save_users(users)
        await interaction.response.send_message(f"✅ {t(lang, "bday_removed")}", ephemeral=True)
        log.info(f"Birthday removed for {interaction.user} ({user_id}).")

    @app_commands.command(
        name="bday-set",
        description=app_commands.locale_str("Set the birthday announcement channel", key="cmd_bday_set"),
    )
    @app_commands.describe(
        channel=app_commands.locale_str("Channel to use (leave empty for current channel)", key="cmd_bday_set_channel"),
    )
    @app_commands.default_permissions(administrator=True)
    async def bday_set(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        user_id = str(interaction.user.id)
        users = load_users()
        lang = detect_lang(interaction)

        target = channel or interaction.channel
        self.bday_channel_id = target.id
        cfg = load_config()
        cfg["bday_channel_id"] = target.id
        save_config(cfg)

        await interaction.response.send_message(
            f"✅ {t(lang, "channel_set", channel=target.mention)}", ephemeral=True
        )
        log.info(f"Birthday channel set to #{target.name} ({target.id}) by {interaction.user}.")

    # ── Task ──────────────────────────────────

    @tasks.loop(hours=1)
    async def check_birthdays(self):
        now = datetime.datetime.now()
        if now.hour != 0:
            return

        channel = self.bot.get_channel(self.bday_channel_id)
        if channel is None:
            log.warning(f"Birthday channel (ID {self.bday_channel_id}) not found.")
            return

        users = load_users()
        today = now.date()
        changed = False

        for user_id, data in users.items():
            if not data.get("active", True):
                continue
            if "day" not in data or "month" not in data:
                continue
            if today.day != data["day"] or today.month != data["month"]:
                continue
            if data.get("last_wished") == today.year:
                continue

            lang = data.get("lang", DEFAULT_LANG)
            if data.get("year"):
                age = today.year - data["year"]
                message = f"🎂 {t(lang, "bday_wish_age", mention=f"<@{user_id}>", age=age)} 🎉🎁"
            else:
                message = f"🎂 {t(lang, "bday_wish", mention=f"<@{user_id}>")} 🎉🎁"

            try:
                await channel.send(message)
                users[user_id]["last_wished"] = today.year
                changed = True
                log.info(f"Birthday wish sent for user {user_id}.")
            except Exception as e:
                log.error(f"Error sending birthday wish for {user_id}: {e}")

        if changed:
            save_users(users)


async def setup(bot: commands.Bot):
    await bot.add_cog(BdayCog(bot))
    await bot.tree.set_translator(LocaleTranslator())
