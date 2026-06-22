#!/usr/bin/env python
"""
db-wizard CLI - Main entry point

Usage:
    # Interactive mode
    dbw

    # Task execution
    dbw -t daily_backup
    dbw -t daily_backup -y  # Automated mode

    # Direct copy (auto-detects engine from URI)
    dbw -s mongodb://source -t mongodb://target --source-db mydb
    dbw -s mysql://user:pass@remote/db -t mysql://localhost/db

    # As module
    python -m db_wizard
"""

import os
import sys
import click
from . import __version__
from .settings import SettingsManager
from .engine import EngineFactory
from .task_runner import run_task, display_task_summary
from .backup import BackupManager
from .formatting import format_number
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()


@click.command()
@click.version_option(version=__version__)
@click.option('-s', '--source', help='Source database URI (mongodb://, mysql://, postgres://, redis://)')
@click.option('-t', '--target', help='Target database URI (mongodb://, mysql://, postgres://, redis://)')
@click.option('--source-db', help='Source database name')
@click.option('--target-db', help='Target database name (defaults to source-db)')
@click.option('--source-collection', help='Source collection/table (omit for all)')
@click.option('--drop-target', is_flag=True, help='Drop target before copying')
@click.option('--schema-only', is_flag=True, help='Copy only structure/DDL, no data (MySQL/Postgres only; schemaless engines are unsupported)')
@click.option('--dry-run', is_flag=True, help='Show what would be done without executing')
@click.option('--verify', is_flag=True, help='Verify copy after completion')
@click.option('--list-tasks', is_flag=True, help='List saved tasks')
@click.option('--list-hosts', is_flag=True, help='List saved hosts')
@click.option('--task', help='Run a saved task')
@click.option('-f', '--force', is_flag=True, help='Skip confirmation when running task (deprecated, use -y)')
@click.option('-y', '--yes', 'assume_yes', is_flag=True, help='Assume yes to all prompts (fully automated)')
@click.option('--force-python', is_flag=True, help='Force Python copy instead of mongodump (slower but more control)')
@click.option('--verify-connection', help='Test connection to MongoDB URI')
@click.option('--backup', help='Backup a database (format: mongodb://uri/database)')
@click.option('--backup-to', help='Backup destination (local path or ssh://user@host/path)')
@click.option('--restore', help='Restore from backup file')
@click.option('--restore-to', help='Restore target MongoDB URI')
@click.option('-c', '--count', is_flag=True, help='Count documents when listing tasks/hosts (slow on remote DBs)')
def main(source, target, source_db, target_db, source_collection, drop_target,
         schema_only, dry_run, verify, list_tasks, list_hosts, task, force, assume_yes, force_python, verify_connection,
         backup, backup_to, restore, restore_to, count):
    """
    db-wizard - Advanced database copy and migration tool

    Supports MongoDB and MySQL. Auto-detects engine from URI scheme.

    Examples:

    Interactive mode:
        dbw

    MongoDB copy:
        dbw -s mongodb://localhost -t mongodb://remote --source-db mydb

    MySQL copy:
        dbw -s mysql://user:pass@remote/db -t mysql://localhost/db

    Run saved task:
        dbw --task daily_backup -y

    List tasks/hosts:
        dbw --list-tasks
        dbw --list-hosts
    """

    # Connection verification mode
    if verify_connection:
        try:
            engine = EngineFactory.create(verify_connection)
            success, message = engine.test_connection()
            if success:
                console.print(f"[green]✅ {message}[/green]")
            else:
                console.print(f"[red]❌ {message}[/red]")
        except ValueError as e:
            console.print(f"[red]❌ {e}[/red]")
        return

    # List tasks mode
    if list_tasks:
        from .utils import format_task_table_row

        settings_manager = SettingsManager()
        tasks = settings_manager.list_tasks()

        if not tasks:
            console.print("[yellow]No saved tasks found[/yellow]")
            console.print("[dim]Create tasks using interactive mode[/dim]")
            return

        console.print()
        console.print(Panel("[bold blue]⚙️  SAVED TASKS[/bold blue]", expand=False))
        console.print()

        table = Table(title="Saved Tasks", box=box.ROUNDED)
        table.add_column("#", style="dim", width=4)
        table.add_column("Name", style="cyan")
        table.add_column("Source → Target", style="green")
        table.add_column("Collection", style="magenta")

        # Format each task (use --count to show document counts)
        for idx, (name, task) in enumerate(tasks.items(), 1):
            task_name, source_target, coll_display = format_task_table_row(name, task, count=count)
            table.add_row(
                str(idx),
                task_name,
                source_target,
                coll_display
            )

        console.print(table)
        console.print()
        console.print(f"[dim]Run a task with: dbw --task <name>[/dim]")
        console.print(f"[dim]Automated mode: dbw --task <name> -y[/dim]")
        if not count:
            console.print(f"[dim]Show doc counts: dbw --list-tasks -c[/dim]")
        return

    # List hosts mode
    if list_hosts:
        settings_manager = SettingsManager()
        hosts = settings_manager.list_hosts()

        if not hosts:
            console.print("[yellow]No saved hosts found[/yellow]")
            console.print("[dim]Add hosts using interactive mode (dbw)[/dim]")
            return

        table = Table(title="💾 Saved Hosts", box=box.ROUNDED)
        table.add_column("Name", style="cyan")
        table.add_column("URI", style="green")
        table.add_column("Status", style="yellow")

        for name, host_value in hosts.items():
            # Mask password in URI for display
            if isinstance(host_value, dict):
                uri = host_value.get('uri', '')
            else:
                uri = host_value
            
            display_uri = uri
            if isinstance(uri, str) and '@' in uri and ':' in uri.split('@')[0]:
                parts = uri.split('@')
                user_pass = parts[0].split('://')[-1]
                user = user_pass.split(':')[0]
                display_uri = uri.replace(user_pass, f"{user}:****")

            # Connection test only with --count flag (slow on remote)
            if count:
                try:
                    engine = EngineFactory.create(uri)
                    is_online, msg = engine.test_connection(timeout=1000)
                except ValueError:
                    is_online = False
                status = "🟢 Online" if is_online else "🔴 Offline"
            else:
                status = "[dim]-[/dim]"

            table.add_row(name, display_uri, status)

        console.print(table)
        console.print(f"\n[dim]Total: {len(hosts)} hosts[/dim]")
        if not count:
            console.print(f"[dim]Check connectivity: dbw --list-hosts -c[/dim]")
        return

    # Run task mode
    if task:
        settings_manager = SettingsManager()
        task_config = settings_manager.get_task(task)

        if not task_config:
            console.print(f"[red]❌ Task '{task}' not found![/red]")
            console.print("[dim]Use --list-tasks to see available tasks[/dim]")
            sys.exit(1)

        console.print(f"[cyan]🚀 Running task: {task}[/cyan]\n")

        # Show task summary
        display_task_summary(task_config)

        if not force and not assume_yes:
            if not click.confirm("\nExecute this task?"):
                console.print("[red]Cancelled[/red]")
                sys.exit(0)

        # Execute task using centralized runner.
        # schema_only: run the saved job but copy structure only, no data.
        success = run_task(task_config, assume_yes, force_python, schema_only)

        if success:
            console.print("[green]✅ Task completed successfully![/green]")
        else:
            sys.exit(1)

        return

    # Backup mode
    if backup:
        if not backup_to:
            console.print("[red]❌ --backup-to is required for backup[/red]")
            sys.exit(1)

        # Parse backup URI (format: mongodb://uri/database)
        if '/' not in backup.replace('mongodb://', ''):
            console.print("[red]❌ Backup URI must include database (mongodb://host/database)[/red]")
            sys.exit(1)

        # Extract database from URI
        parts = backup.rsplit('/', 1)
        db_uri = parts[0]
        database = parts[1]

        console.print(f"[cyan]📦 Backup mode[/cyan]")
        console.print(f"Source: {db_uri}")
        console.print(f"Database: {database}")
        console.print(f"Destination: {backup_to}")

        if not assume_yes:
            if not click.confirm("\nProceed with backup?"):
                console.print("[red]Cancelled[/red]")
                sys.exit(0)

        backup_mgr = BackupManager(db_uri, backup_to)
        result = backup_mgr.backup_database(database)

        if result['success']:
            console.print(f"[green]✅ Backup completed![/green]")
            console.print(f"File: {result['filename']}")
            console.print(f"Size: {result['size_human']}")
            console.print(f"Documents: {format_number(result['documents'])}")
        else:
            console.print(f"[red]❌ Backup failed: {result.get('error')}[/red]")
            sys.exit(1)

        backup_mgr.close()
        return

    # Restore mode
    if restore:
        if not restore_to:
            console.print("[red]❌ --restore-to is required for restore[/red]")
            sys.exit(1)

        console.print(f"[cyan]📥 Restore mode[/cyan]")
        console.print(f"Backup: {restore}")
        console.print(f"Target: {restore_to}")

        if not assume_yes:
            if not click.confirm("\nProceed with restore?"):
                console.print("[red]Cancelled[/red]")
                sys.exit(0)

        # Determine storage location from backup path
        storage_url = os.path.dirname(restore)
        backup_mgr = BackupManager(restore_to, storage_url)
        result = backup_mgr.restore_database(restore, drop_target=drop_target)

        if result['success']:
            console.print(f"[green]✅ Restore completed![/green]")
            console.print(f"Database: {result['database']}")
            console.print(f"Documents: {format_number(result['documents'])}")
        else:
            console.print(f"[red]❌ Restore failed: {result.get('error')}[/red]")
            sys.exit(1)

        backup_mgr.close()
        return

    # Direct copy mode
    if source and target:
        if not source_db:
            console.print("[red]❌ --source-db is required for direct copy[/red]")
            sys.exit(1)

        target_db = target_db or source_db

        # Schema-only is meaningless on schemaless engines. Refuse up-front with
        # a clear alert instead of connecting and silently copying everything.
        if schema_only:
            scheme = EngineFactory.detect_scheme(source)
            if scheme in ('mongodb', 'redis'):
                engine_name = 'MongoDB' if scheme == 'mongodb' else 'Redis'
                console.print(f"[yellow]⚠ {engine_name} is schemaless — --schema-only is not supported.[/yellow]")
                sys.exit(1)

        console.print(f"[cyan]📋 Direct copy mode[/cyan]")
        console.print(f"Source: {source}/{source_db}/{source_collection or 'ALL'}")
        console.print(f"Target: {target}/{target_db}/{source_collection or 'ALL'}")

        if schema_only:
            console.print("[cyan]🏗️  Schema-only: structure/DDL, no data[/cyan]")
        if drop_target:
            console.print("[yellow]⚠ Will drop target before copying[/yellow]")

        if not assume_yes:
            if not click.confirm("\nProceed with copy?"):
                console.print("[red]Cancelled[/red]")
                sys.exit(0)

        # Auto-detect engines from URI schemes
        source_engine = EngineFactory.create(source)
        target_engine = EngineFactory.create(target)

        # Cross-engine copy not supported
        EngineFactory.check_same_engine(source_engine, target_engine)

        try:
            source_engine.connect()
            target_engine.connect()

            result = target_engine.copy(
                source_engine=source_engine,
                source_db=source_db,
                source_table=source_collection,
                target_db=target_db,
                target_table=source_collection,
                drop_target=drop_target,
                force=assume_yes,
                force_python=force_python,
                schema_only=schema_only,
            )

            # Handle both single-result and multi-result returns
            if isinstance(result, dict) and all(isinstance(v, dict) for v in result.values()):
                # Multi-table result
                total_docs = sum(r['documents_copied'] for r in result.values())
                console.print(f"[green]✅ Copied {len(result)} {target_engine.table_term_plural}, {format_number(total_docs)} rows[/green]")
            else:
                method = result.get('method', 'unknown')
                console.print(f"[green]✅ Copied {format_number(result['documents_copied'])} rows (method: {method})[/green]")

            # Verification (MongoDB-specific)
            if verify and source_collection and hasattr(source_engine, 'verify_copy'):
                verify_result = source_engine.verify_copy(
                    source_db, source_collection,
                    target_db, source_collection,
                    target_engine=target_engine
                )
                if verify_result['count_match'] and verify_result['index_match']:
                    console.print("[green]✅ Verification passed![/green]")
                else:
                    console.print("[yellow]⚠ Verification issues found[/yellow]")
                    console.print(f"  Count match: {verify_result['count_match']}")
                    console.print(f"  Index match: {verify_result['index_match']}")

        except Exception as e:
            console.print(f"[red]❌ Error: {e}[/red]")
            sys.exit(1)
        finally:
            source_engine.close()
            target_engine.close()

        return

    # Default: interactive mode
    from .wizard import DbWizard
    wizard = DbWizard()
    wizard.run()


if __name__ == '__main__':
    main()
