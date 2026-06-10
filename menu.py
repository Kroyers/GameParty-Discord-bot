import discord
from discord import app_commands
from discord.ext import commands
import datetime
import os
import re
import logging
from lang import detect_lang, t, LOCALES, _get_explicit_lang, _save_user_lang, clear_user_lang
from bday import validate_bday, save_bday, remove_bday, format_bday, has_bday, get_bday
from voice import _LangSelect, load_voice_data, update_control_panel

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger(__name__)


def _bot_version() -> str:
    # VERSION lives in main.py (the auto-updater depends on it being there),
    # and importing main would start the bot — so read it from the file.
    try:
        with open(os.path.join(SCRIPT_DIR, "main.py"), "r", encoding="utf-8") as f:
            match = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
        return match.group(1) if match else "?"
    except Exception:
        return "?"


def _menu_embed(lang: str, status: str | None = None) -> discord.Embed:
    """Main menu embed; a status line (confirmation/error) shows above the description."""
    desc = t(lang, "menu_desc")
    if status:
        desc = f"{status}\n\n{desc}"
    return discord.Embed(
        title=t(lang, "menu_title"),
        description=desc,
        color=discord.Color.blurple(),
    )


async def _refresh_menu(interaction: discord.Interaction, lang: str, status: str | None = None) -> None:
    """Edits the current ephemeral message back to the main menu."""
    view = MenuView(lang, show_bday_remove=has_bday(str(interaction.user.id)))
    await interaction.response.edit_message(embed=_menu_embed(lang, status), view=view)


async def _menu_status(interaction: discord.Interaction, lang: str, status: str) -> None:
    """Shows a status inside the menu; falls back to a plain message when
    the menu message is not available."""
    if interaction.message:
        await _refresh_menu(interaction, lang, status)
    else:
        await interaction.response.send_message(status, ephemeral=True)


class _BackButton(discord.ui.Button):
    def __init__(self, lang: str, custom_id: str, row: int = 1):
        super().__init__(
            label=t(lang, "menu_btn_back"), style=discord.ButtonStyle.grey,
            emoji="◀️", custom_id=custom_id, row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await _refresh_menu(interaction, detect_lang(interaction))

# ─────────────────────────────────────────────
#  BIRTHDAY — MODAL + DELETE CONFIRMATION
# ─────────────────────────────────────────────

class BdayMenuModal(discord.ui.Modal):
    def __init__(self, lang: str, current: dict | None = None):
        super().__init__(title=t(lang, "menu_bday_modal_title"))
        current = current or {}
        self.day = discord.ui.TextInput(
            label=t(lang, "menu_bday_day"), placeholder="1–31",
            min_length=1, max_length=2,
            default=str(current["day"]) if current.get("day") else None,
        )
        self.month = discord.ui.TextInput(
            label=t(lang, "menu_bday_month"), placeholder="1–12",
            min_length=1, max_length=2,
            default=str(current["month"]) if current.get("month") else None,
        )
        self.year = discord.ui.TextInput(
            label=t(lang, "menu_bday_year"), placeholder="1990",
            required=False, max_length=4,
            default=str(current["year"]) if current.get("year") else None,
        )
        self.add_item(self.day)
        self.add_item(self.month)
        self.add_item(self.year)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        lang = detect_lang(interaction)

        try:
            day = int(self.day.value.strip())
        except ValueError:
            await _menu_status(interaction, lang, f"❌ {t(lang, 'err_day')}")
            return
        try:
            month = int(self.month.value.strip())
        except ValueError:
            await _menu_status(interaction, lang, f"❌ {t(lang, 'err_month')}")
            return

        year = None
        year_raw = self.year.value.strip()
        if year_raw:
            try:
                year = int(year_raw)
            except ValueError:
                await _menu_status(interaction, lang, f"❌ {t(lang, 'err_year', year=datetime.date.today().year)}")
                return

        err = validate_bday(day, month, year)
        if err:
            key, kwargs = err
            await _menu_status(interaction, lang, f"❌ {t(lang, key, **kwargs)}")
            return

        save_bday(str(interaction.user.id), lang, day, month, year)
        await _menu_status(interaction, lang, f"✅ {t(lang, 'saved', date=format_bday(day, month, year))}")
        log.info(f"Birthday saved via menu for {interaction.user}.")


class BdayRemoveConfirmView(discord.ui.View):
    def __init__(self, lang: str = "en"):
        super().__init__(timeout=None)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "menu_bday_confirm":
                child.label = t(lang, "confirm_btn")
        self.add_item(_BackButton(lang, "menu_bday_cancel", row=0))

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.red, emoji="🗑️", custom_id="menu_bday_confirm", row=0)
    async def confirm_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        lang    = detect_lang(interaction)
        removed = remove_bday(str(interaction.user.id))
        if removed:
            status = f"✅ {t(lang, 'bday_removed')}"
            log.info(f"Birthday removed via menu by {interaction.user}.")
        else:
            status = f"❌ {t(lang, 'bday_not_found')}"
        await _refresh_menu(interaction, lang, status)

# ─────────────────────────────────────────────
#  LANGUAGE + INFO SCREENS
# ─────────────────────────────────────────────

class _MenuLangSelect(_LangSelect):
    def __init__(self, current_lang: str | None, lang: str = "en"):
        super().__init__(current_lang, lang)
        self.custom_id = "menu_lang_select"

    async def callback(self, interaction: discord.Interaction):
        chosen  = self.values[0]
        user_id = str(interaction.user.id)
        if chosen == "auto":
            clear_user_lang(user_id)
            new_lang = detect_lang(interaction)
            note = f"✅ {t(new_lang, 'lang_reset')}"
            log.info(f"Language reset to auto via menu for {interaction.user}.")
        else:
            _save_user_lang(user_id, chosen, explicit=True)
            new_lang = chosen
            note = f"✅ {t(chosen, 'lang_changed', name=LOCALES[chosen].get('lang_name', chosen))}"
            log.info(f"Language set to '{chosen}' via menu for {interaction.user}.")

        await _refresh_menu(interaction, new_lang, note)

        # Refresh the voice control panel when the owner changes language in their room.
        data    = load_voice_data()
        ch_data = data.get(str(interaction.channel_id))
        if ch_data and user_id == ch_data.get("owner_id"):
            await update_control_panel(interaction.channel, ch_data, new_lang)


class MenuLangView(discord.ui.View):
    def __init__(self, current_lang: str | None = None, lang: str = "en"):
        super().__init__(timeout=None)
        self.add_item(_MenuLangSelect(current_lang, lang))
        self.add_item(_BackButton(lang, "menu_lang_back", row=1))


class InfoView(discord.ui.View):
    def __init__(self, lang: str = "en"):
        super().__init__(timeout=None)
        self.add_item(_BackButton(lang, "menu_info_back", row=0))

# ─────────────────────────────────────────────
#  MAIN MENU VIEW  (persistent)
# ─────────────────────────────────────────────

_MENU_LABELS = {
    "menu_bday":        "menu_btn_bday",
    "menu_bday_remove": "menu_btn_bday_remove",
    "menu_lang":        "menu_btn_lang",
    "menu_info":        "menu_btn_info",
}


class MenuView(discord.ui.View):
    def __init__(self, lang: str = "en", show_bday_remove: bool = True):
        super().__init__(timeout=None)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id in _MENU_LABELS:
                child.label = t(lang, _MENU_LABELS[child.custom_id])
        # Hidden when the user has no birthday saved. The persistent instance
        # registered in cog_load keeps the default (all buttons), so the
        # custom_id stays routable after a restart.
        if not show_bday_remove:
            self.remove_item(self.bday_remove_btn)

    @discord.ui.button(label="Birthday", style=discord.ButtonStyle.blurple, emoji="🎂", custom_id="menu_bday", row=0)
    async def bday_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        lang = detect_lang(interaction)
        await interaction.response.send_modal(
            BdayMenuModal(lang, get_bday(str(interaction.user.id)))
        )

    @discord.ui.button(label="Remove birthday", style=discord.ButtonStyle.grey, emoji="🗑️", custom_id="menu_bday_remove", row=0)
    async def bday_remove_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        lang  = detect_lang(interaction)
        embed = discord.Embed(
            title=t(lang, "menu_title"),
            description=t(lang, "menu_bday_remove_confirm"),
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=embed, view=BdayRemoveConfirmView(lang))

    @discord.ui.button(label="Language", style=discord.ButtonStyle.grey, emoji="🌐", custom_id="menu_lang", row=1)
    async def lang_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        lang    = detect_lang(interaction)
        current = _get_explicit_lang(str(interaction.user.id))
        embed   = discord.Embed(
            title=t(lang, "menu_title"),
            description=t(lang, "voice_prompt_lang"),
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=MenuLangView(current, lang))

    @discord.ui.button(label="Info", style=discord.ButtonStyle.grey, emoji="ℹ️", custom_id="menu_info", row=1)
    async def info_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        lang  = detect_lang(interaction)
        embed = discord.Embed(title=t(lang, "info_title"), color=discord.Color.blurple())
        embed.add_field(name=t(lang, "info_version"), value=_bot_version(), inline=True)
        repo = os.getenv("GITHUB_REPO")
        if repo:
            embed.add_field(name=t(lang, "info_github"), value=f"https://github.com/{repo}", inline=True)
        embed.add_field(name="—", value=t(lang, "info_features"), inline=False)
        await interaction.response.edit_message(embed=embed, view=InfoView(lang))

# ─────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────

class MenuCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Persistent registration so all menu screens keep working after a restart.
        self.bot.add_view(MenuView())
        self.bot.add_view(BdayRemoveConfirmView())
        self.bot.add_view(MenuLangView())
        self.bot.add_view(InfoView())

    @app_commands.command(
        name="menu",
        description=app_commands.locale_str("Open the interactive bot menu", key="cmd_menu"),
    )
    async def menu(self, interaction: discord.Interaction):
        lang = detect_lang(interaction)
        view = MenuView(lang, show_bday_remove=has_bday(str(interaction.user.id)))
        await interaction.response.send_message(embed=_menu_embed(lang), view=view, ephemeral=True)
        log.debug(f"/menu opened by {interaction.user}")


async def setup(bot: commands.Bot):
    await bot.add_cog(MenuCog(bot))
