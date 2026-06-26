import os
from rich.console import Console
from rich.prompt import Prompt

from ..backup import BackupManager, BackupTask
from ..utils import resolve_host
from ..formatting import format_number
from ._common import _test_connection

console = Console()

class RestoreWizardFlow:
    """Handles the restore workflow logic decoupled from main wizard"""

    def __init__(self, wizard_context):
        self.wizard = wizard_context

    def run(self):
        """Wizard for database restore"""
        self.wizard.clear_screen()
        console.print("\n[bold cyan]📥 DATABASE RESTORE WIZARD[/bold cyan]\n")

        # 1. Select storage source
        console.print("[bold cyan]📍 STORAGE SOURCE[/bold cyan]\n")

        # Check for saved storage configs
        saved_storages = self.wizard.settings_manager.list_storages()
        storage_list = list(saved_storages.items())
        storage_url = None

        if storage_list:
            console.print("[bold]Available storage locations:[/bold]\n")

            for i, (name, config) in enumerate(storage_list, 1):
                storage_type = config.get('type', 'unknown')
                storage_name = config.get('name', name)
                console.print(f"  [cyan]{i}.[/cyan] {storage_name} ({storage_type})")

            console.print(f"  [cyan]{len(storage_list) + 1}.[/cyan] ➕ Add new storage location")

            choices = [str(i) for i in range(1, len(storage_list) + 2)]
            choice = Prompt.ask("\nSelect storage", choices=choices, default="1")

            if choice == str(len(storage_list) + 1):
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

        # 2. List and select backup file
        dummy_uri = "mongodb://localhost:27017"  # Need a URI for BackupManager init
        backup_mgr = BackupManager(dummy_uri, storage_url)

        console.print("\n[dim]Loading backups...[/dim]")
        backups = backup_mgr.list_backups()

        if not backups:
            if isinstance(storage_url, dict):
                location = f"{storage_url.get('name', 'storage')} ({storage_url.get('path', 'N/A')})"
            else:
                location = storage_url
            console.print(f"[yellow]No backups found at: {location}[/yellow]")
            Prompt.ask("Press Enter to continue")
            return

        # Display and select backup
        backup_file = backup_mgr.display_backups(backups)
        if not backup_file:
            return

        # 3. Select target database instance
        self.wizard.clear_screen()
        console.print("\n[bold cyan]SELECT RESTORE TARGET[/bold cyan]\n")
        target_host = self.wizard.select_or_add_host("restore target")
        if not target_host:
            return

        target_uri = target_host['resolved_uri']

        # Test connection
        console.print("\n[dim]Testing connection...[/dim]")
        is_connected, msg = _test_connection(target_uri)
        if not is_connected:
            console.print(f"[red]❌ Connection failed: {msg}[/red]")
            Prompt.ask("Press Enter to continue")
            return

        # 4. Target database name
        backup_filename = os.path.basename(backup_file)
        # Extract database name from filename (format: YYYY_MM_DD_HH_MM-database.tar.gz)
        if '-' in backup_filename and backup_filename.endswith('.tar.gz'):
            default_db = backup_filename.split('-')[1].replace('.tar.gz', '')
        else:
            default_db = "restored_db"

        target_database = Prompt.ask("Target database name", default=default_db)

        # 5. Drop target option
        drop_target = Prompt.ask(
            "[bold yellow]⚠ Drop target database before restore?[/bold yellow]",
            choices=["y", "n"],
            default="n"
        ) == "y"

        # 6. Confirm and restore
        console.print(f"\n[bold]Restore Configuration:[/bold]")
        console.print(f"  Backup: {backup_filename}")
        console.print(
            f"  Target: {resolve_host(target_host['raw_uri'], target_host.get('ssh_tunnel')).connection_label(target_database)}"
        )
        console.print(f"  Database: {target_database}")
        console.print(f"  Drop target: {'Yes' if drop_target else 'No'}")

        if not Prompt.ask("\n[bold yellow]Start restore?[/bold yellow]", choices=["y", "n"], default="y") == "y":
            return

        # Update backup manager with correct target URI
        backup_mgr = BackupManager(target_uri, storage_url)

        # Perform restore
        result = backup_mgr.restore_database(backup_file, target_database, drop_target)

        if result['success']:
            console.print(f"\n[green]✅ Restore completed successfully![/green]")
            console.print(f"[bold]Database:[/bold] {result['database']}")
            console.print(f"[bold]Documents:[/bold] {format_number(result['documents'])}")
            console.print(f"[bold]Collections:[/bold] {result['collections']}")

            # Ask to save as task
            if Prompt.ask("\n[bold]Save as task?[/bold]", choices=["y", "n"], default="n") == "y":
                task_name = Prompt.ask("Task name", default=f"restore_{target_database}")

                task = BackupTask.create_restore_task(
                    name=task_name,
                    db_uri=target_host['raw_uri'],
                    backup_file=backup_file,
                    target_database=target_database,
                    storage_url=storage_url,
                    drop_target=drop_target
                )
                task['ssh_tunnel'] = target_host['ssh_tunnel']

                self.wizard.settings_manager.add_task(task_name, task)
                console.print(f"[green]✓ Task '{task_name}' saved![/green]")
                console.print(f"[dim]Run with: dbw --task {task_name}[/dim]")
        else:
            console.print(f"[red]❌ Restore failed: {result.get('error')}[/red]")

        backup_mgr.close()
        Prompt.ask("\nPress Enter to continue")
