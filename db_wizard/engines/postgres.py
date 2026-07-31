from __future__ import annotations
"""
PostgreSQL engine implementation.
Uses pg_dump/psql CLI tools exclusively - no Python PostgreSQL driver needed.
"""

import os
import shutil
import subprocess
from typing import Any, Self
from urllib.parse import urlparse, unquote

from rich.console import Console

from ..engine import DatabaseEngine
from ..constants import (
    DEFAULT_CONNECTION_TIMEOUT,
    PIPE_TIMEOUT,
)

console = Console()

DEFAULT_POSTGRES_PORT = 5432
POSTGRES_SYSTEM_DATABASES = frozenset({'postgres', 'template0', 'template1'})


def parse_postgres_uri(uri: str) -> dict[str, Any]:
    """
    Parse a postgres:// or postgresql:// URI into connection components.
    """
    parsed = urlparse(uri)
    return {
        'host': parsed.hostname or 'localhost',
        'port': parsed.port or DEFAULT_POSTGRES_PORT,
        'user': unquote(parsed.username or 'postgres'),
        'password': unquote(parsed.password or ''),
        'database': parsed.path.lstrip('/') if parsed.path else 'postgres',
    }


def _build_pg_env(params: dict[str, Any]) -> dict[str, str]:
    """Build environment dict for Postgres CLI tools to pass the password securely."""
    env = os.environ.copy()
    if params['password']:
        env['PGPASSWORD'] = params['password']
    return env


def _build_pg_args(params: dict[str, Any], include_db: bool = True) -> list[str]:
    """Build common psql/pg_dump CLI arguments."""
    args = [
        '-h', params['host'],
        '-p', str(params['port']),
        '-U', params['user'],
    ]
    if include_db and params['database']:
        args.extend(['-d', params['database']])
    return args


class PostgresEngine(DatabaseEngine):
    def __init__(self, uri: str):
        super().__init__(uri)
        self.params = parse_postgres_uri(uri)

    def connect(self, timeout: int = DEFAULT_CONNECTION_TIMEOUT) -> Self:
        """Verify PostgreSQL connectivity by running a quick query."""
        cmd = ['psql'] + _build_pg_args(self.params) + ['-c', 'SELECT 1;']
        env = _build_pg_env(self.params)
        env['PGCONNECT_TIMEOUT'] = str(max(1, timeout // 1000))
        
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout // 1000 + 5)
        if result.returncode != 0:
            error = result.stderr.strip()
            raise ConnectionError(f"PostgreSQL connection failed: {error}")
        return self

    def close(self) -> None:
        pass

    def test_connection(self, timeout: int = DEFAULT_CONNECTION_TIMEOUT) -> tuple[bool, str]:
        try:
            self.connect(timeout=timeout)
            cmd = ['psql'] + _build_pg_args(self.params, include_db=False) + [
                '-d', 'postgres', '-tA', '-c', "SELECT count(*) FROM pg_database WHERE datistemplate = false;"
            ]
            env = _build_pg_env(self.params)
            env['PGCONNECT_TIMEOUT'] = str(max(1, timeout // 1000))
            
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout // 1000 + 5)
            if result.returncode == 0:
                db_count = result.stdout.strip()
                return True, f"OK ({db_count} databases)"
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def list_databases(self) -> list[dict[str, Any]]:
        cmd = ['psql'] + _build_pg_args(self.params, include_db=False) + [
            '-d', 'postgres', '-tA', '-c',
            "SELECT datname, pg_database_size(datname)/1024/1024 as size_mb FROM pg_database WHERE datistemplate = false;"
        ]
        env = _build_pg_env(self.params)
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        
        if result.returncode != 0:
            return []

        databases = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('|')
            if len(parts) >= 2:
                db_name = parts[0]
                if db_name in POSTGRES_SYSTEM_DATABASES:
                    continue
                try:
                    size_mb = float(parts[1]) if parts[1] else 0.0
                except ValueError:
                    size_mb = 0.0
                
                databases.append({
                    'name': db_name,
                    'tables_count': self._count_tables_in_db(db_name, env),
                    'size_mb': size_mb,
                })
        return databases

    def _count_tables_in_db(self, database: str, env: dict) -> int:
        cmd = ['psql'] + _build_pg_args(self.params, include_db=False) + [
            '-d', database, '-tA', '-c',
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';"
        ]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return int(res.stdout.strip())
        return 0

    def list_tables(self, database: str) -> list[dict[str, Any]]:
        cmd = ['psql'] + _build_pg_args(self.params, include_db=False) + [
            '-d', database, '-tA', '-c',
            "SELECT relname, n_live_tup FROM pg_stat_user_tables;"
        ]
        env = _build_pg_env(self.params)
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            return []

        tables = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('|')
            if len(parts) >= 2:
                tables.append({
                    'name': parts[0],
                    'rows': int(parts[1]) if parts[1] else 0,
                    'indexes': 0, # could be queried but keeping it simple
                })
        return tables

    def count_rows(self, database: str, table: str) -> int:
        cmd = ['psql'] + _build_pg_args(self.params, include_db=False) + [
            '-d', database, '-tA', '-c', f"SELECT count(*) FROM {table};"
        ]
        env = _build_pg_env(self.params)
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
        return 0

    def sample_rows(self, database: str, table: str, limit: int = 5) -> tuple[list[str], list[list[str]]]:
        # Using json to easily extract keys and values
        cmd = ['psql'] + _build_pg_args(self.params, include_db=False) + [
            '-d', database, '-tA', '-c',
            f"SELECT row_to_json(t) FROM (SELECT * FROM {table} LIMIT {limit}) t;"
        ]
        env = _build_pg_env(self.params)
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            return [], []

        import json
        rows = []
        keys = []
        for line in result.stdout.strip().split('\n'):
            if line:
                doc = json.loads(line)
                if not keys:
                    keys = list(doc.keys())
                rows.append([str(doc.get(k, '')) for k in keys])
        return keys, rows

    def check_tools(self) -> dict[str, bool]:
        return {
            'pg_dump': shutil.which('pg_dump') is not None,
            'pg_restore': shutil.which('pg_restore') is not None,
            'psql': shutil.which('psql') is not None,
        }

    def dump(self, database: str, tables: list[str] | None, output_path: str) -> bool:
        if not shutil.which('pg_dump'):
            return False

        os.makedirs(output_path, exist_ok=True)
        output_file = os.path.join(output_path, f"{database}.dump")
        
        cmd = ['pg_dump'] + _build_pg_args(self.params, include_db=False) + ['-d', database, '-Fc', '-f', output_file]
        if tables:
            for t in tables:
                cmd.extend(['-t', t])

        env = _build_pg_env(self.params)
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            console.print(f"[red]pg_dump error: {result.stderr}[/red]")
            return False
        return True

    def restore(self, input_path: str, target_database: str, drop_target: bool = False) -> bool:
        if not shutil.which('pg_restore') or not shutil.which('psql'):
            return False

        env = _build_pg_env(self.params)

        # Ensure DB exists
        create_cmd = ['psql'] + _build_pg_args(self.params, include_db=False) + [
            '-d', 'postgres', '-c', f"CREATE DATABASE {target_database};"
        ]
        subprocess.run(create_cmd, env=env, capture_output=True)

        # Find .dump file
        dump_file = input_path
        if os.path.isdir(input_path):
            files = [f for f in os.listdir(input_path) if f.endswith('.dump')]
            if files:
                dump_file = os.path.join(input_path, files[0])

        cmd = ['pg_restore'] + _build_pg_args(self.params, include_db=False) + ['-d', target_database]
        if drop_target:
            cmd.append('--clean')
        cmd.append(dump_file)

        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            console.print(f"[red]pg_restore error: {result.stderr}[/red]")
            return False
        return True

    def copy(
        self,
        source_engine: 'DatabaseEngine',
        source_db: str,
        source_table: str | None,
        target_db: str,
        target_table: str | None,
        drop_target: bool = False,
        force: bool = False,
        **kwargs
    ) -> dict[str, Any]:
        if not shutil.which('pg_dump') or not shutil.which('psql'):
            console.print("[red]pg_dump and psql tools are required[/red]")
            return {'documents_copied': 0, 'method': 'failed'}

        # pg_dump cannot filter rows, so an incremental copy would need a
        # separate COPY (SELECT ... WHERE ...) TO STDOUT pathway. Until that
        # exists, refuse instead of silently copying the whole database.
        if kwargs.get('incremental', False):
            raise ValueError(
                "--incremental is not supported for PostgreSQL: pg_dump cannot "
                "filter rows. Use MySQL or MongoDB, or copy in full."
            )

        source_params = source_engine.params
        target_params = self.params

        # Create target DB
        create_cmd = ['psql'] + _build_pg_args(target_params, include_db=False) + [
            '-d', 'postgres', '-c', f"CREATE DATABASE {target_db};"
        ]
        subprocess.run(create_cmd, env=_build_pg_env(target_params), capture_output=True)

        # schema_only: dump DDL only (CREATE TABLE/INDEX...) without table data,
        # via pg_dump --schema-only.
        schema_only = kwargs.get('schema_only', False)

        dump_cmd = ['pg_dump'] + _build_pg_args(source_params, include_db=False) + ['-d', source_db, '-Fc']
        if schema_only:
            dump_cmd.append('--schema-only')
        if source_table:
            dump_cmd.extend(['-t', source_table])

        restore_cmd = ['pg_restore'] + _build_pg_args(target_params, include_db=False) + ['-d', target_db]
        if drop_target:
            restore_cmd.append('--clean')

        if schema_only:
            console.print(f"[cyan]🏗️  Copying PostgreSQL schema only (no data) {source_db} -> {target_db}...[/cyan]")
        else:
            console.print(f"[cyan]📦 Copying PostgreSQL {source_db} -> {target_db}...[/cyan]")
        
        try:
            dump_env = _build_pg_env(source_params)
            restore_env = _build_pg_env(target_params)
            
            dump_process = subprocess.Popen(dump_cmd, env=dump_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            restore_process = subprocess.Popen(restore_cmd, env=restore_env, stdin=dump_process.stdout, stderr=subprocess.PIPE)
            dump_process.stdout.close()

            try:
                _, restore_stderr = restore_process.communicate(timeout=PIPE_TIMEOUT)
                dump_stderr = dump_process.stderr.read()
            except subprocess.TimeoutExpired:
                restore_process.kill()
                dump_process.kill()
                return {'documents_copied': 0, 'method': 'timeout'}

            if restore_process.returncode != 0:
                console.print(f"[red]pg_restore failed: {restore_stderr.decode()}[/red]")
                return {'documents_copied': 0, 'method': 'error'}

        except Exception as e:
            console.print(f"[red]Copy failed: {e}[/red]")
            return {'documents_copied': 0, 'method': 'error'}

        return {
            'documents_copied': 1, # placeholder since counting rows is separate
            'method': 'pg_dump',
        }

    @property
    def table_term(self) -> str:
        return "table"

    @property
    def table_term_plural(self) -> str:
        return "tables"

    @property
    def scheme(self) -> str:
        return "postgres"
