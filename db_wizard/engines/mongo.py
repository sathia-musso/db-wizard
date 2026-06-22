from __future__ import annotations
"""
MongoDB engine implementation.
ALL pymongo code lives here and nowhere else.
"""

import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from typing import Any, Self

from pymongo import MongoClient
from pymongo.errors import BulkWriteError
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Confirm

from ..engine import DatabaseEngine
from ..formatting import format_number
from ..constants import (
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_BATCH_SIZE,
    DEFAULT_VERIFICATION_SAMPLE_SIZE,
    CHECKSUM_THRESHOLD,
    MONGO_SYSTEM_DATABASES,
    PIPE_TIMEOUT,
)

console = Console()


class MongoEngine(DatabaseEngine):
    """
    MongoDB engine implementation.
    Wraps pymongo and mongodump/mongorestore CLI tools.
    """

    def __init__(self, uri: str):
        super().__init__(uri)
        self.client = None

    # -- Connection lifecycle --

    def connect(self, timeout: int = DEFAULT_CONNECTION_TIMEOUT) -> Self:
        """Connect to MongoDB and verify with ping."""
        self.client = MongoClient(self.uri, serverSelectionTimeoutMS=timeout)
        self.client.admin.command('ping')
        return self

    def close(self) -> None:
        """Close MongoDB connection."""
        if self.client:
            self.client.close()
            self.client = None

    def test_connection(self, timeout: int = DEFAULT_CONNECTION_TIMEOUT) -> tuple[bool, str]:
        """Quick connectivity check."""
        try:
            client = MongoClient(self.uri, serverSelectionTimeoutMS=timeout)
            client.admin.command('ping')
            db_count = len(client.list_database_names())
            client.close()
            return True, f"OK ({db_count} databases)"
        except Exception as e:  # Catching Exception because PyMongo errors might not be imported if pymongo is missing
            return False, str(e)

    # -- Introspection --

    def list_databases(self) -> list[dict[str, Any]]:
        """List non-system databases with stats."""
        databases = []
        for db_name in self.client.list_database_names():
            if db_name in MONGO_SYSTEM_DATABASES:
                continue
            db = self.client[db_name]
            collections = db.list_collection_names()
            stats = db.command("dbStats")
            size_mb = stats.get('dataSize', 0) / (1024 * 1024)
            databases.append({
                'name': db_name,
                'tables_count': len(collections),
                'size_mb': size_mb,
            })
        return databases

    def list_tables(self, database: str) -> list[dict[str, Any]]:
        """List collections in a database with doc counts and index counts."""
        db = self.client[database]
        tables = []
        for coll_name in db.list_collection_names():
            coll = db[coll_name]
            count = coll.estimated_document_count()
            indexes = list(coll.list_indexes())
            tables.append({
                'name': coll_name,
                'rows': count,
                'indexes': len(indexes),
            })
        return tables

    def count_rows(self, database: str, table: str) -> int:
        """Approximate document count."""
        return self.client[database][table].estimated_document_count()

    # -- Tools --

    def check_tools(self) -> dict[str, bool]:
        """Check if MongoDB CLI tools are available."""
        return {
            'mongodump': shutil.which('mongodump') is not None,
            'mongorestore': shutil.which('mongorestore') is not None,
            'mongosh': shutil.which('mongosh') is not None,
        }

    def sample_rows(self, database: str, table: str, limit: int = 5) -> tuple[list[str], list[list[str]]]:
        """Fetch sample documents. Returns (keys, rows) for table display,
        but for MongoDB the browse_database uses JSON display instead."""
        docs = list(self.client[database][table].find().limit(limit))
        if not docs:
            return [], []
        # Collect all keys across all docs
        keys = []
        for doc in docs:
            for k in doc.keys():
                if k not in keys:
                    keys.append(k)
        rows = []
        for doc in docs:
            rows.append([str(doc.get(k, '')) for k in keys])
        return keys, rows

    # -- Core operations --

    def dump(
        self,
        database: str,
        tables: list[str] | None,
        output_path: str
    ) -> bool:
        """Dump database using mongodump."""
        if not shutil.which('mongodump'):
            return False

        cmd = ['mongodump', '--uri', self.uri, '--db', database, '--out', output_path]

        if tables:
            # Dump specific collections one by one
            for table in tables:
                table_cmd = cmd + ['--collection', table]
                result = subprocess.run(table_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    console.print(f"[red]mongodump error for {table}: {result.stderr}[/red]")
                    return False
        else:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                console.print(f"[red]mongodump error: {result.stderr}[/red]")
                return False

        return True

    def restore(
        self,
        input_path: str,
        target_database: str,
        drop_target: bool = False
    ) -> bool:
        """Restore database using mongorestore."""
        if not shutil.which('mongorestore'):
            return False

        cmd = ['mongorestore', '--uri', self.uri]
        if drop_target:
            cmd.append('--drop')

        # Detect source database from the dump directory
        import os
        source_db = None
        if os.path.isdir(input_path):
            dirs = [d for d in os.listdir(input_path) if os.path.isdir(os.path.join(input_path, d))]
            if dirs:
                source_db = dirs[0]

        if source_db and source_db != target_database:
            cmd.extend([
                '--nsFrom', f'{source_db}.*',
                '--nsTo', f'{target_database}.*'
            ])

        cmd.append(input_path)

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            console.print(f"[red]mongorestore error: {result.stderr}[/red]")
            return False
        return True

    def copy(
        self,
        source_engine: 'MongoEngine',
        source_db: str,
        source_table: str | None,
        target_db: str,
        target_table: str | None,
        drop_target: bool = False,
        force: bool = False,
        force_python: bool = False,
        **kwargs: Any
    ) -> dict[str, Any]:
        """
        Copy from source MongoEngine into this engine (target).
        Prefers mongodump|mongorestore pipe, falls back to Python copy.
        """
        # MongoDB is schemaless: there is no "structure without data" to copy.
        # Refuse rather than silently copying everything (which would be the
        # exact opposite of what --schema-only asks for).
        if kwargs.get('schema_only', False):
            raise ValueError(
                "MongoDB is schemaless - --schema-only is not supported. "
                "There is no fixed schema to copy."
            )

        if source_table:
            # Single collection copy
            return self._copy_collection(
                source_engine, source_db, source_table,
                target_db, target_table or source_table,
                drop_target=drop_target, force=force, force_python=force_python
            )
        else:
            # Entire database copy
            return self._copy_database(
                source_engine, source_db, target_db,
                drop_target=drop_target, force=force, force_python=force_python
            )

    # -- UI terminology --

    @property
    def table_term(self) -> str:
        return "collection"

    @property
    def table_term_plural(self) -> str:
        return "collections"

    @property
    def scheme(self) -> str:
        return "mongodb"

    # =========================================================================
    # MongoDB-specific methods (not in the ABC)
    # =========================================================================

    def verify_copy(
        self,
        source_db: str,
        source_coll: str,
        target_db: str,
        target_coll: str,
        target_engine: 'MongoEngine | None' = None,
        sample_size: int = DEFAULT_VERIFICATION_SAMPLE_SIZE
    ) -> dict[str, Any]:
        """Verify that copy was successful (MongoDB-specific)."""
        # Source is self, target might be a different engine
        target_client = target_engine.client if target_engine else self.client

        source_collection = self.client[source_db][source_coll]
        target_collection = target_client[target_db][target_coll]

        console.print("[cyan]🔍 Verifying copy...[/cyan]")

        source_count = source_collection.estimated_document_count()
        target_count = target_collection.estimated_document_count()

        source_indexes = list(source_collection.list_indexes())
        target_indexes = list(target_collection.list_indexes())

        # Sample comparison
        sample_errors = []
        for doc in source_collection.aggregate([{'$sample': {'size': sample_size}}]):
            target_doc = target_collection.find_one({'_id': doc['_id']})
            if not target_doc:
                sample_errors.append(f"Missing document: {doc['_id']}")
            elif doc != target_doc:
                sample_errors.append(f"Document mismatch: {doc['_id']}")

        # Checksums for small collections
        checksum_match = None
        if source_count < CHECKSUM_THRESHOLD:
            source_checksum = self._calculate_checksum(source_collection)
            target_checksum = self._calculate_checksum(target_collection)
            checksum_match = source_checksum == target_checksum

        return {
            'source_count': source_count,
            'target_count': target_count,
            'count_match': source_count == target_count,
            'source_indexes': len(source_indexes),
            'target_indexes': len(target_indexes),
            'index_match': len(source_indexes) == len(target_indexes),
            'sample_errors': sample_errors,
            'sample_match': len(sample_errors) == 0,
            'checksum_match': checksum_match,
        }

    def backup_before_copy(self, target_db: str, target_table: str | None = None) -> str:
        """Create backup of target before copying (MongoDB-specific)."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if target_table:
            backup_name = f"{target_table}_backup_{timestamp}"
            source_coll = self.client[target_db][target_table]
            backup_coll = self.client[target_db][backup_name]

            console.print(f"[cyan]💾 Creating backup: {backup_name}[/cyan]")

            # Batched copy to avoid OOM
            batch = []
            for doc in source_coll.find():
                batch.append(doc)
                if len(batch) >= DEFAULT_BATCH_SIZE:
                    backup_coll.insert_many(batch, ordered=False)
                    batch = []
            if batch:
                backup_coll.insert_many(batch, ordered=False)

            # Copy indexes
            for index in source_coll.list_indexes():
                if index['name'] != '_id_':
                    keys = index['key']
                    options = {k: v for k, v in index.items() if k not in ['key', 'v', 'ns']}
                    backup_coll.create_index(list(keys.items()), **options)

            console.print(f"[green]✅ Backup created: {backup_name}[/green]")
            return backup_name
        else:
            if shutil.which('mongodump'):
                backup_name = f"{target_db}_backup_{timestamp}"
                cmd = ['mongodump', '--uri', self.uri, '--db', target_db,
                       '--archive', f'{backup_name}.archive']
                subprocess.run(cmd, check=True)
                console.print(f"[green]✅ Database backup created: {backup_name}.archive[/green]")
                return f"{backup_name}.archive"

        return ""

    @staticmethod
    def build_connection_uri(
        host: str, port: int = 27017,
        username: str | None = None, password: str | None = None,
        auth_db: str | None = None
    ) -> str:
        """Build a MongoDB connection URI from components."""
        auth = ""
        if username and password:
            auth = f"{username}:{password}@"
        uri = f"mongodb://{auth}{host}:{port}/"
        if auth_db:
            uri += auth_db
        return uri

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _copy_collection(
        self,
        source_engine: 'MongoEngine',
        source_db: str,
        source_coll: str,
        target_db: str,
        target_coll: str,
        drop_target: bool = False,
        force: bool = False,
        force_python: bool = False
    ) -> dict[str, Any]:
        """Copy a single collection from source to target."""
        source_collection = source_engine.client[source_db][source_coll]
        target_collection = self.client[target_db][target_coll]

        total_docs = source_collection.estimated_document_count()
        console.print(f"[green]📊 Collection has ~{format_number(total_docs)} documents[/green]")

        # Prefer mongodump/mongorestore
        if not force_python and shutil.which('mongodump') and shutil.which('mongorestore'):
            console.print("[cyan]🚀 Using mongodump/mongorestore (fast native mode)...[/cyan]")

            if drop_target and target_collection.estimated_document_count() > 0:
                if force or Confirm.ask(f"[yellow]Drop target {target_db}.{target_coll}?[/yellow]"):
                    target_collection.drop()
                    console.print("[yellow]🗑️  Dropped target collection[/yellow]")

            success = self._pipe_mongodump(
                source_engine.uri, source_db, source_coll,
                target_db, target_coll
            )

            if success:
                copied_count = self.client[target_db][target_coll].estimated_document_count()
                console.print(f"[green]✅ Copied {format_number(copied_count)} documents using mongodump[/green]")

                target_indexes = list(self.client[target_db][target_coll].list_indexes())
                indexes_count = len([idx for idx in target_indexes if idx['name'] != '_id_'])

                return {
                    'documents_copied': copied_count,
                    'indexes_created': indexes_count,
                    'source_count': total_docs,
                    'method': 'mongodump',
                }
            else:
                console.print("[yellow]⚠ mongodump failed, falling back to Python copy...[/yellow]")

        # Python fallback
        return self._copy_python(
            source_engine, source_db, source_coll,
            target_db, target_coll,
            total_docs=total_docs, drop_target=drop_target, force=force,
            force_python=force_python
        )

    def _copy_database(
        self,
        source_engine: 'MongoEngine',
        source_db: str,
        target_db: str,
        drop_target: bool = False,
        force: bool = False,
        force_python: bool = False,
        exclude_prefixes: list[str] | None = None
    ) -> dict[str, Any]:
        """Copy entire database from source to target."""
        exclude = exclude_prefixes or ['system.']
        source_database = source_engine.client[source_db]

        collections = source_database.list_collection_names()
        collections = [c for c in collections if not any(c.startswith(ex) for ex in exclude)]

        console.print(f"[green]📚 Found {len(collections)} collections to copy[/green]")

        if drop_target:
            if force or Confirm.ask(f"[red]⚠️  Drop entire target database {target_db}?[/red]"):
                self.client.drop_database(target_db)
                console.print("[yellow]🗑️  Dropped target database[/yellow]")

        results = {}
        for coll_name in collections:
            console.print(f"\n[bold]📁 Copying collection: {coll_name}[/bold]")
            result = self._copy_collection(
                source_engine, source_db, coll_name,
                target_db, coll_name,
                drop_target=False,  # Already handled at DB level
                force=force,
                force_python=force_python
            )
            results[coll_name] = result

        return results

    def _pipe_mongodump(
        self,
        source_uri: str,
        source_db: str,
        source_coll: str,
        target_db: str,
        target_coll: str,
        drop_target: bool = False
    ) -> bool:
        """Pipe mongodump stdout into mongorestore stdin."""
        if not shutil.which('mongodump') or not shutil.which('mongorestore'):
            return False

        try:
            dump_cmd = [
                'mongodump', '--uri', source_uri,
                '--db', source_db, '--collection', source_coll,
                '--archive'
            ]
            restore_cmd = [
                'mongorestore', '--uri', self.uri, '--archive',
                '--nsFrom', f'{source_db}.{source_coll}',
                '--nsTo', f'{target_db}.{target_coll}'
            ]
            if drop_target:
                restore_cmd.append('--drop')

            dump_process = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            restore_process = subprocess.Popen(restore_cmd, stdin=dump_process.stdout, stderr=subprocess.PIPE)
            dump_process.stdout.close()

            try:
                _, restore_stderr = restore_process.communicate(timeout=PIPE_TIMEOUT)
                dump_stderr = dump_process.stderr.read()
            except subprocess.TimeoutExpired:
                restore_process.kill()
                dump_process.kill()
                console.print(f"[red]mongodump/restore timed out after {PIPE_TIMEOUT // 60} minutes[/red]")
                return False

            if restore_process.returncode != 0:
                error_msg = (restore_stderr or dump_stderr or b'').decode('utf-8', errors='replace').strip()
                if error_msg:
                    console.print(f"[yellow]⚠ mongodump/restore stderr: {error_msg}[/yellow]")
                return False

            return True

        except Exception as e:
            console.print(f"[yellow]⚠ mongodump/restore failed: {e}[/yellow]")
            return False

    def _copy_python(
        self,
        source_engine: 'MongoEngine',
        source_db: str,
        source_coll: str,
        target_db: str,
        target_coll: str,
        total_docs: int = 0,
        drop_target: bool = False,
        force: bool = False,
        force_python: bool = False
    ) -> dict[str, Any]:
        """Python fallback copy using pymongo batch inserts."""
        source_collection = source_engine.client[source_db][source_coll]
        target_collection = self.client[target_db][target_coll]

        if force_python:
            console.print("[cyan]📝 Using Python copy mode (--force-python flag set)...[/cyan]")
        else:
            console.print("[yellow]⚠ MongoDB tools not available, using Python copy (slower)...[/yellow]")
            console.print("[dim]Install with: brew install mongodb-database-tools[/dim]")

        # Drop target if requested
        if drop_target and target_collection.estimated_document_count() > 0:
            if force or Confirm.ask(f"[yellow]Drop target {target_db}.{target_coll}?[/yellow]"):
                target_collection.drop()
                console.print("[yellow]🗑️  Dropped target collection[/yellow]")

        # Copy indexes
        console.print("[cyan]📐 Copying indexes...[/cyan]")
        indexes_created = self._copy_indexes(source_engine, source_db, source_coll, target_db, target_coll)

        # Copy documents
        console.print("[cyan]📄 Copying documents...[/cyan]")
        copied = 0
        batch = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        ) as progress:
            task = progress.add_task("Copying...", total=total_docs)

            for doc in source_collection.find():
                batch.append(doc)

                if len(batch) >= DEFAULT_BATCH_SIZE:
                    try:
                        target_collection.insert_many(batch, ordered=False)
                        copied += len(batch)
                    except BulkWriteError as bwe:
                        inserted = bwe.details.get('nInserted', 0)
                        copied += inserted
                        n_errors = len(bwe.details.get('writeErrors', []))
                        console.print(f"  [yellow]⚠[/yellow] Batch: {inserted} inserted, {n_errors} duplicates skipped")
                    progress.update(task, advance=len(batch))
                    batch = []

            if batch:
                try:
                    target_collection.insert_many(batch, ordered=False)
                    copied += len(batch)
                except BulkWriteError as bwe:
                    inserted = bwe.details.get('nInserted', 0)
                    copied += inserted
                    n_errors = len(bwe.details.get('writeErrors', []))
                    console.print(f"  [yellow]⚠[/yellow] Batch: {inserted} inserted, {n_errors} duplicates skipped")
                progress.update(task, advance=len(batch))

        return {
            'documents_copied': copied,
            'indexes_created': indexes_created,
            'source_count': total_docs,
            'method': 'python',
        }

    def _copy_indexes(
        self,
        source_engine: 'MongoEngine',
        source_db: str,
        source_coll: str,
        target_db: str,
        target_coll: str
    ) -> int:
        """Copy indexes from source to target collection."""
        source_collection = source_engine.client[source_db][source_coll]
        target_collection = self.client[target_db][target_coll]

        indexes = list(source_collection.list_indexes())
        created = 0

        for index in indexes:
            if index['name'] == '_id_':
                continue
            try:
                keys = index['key']
                options = {k: v for k, v in index.items() if k not in ['key', 'v', 'ns']}
                target_collection.create_index(list(keys.items()), **options)
                created += 1
                console.print(f"  [green]✓[/green] Created index: {index['name']}")
            except Exception as e:
                console.print(f"  [yellow]⚠[/yellow] Failed to create index {index['name']}: {e}")

        return created

    def _calculate_checksum(self, collection) -> str:
        """Calculate SHA256 checksum for collection data."""
        hasher = hashlib.sha256()
        for doc in collection.find().sort('_id', 1):
            doc_str = json.dumps(doc, sort_keys=True, default=str)
            hasher.update(doc_str.encode())
        return hasher.hexdigest()
