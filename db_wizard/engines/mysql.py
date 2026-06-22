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
    MYSQL_SYSTEM_DATABASES,
    PIPE_TIMEOUT,
)

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

        if source_table:
            return self._copy_table(
                source_params, source_db, source_table,
                target_params, target_db, target_table or source_table,
                drop_target=drop_target, schema_only=schema_only
            )
        else:
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

            try:
                _, restore_stderr = restore_process.communicate(timeout=PIPE_TIMEOUT)
                dump_stderr = dump_process.stderr.read()
            except subprocess.TimeoutExpired:
                restore_process.kill()
                dump_process.kill()
                console.print(f"[red]mysqldump | mysql timed out after {PIPE_TIMEOUT // 60} minutes[/red]")
                return {'documents_copied': 0, 'method': 'mysqldump_timeout'}

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

                try:
                    _, restore_stderr = restore_process.communicate(timeout=PIPE_TIMEOUT)
                    dump_stderr = dump_process.stderr.read()
                except subprocess.TimeoutExpired:
                    restore_process.kill()
                    dump_process.kill()
                    console.print(f"[red]Timed out after {PIPE_TIMEOUT // 60} minutes[/red]")
                    return {'documents_copied': 0, 'method': 'mysqldump_timeout'}

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
