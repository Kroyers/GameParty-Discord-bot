import discord
from discord import app_commands
from discord.ext import commands
from lang import detect_lang, atomic_write_json, t
import datetime
import logging
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────
#  SETTINGS
# ─────────────────────────────────────────────

CONFIG_FILE      = os.path.join(SCRIPT_DIR, "config.json")
SEARCH_LIMIT = 20

INTRO_MESSAGE = (
    "🇨🇿 **Ahoj všichni!** Hlasujte o všem co jste ochotný s námi hrát příští víkend. Tato emoji: 👥 znamená maximální počet hráčů.\n\n"
    "🇸🇰 **Ahojte všetci!** Hlasujte o všetkom, čo ste ochotní s nami hrať budúci víkend. Tieto emoji 👥 znamenajú maximálny počet hráčov.\n\n"
    "🇵🇱 **Cześć wszystkim!** Głosujcie na wszystko, w co jesteście chętni z nami zagrać w przyszły weekend. Te emoji 👥 oznaczają maksymalną liczbę graczy.\n\n"
    "🇬🇧 **Hi, everyone!** Vote for everything you're willing to play with us next weekend. These emojis 👥 mean the maximum number of players."
)

POLL_ANSWERS_1 = [
    ("🧑‍🚀", "LOCKDOWN Protocol (👥 16)"),
    ("🧌",   "Witch It (👥 16)"),
    ("🛒",   "Supermarket Together (👥 16)"),
    ("🦆",   "Goose Goose Duck (👥 16)"),
    ("⛳",   "Golf With Your Friends (👥 12)"),
    ("🕵️",  "Deceive Inc. (👥 12)"),
    ("🤖",   "R.E.P.O. (👥 10)"),
    ("🔦",   "Lethal Company (👥 10)"),
    ("🐶",   "Party Animals (👥 8)"),
    ("⛳",   "Super Battle Golf (👥 8)"),
]

POLL_ANSWERS_2 = [
    ("🎴",  "Tabletop Simulator (👥 8)"),
    ("🔫",  "Pummel Party (👥 8)"),
    ("❄️",  "Project Winter (👥 8)"),
    ("🦊",  "Liar's Bar (👥 4)"),
    ("🔫",  "Buckshot Roulette (👥 4)"),
    ("👻",  "Phasmophobia (👥 4)"),
    ("🤖",  "Risk of Rain (👥 4)"),
    ("⛰️", "PEAK (👥 4)"),
    ("⛓️", "Chained Together (👥 4)"),
    ("❓",  "Jiné/Iné/Inne/Other (👥 ?)"),
]

POLL_ANSWERS_START = [
    ("🕓", "16:00 - Pátek/Piatok/Piątek/Friday"),
    ("🕕", "18:00 - Pátek/Piątek/Friday"),
    ("🕓", "16:00 - Sobota/Saturday"),
    ("🕕", "18:00 - Sobota/Saturday"),
    ("🕓", "16:00 - Neděle/Niedziela/Sunday"),
    ("🕕", "18:00 - Neděle/Nedeľa/Niedziela/Sunday"),
]

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  HELPERS  (t/LOCALES live in lang.py)
# ─────────────────────────────────────────────

def role_mention(role: discord.Role) -> str:
    return "@everyone" if role.is_default() else role.mention

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(data: dict) -> None:
    atomic_write_json(CONFIG_FILE, data)

def next_day_16(days: int) -> tuple[str, int]:
    """Returns (title, hours_until_target_day_16:00) using local clock."""
    now        = datetime.datetime.now()
    target     = (now + datetime.timedelta(days=days)).date()
    end_time   = datetime.datetime(target.year, target.month, target.day, 16, 0, 0)
    hours      = max(1, round((end_time - now).total_seconds() / 3600))
    title      = f"{target.day}.{target.month}.{target.year}"
    return title, hours


def next_weekend() -> tuple[str, int, datetime.datetime]:
    """Returns (title, utc_offset, end_time) for the next weekend."""
    now  = datetime.datetime.now()
    year = now.year

    def last_sunday(month):
        d = datetime.date(year, month, 31 if month in (3, 10) else 30)
        while d.weekday() != 6:
            d -= datetime.timedelta(days=1)
        return d

    dst_start  = datetime.datetime(year, 3,  last_sunday(3).day,  2)
    dst_end    = datetime.datetime(year, 10, last_sunday(10).day, 3)
    utc_offset = 2 if dst_start <= now < dst_end else 1

    wd             = now.weekday()
    days_to_friday = 4 - wd if wd <= 3 else 4 - wd + 7
    friday         = (now + datetime.timedelta(days=days_to_friday)).date()
    sunday         = friday + datetime.timedelta(days=2)
    end_time       = datetime.datetime(friday.year, friday.month, friday.day)

    title = f"{friday.day}.{friday.month}.-{sunday.day}.{sunday.month}.{sunday.year}"
    return title, utc_offset, end_time


async def delete_old_messages(channel, bot_id):
    """Deletes the bot's previous poll messages (polls, intro embed with its
    thread, plain ping messages) and locks the threads. Other bot messages in
    the channel (e.g. the daily guess game) are left untouched."""
    deleted = 0
    async for msg in channel.history(limit=200):
        if msg.author.id != bot_id:
            continue
        if msg.poll is None and msg.thread is None and msg.embeds:
            continue
        if msg.thread is not None:
            try:
                await msg.thread.edit(locked=True, archived=False)
                log.info(f"Thread '{msg.thread.name}' locked.")
            except Exception as e:
                log.warning(f"Could not lock thread {msg.thread.id}: {e}")
        try:
            await msg.delete()
            deleted += 1
        except discord.errors.NotFound:
            pass
        except Exception as e:
            log.warning(f"Could not delete message {msg.id}: {e}")
    log.info(f"Deleted {deleted} bot messages.")

# ─────────────────────────────────────────────
#  CONFIRMATION VIEW
# ─────────────────────────────────────────────

class ConfirmView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=30)
        self.confirmed = False
        self.lang = lang
        self.children[0].label = t(lang, "confirm_btn")
        self.children[1].label = t(lang, "cancel_btn")

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(content=f"❌ {t(self.lang, 'cancelled')}", view=None)
        self.stop()

# ─────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────

class PollCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        cfg = load_config()
        self.poll_channel_id = cfg.get("poll_channel_id")
        self.role_id         = cfg.get("poll_role_id")
        self.noti_role_id    = cfg.get("poll_noti_role_id")

    @app_commands.command(name="poll", description=app_commands.locale_str("Send weekend game polls", key="cmd_poll"))
    @app_commands.describe(ping=app_commands.locale_str("Mention the role (default: True)", key="cmd_poll_ping"))
    @app_commands.default_permissions(administrator=True)
    async def poll(self, interaction: discord.Interaction, ping: bool = True):
        lang = detect_lang(interaction)
        channel = self.bot.get_channel(self.poll_channel_id)
        if channel is None:
            await interaction.response.send_message(f"❌ {t(lang, 'err_poll_no_channel')}", ephemeral=True)
            return

        view = ConfirmView(lang)
        await interaction.response.send_message(
            t(lang, "poll_confirm", channel=channel.mention), view=view, ephemeral=True
        )
        await view.wait()
        if not view.confirmed:
            return

        await interaction.edit_original_response(content=f"⏳ {t(lang, 'poll_sending')}", view=None)

        title, utc_offset, end_time = next_weekend()
        hours = max(1, round((end_time - datetime.datetime.now()).total_seconds() / 3600))

        await delete_old_messages(channel, self.bot.user.id)
        if ping and self.role_id:
            if self.role_id == channel.guild.id:
                await channel.send("@everyone", allowed_mentions=discord.AllowedMentions(everyone=True))
            else:
                role = channel.guild.get_role(self.role_id)
                mention = role.mention if role else f"<@&{self.role_id}>"
                await channel.send(mention)

        embed = discord.Embed(
            title=title,
            description=INTRO_MESSAGE,
            color=discord.Color.blurple(),
        )
        embed_msg = await channel.send(embed=embed)
        log.info(f"Embed sent (ID: {embed_msg.id})")

        thread = await embed_msg.create_thread(
            name=f"💬 Chat - {title}",
            auto_archive_duration=4320,
        )
        if ping and self.noti_role_id:
            if self.noti_role_id == channel.guild.id:
                ping_msg = await thread.send("@everyone", allowed_mentions=discord.AllowedMentions(everyone=True))
            else:
                noti_role = channel.guild.get_role(self.noti_role_id)
                noti_mention = noti_role.mention if noti_role else f"<@&{self.noti_role_id}>"
                ping_msg = await thread.send(noti_mention)
            await ping_msg.delete()
        log.info(f"Thread created: '{thread.name}' (ID: {thread.id})")

        polls = [
            {"question": "(1/2)",                    "answers": POLL_ANSWERS_1},
            {"question": "(2/2)",                    "answers": POLL_ANSWERS_2},
            {"question": f"Start (UTC+{utc_offset})", "answers": POLL_ANSWERS_START},
        ]
        for p in polls:
            discord_poll = discord.Poll(
                question=p["question"],
                duration=datetime.timedelta(hours=hours),
                multiple=True,
            )
            for emoji, text in p["answers"]:
                discord_poll.add_answer(text=text, emoji=emoji)
            msg = await channel.send(poll=discord_poll)
            log.info(f"Poll sent: '{p['question']}' (ID: {msg.id})")

        log.info(f"All polls sent. Running for {hours} hours.")
        await interaction.edit_original_response(content=f"✅ {t(lang, 'poll_sent')}")

    @app_commands.command(name="poll-day", description=app_commands.locale_str("Send game polls until 16:00 in N days", key="cmd_poll_day"))
    @app_commands.describe(
        days=app_commands.locale_str("Number of days until the poll closes at 16:00", key="cmd_poll_day_days"),
        ping=app_commands.locale_str("Mention the role (default: True)", key="cmd_poll_ping"),
    )
    @app_commands.default_permissions(administrator=True)
    async def poll_day(self, interaction: discord.Interaction, days: int, ping: bool = True):
        lang = detect_lang(interaction)
        channel = self.bot.get_channel(self.poll_channel_id)
        if channel is None:
            await interaction.response.send_message(f"❌ {t(lang, 'err_poll_no_channel')}", ephemeral=True)
            return

        view = ConfirmView(lang)
        await interaction.response.send_message(
            t(lang, "poll_confirm", channel=channel.mention), view=view, ephemeral=True
        )
        await view.wait()
        if not view.confirmed:
            return

        await interaction.edit_original_response(content=f"⏳ {t(lang, 'poll_sending')}", view=None)

        title, hours = next_day_16(days)

        await delete_old_messages(channel, self.bot.user.id)
        if ping and self.role_id:
            if self.role_id == channel.guild.id:
                await channel.send("@everyone", allowed_mentions=discord.AllowedMentions(everyone=True))
            else:
                role = channel.guild.get_role(self.role_id)
                mention = role.mention if role else f"<@&{self.role_id}>"
                await channel.send(mention)

        embed = discord.Embed(
            title=title,
            description=INTRO_MESSAGE,
            color=discord.Color.blurple(),
        )
        embed_msg = await channel.send(embed=embed)
        log.info(f"Embed sent (ID: {embed_msg.id})")

        thread = await embed_msg.create_thread(
            name=f"💬 Chat - {title}",
            auto_archive_duration=4320,
        )
        if ping and self.noti_role_id:
            if self.noti_role_id == channel.guild.id:
                ping_msg = await thread.send("@everyone", allowed_mentions=discord.AllowedMentions(everyone=True))
            else:
                noti_role = channel.guild.get_role(self.noti_role_id)
                noti_mention = noti_role.mention if noti_role else f"<@&{self.noti_role_id}>"
                ping_msg = await thread.send(noti_mention)
            await ping_msg.delete()
        log.info(f"Thread created: '{thread.name}' (ID: {thread.id})")

        polls = [
            {"question": "(1/2)", "answers": POLL_ANSWERS_1},
            {"question": "(2/2)", "answers": POLL_ANSWERS_2},
        ]
        for p in polls:
            discord_poll = discord.Poll(
                question=p["question"],
                duration=datetime.timedelta(hours=hours),
                multiple=True,
            )
            for emoji, text in p["answers"]:
                discord_poll.add_answer(text=text, emoji=emoji)
            msg = await channel.send(poll=discord_poll)
            log.info(f"Poll sent: '{p['question']}' (ID: {msg.id})")

        log.info(f"Day polls sent. Running for {hours} hours (until {title} 16:00).")
        await interaction.edit_original_response(content=f"✅ {t(lang, 'poll_sent')}")

    @app_commands.command(name="poll-end", description=app_commands.locale_str("End all active polls in the poll channel", key="cmd_poll_end"))
    @app_commands.default_permissions(administrator=True)
    async def poll_end(self, interaction: discord.Interaction):
        lang = detect_lang(interaction)
        channel = self.bot.get_channel(self.poll_channel_id)
        if channel is None:
            await interaction.response.send_message(f"❌ {t(lang, 'err_poll_no_channel')}", ephemeral=True)
            return

        view = ConfirmView(lang)
        await interaction.response.send_message(
            t(lang, "poll_end_confirm", channel=channel.mention), view=view, ephemeral=True
        )
        await view.wait()
        if not view.confirmed:
            return

        await interaction.edit_original_response(content=f"⏳ {t(lang, 'poll_ending')}", view=None)

        ended = 0
        skipped = 0
        async for msg in channel.history(limit=SEARCH_LIMIT):
            if msg.poll is None:
                continue
            if msg.poll.is_finalised():
                skipped += 1
                continue
            try:
                await msg.end_poll()
                log.info(f"Poll ended: '{msg.poll.question}' (ID: {msg.id})")
                ended += 1
            except Exception as e:
                log.error(f"Could not end poll '{msg.poll.question}': {e}")

        log.info(f"Done. Ended: {ended}, skipped: {skipped}.")
        await interaction.edit_original_response(content=f"✅ {t(lang, 'poll_ended', ended=ended, skipped=skipped)}")

    @app_commands.command(name="poll-set", description=app_commands.locale_str("Set the poll channel", key="cmd_poll_set"))
    @app_commands.describe(channel=app_commands.locale_str("Channel to use (leave empty for current channel)", key="cmd_poll_set_channel"))
    @app_commands.default_permissions(administrator=True)
    async def poll_set(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        lang = detect_lang(interaction)
        target = channel or interaction.channel
        self.poll_channel_id = target.id
        cfg = load_config()
        cfg["poll_channel_id"] = target.id
        save_config(cfg)

        await interaction.response.send_message(
            f"✅ {t(lang, 'poll_channel_set', channel=target.mention)}", ephemeral=True
        )
        log.info(f"Poll channel set to #{target.name} ({target.id}) by {interaction.user}.")

    @app_commands.command(name="poll-role", description=app_commands.locale_str("Set the role to mention when polls are sent", key="cmd_poll_role"))
    @app_commands.describe(role=app_commands.locale_str("Role to mention", key="cmd_poll_role_param"))
    @app_commands.default_permissions(administrator=True)
    async def poll_role(self, interaction: discord.Interaction, role: discord.Role):
        lang = detect_lang(interaction)
        self.role_id = role.id
        cfg = load_config()
        cfg["poll_role_id"] = role.id
        save_config(cfg)

        await interaction.response.send_message(
            f"✅ {t(lang, 'poll_role_set', role=role_mention(role))}", ephemeral=True
        )
        log.info(f"Poll role set to {role.name} ({role.id}) by {interaction.user}.")

    @app_commands.command(name="poll-noti", description=app_commands.locale_str("Set role to add to poll thread", key="cmd_poll_noti"))
    @app_commands.describe(role=app_commands.locale_str("Role whose members will be added to the thread", key="cmd_poll_noti_param"))
    @app_commands.default_permissions(administrator=True)
    async def poll_noti(self, interaction: discord.Interaction, role: discord.Role):
        lang = detect_lang(interaction)
        self.noti_role_id = role.id
        cfg = load_config()
        cfg["poll_noti_role_id"] = role.id
        save_config(cfg)

        await interaction.response.send_message(
            f"✅ {t(lang, 'poll_noti_set', role=role_mention(role))}", ephemeral=True
        )
        log.info(f"Poll noti role set to {role.name} ({role.id}) by {interaction.user}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(PollCog(bot))
