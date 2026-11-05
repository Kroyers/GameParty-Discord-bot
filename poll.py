import discord
from discord import app_commands
from discord.ext import commands, tasks
from lang import detect_lang, atomic_write_json, t
import asyncio
import datetime
import logging
import json
import os
import re
import unicodedata

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────
#  SETTINGS
# ─────────────────────────────────────────────

CONFIG_FILE      = os.path.join(SCRIPT_DIR, "config.json")
GAMES_FILE       = os.path.join(SCRIPT_DIR, "poll_games.json")
RUN_FILE         = os.path.join(SCRIPT_DIR, "poll_run.json")
SEARCH_LIMIT = 20
DELETE_SCAN_LIMIT = 200    # how far back old poll messages are looked for
PICKER_TIMEOUT    = 3600   # seconds the /poll game picker stays usable
PICKER_PAGE_SIZE  = 10     # games per picker page (Discord allows at most 25 select options)
POLL_ANSWER_LIMIT = 10     # Discord limit of answers in one poll
POLL_ANSWER_TEXT_LIMIT = 55   # Discord limit of one poll answer's text
PICKER_DATE_OPTIONS = 24   # days offered next to the weekend (Discord allows at most 25 select options)
POLL_DATE_CLOSE_HOUR = 15  # a poll for one date closes at its earliest start time
PICKER_WEEKEND = "weekend" # date select value of the default weekend poll
RESULTS_LANG = "en"        # language of the results posted into the poll thread
RESULTS_NAME_WIDTH = 22    # game name column of the results table, keeps it narrow for mobile
RESULT_CLEANUP_TRIES = 5   # checks (one a minute) for Discord's "poll has closed" messages after the results

INTRO_MESSAGE = (
    "🇨🇿 **Ahoj všichni!** Hlasujte o všem co jste ochotný s námi hrát příští víkend. Tato emoji: 👥 znamená maximální počet hráčů.\n\n"
    "🇸🇰 **Ahojte všetci!** Hlasujte o všetkom, čo ste ochotní s nami hrať budúci víkend. Tieto emoji 👥 znamenajú maximálny počet hráčov.\n\n"
    "🇵🇱 **Cześć wszystkim!** Głosujcie na wszystko, w co jesteście chętni z nami zagrać w przyszły weekend. Te emoji 👥 oznaczają maksymalną liczbę graczy.\n\n"
    "🇬🇧 **Hi, everyone!** Vote for everything you're willing to play with us next weekend. These emojis 👥 mean the maximum number of players."
)

# Initial game list, seeded into the editable poll_games.json on first use.
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
    ("🕔", "17:00 - Pátek/Piatok/Piątek/Friday"),
    ("🕕", "18:00 - Pátek/Piątek/Friday"),
    ("🕓", "16:00 - Sobota/Saturday"),
    ("🕔", "17:00 - Sobota/Saturday"),
    ("🕕", "18:00 - Sobota/Saturday"),
    ("🕓", "16:00 - Neděle/Niedziela/Sunday"),
    ("🕔", "17:00 - Neděle/Nedeľa/Niedziela/Sunday"),
    ("🕕", "18:00 - Neděle/Nedeľa/Niedziela/Sunday"),
]

POLL_ANSWERS_START_DATE = [               # /poll for one date
    ("🕒", "15:00"),
    ("🕓", "16:00"),
    ("🕔", "17:00"),
    ("🕕", "18:00"),
    ("🕖", "19:00"),
    ("🕗", "20:00"),
]

INTRO_MESSAGE_DATE = (
    "🇨🇿 **Ahoj všichni!** Hlasujte o všem co jste ochotný s námi hrát {date}. Tato emoji: 👥 znamená maximální počet hráčů.\n\n"
    "🇸🇰 **Ahojte všetci!** Hlasujte o všetkom, čo ste ochotní s nami hrať {date}. Tieto emoji 👥 znamenajú maximálny počet hráčov.\n\n"
    "🇵🇱 **Cześć wszystkim!** Głosujcie na wszystko, w co jesteście chętni z nami zagrać {date}. Te emoji 👥 oznaczają maksymalną liczbę graczy.\n\n"
    "🇬🇧 **Hi, everyone!** Vote for everything you're willing to play with us on {date}. These emojis 👥 mean the maximum number of players."
)

INTRO_PREFIX = "🇨🇿 **Ahoj všichni!** Hlasujte"   # both intro texts start with this

POLL_ANSWER_OTHER = POLL_ANSWERS_2[-1]    # always the last answer of /poll

# /poll with a single game: the game is the question, one answer per voter.
POLL_SINGLE_ANSWERS = [
    ("<a:catYes:1162876619141165157>",   "Ano/Áno/Tak/Yes"),
    ("🕑",                               "Ano, později/Áno, neskôr/Tak, później/Yes, later"),
    ("<a:hmmm:1022959572530495618>",     "Možná/Možno/Może/Maybe"),
    ("<a:OkayNope:733785127007813632>",  "Ne/Nie/No"),
]
# Used when the bot can't see a custom emoji above (it must be on the emoji's server).
POLL_EMOJI_FALLBACK = {
    "<a:catYes:1162876619141165157>":  "✅",
    "<a:hmmm:1022959572530495618>":    "🤔",
    "<a:OkayNope:733785127007813632>": "❌",
}
POLL_GAMES_QUESTION = "Hry/Gry/Games"     # question of /poll when all games fit into one poll

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

def _seed_games() -> dict:
    games = []
    for emoji, text in POLL_ANSWERS_1 + POLL_ANSWERS_2[:-1]:
        name, players = re.fullmatch(r"(.+) \(👥 (\d+)\)", text).groups()
        games.append({"name": name, "emoji": emoji, "players": int(players), "url": None})
    return {"games": games, "last_selected": [g["name"] for g in games]}

def load_games() -> dict:
    """{"games": [{name, emoji, players, url}], "last_selected": [name]} — shared by the whole server."""
    if not os.path.exists(GAMES_FILE):
        return _seed_games()
    with open(GAMES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_games(data: dict) -> None:
    atomic_write_json(GAMES_FILE, data)

def game_answer_text(game: dict) -> str:
    return f"{game['name']} (👥 {game['players']})"

def is_valid_emoji(emoji: str) -> bool:
    """Custom emoji, keycap, or a sequence of Unicode emoji characters."""
    if re.fullmatch(r"<a?:\w{2,32}:\d+>|[0-9#*]\ufe0f?\u20e3", emoji):
        return True
    categories = [unicodedata.category(ch) for ch in emoji]
    return bool(categories) and categories[0] == "So" and all(c in ("So", "Sk", "Mn", "Me", "Cf") for c in categories)

def build_game_polls(games: list[dict]) -> list[dict]:
    """Splits the selected games plus the closing "other" answer into polls of up to 10 answers.
    A single game becomes a yes/later/maybe/no poll with the game as the question."""
    if len(games) == 1:
        return [{"question": game_answer_text(games[0]), "answers": POLL_SINGLE_ANSWERS, "multiple": False}]
    answers = [(g["emoji"], game_answer_text(g)) for g in games] + [POLL_ANSWER_OTHER]
    chunks  = [answers[i:i + POLL_ANSWER_LIMIT] for i in range(0, len(answers), POLL_ANSWER_LIMIT)]
    if len(chunks) == 1:
        return [{"question": POLL_GAMES_QUESTION, "answers": chunks[0]}]
    return [{"question": f"({i}/{len(chunks)})", "answers": chunk} for i, chunk in enumerate(chunks, 1)]

def load_run() -> dict | None:
    """The sent /poll waiting for its results: {channel_id, thread_id, title, game_poll_ids, start_poll_id}."""
    if not os.path.exists(RUN_FILE):
        return None
    with open(RUN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_run(run: dict) -> None:
    atomic_write_json(RUN_FILE, run)

def clear_run() -> None:
    if os.path.exists(RUN_FILE):
        os.remove(RUN_FILE)

def rank_games(answers: list[tuple[str, int]]) -> list[tuple[str, int, int | None]]:
    """(answer text, votes) -> (name, votes, max players), best first. Ranked by how many voters can
    actually play, then by votes, then by max players. No votes = left out, unknown player count = last."""
    games, others = [], []
    for text, votes in answers:
        if votes == 0:
            continue
        match = re.fullmatch(r"(.+) \(👥 (\d+)\)", text)
        if match:
            games.append((match.group(1), votes, int(match.group(2))))
        else:
            others.append((re.sub(r" \(👥 \?\)$", "", text), votes, None))
    games.sort(key=lambda g: (-min(g[1], g[2]), -g[1], -g[2]))
    others.sort(key=lambda g: -g[1])
    return games + others

def rank_starts(answers: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """(answer text, votes) -> (time and English day, votes), most votes first, ties keep the poll's order."""
    starts = [(re.sub(r" - .*/", " - ", text), votes) for text, votes in answers if votes > 0]
    return sorted(starts, key=lambda s: -s[1])

def results_description(games: list[tuple[str, int, int | None]], starts: list[tuple[str, int]]) -> str:
    lang = RESULTS_LANG
    if not games and not starts:
        return t(lang, "poll_results_none")

    parts = []
    if games:
        w = RESULTS_NAME_WIDTH
        lines = [
            f"{'#':>2}  {t(lang, 'poll_results_game'):<{w}}  {t(lang, 'poll_results_votes'):>5}  {t(lang, 'poll_results_max'):>3}",
            "─" * (w + 16),
        ]
        for i, (name, votes, players) in enumerate(games, 1):
            if len(name) > w:
                name = name[:w - 1] + "…"
            lines.append(f"{i:>2}  {name:<{w}}  {votes:>5}  {'?' if players is None else players:>3}")
        table = "\n".join(lines).replace("`", "'")
        parts.append(f"```\n{table}\n```")
    if starts:
        lines = [f"{t(lang, 'poll_results_start'):<16}  {t(lang, 'poll_results_votes'):>5}", "─" * 23]
        lines += [f"{text:<16}  {votes:>5}" for text, votes in starts]
        table = "\n".join(lines)
        parts.append(f"```\n{table}\n```")
    return "\n".join(parts)

def single_results_description(game: str, answers: list[tuple[str, int]], starts: list[tuple[str, int]]) -> str:
    """Results of a single game poll: every answer in the poll's order (English part of its text),
    the start times as usual."""
    answers = [(text.rsplit("/", 1)[-1], votes) for text, votes in answers]
    parts = [f"**{discord.utils.escape_markdown(game)}**"]
    w = max(len(text) for text, _ in answers)
    lines = [f"{text:<{w}}  {votes:>5}" for text, votes in answers]
    parts.append("```\n" + "\n".join(lines).replace("`", "'") + "\n```")
    start = results_description([], starts)
    if starts:
        parts.append(start)
    return "\n".join(parts)

def utc_offset_at(moment: datetime.datetime) -> int:
    """Central European offset at the given local time."""
    year = moment.year

    def last_sunday(month):
        d = datetime.date(year, month, 31 if month in (3, 10) else 30)
        while d.weekday() != 6:
            d -= datetime.timedelta(days=1)
        return d

    dst_start = datetime.datetime(year, 3,  last_sunday(3).day,  2)
    dst_end   = datetime.datetime(year, 10, last_sunday(10).day, 3)
    return 2 if dst_start <= moment < dst_end else 1


def date_title(day: datetime.date) -> str:
    return f"{day.day}.{day.month}.{day.year}"


WEEKEND_DAYS = {"Friday": 0, "Saturday": 1, "Sunday": 2}   # days after the weekend's Friday


def single_start(when_day: datetime.date, answer: str) -> tuple[datetime.date, str]:
    """(day, "HH:MM") of the only start answer picked. Weekend answers name their day in English last."""
    time = answer[:5]
    if " - " in answer:
        when_day += datetime.timedelta(days=WEEKEND_DAYS[answer.rsplit("/", 1)[-1]])
    return when_day, time


def date_poll_end(day: datetime.date) -> datetime.datetime:
    return datetime.datetime(day.year, day.month, day.day, POLL_DATE_CLOSE_HOUR)


def picker_dates(now: datetime.datetime) -> list[datetime.date]:
    """Days a poll can be sent for: today only while it can still close at POLL_DATE_CLOSE_HOUR."""
    first = now.date() if now < date_poll_end(now.date()) else now.date() + datetime.timedelta(days=1)
    return [first + datetime.timedelta(days=i) for i in range(PICKER_DATE_OPTIONS)]


def next_weekend() -> tuple[str, int, datetime.datetime]:
    """Returns (title, utc_offset, end_time) for the next weekend."""
    now        = datetime.datetime.now()
    utc_offset = utc_offset_at(now)

    wd             = now.weekday()
    days_to_friday = 4 - wd if wd <= 3 else 4 - wd + 7
    friday         = (now + datetime.timedelta(days=days_to_friday)).date()
    sunday         = friday + datetime.timedelta(days=2)
    end_time       = datetime.datetime(friday.year, friday.month, friday.day)

    title = f"{friday.day}.{friday.month}.-{sunday.day}.{sunday.month}.{sunday.year}"
    return title, utc_offset, end_time


async def count_active_polls(channel, bot_id) -> int:
    """Counts the bot's running polls that delete_old_messages would remove."""
    active = 0
    async for msg in channel.history(limit=DELETE_SCAN_LIMIT):
        if msg.author.id == bot_id and msg.poll is not None and not msg.poll.is_finalised():
            active += 1
    return active


async def delete_old_messages(channel, bot_id):
    """Deletes the bot's previous poll messages (polls, intro embed with its
    thread, plain ping messages) and locks the threads. Other bot messages in
    the channel (e.g. the daily guess game) are left untouched."""
    deleted = 0
    async for msg in channel.history(limit=DELETE_SCAN_LIMIT):
        if msg.author.id != bot_id:
            continue
        # The intro is recognised by its text too, its thread may have been deleted by hand.
        is_intro = any((e.description or "").startswith(INTRO_PREFIX) for e in msg.embeds)
        if msg.poll is None and msg.thread is None and msg.embeds and not is_intro:
            log.debug(f"Keeping bot message {msg.id}, not a poll message.")
            continue
        if msg.thread is not None:
            try:
                await msg.thread.edit(locked=True, archived=False)
                log.debug(f"Thread '{msg.thread.name}' locked.")
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
#  GAME PICKER VIEW
# ─────────────────────────────────────────────

class GamePickerView(discord.ui.View):
    """Paged multi-select for /poll. The selection survives flipping between pages."""

    def __init__(self, lang: str, games: list[dict], selected: list[str], channel_mention: str,
                 interaction: discord.Interaction):
        super().__init__(timeout=PICKER_TIMEOUT)
        self.lang      = lang
        self.games     = games
        self.selected  = {g["name"] for g in games} & set(selected)
        self.channel_mention = channel_mention
        self.confirmed = False
        self.ping      = True
        self.when      = PICKER_WEEKEND   # PICKER_WEEKEND or an ISO date
        self.page      = 0
        self.pages     = max(1, (len(games) + PICKER_PAGE_SIZE - 1) // PICKER_PAGE_SIZE)
        # The webhook token of an interaction expires after 15 minutes, so always keep the latest one.
        self.interaction = interaction

        self.select = discord.ui.Select(row=0, min_values=0, placeholder=t(lang, "poll_pick_placeholder"))
        self.select.callback = self._on_select
        self.add_item(self.select)

        weekdays = t(lang, "poll_pick_weekdays").split(",")
        self.when_labels = {PICKER_WEEKEND: t(lang, "poll_pick_weekend", title=next_weekend()[0])}
        for day in picker_dates(datetime.datetime.now()):
            self.when_labels[day.isoformat()] = f"{weekdays[day.weekday()]} {date_title(day)}"
        self.when_select = discord.ui.Select(row=2)
        self.when_select.callback = self._on_when_select
        self.add_item(self.when_select)

        self.start_selected = {text for _, text in self._start_options()}
        self.start_select = discord.ui.Select(row=3, placeholder=t(lang, "poll_pick_start_placeholder"))
        self.start_select.callback = self._on_start_select
        self.add_item(self.start_select)

        self.prev_btn.label   = t(lang, "poll_pick_prev")
        self.next_btn.label   = t(lang, "poll_pick_next")
        self.send_btn.label   = t(lang, "poll_pick_send")
        self.cancel_btn.label = t(lang, "cancel_btn")
        self._refresh()

    def _page_games(self) -> list[dict]:
        start = self.page * PICKER_PAGE_SIZE
        return self.games[start:start + PICKER_PAGE_SIZE]

    def _start_options(self) -> list[tuple[str, str]]:
        return POLL_ANSWERS_START if self.when == PICKER_WEEKEND else POLL_ANSWERS_START_DATE

    def selected_starts(self) -> list[tuple[str, str]]:
        return [answer for answer in self._start_options() if answer[1] in self.start_selected]

    def set_when(self, when: str) -> None:
        """Another date offers other start times, all of them selected again."""
        self.when = when
        self.start_selected = {text for _, text in self._start_options()}

    def _refresh(self) -> None:
        self.start_select.options = [
            discord.SelectOption(label=text, value=text, default=text in self.start_selected)
            for _, text in self._start_options()
        ]
        self.start_select.max_values = len(self.start_select.options)
        self.select.options = [
            discord.SelectOption(label=f"{g['name']} ({g['players']})", value=g["name"],
                                 default=g["name"] in self.selected)
            for g in self._page_games()
        ]
        self.select.max_values = len(self.select.options)
        self.when_select.options = [
            discord.SelectOption(label=label, value=value, default=value == self.when)
            for value, label in self.when_labels.items()
        ]
        self.status_btn.label = t(self.lang, "poll_pick_status", count=len(self.selected),
                                  page=self.page + 1, pages=self.pages)
        self.ping_btn.label = t(self.lang, "poll_pick_ping_on" if self.ping else "poll_pick_ping_off")
        self.ping_btn.emoji = "🔔" if self.ping else "🔕"
        self.ping_btn.style = discord.ButtonStyle.primary if self.ping else discord.ButtonStyle.secondary
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1
        self.send_btn.disabled = not self.selected

    def content(self) -> str:
        return t(self.lang, "poll_pick_prompt", channel=self.channel_mention)

    def apply_page_selection(self, values: list[str]) -> None:
        self.selected -= {g["name"] for g in self._page_games()}
        self.selected |= set(values)

    def selected_games(self) -> list[dict]:
        return [g for g in self.games if g["name"] in self.selected]

    async def _show(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self._refresh()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.apply_page_selection(self.select.values)
        await self._show(interaction)

    async def _on_when_select(self, interaction: discord.Interaction) -> None:
        self.set_when(self.when_select.values[0])
        await self._show(interaction)

    async def _on_start_select(self, interaction: discord.Interaction) -> None:
        self.start_selected = set(self.start_select.values)
        await self._show(interaction)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", row=1)
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page -= 1
        await self._show(interaction)

    # Message text can't sit between components, so a disabled button shows the status there.
    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, disabled=True, row=1)
    async def status_btn(self, _interaction: discord.Interaction, _button: discord.ui.Button):
        pass

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="▶️", row=1)
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page += 1
        await self._show(interaction)

    @discord.ui.button(label="Ping", style=discord.ButtonStyle.primary, emoji="🔔", row=4)
    async def ping_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.ping = not self.ping
        await self._show(interaction)

    @discord.ui.button(label="Send", style=discord.ButtonStyle.green, emoji="✅", row=4)
    async def send_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.confirmed   = True
        self.interaction = interaction
        await interaction.response.edit_message(content=f"⏳ {t(self.lang, 'poll_sending')}", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌", row=4)
    async def cancel_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(content=f"❌ {t(self.lang, 'cancelled')}", view=None)
        self.stop()

    async def on_timeout(self) -> None:
        try:
            await self.interaction.edit_original_response(content=f"⌛ {t(self.lang, 'poll_pick_timeout')}", view=None)
        except discord.HTTPException:
            pass   # token already expired, the stale menu just stops responding

# ─────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────

class _PollNotFinished(Exception):
    pass


class PollCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        cfg = load_config()
        self.poll_channel_id = cfg.get("poll_channel_id")
        self.role_id         = cfg.get("poll_role_id")
        self.noti_role_id    = cfg.get("poll_noti_role_id")
        self._results_lock   = asyncio.Lock()

    async def cog_load(self):
        self.check_results.start()

    async def cog_unload(self):
        self.check_results.cancel()

    # ── Poll results ─────────────────────────────
    # Driven by the saved run, not by a Discord event: it also covers polls that ended while the
    # bot was offline, and the results wait until every poll of the run is finalised.

    async def _report_results(self) -> None:
        async with self._results_lock:
            run = load_run()
            if not run:
                return
            channel = self.bot.get_channel(run["channel_id"])
            if channel is None:
                log.warning(f"Poll results: channel {run['channel_id']} not available, will retry.")
                return
            if run.get("results_posted"):
                await self._delete_result_messages(channel, run)
                return

            async def fetch_answers(poll_id: int) -> list[tuple[str, int]] | None:
                """None = poll deleted, nothing to count. Raises _PollNotFinished while it runs."""
                try:
                    msg = await channel.fetch_message(poll_id)
                except discord.NotFound:
                    log.warning(f"Poll results: poll {poll_id} no longer exists, skipped.")
                    return None
                if msg.poll is None:
                    return None
                if not msg.poll.is_finalised():
                    raise _PollNotFinished
                return [(a.text, a.vote_count) for a in msg.poll.answers]

            try:
                game_answers = [await fetch_answers(i) for i in run["game_poll_ids"]]
                start_answers = await fetch_answers(run["start_poll_id"]) if run["start_poll_id"] else None
            except _PollNotFinished:
                return
            except discord.HTTPException as e:
                log.warning(f"Poll results: could not read the polls, will retry: {e}")
                return

            if start_answers is None and all(a is None for a in game_answers):
                log.info("Poll results: all polls of the run were deleted, nothing to post.")
                clear_run()
                return

            starts = rank_starts(start_answers or [])
            if run.get("single_game") and game_answers and game_answers[0]:
                description = single_results_description(run["single_game"], game_answers[0], starts)
            else:
                games = rank_games([answer for answers in game_answers if answers for answer in answers])
                description = results_description(games, starts)
            embed = discord.Embed(
                title=t(RESULTS_LANG, "poll_results_title", title=run["title"]),
                description=description,
                color=discord.Color.blurple(),
            )
            try:
                thread = await self.bot.fetch_channel(run["thread_id"])
                await thread.send(embed=embed)
            except (discord.NotFound, discord.Forbidden) as e:
                log.error(f"Poll results: cannot post to thread {run['thread_id']}, giving up: {e}")
                clear_run()
                return
            except discord.HTTPException as e:
                log.warning(f"Poll results: posting failed, will retry: {e}")
                return
            log.info(f"Poll results posted to thread {run['thread_id']}.")
            run["results_posted"] = True
            run["pending_results"] = [i for i in (*run["game_poll_ids"], run["start_poll_id"]) if i]
            run["cleanup_tries"] = 0
            save_run(run)
            await self._delete_result_messages(channel, run)

    async def _delete_result_messages(self, channel, run: dict) -> None:
        """Deletes Discord's "poll has closed" messages of the run. Such a message may show up only after
        its poll is finalised, and a deleted poll never gets one, so this retries a limited number of times."""
        pending = set(run["pending_results"])
        try:
            async for msg in channel.history(limit=DELETE_SCAN_LIMIT):
                if msg.type is not discord.MessageType.poll_result or msg.reference is None:
                    continue
                poll_id = msg.reference.message_id
                if poll_id not in pending:
                    continue
                try:
                    await msg.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    log.warning(f"Poll results: could not delete closing message {msg.id}: {e}")
                pending.discard(poll_id)
        except discord.HTTPException as e:
            log.warning(f"Poll results: could not read the channel, will retry: {e}")

        run["cleanup_tries"] += 1
        if not pending or run["cleanup_tries"] >= RESULT_CLEANUP_TRIES:
            clear_run()
            return
        run["pending_results"] = sorted(pending)
        save_run(run)

    def _usable_emoji(self, emoji: str) -> str:
        """A custom emoji the bot can't see would make Discord reject the whole poll."""
        match = re.fullmatch(r"<a?:\w+:(\d+)>", emoji)
        if match and self.bot.get_emoji(int(match.group(1))) is None:
            fallback = POLL_EMOJI_FALLBACK.get(emoji, "❔")
            log.warning(f"Emoji {emoji} not available to the bot, using {fallback} instead.")
            return fallback
        return emoji

    @tasks.loop(minutes=1)
    async def check_results(self):
        try:
            await self._report_results()
        except Exception as e:   # an unhandled error would stop the loop for good
            log.error(f"Poll results check failed: {e}")

    @check_results.before_loop
    async def _before_check_results(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="poll", description=app_commands.locale_str("Send game polls", key="cmd_poll"))
    @app_commands.default_permissions(administrator=True)
    async def poll(self, interaction: discord.Interaction):
        lang = detect_lang(interaction)
        channel = self.bot.get_channel(self.poll_channel_id)
        if channel is None:
            await interaction.response.send_message(f"❌ {t(lang, 'err_poll_no_channel')}", ephemeral=True)
            return

        data = load_games()
        if not data["games"]:
            await interaction.response.send_message(f"❌ {t(lang, 'poll_games_empty')}", ephemeral=True)
            return

        view = GamePickerView(lang, data["games"], data["last_selected"], channel.mention, interaction)
        await interaction.response.send_message(view.content(), view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            log.debug(f"Poll action cancelled or timed out ({interaction.user}).")
            return

        active = await count_active_polls(channel, self.bot.user.id)
        if active:
            confirm_view = ConfirmView(lang)
            await view.interaction.edit_original_response(
                content=f"⚠️ {t(lang, 'poll_active_warning', channel=channel.mention, count=active)}", view=confirm_view
            )
            await confirm_view.wait()
            if not confirm_view.confirmed:
                log.debug(f"Poll action cancelled or timed out ({interaction.user}).")
                await view.interaction.edit_original_response(content=f"❌ {t(lang, 'cancelled')}", view=None)
                return
            await view.interaction.edit_original_response(content=f"⏳ {t(lang, 'poll_sending')}", view=None)

        selected = view.selected_games()
        data = load_games()   # the list may have been edited while the picker was open
        data["last_selected"] = [g["name"] for g in selected]
        save_games(data)

        ping = view.ping
        start_answers = view.selected_starts()
        if view.when == PICKER_WEEKEND:
            title, utc_offset, end_time = next_weekend()
            intro = INTRO_MESSAGE
        else:
            day = datetime.date.fromisoformat(view.when)
            title = date_title(day)
            end_time = date_poll_end(day)
            utc_offset = utc_offset_at(end_time)
            intro = INTRO_MESSAGE_DATE.format(date=title)
        if len(start_answers) == 1:
            # A Start poll with a single answer has nothing to decide, the time goes into the title.
            first_day = end_time.date() if view.when == PICKER_WEEKEND else datetime.date.fromisoformat(view.when)
            day, time = single_start(first_day, start_answers[0][1])
            hour, minute = (int(x) for x in time.split(":"))
            utc_offset = utc_offset_at(datetime.datetime(day.year, day.month, day.day, hour, minute))
            title = f"{date_title(day)} {time} (UTC+{utc_offset})"
            intro = INTRO_MESSAGE_DATE.format(date=title)
            start_answers = []
        hours = max(1, round((end_time - datetime.datetime.now()).total_seconds() / 3600))

        # Polls that have just ended still get their results, running ones are deleted below without any.
        await self._report_results()
        clear_run()

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
            description=intro,
            color=discord.Color.blurple(),
        )
        embed_msg = await channel.send(embed=embed)
        log.debug(f"Embed sent (ID: {embed_msg.id})")

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
        log.debug(f"Thread created: '{thread.name}' (ID: {thread.id})")

        polls = build_game_polls(selected)
        if start_answers:
            polls.append({"question": f"Start {title} (UTC+{utc_offset})", "answers": start_answers})
        poll_ids = []
        for p in polls:
            discord_poll = discord.Poll(
                question=p["question"],
                duration=datetime.timedelta(hours=hours),
                multiple=p.get("multiple", True),
            )
            for emoji, text in p["answers"]:
                discord_poll.add_answer(text=text, emoji=self._usable_emoji(emoji))
            msg = await channel.send(poll=discord_poll)
            poll_ids.append(msg.id)
            log.debug(f"Poll sent: '{p['question']}' (ID: {msg.id})")

        save_run({"channel_id": channel.id, "thread_id": thread.id, "title": title,
                  "game_poll_ids": poll_ids[:-1] if start_answers else poll_ids,
                  "start_poll_id": poll_ids[-1] if start_answers else None,
                  "single_game": polls[0]["question"] if len(selected) == 1 else None})
        log.info(f"All polls sent. Running for {hours} hours.")
        await view.interaction.edit_original_response(content=f"✅ {t(lang, 'poll_sent')}")

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
            log.debug(f"Poll action cancelled or timed out ({interaction.user}).")
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
                log.debug(f"Poll ended: '{msg.poll.question}' (ID: {msg.id})")
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


    @app_commands.command(name="poll-game-add", description=app_commands.locale_str("Add a game to the /poll list", key="cmd_poll_game_add"))
    @app_commands.describe(
        name=app_commands.locale_str("Game name", key="cmd_poll_game_name"),
        emoji=app_commands.locale_str("Emoji shown next to the game", key="cmd_poll_game_emoji"),
        players=app_commands.locale_str("Maximum number of players", key="cmd_poll_game_players"),
        url=app_commands.locale_str("Link to the game (optional)", key="cmd_poll_game_url"),
    )
    @app_commands.default_permissions(administrator=True)
    async def poll_game_add(self, interaction: discord.Interaction, name: str, emoji: str,
                            players: app_commands.Range[int, 1, 999], url: str | None = None):
        lang  = detect_lang(interaction)
        name  = " ".join(name.split())
        emoji = emoji.strip()
        game  = {"name": name, "emoji": emoji, "players": players, "url": url.strip() if url else None}
        data  = load_games()

        error = None
        if any(g["name"].casefold() == name.casefold() for g in data["games"]):
            error = t(lang, "poll_game_exists", game=name)
        elif not name or len(game_answer_text(game)) > POLL_ANSWER_TEXT_LIMIT:
            error = t(lang, "poll_game_bad_name", max=POLL_ANSWER_TEXT_LIMIT - len(game_answer_text({**game, "name": ""})))
        elif not is_valid_emoji(emoji):
            error = t(lang, "poll_game_bad_emoji")
        elif game["url"] and not re.fullmatch(r"https?://\S+", game["url"]):
            error = t(lang, "poll_game_bad_url")
        if error:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return

        # The list stays ordered by player count, a new game goes last among equals.
        index = next((i for i, g in enumerate(data["games"]) if g["players"] < players), len(data["games"]))
        data["games"].insert(index, game)
        save_games(data)
        await interaction.response.send_message(
            f"✅ {t(lang, 'poll_game_added', game=game_answer_text(game))}", ephemeral=True
        )
        log.info(f"Poll game added: {game} by {interaction.user}.")

    @app_commands.command(name="poll-game-remove", description=app_commands.locale_str("Remove a game from the /poll list", key="cmd_poll_game_remove"))
    @app_commands.describe(name=app_commands.locale_str("Game name", key="cmd_poll_game_name"))
    @app_commands.default_permissions(administrator=True)
    async def poll_game_remove(self, interaction: discord.Interaction, name: str):
        lang = detect_lang(interaction)
        data = load_games()
        game = next((g for g in data["games"] if g["name"].casefold() == name.strip().casefold()), None)
        if game is None:
            await interaction.response.send_message(f"❌ {t(lang, 'poll_game_not_found', game=name)}", ephemeral=True)
            return

        data["games"].remove(game)
        data["last_selected"] = [n for n in data["last_selected"] if n != game["name"]]
        save_games(data)
        await interaction.response.send_message(
            f"✅ {t(lang, 'poll_game_removed', game=game_answer_text(game))}", ephemeral=True
        )
        log.info(f"Poll game removed: {game['name']} by {interaction.user}.")

    @poll_game_remove.autocomplete("name")
    async def _poll_game_remove_autocomplete(self, _interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = current.casefold()
        names  = [g["name"] for g in load_games()["games"] if needle in g["name"].casefold()]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    @app_commands.command(name="poll-game-list", description=app_commands.locale_str("Show the /poll game list", key="cmd_poll_game_list"))
    @app_commands.default_permissions(administrator=True)
    async def poll_game_list(self, interaction: discord.Interaction):
        lang  = detect_lang(interaction)
        games = load_games()["games"]
        if not games:
            await interaction.response.send_message(f"❌ {t(lang, 'poll_games_empty')}", ephemeral=True)
            return

        # Split over several messages if needed, an embed description holds 4096 characters.
        chunks = [""]
        for g in games:
            line = f"{g['emoji']} {discord.utils.escape_markdown(g['name'])} (👥 {g['players']})"
            if g["url"]:
                line += f" · [{t(lang, 'poll_game_link')}]({g['url']})"
            if len(chunks[-1]) + len(line) + 1 > 4000:
                chunks.append("")
            chunks[-1] += f"{line}\n"

        title  = t(lang, "poll_games_title", count=len(games))
        embeds = [discord.Embed(title=title, description=c, color=discord.Color.blurple()) for c in chunks]
        await interaction.response.send_message(embed=embeds[0], ephemeral=True)
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PollCog(bot))
