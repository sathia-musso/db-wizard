"""
Centralized task runner - engine-agnostic.
Handles copy, backup, and restore tasks.
"""

from typing import Any
from rich.console import Console
from .engine import EngineFactory
from .formatting import format_number
from .utils import resolve_host

console = Console()


def run_task(task_config: dict[str, Any], assume_yes: bool = False, force_python: bool = False,
             schema_only: bool = False, incremental: bool = False,
             since_column: str | None = None) -> bool:
    """
    Execute a task based on its type.
    Auto-detects engine from URIs in the task config.

    schema_only: run a copy task copying only structure/DDL, no data.
                 Ignored for backup/restore tasks (no schema concept there).
    incremental: run a copy task copying only the rows above the target's
                 watermark. Same rules: copy tasks only.
    since_column: watermark column override for incremental.

    The task's before_command / after_command hooks wrap every task type
    (see hooks.py for the rules): before runs first and can veto the task,
    after runs only when the task succeeded.
    """
    from .hooks import AFTER, BEFORE, run_hook

    task_type = task_config.get('type', 'copy')

    try:
        if not run_hook(task_config, BEFORE):
            console.print("[red]❌ Task not run: before_command failed[/red]")
            return False

        if task_type == 'backup':
            ok = run_backup_task(task_config)
        elif task_type == 'restore':
            ok = run_restore_task(task_config)
        else:
            ok = run_copy_task(
                task_config, assume_yes, force_python, schema_only,
                incremental=incremental, since_column=since_column
            )

        if not ok:
            # Say it explicitly: a silently missing post-step (e.g. resetting
            # the copied users' passwords) is easy to overlook.
            if task_config.get(AFTER):
                console.print("[yellow]⚠ after_command skipped: the task failed[/yellow]")
            return False

        return run_hook(task_config, AFTER)
    except Exception as e:
        console.print(f"[red]❌ Task error: {e}[/red]")
        return False


def run_copy_task(task_config: dict[str, Any], assume_yes: bool = False, force_python: bool = False,
                  schema_only: bool = False, incremental: bool = False,
                  since_column: str | None = None) -> bool:
    """Execute a copy task using the appropriate engine."""
    from .utils import resolve_host

    # schema_only can come from the saved task itself or be forced via the CLI
    # flag for this single run. Either one enables it.
    schema_only = schema_only or task_config.get('schema_only', False)

    # Same for incremental, and the CLI column overrides the saved one.
    incremental = incremental or task_config.get('incremental', False)
    since_column = since_column or task_config.get('since_column')

    if incremental:
        scheme = EngineFactory.detect_scheme(task_config['source_uri'])
        if scheme not in ('mysql', 'mongodb'):
            console.print(
                f"[yellow]⚠ --incremental is not supported for {scheme}. "
                f"Supported: mysql, mongodb.[/yellow]"
            )
            return False
        if schema_only:
            console.print("[yellow]⚠ --incremental and --schema-only are mutually exclusive.[/yellow]")
            return False
        if task_config.get('drop_target', False):
            console.print(
                "[yellow]⚠ This task drops the target, which erases the watermark "
                "--incremental resumes from. Refusing.[/yellow]"
            )
            return False
        console.print("[cyan]⏩ Incremental: only rows above the target watermark[/cyan]")

    # Schema-only is meaningless on schemaless engines: refuse up-front before
    # opening tunnels/connections, instead of copying everything by surprise.
    if schema_only:
        scheme = EngineFactory.detect_scheme(task_config['source_uri'])
        if scheme in ('mongodb', 'redis'):
            engine_name = 'MongoDB' if scheme == 'mongodb' else 'Redis'
            console.print(f"[yellow]⚠ {engine_name} is schemaless — --schema-only is not supported.[/yellow]")
            return False
        console.print("[cyan]🏗️  Schema-only: structure/DDL, no data[/cyan]")

    source_info = resolve_host(task_config['source_uri'], task_config.get('source_ssh_tunnel'))
    target_info = resolve_host(task_config['target_uri'], task_config.get('target_ssh_tunnel'))

    # Open SSH tunnels if needed (rewrites URI to localhost:free_port)
    source_uri = source_info.uri
    target_uri = target_info.uri
    if source_info.has_tunnel:
        from .tunnel import open_tunnel
        source_uri = open_tunnel(source_uri, source_info.tunnel_config)
    if target_info.has_tunnel:
        from .tunnel import open_tunnel
        target_uri = open_tunnel(target_uri, target_info.tunnel_config)

    # Create engines from URIs (auto-detects MongoDB vs MySQL)
    source_engine = EngineFactory.create(source_uri)
    target_engine = EngineFactory.create(target_uri)

    # Cross-engine copy not supported
    EngineFactory.check_same_engine(source_engine, target_engine)

    try:
        source_engine.connect()
        target_engine.connect()

        source_table = task_config.get('source_collection')
        target_table = task_config.get('target_collection', source_table)

        from .table_selection import has_patterns, resolve_task_tables

        # Engines whose table entries are not names (Redis key patterns) keep
        # the old behaviour: entries are passed through untouched.
        expand_patterns = source_engine.supports_table_patterns
        uses_patterns = expand_patterns and has_patterns(source_table)

        if isinstance(source_table, list) or uses_patterns:
            # Multiple tables (or a pattern, which can match many).
            # Resolve and pair everything BEFORE the first copy: a bad
            # selection (pattern matching nothing, source/target lists of
            # different lengths) must abort with the target untouched, not
            # halfway through a drop-and-copy run.
            try:
                pairs = resolve_task_tables(
                    source_table,
                    target_table if isinstance(target_table, list) else None,
                    lambda: [t['name'] for t in source_engine.list_tables(task_config['source_db'])],
                    expand_patterns=expand_patterns,
                )
            except ValueError as e:
                console.print(f"[red]❌ Invalid table selection: {e}[/red]")
                return False

            if uses_patterns:
                # Show what the patterns expanded to, so an unattended run's
                # log records exactly which tables were copied this time.
                console.print(
                    f"[cyan]🔎 Patterns resolved to {len(pairs)} "
                    f"{target_engine.table_term_plural}: {', '.join(src for src, _ in pairs)}[/cyan]"
                )

            from .utils import copy_failed

            total_docs = 0
            skipped = []
            for src_t, tgt_t in pairs:
                console.print(f"\n[bold]📁 Copying: {src_t}[/bold]")
                result = target_engine.copy(
                    source_engine=source_engine,
                    source_db=task_config['source_db'],
                    source_table=src_t,
                    target_db=task_config['target_db'],
                    target_table=tgt_t,
                    drop_target=task_config.get('drop_target', False),
                    force=assume_yes,
                    force_python=task_config.get('force_python', force_python),
                    schema_only=schema_only,
                    incremental=incremental,
                    since_column=since_column,
                )
                total_docs += result.get('documents_copied', 0)
                if copy_failed(result):
                    skipped.append(src_t)
            # Count the pairs actually processed, not the raw config list:
            # with patterns the two differ.
            copied = len(pairs) - len(skipped)
            console.print(f"[green]✅ Copied {copied} {target_engine.table_term_plural}, {format_number(total_docs)} rows[/green]")
            # Per-table refusals must not hide behind the total (same rule as
            # the whole-database branch below).
            if skipped:
                console.print(f"[yellow]⚠ Skipped {len(skipped)}: {', '.join(skipped)}[/yellow]")
                return False
        elif source_table:
            # Single table
            result = target_engine.copy(
                source_engine=source_engine,
                source_db=task_config['source_db'],
                source_table=source_table,
                target_db=task_config['target_db'],
                target_table=target_table,
                drop_target=task_config.get('drop_target', False),
                force=assume_yes,
                force_python=task_config.get('force_python', force_python),
                schema_only=schema_only,
                incremental=incremental,
                since_column=since_column,
            )
            from .utils import copy_failed

            method = result.get('method', 'unknown')
            if copy_failed(result):
                console.print(f"[red]❌ Copy did not run (reason: {method})[/red]")
                return False
            console.print(f"[green]✅ Copied {format_number(result['documents_copied'])} rows (method: {method})[/green]")
        else:
            results = target_engine.copy(
                source_engine=source_engine,
                source_db=task_config['source_db'],
                source_table=None,
                target_db=task_config['target_db'],
                target_table=None,
                drop_target=task_config.get('drop_target', False),
                force=assume_yes,
                force_python=task_config.get('force_python', force_python),
                schema_only=schema_only,
                incremental=incremental,
                since_column=since_column,
            )
            # Results can be a dict of dicts (one per table) or a single dict
            if isinstance(results, dict) and all(isinstance(v, dict) for v in results.values()):
                from .utils import copy_failed

                total_docs = sum(r['documents_copied'] for r in results.values())
                console.print(f"[green]✅ Copied {len(results)} {target_engine.table_term_plural}, {format_number(total_docs)} rows[/green]")
                # Per-table refusals must not hide behind the total.
                skipped = [name for name, r in results.items() if copy_failed(r)]
                if skipped:
                    console.print(f"[yellow]⚠ Skipped {len(skipped)}: {', '.join(skipped)}[/yellow]")
            else:
                docs = results.get('documents_copied', 0)
                console.print(f"[green]✅ Copied {format_number(docs)} rows[/green]")

        return True

    except Exception as e:
        console.print(f"[red]❌ Copy error: {e}[/red]")
        return False
    finally:
        source_engine.close()
        target_engine.close()


def _get_uri(task_config: dict[str, Any]) -> str:
    """Get database URI from task config."""
    return task_config['db_uri']


def run_backup_task(task_config: dict[str, Any]) -> bool:
    """Execute a backup task."""
    from .backup import BackupManager
    from .utils import resolve_host

    host_info = resolve_host(task_config['db_uri'], task_config.get('ssh_tunnel'))
    db_uri = host_info.uri
    if host_info.has_tunnel:
        from .tunnel import open_tunnel
        db_uri = open_tunnel(db_uri, host_info.tunnel_config)

    backup_mgr = BackupManager(
        db_uri,
        task_config['storage_url']
    )

    result = backup_mgr.backup_database(
        task_config['database'],
        task_config.get('collections'),
        custom_name=task_config.get('custom_name')
    )

    if result['success']:
        console.print(f"[green]✅ Backup completed![/green]")
        console.print(f"  File: {result['filename']}")
        console.print(f"  Size: {result['size_human']}")
        console.print(f"  Documents: {format_number(result['documents'])}")
        console.print(f"  Collections: {result['collections']}")
        backup_mgr.close()
        return True
    else:
        console.print(f"[red]❌ Backup failed: {result.get('error')}[/red]")
        backup_mgr.close()
        return False


def run_restore_task(task_config: dict[str, Any]) -> bool:
    """Execute a restore task."""
    from .backup import BackupManager
    from .utils import resolve_host

    host_info = resolve_host(task_config['db_uri'], task_config.get('ssh_tunnel'))
    db_uri = host_info.uri
    if host_info.has_tunnel:
        from .tunnel import open_tunnel
        db_uri = open_tunnel(db_uri, host_info.tunnel_config)

    backup_mgr = BackupManager(
        db_uri,
        task_config['storage_url']
    )

    result = backup_mgr.restore_database(
        task_config['backup_file'],
        task_config.get('target_database'),
        task_config.get('drop_target', False)
    )

    if result['success']:
        console.print(f"[green]✅ Restore completed![/green]")
        console.print(f"  Database: {result['database']}")
        console.print(f"  Documents: {format_number(result['documents'])}")
        console.print(f"  Collections: {result['collections']}")
        backup_mgr.close()
        return True
    else:
        console.print(f"[red]❌ Restore failed: {result.get('error')}[/red]")
        backup_mgr.close()
        return False


def display_task_summary(task_config: dict[str, Any]) -> None:
    """Display task configuration summary."""
    task_type = task_config.get('type', 'copy')

    if task_type == 'backup':
        from .utils import resolve_host

        host_info = resolve_host(_get_uri(task_config), task_config.get('ssh_tunnel'))
        console.print(f"[bold]Type:[/bold] BACKUP")
        console.print(f"[bold]Source:[/bold] {host_info.connection_label(task_config['database'])}")
        console.print(f"[bold]Database:[/bold] {task_config['database']}")
        console.print(f"[bold]Collections:[/bold] {task_config.get('collections', 'ALL')}")
        console.print(f"[bold]Destination:[/bold] {task_config['storage_url']}")

    elif task_type == 'restore':
        from .utils import resolve_host

        host_info = resolve_host(_get_uri(task_config), task_config.get('ssh_tunnel'))
        console.print(f"[bold]Type:[/bold] RESTORE")
        console.print(f"[bold]Backup:[/bold] {task_config['backup_file']}")
        console.print(
            f"[bold]Target:[/bold] "
            f"{host_info.connection_label(task_config.get('target_database'))}"
        )
        console.print(f"[bold]Database:[/bold] {task_config.get('target_database', 'from backup')}")
        console.print(f"[bold]Drop Target:[/bold] {'Yes' if task_config.get('drop_target') else 'No'}")
        console.print(f"[bold]Storage:[/bold] {task_config['storage_url']}")

    else:
        from .utils import resolve_host

        console.print(f"[bold]Type:[/bold] COPY")
        source_info = resolve_host(task_config.get('source_uri'), task_config.get('source_ssh_tunnel'))
        target_info = resolve_host(task_config.get('target_uri'), task_config.get('target_ssh_tunnel'))

        console.print(
            f"[bold]Source:[/bold] {source_info.connection_label(task_config.get('source_db'))}"
        )
        console.print(
            f"[bold]Target:[/bold] {target_info.connection_label(task_config.get('target_db'))}"
        )
        console.print(f"[bold]Database:[/bold] {task_config.get('source_db', '?')} → {task_config.get('target_db', '?')}")
        if task_config.get('source_collection'):
            console.print(
                f"[bold]Collection:[/bold] {task_config['source_collection']} → "
                f"{task_config.get('target_collection', task_config['source_collection'])}"
            )
        console.print(f"[bold]Drop Target:[/bold] {'Yes' if task_config.get('drop_target') else 'No'}")
        if task_config.get('schema_only'):
            console.print(f"[bold]Schema Only:[/bold] Yes (no data)")
        if task_config.get('incremental'):
            column = task_config.get('since_column') or 'auto-detected'
            console.print(f"[bold]Incremental:[/bold] Yes (watermark: {column})")

    # Hooks apply to every task type: show them so the confirmation prompt
    # reveals every shell command that is about to run.
    from .hooks import describe_hooks

    for label, command in describe_hooks(task_config):
        console.print(f"[bold]{label}:[/bold] {command}")
