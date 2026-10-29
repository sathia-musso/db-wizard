from __future__ import annotations
"""
DatabaseEngine ABC and EngineFactory

The engine abstraction is what allows db-wizard to support multiple
database systems (MongoDB, MySQL, etc.) through a single interface.
The wizard, CLI, backup manager, and task runner are all engine-agnostic:
they call these methods and don't care what's underneath.
"""

from abc import ABC, abstractmethod
from typing import Any, Self
from urllib.parse import urlparse


class DatabaseEngine(ABC):
    """
    Abstract database engine.
    Each engine wraps a connection to a database server via URI.
    """

    def __init__(self, uri: str):
        self.uri = uri

    # -- Connection lifecycle --

    @abstractmethod
    def connect(self, timeout: int = 5000) -> Self:
        """Connect to the server. Returns self for chaining. Raises on failure."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the connection."""
        ...

    @abstractmethod
    def test_connection(self, timeout: int = 5000) -> tuple[bool, str]:
        """Quick connectivity check. Returns (success, message)."""
        ...

    # -- Introspection --

    @abstractmethod
    def list_databases(self) -> list[dict[str, Any]]:
        """
        List databases on the server.
        Returns list of dicts: {'name': str, 'tables_count': int, 'size_mb': float}
        Excludes system databases.
        """
        ...

    @abstractmethod
    def list_tables(self, database: str) -> list[dict[str, Any]]:
        """
        List tables/collections in a database.
        Returns list of dicts: {'name': str, 'rows': int, 'indexes': int}
        """
        ...

    @abstractmethod
    def count_rows(self, database: str, table: str) -> int:
        """Approximate row/document count for a table/collection."""
        ...

    # -- Incremental copy support --
    #
    # Incremental copy is NOT a diff: it never detects UPDATEs or DELETEs.
    # It answers two questions and copies whatever sits above the answer:
    #   1. which column grows monotonically?   -> detect_watermark_column()
    #   2. how far did the target already get? -> max_watermark()
    # Engines that cannot filter rows by such a column leave the defaults
    # below and the copy layer refuses the flag with a clear message.

    supports_incremental: bool = False

    # Whether a task's table list may contain glob patterns ("compliance_*")
    # that are expanded against list_tables() at run time. True for engines
    # with real, listable tables. Redis turns it off: its "tables" already
    # ARE key patterns handed to SCAN, and expanding them against the single
    # dummy '*' entry its list_tables() returns would break every task.
    supports_table_patterns: bool = True

    def detect_watermark_column(self, database: str, table: str) -> str | None:
        """
        Return the name of a monotonically increasing column usable as a
        watermark (AUTO_INCREMENT primary key, ObjectId field, ...), or None
        when the table has nothing suitable.
        """
        return None

    def max_watermark(self, database: str, table: str, column: str) -> Any | None:
        """
        Highest value of `column` in `table`, or None when the table does not
        exist or is empty. Both cases mean the same thing to the caller:
        there is no watermark to resume from, so copy everything.
        """
        return None

    # -- Tools --

    @abstractmethod
    def sample_rows(self, database: str, table: str, limit: int = 5) -> tuple[list[str], list[list[str]]]:
        """
        Fetch sample rows from a table/collection.
        Returns (column_names, rows) where rows is a list of lists of strings.
        """
        ...

    @abstractmethod
    def check_tools(self) -> dict[str, bool]:
        """
        Check if required CLI tools are available.
        Returns dict like {'mongodump': True, 'mongorestore': False}
        or {'mysqldump': True, 'mysql': True}
        """
        ...

    # -- Core operations --

    @abstractmethod
    def dump(
        self,
        database: str,
        tables: list[str] | None,
        output_path: str
    ) -> bool:
        """
        Dump database (or specific tables) to output_path directory.
        Returns True on success.
        """
        ...

    @abstractmethod
    def restore(
        self,
        input_path: str,
        target_database: str,
        drop_target: bool = False
    ) -> bool:
        """
        Restore from input_path into target_database.
        Returns True on success.
        """
        ...

    @abstractmethod
    def copy(
        self,
        source_engine: 'DatabaseEngine',
        source_db: str,
        source_table: str | None,
        target_db: str,
        target_table: str | None,
        drop_target: bool = False,
        force: bool = False,
        **kwargs: Any
    ) -> dict[str, Any]:
        """
        Copy from source_engine into this engine (the target).
        This is the main copy pathway:
        - Mongo: mongodump --archive | mongorestore --archive (or Python fallback)
        - MySQL: mysqldump ... | mysql ...

        Recognised kwargs:
            schema_only: bool  -- copy structure/DDL only, no rows
            incremental: bool  -- copy only rows above the target's watermark
            since_column: str  -- watermark column override for incremental

        Returns result dict with at least:
            'documents_copied': int, 'method': str
        """
        ...

    # -- UI terminology --

    @property
    @abstractmethod
    def table_term(self) -> str:
        """Singular term: 'collection' for MongoDB, 'table' for MySQL."""
        ...

    @property
    @abstractmethod
    def table_term_plural(self) -> str:
        """Plural term: 'collections' for MongoDB, 'tables' for MySQL."""
        ...

    @property
    @abstractmethod
    def scheme(self) -> str:
        """URI scheme: 'mongodb' or 'mysql'."""
        ...


class EngineFactory:
    """Pick the right engine from URI scheme."""

    @staticmethod
    def create(uri: str) -> DatabaseEngine:
        """
        Create a DatabaseEngine from a URI string.
        Auto-detects engine type from the scheme.

        Supported schemes:
            mongodb://, mongodb+srv:// -> MongoEngine
            mysql:// -> MySQLEngine
            postgres://, postgresql:// -> PostgresEngine
            redis:// -> RedisEngine
        """
        parsed = urlparse(uri)
        scheme = parsed.scheme.lower()

        if scheme.startswith('mongodb'):
            from .engines.mongo import MongoEngine
            return MongoEngine(uri)

        elif scheme == 'mysql':
            from .engines.mysql import MySQLEngine
            return MySQLEngine(uri)

        elif scheme in ('postgres', 'postgresql'):
            from .engines.postgres import PostgresEngine
            return PostgresEngine(uri)

        elif scheme == 'redis':
            from .engines.redis import RedisEngine
            return RedisEngine(uri)

        else:
            raise ValueError(
                f"Unsupported database URI scheme: '{scheme}'. "
                f"Supported: mongodb://, mongodb+srv://, mysql://, postgres://, redis://"
            )

    @staticmethod
    def detect_scheme(uri: str) -> str:
        """Return engine type string from URI."""
        parsed = urlparse(uri)
        if parsed.scheme.startswith('mongodb'):
            return 'mongodb'
        elif parsed.scheme == 'mysql':
            return 'mysql'
        elif parsed.scheme in ('postgres', 'postgresql'):
            return 'postgres'
        elif parsed.scheme == 'redis':
            return 'redis'
        raise ValueError(f"Unknown database scheme: {parsed.scheme}")

    @staticmethod
    def check_same_engine(source: 'DatabaseEngine', target: 'DatabaseEngine') -> None:
        """Raise ValueError if source and target are different engine types.
        Cross-engine copy (e.g. MySQL -> MongoDB) is not supported."""
        if source.scheme != target.scheme:
            raise ValueError(
                f"Cross-engine copy not supported: "
                f"source is {source.scheme}, target is {target.scheme}. "
                f"Both must be the same database type."
            )
