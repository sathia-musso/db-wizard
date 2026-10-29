import json
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Confirm, Prompt
from rich import box

console = Console()

class ManageTasksFlow:
    """Flow for managing saved execution tasks"""
    def __init__(self, wizard_context):
        self.wizard = wizard_context

    def run(self):
        while True:
            self.wizard.clear_screen()
            console.print(Panel("[bold cyan]⚙ MANAGE SAVED TASKS[/bold cyan]", style="cyan", expand=False))

            saved_tasks_dict = self.wizard.settings_manager.list_tasks()
            saved_tasks_list = list(saved_tasks_dict.items())

            if not saved_tasks_list:
                console.print("[yellow]No saved tasks yet![/yellow]")
            else:
                from ..utils import format_task_table_row

                table = Table(title="Saved Tasks", box=box.ROUNDED)
                table.add_column("#", style="cyan", width=4)
                table.add_column("Name", style="green")
                table.add_column("Source → Target", style="yellow")
                table.add_column("Collection", style="magenta")

                for i, (task_name, task_config) in enumerate(saved_tasks_list, 1):
                    _, source_target, coll_display = format_task_table_row(task_name, task_config)
                    table.add_row(
                        str(i),
                        task_name,
                        source_target,
                        coll_display
                    )

                console.print(table)

            console.print("\n[bold]Options:[/bold]")
            if saved_tasks_list:
                console.print("  [cyan]1.[/cyan] Run task")
                console.print("  [cyan]2.[/cyan] Delete task")
                console.print("  [cyan]3.[/cyan] Export tasks to file")
                console.print("  [cyan]5.[/cyan] Toggle incremental copy")
                console.print("  [cyan]6.[/cyan] Edit before/after commands")
            console.print("  [cyan]4.[/cyan] Back to main menu")

            choices = ["1", "2", "3", "4", "5", "6"] if saved_tasks_list else ["4"]
            choice = Prompt.ask("Choose option", choices=choices)

            if choice == "1" and saved_tasks_list:
                self.wizard.run_saved_task()
            elif choice == "2" and saved_tasks_list:
                while True:
                    try:
                        idx = int(Prompt.ask("Select task to delete"))
                        if 1 <= idx <= len(saved_tasks_list):
                            break
                        console.print(f"[red]Please enter a number between 1 and {len(saved_tasks_list)}[/red]")
                    except ValueError:
                        console.print("[red]Please enter a valid number[/red]")

                task_name, _ = saved_tasks_list[idx - 1]
                if Confirm.ask(f"Delete task '{task_name}'?"):
                    if self.wizard.settings_manager.delete_task(task_name):
                        console.print(f"[green]✓ Deleted '{task_name}'[/green]")
                Prompt.ask("Press Enter to continue")
            elif choice == "3" and saved_tasks_list:
                filename = Prompt.ask("Export filename", default="mongo_tasks.json")
                with open(filename, 'w') as f:
                    json.dump(saved_tasks_dict, f, indent=2)
                console.print(f"[green]✓ Exported {len(saved_tasks_list)} tasks to {filename}[/green]")
                Prompt.ask("Press Enter to continue")
            elif choice == "5" and saved_tasks_list:
                self._toggle_incremental(saved_tasks_list)
            elif choice == "6" and saved_tasks_list:
                self._edit_hooks(saved_tasks_list)
            elif choice == "4":
                break

    def _edit_hooks(self, saved_tasks_list):
        """
        Set or clear a task's before_command / after_command.

        One shell command line per hook here: chain several with `&&` (stop
        at the first failure) or `;`. A list of commands is also accepted,
        but only by editing the settings file; this editor shows it joined
        with ' && ', which runs the same way (stop at the first failure).
        Rules and caveats (no shell aliases!) are in hooks.py.
        """
        from ..hooks import AFTER, BEFORE, hook_commands

        while True:
            try:
                idx = int(Prompt.ask("Select task"))
                if 1 <= idx <= len(saved_tasks_list):
                    break
                console.print(f"[red]Please enter a number between 1 and {len(saved_tasks_list)}[/red]")
            except ValueError:
                console.print("[red]Please enter a valid number[/red]")

        task_name, task_config = saved_tasks_list[idx - 1]

        console.print(f"\n[bold]Hooks for '{task_name}'[/bold]")
        console.print("[dim]Shell commands run with /bin/sh: ~/.zshrc aliases are NOT available.[/dim]")
        console.print("[dim]Before fails -> task not run. Task fails -> after skipped.[/dim]")
        console.print("[dim]Enter keeps the current value, '-' removes it.[/dim]")

        changed = False
        for key, label in ((BEFORE, 'Before'), (AFTER, 'After')):
            try:
                current = ' && '.join(hook_commands(task_config, key))
            except ValueError:
                # A broken value is shown raw so it can be replaced.
                current = str(task_config.get(key))
            answer = Prompt.ask(f"{label} command", default=current or "", show_default=bool(current)).strip()
            if answer == '-':
                if key in task_config:
                    task_config.pop(key)
                    changed = True
            elif answer and answer != current:
                task_config[key] = answer
                changed = True

        if changed:
            self.wizard.settings_manager.add_task(task_name, task_config)
            console.print(f"[green]✓ Hooks saved for '{task_name}'[/green]")
        else:
            console.print("[dim]No changes[/dim]")
        Prompt.ask("Press Enter to continue")

    def _toggle_incremental(self, saved_tasks_list):
        """
        Turn incremental copy on or off for a saved task.

        A task marked incremental copies only the rows above the target's
        watermark on every run, instead of re-copying (and possibly dropping)
        everything. It never detects updates or deletes.
        """
        from ..engine import EngineFactory

        while True:
            try:
                idx = int(Prompt.ask("Select task"))
                if 1 <= idx <= len(saved_tasks_list):
                    break
                console.print(f"[red]Please enter a number between 1 and {len(saved_tasks_list)}[/red]")
            except ValueError:
                console.print("[red]Please enter a valid number[/red]")

        task_name, task_config = saved_tasks_list[idx - 1]

        if task_config.get('type', 'copy') != 'copy':
            console.print("[yellow]⚠ Incremental copy applies to copy tasks only.[/yellow]")
            Prompt.ask("Press Enter to continue")
            return

        # Only engines that can filter rows by a monotonic column qualify.
        try:
            scheme = EngineFactory.detect_scheme(task_config['source_uri'])
        except (KeyError, ValueError):
            scheme = None
        if scheme not in ('mysql', 'mongodb'):
            console.print(
                f"[yellow]⚠ Incremental copy is not supported for {scheme or 'this engine'}. "
                f"Supported: mysql, mongodb.[/yellow]"
            )
            Prompt.ask("Press Enter to continue")
            return

        # Turning it off is unconditional; turning it on has consequences.
        if task_config.get('incremental'):
            task_config['incremental'] = False
            task_config.pop('since_column', None)
            # Give back the drop_target we had to switch off when enabling,
            # so toggling twice leaves the task exactly as it was found.
            restored = task_config.pop('drop_target_before_incremental', None)
            if restored is not None:
                task_config['drop_target'] = restored
            self.wizard.settings_manager.add_task(task_name, task_config)
            console.print(
                f"[green]✓ '{task_name}' is back to full copy"
                f"{' (drop target restored)' if restored else ''}[/green]"
            )
            Prompt.ask("Press Enter to continue")
            return

        console.print("\n[bold]Incremental copy[/bold]")
        console.print("[dim]Copies only rows above the target's watermark on every run.[/dim]")
        console.print("[dim]Updates and deletes on already-copied rows are NOT detected.[/dim]")

        # Dropping the target erases the watermark, so the two cannot coexist.
        if task_config.get('drop_target'):
            console.print(
                "\n[yellow]⚠ This task currently drops the target, which erases the "
                "watermark. Enabling incremental will turn dropping OFF.[/yellow]"
            )
            if not Confirm.ask("Continue?", default=True):
                return

        if task_config.get('schema_only'):
            console.print("[yellow]⚠ This task is schema-only: there are no rows to copy.[/yellow]")
            Prompt.ask("Press Enter to continue")
            return

        column = Prompt.ask("Watermark column (blank = auto-detect)", default="").strip()

        task_config['incremental'] = True
        # Remember the drop setting so disabling incremental can restore it.
        if task_config.get('drop_target'):
            task_config['drop_target_before_incremental'] = True
        task_config['drop_target'] = False
        if column:
            task_config['since_column'] = column
        else:
            task_config.pop('since_column', None)

        self.wizard.settings_manager.add_task(task_name, task_config)
        console.print(
            f"[green]✓ '{task_name}' now runs incrementally "
            f"(watermark: {column or 'auto-detected'})[/green]"
        )
        Prompt.ask("Press Enter to continue")
