import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import logging
from lang import _get_user_lang, _get_explicit_lang, _save_user_lang, clear_user_lang, detect_lang

SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE      = os.path.join(SCRIPT_DIR, "config.json")
VOICE_DATA_FILE  = os.path.join(SCRIPT_DIR, "voice_data.json")
USERS_FILE       = os.path.join(SCRIPT_DIR, "users.json")
LOCALES_DIR      = os.path.join(SCRIPT_DIR, "locales")
DEFAULT_LANG     = "en"

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  LOCALIZATION
# ─────────────────────────────────────────────

def _load_locales() -> dict:
    locales = {}
    for fname in os.listdir(LOCALES_DIR):
        if fname.endswith(".json"):
            code = fname[:-5]
            with open(os.path.join(LOCALES_DIR, fname), "r", encoding="utf-8") as f:
                locales[code] = json.load(f)
    return locales

LOCALES = _load_locales()

# Přeloží klíč do daného jazyka. Při chybějícím překladu fallback na angličtinu.
def t(lang: str, key: str, **kwargs) -> str:
    text = LOCALES.get(lang, {}).get(key)
    if text is None:
        text = LOCALES.get(DEFAULT_LANG, {}).get(key, key)
    return text.format(**kwargs) if kwargs else text

# ─────────────────────────────────────────────
#  STORAGE
#  Funkce pro čtení a zápis všech JSON souborů.
# ─────────────────────────────────────────────

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(data: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_voice_data() -> dict:
    if not os.path.exists(VOICE_DATA_FILE):
        return {}
    with open(VOICE_DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_voice_data(data: dict) -> None:
    with open(VOICE_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_voice_prefs() -> dict:
    # Vrátí {user_id: voice_prefs} ze sekce "voice" v users.json.
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {uid: record["voice"] for uid, record in data.items() if "voice" in record}

def save_voice_prefs(prefs: dict) -> None:
    # Zapíše {user_id: voice_prefs} do sekce "voice" v users.json. Ostatní data zachová.
    data = {}
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    for uid in data:
        data[uid].pop("voice", None)
    for uid, vp in prefs.items():
        if vp:
            data.setdefault(uid, {})["voice"] = vp
    data = {uid: rec for uid, rec in data.items() if rec}
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ─────────────────────────────────────────────
#  HELPERS
#  Sestavení embedu, aktualizace panelu,
#  správa oprávnění kanálu a předvoleb.
# ─────────────────────────────────────────────

# Sestaví Discord embed zobrazující stav kanálu (vlastník, zámek, limit, povolení, blokace).
def make_control_embed(guild: discord.Guild, ch_data: dict, lang: str = DEFAULT_LANG) -> discord.Embed:
    owner_id   = ch_data.get("owner_id")
    status     = t(lang, "voice_locked") if ch_data.get("locked") else t(lang, "voice_unlocked")
    user_limit = ch_data.get("user_limit", 0)
    limit_str  = f"{user_limit}" if user_limit else "∞"

    allowed = [f"<@{uid}>"  for uid in ch_data.get("allowed_users", [])] + \
              [f"<@&{rid}>" for rid in ch_data.get("allowed_roles", [])]
    banned  = [f"<@{uid}>"  for uid in ch_data.get("banned_users", [])] + \
              [f"<@&{rid}>" for rid in ch_data.get("banned_roles", [])]

    embed = discord.Embed(title=t(lang, "voice_panel_title"), color=discord.Color.blurple())
    embed.add_field(name=t(lang, "voice_field_owner"),  value=f"<@{owner_id}>", inline=True)
    embed.add_field(name=t(lang, "voice_field_status"), value=status,           inline=True)
    embed.add_field(name=t(lang, "voice_field_limit"),  value=limit_str,        inline=True)
    if allowed:
        embed.add_field(name=t(lang, "voice_field_allowed"), value=" ".join(allowed), inline=False)
    if banned:
        embed.add_field(name=t(lang, "voice_field_banned"),  value=" ".join(banned),  inline=False)
    return embed


# Upraví existující zprávu ovládacího panelu na místě (používá se při změně nastavení kanálu).
async def update_control_panel(channel: discord.VoiceChannel, ch_data: dict, lang: str = DEFAULT_LANG) -> None:
    msg_id = ch_data.get("control_message_id")
    if not msg_id:
        return
    owner_lang = _get_user_lang(str(ch_data.get("owner_id", ""))) or lang
    try:
        msg   = await channel.fetch_message(int(msg_id))
        embed = make_control_embed(channel.guild, ch_data, owner_lang)
        await msg.edit(content=f"👑 <@{ch_data['owner_id']}>", embed=embed, view=ControlView(owner_lang))
    except Exception as e:
        log.warning(f"Could not update control panel in {channel.id}: {e}")


# Při přenosu vlastnictví: smaže starý panel, pošle oznámení a hned pod ním nový panel.
async def transfer_control_panel(
    channel: discord.VoiceChannel,
    ch_data: dict,
    data: dict,
    announcement: str,
    lang: str = DEFAULT_LANG,
) -> None:
    old_msg_id = ch_data.get("control_message_id")
    if old_msg_id:
        try:
            old_msg = await channel.fetch_message(int(old_msg_id))
            await old_msg.delete()
        except Exception:
            pass
    owner_lang = _get_user_lang(str(ch_data.get("owner_id", ""))) or lang
    try:
        await channel.send(announcement)
        embed   = make_control_embed(channel.guild, ch_data, owner_lang)
        new_msg = await channel.send(
            content=f"👑 <@{ch_data['owner_id']}>",
            embed=embed,
            view=ControlView(owner_lang),
        )
        ch_data["control_message_id"] = str(new_msg.id)
        save_voice_data(data)
    except Exception as e:
        log.warning(f"Could not transfer control panel in {channel.id}: {e}")


# Přepíše všechna oprávnění kanálu jedním API voláním.
# Zahrnuje: @everyone (z kategorie), hlasovou roli, bota, vlastníka, povolené a blokované uživatele/role.
async def apply_permissions(
    channel: discord.VoiceChannel,
    ch_data: dict,
    base_overwrites: dict | None = None,
    voice_role: discord.Role | None = None,
) -> None:
    guild   = channel.guild
    bot_top = guild.me.top_role

    # Build merged overwrite dict in Python first to avoid cache issues.
    # Skip @everyone — managed explicitly below via voice_role.
    # Skip roles above the bot's top role to avoid 403.
    merged: dict = {}
    if base_overwrites:
        for target, ow in base_overwrites.items():
            if target == guild.default_role:
                continue
            can_set = (
                isinstance(target, discord.Member) or
                (isinstance(target, discord.Role) and target.position <= bot_top.position)
            )
            if can_set:
                merged[target] = discord.PermissionOverwrite(**dict(ow))

    def get_ow(target: discord.abc.Snowflake) -> discord.PermissionOverwrite:
        if target not in merged:
            merged[target] = discord.PermissionOverwrite()
        return merged[target]

    # Preserve @everyone view_channel from the category so the channel stays hidden.
    if channel.category:
        cat_ow = channel.category.overwrites_for(guild.default_role)
        if cat_ow.view_channel is not None:
            get_ow(guild.default_role).view_channel = cat_ow.view_channel

    locked = ch_data.get("locked", False)

    # Voice role: always view, connect only when unlocked.
    if voice_role:
        get_ow(voice_role).view_channel = True
        get_ow(voice_role).connect = False if locked else True

    # Bot: always view + connect so move_to works.
    get_ow(guild.me).view_channel = True
    get_ow(guild.me).connect = True

    # Owner: always view + connect in case they ban a role they belong to.
    owner = guild.get_member(int(ch_data["owner_id"]))
    if owner:
        get_ow(owner).view_channel = True
        get_ow(owner).connect = True

    for uid in ch_data.get("allowed_users", []):
        m = guild.get_member(int(uid))
        if m:
            get_ow(m).view_channel = True
            get_ow(m).connect = True
    for rid in ch_data.get("allowed_roles", []):
        r = guild.get_role(int(rid))
        if r:
            get_ow(r).view_channel = True
            get_ow(r).connect = True
    for uid in ch_data.get("banned_users", []):
        m = guild.get_member(int(uid))
        if m:
            get_ow(m).view_channel = False
            get_ow(m).connect = False
    for rid in ch_data.get("banned_roles", []):
        r = guild.get_role(int(rid))
        if r:
            get_ow(r).view_channel = False
            get_ow(r).connect = False

    try:
        await channel.edit(overwrites=merged)
    except discord.Forbidden:
        log.warning(f"Could not set overwrites for channel {channel.id}.")


# Vrátí roli potřebnou pro vstup do auto-místností (nastavenou přes /voice-role), nebo None.
def get_voice_role(guild: discord.Guild) -> discord.Role | None:
    role_id = load_config().get("voice_role_id")
    return guild.get_role(role_id) if role_id else None


REMEMBER_KEYS = ("name", "limit", "locked", "allowed_users", "allowed_roles", "banned_users", "banned_roles")

# Vrátí slovník {klíč: bool} — která nastavení si má bot pamatovat pro příští kanál uživatele.
def get_remember(user_prefs: dict) -> dict:
    r = user_prefs.get("remember", {})
    if not isinstance(r, dict):
        r = {}
    return {k: r.get(k, True) for k in REMEMBER_KEYS}


# Uloží aktuální stav kanálu do předvoleb vlastníka (jen pro klíče kde remember=True).
def save_prefs_from_channel(user_id: str, ch_data: dict) -> None:
    prefs = load_voice_prefs()
    p = prefs.setdefault(user_id, {})
    r = get_remember(p)
    if r["locked"]:        p["locked"]        = ch_data.get("locked")
    if r["allowed_users"]: p["allowed_users"] = ch_data.get("allowed_users")
    if r["allowed_roles"]: p["allowed_roles"] = ch_data.get("allowed_roles")
    if r["banned_users"]:  p["banned_users"]  = ch_data.get("banned_users")
    if r["banned_roles"]:  p["banned_roles"]  = ch_data.get("banned_roles")
    if r["limit"]:         p["user_limit"]    = ch_data.get("user_limit")
    save_voice_prefs(prefs)

# ─────────────────────────────────────────────
#  MODAL
#  Textové dialogy otevírané tlačítky Name a Limit.
# ─────────────────────────────────────────────

# Dialog pro přejmenování kanálu. Prázdné pole = reset na výchozí název.
class RenameModal(discord.ui.Modal):
    def __init__(self, lang: str = DEFAULT_LANG):
        super().__init__(title=t(lang, "voice_modal_rename_title"))
        self.lang     = lang
        self.new_name = discord.ui.TextInput(
            label=t(lang, "voice_modal_rename_label"),
            placeholder=t(lang, "voice_modal_rename_placeholder"),
            min_length=0,
            max_length=100,
            required=False,
        )
        self.add_item(self.new_name)

    async def on_submit(self, interaction: discord.Interaction):
        lang       = self.lang
        channel_id = str(interaction.channel_id)
        data       = load_voice_data()
        if channel_id not in data or str(interaction.user.id) != data[channel_id].get("owner_id"):
            await interaction.response.send_message(t(lang, "voice_err_owner_only"), ephemeral=True)
            return

        name = self.new_name.value.strip() or f"{interaction.user.display_name}'s channel"
        await interaction.channel.edit(name=name)

        prefs   = load_voice_prefs()
        user_id = str(interaction.user.id)
        if get_remember(prefs.get(user_id, {}))["name"]:
            p = prefs.setdefault(user_id, {})
            if self.new_name.value.strip():
                p["name"] = name
            else:
                p.pop("name", None)
            save_voice_prefs(prefs)

        await interaction.response.send_message(t(lang, "voice_ok_renamed", name=name), ephemeral=True)
        log.info(f"Voice channel {channel_id} renamed to '{name}' by {interaction.user}.")


# Dialog pro nastavení limitu uživatelů (0 = neomezeno).
class UserLimitModal(discord.ui.Modal):
    def __init__(self, lang: str = DEFAULT_LANG):
        super().__init__(title=t(lang, "voice_modal_limit_title"))
        self.lang  = lang
        self.limit = discord.ui.TextInput(
            label=t(lang, "voice_modal_limit_label"),
            placeholder=t(lang, "voice_modal_limit_placeholder"),
            min_length=1,
            max_length=2,
        )
        self.add_item(self.limit)

    async def on_submit(self, interaction: discord.Interaction):
        lang       = self.lang
        channel_id = str(interaction.channel_id)
        data       = load_voice_data()
        if channel_id not in data or str(interaction.user.id) != data[channel_id].get("owner_id"):
            await interaction.response.send_message(t(lang, "voice_err_owner_only"), ephemeral=True)
            return

        try:
            value = int(self.limit.value)
            if not (0 <= value <= 99):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(t(lang, "voice_err_limit_range"), ephemeral=True)
            return

        await interaction.channel.edit(user_limit=value)

        data[channel_id]["user_limit"] = value
        save_voice_data(data)
        await update_control_panel(interaction.channel, data[channel_id], lang)
        save_prefs_from_channel(str(interaction.user.id), data[channel_id])

        limit_str = f"**{value}**" if value else f"**{t(lang, 'voice_unlimited')}**"
        await interaction.response.send_message(t(lang, "voice_ok_limit", limit=limit_str), ephemeral=True)
        log.info(f"Voice channel {channel_id} user limit set to {value} by {interaction.user}.")

# ─────────────────────────────────────────────
#  MEMORY VIEW
#  Select menu pro výběr nastavení, která si bot
#  zapamatuje pro příští vytvořený kanál.
# ─────────────────────────────────────────────

class _MemorySelect(discord.ui.Select):
    def __init__(self, current: dict, lang: str = DEFAULT_LANG):
        options = [
            discord.SelectOption(label=t(lang, "voice_mem_name"),          value="name",          emoji="✏️", default=current["name"]),
            discord.SelectOption(label=t(lang, "voice_mem_limit"),         value="limit",         emoji="👥", default=current["limit"]),
            discord.SelectOption(label=t(lang, "voice_mem_locked"),        value="locked",        emoji="🔒", default=current["locked"]),
            discord.SelectOption(label=t(lang, "voice_mem_allowed_users"), value="allowed_users", emoji="✅", default=current["allowed_users"]),
            discord.SelectOption(label=t(lang, "voice_mem_allowed_roles"), value="allowed_roles", emoji="🎭", default=current["allowed_roles"]),
            discord.SelectOption(label=t(lang, "voice_mem_banned_users"),  value="banned_users",  emoji="⛔", default=current["banned_users"]),
            discord.SelectOption(label=t(lang, "voice_mem_banned_roles"),  value="banned_roles",  emoji="🚫", default=current["banned_roles"]),
        ]
        super().__init__(
            placeholder=t(lang, "voice_memory_placeholder"),
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        lang     = detect_lang(interaction)
        selected = set(self.values)
        user_id  = str(interaction.user.id)
        prefs    = load_voice_prefs()
        prefs.setdefault(user_id, {})["remember"] = {k: (k in selected) for k in REMEMBER_KEYS}
        save_voice_prefs(prefs)
        log.info(f"Voice memory settings updated for {interaction.user}: {sorted(selected) or 'none'}.")
        await interaction.response.edit_message(content=t(lang, "voice_ok_memory"), view=None)


class MemoryView(discord.ui.View):
    def __init__(self, current: dict, lang: str = DEFAULT_LANG):
        super().__init__(timeout=60)
        self.add_item(_MemorySelect(current, lang))

# ─────────────────────────────────────────────
#  LANGUAGE VIEW
#  Select menu pro změnu jazyka bota pro daného
#  uživatele (uloží do users.json, sdíleno s bday.py).
# ─────────────────────────────────────────────

# Sestaveno z dostupných locale souborů — přidání nového jazyka stačí vytvořit JSON v locales/.
_LANG_OPTIONS = [
    discord.SelectOption(
        label=LOCALES[code].get("lang_name", code),
        value=code,
        emoji=LOCALES[code].get("lang_flag"),
    )
    for code in sorted(LOCALES)
]


class _LangSelect(discord.ui.Select):
    def __init__(self, current_lang: str | None, lang: str = DEFAULT_LANG):
        auto_option = discord.SelectOption(
            label=t(lang, "lang_auto"), value="auto", emoji=None,
            default=(current_lang is None),
        )
        lang_options = [
            discord.SelectOption(
                label=opt.label, value=opt.value, emoji=opt.emoji,
                default=(opt.value == current_lang),
            )
            for opt in _LANG_OPTIONS
        ]
        super().__init__(
            placeholder=t(lang, "voice_lang_placeholder"),
            min_values=1, max_values=1,
            options=[auto_option] + lang_options,
        )

    async def callback(self, interaction: discord.Interaction):
        chosen  = self.values[0]
        user_id = str(interaction.user.id)
        lang    = detect_lang(interaction)
        if chosen == "auto":
            clear_user_lang(user_id)
            log.info(f"Language reset to auto for {interaction.user}.")
            await interaction.response.edit_message(content=t(lang, "lang_reset"), view=None)
            return
        _save_user_lang(user_id, chosen, explicit=True)
        log.info(f"Language set to '{chosen}' for {interaction.user}.")
        await interaction.response.edit_message(
            content=t(chosen, "lang_changed", name=LOCALES[chosen].get("lang_name", chosen)), view=None
        )
        channel_id = str(interaction.channel_id)
        data = load_voice_data()
        ch_data = data.get(channel_id)
        if ch_data and user_id == ch_data.get("owner_id"):
            await update_control_panel(interaction.channel, ch_data, chosen)


class LangView(discord.ui.View):
    def __init__(self, current_lang: str | None, lang: str = DEFAULT_LANG):
        super().__init__(timeout=60)
        self.add_item(_LangSelect(current_lang, lang))

# ─────────────────────────────────────────────
#  SELECT VIEWS
#  Ephemeral views se selectem uživatele nebo role,
#  které se zobrazí po kliknutí na tlačítka panelu.
# ─────────────────────────────────────────────

# UserActionView: select uživatele pro akce allow, ban, kick, transfer.
class _UserSelect(discord.ui.UserSelect):
    async def callback(self, interaction: discord.Interaction):
        view: "UserActionView" = self.view
        await view.handle(interaction, self.values[0])


class UserActionView(discord.ui.View):
    def __init__(self, action: str, channel_id: int, lang: str = DEFAULT_LANG):
        super().__init__(timeout=60)
        self.action     = action
        self.channel_id = channel_id
        self.add_item(_UserSelect(placeholder=t(lang, "voice_select_user")))

    async def handle(self, interaction: discord.Interaction, target: discord.Member):
        lang    = detect_lang(interaction)
        channel = interaction.guild.get_channel(self.channel_id)
        data    = load_voice_data()
        ch_data = data.get(str(self.channel_id))

        if channel is None or ch_data is None:
            await interaction.response.edit_message(content=t(lang, "voice_err_not_found"), view=None)
            return

        uid = str(target.id)

        if self.action == "allow_user":
            lst = ch_data.setdefault("allowed_users", [])
            if uid in lst:
                lst.remove(uid)
                msg = t(lang, "voice_ok_allowed_removed", mention=target.mention)
                log.info(f"Voice channel {self.channel_id}: allow removed for {target} by {interaction.user}.")
            else:
                lst.append(uid)
                ch_data.setdefault("banned_users", [])
                if uid in ch_data["banned_users"]:
                    ch_data["banned_users"].remove(uid)
                msg = t(lang, "voice_ok_allowed_added", mention=target.mention)
                log.info(f"Voice channel {self.channel_id}: {target} allowed by {interaction.user}.")

        elif self.action == "ban_user":
            if uid == str(interaction.user.id):
                await interaction.response.edit_message(content=t(lang, "voice_err_self_ban"), view=None)
                return
            lst = ch_data.setdefault("banned_users", [])
            if uid in lst:
                lst.remove(uid)
                msg = t(lang, "voice_ok_unbanned", mention=target.mention)
                log.info(f"Voice channel {self.channel_id}: {target} unbanned by {interaction.user}.")
            else:
                lst.append(uid)
                ch_data.setdefault("allowed_users", [])
                if uid in ch_data["allowed_users"]:
                    ch_data["allowed_users"].remove(uid)
                msg = t(lang, "voice_ok_banned", mention=target.mention)
                log.info(f"Voice channel {self.channel_id}: {target} banned by {interaction.user}.")
                member = interaction.guild.get_member(target.id)
                if member and member.voice and member.voice.channel and member.voice.channel.id == self.channel_id:
                    await member.move_to(None)

        elif self.action == "kick":
            member = interaction.guild.get_member(target.id)
            if member and member.voice and member.voice.channel and member.voice.channel.id == self.channel_id:
                await member.move_to(None)
                msg = t(lang, "voice_ok_kicked", mention=target.mention)
                log.info(f"Voice channel {self.channel_id}: {target} kicked by {interaction.user}.")
            else:
                msg = t(lang, "voice_err_not_in_channel", mention=target.mention)

        elif self.action == "transfer":
            if uid == str(interaction.user.id):
                await interaction.response.edit_message(content=t(lang, "voice_err_already_owner"), view=None)
                return
            if not any(m.id == target.id for m in channel.members):
                await interaction.response.edit_message(content=t(lang, "voice_err_not_in_channel", mention=target.mention), view=None)
                return
            ch_data["owner_id"] = uid
            save_voice_data(data)
            await apply_permissions(channel, ch_data, voice_role=get_voice_role(interaction.guild))
            await transfer_control_panel(
                channel, ch_data, data,
                announcement=t(lang, "voice_ok_new_owner", mention=target.mention),
                lang=lang,
            )
            save_prefs_from_channel(str(interaction.user.id), ch_data)
            log.info(f"Voice channel {self.channel_id}: ownership transferred to {target} by {interaction.user}.")
            self.stop()
            await interaction.response.edit_message(content=t(lang, "voice_ok_transferred", mention=target.mention), view=None)
            return

        else:
            msg = "❌"

        save_voice_data(data)
        await apply_permissions(channel, ch_data, voice_role=get_voice_role(interaction.guild))
        await update_control_panel(channel, ch_data, lang)
        save_prefs_from_channel(str(interaction.user.id), ch_data)
        self.stop()
        await interaction.response.edit_message(content=msg, view=None)


# RoleActionView: select role pro akce allow_role a ban_role.
class _RoleSelect(discord.ui.RoleSelect):
    async def callback(self, interaction: discord.Interaction):
        view: "RoleActionView" = self.view
        await view.handle(interaction, self.values[0])


class RoleActionView(discord.ui.View):
    def __init__(self, action: str, channel_id: int, lang: str = DEFAULT_LANG):
        super().__init__(timeout=60)
        self.action     = action
        self.channel_id = channel_id
        self.add_item(_RoleSelect(placeholder=t(lang, "voice_select_role")))

    async def handle(self, interaction: discord.Interaction, target: discord.Role):
        lang    = detect_lang(interaction)
        channel = interaction.guild.get_channel(self.channel_id)
        data    = load_voice_data()
        ch_data = data.get(str(self.channel_id))

        if channel is None or ch_data is None:
            await interaction.response.edit_message(content=t(lang, "voice_err_not_found"), view=None)
            return

        rid = str(target.id)

        if self.action == "allow_role":
            lst = ch_data.setdefault("allowed_roles", [])
            if rid in lst:
                lst.remove(rid)
                msg = t(lang, "voice_ok_allowed_removed", mention=target.mention)
                log.info(f"Voice channel {self.channel_id}: allow removed for role {target.name} by {interaction.user}.")
            else:
                lst.append(rid)
                ch_data.setdefault("banned_roles", [])
                if rid in ch_data["banned_roles"]:
                    ch_data["banned_roles"].remove(rid)
                msg = t(lang, "voice_ok_allowed_added", mention=target.mention)
                log.info(f"Voice channel {self.channel_id}: role {target.name} allowed by {interaction.user}.")

        elif self.action == "ban_role":
            lst = ch_data.setdefault("banned_roles", [])
            if rid in lst:
                lst.remove(rid)
                msg = t(lang, "voice_ok_unbanned", mention=target.mention)
                log.info(f"Voice channel {self.channel_id}: role {target.name} unbanned by {interaction.user}.")
            else:
                lst.append(rid)
                ch_data.setdefault("allowed_roles", [])
                if rid in ch_data["allowed_roles"]:
                    ch_data["allowed_roles"].remove(rid)
                msg = t(lang, "voice_ok_banned", mention=target.mention)
                log.info(f"Voice channel {self.channel_id}: role {target.name} banned by {interaction.user}.")
                for member in list(channel.members):
                    if target in member.roles:
                        await member.move_to(None)

        else:
            msg = "❌"

        save_voice_data(data)
        await apply_permissions(channel, ch_data, voice_role=get_voice_role(interaction.guild))
        await update_control_panel(channel, ch_data, lang)
        save_prefs_from_channel(str(interaction.user.id), ch_data)
        self.stop()
        await interaction.response.edit_message(content=msg, view=None)

# ─────────────────────────────────────────────
#  CONTROL PANEL VIEW  (persistent)
#  Hlavní ovládací panel kanálu. Persistent view
#  (timeout=None) přežije restart bota díky custom_id.
#  Tlačítka jsou přeložena do jazyka vlastníka.
# ─────────────────────────────────────────────

# Mapování custom_id tlačítka → locale klíč pro překlad labelu.
_BUTTON_LABELS = {
    "vc_lock":        "voice_btn_privacy",
    "vc_rename":      "voice_btn_name",
    "vc_limit":       "voice_btn_limit",
    "vc_transfer":    "voice_btn_transfer",
    "vc_allow_user":  "voice_btn_allow_user",
    "vc_allow_role":  "voice_btn_allow_role",
    "vc_kick":        "voice_btn_kick",
    "vc_ban_user":    "voice_btn_ban_user",
    "vc_ban_role":    "voice_btn_ban_role",
    "vc_memory":      "voice_btn_memory",
    "vc_clear_prefs": "voice_btn_clear_prefs",
    "vc_lang":        "voice_btn_lang",
}


class ControlView(discord.ui.View):
    def __init__(self, lang: str = DEFAULT_LANG):
        super().__init__(timeout=None)
        if lang != DEFAULT_LANG:
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.custom_id in _BUTTON_LABELS:
                    child.label = t(lang, _BUTTON_LABELS[child.custom_id])

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        data = load_voice_data()
        ch   = data.get(str(interaction.channel_id), {})
        if str(interaction.user.id) != ch.get("owner_id"):
            lang = detect_lang(interaction)
            await interaction.response.send_message(t(lang, "voice_err_owner_only"), ephemeral=True)
            return False
        return True

    # ── Row 0: základní nastavení kanálu ────────
    @discord.ui.button(label="Privacy", style=discord.ButtonStyle.red,     emoji="🔒", custom_id="vc_lock",     row=0)
    async def lock_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        channel    = interaction.channel
        channel_id = str(channel.id)
        data       = load_voice_data()
        ch_data    = data[channel_id]

        lang = detect_lang(interaction)
        ch_data["locked"] = not ch_data.get("locked", False)
        save_voice_data(data)
        await apply_permissions(channel, ch_data, voice_role=get_voice_role(interaction.guild))
        await update_control_panel(channel, ch_data, lang)
        save_prefs_from_channel(str(interaction.user.id), ch_data)

        state = "locked" if ch_data["locked"] else "unlocked"
        log.info(f"Voice channel {channel_id} {state} by {interaction.user}.")
        msg = t(lang, "voice_ok_locked") if ch_data["locked"] else t(lang, "voice_ok_unlocked")
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Name",     style=discord.ButtonStyle.blurple, emoji="✏️", custom_id="vc_rename",   row=0)
    async def rename_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang = detect_lang(interaction)
        await interaction.response.send_modal(RenameModal(lang))

    @discord.ui.button(label="Limit",    style=discord.ButtonStyle.blurple, emoji="👥", custom_id="vc_limit",    row=0)
    async def limit_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang = detect_lang(interaction)
        await interaction.response.send_modal(UserLimitModal(lang))

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.grey,    emoji="🔄", custom_id="vc_transfer", row=1)
    async def transfer_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang = detect_lang(interaction)
        view = UserActionView("transfer", interaction.channel_id, lang)
        await interaction.response.send_message(t(lang, "voice_prompt_new_owner"), view=view, ephemeral=True)

    # ── Row 1: přístup a převod vlastnictví ─────
    @discord.ui.button(label="Allow User", style=discord.ButtonStyle.green, emoji="✅", custom_id="vc_allow_user", row=1)
    async def allow_user_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang = detect_lang(interaction)
        view = UserActionView("allow_user", interaction.channel_id, lang)
        await interaction.response.send_message(t(lang, "voice_prompt_allow_user"), view=view, ephemeral=True)

    @discord.ui.button(label="Allow Role", style=discord.ButtonStyle.green, emoji="🎭", custom_id="vc_allow_role", row=1)
    async def allow_role_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang = detect_lang(interaction)
        view = RoleActionView("allow_role", interaction.channel_id, lang)
        await interaction.response.send_message(t(lang, "voice_prompt_allow_role"), view=view, ephemeral=True)

    # ── Row 2: odebrání přístupu ─────────────────
    @discord.ui.button(label="Kick",     style=discord.ButtonStyle.red, emoji="👢", custom_id="vc_kick",     row=2)
    async def kick_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang = detect_lang(interaction)
        view = UserActionView("kick", interaction.channel_id, lang)
        await interaction.response.send_message(t(lang, "voice_prompt_kick"), view=view, ephemeral=True)

    @discord.ui.button(label="Ban User", style=discord.ButtonStyle.red, emoji="⛔", custom_id="vc_ban_user", row=2)
    async def ban_user_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang = detect_lang(interaction)
        view = UserActionView("ban_user", interaction.channel_id, lang)
        await interaction.response.send_message(t(lang, "voice_prompt_ban_user"), view=view, ephemeral=True)

    @discord.ui.button(label="Ban Role", style=discord.ButtonStyle.red, emoji="⛔", custom_id="vc_ban_role", row=2)
    async def ban_role_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang = detect_lang(interaction)
        view = RoleActionView("ban_role", interaction.channel_id, lang)
        await interaction.response.send_message(t(lang, "voice_prompt_ban_role"), view=view, ephemeral=True)

    # ── Row 3: předvolby uživatele ───────────────
    @discord.ui.button(label="Memory settings", style=discord.ButtonStyle.grey, emoji="💾", custom_id="vc_memory",      row=3)
    async def memory_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang    = detect_lang(interaction)
        user_id = str(interaction.user.id)
        prefs   = load_voice_prefs()
        current = get_remember(prefs.get(user_id, {}))
        view    = MemoryView(current, lang)
        await interaction.response.send_message(t(lang, "voice_prompt_memory"), view=view, ephemeral=True)

    @discord.ui.button(label="Restart settings", style=discord.ButtonStyle.grey, emoji="🗑️", custom_id="vc_clear_prefs", row=3)
    async def clear_prefs_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        lang    = detect_lang(interaction)
        user_id = str(interaction.user.id)
        prefs   = load_voice_prefs()
        prefs.pop(user_id, None)
        save_voice_prefs(prefs)
        log.info(f"Voice preferences cleared for {interaction.user}.")
        await interaction.response.send_message(t(lang, "voice_ok_prefs_cleared"), ephemeral=True)

    @discord.ui.button(label="Language", style=discord.ButtonStyle.grey, emoji="🌐", custom_id="vc_lang", row=3)
    async def lang_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        lang         = detect_lang(interaction)
        user_id      = str(interaction.user.id)
        current_lang = _get_explicit_lang(user_id)
        view         = LangView(current_lang, lang)
        await interaction.response.send_message(t(lang, "voice_prompt_lang"), view=view, ephemeral=True)
        # Update panel immediately if owner — catches first-time English panel
        data    = load_voice_data()
        ch_data = data.get(str(interaction.channel_id))
        if ch_data and ch_data.get("owner_id") == user_id:
            await update_control_panel(interaction.channel, ch_data, lang)

# ─────────────────────────────────────────────
#  COG
#  Hlavní třída bota. Obsahuje admin příkazy
#  a listener na vstup/odchod z hlasových kanálů.
# ─────────────────────────────────────────────

class VoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot  = bot
        cfg       = load_config()
        self.trigger_channel_id = cfg.get("voice_trigger_id")  # kanál jehož vstup spustí vytvoření místnosti
        self.voice_role_id      = cfg.get("voice_role_id")      # role potřebná pro vstup do auto-místností

    async def cog_load(self):
        # Zaregistruje persistentní ControlView aby tlačítka fungovala i po restartu bota.
        self.bot.add_view(ControlView())

    @app_commands.command(name="voice-set", description=app_commands.locale_str("Set the trigger voice channel for auto-rooms", key="cmd_voice_set"))
    @app_commands.describe(channel=app_commands.locale_str("Voice channel that creates a new room on join", key="cmd_voice_set_channel"))
    @app_commands.default_permissions(administrator=True)
    async def voice_set(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        self.trigger_channel_id = channel.id
        cfg = load_config()
        cfg["voice_trigger_id"] = channel.id
        save_config(cfg)
        lang = detect_lang(interaction)
        await interaction.response.send_message(t(lang, "voice_trigger_set", channel=channel.mention), ephemeral=True)
        log.info(f"Voice trigger set to #{channel.name} ({channel.id}) by {interaction.user}.")

    @app_commands.command(name="voice-role", description=app_commands.locale_str("Set the role required to join auto-rooms", key="cmd_voice_role"))
    @app_commands.describe(role=app_commands.locale_str("Role that gets connect permission on every auto-room", key="cmd_voice_role_param"))
    @app_commands.default_permissions(administrator=True)
    async def voice_role(self, interaction: discord.Interaction, role: discord.Role):
        self.voice_role_id = role.id
        cfg = load_config()
        cfg["voice_role_id"] = role.id
        save_config(cfg)
        lang = detect_lang(interaction)
        await interaction.response.send_message(t(lang, "voice_role_set", role=role.mention), ephemeral=True)
        log.info(f"Voice role set to @{role.name} ({role.id}) by {interaction.user}.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        data = load_voice_data()

        # ── Odchod z auto-místnosti: smazání prázdné nebo auto-přenos vlastnictví ──
        if before.channel and str(before.channel.id) in data:
            channel_id = str(before.channel.id)
            ch_data    = data[channel_id]
            channel    = before.channel
            humans     = [m for m in channel.members if not m.bot]

            if not humans:
                del data[channel_id]
                save_voice_data(data)
                try:
                    await channel.delete(reason="Auto-room empty.")
                    log.info(f"Deleted empty voice room {channel_id}.")
                except Exception as e:
                    log.warning(f"Could not delete voice channel {channel_id}: {e}")

            elif str(member.id) == ch_data.get("owner_id"):
                new_owner = humans[0]
                ch_data["owner_id"] = str(new_owner.id)
                owner_lang = _get_user_lang(str(new_owner.id)) or DEFAULT_LANG
                await apply_permissions(channel, ch_data, voice_role=get_voice_role(channel.guild))
                await transfer_control_panel(
                    channel, ch_data, data,
                    announcement=t(owner_lang, "voice_ok_new_owner", mention=new_owner.mention),
                    lang=owner_lang,
                )
                log.info(f"Voice room {channel_id} ownership auto-transferred to {new_owner}.")

        # ── Vstup do trigger kanálu: vytvoření nové auto-místnosti ──────────
        if after.channel and after.channel.id == self.trigger_channel_id:
            user_id    = str(member.id)
            prefs      = load_voice_prefs()
            user_prefs = prefs.get(user_id, {})
            r          = get_remember(user_prefs)

            name = (user_prefs.get("name") if r["name"] else None) or f"{member.display_name}'s channel"

            trigger = after.channel

            # User limit: use remembered preference if available, else copy from trigger.
            user_limit = (user_prefs.get("user_limit") if r["limit"] else None)
            if user_limit is None:
                user_limit = trigger.user_limit

            # Create channel first (only needs Manage Channels).
            # Audio/video settings are copied from the trigger channel.
            new_channel = await member.guild.create_voice_channel(
                name               = name,
                category           = after.channel.category,
                bitrate            = trigger.bitrate,
                user_limit         = user_limit,
                rtc_region         = trigger.rtc_region,
                video_quality_mode = trigger.video_quality_mode,
                reason             = f"Auto-room for {member}",
            )

            ch_data = {
                "owner_id":           user_id,
                "locked":             user_prefs.get("locked", False) if r["locked"] else False,
                "user_limit":         user_limit,
                "allowed_users":      list(user_prefs.get("allowed_users") or []) if r["allowed_users"] else [],
                "allowed_roles":      list(user_prefs.get("allowed_roles") or []) if r["allowed_roles"] else [],
                "banned_users":       list(user_prefs.get("banned_users")  or []) if r["banned_users"]  else [],
                "banned_roles":       list(user_prefs.get("banned_roles")  or []) if r["banned_roles"]  else [],
                "control_message_id": None,
            }

            voice_role = member.guild.get_role(self.voice_role_id) if self.voice_role_id else None
            await apply_permissions(new_channel, ch_data, base_overwrites=trigger.overwrites, voice_role=voice_role)
            await member.move_to(new_channel)

            member_lang = _get_user_lang(user_id) or DEFAULT_LANG
            embed    = make_control_embed(member.guild, ch_data, member_lang)
            ctrl_msg = await new_channel.send(
                content = f"👑 {member.mention}",
                embed   = embed,
                view    = ControlView(member_lang),
            )
            ch_data["control_message_id"] = str(ctrl_msg.id)
            data[str(new_channel.id)] = ch_data
            save_voice_data(data)
            log.info(f"Created voice room '{name}' ({new_channel.id}) for {member}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceCog(bot))
