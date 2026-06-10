import discord
from discord import app_commands
from discord.ext import commands, tasks
from cryptography.fernet import Fernet
from lang import detect_lang, _save_user_lang, clear_user_lang, _get_user_lang, _get_explicit_lang, atomic_write_json, t, DEFAULT_LANG
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

log = logging.getLogger(__name__)

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
    atomic_write_json(CONFIG_FILE, data)

BDAY_KEYS = {"day", "month", "year", "last_wished"}

def load_users() -> dict:
    """Loads users.json with bday fields decrypted in place. All other keys
    (rps, guess, voice, seasons_won, ...) are kept untouched so save_users
    can write them back without losing data owned by other modules."""
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    result = {}
    for user_id, record in raw.items():
        entry = dict(record)
        if "bday" in record:
            try:
                bday = json.loads(fernet.decrypt(record["bday"].encode()))
                del entry["bday"]
                entry.update(bday)
            except Exception:
                # Keep the encrypted blob so it survives the next save_users.
                log.warning(f"Failed to decrypt bday for user {user_id}, skipping.")
        result[user_id] = entry
    return result


def save_users(data: dict) -> None:
    output = {}
    for user_id, entry in data.items():
        record = {k: v for k, v in entry.items() if k not in BDAY_KEYS}
        if not record.get("lang"):
            record.pop("lang", None)
        if not record.get("lang_explicit"):
            record.pop("lang_explicit", None)
        if record.get("active", True):
            record.pop("active", None)
        bday_data = {k: entry[k] for k in BDAY_KEYS if k in entry}
        if bday_data:
            record["bday"] = fernet.encrypt(json.dumps(bday_data).encode()).decode()
        output[user_id] = record
    atomic_write_json(USERS_FILE, output)

# ─────────────────────────────────────────────
#  SHARED LOGIC  (used by /bday and the menu)
# ─────────────────────────────────────────────

def validate_bday(day: int, month: int, year: int | None) -> tuple[str, dict] | None:
    """Returns (error_locale_key, format_kwargs), or None when the date is valid."""
    if not (1 <= day <= 31):
        return "err_day", {}
    if not (1 <= month <= 12):
        return "err_month", {}
    check_year = year or 2000
    try:
        datetime.date(check_year, month, day)
    except ValueError:
        return "err_date", {"day": day, "month": month, "year": check_year}
    if year is not None and not (1900 <= year <= datetime.date.today().year):
        return "err_year", {"year": datetime.date.today().year}
    return None


def save_bday(user_id: str, lang: str, day: int, month: int, year: int | None) -> None:
    users = load_users()
    entry = users.setdefault(user_id, {})
    entry["day"]   = day
    entry["month"] = month
    entry["year"]  = year
    entry["lang"]  = lang
    save_users(users)


def get_bday(user_id: str) -> dict | None:
    """Returns {"day", "month", "year"} of the stored birthday, or None."""
    entry = load_users().get(user_id, {})
    if "day" not in entry:
        return None
    return {"day": entry["day"], "month": entry["month"], "year": entry.get("year")}


def has_bday(user_id: str) -> bool:
    """True when the user has a birthday stored (raw check, no decryption)."""
    if not os.path.exists(USERS_FILE):
        return False
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return "bday" in json.load(f).get(user_id, {})


def remove_bday(user_id: str) -> bool:
    """Removes the stored birthday. Returns False when none was saved."""
    users = load_users()
    if "day" not in users.get(user_id, {}):
        return False
    for key in BDAY_KEYS:
        users[user_id].pop(key, None)
    if not any(users[user_id].values()):
        del users[user_id]
    save_users(users)
    return True


def format_bday(day: int, month: int, year: int | None) -> str:
    return f"{day}.{month}.{year}" if year else f"{day}.{month}."

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
    # User-facing birthday registration/removal lives in the /menu (menu.py).

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

        log.debug("Running daily birthday check.")
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
