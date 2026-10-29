"""
Task hooks: shell commands run before and after a saved task.

    "CMP": {
      ...,
      "before_command": "echo starting",
      "after_command": ["flat_dev_pass", "touch ~/cmp.done"]
    }

Each key takes a string or a list of strings. Commands run through
`/bin/sh -c` (like git hooks or npm scripts), in the directory dbw was
started from, with dbw's environment, and their output goes straight to the
terminal.

Shell aliases and functions from ~/.zshrc are NOT available: `sh -c` does
not read them, and loading an interactive shell for every hook would be slow
and noisy. Turn an alias into a script on the PATH, or write the command
itself in the hook.

Rules:
- before_command fails (non-zero exit) -> the task does not run, nor does
  after_command, and the task fails.
- the task fails -> after_command is skipped. A failed copy may have dropped
  and half-filled the target; post-processing that state as if it were good
  is worse than not running at all.
- after_command fails -> the task fails (non-zero exit for cron and `&&`).
- In a list, the first failing command stops the ones after it.

There is no timeout, on purpose: same reasoning as the dump pipes (2.5.1), a
fixed deadline cannot tell "slow" from "hung". Ctrl-C stops the command.
"""

import subprocess
from typing import Any

from rich.console import Console

console = Console()

BEFORE = 'before_command'
AFTER = 'after_command'


def hook_commands(task_config: dict[str, Any], key: str) -> list[str]:
    """
    Normalise a hook value into a list of commands.

    Missing, None, "" and [] all mean "no hook". Blank entries in a list are
    dropped. Anything else (a number, a dict, a list holding one) is a broken
    settings file and raises ValueError: guessing what it meant could run the
    wrong thing.
    """
    value = task_config.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
        raise ValueError(f"'{key}' must be a string or a list of strings")
    return [c for c in (c.strip() for c in value) if c]


def run_hook(task_config: dict[str, Any], key: str) -> bool:
    """Run the commands of one hook in order. True when all of them exit 0 (or there are none)."""
    label = 'before' if key == BEFORE else 'after'
    for command in hook_commands(task_config, key):
        console.print(f"\n[bold cyan]▶ {label}:[/bold cyan] {command}")
        # shell=True is the point of the feature: the user writes a shell
        # command line (pipes, &&, redirects, ~). The command comes from the
        # user's own settings file, the same trust level as the task itself.
        result = subprocess.run(command, shell=True)
        if result.returncode != 0:
            console.print(f"[red]❌ {label} command failed (exit {result.returncode}): {command}[/red]")
            return False
    return True


def describe_hooks(task_config: dict[str, Any]) -> list[tuple[str, str]]:
    """(label, command) rows for summaries, so the confirmation shows what will run."""
    rows = []
    for key, label in ((BEFORE, 'Before'), (AFTER, 'After')):
        try:
            commands = hook_commands(task_config, key)
        except ValueError as e:
            rows.append((label, f"[red]invalid: {e}[/red]"))
            continue
        rows.extend((label, command) for command in commands)
    return rows
