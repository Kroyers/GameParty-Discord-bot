import importlib.metadata
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────
#  DEPENDENCY BOOTSTRAP  (stdlib only above this point)
# ─────────────────────────────────────────────

DEPENDENCIES = {
    "discord.py":    "2.4.0",
    "aiohttp":       "3.9.0",
    "cryptography":  "42.0.0",
    "python-dotenv": "1.0.0",
}

def _ensure_dependencies() -> None:
    """Installs missing or outdated dependencies, so a fresh setup or an
    update that adds/raises a dependency starts on its own."""
    def parse(version: str) -> tuple:
        parts = []
        for x in version.split("."):
            if not x.isdigit():
                break
            parts.append(int(x))
        return tuple(parts)

    needed = []
    for package, minimum in DEPENDENCIES.items():
        try:
            if parse(importlib.metadata.version(package)) < parse(minimum):
                needed.append(f"{package}>={minimum}")
        except importlib.metadata.PackageNotFoundError:
            needed.append(f"{package}>={minimum}")
    if not needed:
        return
    if os.environ.get("GP_DEPS_RETRY"):
        # Already installed once and they are still not visible — don't loop.
        print(f"ERROR: Dependencies still unavailable after install: {', '.join(needed)}. "
              f"Run: pip install {' '.join(needed)}", file=sys.stderr)
        sys.exit(1)

    print(f"Installing dependencies: {', '.join(needed)}")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                               "--no-warn-script-location", *needed])
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Could not install dependencies ({e}). "
              f"Run: pip install {' '.join(needed)}", file=sys.stderr)
        sys.exit(1)

    # A user site-packages dir created by pip just now is not on sys.path of
    # the already-running interpreter — restart so the imports can see it.
    print("Dependencies installed — restarting...")
    os.environ["GP_DEPS_RETRY"] = "1"
    if sys.platform == "win32":
        subprocess.Popen([sys.executable] + sys.argv, env=os.environ)
        sys.exit(0)
    os.execv(sys.executable, [sys.executable] + sys.argv)

_ensure_dependencies()

import discord
from discord import app_commands
from discord.ext import commands
from lang import detect_lang, t as _t
import re
import io
import zipfile
import urllib.request
import urllib.error
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from dotenv import load_dotenv

ENV_FILE   = os.path.join(SCRIPT_DIR, ".env")
load_dotenv(ENV_FILE)
# místo reakcí tlačítka na role, /leadeboard jen neviditelný chat - udělat interaktivní menu pro hry

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
VERSION     = "0.3.5" #MAJOR . MINOR - new functions . PATCH - bugfix
STATUS_TEXT = "👥 {count}"   # bot presence text, {count} = humans on the server
LOG_LEVEL   = "INFO"         # DEBUG (everything incl. stale-button hits) | INFO (normal operation) | WARNING (problems only) | ERROR (failures only)

EXTENSIONS = [
    "lang",
    "bday",
    "poll",
    "voice",
    "noti",
    "games",
    "menu",
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

logging.basicConfig(level=LOG_LEVEL, handlers=[_file_handler, _console_handler])
# The discord library stays at INFO so LOG_LEVEL="DEBUG" only verboses bot code.
logging.getLogger("discord").setLevel(logging.INFO)
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
            root   = os.path.realpath(SCRIPT_DIR)
            for item in z.namelist():
                relative = item[len(prefix):]
                if not relative:
                    continue
                top = relative.split("/")[0]
                if top in SKIP_ON_UPDATE:
                    continue
                target = os.path.join(SCRIPT_DIR, relative)
                # Zip Slip guard: never write outside the bot directory.
                if not os.path.realpath(target).startswith(root + os.sep):
                    log.warning(f"Skipping unsafe zip entry: {item}")
                    continue
                if item.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(item) as src, open(target, "wb") as dst:
                        dst.write(src.read())

        # New or raised dependencies are handled by _ensure_dependencies()
        # when the updated script starts after the restart.
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


async def _update_member_status():
    """Sets the online presence with the current human member count."""
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else (bot.guilds[0] if bot.guilds else None)
    if guild is None:
        await bot.change_presence(status=discord.Status.online)
        return
    count = sum(1 for m in guild.members if not m.bot) or (guild.member_count or 0)
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.CustomActivity(name=STATUS_TEXT.format(count=count)),
    )
    log.debug(f"Presence updated: {count} members.")


@bot.event
async def on_ready():
    await _update_member_status()
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_member_join(_member):
    await _update_member_status()


@bot.event
async def on_member_remove(_member):
    await _update_member_status()


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


# /info for users lives in the /menu (menu.py).

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
