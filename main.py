import discord
from discord import app_commands
from discord.ext import commands
from lang import detect_lang
import os
import sys
import subprocess
import re
import io
import json
import zipfile
import urllib.request
import urllib.error
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE   = os.path.join(SCRIPT_DIR, ".env")
load_dotenv(ENV_FILE)
# místo příkazů interaktivní menu | místo reakcí tlačítka na role

# ─────────────────────────────────────────────
#  FIRST-RUN SETUP
# ─────────────────────────────────────────────

_INTERACTIVE = sys.stdin.isatty()

def _prompt_and_save(key: str, prompt: str) -> str:
    """Ask for a missing .env value in the console and append it to .env."""
    if not _INTERACTIVE:
        print(f"ERROR: Required env variable '{key}' not set. Add it to .env.", file=sys.stderr)
        sys.exit(1)
    value = input(prompt).strip()
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{key}={value}\n")
    return value

# ─────────────────────────────────────────────
#  SETTINGS  ← edit these values
# ─────────────────────────────────────────────

BOT_TOKEN  = os.getenv("BDAY_BOT_TOKEN") or os.getenv("BOT_TOKEN") \
             or _prompt_and_save("BOT_TOKEN", "Bot token not set. Paste your Discord bot token: ")
_guild_env  = os.getenv("GUILD_ID") \
              or _prompt_and_save("GUILD_ID", "Guild ID not set. Paste your Discord server ID: ")
GUILD_ID    = int(_guild_env) if _guild_env else None
_github_env = os.getenv("GITHUB_REPO")
if not _github_env:
    if _INTERACTIVE:
        _github_input = input("GitHub repo (username/repo-name, leave empty to skip): ").strip()
        if _github_input:
            with open(ENV_FILE, "a", encoding="utf-8") as f:
                f.write(f"\nGITHUB_REPO={_github_input}\n")
        GITHUB_REPO = _github_input
    else:
        GITHUB_REPO = ""
else:
    GITHUB_REPO = _github_env
GIT_BRANCH  = "main"
VERSION     = "0.2.2" #MAJOR . MINOR - new functions . PATCH - bugfix

LOCALES_DIR = os.path.join(SCRIPT_DIR, "locales")

def _load_locales() -> dict:
    locales = {}
    if os.path.isdir(LOCALES_DIR):
        for fname in os.listdir(LOCALES_DIR):
            if fname.endswith(".json"):
                with open(os.path.join(LOCALES_DIR, fname), "r", encoding="utf-8") as f:
                    locales[fname[:-5]] = json.load(f)
    return locales

_LOCALES = _load_locales()

def _t(lang: str, key: str) -> str:
    return _LOCALES.get(lang, {}).get(key) or _LOCALES.get("en", {}).get(key, key)

EXTENSIONS = [
    "lang",
    "bday",
    "poll",
    "voice",
    "noti",
    "games",
]

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)


class _DailyFileHandler(TimedRotatingFileHandler):
    # On midnight rollover: keep old file as-is, open a new file with today's date.
    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        self.baseFilename = os.path.abspath(
            os.path.join(LOGS_DIR, datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log")
        )
        self.stream = self._open()
        self.rolloverAt = self.computeRollover(int(datetime.now().timestamp()))


_LEVEL_COLORS = {
    logging.DEBUG:    "\x1b[36m",    # cyan
    logging.INFO:     "\x1b[32m",    # green
    logging.WARNING:  "\x1b[33m",    # yellow
    logging.ERROR:    "\x1b[31m",    # red
    logging.CRITICAL: "\x1b[31;1m",  # bright red
}
_RESET = "\x1b[0m"
_DIM   = "\x1b[2m"


def _enable_color() -> bool:
    if not sys.stdout.isatty():
        return False
    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # type: ignore[attr-defined]
        mode   = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):  # type: ignore[attr-defined]
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # type: ignore[attr-defined]
            return True
        return False
    except AttributeError:
        return True   # Linux/Mac — ctypes.windll doesn't exist, colors work natively
    except Exception:
        return False


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color   = _LEVEL_COLORS.get(record.levelno, "")
        ts      = self.formatTime(record, self.datefmt)
        level   = f"{color}{record.levelname:<8}{_RESET}"
        module  = f"{_DIM}[{record.name[:5]:<5}]{_RESET}"
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return f"{ts}  {level}  {module}  {message}"


_start_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
_log_path   = os.path.join(LOGS_DIR, f"{_start_time}.log")

_file_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  [%(name)-5s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = _DailyFileHandler(_log_path, when="midnight", encoding="utf-8", backupCount=0)
_file_handler.setFormatter(_file_fmt)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(
    _ColorFormatter(datefmt="%Y-%m-%d %H:%M:%S") if _enable_color() else _file_fmt
)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
logging.getLogger("discord.http").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  AUTO-UPDATE
# ─────────────────────────────────────────────

SKIP_ON_UPDATE = {".gitignore", ".gitattributes"}

def check_for_updates() -> bool:
    """Downloads ZIP from GitHub if remote VERSION is newer. Returns True if updated."""
    if not GITHUB_REPO:
        return False
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GIT_BRANCH}/main.py"
        with urllib.request.urlopen(url, timeout=10) as r:
            content = r.read().decode()

        match = re.search(r'^VERSION\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if not match:
            log.warning("VERSION not found in remote main.py — auto-update skipped.")
            return False

        remote_version = match.group(1)
        parse = lambda v: tuple(int(x) for x in v.split("-")[0].split("."))
        if parse(remote_version) <= parse(VERSION):
            log.info(f"Up to date (v{VERSION}).")
            return False

        log.info(f"Update available: v{VERSION} -> v{remote_version}. Downloading...")
        zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/{GIT_BRANCH}.zip"
        with urllib.request.urlopen(zip_url, timeout=30) as r:
            zip_data = r.read()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
            prefix = z.namelist()[0].split("/")[0] + "/"
            for item in z.namelist():
                relative = item[len(prefix):]
                if not relative:
                    continue
                top = relative.split("/")[0]
                if top in SKIP_ON_UPDATE:
                    continue
                target = os.path.join(SCRIPT_DIR, relative)
                if item.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(item) as src, open(target, "wb") as dst:
                        dst.write(src.read())

        log.info(f"Updated to v{remote_version}.")
        return True

    except urllib.error.URLError as e:
        log.warning(f"GitHub unreachable: {e}")
    except Exception as e:
        log.warning(f"Auto-update error: {e}")
    return False

# ─────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────

_restart = False

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix=[], intents=intents, status=discord.Status.idle)


@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.online)
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


async def setup_hook_fn():
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
        log.info(f"Extension loaded: {ext}")
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log.info(f"Slash commands synced for server {GUILD_ID}.")
    else:
        await bot.tree.sync()
        log.info("Slash commands synced globally.")


@bot.tree.command(name="info", description=app_commands.locale_str("Show information about the bot", key="cmd_info"))
async def info_cmd(interaction: discord.Interaction):
    lang  = detect_lang(interaction)
    embed = discord.Embed(title=_t(lang, "info_title"), color=discord.Color.blurple())
    embed.add_field(name=_t(lang, "info_version"), value=VERSION, inline=True)
    if GITHUB_REPO:
        embed.add_field(name=_t(lang, "info_github"), value=f"https://github.com/{GITHUB_REPO}", inline=True)
    embed.add_field(name="—", value=_t(lang, "info_features"), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="restart", description=app_commands.locale_str("Pull latest version from GitHub and restart the bot", key="cmd_restart"))
@app_commands.default_permissions(administrator=True)
async def restart_cmd(interaction: discord.Interaction):
    global _restart
    lang = detect_lang(interaction)
    await interaction.response.send_message(f"🔄 {_t(lang, 'restarting')}", ephemeral=True)
    log.info(f"Restart triggered by {interaction.user}.")
    _restart = True
    await bot.change_presence(status=discord.Status.idle)
    await bot.close()


bot.setup_hook = setup_hook_fn

def _do_restart():
    if sys.platform == "win32":
        subprocess.Popen([sys.executable] + sys.argv)
        sys.exit(0)
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)

if check_for_updates():
    log.info("Restarting to apply update...")
    _do_restart()

bot.run(BOT_TOKEN, log_handler=None)

if _restart:
    _do_restart()
