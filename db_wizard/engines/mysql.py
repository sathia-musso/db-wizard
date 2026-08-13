from __future__ import annotations
"""
MySQL engine implementation.
Uses mysqldump/mysql CLI tools exclusively - no Python MySQL driver needed.
"""

import os
import shutil
import subprocess
from typing import Any, Self
from urllib.parse import urlparse, unquote

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ..engine import DatabaseEngine
from ..formatting import format_number
from ..constants import (
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_MYSQL_PORT,
    INCREMENTAL_OVERLAP_ROWS,
    MYSQL_SYSTEM_DATABASES,
)
from ..utils import wait_for_dump_pipe

console = Console()


def parse_mysql_uri(uri: str) -> dict[str, Any]:
    """
    Parse a mysql:// URI into connection components.

    Format: mysql://user:password@host:port/database
    Password is URL-decoded to handle special characters.

    Returns dict with: host, port, user, password, database
    """
    parsed = urlparse(uri)
    return {
        'host': parsed.hostname or 'localhost',
        'port': parsed.port or DEFAULT_MYSQL_PORT,
        'user': unquote(parsed.username or 'root'),
        'password': unquote(parsed.password or ''),
        'database': parsed.path.lstrip('/') if parsed.path else None,
    }


def _build_mysql_args(params: dict[str, Any]) -> list[str]:
    """
    Build common mysql/mysqldump CLI arguments from parsed URI params.
    Returns list of args like ['-h', 'host', '-P', '3306', '-u', 'user']
    """
    args = [
        '-h', params['host'],
        '-P', str(params['port']),
        '-u', params['user'],
    ]
    # Skip SSL by default - most internal/tunneled servers don't support it
    args.append('--skip-ssl')
    return args

def _get_mysql_env(params: dict[str, Any]) -> dict[str, str]:
    """Return environment dict with MYSQL_PWD set if password exists."""
    env = os.environ.copy()
    if params.get('password'):
        env['MYSQL_PWD'] = params['password']
    return env


def _escape_literal(value: str) -> str:
    """Escape a string so it can be embedded in a single-quoted SQL literal."""
    return value.replace("\\", "\\\\").replace("'", "''")


def _mysql_scalar(params: dict[str, Any], sql: str) -> str | None:
    """
    Run a query expected to return one value and give it back as a string.

    Returns None when the query fails (e.g. the table does not exist), when it
    returns no rows, or when the value is SQL NULL (empty table). Callers treat
    all three the same way: there is nothing to resume from.
    """
    cmd = ['mysql'] + _build_mysql_args(params) + ['-N', '-B', '-e', sql]
    try:
        result = subprocess.run(cmd, env=_get_mysql_env(params), capture_output=True, text=True)
    except subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value or value == 'NULL':
        return None
    return value


def _detect_auto_increment_column(params: dict[str, Any], database: str, table: str) -> str | None:
    """Find the AUTO_INCREMENT column of a table, if it has one."""
    sql = (
        f"SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA = '{_escape_literal(database)}' "
        f"AND TABLE_NAME = '{_escape_literal(table)}' "
        f"AND EXTRA LIKE '%auto_increment%' LIMIT 1"
    )
    return _mysql_scalar(params, sql)


def _list_table_names(params: dict[str, Any], database: str) -> list[str]:
    """List base table names of a database (no views)."""
    cmd = ['mysql'] + _build_mysql_args(params) + [
        '-N', '-B', '-e',
        f"SELECT TABLE_NAME FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = '{_escape_literal(database)}' "
        f"AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
    ]
    result = subprocess.run(cmd, env=_get_mysql_env(params), capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]


def _run_dump_pipe(
    dump_cmd: list[str], source_params: dict[str, Any],
    restore_cmd: list[str], target_params: dict[str, Any]
) -> tuple[bool, str]:
    """
    Run `dump_cmd | restore_cmd` and report (success, error_message).
    The pipe runs until it finishes; Ctrl-C kills both processes.
    """
    try:
        dump_process = subprocess.Popen(
            dump_cmd, env=_get_mysql_env(source_params),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        restore_process = subprocess.Popen(
            restore_cmd, env=_get_mysql_env(target_params),
            stdin=dump_process.stdout, stderr=subprocess.PIPE
        )
        dump_process.stdout.close()

        interrupted, restore_stderr, dump_stderr = wait_for_dump_pipe(dump_process, restore_process)
        if interrupted:
            return False, "interrupted by user"

        if restore_process.returncode != 0:
            return False, (restore_stderr or dump_stderr or b'').decode('utf-8', errors='replace').strip()

        return True, ""

    except Exception as e:
        return False, str(e)


def _build_incremental_where(column: str, watermark: str) -> str:
    """
    Build the WHERE clause that selects only the rows the target is missing.

    Numeric watermarks step back INCREMENTAL_OVERLAP_ROWS rows, because an
    AUTO_INCREMENT id lower than MAX(id) can still be committed after it.
    Non-numeric watermarks (timestamps, mostly) use >= so rows sharing the
    boundary value are not lost. Either way the re-copied rows collide on the
    primary key and INSERT IGNORE drops them.
    """
    col = f"`{column}`"
    try:
        return f"{col} > {int(watermark) - INCREMENTAL_OVERLAP_ROWS}"
    except ValueError:
        return f"{col} >= '{_escape_literal(watermark)}'"


class MySQLEngine(DatabaseEngine):
    """
    MySQL engine implementation.
    Uses mysqldump and mysql CLI tools for all operations.
    Zero Python MySQL dependencies.
    """

    def __init__(self, uri: str):
        super().__init__(uri)
        self.params = parse_mysql_uri(uri)

    # -- Connection lifecycle --

    def connect(self, timeout: int = DEFAULT_CONNECTION_TIMEOUT) -> Self:
        """Verify MySQL connectivity by running a quick query."""
        cmd = ['mysql'] + _build_mysql_args(self.params) + [
            '--connect-timeout', str(max(1, timeout // 1000)),
            '-e', 'SELECT 1',
        ]
        result = subprocess.run(cmd, env=_get_mysql_env(self.params), capture_output=True, text=True, timeout=timeout // 1000 + 5)
        if result.returncode != 0:
            error = result.stderr.strip()
            raise ConnectionError(f"MySQL connection failed: {error}")
        return self

    def close(self) -> None:
        """No persistent connection to close (CLI-based)."""
        pass

    def test_connection(self, timeout: int = DEFAULT_CONNECTION_TIMEOUT) -> tuple[bool, str]:
        """Quick connectivity check."""
        try:
            self.connect(timeout=timeout)
            # Count databases
            cmd = ['mysql'] + _build_mysql_args(self.params) + [
                '--connect-timeout', str(max(1, timeout // 1000)),
                '-N', '-e', 'SELECT COUNT(*) FROM information_schema.SCHEMATA',
            ]
            result = subprocess.run(cmd, env=_get_mysql_env(self.params), capture_output=True, text=True, timeout=timeout // 1000 + 5)
            if result.returncode == 0:
                db_count = result.stdout.strip()
                return True, f"OK ({db_count} databases)"
            return True, "OK"
        except Exception as e:
            return False, str(e)

    # -- Introspection --

    def list_databases(self) -> list[dict[str, Any]]:
        """List non-system databases with approximate size."""
        cmd = ['mysql'] + _build_mysql_args(self.params) + [
            '-N', '-e',
            "SELECT s.SCHEMA_NAME, COUNT(t.TABLE_NAME), "
            "ROUND(SUM(t.DATA_LENGTH + t.INDEX_LENGTH) / 1024 / 1024, 1) "
            "FROM information_schema.SCHEMATA s "
            "LEFT JOIN information_schema.TABLES t ON s.SCHEMA_NAME = t.TABLE_SCHEMA "
            "GROUP BY s.SCHEMA_NAME ORDER BY s.SCHEMA_NAME"
        ]
        result = subprocess.run(cmd, env=_get_mysql_env(self.params), capture_output=True, text=True)
        if result.returncode != 0:
            return []

        databases = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('\t')
            if len(parts) >= 3:
                db_name = parts[0]
                if db_name in MYSQL_SYSTEM_DATABASES:
                    continue
                try:
                    tables_count = int(parts[1]) if parts[1] != 'NULL' else 0
                    size_mb = float(parts[2]) if parts[2] != 'NULL' else 0.0
                except (ValueError, IndexError):
                    tables_count = 0
                    size_mb = 0.0
                databases.append({
                    'name': db_name,
                    'tables_count': tables_count,
                    'size_mb': size_mb,
                })
        return databases

    def list_tables(self, database: str) -> list[dict[str, Any]]:
        """List tables in a database with row counts and index info."""
        safe_db = database.replace("'", "''")
        cmd = ['mysql'] + _build_mysql_args(self.params) + [
            '-N', '-e',
            f"SELECT TABLE_NAME, TABLE_ROWS, "
            f"(SELECT COUNT(*) FROM information_schema.STATISTICS "
            f"WHERE TABLE_SCHEMA = '{safe_db}' AND TABLE_NAME = t.TABLE_NAME) "
            f"FROM information_schema.TABLES t "
            f"WHERE TABLE_SCHEMA = '{safe_db}' AND TABLE_TYPE = 'BASE TABLE' "
            f"ORDER BY TABLE_NAME"
        ]
        result = subprocess.run(cmd, env=_get_mysql_env(self.params), capture_output=True, text=True)
        if result.returncode != 0:
            return []

        tables = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('\t')
            if len(parts) >= 3:
                try:
                    rows = int(parts[1]) if parts[1] != 'NULL' else 0
                    indexes = int(parts[2]) if parts[2] != 'NULL' else 0
                except (ValueError, IndexError):
                    rows = 0
                    indexes = 0
                tables.append({
                    'name': parts[0],
                    'rows': rows,
                    'indexes': indexes,
                })
        return tables

    def count_rows(self, database: str, table: str) -> int:
        """Get approximate row count (from TABLE_STATUS, fast)."""
        safe_db = database.replace("'", "''")
        safe_table = table.replace("'", "''")
        cmd = ['mysql'] + _build_mysql_args(self.params) + [
            '-N', '-e',
            f"SELECT TABLE_ROWS FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = '{safe_db}' AND TABLE_NAME = '{safe_table}'"
        ]
        result = subprocess.run(cmd, env=_get_mysql_env(self.params), capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            try:
                return int(result.stdout.strip())
            except ValueError:
                return 0
        return 0

    # -- Incremental copy support --

    supports_incremental = True

    def detect_watermark_column(self, database: str, table: str) -> str | None:
        """The AUTO_INCREMENT column, which is the only one MySQL guarantees to grow."""
        return _detect_auto_increment_column(self.params, database, table)

    def max_watermark(self, database: str, table: str, column: str) -> str | None:
        """Highest value of `column`, as a string. None if table missing or empty."""
        return _mysql_scalar(self.params, f"SELECT MAX(`{column}`) FROM `{database}`.`{table}`")

    def sample_rows(self, database: str, table: str, limit: int = 5) -> tuple[list[str], list[list[str]]]:
        """Fetch sample rows via mysql CLI."""
        cmd = ['mysql'] + _build_mysql_args(self.params) + [
            '-N', '--column-names', '-e',
            f"SELECT * FROM `{database}`.`{table}` LIMIT {limit}"
        ]
        result = subprocess.run(cmd, env=_get_mysql_env(self.params), capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            return [], []

        lines = result.stdout.strip().split('\n')
        if not lines:
            return [], []

        # First line is column headers (thanks to --column-names)
        columns = lines[0].split('\t')
        rows = [line.split('\t') for line in lines[1:]]
        return columns, rows

    # -- Tools --

    def check_tools(self) -> dict[str, bool]:
        """Check if MySQL CLI tools are available."""
        return {
            'mysqldump': shutil.which('mysqldump') is not None,
            'mysql': shutil.which('mysql') is not None,
        }

    # -- Core operations --

    def dump(
        self,
        database: str,
        tables: list[str] | None,
        output_path: str
    ) -> bool:
        """Dump database using mysqldump to a .sql file."""
        if not shutil.which('mysqldump'):
            console.print("[red]mysqldump not found![/red]")
            return False

        cmd = ['mysqldump'] + _build_mysql_args(self.params) + [
            '--single-transaction',
            '--skip-lock-tables',
            database,
        ]

        if tables:
            cmd.extend(tables)

        # Write to output file
        os.makedirs(output_path, exist_ok=True)
        output_file = os.path.join(output_path, f"{database}.sql")

        with open(output_file, 'w') as f:
            result = subprocess.run(cmd, env=_get_mysql_env(self.params), stdout=f, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0:
            console.print(f"[red]mysqldump error: {result.stderr}[/red]")
            return False
        return True

    def restore(
        self,
        input_path: str,
        target_database: str,
        drop_target: bool = False
    ) -> bool:
        """Restore from .sql file using mysql CLI."""
        if not shutil.which('mysql'):
            console.print("[red]mysql CLI not found![/red]")
            return False

        # Find .sql file in input_path
        if os.path.isfile(input_path) and input_path.endswith('.sql'):
            sql_file = input_path
        else:
            # Look for .sql files in the directory
            sql_files = [f for f in os.listdir(input_path) if f.endswith('.sql')]
            if not sql_files:
                console.print("[red]No .sql files found in restore path[/red]")
                return False
            sql_file = os.path.join(input_path, sql_files[0])

        # Drop and recreate database if requested
        if drop_target:
            drop_cmd = ['mysql'] + _build_mysql_args(self.params) + [
                '-e', f"DROP DATABASE IF EXISTS `{target_database}`; CREATE DATABASE `{target_database}`"
            ]
            subprocess.run(drop_cmd, env=_get_mysql_env(self.params), capture_output=True, text=True)

        # Restore
        cmd = ['mysql'] + _build_mysql_args(self.params) + [target_database]

        with open(sql_file, 'r') as f:
            result = subprocess.run(cmd, env=_get_mysql_env(self.params), stdin=f, capture_output=True, text=True)

        if result.returncode != 0:
            console.print(f"[red]mysql restore error: {result.stderr}[/red]")
            return False
        return True

    def copy(
        self,
        source_engine: 'MySQLEngine',
        source_db: str,
        source_table: str | None,
        target_db: str,
        target_table: str | None,
        drop_target: bool = False,
        force: bool = False,
        **kwargs
    ) -> dict[str, Any]:
        """
        Copy from source MySQLEngine into this engine (target).
        Core operation: mysqldump ... | mysql ...
        This is the direct equivalent of the user's shell alias.
        """
        if not shutil.which('mysqldump') or not shutil.which('mysql'):
            console.print("[red]mysqldump and mysql CLI tools are required[/red]")
            return {'documents_copied': 0, 'method': 'failed'}

        source_params = source_engine.params
        target_params = self.params

        # schema_only: dump table structure (and indexes) without any rows.
        # Implemented via mysqldump --no-data.
        schema_only = kwargs.get('schema_only', False)

        # incremental: copy only the rows sitting above the target's watermark.
        incremental = kwargs.get('incremental', False)
        since_column = kwargs.get('since_column')

        # The two flags pull in opposite directions: schema-only copies no rows
        # at all, incremental exists precisely to copy the missing ones.
        if incremental and schema_only:
            raise ValueError("--incremental and --schema-only are mutually exclusive.")

        # Dropping the target destroys the very watermark we resume from, which
        # would silently turn an incremental run into a full copy.
        if incremental and drop_target:
            raise ValueError(
                "--incremental and --drop-target are mutually exclusive: "
                "dropping the target erases the watermark to resume from."
            )

        if source_table:
            if incremental:
                return self._copy_table_incremental(
                    source_params, source_db, source_table,
                    target_params, target_db, target_table or source_table,
                    since_column=since_column
                )
            return self._copy_table(
                source_params, source_db, source_table,
                target_params, target_db, target_table or source_table,
                drop_target=drop_target, schema_only=schema_only
            )
        else:
            if incremental:
                return self._copy_database_incremental(
                    source_params, source_db,
                    target_params, target_db,
                    since_column=since_column
                )
            return self._copy_database(
                source_params, source_db,
                target_params, target_db,
                drop_target=drop_target, schema_only=schema_only
            )

    # -- UI terminology --

    @property
    def table_term(self) -> str:
        return "table"

    @property
    def table_term_plural(self) -> str:
        return "tables"

    @property
    def scheme(self) -> str:
        return "mysql"

    # =========================================================================
    # MySQL-specific helpers
    # =========================================================================

    @staticmethod
    def build_connection_uri(
        host: str, port: int = DEFAULT_MYSQL_PORT,
        username: str = 'root', password: str = '',
        database: str | None = None
    ) -> str:
        """Build a MySQL connection URI from components."""
        from urllib.parse import quote
        auth = f"{quote(username)}:{quote(password)}@" if password else f"{quote(username)}@"
        uri = f"mysql://{auth}{host}:{port}"
        if database:
            uri += f"/{database}"
        return uri

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _copy_table(
        self,
        source_params: dict, source_db: str, source_table: str,
        target_params: dict, target_db: str, target_table: str,
        drop_target: bool = False, schema_only: bool = False
    ) -> dict[str, Any]:
        """Copy a single table via mysqldump | mysql pipe."""

        # Get source row count before copy
        source_count = 0
        try:
            cmd = ['mysql'] + _build_mysql_args(source_params) + [
                '-N', '-e', f"SELECT COUNT(*) FROM `{source_db}`.`{source_table}`"
            ]
            result = subprocess.run(cmd, env=_get_mysql_env(source_params), capture_output=True, text=True)
            if result.returncode == 0:
                source_count = int(result.stdout.strip())
        except (ValueError, subprocess.SubprocessError):
            pass

        if schema_only:
            console.print("[cyan]🏗️  Schema-only copy (no data) via mysqldump --no-data...[/cyan]")
        else:
            console.print(f"[green]📊 Table has ~{format_number(source_count)} rows[/green]")
            console.print("[cyan]🚀 Using mysqldump | mysql pipe...[/cyan]")

        # Build dump command. --no-data dumps CREATE TABLE statements only.
        dump_cmd = ['mysqldump'] + _build_mysql_args(source_params) + [
            '--single-transaction',
            '--skip-lock-tables',
        ]
        if schema_only:
            dump_cmd.append('--no-data')
        dump_cmd += [source_db, source_table]

        # Build restore command
        # Ensure target database exists
        create_db_cmd = ['mysql'] + _build_mysql_args(target_params) + [
            '-e', f"CREATE DATABASE IF NOT EXISTS `{target_db}`"
        ]
        subprocess.run(create_db_cmd, env=_get_mysql_env(target_params), capture_output=True, text=True)

        if drop_target:
            drop_cmd = ['mysql'] + _build_mysql_args(target_params) + [
                '-e', f"DROP TABLE IF EXISTS `{target_db}`.`{target_table}`"
            ]
            subprocess.run(drop_cmd, env=_get_mysql_env(target_params), capture_output=True, text=True)
            console.print(f"[yellow]🗑️  Dropped target table {target_db}.{target_table}[/yellow]")

        restore_cmd = ['mysql'] + _build_mysql_args(target_params) + [target_db]

        # Pipe: mysqldump | mysql
        try:
            dump_process = subprocess.Popen(dump_cmd, env=_get_mysql_env(source_params), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            restore_process = subprocess.Popen(restore_cmd, env=_get_mysql_env(target_params), stdin=dump_process.stdout, stderr=subprocess.PIPE)
            dump_process.stdout.close()

            interrupted, restore_stderr, dump_stderr = wait_for_dump_pipe(dump_process, restore_process)
            if interrupted:
                console.print("[yellow]Interrupted: mysqldump | mysql stopped, target may be incomplete[/yellow]")
                return {'documents_copied': 0, 'method': 'mysqldump_interrupted'}

            if restore_process.returncode != 0:
                error_msg = (restore_stderr or dump_stderr or b'').decode('utf-8', errors='replace').strip()
                console.print(f"[red]mysqldump | mysql failed: {error_msg}[/red]")
                return {'documents_copied': 0, 'method': 'mysqldump_failed'}

        except Exception as e:
            console.print(f"[red]Copy failed: {e}[/red]")
            return {'documents_copied': 0, 'method': 'error'}

        # Get target row count after copy
        copied = 0
        try:
            cmd = ['mysql'] + _build_mysql_args(target_params) + [
                '-N', '-e', f"SELECT COUNT(*) FROM `{target_db}`.`{target_table}`"
            ]
            result = subprocess.run(cmd, env=_get_mysql_env(target_params), capture_output=True, text=True)
            if result.returncode == 0:
                copied = int(result.stdout.strip())
        except (ValueError, subprocess.SubprocessError):
            copied = source_count  # Best estimate

        if schema_only:
            console.print(f"[green]✅ Schema copied for table {target_db}.{target_table} (no data)[/green]")
            return {
                'documents_copied': 0,
                'source_count': source_count,
                'method': 'mysqldump_schema',
            }

        console.print(f"[green]✅ Copied {format_number(copied)} rows using mysqldump[/green]")

        return {
            'documents_copied': copied,
            'source_count': source_count,
            'method': 'mysqldump',
        }

    # -- Incremental copy --

    def _copy_table_incremental(
        self,
        source_params: dict, source_db: str, source_table: str,
        target_params: dict, target_db: str, target_table: str,
        since_column: str | None = None
    ) -> dict[str, Any]:
        """
        Copy only the rows the target is missing, assuming an append-only table.

        Reads MAX(watermark) from the TARGET, then dumps from the source
        everything above it. Never sees UPDATEs or DELETEs - by design.
        """
        # The dump emits INSERT statements naming `source_table`, and with
        # --no-create-info there is no CREATE TABLE to rewrite them against.
        if source_table != target_table:
            console.print(
                f"[red]✗ Incremental copy cannot rename tables "
                f"({source_table} -> {target_table}). Use identical names.[/red]"
            )
            return {'documents_copied': 0, 'method': 'incremental_rename_unsupported'}

        # Which column do we resume on? Explicit override wins, otherwise the
        # source's AUTO_INCREMENT column. Without one we refuse rather than
        # quietly copying the whole table.
        column = since_column or _detect_auto_increment_column(source_params, source_db, source_table)
        if not column:
            console.print(
                f"[red]✗ {source_db}.{source_table}: no AUTO_INCREMENT column found. "
                f"Pass --since-column to name the column to resume from.[/red]"
            )
            return {'documents_copied': 0, 'method': 'incremental_no_watermark'}

        # Make sure the target database exists before asking it anything.
        subprocess.run(
            ['mysql'] + _build_mysql_args(target_params) + [
                '-e', f"CREATE DATABASE IF NOT EXISTS `{target_db}`"
            ],
            env=_get_mysql_env(target_params), capture_output=True, text=True
        )

        watermark = _mysql_scalar(
            target_params, f"SELECT MAX(`{column}`) FROM `{target_db}`.`{target_table}`"
        )

        # No watermark means the target table is missing or empty: there is
        # nothing to resume from, so the incremental copy IS the full copy.
        if watermark is None:
            console.print(
                f"[yellow]↻ {target_db}.{target_table} is empty or missing — full copy this time[/yellow]"
            )
            return self._copy_table(
                source_params, source_db, source_table,
                target_params, target_db, target_table,
                drop_target=False, schema_only=False
            )

        where = _build_incremental_where(column, watermark)
        console.print(f"[cyan]⏩ Incremental copy of {source_db}.{source_table} WHERE {where}[/cyan]")

        rows_before = _mysql_scalar(target_params, f"SELECT COUNT(*) FROM `{target_db}`.`{target_table}`")

        # --no-create-info: never touch the existing table definition.
        # --insert-ignore: the overlap rows collide on the PK and get dropped,
        #                  which is what makes re-running this safe.
        dump_cmd = ['mysqldump'] + _build_mysql_args(source_params) + [
            '--single-transaction',
            '--skip-lock-tables',
            '--no-create-info',
            '--skip-add-locks',
            '--insert-ignore',
            f'--where={where}',
            source_db, source_table,
        ]
        restore_cmd = ['mysql'] + _build_mysql_args(target_params) + [target_db]

        ok, error = _run_dump_pipe(dump_cmd, source_params, restore_cmd, target_params)
        if not ok:
            console.print(f"[red]Incremental copy failed: {error}[/red]")
            return {'documents_copied': 0, 'method': 'incremental_failed'}

        rows_after = _mysql_scalar(target_params, f"SELECT COUNT(*) FROM `{target_db}`.`{target_table}`")

        copied = 0
        if rows_before is not None and rows_after is not None:
            try:
                copied = int(rows_after) - int(rows_before)
            except ValueError:
                copied = 0

        console.print(f"[green]✅ Appended {format_number(copied)} new rows to {target_db}.{target_table}[/green]")

        return {
            'documents_copied': copied,
            'watermark_column': column,
            'watermark_value': watermark,
            'method': 'mysqldump_incremental',
        }

    def _copy_database_incremental(
        self,
        source_params: dict, source_db: str,
        target_params: dict, target_db: str,
        since_column: str | None = None
    ) -> dict[str, Any]:
        """
        Incremental copy of every table in a database.

        Each table has its own watermark, so this is a loop of single-table
        copies rather than one big pipe. Returns one result dict per table.
        """
        tables = _list_table_names(source_params, source_db)
        if not tables:
            console.print(f"[yellow]No tables found in {source_db}[/yellow]")
            return {}

        console.print(f"[cyan]⏩ Incremental copy of {len(tables)} tables: {source_db} -> {target_db}[/cyan]")

        results = {}
        for table in tables:
            console.print(f"\n[bold]📁 {table}[/bold]")
            results[table] = self._copy_table_incremental(
                source_params, source_db, table,
                target_params, target_db, table,
                since_column=since_column
            )
        return results

    def _copy_database(
        self,
        source_params: dict, source_db: str,
        target_params: dict, target_db: str,
        drop_target: bool = False, schema_only: bool = False
    ) -> dict[str, Any]:
        """Copy entire database via mysqldump | mysql pipe."""

        if schema_only:
            console.print(f"[cyan]🏗️  Copying schema only (no data) {source_db} -> {target_db}...[/cyan]")
        else:
            console.print(f"[cyan]📦 Copying entire database {source_db} -> {target_db}...[/cyan]")

        # Ensure target database exists (or recreate if drop)
        if drop_target:
            cmd = ['mysql'] + _build_mysql_args(target_params) + [
                '-e', f"DROP DATABASE IF EXISTS `{target_db}`; CREATE DATABASE `{target_db}`"
            ]
        else:
            cmd = ['mysql'] + _build_mysql_args(target_params) + [
                '-e', f"CREATE DATABASE IF NOT EXISTS `{target_db}`"
            ]
        subprocess.run(cmd, env=_get_mysql_env(target_params), capture_output=True, text=True)

        # Build pipe. --no-data dumps CREATE TABLE statements only (schema, no rows).
        dump_cmd = ['mysqldump'] + _build_mysql_args(source_params) + [
            '--single-transaction',
            '--skip-lock-tables',
        ]
        if schema_only:
            dump_cmd.append('--no-data')
        dump_cmd.append(source_db)
        restore_cmd = ['mysql'] + _build_mysql_args(target_params) + [target_db]

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
            task = progress.add_task("[cyan]mysqldump | mysql ...", total=None)

            try:
                dump_process = subprocess.Popen(dump_cmd, env=_get_mysql_env(source_params), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                restore_process = subprocess.Popen(restore_cmd, env=_get_mysql_env(target_params), stdin=dump_process.stdout, stderr=subprocess.PIPE)
                dump_process.stdout.close()

                interrupted, restore_stderr, dump_stderr = wait_for_dump_pipe(dump_process, restore_process)
                if interrupted:
                    console.print("[yellow]Interrupted: copy stopped, target may be incomplete[/yellow]")
                    return {'documents_copied': 0, 'method': 'mysqldump_interrupted'}

                if restore_process.returncode != 0:
                    error_msg = (restore_stderr or dump_stderr or b'').decode('utf-8', errors='replace').strip()
                    console.print(f"[red]Failed: {error_msg}[/red]")
                    return {'documents_copied': 0, 'method': 'mysqldump_failed'}

            except Exception as e:
                console.print(f"[red]Copy failed: {e}[/red]")
                return {'documents_copied': 0, 'method': 'error'}

        # Count total rows in target
        total_rows = 0
        try:
            cmd = ['mysql'] + _build_mysql_args(target_params) + [
                '-N', '-e',
                f"SELECT SUM(TABLE_ROWS) FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA = '{target_db}'"
            ]
            result = subprocess.run(cmd, env=_get_mysql_env(target_params), capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip() and result.stdout.strip() != 'NULL':
                total_rows = int(result.stdout.strip())
        except (ValueError, subprocess.SubprocessError):
            pass

        if schema_only:
            console.print(f"[green]✅ Schema copied for database {target_db} (no data)[/green]")
            return {
                'documents_copied': 0,
                'method': 'mysqldump_schema',
            }

        console.print(f"[green]✅ Database copied: ~{format_number(total_rows)} total rows[/green]")

        return {
            'documents_copied': total_rows,
            'method': 'mysqldump',
        }
