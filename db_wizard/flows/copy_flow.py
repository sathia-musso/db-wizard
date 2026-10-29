from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from ..engine import EngineFactory
from ..formatting import format_number
from ..table_selection import has_patterns, resolve_tables
from ..utils import resolve_host

console = Console()

class CopyWizardFlow:
    """Handles the copy workflow logic decoupled from main wizard"""

    def __init__(self, wizard_context):
        self.wizard = wizard_context

    def run(self):
        """Interactive copy wizard"""
        self.wizard.clear_screen()
        console.print(Panel("[bold cyan]📋 COPY WIZARD[/bold cyan]", style="cyan"))

        # Step 1: Select source
        console.print("\n[bold]STEP 1: SELECT SOURCE[/bold]")
        source_host = self.wizard.select_or_add_host("source")
        source_engine = EngineFactory.create(source_host['resolved_uri'])
        source_engine.connect()

        source_db = self.wizard.select_database(source_engine, "source")

        # Ask if user wants single or multiple collection selection
        term_plural = source_engine.table_term_plural
        if Confirm.ask(f"\n[cyan]Do you want to select multiple {term_plural}?[/cyan]"):
            source_collection = self.wizard.select_collection(source_engine, source_db, "source", allow_all=True,
                                                       allow_multiple=True)
        else:
            source_collection = self.wizard.select_collection(source_engine, source_db, "source", allow_all=True,
                                                       allow_multiple=False)

        # Step 2: Select target (filtered to same engine type)
        console.print("\n[bold]STEP 2: SELECT TARGET[/bold]")
        source_scheme = source_engine.scheme
        target_host = self.wizard.select_or_add_host("target", filter_scheme=source_scheme)
        target_engine = EngineFactory.create(target_host['resolved_uri'])

        target_engine.connect()

        # Ask if want same or different database name
        if Confirm.ask(f"\n[yellow]Use same database name '{source_db}' on target?[/yellow]", default=True):
            target_db = source_db
        else:
            target_db = Prompt.ask("Enter target database name", default=f"{source_db}_copy")

        # The selection may contain patterns ("compliance_*"). Copy and count
        # the tables they match now, but save the task with the patterns
        # themselves (source_collection below) so every later run re-resolves
        # them and picks up tables created in the meantime.
        copy_tables = source_collection
        if (isinstance(source_collection, list) and source_engine.supports_table_patterns
                and has_patterns(source_collection)):
            copy_tables = resolve_tables(
                source_collection, [t['name'] for t in source_engine.list_tables(source_db)]
            )

        target_collection = None
        if isinstance(source_collection, list):
            # Multi-collection: always use same names on target
            target_collection = source_collection
            console.print(f"[dim]Target collections will use the same names as source ({len(copy_tables)} collections)[/dim]")
        elif source_collection:
            if Confirm.ask(f"[yellow]Use same collection name '{source_collection}' on target?[/yellow]", default=True):
                target_collection = source_collection
            else:
                target_collection = Prompt.ask("Enter target collection name", default=f"{source_collection}_copy")

        # Step 3: Options
        console.print("\n[bold]STEP 3: COPY OPTIONS[/bold]")
        is_mongo = source_engine.scheme == 'mongodb'

        drop_target = Confirm.ask("[yellow]Drop target before copying?[/yellow]", default=False)
        create_backup = False
        if drop_target and is_mongo:
            create_backup = Confirm.ask("[cyan]💾 Create backup before dropping?[/cyan]", default=True)
        # Schema-only is only meaningful on relational engines (MySQL/Postgres):
        # schemaless stores (MongoDB/Redis) have no structure to copy on its own.
        supports_schema_only = source_engine.scheme in ('mysql', 'postgres')
        schema_only = False
        if supports_schema_only:
            schema_only = Confirm.ask("[cyan]🏗️  Copy schema only (no data)?[/cyan]", default=False)

        # Incremental copy only makes sense on engines that can filter rows by a
        # monotonic column, and it contradicts both dropping and schema-only.
        supports_incremental = (
            source_engine.supports_incremental and not drop_target and not schema_only
        )
        incremental = False
        since_column = None
        if supports_incremental:
            incremental = Confirm.ask(
                "[cyan]⏩ Incremental copy (only new rows, for append-only data)?[/cyan]",
                default=False
            )
            if incremental:
                console.print("[dim]Note: updates and deletes on existing rows are NOT detected[/dim]")
                answer = Prompt.ask(
                    "Watermark column (blank = auto-detect)",
                    default=""
                ).strip()
                since_column = answer or None
        copy_indexes = True
        if is_mongo:
            copy_indexes = Confirm.ask("[cyan]Copy indexes?[/cyan]", default=True)
        verify = False
        if is_mongo:
            verify = Confirm.ask("[green]Verify after copy?[/green]", default=True)

        # Summary
        self.wizard.clear_screen()
        console.print(Panel("[bold green]📋 COPY SUMMARY[/bold green]", style="green"))

        summary = Table(box=box.SIMPLE)
        summary.add_column("Property", style="cyan")
        summary.add_column("Value", style="yellow")

        summary.add_row(
            "Source",
            resolve_host(source_host['raw_uri'], source_host.get('ssh_tunnel')).connection_label(source_db),
        )
        summary.add_row("Source DB", source_db)
        if isinstance(source_collection, list):
            summary.add_row("Source Collections", f"{len(copy_tables)} selected")
            if copy_tables != source_collection:
                summary.add_row("Patterns", ", ".join(source_collection))
        else:
            summary.add_row("Source Collection", source_collection or "ALL")
        summary.add_row("", "")
        summary.add_row(
            "Target",
            resolve_host(target_host['raw_uri'], target_host.get('ssh_tunnel')).connection_label(target_db),
        )
        summary.add_row("Target DB", target_db)
        if isinstance(source_collection, list):
            summary.add_row("Target Collections", "Same names as source")
        else:
            summary.add_row("Target Collection", target_collection or "ALL")
        summary.add_row("", "")
        summary.add_row("Drop Target", "YES" if drop_target else "NO")
        if drop_target and create_backup:
            summary.add_row("Create Backup", "YES 💾")
        if supports_schema_only:
            summary.add_row("Schema Only", "YES (no data)" if schema_only else "NO")
        if incremental:
            summary.add_row("Incremental", f"YES (watermark: {since_column or 'auto-detected'})")
        summary.add_row("Copy Indexes", "YES" if copy_indexes else "NO")
        summary.add_row("Verify", "YES" if verify else "NO")

        console.print(summary)

        # Get document counts for warning
        source_count = None
        target_count = None
        try:
            if isinstance(source_collection, list):
                source_count = sum(
                    source_engine.count_rows(source_db, c) for c in copy_tables
                )
                target_tables = [t['name'] for t in target_engine.list_tables(target_db)]
                target_count = sum(
                    target_engine.count_rows(target_db, c)
                    for c in copy_tables if c in target_tables
                )
            elif source_collection:
                source_count = source_engine.count_rows(source_db, source_collection)
                target_tables = [t['name'] for t in target_engine.list_tables(target_db)]
                if target_collection in target_tables:
                    target_count = target_engine.count_rows(target_db, target_collection)
            else:
                source_count = sum(t['rows'] for t in source_engine.list_tables(source_db))
                try:
                    target_count = sum(t['rows'] for t in target_engine.list_tables(target_db))
                except Exception:
                    target_count = None
        except Exception:
            pass

        if drop_target:
            console.print("\n[bold red]⚠️  WARNING: This will DELETE existing target data![/bold red]")
            if source_count is not None:
                console.print(f"[cyan]📊 Source has {format_number(source_count)} documents[/cyan]")
            if target_count is not None and target_count > 0:
                console.print(f"[red]🗑️  Target has {format_number(target_count)} documents that will be DELETED![/red]")
        else:
            if source_count is not None:
                console.print(f"\n[cyan]📊 Source: {format_number(source_count)} documents[/cyan]")
            if target_count is not None and target_count > 0:
                console.print(f"[yellow]📊 Target: {format_number(target_count)} existing documents (will merge)[/yellow]")

        if not Confirm.ask("\n[bold yellow]Proceed with copy?[/bold yellow]"):
            console.print("[red]✗ Cancelled[/red]")
            return

        # Execute copy using engine interface
        console.print("\n[cyan]Starting copy operation...[/cyan]")

        try:
            if isinstance(source_collection, list):
                # Multiple collections
                console.print(f"[cyan]📋 Copying {len(copy_tables)} {target_engine.table_term_plural}[/cyan]")

                if create_backup and drop_target and hasattr(target_engine, 'backup_before_copy'):
                    for coll in copy_tables:
                        if coll in [t['name'] for t in target_engine.list_tables(target_db)]:
                            target_engine.backup_before_copy(target_db, coll)

                results = {}
                for coll_name in copy_tables:
                    console.print(f"\n[bold]📁 Copying: {coll_name}[/bold]")
                    result = target_engine.copy(
                        source_engine=source_engine,
                        source_db=source_db, source_table=coll_name,
                        target_db=target_db, target_table=coll_name,
                        drop_target=drop_target, schema_only=schema_only,
                    incremental=incremental, since_column=since_column
                    )
                    results[coll_name] = result

                total_docs = sum(r['documents_copied'] for r in results.values())
                total_indexes = sum(r.get('indexes_created', 0) for r in results.values())
                console.print(f"[green]✓ Copied {len(results)} {target_engine.table_term_plural}[/green]")
                console.print(f"[green]✓ Total: {format_number(total_docs)} rows, {total_indexes} indexes[/green]")

            elif source_collection:
                # Single collection/table
                if create_backup and drop_target and hasattr(target_engine, 'backup_before_copy'):
                    target_tables = [t['name'] for t in target_engine.list_tables(target_db)]
                    if target_collection in target_tables:
                        target_engine.backup_before_copy(target_db, target_collection)

                result = target_engine.copy(
                    source_engine=source_engine,
                    source_db=source_db, source_table=source_collection,
                    target_db=target_db, target_table=target_collection,
                    drop_target=drop_target, schema_only=schema_only,
                    incremental=incremental, since_column=since_column
                )
                console.print(f"[green]✓ Copied {format_number(result['documents_copied'])} rows[/green]")
                console.print(f"[green]✓ Created {result.get('indexes_created', 0)} indexes[/green]")

                if verify and hasattr(source_engine, 'verify_copy'):
                    verification = source_engine.verify_copy(
                        source_db, source_collection,
                        target_db, target_collection,
                        target_engine=target_engine
                    )
                    if verification['count_match']:
                        console.print("[green]✓ Verification passed![/green]")
                    else:
                        console.print(
                            f"[yellow]⚠ Count mismatch: {verification['source_count']} vs {verification['target_count']}[/yellow]")

            else:
                # Entire database
                results = target_engine.copy(
                    source_engine=source_engine,
                    source_db=source_db, source_table=None,
                    target_db=target_db, target_table=None,
                    drop_target=drop_target, schema_only=schema_only,
                    incremental=incremental, since_column=since_column
                )
                if isinstance(results, dict) and all(isinstance(v, dict) for v in results.values()):
                    total_docs = sum(r['documents_copied'] for r in results.values())
                    console.print(f"[green]✓ Copied {len(results)} {target_engine.table_term_plural}, {format_number(total_docs)} total rows[/green]")
                else:
                    console.print(f"[green]✓ Copy completed[/green]")

        except Exception as e:
            console.print(f"[red]✗ Error: {e}[/red]")

        finally:
            source_engine.close()
            target_engine.close()

        console.print("\n[bold green]✅ COPY COMPLETE![/bold green]")

        # Ask if user wants to save this task
        if Confirm.ask("\n[cyan]💾 Would you like to save this task for future use?[/cyan]"):
            task_name = Prompt.ask("Enter a name for this task")

            task_config = {
                'source_uri': source_host['raw_uri'],
                'target_uri': target_host['raw_uri'],
                'source_ssh_tunnel': source_host['ssh_tunnel'],
                'target_ssh_tunnel': target_host['ssh_tunnel'],
                'source_db': source_db,
                'target_db': target_db,
                'source_collection': source_collection,
                'target_collection': target_collection,
                'drop_target': drop_target,
                'incremental': incremental,
                'since_column': since_column,
                'copy_indexes': copy_indexes,
                'verify': verify,
                'schema_only': schema_only
            }

            if self.wizard.settings_manager.add_task(task_name, task_config):
                console.print(f"[green]✅ Task '{task_name}' saved![/green]")
                console.print(f"[dim]Run it anytime with: db-wizard --task {task_name}[/dim]")

        Prompt.ask("\nPress Enter to continue")