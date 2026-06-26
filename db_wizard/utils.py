"""
Generic utility functions for db-wizard.
No database-specific imports here - all engine-specific code lives in engines/.
"""

from typing import Any

from rich.console import Console
from rich.table import Table


def mask_password(uri: str | None) -> str:
    """Replace password in URI with ****. Works for both mongodb:// and mysql://."""
    if not uri:
        return "(not set)"
    if '@' not in uri:
        return uri
    before_at, after_at = uri.split('@', 1)
    if ':' in before_at.split('://')[-1]:
        scheme_user = before_at.rsplit(':', 1)[0]
        return f"{scheme_user}:****@{after_at}"
    return uri

console = Console()


# ============================================================================
# Host Resolution
# ============================================================================

class HostInfo:
    """Resolved info about a database host URI."""
    def __init__(
        self,
        uri: str,
        name: str,
        has_tunnel: bool,
        tunnel_config: str | dict | None,
        saved_name: str | None = None,
    ):
        self.uri = uri
        # Friendly display name (saved host name or hostname from URI)
        self.name = name
        # Saved host name from settings, if the URI matched a configured host
        self.saved_name = saved_name
        # Whether this host requires an SSH tunnel
        self.has_tunnel = has_tunnel
        # SSH tunnel config (string hostname or dict with host/user/port/key_path)
        self.tunnel_config = tunnel_config

    @property
    def tunnel_label(self) -> str:
        """Human-readable tunnel target (e.g. 'scriba', 'claim')."""
        if not self.tunnel_config:
            return ""
        if isinstance(self.tunnel_config, str):
            return self.tunnel_config
        return self.tunnel_config.get('host', '?')

    def masked_uri(self) -> str:
        return mask_password(self.uri)

    def connection_label(self, database: str | None = None) -> str:
        """Masked URI for confirmation prompts, with host:db prefix and SSH badge when applicable."""
        parts = []
        if self.saved_name:
            tag = f"{self.saved_name}:{database}" if database else self.saved_name
            parts.append(f"[cyan]{tag}[/cyan]")
        parts.append(self.masked_uri())
        label = " ".join(parts)
        if self.has_tunnel:
            label += f" [yellow][OVER SSH {self.tunnel_label}][/yellow]"
        return label


def resolve_host(uri: str | None, tunnel_override: str | dict | None = None) -> HostInfo:
    """
    Resolve a database URI against saved hosts.
    Returns a HostInfo with the friendly name, tunnel detection, and tunnel config.

    Args:
        uri: Database URI to resolve
        tunnel_override: Explicit tunnel config from task (takes precedence)
    """
    from urllib.parse import urlparse

    if not uri:
        return HostInfo(uri='', name='(not set)', has_tunnel=False, tunnel_config=None)

    # Start with what we can extract from the URI itself
    try:
        parsed = urlparse(uri)
        fallback_name = parsed.hostname or 'localhost'
    except Exception:
        fallback_name = 'unknown'

    resolved_name = None
    tunnel_config = tunnel_override
    has_tunnel = bool(tunnel_override)

    # Look up in saved hosts for a friendly name and tunnel config
    try:
        from .settings import SettingsManager
        for host_name, host_val in SettingsManager().list_hosts().items():
            if isinstance(host_val, dict):
                host_uri = host_val.get('uri', '')
                host_tunnel = host_val.get('ssh_tunnel')
            else:
                host_uri = host_val
                host_tunnel = None

            if host_uri == uri:
                resolved_name = host_name
                if host_tunnel and not tunnel_config:
                    tunnel_config = host_tunnel
                    has_tunnel = True
                break
    except Exception:
        pass

    return HostInfo(
        uri=uri,
        name=resolved_name or fallback_name,
        has_tunnel=has_tunnel,
        tunnel_config=tunnel_config,
        saved_name=resolved_name,
    )


# ============================================================================
# Storage Helper Functions
# ============================================================================

def storage_config_to_url(storage_url_or_config: str | dict[str, Any]) -> str:
    """
    Convert storage config dict to URL string.
    If already a string, return as-is.
    """
    if isinstance(storage_url_or_config, str):
        return storage_url_or_config

    config = storage_url_or_config
    config_type = config.get('type', 'local')

    if config_type == 'ssh':
        user = config['user']
        host = config['host']
        port = config.get('port', 22)
        path = config.get('path', '/')
        if not path.startswith('/'):
            path = '/' + path
        return f"ssh://{user}@{host}:{port}{path}"

    elif config_type == 'ftp':
        user = config['user']
        password = config['password']
        host = config['host']
        port = config.get('port', 21)
        path = config.get('path', '/')
        if not path.startswith('/'):
            path = '/' + path
        return f"ftp://{user}:{password}@{host}:{port}{path}"

    else:  # local
        return config.get('path', '/')


# ============================================================================
# Selection Helpers
# ============================================================================

def parse_collection_selection(selection: str, max_value: int) -> list[int]:
    """Parse selection string like '1,3-5,7' into list of indices."""
    if selection.upper() == 'ALL':
        return list(range(max_value))

    indices = []
    parts = selection.split(',')

    for part in parts:
        part = part.strip()
        if '-' in part:
            start, end = part.split('-')
            try:
                start_idx = int(start) - 1
                end_idx = int(end)
                # Clamp to valid bounds
                start_idx = max(0, start_idx)
                end_idx = min(end_idx, max_value)
                indices.extend(range(start_idx, end_idx))
            except ValueError:
                console.print(f"[red]Invalid range: {part}[/red]")
        else:
            try:
                idx = int(part) - 1
                if 0 <= idx < max_value:
                    indices.append(idx)
            except ValueError:
                console.print(f"[red]Invalid number: {part}[/red]")

    return sorted(list(set(indices)))


# ============================================================================
# Display Helpers
# ============================================================================

def display_copy_summary(
    source_db: str, target_db: str,
    tables: list[str], row_counts: dict[str, int],
    drop_target: bool = False, table_term: str = "collection"
):
    """Display a summary of the copy operation."""
    from .formatting import format_docs

    table = Table(title="📋 Copy Summary", show_header=True)
    table.add_column(table_term.capitalize(), style="cyan")
    table.add_column("Rows", justify="right", style="green")
    table.add_column("Status", style="yellow")

    total_rows = 0
    for t in tables:
        count = row_counts.get(t, 0)
        total_rows += count
        status = "Drop & Replace" if drop_target else "Merge"
        table.add_row(t, format_docs(count), status)

    console.print(table)
    console.print(f"\n[bold]Total:[/bold] {format_docs(total_rows)} rows")
    console.print(f"[bold]Source:[/bold] {source_db}")


def format_task_table_row(
    task_name: str, task_config: dict, count: bool = False
) -> tuple[str, str, str]:
    """
    Format a task for display in a table.
    Returns: (task_name, source_target_display, collection_display)

    Args:
        task_name: Name of the task
        task_config: Task configuration dictionary
        count: If True, connect to database to count rows (slow on remote DBs).
    """
    from urllib.parse import urlparse

    task_type = task_config.get('type', 'copy')

    if task_type in ('backup', 'restore'):
        if task_type == 'backup':
            display = f"[cyan]BACKUP:[/cyan] {task_config.get('database', 'N/A')}"
            coll_display = task_config.get('collections', 'ALL')
            if isinstance(coll_display, list):
                coll_display = f"{len(coll_display)} items"
        else:
            display = f"[cyan]RESTORE:[/cyan] {task_config.get('backup_file', 'N/A')}"
            coll_display = task_config.get('target_database', 'from backup')

        return task_name, display, str(coll_display)

    # Copy task
    source_uri = task_config.get('source_uri')
    target_uri = task_config.get('target_uri')
    source_db = task_config.get('source_db')
    target_db = task_config.get('target_db')

    if not all([source_uri, target_uri, source_db, target_db]):
        return task_name, "[red]Invalid task config[/red]", "N/A"

    # Resolve host names and SSH tunnel info
    source_info = resolve_host(source_uri, task_config.get('source_ssh_tunnel'))
    target_info = resolve_host(target_uri, task_config.get('target_ssh_tunnel'))
    source_host = source_info.name
    target_host = target_info.name

    # Handle collection/table display
    coll = task_config.get('source_collection', 'ALL')
    if isinstance(coll, list):
        coll_display = f"{len(coll)} items"
        collection_list = coll
    elif coll and coll != 'ALL':
        coll_display = str(coll)
        collection_list = [coll]
    else:
        coll_display = 'ALL'
        collection_list = None

    # Default: fast mode, no connections
    source_host_color = "white"
    target_host_color = "white"
    source_count = ""
    target_count = ""

    # Only connect and count if explicitly requested with --count
    if count:
        try:
            from .engine import EngineFactory

            with console.status(f"[dim]Counting rows for {task_name}...[/dim]"):
                # Source
                source_engine = EngineFactory.create(source_uri)
                try:
                    source_engine.connect(timeout=3000)
                    source_count_int = 0
                    if collection_list:
                        for c in collection_list:
                            try:
                                source_count_int += source_engine.count_rows(source_db, c)
                            except Exception:
                                pass
                    else:
                        for t in source_engine.list_tables(source_db):
                            source_count_int += t.get('rows', 0)
                    source_count = f"~{source_count_int:_} rows" if source_count_int > 0 else "0 rows"
                    source_host_color = "green"
                except Exception:
                    source_host_color = "red"
                finally:
                    source_engine.close()

                # Target
                target_engine = EngineFactory.create(target_uri)
                try:
                    target_engine.connect(timeout=3000)
                    target_count_int = 0
                    if collection_list:
                        for c in collection_list:
                            try:
                                target_count_int += target_engine.count_rows(target_db, c)
                            except Exception:
                                pass
                    else:
                        for t in target_engine.list_tables(target_db):
                            target_count_int += t.get('rows', 0)
                    target_count = f"~{target_count_int:_} rows" if target_count_int > 0 else "0 rows"
                    target_host_color = "green"

                    if target_count_int < source_count_int:
                        target_count = f"[green]{target_count}[/green]"
                    elif target_count_int > source_count_int:
                        target_count = f"[red]{target_count}[/red]"
                except Exception:
                    target_count = "? rows"
                    target_host_color = "red"
                finally:
                    target_engine.close()

        except Exception:
            source_host_color = "white"
            target_host_color = "white"
            source_count = ""
            target_count = ""

    # Build display string
    def _format_host_part(info, db, color, count_str):
        host_db = f"[{color}]{info.name}[/{color}]:[blue]{db}[/blue]"
        if info.has_tunnel:
            host_db += f" [yellow][OVER SSH {info.tunnel_label}][/yellow]"
        return f"{host_db} {count_str}".strip()

    source_part = _format_host_part(source_info, source_db, source_host_color, source_count)
    target_part = _format_host_part(target_info, target_db, target_host_color, target_count)

    source_target = f"{source_part} → {target_part}"

    return task_name, source_target, coll_display


# Re-export for backward compatibility
from .formatting import format_document_count  # noqa: F401
