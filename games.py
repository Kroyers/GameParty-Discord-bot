import asyncio
import datetime
import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import random
import logging
from lang import detect_lang, _get_user_lang, atomic_write_json, t

SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE      = os.path.join(SCRIPT_DIR, "config.json")
USERS_FILE       = os.path.join(SCRIPT_DIR, "users.json")
GUESS_STATE_FILE = os.path.join(SCRIPT_DIR, "guess_state.json")
RPS_STATS_FILE   = os.path.join(SCRIPT_DIR, "rps_stats.json")

_users_lock  = asyncio.Lock()
_guess_ephs: dict[str, dict] = {}   # uid -> {"wh": Webhook, "id": int}
log = logging.getLogger(__name__)

_GUESS_POINTS  = [10, 7, 5, 3]   # rank 0=1st, 1=2nd, 2=3rd, 3=4th
_GUESS_BANNED: set[int] = {6767, 67, 666, 616, 13, 1313, 1488, 8814, 88, 18, 1933, 1939, 4200, 1312, 911}  # numbers the daily draw will never pick, e.g. {1111, 1234}
_GUESS_HINT_AFTER = 15           # attempts to unlock the first daily hint (0 = disabled)
_GUESS_HINT_EVERY = 10           # further hints unlock every this many attempts
_GUESS_HINT_TYPES = ("parity", "digits", "digitsum", "first_digit",
                     "last_digit", "half", "div3")     # shuffled order drawn per day
_GUESS_NORMAL_CHANCE = 0.60      # chance of a plain day without a modifier
_GUESS_MODIFIERS = (             # relative weights among modifiers on modifier days
    ("wordle",  30),             # digits marked like Wordle instead of high/low
    ("hotcold", 15),             # distance only (hot/cold), no direction
    ("reverse", 5),             # high/low hints are swapped
)                                # April 1st is always "pokerface" — no hints at all
_RPS_DAILY_CAP = 10


def _pts_for_rank(rank: int) -> int:
    if rank < len(_GUESS_POINTS):
        return _GUESS_POINTS[rank]
    return 1 if rank < 10 else 0


def _solver_key(item: tuple) -> tuple:
    """Ranking: fewest attempts, then fastest solve (first attempt → correct
    guess), then who solved earlier in the day."""
    data = item[1]
    return (data["attempts"], data.get("duration", 0.0), data["solved_at"])


def _hint_text(hint_type: str, number: int, lang: str) -> str:
    if hint_type == "parity":
        return t(lang, "guess_hint_even" if number % 2 == 0 else "guess_hint_odd")
    if hint_type == "digits":
        return t(lang, "guess_hint_digits", digits=len(str(number)))
    if hint_type == "first_digit":
        return t(lang, "guess_hint_first_digit", digit=str(number)[0])
    if hint_type == "last_digit":
        return t(lang, "guess_hint_last_digit", digit=str(number)[-1])
    if hint_type == "half":
        return t(lang, "guess_hint_low_half" if number <= 5000 else "guess_hint_high_half")
    if hint_type == "div3":
        return t(lang, "guess_hint_div3_yes" if number % 3 == 0 else "guess_hint_div3_no")
    return t(lang, "guess_hint_digitsum", sum=sum(int(d) for d in str(number)))


def _daily_hints(state: dict, attempts_made: int, lang: str) -> list[str]:
    """Hints of the day in a fixed random order (same for everyone). The first
    unlocks at _GUESS_HINT_AFTER attempts, each further one every
    _GUESS_HINT_EVERY. They stay visible until the player solves the number."""
    number = state.get("number")
    if not _GUESS_HINT_AFTER or not number or attempts_made < _GUESS_HINT_AFTER:
        return []
    if state.get("modifier") == "wordle":
        return []   # Wordle feedback is hint enough
    # Legacy state stored a single "hint"; fall back to it (or a stable order).
    order = state.get("hints") or ([state["hint"]] if state.get("hint") else list(_GUESS_HINT_TYPES))
    count = 1 + (attempts_made - _GUESS_HINT_AFTER) // _GUESS_HINT_EVERY
    return [_hint_text(h, number, lang) for h in order[:count]]


def _hint_key(diff: int, low: bool) -> str:
    """Graded hint by distance: >1000 far, 101–1000 mid, <=100 close."""
    if diff > 1000:
        return "guess_too_low" if low else "guess_too_high"
    if diff > 100:
        return "guess_low" if low else "guess_high"
    return "guess_close_low" if low else "guess_close_high"


def _draw_modifier(today: str) -> str | None:
    if today.endswith("-04-01"):
        return "pokerface"   # April Fools — the whole day, no draw
    if random.random() < _GUESS_NORMAL_CHANCE:
        return None
    names   = [name for name, _ in _GUESS_MODIFIERS]
    weights = [weight for _, weight in _GUESS_MODIFIERS]
    return random.choices(names, weights=weights)[0]


def _wordle_feedback(guess: int, number: int) -> str:
    """Wordle-style feedback over 4 left-zero-padded digits: 🟩 right place,
    🟨 right digit elsewhere, ⬛ not in the number (duplicates handled).
    Returns two lines — the marks above, the digits below."""
    g, n = f"{guess:04d}", f"{number:04d}"
    marks     = ["⬛"] * 4
    remaining = []
    for i in range(4):
        if g[i] == n[i]:
            marks[i] = "🟩"
        else:
            remaining.append(n[i])
    for i in range(4):
        if marks[i] == "⬛" and g[i] in remaining:
            marks[i] = "🟨"
            remaining.remove(g[i])
    return "".join(marks) + "\n" + " ".join(g)


def _wrong_status(state: dict, guess: int, number: int, attempt: int, lang: str) -> str:
    """Status line for a wrong guess, honouring the daily modifier."""
    mod  = state.get("modifier")
    diff = abs(guess - number)
    if mod == "wordle":
        return t(lang, "guess_wordle_result", result=_wordle_feedback(guess, number), attempt=attempt)
    if mod == "hotcold":
        if diff < 10:
            key = "guess_hot_scorch"
        elif diff <= 50:
            key = "guess_hot_burn"
        elif diff <= 500:
            key = "guess_hot_warm"
        else:
            key = "guess_hot_cold"
        return t(lang, key, attempt=attempt)
    if mod == "pokerface":
        return t(lang, "guess_pokerface", attempt=attempt)
    low = guess < number
    if mod == "reverse":
        low = not low
        if diff <= 100:
            # Close guesses are "far far away" on the opposite day.
            return t(lang, "guess_rev_close_low" if low else "guess_rev_close_high", attempt=attempt)
    return t(lang, _hint_key(diff, low=low), attempt=attempt)


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


# ─────────────────────────────────────────────
#  LOCALIZATION  (t/LOCALES live in lang.py)
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
#  STORAGE HELPERS
# ─────────────────────────────────────────────

def _load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_users(data: dict) -> None:
    atomic_write_json(USERS_FILE, data, ensure_ascii=False)


def _load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_config(cfg: dict) -> None:
    atomic_write_json(CONFIG_FILE, cfg, ensure_ascii=False)


def _load_guess_state() -> dict:
    if os.path.exists(GUESS_STATE_FILE):
        with open(GUESS_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_guess_state(state: dict) -> None:
    atomic_write_json(GUESS_STATE_FILE, state, ensure_ascii=False)


async def _migrate_rps_stats() -> None:
    """One-time migration of the old rps_stats.json (wins/games per user)
    into users.json. The player with the most wins on the old leaderboard
    is credited with one season win."""
    if not os.path.exists(RPS_STATS_FILE):
        return
    with open(RPS_STATS_FILE, "r", encoding="utf-8") as f:
        old = json.load(f)

    async with _users_lock:
        users = _load_users()
        for uid, data in old.items():
            entry = users.setdefault(uid, {})
            rps   = entry.setdefault("rps", {})
            rps["wins"]  = rps.get("wins",  0) + data.get("wins",  0)
            rps["games"] = rps.get("games", 0) + data.get("games", 0)

        winners = []
        if old:
            top_wins = max(d.get("wins", 0) for d in old.values())
            if top_wins > 0:
                winners = [uid for uid, d in old.items() if d.get("wins", 0) == top_wins]
                for uid in winners:
                    entry = users.setdefault(uid, {})
                    entry["seasons_won"] = entry.get("seasons_won", 0) + 1
        _save_users(users)

    os.replace(RPS_STATS_FILE, RPS_STATS_FILE + ".migrated")
    log.info(f"Migrated rps_stats.json into users.json, old leaderboard winner(s): {winners}")


def _today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _season_name(offset_months: int = 0) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    month = now.month - offset_months
    year  = now.year
    while month < 1:
        month += 12
        year  -= 1
    return f"{month:02d}-{str(year)[2:]}"


# ─────────────────────────────────────────────
#  RPS STATS
# ─────────────────────────────────────────────

async def _record_rps_result(c_id: int, o_id: int, c_pick: str, o_pick: str) -> None:
    today = _today_utc()
    async with _users_lock:
        users = _load_users()
        for uid in (str(c_id), str(o_id)):
            entry = users.setdefault(uid, {})
            rps   = entry.setdefault("rps", {})
            rps.setdefault("wins", 0)
            rps.setdefault("games", 0)
            rps.setdefault("season_wins", 0)
            rps.setdefault("season_games", 0)
            rps.setdefault("season_pts", 0)
            rps.setdefault("today_pts", 0)
            rps.setdefault("today_date", "")
            rps["games"] += 1
            rps["season_games"] += 1
            if rps["today_date"] != today:
                rps["today_pts"]  = 0
                rps["today_date"] = today

        if c_pick != o_pick:
            winner_id = str(c_id) if _BEATS[c_pick] == o_pick else str(o_id)
            rps = users[winner_id]["rps"]
            rps["wins"] += 1
            rps["season_wins"] += 1
            if rps["today_pts"] < _RPS_DAILY_CAP:
                rps["today_pts"]  += 1
                rps["season_pts"] += 1

        _save_users(users)


def _rps_result_line(lang: str, c_id: int, o_id: int, c_pick: str, o_pick: str) -> str:
    if c_pick == o_pick:
        return f"🤝 {t(lang, 'rps_result_draw')}"
    elif _BEATS[c_pick] == o_pick:
        return f"🏆 {t(lang, 'rps_result_win', winner=f'<@{c_id}>')}"
    return f"🏆 {t(lang, 'rps_result_win', winner=f'<@{o_id}>')}"


async def _eph_edit(eph: dict | None, **kwargs) -> None:
    if not eph:
        return
    try:
        await eph["wh"].edit_message(eph["id"], **kwargs)
    except Exception as e:
        log.debug(f"Ephemeral edit failed (msg {eph.get('id')}): {e}")


# ─────────────────────────────────────────────
#  ROCK PAPER SCISSORS
# ─────────────────────────────────────────────

ROCK, PAPER, SCISSORS = "rock", "paper", "scissors"
_EMOJI = {ROCK: "✊", PAPER: "🖐️", SCISSORS: "✌️"}
_BEATS = {ROCK: SCISSORS, PAPER: ROCK, SCISSORS: PAPER}

_games: dict[int, dict] = {}


async def _pick_timeout(client: discord.Client, msg_id: int) -> None:
    await asyncio.sleep(120)
    game = _games.pop(msg_id, None)
    if not game:
        return
    log.debug(f"RPS pick timeout (msg {msg_id}).")
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
        description=(
            f"{result}\n\n"
            f"{_EMOJI[c_pick]} **{c_name}**"
            f" · **{score_c} : {score_o}** · "
            f"**{o_name}** {_EMOJI[o_pick]}"
        ),
        color=discord.Color.gold(),
    )

    await msg.edit(embed=embed, view=None)
    await _record_rps_result(c_id, o_id, c_pick, o_pick)

    ephs = game.get("ephemerals", {})
    for uid, player_lang, target_id, target_lang in (
        (c_id, lang_c, o_id, lang_o),
        (o_id, lang_o, c_id, lang_c),
    ):
        eph = ephs.get(uid)
        if not eph:
            continue
        target_name = (c_name if target_id == c_id else o_name)
        new_sc = score_c if uid == c_id else score_o
        new_so = score_o if uid == c_id else score_c
        rematch_view = RpsEphemeralRematchView(
            uid, target_id, player_lang, target_lang, ephs, msg, new_sc, new_so
        )
        eph["view"] = rematch_view
        await _eph_edit(eph, content=f"🔄 **{target_name}**", view=rematch_view)

    log.info(f"RPS resolved (msg {msg_id}): <@{c_id}> {c_pick} vs {o_pick} <@{o_id}>")


# ─────────────────────────────────────────────
#  GUESS MODAL + VIEW
# ─────────────────────────────────────────────

def _build_guess_embed(uid: str, state: dict, lang: str, status: str | None = None) -> discord.Embed:
    solvers      = state.get("solvers", {})
    attempts_made = state.get("attempts", {}).get(uid, 0)
    solved       = uid in solvers

    if status:
        user_line = status
    elif solved:
        user_line = t(lang, "guess_already_solved", attempts=solvers[uid]["attempts"])
    elif attempts_made > 0:
        user_line = t(lang, "guess_in_progress", attempts=attempts_made)
    else:
        user_line = t(lang, "guess_ongoing")

    ranked = sorted(solvers.items(), key=_solver_key)
    medals = ["🥇", "🥈", "🥉"]
    parts  = [user_line]

    if not solved:
        # Modifiers stay hidden — discovering the day's twist is the puzzle.
        # Only April Fools reveals itself (the 1–99 range has to be known).
        if state.get("modifier") == "pokerface":
            parts.append("")
            parts.append(t(lang, "guess_mod_pokerface"))
        hints = _daily_hints(state, attempts_made, lang)
        if hints:
            parts.append("")
            parts.extend(f"💡 {h}" for h in hints)

    # Players still guessing (have attempts, not solved yet), most active first.
    in_progress = sorted(
        ((u, n) for u, n in state.get("attempts", {}).items() if u not in solvers and n > 0),
        key=lambda x: x[1], reverse=True,
    )

  if ranked or in_progress:
        parts.append("")
        parts.append(t(lang, "guess_standings"))
        for i, (s_uid, data) in enumerate(ranked[:5]):
            medal = medals[i] if i < 3 else f" {i + 1}."
            # Discord markdown dynamically converts the UNIX timestamp to the viewer's local time
            solved_time = f"<t:{int(data['solved_at'])}:t>"
            
            parts.append(
                f"{medal} <@{s_uid}> — {data['attempts']} {t(lang, 'guess_attempts')}"
                f" · {_fmt_duration(data.get('duration', 0.0))} · {solved_time}"
            )
        for p_uid, n in in_progress[:5]:
            parts.append(f"⏳ <@{p_uid}> — {n} {t(lang, 'guess_attempts')}")

    return discord.Embed(
        title=t(lang, "guess_title"),
        description="\n".join(parts),
        color=discord.Color.green() if solved else discord.Color.blurple(),
    )


async def _update_guess_eph(
    interaction: discord.Interaction,
    uid: str,
    embed: discord.Embed,
    solved: bool = False,
) -> None:
    view = GuessEphemeralView(solved=solved)
    eph  = _guess_ephs.get(uid)
    if eph:
        try:
            await eph["wh"].edit_message(eph["id"], embed=embed, view=view)
            await interaction.response.defer()
            return
        except Exception as e:
            log.debug(f"Guess panel edit failed for {uid}, sending a new one: {e}")
            _guess_ephs.pop(uid, None)

    await interaction.response.defer(ephemeral=True)
    msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
    _guess_ephs[uid] = {"wh": interaction.followup, "id": msg.id}


class GuessModal(discord.ui.Modal):
    def __init__(self, lang: str):
        super().__init__(title=t(lang, "guess_modal_title"))
        self.lang = lang
        self.number_input = discord.ui.TextInput(
            label=t(lang, "guess_modal_label"),
            placeholder="1 – 9999",
            min_length=1,
            max_length=4,
        )
        self.add_item(self.number_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        lang = _get_user_lang(str(interaction.user.id)) or self.lang
        uid  = str(interaction.user.id)

        try:
            guess = int(self.number_input.value.strip())
        except ValueError:
            await interaction.response.send_message(t(lang, "guess_out_of_range"), ephemeral=True)
            return

        if not (1 <= guess <= 9999):
            await interaction.response.send_message(t(lang, "guess_out_of_range"), ephemeral=True)
            return

        state = _load_guess_state()
        today = _today_utc()
        if state.get("date") != today or not state.get("number"):
            await interaction.response.send_message(t(lang, "guess_no_active"), ephemeral=True)
            return

        if uid in state.get("solvers", {}):
            embed = _build_guess_embed(uid, state, lang)
            await _update_guess_eph(interaction, uid, embed, solved=True)
            return

        attempts = state.setdefault("attempts", {})
        attempts[uid] = attempts.get(uid, 0) + 1
        current = attempts[uid]
        number  = state["number"]

        now     = interaction.created_at.timestamp()
        started = state.setdefault("started", {})
        if uid not in started:
            started[uid] = now

        if guess == number:
            state.setdefault("solvers", {})[uid] = {
                "attempts": current,
                "solved_at": now,
                "duration": now - started[uid],
            }
            _save_guess_state(state)
            status = t(lang, "guess_correct", attempts=current)
            solved = True
            log.info(f"Guess solved by {interaction.user} in {current} attempts")
        else:
            _save_guess_state(state)
            status = _wrong_status(state, guess, number, current, lang)
            solved = False

        if not solved:
            log.debug(f"Guess attempt {current} by {interaction.user}: {guess}.")
        embed = _build_guess_embed(uid, state, lang, status=status)
        await _update_guess_eph(interaction, uid, embed, solved=solved)


class GuessEphemeralView(discord.ui.View):
    def __init__(self, solved: bool = False):
        super().__init__(timeout=None)
        if solved:
            for child in self.children:
                child.disabled = True

    @discord.ui.button(
        label="Guess",
        style=discord.ButtonStyle.primary,
        emoji="🔢",
        custom_id="guess_eph_btn",
    )
    async def guess_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        state = _load_guess_state()
        today = _today_utc()
        lang  = detect_lang(interaction)
        uid   = str(interaction.user.id)

        if state.get("date") != today or not state.get("number"):
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title=t(lang, "guess_title"),
                    description=t(lang, "guess_no_active"),
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return

        if uid in state.get("solvers", {}):
            embed = _build_guess_embed(uid, state, lang)
            await interaction.response.edit_message(embed=embed, view=GuessEphemeralView(solved=True))
            return

        await interaction.response.send_modal(GuessModal(lang))


# ─────────────────────────────────────────────
#  EPHEMERAL VIEWS
# ─────────────────────────────────────────────

class RpsEphemeralPickView(discord.ui.View):
    def __init__(self, msg_id: int, player_id: int, lang: str):
        super().__init__(timeout=None)
        self.msg_id    = msg_id
        self.player_id = player_id
        self.lang      = lang

    async def _pick(self, interaction: discord.Interaction, choice: str) -> None:
        lang = detect_lang(interaction)
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(t(lang, "rps_not_for_you"), ephemeral=True)
            return
        game = _games.get(self.msg_id)
        if not game:
            await interaction.response.edit_message(content="❌", view=None)
            return
        role_key = "challenger_pick" if interaction.user.id == game["challenger_id"] else "opponent_pick"
        if game[role_key]:
            await interaction.response.send_message(t(lang, "rps_already_picked"), ephemeral=True)
            return
        # Acknowledge first — when the ack doesn't make it in time (slow
        # network, Discord's 3s limit), leave the game state untouched so the
        # next click is a clean retry instead of "already picked".
        try:
            await interaction.response.edit_message(content=f"✅ {t(lang, 'rps_waiting')}", view=None)
        except discord.HTTPException as e:
            log.warning(f"RPS pick ack failed for {interaction.user}: {e}")
            return
        game[role_key] = choice
        self.stop()
        if game["challenger_pick"] and game["opponent_pick"]:
            await _resolve_rps(interaction.client, self.msg_id)

    @discord.ui.button(emoji="✊", style=discord.ButtonStyle.grey, custom_id="rps_pick_rock")
    async def rock(self, i: discord.Interaction, _: discord.ui.Button):
        await self._pick(i, ROCK)

    @discord.ui.button(emoji="🖐️", style=discord.ButtonStyle.grey, custom_id="rps_pick_paper")
    async def paper(self, i: discord.Interaction, _: discord.ui.Button):
        await self._pick(i, PAPER)

    @discord.ui.button(emoji="✌️", style=discord.ButtonStyle.grey, custom_id="rps_pick_scissors")
    async def scissors(self, i: discord.Interaction, _: discord.ui.Button):
        await self._pick(i, SCISSORS)


class RpsEphemeralRematchView(discord.ui.View):
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
        log.debug(f"RPS rematch offer timeout (msg {self.public_msg.id}).")
        await _eph_edit(self.ephemerals.get(self.clicker_id),
                        content=t(self.lang_c, "rps_timeout"), view=None)

    @discord.ui.button(style=discord.ButtonStyle.grey, emoji="🔄", custom_id="rps_rematch")
    async def rematch(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.clicker_id:
            await interaction.response.send_message(
                t(detect_lang(interaction), "rps_not_for_you"), ephemeral=True)
            return
        if _games.get(self.public_msg.id):
            await interaction.response.send_message("❌", ephemeral=True)
            return
        self.stop()
        target = interaction.guild.get_member(self.target_id) if interaction.guild else None
        if not target:
            await interaction.response.send_message("❌", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"⏳ {t(self.lang_c, 'rps_wait_accept', opponent=target.display_name)}",
            view=None
        )

        target_eph = self.ephemerals.get(self.target_id)
        if target_eph:
            old_view = target_eph.get("view")
            if old_view:
                old_view.stop()

        new_ephs = dict(self.ephemerals)
        clicker_eph = new_ephs.get(self.clicker_id)
        if clicker_eph:
            new_ephs[self.clicker_id] = {"wh": interaction.followup, "id": clicker_eph["id"]}

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
            await interaction.response.send_message(
                t(detect_lang(interaction), "rps_not_for_you"), ephemeral=True)
            return
        game = _games.get(self.public_msg.id)
        if not game:
            await interaction.response.edit_message(content="❌", view=None)
            return
        self.stop()

        opp_pick = RpsEphemeralPickView(self.public_msg.id, self.opponent_id, self.lang_o)
        await interaction.response.edit_message(
            content=f"🎯 {t(self.lang_o, 'rps_choose')}", view=opp_pick
        )
        self.ephemerals[self.opponent_id] = {
            "wh": interaction.followup,
            "id": self.ephemerals[self.opponent_id]["id"]
        }
        game["ephemerals"] = self.ephemerals

        c_pick = RpsEphemeralPickView(self.public_msg.id, self.challenger_id, self.lang_c)
        await _eph_edit(self.ephemerals.get(self.challenger_id),
                        content=f"🎯 {t(self.lang_c, 'rps_choose')}", view=c_pick)

        game["pick_task"] = asyncio.create_task(
            _pick_timeout(interaction.client, self.public_msg.id)
        )

    @discord.ui.button(style=discord.ButtonStyle.red, custom_id="eph_decline")
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message(
                t(detect_lang(interaction), "rps_not_for_you"), ephemeral=True)
            return
        _games.pop(self.public_msg.id, None)
        self.stop()
        declined_text = _bi(self.lang_c, self.lang_o, "rps_declined", opponent=interaction.user.mention)
        await interaction.response.edit_message(
            content=t(self.lang_o, "rps_declined", opponent=interaction.user.mention), view=None
        )
        await _eph_edit(self.ephemerals.get(self.challenger_id), content=declined_text, view=None)
        log.info(f"RPS rematch declined by {interaction.user} (msg {self.public_msg.id}).")

    async def on_timeout(self):
        _games.pop(self.public_msg.id, None)
        log.debug(f"RPS rematch accept timeout (msg {self.public_msg.id}).")
        await _eph_edit(self.ephemerals.get(self.opponent_id),   content=t(self.lang_o, "rps_timeout"), view=None)
        await _eph_edit(self.ephemerals.get(self.challenger_id), content=t(self.lang_c, "rps_timeout"), view=None)


# ─────────────────────────────────────────────
#  PUBLIC ACCEPT VIEW
# ─────────────────────────────────────────────

class RpsAcceptView(discord.ui.View):
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
            await interaction.response.send_message(
                t(detect_lang(interaction), "rps_not_for_you"), ephemeral=True)
            return
        game = _games.get(interaction.message.id)
        if not game:
            await interaction.response.send_message("❌", ephemeral=True)
            return
        self.stop()

        embed = discord.Embed(
            title=_bi_title(self.lang_c, self.lang_o, "rps_title"),
            description=_bi(self.lang_c, self.lang_o, "rps_pick_prompt",
                            challenger=f"<@{self.challenger_id}>",
                            opponent=f"<@{self.opponent_id}>"),
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)

        opp_pick = RpsEphemeralPickView(interaction.message.id, self.opponent_id, self.lang_o)
        o_eph = await interaction.followup.send(
            content=f"🎯 {t(self.lang_o, 'rps_choose')}",
            view=opp_pick, ephemeral=True, wait=True
        )
        game["ephemerals"][self.opponent_id] = {"wh": interaction.followup, "id": o_eph.id}

        c_pick = RpsEphemeralPickView(interaction.message.id, self.challenger_id, self.lang_c)
        await _eph_edit(game["ephemerals"].get(self.challenger_id),
                        content=f"🎯 {t(self.lang_c, 'rps_choose')}", view=c_pick)

        game["pick_task"] = asyncio.create_task(
            _pick_timeout(interaction.client, interaction.message.id)
        )

    @discord.ui.button(style=discord.ButtonStyle.red, custom_id="rps_decline")
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message(
                t(detect_lang(interaction), "rps_not_for_you"), ephemeral=True)
            return
        game = _games.pop(self._msg.id, None) if self._msg else None
        self.stop()
        declined_text = _bi(self.lang_c, self.lang_o, "rps_declined", opponent=interaction.user.mention)
        await interaction.response.edit_message(content=declined_text, embed=None, view=None)
        if game:
            await _eph_edit(game.get("ephemerals", {}).get(self.challenger_id),
                            content=declined_text, view=None)
        log.info(f"RPS declined by {interaction.user} (msg {interaction.message.id}).")

    async def on_timeout(self):
        if not self._msg:
            return
        game = _games.pop(self._msg.id, None)
        log.debug(f"RPS challenge timeout (msg {self._msg.id}).")
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

    async def cog_load(self):
        await _migrate_rps_stats()
        self.bot.add_view(GuessEphemeralView())
        self.daily_guess.start()

    async def cog_unload(self):
        self.daily_guess.cancel()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Safety net for stale RPS buttons. RPS games live only in memory, so
        after a bot restart clicks on old messages have no view to answer them
        and Discord shows "interaction failed" — answer them here instead."""
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith(("rps_", "eph_")):
            return
        # Give a live view (or a slow ack in flight) a moment first.
        await asyncio.sleep(2.0)
        if interaction.response.is_done():
            return
        lang = detect_lang(interaction)
        try:
            await interaction.response.edit_message(content=t(lang, "rps_timeout"), embed=None, view=None)
        except discord.HTTPException:
            pass
        log.debug(f"Stale RPS component '{custom_id}' answered for {interaction.user}.")

    # ── GUESS GAME TASK ───────────────────────

    @tasks.loop(time=datetime.time(0, 0, 0, tzinfo=datetime.timezone.utc))
    async def daily_guess(self):
        await self._rollover_guess()

    @daily_guess.before_loop
    async def before_daily_guess(self):
        await self.bot.wait_until_ready()
        # Recovery after downtime: closes out a stale game (results, points,
        # missed season reset) instead of silently dropping it.
        await self._rollover_guess()

    async def _rollover_guess(self):
        state = _load_guess_state()
        today = _today_utc()
        if state.get("date") == today and state.get("number"):
            return

        cfg   = _load_config()
        ch_id = cfg.get("game_channel_id")
        ch    = self.bot.get_channel(ch_id) if ch_id else None
        if not ch:
            log.debug("No game channel set — guess rollover runs without announcements.")

        embeds = []
        _guess_ephs.clear()

        results_embed = None
        if state.get("number"):
            if ch:
                results_embed = self._build_results_embed(state, ch)
            if state.get("solvers"):
                await self._award_guess_points(state)

        modifier = _draw_modifier(today)
        lo, hi = 1, 9999
        if modifier == "wordle":
            lo = 1000          # always 4 digits
        elif modifier == "pokerface":
            hi = 99            # tiny range to balance the missing hints
        number = random.randint(lo, hi)
        while number in _GUESS_BANNED:
            number = random.randint(lo, hi)
        new_state = {"date": today, "number": number, "attempts": {}, "started": {}, "solvers": {},
                     "hints": random.sample(_GUESS_HINT_TYPES, len(_GUESS_HINT_TYPES)),
                     "modifier": modifier}
        _save_guess_state(new_state)

        start_line = t("en", "guess_start")
        if modifier == "pokerface":
            start_line += "\n" + t("en", "guess_mod_pokerface")

        if results_embed:
            # Yesterday's results and the new-game announcement in one embed.
            results_embed.description += f"\n\n{start_line}"
            embeds.append(results_embed)

        # Season reset whenever a month boundary was crossed — also catches
        # a 1st-of-month midnight missed while the bot was offline.
        if state.get("date") and state["date"][:7] != today[:7]:
            reset_embed = await self._auto_season_reset(ch)
            embeds.append(reset_embed)

        if ch:
            if not results_embed:
                embeds.append(discord.Embed(
                    title=t("en", "guess_title"),
                    description=start_line,
                    color=discord.Color.blurple(),
                ))
            await ch.send(embeds=embeds)
        log.info(f"Guess game started: number={number}, date={today}, modifier={modifier}")

    async def _auto_season_reset(self, ch: discord.TextChannel | None) -> discord.Embed:
        guild = getattr(ch, "guild", None)
        async with _users_lock:
            users = _load_users()

            rows = []
            for uid, entry in users.items():
                rps   = entry.get("rps",   {})
                guess = entry.get("guess", {})
                total = rps.get("season_pts", 0) + guess.get("season_pts", 0)
                if total > 0:
                    rows.append((uid, total, guess.get("season_pts", 0),
                                 guess.get("season_games", 0),
                                 rps.get("season_pts", 0), rps.get("season_games", 0)))

            rows.sort(key=lambda x: x[1], reverse=True)

            if rows:
                top_total = rows[0][1]
                for uid, total, *_ in rows:
                    if total != top_total:
                        break
                    winner = users.setdefault(uid, {})
                    winner["seasons_won"] = winner.get("seasons_won", 0) + 1

            for entry in users.values():
                for section in ("rps", "guess"):
                    s = entry.setdefault(section, {})
                    s["season_pts"]   = 0
                    s["season_games"] = 0
                    if section == "rps":
                        s["season_wins"] = 0
            _save_users(users)

        medals = ["🥇", "🥈", "🥉"]
        if rows:
            lines = []
            for i, (uid, total, gpts, gg, rpts, rg) in enumerate(rows[:10]):
                member = guild.get_member(int(uid)) if guild else None
                name   = member.display_name if member else f"<@{uid}>"
                medal  = medals[i] if i < 3 else f" {i + 1}."
                lines.append(f"{medal} **{name}** — **{total}** b · 🔢 {gpts}/{gg} · ✊ {rpts}/{rg}")
            desc = "\n".join(lines)
        else:
            desc = t("en", "season_auto_reset_empty")

        log.info(f"Auto season reset ({_season_name(offset_months=1)}). Top: {rows[0] if rows else None}")
        return discord.Embed(
            title=t("en", "season_auto_reset_title", season=_season_name(offset_months=1)),
            description=desc,
            color=discord.Color.purple(),
        )

    def _build_results_embed(self, state: dict, ch: discord.TextChannel) -> discord.Embed | None:
        solvers = state.get("solvers", {})
        number  = state.get("number")
        if not number:
            return None

        guild  = getattr(ch, "guild", None)
        medals = ["🥇", "🥈", "🥉"]

        ranked = sorted(solvers.items(), key=_solver_key)
        mult   = 2 if state.get("modifier") == "pokerface" else 1

    if ranked:
            lines = []
            for i, (uid, data) in enumerate(ranked):
                member = guild.get_member(int(uid)) if guild else None
                name   = member.display_name if member else f"<@{uid}>"
                medal  = medals[i] if i < 3 else f"{i + 1}."
                pts    = _pts_for_rank(i) * mult
                # Discord markdown dynamically converts the UNIX timestamp to the viewer's local time
                solved_time = f"<t:{int(data['solved_at'])}:t>"
                
                lines.append(
                    f"{medal} **{name}** — {data['attempts']} {t('en', 'guess_attempts')}"
                    f" · {_fmt_duration(data.get('duration', 0.0))} · {solved_time} · +{pts} {t('en', 'guess_pts_label')}"
                )
            desc = "\n".join(lines)
        else:
            desc = t("en", "guess_no_solvers")

        # Reveal the day's hidden twist now that the round is over (under the title).
        mod = state.get("modifier")
        if mod:
            desc = t("en", "guess_results_mod", name=t("en", f"guess_modname_{mod}")) + "\n\n" + desc

        return discord.Embed(
            title=t("en", "guess_results_title", number=number),
            description=desc,
            color=discord.Color.blurple(),
        )

    async def _award_guess_points(self, state: dict) -> None:
        solvers = state.get("solvers", {})
        ranked  = sorted(solvers.items(), key=_solver_key)
        awarded = []
        mult    = 2 if state.get("modifier") == "pokerface" else 1
        async with _users_lock:
            users = _load_users()
            for i, (uid, _) in enumerate(ranked):
                pts   = _pts_for_rank(i) * mult
                entry = users.setdefault(uid, {})
                guess = entry.setdefault("guess", {})
                guess["season_pts"]   = guess.get("season_pts",   0) + pts
                guess["season_games"] = guess.get("season_games", 0) + 1
                guess["total_pts"]    = guess.get("total_pts",    0) + pts
                guess["total_games"]  = guess.get("total_games",  0) + 1
                awarded.append(f"{uid}+{pts}")
            _save_users(users)
        if awarded:
            log.info(f"Guess points awarded: {', '.join(awarded)}")

    # ── SLASH COMMANDS ────────────────────────

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
        description=app_commands.locale_str("Show the combined season leaderboard", key="cmd_leaderboard"),
    )
    async def leaderboard(self, interaction: discord.Interaction):
        lang  = detect_lang(interaction)
        users = _load_users()

        rows = []
        for uid, entry in users.items():
            rps   = entry.get("rps",   {})
            guess = entry.get("guess", {})
            rps_pts    = rps.get("season_pts",   0)
            guess_pts  = guess.get("season_pts", 0)
            total      = rps_pts + guess_pts
            if total == 0 and rps.get("season_games", 0) == 0 and guess.get("season_games", 0) == 0:
                continue
            rows.append((uid, entry, total, rps_pts, guess_pts, rps, guess))

        if not rows:
            await interaction.response.send_message(t(lang, "leaderboard_empty"), ephemeral=True)
            return

        rows.sort(key=lambda x: x[2], reverse=True)

        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, (uid, entry, total, rps_pts, guess_pts, rps, guess) in enumerate(rows[:10]):
            member = interaction.guild.get_member(int(uid)) if interaction.guild else None
            name   = member.display_name if member else f"<@{uid}>"
            medal  = medals[i] if i < 3 else f" {i + 1}."
            sw = entry.get("seasons_won", 0)
            rg = rps.get("season_games",   0)
            gg = guess.get("season_games", 0)
            lines.append(
                f"{medal} **{name}** [{sw}] — **{total}** {t(lang, 'leaderboard_pts')}"
                f" · 🔢 {guess_pts}/{gg} · ✊ {rps_pts}/{rg}"
            )

        embed = discord.Embed(
            title=t(lang, "leaderboard_title", season=_season_name()),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)
        log.debug(f"/leaderboard viewed by {interaction.user}")

    @app_commands.command(
        name="game-set",
        description=app_commands.locale_str("Set the channel for the daily number guessing game", key="cmd_game_set"),
    )
    @app_commands.describe(
        channel=app_commands.locale_str("Channel to use (leave empty for current channel)", key="cmd_game_set_channel"),
    )
    @app_commands.default_permissions(administrator=True)
    async def game_set(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        lang   = detect_lang(interaction)
        target = channel or interaction.channel
        cfg    = _load_config()
        cfg["game_channel_id"] = target.id
        _save_config(cfg)
        await interaction.response.send_message(
            t(lang, "game_channel_set", channel=target.mention), ephemeral=True
        )
        log.info(f"Guess channel set to #{target.name} by {interaction.user}")

        # Announce the current game right away so the channel isn't empty
        # until the next midnight rollover.
        try:
            state = _load_guess_state()
            if state.get("date") != _today_utc() or not state.get("number"):
                await self._rollover_guess()
            else:
                desc = t("en", "guess_start")
                if state.get("modifier") == "pokerface":
                    desc += "\n" + t("en", "guess_mod_pokerface")
                await target.send(embed=discord.Embed(
                    title=t("en", "guess_title"),
                    description=desc,
                    color=discord.Color.blurple(),
                ))
        except Exception as e:
            log.warning(f"Could not announce current guess game in #{target.name}: {e}")

    @app_commands.command(
        name="guess",
        description=app_commands.locale_str("Show today's guess game and your current status", key="cmd_guess_show"),
    )
    async def guess_cmd(self, interaction: discord.Interaction):
        lang  = detect_lang(interaction)
        state = _load_guess_state()
        today = _today_utc()
        uid   = str(interaction.user.id)

        if state.get("date") != today or not state.get("number"):
            await interaction.response.send_message(t(lang, "guess_no_active"), ephemeral=True)
            return

        solved = uid in state.get("solvers", {})
        embed  = _build_guess_embed(uid, state, lang)
        view   = GuessEphemeralView(solved=solved)

        await interaction.response.defer(ephemeral=True)
        msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
        _guess_ephs[uid] = {"wh": interaction.followup, "id": msg.id}
        log.debug(f"/guess opened by {interaction.user}")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            lang  = detect_lang(interaction)
            retry = round(error.retry_after)
            log.debug(f"Cooldown hit by {interaction.user} ({retry}s).")
            await interaction.response.send_message(t(lang, "rps_cooldown", seconds=retry), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GamesCog(bot))
