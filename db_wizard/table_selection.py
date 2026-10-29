"""
Table selection: glob patterns and source/target pairing.

A task's `source_collection` list can mix exact names and shell-style
patterns:

    ["users", "audit*", "compliance_*", "!compliance_*_backup"]

- An exact name is copied as-is (unchanged behaviour: it is not checked
  against the source, the engine reports a missing table itself).
- A pattern (`*`, `?`, `[...]`) is expanded against the tables that exist on
  the source AT RUN TIME. That is the whole point: the task is saved with the
  pattern, not with a snapshot of its matches, so tables created after the
  task was saved are picked up without editing the settings file.
- An entry starting with `!` removes every table it matches from the result,
  whatever entry added it. A list made only of exclusions starts from all the
  source's tables.

Matching is case-sensitive (`fnmatchcase`): MySQL table names are
case-sensitive on Linux servers, and a pattern that silently matched a
differently-cased table locally would behave differently in production.
"""

from fnmatch import fnmatchcase

# Characters that turn an entry into a glob pattern. Anything else is an
# exact table name.
_GLOB_CHARS = ('*', '?', '[')


def is_pattern(entry: str) -> bool:
    """True for a glob or an exclusion, i.e. anything that needs the source's table list."""
    return entry.startswith('!') or any(ch in entry for ch in _GLOB_CHARS)


def has_patterns(entries) -> bool:
    """True when a selection (string or list) contains at least one pattern."""
    if isinstance(entries, str):
        return is_pattern(entries)
    if isinstance(entries, list):
        return any(isinstance(e, str) and is_pattern(e) for e in entries)
    return False


def resolve_tables(entries: list[str], available: list[str]) -> list[str]:
    """
    Expand patterns against `available` (the source's table names).

    Order is kept: entries are processed left to right, a pattern contributes
    its matches sorted by name, and a table appears once (first occurrence
    wins). Exclusions are applied last, so their position in the list does
    not matter.

    Raises ValueError when an include pattern matches nothing. A pattern that
    matches nothing is almost always a typo or a connection that returned an
    empty table list (MySQL's list_tables returns [] on error); in an
    unattended `-y` run, copying "nothing" and reporting success would hide
    both, so the task stops before touching the target.
    """
    available_sorted = sorted(available)
    includes = [e for e in entries if not e.startswith('!')]
    excludes = [e[1:] for e in entries if e.startswith('!')]

    selected: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            selected.append(name)

    if not includes:
        # Only exclusions: "everything except ...".
        for name in available_sorted:
            add(name)

    for entry in includes:
        if is_pattern(entry):
            matches = [name for name in available_sorted if fnmatchcase(name, entry)]
            if not matches:
                raise ValueError(f"Pattern '{entry}' matches no table on the source")
            for name in matches:
                add(name)
        else:
            add(entry)

    return [name for name in selected if not any(fnmatchcase(name, ex) for ex in excludes)]


def pair_tables(source: list[str], target: list[str] | None) -> list[tuple[str, str]]:
    """
    Pair each source table with its target name.

    `source` must already be resolved (no patterns left). `target` is the
    task's `target_collection`:
      - None, or the same list as the source: same names on the target.
      - A list of the same length: positional renaming.
      - Anything else is refused.

    This replaces a bare `zip(source, target)`, which truncates silently to
    the shorter list: a task with 10 source tables and 9 target names copied
    9 tables, skipped the last one and still reported success.
    """
    if target is None or target == source:
        return [(name, name) for name in source]
    if not isinstance(target, list):
        raise ValueError("target_collection must be a list when source_collection is a list")
    if len(target) != len(source):
        raise ValueError(
            f"source_collection has {len(source)} entries but target_collection has "
            f"{len(target)}: they must match one to one (or drop target_collection "
            f"to keep the same names)"
        )
    return list(zip(source, target))


def resolve_task_tables(source_entries, target_entries, available_loader,
                        expand_patterns: bool = True) -> list[tuple[str, str]]:
    """
    Resolve a task's table selection into (source, target) pairs.

    `available_loader` is a zero-argument callable returning the source's
    table names. It is only called when there are patterns, so tasks with
    plain names do not pay for a table listing (slow over an SSH tunnel).
    A string selection with a pattern ("compliance_*") is treated as a
    one-entry list.

    With `expand_patterns=False` (engines like Redis, whose entries are key
    patterns, not table names) entries are only paired, never expanded; the
    length check still applies.
    """
    if isinstance(source_entries, str):
        source_entries = [source_entries]

    if expand_patterns and has_patterns(source_entries):
        # A renaming list cannot be lined up with a pattern whose size is only
        # known at run time, so patterns always copy to the same names.
        if target_entries is not None and target_entries != source_entries:
            raise ValueError(
                "source_collection uses patterns, so target_collection must be omitted "
                "(or identical): matched tables keep their names on the target"
            )
        resolved = resolve_tables(source_entries, available_loader())
        if not resolved:
            # Every match was excluded: same reasoning as an empty pattern.
            raise ValueError("The selection resolves to no table after exclusions")
        return [(name, name) for name in resolved]

    return pair_tables(source_entries, target_entries)
