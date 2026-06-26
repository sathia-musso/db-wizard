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
             schema_only: bool = False) -> bool:
    """
    Execute a task based on its type.
    Auto-detects engine from URIs in the task config.

    schema_only: run a copy task copying only structure/DDL, no data.
                 Ignored for backup/restore tasks (no schema concept there).
    """
    task_type = task_config.get('type', 'copy')

    try:
        if task_type == 'backup':
            return run_backup_task(task_config)
        elif task_type == 'restore':
            return run_restore_task(task_config)
        else:
            return run_copy_task(task_config, assume_yes, force_python, schema_only)
    except Exception as e:
        console.print(f"[red]❌ Task error: {e}[/red]")
        return False


def run_copy_task(task_config: dict[str, Any], assume_yes: bool = False, force_python: bool = False,
                  schema_only: bool = False) -> bool:
    """Execute a copy task using the appropriate engine."""
    from .utils import resolve_host

    # schema_only can come from the saved task itself or be forced via the CLI
    # flag for this single run. Either one enables it.
    schema_only = schema_only or task_config.get('schema_only', False)

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

        if isinstance(source_table, list):
            # Multiple tables -- iterate one by one
            target_tables = target_table if isinstance(target_table, list) else source_table
            total_docs = 0
            for src_t, tgt_t in zip(source_table, target_tables):
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
                )
                total_docs += result.get('documents_copied', 0)
            console.print(f"[green]✅ Copied {len(source_table)} {target_engine.table_term_plural}, {format_number(total_docs)} rows[/green]")
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
            )
            method = result.get('method', 'unknown')
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
            )
            # Results can be a dict of dicts (one per table) or a single dict
            if isinstance(results, dict) and all(isinstance(v, dict) for v in results.values()):
                total_docs = sum(r['documents_copied'] for r in results.values())
                console.print(f"[green]✅ Copied {len(results)} {target_engine.table_term_plural}, {format_number(total_docs)} rows[/green]")
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
