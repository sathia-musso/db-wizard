import re
from typing import Any
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt, IntPrompt

from ..formatting import format_number
from ..table_selection import has_patterns, is_pattern, resolve_tables
from ._common import _ask, _test_connection

console = Console()

class SelectionFlow:
    """Handles selection prompts (hosts, databases, collections)"""

    def __init__(self, wizard_context):
        self.wizard = wizard_context

    def select_or_add_host(self, purpose: str = "source", filter_scheme: str | None = None) -> dict[str, Any]:
        """Select saved host or add new one. Returns a dict with resolved_uri, raw_uri, and ssh_tunnel."""
        self.wizard.clear_screen()
        console.print(f"\n[bold cyan]Select {purpose.upper()} host:[/bold cyan]")
        console.print("[dim]Type 'x' at any prompt to return to main menu[/dim]\n")

        saved_hosts_dict = self.wizard.settings_manager.list_hosts()

        if filter_scheme:
            saved_hosts_list = [
                (name, val) for name, val in saved_hosts_dict.items()
                if filter_scheme in self.wizard._get_host_uri(val)
            ]
        else:
            saved_hosts_list = list(saved_hosts_dict.items())

        if saved_hosts_list:
            for i, (host_name, host_value) in enumerate(saved_hosts_list, 1):
                display = self.wizard._host_display(host_name, host_value)
                console.print(f"  [green]{i}.[/green] {display}")

        console.print(f"\n  [yellow]{len(saved_hosts_list) + 1}.[/yellow] ➕ Add new host")
        console.print(f"  [yellow]{len(saved_hosts_list) + 2}.[/yellow] ✏️  Enter URI manually")

        choice_str = _ask("\nChoose option")
        choice = int(choice_str)

        if choice <= len(saved_hosts_list) and choice >= 1:
            host_name, host_value = saved_hosts_list[choice - 1]
            resolved_uri = self.wizard._resolve_host(host_value)
            raw_uri = self.wizard._get_host_uri(host_value)
            tunnel_config = host_value.get('ssh_tunnel') if isinstance(host_value, dict) else None
            
            console.print(f"[yellow]Testing connection to {host_name}...[/yellow]")
            is_online, status = _test_connection(resolved_uri)
            if is_online:
                console.print(f"[green]✅ Connected: {status}[/green]")
                return {'resolved_uri': resolved_uri, 'raw_uri': raw_uri, 'ssh_tunnel': tunnel_config}
            else:
                console.print(f"[red]❌ Cannot connect: {status}[/red]")
                if Confirm.ask("Use anyway?"):
                    return {'resolved_uri': resolved_uri, 'raw_uri': raw_uri, 'ssh_tunnel': tunnel_config}
                return self.select_or_add_host(purpose, filter_scheme)

        elif choice == len(saved_hosts_list) + 1:
            return self.add_new_host()
        else:
            uri = _ask(f"Enter {purpose} URI (e.g. mongodb://..., mysql://...)")
            return {'resolved_uri': uri, 'raw_uri': uri, 'ssh_tunnel': None}

    def add_new_host(self) -> dict[str, Any]:
        """Interactive flow to add a new host to settings"""
        self.wizard.clear_screen()
        console.print(Panel("[bold cyan]➕ ADD NEW HOST[/bold cyan]", style="cyan"))

        # Step 1: Engine Type
        console.print("\n[bold]Select Database Engine:[/bold]")
        console.print("  [cyan]1.[/cyan] MongoDB")
        console.print("  [cyan]2.[/cyan] MySQL")
        console.print("  [cyan]3.[/cyan] PostgreSQL")
        console.print("  [cyan]4.[/cyan] Redis")

        engine_choice = _ask("\nChoose engine", choices=["1", "2", "3", "4"])
        scheme = {
            "1": "mongodb",
            "2": "mysql",
            "3": "postgres",
            "4": "redis"
        }.get(engine_choice, "mongodb")

        # Step 2: Connection details
        name = _ask("\nEnter a name for this host (e.g., 'Production DB')")

        if scheme == "redis":
            uri = _ask("Enter Redis URI (e.g. redis://user:pass@host:port/db)")
        else:
            host = _ask("Hostname", default="localhost")
            port_default = {"mongodb": 27017, "mysql": 3306, "postgres": 5432}.get(scheme, 27017)
            port_str = _ask("Port", default=str(port_default))

            user = _ask("Username (Enter to skip)", default="")
            password = ""
            if user:
                password = Prompt.ask("Password", password=True)

            # Build URI
            auth = f"{user}:{password}@" if user else ""
            uri = f"{scheme}://{auth}{host}:{port_str}"

            if scheme in ["mysql", "postgres"]:
                db = _ask("Default database (Enter to skip)", default="")
                if db:
                    uri = f"{uri}/{db}"

        # Step 3: SSH Tunneling
        ssh_tunnel = None
        if Confirm.ask("\nDoes this database require an SSH tunnel?", default=False):
            ssh_host = _ask("SSH Host (e.g. user@bastion.com or config alias)")

            if '@' in ssh_host or Confirm.ask("Need to specify SSH user/port/key?", default=False):
                if '@' in ssh_host:
                    ssh_user, ssh_host_name = ssh_host.split('@', 1)
                else:
                    ssh_host_name = ssh_host
                    ssh_user = _ask("SSH user", default="root")
                ssh_port = IntPrompt.ask("SSH port", default=22)
                key_path = _ask("SSH private key path (Enter to skip)", default="")
                # Catch the classic .pub mistake
                if key_path.endswith('.pub'):
                    console.print("[yellow]⚠ That's a public key (.pub). SSH needs the private key.[/yellow]")
                    key_path = key_path.removesuffix('.pub')
                    console.print(f"[dim]Using: {key_path}[/dim]")

                ssh_tunnel = {
                    'host': ssh_host_name,
                    'user': ssh_user,
                    'port': ssh_port,
                }
                if key_path:
                    ssh_tunnel['key_path'] = key_path
            else:
                # Simple hostname - uses ~/.ssh/config
                ssh_tunnel = ssh_host

        # Step 4: Test connection
        test_uri = uri
        if ssh_tunnel:
            console.print("[yellow]Opening SSH tunnel and testing...[/yellow]")
            try:
                from ..tunnel import open_tunnel
                test_uri = open_tunnel(uri, ssh_tunnel)
            except Exception as e:
                console.print(f"[red]❌ SSH tunnel failed: {e}[/red]")
                if not Confirm.ask("Save host anyway (without tunnel test)?"):
                    return self.add_new_host()
        else:
            console.print("[yellow]Testing connection...[/yellow]")

        is_online, status = _test_connection(test_uri)
        if is_online:
            console.print(f"[green]✅ Connection successful: {status}[/green]")
        else:
            console.print(f"[red]❌ Connection failed: {status}[/red]")
            if not Confirm.ask("Save anyway?"):
                return self.add_new_host()

        # Save
        self.wizard.settings_manager.add_host(name, uri, ssh_tunnel=ssh_tunnel)
        console.print(f"[green]✅ Saved host '{name}'[/green]")

        if ssh_tunnel and is_online:
            return {'resolved_uri': test_uri, 'raw_uri': uri, 'ssh_tunnel': ssh_tunnel}
        return {'resolved_uri': test_uri, 'raw_uri': uri, 'ssh_tunnel': ssh_tunnel}

    def select_database(self, engine, purpose: str = "source") -> str:
        """Select database from list. Accepts a DatabaseEngine."""
        self.wizard.clear_screen()
        console.print(f"\n[bold cyan]Select {purpose.upper()} database:[/bold cyan]\n")

        databases = []
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
            task = progress.add_task("Loading databases...", total=None)
            databases = engine.list_databases()
            progress.remove_task(task)

        table_term = getattr(engine, 'table_term_plural', 'collections').capitalize()

        tbl = Table(title="📚 Available Databases", box=box.ROUNDED)
        tbl.add_column("#", style="cyan", width=4)
        tbl.add_column("Database", style="green")
        tbl.add_column(table_term, style="yellow", justify="right")
        tbl.add_column("Size (MB)", style="magenta", justify="right")

        for i, db in enumerate(databases, 1):
            tbl.add_row(
                str(i),
                db['name'],
                str(db.get('tables_count', db.get('collections', 0))),
                f"{db['size_mb']:.1f}"
            )

        console.print(tbl)

        if not databases:
            console.print("[yellow]No databases found![/yellow]")
            return _ask("Enter database name manually")

        while True:
            choice_str = _ask("\nSelect database")
            try:
                choice = int(choice_str)
                if 1 <= choice <= len(databases):
                    return databases[choice - 1]['name']
                else:
                    console.print(f"[red]Please enter a number between 1 and {len(databases)}[/red]")
            except ValueError:
                console.print("[red]Please enter a valid number[/red]")

    @staticmethod
    def _parse_multi_selection(selection: str, names: list[str], expand_patterns: bool = True) -> list[str]:
        """
        Parse the multi-selection prompt into a list of entries.

        Each comma-separated token is one of:
          - a number (1-based index into `names`),
          - a range of numbers ("3-7"),
          - an exact table name, which must exist,
          - a glob pattern or a `!` exclusion, kept verbatim.

        Numbers and ranges become concrete names; patterns stay patterns, so
        the saved task re-resolves them on every run. A range is only
        recognised as digits-dash-digits: table names containing a dash
        ("events-2024") are names, not broken ranges.

        Raises ValueError for an unknown name or a pattern that matches
        nothing, so a typo is caught here rather than on the first run.

        With `expand_patterns=False` (Redis) any typed token is accepted
        as-is: it is a key pattern for the engine, not a table name.
        """
        selected: list[str] = []
        for part in (p.strip() for p in selection.split(',')):
            if not part:
                continue
            range_match = re.fullmatch(r'(\d+)\s*-\s*(\d+)', part)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                selected.extend(names[i - 1] for i in range(start, end + 1) if 1 <= i <= len(names))
            elif part.isdigit():
                idx = int(part)
                if 1 <= idx <= len(names):
                    selected.append(names[idx - 1])
            elif not expand_patterns or is_pattern(part):
                selected.append(part)
            elif part in names:
                selected.append(part)
            else:
                raise ValueError(f"'{part}' is not a table in this database")

        # De-duplicate, keeping the first occurrence.
        seen: set[str] = set()
        selected = [x for x in selected if not (x in seen or seen.add(x))]

        if expand_patterns and has_patterns(selected):
            # Fail on a pattern that matches nothing (the resolver raises).
            resolve_tables(selected, names)
        return selected


    def select_collection(self, engine, database: str, purpose: str = "source", allow_all: bool = True,
                          allow_multiple: bool = False) -> Any | None:
        """Select collection/table from database. Accepts a DatabaseEngine."""
        term = getattr(engine, 'table_term', 'collection')
        term_plural = getattr(engine, 'table_term_plural', 'collections')

        self.wizard.clear_screen()
        console.print(f"\n[bold cyan]Select {purpose.upper()} {term} from {database}:[/bold cyan]\n")

        collections = []

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
            task = progress.add_task(f"Loading {term_plural}...", total=None)
            for t in engine.list_tables(database):
                collections.append({
                    'name': t['name'],
                    'documents': t.get('rows', 0),
                    'indexes': t.get('indexes', 0),
                })
            progress.remove_task(task)

        # Sort toggle: alphabetical (default) or by row count
        sort_by_name = True

        while True:
            if sort_by_name:
                collections.sort(key=lambda x: x['name'])
                sort_label = "alphabetical"
            else:
                collections.sort(key=lambda x: x['documents'], reverse=True)
                sort_label = "by rows"

            self.wizard.clear_screen()
            console.print(f"\n[bold cyan]Select {purpose.upper()} {term} from {database}:[/bold cyan]\n")

            table = Table(title=f"📁 {term_plural.capitalize()} in {database} ({sort_label})", box=box.ROUNDED)
            table.add_column("#", style="cyan", width=4)
            table.add_column(term.capitalize(), style="green")
            table.add_column("Rows", style="yellow", justify="right")
            table.add_column("Indexes", style="magenta", justify="right")

            if allow_all:
                table.add_row("0", "[bold]ALL COLLECTIONS[/bold]", format_number(sum(c['documents'] for c in collections)), "-")

            for i, coll in enumerate(collections, 1):
                table.add_row(
                    str(i),
                    coll['name'],
                    f"{format_number(coll['documents'])}",
                    str(coll['indexes'])
                )

            console.print(table)
            console.print("[dim]Type 's' to toggle sort (alphabetical/by rows)[/dim]")

            if allow_multiple:
                console.print("\n[yellow]💡 Multiple selection mode:[/yellow]")
                console.print("[dim]Enter numbers separated by commas (e.g., 1,3,5) or ranges (e.g., 1-5)[/dim]")
                console.print("[dim]Names and patterns work too: users,audit*,compliance_*,!compliance_*_backup[/dim]")
                console.print("[dim]Patterns are saved as typed and re-resolved on every run[/dim]")
                console.print("[dim]Enter 0 for ALL collections, or leave empty to cancel[/dim]")

                selection = Prompt.ask("\nSelect collections")

                if selection.lower() == 's':
                    sort_by_name = not sort_by_name
                    continue

                if not selection:
                    return []

                if selection == "0":
                    return None  # All collections

                try:
                    selected = self._parse_multi_selection(
                        selection, [c['name'] for c in collections],
                        expand_patterns=getattr(engine, 'supports_table_patterns', True),
                    )
                except ValueError as e:
                    console.print(f"[red]Invalid selection: {e}[/red]")
                    return []

                if not selected:
                    console.print("[red]No valid collections selected[/red]")
                    return []

                if getattr(engine, 'supports_table_patterns', True) and has_patterns(selected):
                    # Preview what the patterns match NOW. The task keeps the
                    # patterns, so a later run may match more (new tables).
                    resolved = resolve_tables(selected, [c['name'] for c in collections])
                    console.print(
                        f"[green]✅ {', '.join(selected)} → {len(resolved)} {term_plural} today:[/green] "
                        f"[dim]{', '.join(resolved)}[/dim]"
                    )
                    if not Confirm.ask("Use this selection?", default=True):
                        continue
                else:
                    console.print(f"[green]✅ Selected {len(selected)} collections: {', '.join(selected[:3])}{'...' if len(selected) > 3 else ''}[/green]")
                return selected

            else:
                choice_str = _ask(f"\nSelect {term} (0 for ALL)" if allow_all else f"\nSelect {term}")

                if choice_str.lower() == 's':
                    sort_by_name = not sort_by_name
                    continue

                try:
                    choice = int(choice_str)
                    if choice == 0 and allow_all:
                        return None
                    if 1 <= choice <= len(collections):
                        return collections[choice - 1]['name']
                    console.print(f"[red]Please enter a number between {'0' if allow_all else '1'} and {len(collections)}[/red]")
                except ValueError:
                    console.print("[red]Please enter a valid number[/red]")
