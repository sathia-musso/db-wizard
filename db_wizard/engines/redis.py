from __future__ import annotations
"""
Redis engine implementation.
Uses redis-cli exclusively.
"""

import os
import shutil
import subprocess
from typing import Any, Self
from urllib.parse import urlparse, unquote

from rich.console import Console

from ..engine import DatabaseEngine
from ..constants import DEFAULT_CONNECTION_TIMEOUT

console = Console()

DEFAULT_REDIS_PORT = 6379

def parse_redis_uri(uri: str) -> dict[str, Any]:
    parsed = urlparse(uri)
    return {
        'host': parsed.hostname or 'localhost',
        'port': parsed.port or DEFAULT_REDIS_PORT,
        'password': unquote(parsed.password or ''),
        'database': parsed.path.lstrip('/') if parsed.path else '0',
    }

def _build_redis_args(params: dict[str, Any], include_db: bool = True) -> list[str]:
    args = [
        '-h', params['host'],
        '-p', str(params['port']),
    ]
    if params['password']:
        args.extend(['-a', params['password']])
    if include_db and params['database']:
        args.extend(['-n', params['database']])
    return args

class RedisEngine(DatabaseEngine):
    """
    Redis engine using redis-cli.
    Note: Redis uses logical databases (0-15) and doesn't have tables.
    We treat 'tables' as key patterns (e.g. '*') or omit them.
    """
    def __init__(self, uri: str):
        super().__init__(uri)
        self.params = parse_redis_uri(uri)

    def connect(self, timeout: int = DEFAULT_CONNECTION_TIMEOUT) -> Self:
        cmd = ['redis-cli'] + _build_redis_args(self.params, include_db=False) + ['PING']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout // 1000 + 5)
        if result.returncode != 0 or 'PONG' not in result.stdout:
            raise ConnectionError(f"Redis connection failed: {result.stderr.strip() or result.stdout.strip()}")
        return self

    def close(self) -> None:
        pass

    def test_connection(self, timeout: int = DEFAULT_CONNECTION_TIMEOUT) -> tuple[bool, str]:
        try:
            self.connect(timeout=timeout)
            return True, "OK (Redis)"
        except Exception as e:
            return False, str(e)

    def list_databases(self) -> list[dict[str, Any]]:
        cmd = ['redis-cli'] + _build_redis_args(self.params, include_db=False) + ['INFO', 'keyspace']
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return []

        databases = []
        for line in result.stdout.strip().split('\n'):
            if line.startswith('db'):
                parts = line.split(':')
                db_name = parts[0][2:]  # e.g., db0 -> 0
                keys_info = parts[1].split(',')
                keys = 0
                for info in keys_info:
                    if info.startswith('keys='):
                        keys = int(info.split('=')[1])
                databases.append({
                    'name': db_name,
                    'tables_count': keys, # Using keys as tables_count for display
                    'size_mb': 0.0,
                })
        return databases

    def list_tables(self, database: str) -> list[dict[str, Any]]:
        # Redis doesn't have tables. We can just return a dummy table or scan key types.
        # Let's just return one "table" representing all keys.
        params = self.params.copy()
        params['database'] = database
        cmd = ['redis-cli'] + _build_redis_args(params) + ['DBSIZE']
        result = subprocess.run(cmd, capture_output=True, text=True)
        keys = 0
        if result.returncode == 0:
            try:
                keys = int(result.stdout.strip())
            except ValueError:
                pass

        return [{
            'name': '*',
            'rows': keys,
            'indexes': 0,
        }]

    def count_rows(self, database: str, table: str) -> int:
        params = self.params.copy()
        params['database'] = database
        if table and table != '*':
            # If a specific pattern is passed
            return 0  # not efficient to count exact pattern without SCAN
        cmd = ['redis-cli'] + _build_redis_args(params) + ['DBSIZE']
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            try:
                return int(result.stdout.strip())
            except ValueError:
                return 0
        return 0

    def _redis_run(self, base_args: list[str], *redis_cmd: str) -> str:
        """Run a redis-cli command and return output, handling binary data safely."""
        cmd = ['redis-cli'] + base_args + list(redis_cmd)
        # Use bytes mode to handle binary values stored in Redis
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            return ""
        return result.stdout.decode('utf-8', errors='replace').strip()

    def sample_rows(self, database: str, table: str, limit: int = 5) -> tuple[list[str], list[list[str]]]:
        """Sample random keys from Redis with their type and value preview."""
        params = self.params.copy()
        params['database'] = database
        base_args = _build_redis_args(params)

        # Commands to preview each Redis data type
        preview_cmds = {
            'string': lambda k: self._redis_run(base_args, 'GET', k),
            'list':   lambda k: self._redis_run(base_args, 'LRANGE', k, '0', '4'),
            'set':    lambda k: self._redis_run(base_args, 'SRANDMEMBER', k, '5'),
            'hash':   lambda k: self._redis_run(base_args, 'HGETALL', k),
            'zset':   lambda k: self._redis_run(base_args, 'ZRANGE', k, '0', '4', 'WITHSCORES'),
        }

        rows = []
        for _ in range(limit):
            key = self._redis_run(base_args, 'RANDOMKEY')
            if not key or key == '(nil)':
                break

            key_type = self._redis_run(base_args, 'TYPE', key) or "?"

            # Get value preview, truncate to 100 chars
            value = preview_cmds.get(key_type, lambda k: "")(key)
            if len(value) > 100:
                value = value[:97] + "..."

            rows.append([key, key_type, value])

        if not rows:
            rows = [["(empty database)", "", ""]]

        return ["Key", "Type", "Value"], rows

    def check_tools(self) -> dict[str, bool]:
        return {
            'redis-cli': shutil.which('redis-cli') is not None,
        }

    def dump(self, database: str, tables: list[str] | None, output_path: str) -> bool:
        """Dump Redis database using redis-cli --rdb.
        Downloads the RDB snapshot from the server to output_path/dump.rdb."""
        os.makedirs(output_path, exist_ok=True)
        rdb_path = os.path.join(output_path, 'dump.rdb')

        base_args = _build_redis_args(self.params, include_db=False)
        cmd = ['redis-cli'] + base_args + ['--rdb', rdb_path]

        console.print(f"[dim]Downloading RDB snapshot...[/dim]")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            console.print(f"[red]redis-cli --rdb failed: {error}[/red]")
            return False

        if not os.path.exists(rdb_path) or os.path.getsize(rdb_path) == 0:
            console.print("[red]RDB file is empty or missing[/red]")
            return False

        console.print(f"[green]✓ RDB snapshot saved ({os.path.getsize(rdb_path)} bytes)[/green]")
        return True

    def restore(self, input_path: str, target_database: str, drop_target: bool = False) -> bool:
        """Restore Redis from RDB file.
        Note: RDB restore requires stopping the Redis server and replacing its dump.rdb,
        which can't be done safely via CLI alone. We use the RESTORE command for individual
        keys instead, but that requires a different approach.
        For now, we guide the user on manual restore."""
        console.print("[yellow]Redis RDB restore requires server-side access.[/yellow]")
        console.print("[dim]To restore manually:[/dim]")
        console.print(f"[dim]  1. Stop Redis server[/dim]")
        console.print(f"[dim]  2. Copy {input_path}/dump.rdb to your Redis data directory[/dim]")
        console.print(f"[dim]  3. Restart Redis server[/dim]")
        console.print("[dim]The RDB file contains all databases, not just the selected one.[/dim]")
        return False

    def copy(self, source_engine: 'DatabaseEngine', source_db: str, source_table: str | None, target_db: str, target_table: str | None, drop_target: bool = False, force: bool = False, **kwargs) -> dict[str, Any]:
        # Redis is a schemaless key-value store: there is no schema to copy.
        if kwargs.get('schema_only', False):
            raise ValueError(
                "Redis is schemaless (key-value) - --schema-only is not supported."
            )
        # Keys have no ordering to resume from: there is no watermark concept.
        if kwargs.get('incremental', False):
            raise ValueError(
                "--incremental is not supported for Redis: keys are unordered, "
                "so there is no watermark to resume from."
            )
        console.print("[red]Redis cross-copy currently not implemented fully.[/red]")
        return {'documents_copied': 0, 'method': 'unsupported'}

    @property
    def table_term(self) -> str:
        return "pattern"

    @property
    def table_term_plural(self) -> str:
        return "patterns"

    @property
    def scheme(self) -> str:
        return "redis"
