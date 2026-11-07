"""Where the bot keeps its files. Standard library only: run.py imports it before any dependency exists."""
import os
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(SCRIPT_DIR, "data")   # secrets and runtime data, never part of the repository
UPDATE_DIR = os.path.join(DATA_DIR, "update")   # run.py: backup, manifest and state of updates

# Files that used to live next to the code, moved into DATA_DIR on first start.
_LEGACY_DATA_FILES = (
    ".env", "bday.key", "config.json", "users.json", "voice_data.json", "noti.json",
    "guess_state.json", "watch_state.json", "poll_games.json", "poll_run.json", "rps_stats.json",
)


def data_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def migrate_data() -> list[str]:
    """Moves data files from the bot folder into DATA_DIR. Never overwrites a file already there."""
    os.makedirs(DATA_DIR, exist_ok=True)
    moved = []
    for name in _LEGACY_DATA_FILES:
        old, new = os.path.join(SCRIPT_DIR, name), data_path(name)
        if os.path.isfile(old) and not os.path.exists(new):
            shutil.move(old, new)
            moved.append(name)
    return moved


# Before any module reads or creates a data file. Above all bday.py: it would otherwise create
# a new key in data/ while the old one still sits next to the code, and stored birthdays would be lost.
migrate_data()
