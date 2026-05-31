import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import random
import logging
from lang import detect_lang, _get_user_lang

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
LOCALES_DIR = os.path.join(SCRIPT_DIR, "locales")
STATS_FILE  = os.path.join(SCRIPT_DIR, "rps_stats.json")
_stats_lock = asyncio.Lock()

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


def _bi_title(lang_c: str, lang_o: str, key: str) -> str:
    tc = t(lang_c, key)
    to = t(lang_o, key)
    return tc if lang_c == lang_o or tc == to else f"{tc} | {to}"

def _bi(lang_c: str, lang_o: str, key: str, **kwargs) -> str:
    tc = t(lang_c, key, **kwargs)
    to = t(lang_o, key, **kwargs)
    if lang_c == lang_o or tc == to:
        return tc
    fc, fo = t(lang_c, "lang_flag"), t(lang_o, "lang_flag")
    return f"{fc} {tc}\n{fo} {to}"

def _load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_stats(stats: dict) -> None:
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

async def _record_result(c_id: int, o_id: int, c_pick: str, o_pick: str) -> None:
    async with _stats_lock:
        stats = _load_stats()
        for uid in (str(c_id), str(o_id)):
            stats.setdefault(uid, {"wins": 0, "games": 0})
            stats[uid]["games"] += 1
        if c_pick != o_pick:
            winner = str(c_id) if _BEATS[c_pick] == o_pick else str(o_id)
            stats[winner]["wins"] += 1
        _save_stats(stats)


def _rps_result_line(lang: str, c_id: int, o_id: int, c_pick: str, o_pick: str) -> str:
    if c_pick == o_pick:
        return f"🤝 {t(lang, 'rps_result_draw')}"
    elif _BEATS[c_pick] == o_pick:
        return f"🏆 {t(lang, 'rps_result_win', winner=f'<@{c_id}>')}"
    return f"🏆 {t(lang, 'rps_result_win', winner=f'<@{o_id}>')}"


async def _eph_edit(eph: dict | None, **kwargs) -> None:
    """Edit a stored ephemeral. eph = {"wh": Webhook, "id": int}"""
    if not eph:
        return
    try:
        await eph["wh"].edit_message(eph["id"], **kwargs)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  ROCK PAPER SCISSORS
# ─────────────────────────────────────────────

ROCK, PAPER, SCISSORS = "rock", "paper", "scissors"
_EMOJI = {ROCK: "✊", PAPER: "🖐️", SCISSORS: "✌️"}
_BEATS = {ROCK: SCISSORS, PAPER: ROCK, SCISSORS: PAPER}

_games: dict[int, dict] = {}


async def _pick_timeout(client: discord.Client, msg_id: int) -> None:
    """Called 120 s after both players get pick buttons — cleans up if nobody picked."""
    await asyncio.sleep(120)
    game = _games.pop(msg_id, None)
    if not game:
        return
    lang_c = game.get("lang_c", "en")
    lang_o = game.get("lang_o", lang_c)
    ch = client.get_channel(game["channel_id"])
    if ch:
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.edit(content=_bi(lang_c, lang_o, "rps_timeout"), embed=None, view=None)
        except Exception:
            pass
    ephs = game.get("ephemerals", {})
    await _eph_edit(ephs.get(game["challenger_id"]), content=t(lang_c, "rps_timeout"), view=None)
    await _eph_edit(ephs.get(game["opponent_id"]),   content=t(lang_o, "rps_timeout"), view=None)


async def _resolve_rps(client: discord.Client, msg_id: int) -> None:
    game = _games.pop(msg_id, None)
    if not game:
        return
    task = game.pop("pick_task", None)
    if task:
        task.cancel()

    ch = client.get_channel(game["channel_id"])
    if not ch:
        return
    try:
        msg = await ch.fetch_message(msg_id)
    except Exception:
        return

    lang_c = game.get("lang_c", "en")
    lang_o = game.get("lang_o", lang_c)
    c_id   = game["challenger_id"]
    o_id   = game["opponent_id"]
    c_pick = game["challenger_pick"]
    o_pick = game["opponent_pick"]

    result_c = _rps_result_line(lang_c, c_id, o_id, c_pick, o_pick)
    result_o = _rps_result_line(lang_o, c_id, o_id, c_pick, o_pick)
    if lang_c == lang_o or result_c == result_o:
        result = result_c
    else:
        fc, fo = t(lang_c, "lang_flag"), t(lang_o, "lang_flag")
        result = f"{fc} {result_c}\n{fo} {result_o}"

    score_c = game.get("score_c", 0)
    score_o = game.get("score_o", 0)
    if c_pick != o_pick:
        if _BEATS[c_pick] == o_pick:
            score_c += 1
        else:
            score_o += 1

    guild    = getattr(ch, "guild", None)
    c_member = guild.get_member(c_id) if guild else None
    o_member = guild.get_member(o_id) if guild else None
    c_name   = c_member.display_name if c_member else f"<@{c_id}>"
    o_name   = o_member.display_name if o_member else f"<@{o_id}>"

    embed = discord.Embed(
        title=_bi_title(lang_c, lang_o, "rps_title"),
        description=result,
        color=discord.Color.gold(),
    )
    embed.add_field(name=c_name, value=f"{_EMOJI[c_pick]} {t(lang_c, f'rps_{c_pick}')}", inline=True)
    embed.add_field(name="VS",   value=f"**{score_c} : {score_o}**", inline=True)
    embed.add_field(name=o_name, value=f"{_EMOJI[o_pick]} {t(lang_o, f'rps_{o_pick}')}", inline=True)

    await msg.edit(embed=embed, view=None)
    await _record_result(c_id, o_id, c_pick, o_pick)

    ephs = game.get("ephemerals", {})
    for uid, player_lang, target_id, target_lang in (
        (c_id, lang_c, o_id, lang_o),
        (o_id, lang_o, c_id, lang_c),
    ):
        eph = ephs.get(uid)
        if not eph:
            continue
        target_name = (c_name if target_id == c_id else o_name)
        rematch_view = RpsEphemeralRematchView(
            uid, target_id, player_lang, target_lang, ephs, msg, score_c, score_o
        )
        await _eph_edit(eph, content=f"🔄 **{target_name}**", view=rematch_view)

    log.info(f"RPS resolved (msg {msg_id}): <@{c_id}> {c_pick} vs {o_pick} <@{o_id}>")


# ─────────────────────────────────────────────
#  EPHEMERAL VIEWS
# ─────────────────────────────────────────────

class RpsEphemeralPickView(discord.ui.View):
    """Ephemeral ✊🖐️✌️ pick buttons shown in each player's private message."""

    def __init__(self, msg_id: int, player_id: int, lang: str):
        super().__init__(timeout=None)
        self.msg_id    = msg_id
        self.player_id = player_id
        self.lang      = lang

    async def _pick(self, interaction: discord.Interaction, choice: str) -> None:
        lang = detect_lang(interaction)
        if interaction.user.id != self.player_id:
            await interaction.response.defer()
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


class RpsEphemeralRematchView(discord.ui.View):
    """Ephemeral rematch button sent to each player after the game resolves."""

    def __init__(self, clicker_id: int, target_id: int, lang_c: str, lang_o: str,
                 ephemerals: dict, public_msg: discord.Message, score_c: int, score_o: int):
        super().__init__(timeout=60)
        self.clicker_id  = clicker_id
        self.target_id   = target_id
        self.lang_c      = lang_c
        self.lang_o      = lang_o
        self.ephemerals  = ephemerals
        self.public_msg  = public_msg
        self.score_c     = score_c
        self.score_o     = score_o
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.label = t(lang_c, "rps_rematch_btn")

    async def on_timeout(self):
        await _eph_edit(self.ephemerals.get(self.clicker_id),
                        content=t(self.lang_c, "rps_timeout"), view=None)

    @discord.ui.button(style=discord.ButtonStyle.grey, emoji="🔄")
    async def rematch(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.clicker_id:
            await interaction.response.defer()
            return
        if _games.get(self.public_msg.id):
            await interaction.response.send_message("❌", ephemeral=True)
            return
        self.stop()
        target = interaction.guild.get_member(self.target_id) if interaction.guild else None
        if not target:
            await interaction.response.send_message("❌", ephemeral=True)
            return

        # Update own ephemeral to "waiting for accept"
        await interaction.response.edit_message(
            content=f"⏳ {t(self.lang_c, 'rps_wait_accept', opponent=target.display_name)}",
            view=None
        )

        # New ephemerals dict — update clicker's webhook, keep target's entry
        new_ephs = dict(self.ephemerals)
        clicker_eph = new_ephs.get(self.clicker_id)
        if clicker_eph:
            new_ephs[self.clicker_id] = {"wh": interaction.followup, "id": clicker_eph["id"]}

        # Send accept/decline to target's ephemeral
        accept_view = RpsEphemeralAcceptView(
            self.clicker_id, self.target_id, self.lang_c, self.lang_o,
            new_ephs, self.public_msg, self.score_c, self.score_o
        )
        await _eph_edit(new_ephs.get(self.target_id),
                        content=t(self.lang_o, "rps_rematch_challenge", challenger=interaction.user.display_name),
                        view=accept_view)

        _games[self.public_msg.id] = {
            "challenger_id":   self.clicker_id,
            "opponent_id":     self.target_id,
            "channel_id":      self.public_msg.channel.id,
            "challenger_pick": None,
            "opponent_pick":   None,
            "lang_c":          self.lang_c,
            "lang_o":          self.lang_o,
            "score_c":         self.score_c,
            "score_o":         self.score_o,
            "msg_ref":         self.public_msg,
            "ephemerals":      new_ephs,
        }
        log.info(f"RPS rematch: <@{self.clicker_id}> -> <@{self.target_id}> (msg {self.public_msg.id})")


class RpsEphemeralAcceptView(discord.ui.View):
    """Ephemeral accept/decline for rematches — shown in opponent's private message."""

    def __init__(self, challenger_id: int, opponent_id: int, lang_c: str, lang_o: str,
                 ephemerals: dict, public_msg: discord.Message, score_c: int, score_o: int):
        super().__init__(timeout=60)
        self.challenger_id = challenger_id
        self.opponent_id   = opponent_id
        self.lang_c        = lang_c
        self.lang_o        = lang_o
        self.ephemerals    = ephemerals
        self.public_msg    = public_msg
        self.score_c       = score_c
        self.score_o       = score_o
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "eph_accept":
                    child.label = t(lang_o, "rps_accept_btn")
                elif child.custom_id == "eph_decline":
                    child.label = t(lang_o, "rps_decline_btn")

    @discord.ui.button(style=discord.ButtonStyle.green, custom_id="eph_accept")
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.defer()
            return
        game = _games.get(self.public_msg.id)
        if not game:
            await interaction.response.edit_message(content="❌", view=None)
            return
        self.stop()

        # Update opponent's ephemeral to pick buttons
        opp_pick = RpsEphemeralPickView(self.public_msg.id, self.opponent_id, self.lang_o)
        await interaction.response.edit_message(
            content=f"🎯 {t(self.lang_o, 'rps_choose')}", view=opp_pick
        )
        self.ephemerals[self.opponent_id] = {
            "wh": interaction.followup,
            "id": self.ephemerals[self.opponent_id]["id"]
        }
        game["ephemerals"] = self.ephemerals

        # Update challenger's ephemeral to pick buttons
        c_pick = RpsEphemeralPickView(self.public_msg.id, self.challenger_id, self.lang_c)
        await _eph_edit(self.ephemerals.get(self.challenger_id),
                        content=f"🎯 {t(self.lang_c, 'rps_choose')}", view=c_pick)

        game["pick_task"] = asyncio.create_task(
            _pick_timeout(interaction.client, self.public_msg.id)
        )

    @discord.ui.button(style=discord.ButtonStyle.red, custom_id="eph_decline")
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.defer()
            return
        _games.pop(self.public_msg.id, None)
        self.stop()
        declined_text = _bi(self.lang_c, self.lang_o, "rps_declined", opponent=interaction.user.mention)
        await interaction.response.edit_message(
            content=t(self.lang_o, "rps_declined", opponent=interaction.user.mention), view=None
        )
        await _eph_edit(self.ephemerals.get(self.challenger_id), content=declined_text, view=None)

    async def on_timeout(self):
        _games.pop(self.public_msg.id, None)
        await _eph_edit(self.ephemerals.get(self.opponent_id),   content=t(self.lang_o, "rps_timeout"), view=None)
        await _eph_edit(self.ephemerals.get(self.challenger_id), content=t(self.lang_c, "rps_timeout"), view=None)


# ─────────────────────────────────────────────
#  PUBLIC ACCEPT VIEW (initial challenge)
# ─────────────────────────────────────────────

class RpsAcceptView(discord.ui.View):
    """Initial challenge on public message — only the opponent can accept or decline."""

    def __init__(self, challenger_id: int, opponent_id: int, lang_c: str, lang_o: str):
        super().__init__(timeout=60)
        self.challenger_id = challenger_id
        self.opponent_id   = opponent_id
        self.lang_c        = lang_c
        self.lang_o        = lang_o
        self._msg: discord.Message | None = None
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "rps_accept":
                    child.label = t(lang_o, "rps_accept_btn")
                elif child.custom_id == "rps_decline":
                    child.label = t(lang_o, "rps_decline_btn")

    @discord.ui.button(style=discord.ButtonStyle.green, custom_id="rps_accept")
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.defer()
            return
        game = _games.get(interaction.message.id)
        if not game:
            await interaction.response.send_message("❌", ephemeral=True)
            return
        self.stop()

        # Update public message to pick prompt (no buttons)
        embed = discord.Embed(
            title=_bi_title(self.lang_c, self.lang_o, "rps_title"),
            description=_bi(self.lang_c, self.lang_o, "rps_pick_prompt",
                            challenger=f"<@{self.challenger_id}>",
                            opponent=f"<@{self.opponent_id}>"),
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)

        # Send opponent's ephemeral with pick buttons
        opp_pick = RpsEphemeralPickView(interaction.message.id, self.opponent_id, self.lang_o)
        o_eph = await interaction.followup.send(
            content=f"🎯 {t(self.lang_o, 'rps_choose')}",
            view=opp_pick, ephemeral=True, wait=True
        )
        game["ephemerals"][self.opponent_id] = {"wh": interaction.followup, "id": o_eph.id}

        # Update challenger's ephemeral to pick buttons
        c_pick = RpsEphemeralPickView(interaction.message.id, self.challenger_id, self.lang_c)
        await _eph_edit(game["ephemerals"].get(self.challenger_id),
                        content=f"🎯 {t(self.lang_c, 'rps_choose')}", view=c_pick)

        game["pick_task"] = asyncio.create_task(
            _pick_timeout(interaction.client, interaction.message.id)
        )

    @discord.ui.button(style=discord.ButtonStyle.red, custom_id="rps_decline")
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.defer()
            return
        game = _games.pop(self._msg.id, None) if self._msg else None
        self.stop()
        declined_text = _bi(self.lang_c, self.lang_o, "rps_declined", opponent=interaction.user.mention)
        await interaction.response.edit_message(content=declined_text, embed=None, view=None)
        if game:
            await _eph_edit(game.get("ephemerals", {}).get(self.challenger_id),
                            content=declined_text, view=None)

    async def on_timeout(self):
        if not self._msg:
            return
        game = _games.pop(self._msg.id, None)
        timeout_text = _bi(self.lang_c, self.lang_o, "rps_timeout")
        try:
            await self._msg.edit(content=timeout_text, embed=None, view=None)
        except Exception:
            pass
        if game:
            await _eph_edit(game.get("ephemerals", {}).get(self.challenger_id),
                            content=t(self.lang_c, "rps_timeout"), view=None)


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
        lang_c = detect_lang(interaction)
        lang_o = _get_user_lang(str(opponent.id)) or lang_c

        if opponent.bot:
            await interaction.response.send_message(t(lang_c, "rps_err_bot"), ephemeral=True)
            return
        if opponent.id == interaction.user.id:
            await interaction.response.send_message(t(lang_c, "rps_err_self"), ephemeral=True)
            return

        embed = discord.Embed(
            title=_bi_title(lang_c, lang_o, "rps_title"),
            description=_bi(lang_c, lang_o, "rps_challenge",
                            challenger=interaction.user.mention,
                            opponent=opponent.mention),
            color=discord.Color.blurple(),
        )
        view = RpsAcceptView(interaction.user.id, opponent.id, lang_c, lang_o)

        await interaction.response.send_message(content=opponent.mention, embed=embed, view=view)
        msg = await interaction.original_response()

        # Send challenger's waiting ephemeral
        c_eph = await interaction.followup.send(
            content=f"⏳ {t(lang_c, 'rps_wait_accept', opponent=opponent.display_name)}",
            ephemeral=True, wait=True
        )

        view._msg = msg
        _games[msg.id] = {
            "challenger_id":   interaction.user.id,
            "opponent_id":     opponent.id,
            "channel_id":      msg.channel.id,
            "challenger_pick": None,
            "opponent_pick":   None,
            "lang_c":          lang_c,
            "lang_o":          lang_o,
            "score_c":         0,
            "score_o":         0,
            "msg_ref":         msg,
            "ephemerals": {
                interaction.user.id: {"wh": interaction.followup, "id": c_eph.id},
            },
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

    @app_commands.command(
        name="leaderboard",
        description=app_commands.locale_str("Show RPS leaderboard", key="cmd_leaderboard"),
    )
    async def leaderboard(self, interaction: discord.Interaction):
        lang  = detect_lang(interaction)
        stats = _load_stats()
        if not stats:
            await interaction.response.send_message(t(lang, "leaderboard_empty"), ephemeral=True)
            return

        top    = sorted(stats.items(), key=lambda x: x[1]["wins"], reverse=True)[:10]
        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, (uid, data) in enumerate(top):
            member = interaction.guild.get_member(int(uid)) if interaction.guild else None
            name   = member.display_name if member else f"<@{uid}>"
            rank   = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{rank} **{name}** — {data['wins']} {t(lang, 'leaderboard_wins')} / {data['games']} {t(lang, 'leaderboard_games')}")

        embed = discord.Embed(
            title=t(lang, "leaderboard_title"),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            lang = detect_lang(interaction)
            retry = round(error.retry_after)
            await interaction.response.send_message(t(lang, "rps_cooldown", seconds=retry), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GamesCog(bot))
