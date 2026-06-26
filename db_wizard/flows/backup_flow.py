from rich.console import Console
from rich.prompt import Prompt, Confirm
from urllib.parse import urlparse

from ..engine import EngineFactory
from ..backup import BackupManager, BackupTask
from ..utils import resolve_host
from ..formatting import format_number

console = Console()

class BackupWizardFlow:
    """Handles the backup workflow logic decoupled from main wizard"""

    def __init__(self, wizard_context):
        self.wizard = wizard_context

    def run(self):
        """Wizard for database backup"""
        self.wizard.clear_screen()
        console.print("\n[bold cyan]🗄️  DATABASE BACKUP WIZARD[/bold cyan]\n")

        # 1. Select source
        source_host = self.wizard.select_or_add_host("backup source")
        if not source_host:
            return

        source_uri = source_host['resolved_uri']

        # Connect
        try:
            engine = EngineFactory.create(source_uri)
            engine.connect()
        except Exception as e:
            console.print(f"[red]❌ Connection failed: {e}[/red]")
            Prompt.ask("Press Enter to continue")
            return

        # 2. Select database
        database = self.wizard.select_database(engine, "backup")
        if not database:
            engine.close()
            return

        # 3. Select collections or all
        console.print(f"\n[bold]Backup scope for {database}:[/bold]")
        console.print("  [cyan]1.[/cyan] 📁 Entire database")
        console.print("  [cyan]2.[/cyan] 📄 Specific collections")

        scope_choice = Prompt.ask("Choose", choices=["1", "2"])

        collections = None
        if scope_choice == "2":
            selected = self.wizard.select_collection(engine, database, "backup", allow_multiple=True)
            if selected and selected != "ALL":
                collections = selected if isinstance(selected, list) else [selected]

        engine.close()

        # 4. Select storage destination
        self.wizard.clear_screen()
        console.print("\n[bold cyan]📍 STORAGE DESTINATION[/bold cyan]\n")

        # Show saved storage configs first
        saved_storages = self.wizard.settings_manager.list_storages()
        storage_list = list(saved_storages.items())

        if storage_list:
            console.print("[bold]Saved storage configs:[/bold]")
            for i, (name, config) in enumerate(storage_list, 1):
                storage_type = config.get('type', 'unknown')
                if storage_type == 'ssh':
                    details = f"{config.get('user')}@{config.get('host')}:{config.get('path', '/')}"
                elif storage_type == 'ftp':
                    details = f"{config.get('user')}@{config.get('host')}:{config.get('path', '/')}"
                else:
                    details = config.get('path', '/')
                console.print(f"  [cyan]{i}.[/cyan] 📁 {name} ({storage_type.upper()}: {details})")

            console.print(f"\n  [cyan]{len(storage_list) + 1}.[/cyan] ➕ Add new storage")

            max_choice = str(len(storage_list) + 1)
            choices = [str(i) for i in range(1, len(storage_list) + 2)]
            choice = Prompt.ask("\nChoose storage", choices=choices)

            if choice == max_choice:
                # Add new storage
                storage_config = self.wizard._prompt_new_storage()
                storage_url = storage_config
            else:
                # Use saved storage
                idx = int(choice) - 1
                _, storage_config = storage_list[idx]
                storage_url = storage_config
        else:
            # No saved configs, prompt for new one
            console.print("[yellow]No saved storage configs. Let's create one:[/yellow]\n")
            storage_config = self.wizard._prompt_new_storage()
            storage_url = storage_config

        # 5. Ask for backup name (optional - for overwriting same file)
        console.print(f"\n[cyan]📝 BACKUP FILENAME[/cyan]\n")
        console.print("[dim]By default, backups include a timestamp (e.g., 2025_09_30_16_45-mydb.tar.gz)[/dim]")
        console.print("[dim]You can specify a custom name to always overwrite the same file[/dim]\n")

        use_custom_name = Confirm.ask("Use custom filename (no timestamp)?", default=False)
        custom_name = None
        if use_custom_name:
            custom_name = Prompt.ask("Enter filename", default=f"{database}.tar.gz")
            if not custom_name.endswith('.tar.gz') and not custom_name.endswith('.dump') and not custom_name.endswith('.sql'):
                # Basic guess for extension based on engine could be done here, 
                # but BackupManager handles tar.gz wrapping generally.
                custom_name += '.tar.gz'

        # 6. Show configuration and confirm
        console.print(f"\n[bold]Backup Configuration:[/bold]")
        console.print(
            f"  Source: {resolve_host(source_host['raw_uri'], source_host.get('ssh_tunnel')).connection_label(database)}"
        )
        console.print(f"  Database: {database}")
        console.print(f"  Collections: {collections if collections else 'ALL'}")
        if isinstance(storage_url, dict):
            storage_desc = f"{storage_url['name']} ({storage_url['type']})"
        else:
            storage_desc = storage_url
        console.print(f"  Destination: {storage_desc}")
        if custom_name:
            console.print(f"  Filename: {custom_name} [yellow](will overwrite existing)[/yellow]")
        else:
            console.print(f"  Filename: <timestamp>-{database}.tar.gz")

        if not Prompt.ask("\n[bold yellow]Start backup?[/bold yellow]", choices=["y", "n"], default="y") == "y":
            return

        # Test storage connection first if remote
        from ..storage import StorageFactory

        # Determine if storage_url is a config dict or URL string
        if isinstance(storage_url, dict):
            # Using saved config
            storage = StorageFactory.create(storage_url)
            storage_type = storage_url.get('type', 'local')

            if storage_type != 'local':
                console.print("\n[dim]Testing storage connection and write permissions...[/dim]")
                if hasattr(storage, 'test_connection'):
                    test_path = storage_url.get('path', '/')
                    success, error_msg = storage.test_connection(test_path)
                    if not success:
                        console.print(f"[red]❌ Storage connection failed: {error_msg}[/red]")
                        Prompt.ask("Press Enter to continue")
                        return
                    console.print("[green]✓ Storage connection and permissions OK[/green]")
        elif '://' in storage_url:
            # Using URL string
            storage = StorageFactory.create(storage_url)

            console.print("\n[dim]Testing storage connection and write permissions...[/dim]")
            if hasattr(storage, 'test_connection'):
                parsed = urlparse(storage_url)
                test_path = parsed.path or '/'
                success, error_msg = storage.test_connection(test_path)
                if not success:
                    console.print(f"[red]❌ Storage connection failed: {error_msg}[/red]")
                    Prompt.ask("Press Enter to continue")
                    return
                console.print("[green]✓ Storage connection OK[/green]")

        # Pass storage config directly to BackupManager (it handles both dict and URL)
        backup_mgr = BackupManager(source_uri, storage_url)

        # Perform backup
        result = backup_mgr.backup_database(database, collections, custom_name=custom_name)

        if result['success']:
            console.print(f"\n[green]✅ Backup completed successfully![/green]")
            console.print(f"[bold]File:[/bold] {result['filename']}")
            console.print(f"[bold]Size:[/bold] {result['size_human']}")
            console.print(f"[bold]Documents:[/bold] {format_number(result['documents'])}")
            console.print(f"[bold]Collections:[/bold] {result['collections']}")

            # Ask to save as task
            if Prompt.ask("\n[bold]Save as task?[/bold]", choices=["y", "n"], default="y") == "y":
                task_name = Prompt.ask("Task name", default=f"backup_{database}")

                task = BackupTask.create_backup_task(
                    name=task_name,
                    db_uri=source_host['raw_uri'],
                    database=database,
                    collections=collections,
                    storage_url=storage_url,
                    custom_name=custom_name
                )
                task['ssh_tunnel'] = source_host['ssh_tunnel']

                self.wizard.settings_manager.add_task(task_name, task)
                console.print(f"[green]✓ Task '{task_name}' saved![/green]")
                console.print(f"[dim]Run with: dbw --task {task_name}[/dim]")
        else:
            console.print(f"[red]❌ Backup failed: {result.get('error')}[/red]")

        backup_mgr.close()
        Prompt.ask("\nPress Enter to continue")