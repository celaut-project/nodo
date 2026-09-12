"""What a node asks its operator once, before it does anything else.

Two questions, and they are asked here rather than by the installer because the
installer is not always a place where a question can be asked. On Windows the node
is installed by ``bash/install.ps1``, which pipes ``curl`` into ``bash`` inside the
distro and, compiled as ``Nodo-Setup.exe``, runs with no console at all. Anything
``install.sh`` asks there is asked into a pipe: skipped in silence, default kept.

So the install stays quiet and the *first run* asks. That is the first moment there
is reliably a terminal and a person in front of it, on every platform, and it is the
same moment on Linux -- ``nodo`` is what the operator types next either way.

The two questions are not the same kind of thing and are not treated as one:

* **KyA** is a refusal. Declining stops the node, because the KyA is what the
  operator is agreeing to in order to run it at all.
* **The donation share** is a default with a number attached. Declining is setting it
  to ``0``, which is one keystroke, and the node runs either way. What matters is
  that the figure is *shown* -- a default nobody is told about is not consent, and
  2 % of earnings leaving a wallet is not something to discover later.

Both are recorded by a marker in ``storage/``, so neither is asked twice. The KyA
marker is the one that already existed (``.acceptedkya``); an install that has it and
not the donation marker is an *upgrade*, and its operator already answered the
donation question in ``install.sh`` back when it asked. Seeding the marker for them
is what stops this from nagging nodes that have been running for months.

Nothing here is fatal except a declined KyA. A node that cannot write a marker asks
again next time, which is mildly annoying; a node that refuses to start because it
could not write a dotfile is worse.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

#: Where the answers are remembered, under ``storage/``.
KYA_MARKER = ".acceptedkya"
DONATION_MARKER = ".askeddonation"

#: The key the share is written to. Ergo only, like ``install.sh`` asked for: it is
#: the payment system a node has switched on by default, and a question about a
#: ledger that is off is a question about nothing.
DONATION_KEY = "ledgers.ergo.payments.DONATION_PERCENTAGE"

#: Shown when config.yaml has nothing to say, which should not happen.
FALLBACK_SHARE = "0.02"

RULE = "-" * 60


def _marker(main_dir: str, name: str) -> str:
    return os.path.join(main_dir, "storage", name)


def _has(main_dir: str, name: str) -> bool:
    return os.path.exists(_marker(main_dir, name))


def _record(main_dir: str, name: str) -> bool:
    """Write a marker. False if it could not be written -- never raises."""
    try:
        path = _marker(main_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a"):
            pass
        return True
    except OSError as e:
        print(f"nodo: could not record {name} ({e}); this will be asked again.", flush=True)
        return False


def _interactive() -> bool:
    """Whether there is someone to ask.

    Both ends checked, not just stdin: a question needs an answer *and* somewhere to
    appear. Under ``Nodo-Setup.exe`` there is no console on either side.
    """
    return sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()


def accept_kya(main_dir: str) -> bool:
    """Show the KyA and ask. True if the node may continue.

    Delegates to the script that already does this so there is one copy of the
    wording and one copy of the pager handling. Unlike the call this replaced, the
    **exit code is honoured**: ``accept_kya.sh`` returns 1 on a refusal, and running
    it through ``os.system`` without looking meant "no" was accepted as readily as
    "yes".
    """
    if _has(main_dir, KYA_MARKER):
        return True

    script = os.path.join(main_dir, "bash", "accept_kya.sh")
    if not os.path.exists(script):
        # Nothing to show. Not a reason to refuse to run: a missing doc is a broken
        # install, and the node saying so is more use than the node exiting silently.
        print(f"nodo: {script} is missing; cannot show the KyA.", flush=True)
        return True

    try:
        return subprocess.run(["/bin/bash", script, main_dir]).returncode == 0
    except OSError as e:
        print(f"nodo: could not run the KyA script ({e}).", flush=True)
        return True


def _current_share(main_dir: str) -> str:
    """What ``config.yaml`` says today, read without importing the config stack.

    Through ``yq``, the same binary ``install.sh`` and the TUI use. Reading it with a
    YAML library would be easy; *writing* it back would reformat the file and drop
    every comment in it, and this runs on a config the operator is meant to keep
    reading.
    """
    yq = os.path.join(main_dir, "bin", "yq")
    config = os.path.join(main_dir, "config.yaml")
    if not (os.access(yq, os.X_OK) and os.path.isfile(config)):
        return FALLBACK_SHARE
    try:
        result = subprocess.run(
            [yq, "-r", f".{DONATION_KEY} // \"\"", config],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return FALLBACK_SHARE
    value = (result.stdout or "").strip()
    return value if value and value != "null" else FALLBACK_SHARE


def _write_share(main_dir: str, share: str) -> bool:
    """Set the share in ``config.yaml`` through ``yq``, preserving the file."""
    yq = os.path.join(main_dir, "bin", "yq")
    config = os.path.join(main_dir, "config.yaml")
    if not (os.access(yq, os.X_OK) and os.path.isfile(config)):
        print("nodo: yq is unavailable; config.yaml was left as it is.", flush=True)
        return False
    try:
        # The value goes through the environment rather than into the expression, so
        # nothing typed at the prompt can be read as yq syntax. Same rule the TUI's
        # config editor follows.
        result = subprocess.run(
            [yq, "-i", f".{DONATION_KEY} = strenv(NODO_SHARE)", config],
            env={**os.environ, "NODO_SHARE": share}, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        print(f"nodo: could not write the donation share ({e}).", flush=True)
        return False


def valid_share(answer: str) -> Optional[str]:
    """A share is a fraction of one. ``None`` if it is not one.

    Refused rather than coerced, for the reason ``install.sh`` gives: ``2`` meant as a
    percentage would donate everything this node earns.
    """
    answer = "".join(answer.split())
    if not answer:
        return None
    try:
        value = float(answer)
    except ValueError:
        return None
    if value != value or not (0.0 <= value <= 1.0):  # NaN, or out of range
        return None
    return answer


def ask_donation(main_dir: str) -> None:
    """Show the donation share and let it be changed. Never fatal, never blocking."""
    if _has(main_dir, DONATION_MARKER):
        return

    # An install that accepted the KyA before this question existed has already been
    # asked it by install.sh. Record it as answered rather than asking again.
    if _has(main_dir, KYA_MARKER):
        _record(main_dir, DONATION_MARKER)
        return

    suggested = _current_share(main_dir)
    answer = os.environ.get("NODO_DONATION_PERCENTAGE")

    if answer is None:
        if not _interactive():
            # No terminal: leave the config alone and leave the marker unwritten, so
            # the next run on a terminal asks. Silence is not an answer.
            return
        print(f"\n{RULE}\n Donations\n{RULE}")
        print(
            " nodo can donate a share of what this node EARNS to the people who\n"
            " write it. It applies to incoming payments, not to your savings, and\n"
            " the transaction fee comes out of that share -- never on top of it.\n\n"
            " It is not charity for its own sake: other nodes read donations off the\n"
            " chain and weigh them when they choose whom to delegate work to, so it\n"
            " buys a better position in their routing. Nothing here is enforced --\n"
            " this is open source, and you can set it to 0 now or edit it later.\n\n"
            " Who is funded, and whose contributions this node recognises, are the\n"
            " two wallet lists under ledgers.ergo.payments in config.yaml.\n"
            " See docs/DONATIONS.md.\n"
        )
        try:
            answer = input(f" Share of earnings to donate [{suggested}]: ")
        except (EOFError, KeyboardInterrupt):
            # Ctrl-C out of the question is not consent, so nothing is recorded and
            # the node still starts on whatever the config already said.
            print("\n Skipped; config.yaml keeps its current share.", flush=True)
            return

    share = valid_share(answer) if answer.strip() else suggested
    if share is None:
        print(f" '{answer.strip()}' is not a share between 0 and 1; keeping {suggested}.")
        share = suggested

    if share == suggested or _write_share(main_dir, share):
        if share == "0":
            print(" Donations off. Change DONATION_PERCENTAGE in config.yaml to turn them on.")
        else:
            print(f" Donating {share} of incoming payments. Edit config.yaml to change it.")
        _record(main_dir, DONATION_MARKER)


def run(main_dir: str) -> bool:
    """The whole first run. False means the node must not continue.

    KyA first, and the donation question only after it is accepted: asking someone to
    fund the project before they have agreed to run it is the wrong order, and a
    refused KyA means there is no node to donate anything.
    """
    if not accept_kya(main_dir):
        return False
    _record(main_dir, KYA_MARKER)
    ask_donation(main_dir)
    return True
