"""Starts and watches the bot (main.py) and installs updates from GitHub.

Start the bot with this file, e.g. in systemd: ExecStart=/usr/local/bin/python3.14 /path/to/run.py
Standard library only, and kept small: it has to work even when an update breaks everything else.

  1. Before every start: if GitHub has a newer VERSION, back up the current files and install the new ones.
  2. Start main.py and wait until it reports that it is connected to Discord.
  3. A new version that crashes or doesn't connect in time is replaced by the backup again.
     A version that crashed is remembered and never installed automatically again.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

import paths

MAIN_FILE      = os.path.join(paths.SCRIPT_DIR, "main.py")
STATE_FILE     = os.path.join(paths.UPDATE_DIR, "state.json")      # phase, versions, versions that failed
MANIFEST_FILE  = os.path.join(paths.UPDATE_DIR, "manifest.json")   # files the last update installed
BACKUP_DIR     = os.path.join(paths.UPDATE_DIR, "backup")
STAGING_DIR    = os.path.join(paths.UPDATE_DIR, "staging")
READY_FILE     = os.path.join(paths.UPDATE_DIR, "ready")           # written by main.py once connected

GIT_BRANCH     = "main"
RESTART_CODE   = 3      # exit code of main.py asking for a restart (and an update check)
READY_TIMEOUT  = 300    # seconds a freshly updated bot gets to connect to Discord
CRASH_DELAYS   = (5, 15, 60, 300)   # waits between restarts after crashes in a row

# Never written, deleted or backed up by an update: data, logs, git and editor folders, local tools.
PROTECTED_TOP  = {"data", "logs", ".git", ".claude", ".vscode", "__pycache__", "venv", ".venv"}
SKIP_FROM_ZIP  = {".gitignore", ".gitattributes"}


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  RUNNER    {msg}", flush=True)


def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("-")[0].split("."))


def version_in(text: str) -> str | None:
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def local_version() -> str:
    try:
        with open(MAIN_FILE, "r", encoding="utf-8") as f:
            return version_in(f.read()) or "0"
    except OSError:
        return "0"


def github_repo() -> str:
    for path in (paths.data_path(".env"), os.path.join(paths.SCRIPT_DIR, ".env")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                match = re.search(r"^GITHUB_REPO=(.+)$", f.read(), re.MULTILINE)
            if match:
                return match.group(1).strip().strip('"')
        except OSError:
            pass
    return ""


def is_protected(relative: str) -> bool:
    return relative.replace("\\", "/").split("/")[0] in PROTECTED_TOP


def target_of(relative: str) -> str:
    target = os.path.realpath(os.path.join(paths.SCRIPT_DIR, relative))
    if not target.startswith(os.path.realpath(paths.SCRIPT_DIR) + os.sep):
        raise ValueError(f"unsafe path in update: {relative}")   # zip slip guard
    return target

# ─────────────────────────────────────────────
#  UPDATE / ROLLBACK
# ─────────────────────────────────────────────

def download(repo: str) -> tuple[str, dict[str, bytes]] | None:
    """(version, {relative path: content}) of the branch on GitHub, or None if it isn't newer or usable."""
    url = f"https://raw.githubusercontent.com/{repo}/{GIT_BRANCH}/main.py"
    with urllib.request.urlopen(url, timeout=10) as r:
        remote = version_in(r.read().decode())
    current = local_version()
    if not remote or parse_version(remote) <= parse_version(current):
        log(f"Up to date (v{current}).")
        return None
    if remote in read_json(STATE_FILE, {}).get("failed", []):
        log(f"v{remote} failed before, not installing it again. Push a newer version.")
        return None

    log(f"Update available: v{current} -> v{remote}. Downloading...")
    zip_url = f"https://github.com/{repo}/archive/refs/heads/{GIT_BRANCH}.zip"
    with urllib.request.urlopen(zip_url, timeout=60) as r:
        data = r.read()
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        prefix = z.namelist()[0].split("/")[0] + "/"
        for item in z.namelist():
            relative = item[len(prefix):]
            if not relative or item.endswith("/") or relative in SKIP_FROM_ZIP or is_protected(relative):
                continue
            target_of(relative)
            files[relative] = z.read(item)
    version = version_in(files.get("main.py", b"").decode("utf-8", "replace"))
    if not version:
        log("Downloaded update has no main.py with VERSION, skipped.")
        return None
    return version, files


def install(version: str, files: dict[str, bytes]) -> None:
    """Backs up the files it touches, then puts the new ones in place. Any crash on the way is rolled back
    on the next start, because the state says "installing" until everything is done."""
    old_manifest = read_json(MANIFEST_FILE, None)
    removed = sorted(set(old_manifest or []) - set(files)) if old_manifest is not None else []
    removed = [r for r in removed if not is_protected(r)]

    # 1. Everything new is written next to the bot first, so a broken download never touches it.
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    for relative, content in files.items():
        path = os.path.join(STAGING_DIR, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)

    # 2. Backup of every file the update replaces or deletes; files it adds are listed to be removed on rollback.
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    created = []
    for relative in sorted(set(files) | set(removed)):
        source = target_of(relative)
        if os.path.isfile(source):
            backup = os.path.join(BACKUP_DIR, relative)
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(source, backup)
        else:
            created.append(relative)
    state = read_json(STATE_FILE, {})
    state.update({"phase": "installing", "from": local_version(), "to": version,
                  "created": created, "previous_manifest": old_manifest})
    write_json(STATE_FILE, state)

    # 3. Swap the files in and delete the ones the repository no longer has.
    for relative in files:
        target = target_of(relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(os.path.join(STAGING_DIR, relative), target)
    for relative in removed:
        try:
            os.remove(target_of(relative))
            log(f"Deleted {relative} (no longer in the repository).")
        except FileNotFoundError:
            pass
    shutil.rmtree(STAGING_DIR, ignore_errors=True)

    write_json(MANIFEST_FILE, sorted(files))
    state["phase"] = "verifying"
    write_json(STATE_FILE, state)
    log(f"Installed v{version}, waiting for it to connect.")


def rollback(failed: bool) -> None:
    """Puts the backed up files back. failed=True remembers the version so it isn't installed again."""
    state = read_json(STATE_FILE, {})
    for root, _dirs, names in os.walk(BACKUP_DIR):
        for name in names:
            backup = os.path.join(root, name)
            relative = os.path.relpath(backup, BACKUP_DIR)
            target = target_of(relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(backup, target)
    for relative in state.get("created", []):
        try:
            os.remove(target_of(relative))
        except FileNotFoundError:
            pass
    if state.get("previous_manifest") is None:
        try:
            os.remove(MANIFEST_FILE)
        except FileNotFoundError:
            pass
    else:
        write_json(MANIFEST_FILE, state["previous_manifest"])
    if failed and state.get("to"):
        state.setdefault("failed", []).append(state["to"])
    log(f"Rolled back from v{state.get('to')} to v{state.get('from')}"
        + (" — this version will be skipped from now on." if failed else ", will try again next time."))
    write_json(STATE_FILE, {"failed": state.get("failed", [])})


def update_if_available() -> None:
    repo = github_repo()
    if not repo:
        log("GITHUB_REPO not set, no update check.")
        return
    try:
        result = download(repo)
        if result:
            install(*result)
    except Exception as e:
        log(f"Update failed, keeping the current version: {e}")
        if read_json(STATE_FILE, {}).get("phase") == "installing":
            rollback(failed=False)

# ─────────────────────────────────────────────
#  RUNNING
# ─────────────────────────────────────────────

def run_bot() -> int:
    """Runs main.py once. Returns its exit code; rolls back an update that doesn't come up."""
    try:
        os.remove(READY_FILE)
    except FileNotFoundError:
        pass
    env = dict(os.environ, GP_RUNNER="1")
    child = subprocess.Popen([sys.executable, MAIN_FILE, *sys.argv[1:]], env=env)
    verifying = read_json(STATE_FILE, {}).get("phase") == "verifying"
    started = time.monotonic()
    try:
        while True:
            code = child.poll()
            ready = os.path.exists(READY_FILE)
            if verifying and ready:
                log(f"v{local_version()} is up and running.")
                write_json(STATE_FILE, {"failed": read_json(STATE_FILE, {}).get("failed", [])})
                verifying = False
            if code is not None:
                if verifying and code != RESTART_CODE:
                    log(f"The new version stopped (exit code {code}) before connecting.")
                    rollback(failed=True)
                    return RESTART_CODE
                return code
            if verifying and time.monotonic() - started > READY_TIMEOUT:
                log(f"The new version didn't connect within {READY_TIMEOUT} s.")
                child.terminate()
                try:
                    child.wait(30)
                except subprocess.TimeoutExpired:
                    child.kill()
                rollback(failed=False)
                return RESTART_CODE
            time.sleep(1)
    except KeyboardInterrupt:
        child.wait()
        return 0


def main() -> None:
    moved = paths.migrate_data()
    if moved:
        log(f"Moved data files into data/: {', '.join(moved)}")
    if read_json(STATE_FILE, {}).get("phase") == "installing":
        log("The last update was interrupted.")
        rollback(failed=False)

    crashes = 0
    while True:
        update_if_available()
        started = time.monotonic()
        code = run_bot()
        if code == 0:
            log("Bot stopped.")
            return
        if code == RESTART_CODE:
            crashes = 0
            log("Restarting...")
            continue
        crashes = 0 if time.monotonic() - started > 600 else crashes + 1
        delay = CRASH_DELAYS[min(crashes, len(CRASH_DELAYS) - 1)]
        log(f"Bot crashed (exit code {code}), restarting in {delay} s.")
        time.sleep(delay)


if __name__ == "__main__":
    main()
