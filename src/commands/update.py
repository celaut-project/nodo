"""``nodo update``: pull new code only when the node can run it as it is (#518).

The incoming version has to be compatible with the installed one: ``compat.json`` (see
``src/utils/compat.py``) says what the node depends on, and the command compares the
manifest at ``HEAD`` with the one at ``origin/<branch>``. If they are equal it pulls,
refreshes the install and restarts the daemon, going back to the previous commit if the
node does not come up. If they differ it stops with nothing changed, and the operator
updates by hand: migrating the configuration, telling peers about the protocol, and so on.
"""
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.utils import compat

# How long a restarted node has to answer on its gateway port before it counts as down.
START_TIMEOUT_SECONDS = 90

UP_TO_DATE = "up_to_date"
DIRTY = "dirty"
INCOMPATIBLE = "incompatible"
UNKNOWN = "unknown"
COMPATIBLE = "compatible"


class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result


def current_branch(repo: str) -> str:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch == "HEAD":
        raise GitError("HEAD is detached, so there is no branch to update from.")
    return branch


def local_changes(repo: str) -> List[str]:
    """Tracked files with changes, staged or not. Untracked files are not touched by an update."""
    # -z, because the default output quotes paths with spaces and prints a rename as
    # "old -> new", which no field split can read back.
    output = _git(repo, "status", "--porcelain", "-z", "--untracked-files=no").stdout
    entries = output.split("\0")
    paths: List[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":  # A rename or copy carries its source as the next entry.
            index += 1
    return paths


def restore_local_changes(repo: str) -> None:
    _git(repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", ":/")


def _manifest_at(repo: str, ref: str) -> Optional[Dict[str, object]]:
    result = _git(repo, "show", f"{ref}:{compat.MANIFEST_FILE}", check=False)
    return compat.read_manifest(result.stdout if result.returncode == 0 else None)


@dataclass
class Verdict:
    status: str
    branch: str = ""
    installed: str = ""
    incoming: str = ""
    dirty: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)


def check_update(repo: str, branch: Optional[str] = None, force: bool = False) -> Verdict:
    """Steps 2 to 5 of the flow: fetch, then decide. Changes only what ``git fetch`` does."""
    branch = branch or current_branch(repo)
    _git(repo, "fetch", "origin", branch)
    installed = _git(repo, "rev-parse", "HEAD").stdout.strip()
    incoming = _git(repo, "rev-parse", f"origin/{branch}").stdout.strip()
    verdict = Verdict(UP_TO_DATE, branch, installed, incoming)
    if installed == incoming:
        return verdict
    if _git(repo, "merge-base", "--is-ancestor", installed, incoming, check=False).returncode != 0:
        verdict.status = UNKNOWN
        verdict.lines = [f"Local {branch} is not behind origin/{branch}: it cannot be fast-forwarded."]
        return verdict

    verdict.dirty = local_changes(repo)
    if verdict.dirty and not force:
        verdict.status = DIRTY
        verdict.lines = ["Tracked files have local changes:", *[f"  {path}" for path in verdict.dirty],
                         "Commit or stash them, or run with --force to discard them."]
        return verdict

    before = _manifest_at(repo, installed)
    if before is None and os.path.realpath(repo) == os.path.realpath(compat.ROOT):
        # An install older than compat.json: it is the running code, and the tree is clean.
        before = compat.compute_manifest(repo)
    after = _manifest_at(repo, f"origin/{branch}")
    if before is None or after is None:
        side = "installed" if before is None else "incoming"
        verdict.status = UNKNOWN
        verdict.lines = [f"The {side} version has no readable {compat.MANIFEST_FILE}, so compatibility cannot be checked."]
        return verdict

    verdict.changed = compat.changed_items(before, after)
    if verdict.changed:
        verdict.status = INCOMPATIBLE
        verdict.lines = ["The incoming version is not compatible with the installed one:",
                         *[compat.describe_change(item, before, after) for item in verdict.changed]]
        return verdict
    verdict.status = COMPATIBLE
    return verdict


def _report(verdict: Verdict) -> None:
    if verdict.status == UP_TO_DATE:
        print("Already up to date.", flush=True)
        return
    for line in verdict.lines:
        print(line, flush=True)
    if verdict.status == COMPATIBLE:
        print(f"Compatible: {verdict.installed[:12]} -> {verdict.incoming[:12]}.", flush=True)
    elif verdict.status == INCOMPATIBLE:
        print("Nothing was changed. Update by hand: migrate the configuration if the keys changed, "
              "and tell your peers about changes to the protocol.", flush=True)
    elif verdict.status in (DIRTY, UNKNOWN):
        print("Nothing was changed.", flush=True)


def _wait_serving(is_serving) -> bool:
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_serving():
            return True
        time.sleep(2)
    return False


def apply_update(repo: str, verdict: Verdict, install_command: Optional[List[str]] = None) -> bool:
    """Step 6: pull, refresh the install, restart; back to the previous commit if it fails."""
    from src.commands.daemon import daemon_command, is_serving

    was_serving = is_serving()
    previous = verdict.installed
    if install_command is None:
        install_command = ["/bin/bash", os.path.join(repo, "install.sh"), "--target-dir", repo]

    def roll_back(reason: str) -> bool:
        print(f"{reason} Going back to {previous[:12]}.", flush=True)
        _git(repo, "reset", "--hard", previous)
        # The new install may have changed the dependencies the old code runs on.
        if subprocess.run(install_command, cwd=repo).returncode != 0:
            print("Reinstalling the previous version failed: its dependencies may not match.", flush=True)
        if was_serving and not daemon_command("restart", None):
            print("The previous version did not restart either. Check `nodo daemon status`.", flush=True)
        return False

    try:
        if verdict.dirty:  # Only --force gets here; discard right before the pull, not before the checks.
            print("Discarding local changes:", *verdict.dirty, sep="\n  ", flush=True)
            restore_local_changes(repo)
        _git(repo, "pull", "--ff-only", "origin", verdict.branch)
    except GitError as error:
        print(f"{error}\nNothing was changed.", flush=True)
        return False
    if subprocess.run(install_command, cwd=repo).returncode != 0:
        return roll_back("The install step failed.")
    if was_serving and os.geteuid() != 0:
        # A node installed without sudo has no unit this user may restart.
        print("The running node is still on the old version. Restart it to load the new one.", flush=True)
    elif was_serving:
        if not daemon_command("restart", None) or not _wait_serving(is_serving):
            return roll_back("The node did not start on the new version.")
    print(f"Updated to {verdict.incoming[:12]}.", flush=True)
    return True


def update_command(repo: str, check_only: bool = False, force: bool = False) -> bool:
    try:
        verdict = check_update(repo, force=force)
        _report(verdict)
        if check_only or verdict.status == UP_TO_DATE:
            return verdict.status in (UP_TO_DATE, COMPATIBLE)
        if verdict.status != COMPATIBLE:
            return False
        return apply_update(repo, verdict)
    except GitError as error:
        print(f"Error: {error}", flush=True)
        return False
