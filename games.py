import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import random
import logging
from lang import detect_lang

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
LOCALES_DIR = os.path.join(SCRIPT_DIR, "locales")

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
#  ROCK PAPER SCISSORS
# ─────────────────────────────────────────────

ROCK, PAPER, SCISSORS = "rock", "paper", "scissors"
_EMOJI = {ROCK: "✊", PAPER: "🖐️", SCISSORS: "✌️"}
_BEATS = {ROCK: SCISSORS, PAPER: ROCK, SCISSORS: PAPER}

_games: dict[int, dict] = {}


async def _resolve_rps(client: discord.Client, msg_id: int) -> None:
    game = _games.pop(msg_id, None)
    if not game:
        return
    ch = client.get_channel(game["channel_id"])
    if not ch:
        return
    try:
        msg = await ch.fetch_message(msg_id)
    except Exception:
        return

    lang   = game.get("lang", "en")
    c_id   = game["challenger_id"]
    o_id   = game["opponent_id"]
    c_pick = game["challenger_pick"]
    o_pick = game["opponent_pick"]
    c_str  = f"{_EMOJI[c_pick]} {t(lang, f'rps_{c_pick}')}"
    o_str  = f"{_EMOJI[o_pick]} {t(lang, f'rps_{o_pick}')}"

    if c_pick == o_pick:
        result = f"🤝 {t(lang, 'rps_result_draw', pick=c_str)}"
    elif _BEATS[c_pick] == o_pick:
        result = f"🏆 {t(lang, 'rps_result_win', winner=f'<@{c_id}>', winner_pick=c_str, loser_pick=o_str)}"
    else:
        result = f"🏆 {t(lang, 'rps_result_win', winner=f'<@{o_id}>', winner_pick=o_str, loser_pick=c_str)}"

    embed = discord.Embed(
        title=t(lang, "rps_title"),
        description=f"<@{c_id}> {_EMOJI[c_pick]} **vs** {_EMOJI[o_pick]} <@{o_id}>\n\n{result}",
        color=discord.Color.gold(),
    )
    await msg.edit(embed=embed, view=None)
    log.info(f"RPS resolved (msg {msg_id}): <@{c_id}> {c_pick} vs {o_pick} <@{o_id}>")


class RpsChoiceView(discord.ui.View):
    """Ephemeral view for one player to pick rock/paper/scissors."""

    def __init__(self, msg_id: int, player_id: int, lang: str):
        super().__init__(timeout=120)
        self.msg_id    = msg_id
        self.player_id = player_id
        self.lang      = lang

    async def _pick(self, interaction: discord.Interaction, choice: str) -> None:
        lang = detect_lang(interaction)
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(t(lang, "rps_not_your_game"), ephemeral=True)
            return
        game = _games.get(self.msg_id)
        if not game:
            await interaction.response.edit_message(content="❌", view=None)
            return
        role_key = "challenger_pick" if interaction.user.id == game["challenger_id"] else "opponent_pick"
        if game[role_key]:
            await interaction.response.send_message(t(lang, "rps_already_picked"), ephemeral=True)
            return
        game[role_key] = choice
        self.stop()
        await interaction.response.edit_message(content=f"✅ {t(lang, 'rps_waiting')}", view=None)
        if game["challenger_pick"] and game["opponent_pick"]:
            await _resolve_rps(interaction.client, self.msg_id)

    @discord.ui.button(emoji="✊", style=discord.ButtonStyle.grey)
    async def rock(self, i: discord.Interaction, _: discord.ui.Button):
        await self._pick(i, ROCK)

    @discord.ui.button(emoji="🖐️", style=discord.ButtonStyle.grey)
    async def paper(self, i: discord.Interaction, _: discord.ui.Button):
        await self._pick(i, PAPER)

    @discord.ui.button(emoji="✌️", style=discord.ButtonStyle.grey)
    async def scissors(self, i: discord.Interaction, _: discord.ui.Button):
        await self._pick(i, SCISSORS)


class RpsPickView(discord.ui.View):
    """Shown on the channel message after opponent accepts — both players click Pick."""

    def __init__(self, challenger_id: int, opponent_id: int, lang: str, msg: discord.Message):
        super().__init__(timeout=120)
        self.challenger_id = challenger_id
        self.opponent_id   = opponent_id
        self.lang          = lang
        self.msg           = msg
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.label = t(lang, "rps_pick_btn")

    @discord.ui.button(style=discord.ButtonStyle.green)
    async def pick(self, interaction: discord.Interaction, _: discord.ui.Button):
        lang = detect_lang(interaction)
        if interaction.user.id not in (self.challenger_id, self.opponent_id):
            await interaction.response.send_message(t(lang, "rps_not_your_game"), ephemeral=True)
            return
        if not _games.get(self.msg.id):
            await interaction.response.send_message("❌", ephemeral=True)
            return
        view = RpsChoiceView(self.msg.id, interaction.user.id, lang)
        await interaction.response.send_message(
            content=f"🎯 {t(lang, 'rps_choose')}",
            view=view,
            ephemeral=True,
        )

    async def on_timeout(self):
        if _games.pop(self.msg.id, None) is None:
            return
        try:
            await self.msg.edit(content=t(self.lang, "rps_timeout"), embed=None, view=None)
        except Exception:
            pass


class RpsAcceptView(discord.ui.View):
    """Initial challenge message — only the opponent can accept or decline."""

    def __init__(self, challenger_id: int, opponent_id: int, lang: str):
        super().__init__(timeout=60)
        self.challenger_id = challenger_id
        self.opponent_id   = opponent_id
        self.lang          = lang
        self._msg: discord.Message | None = None
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "rps_accept":
                    child.label = t(lang, "rps_accept_btn")
                elif child.custom_id == "rps_decline":
                    child.label = t(lang, "rps_decline_btn")

    @discord.ui.button(style=discord.ButtonStyle.green, custom_id="rps_accept")
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message(t(detect_lang(interaction), "rps_not_your_game"), ephemeral=True)
            return
        self.stop()
        embed = discord.Embed(
            title=t(self.lang, "rps_title"),
            description=t(self.lang, "rps_pick_prompt",
                          challenger=f"<@{self.challenger_id}>",
                          opponent=f"<@{self.opponent_id}>"),
            color=discord.Color.blurple(),
        )
        pick_view = RpsPickView(self.challenger_id, self.opponent_id, self.lang, interaction.message)
        await interaction.response.edit_message(content=None, embed=embed, view=pick_view)

    @discord.ui.button(style=discord.ButtonStyle.red, custom_id="rps_decline")
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message(t(detect_lang(interaction), "rps_not_your_game"), ephemeral=True)
            return
        if self._msg:
            _games.pop(self._msg.id, None)
        self.stop()
        await interaction.response.edit_message(
            content=t(self.lang, "rps_declined", opponent=interaction.user.mention),
            embed=None, view=None,
        )

    async def on_timeout(self):
        if self._msg:
            _games.pop(self._msg.id, None)
            try:
                await self._msg.edit(content=t(self.lang, "rps_timeout"), embed=None, view=None)
            except Exception:
                pass

# ─────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────

class GamesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="rps",
        description=app_commands.locale_str("Challenge someone to Rock Paper Scissors", key="cmd_rps"),
    )
    @app_commands.describe(
        opponent=app_commands.locale_str("Player to challenge", key="cmd_rps_opponent"),
    )
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.user.id)
    async def rps(self, interaction: discord.Interaction, opponent: discord.Member):
        lang = detect_lang(interaction)

        if opponent.bot:
            await interaction.response.send_message(t(lang, "rps_err_bot"), ephemeral=True)
            return
        if opponent.id == interaction.user.id:
            await interaction.response.send_message(t(lang, "rps_err_self"), ephemeral=True)
            return

        embed = discord.Embed(
            title=t(lang, "rps_title"),
            description=t(lang, "rps_challenge",
                          challenger=interaction.user.mention,
                          opponent=opponent.mention),
            color=discord.Color.blurple(),
        )
        view = RpsAcceptView(interaction.user.id, opponent.id, lang)
        await interaction.response.send_message(embed=embed, view=view)

        msg = await interaction.original_response()
        view._msg = msg
        _games[msg.id] = {
            "challenger_id":   interaction.user.id,
            "opponent_id":     opponent.id,
            "channel_id":      interaction.channel_id,
            "challenger_pick": None,
            "opponent_pick":   None,
            "lang":            lang,
        }
        log.info(f"RPS challenge: {interaction.user} -> {opponent} (msg {msg.id})")

    @app_commands.command(
        name="roll",
        description=app_commands.locale_str("Roll a random number", key="cmd_roll"),
    )
    @app_commands.describe(
        maximum=app_commands.locale_str("Maximum value (default: 100)", key="cmd_roll_max"),
    )
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.user.id)
    async def roll(self, interaction: discord.Interaction, maximum: int = 100):
        lang = detect_lang(interaction)
        if maximum < 2:
            await interaction.response.send_message(t(lang, "roll_err_min"), ephemeral=True)
            return
        result = random.randint(1, maximum)
        await interaction.response.send_message(
            f"🎲 {t(lang, 'roll_result', user=interaction.user.display_name, result=result, max=maximum)}"
        )
        log.info(f"Roll: {interaction.user} rolled {result} (1–{maximum})")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            lang = detect_lang(interaction)
            retry = round(error.retry_after)
            await interaction.response.send_message(t(lang, "rps_cooldown", seconds=retry), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GamesCog(bot))
