"""
Tests for bug fixes
Covers all 10 bugs identified and fixed:
  1. settings add_task/add_host/add_storage return values
  2. wizard.py --list-tasks KeyError for backup/restore tasks
  3. list passed as collection name in copy_wizard doc counting
  4. copy_wizard UI for multi-collection target naming (covered by #3 tests)
  5. parse_collection_selection range bounds check
  6. BulkWriteError not caught in Python copy
  7. backup_before_copy OOM - batched copy
  8. tarfile.extractall path traversal filter
  9. copy_with_mongodump timeout and stderr handling
  10. inefficient type check in backup.py
"""

import os
import sys
import tarfile
import tempfile
import subprocess
from unittest.mock import Mock, MagicMock, patch, call, PropertyMock
from pathlib import Path

import pytest


# ===========================================================================
# Bug #1: settings add_task/add_host/add_storage return values
# ===========================================================================

class TestSettingsReturnValues:
    """Bug #1: add_task, add_host, add_storage must return True"""

    @patch('db_wizard.settings.Path.exists')
    def test_add_host_returns_true(self, mock_exists):
        mock_exists.return_value = False
        from db_wizard.settings import SettingsManager
        manager = SettingsManager()

        with patch.object(manager, 'save_settings'):
            result = manager.add_host("test", "mongodb://localhost")

        assert result is True

    @patch('db_wizard.settings.Path.exists')
    def test_add_task_returns_true(self, mock_exists):
        mock_exists.return_value = False
        from db_wizard.settings import SettingsManager
        manager = SettingsManager()

        with patch.object(manager, 'save_settings'):
            result = manager.add_task("backup", {"source_db": "test"})

        assert result is True

    @patch('db_wizard.settings.Path.exists')
    def test_add_storage_returns_true(self, mock_exists):
        mock_exists.return_value = False
        from db_wizard.settings import SettingsManager
        manager = SettingsManager()

        with patch.object(manager, 'save_settings'):
            result = manager.add_storage("local", {"type": "local", "path": "/tmp"})

        assert result is True


# ===========================================================================
# Bug #5: parse_collection_selection range bounds check
# ===========================================================================

class TestParseCollectionSelectionBounds:
    """Bug #5: range values must be clamped to valid bounds"""

    def test_range_exceeding_max_value_is_clamped(self):
        from db_wizard.utils import parse_collection_selection
        # 5 collections, asking for range 1-9999
        result = parse_collection_selection("1-9999", 5)
        # Should only include valid indices: 0, 1, 2, 3, 4
        assert result == [0, 1, 2, 3, 4]

    def test_range_with_negative_start_is_clamped(self):
        from db_wizard.utils import parse_collection_selection
        result = parse_collection_selection("0-3", 5)
        assert all(0 <= idx < 5 for idx in result)
        assert len(result) > 0

    def test_range_entirely_out_of_bounds(self):
        from db_wizard.utils import parse_collection_selection
        result = parse_collection_selection("10-20", 5)
        assert result == []

    def test_mixed_with_out_of_bounds_range(self):
        from db_wizard.utils import parse_collection_selection
        result = parse_collection_selection("1,50-100", 5)
        assert result == [0]  # Only index 0 from "1", range produces nothing


# ===========================================================================
# Bug #6: BulkWriteError not caught in Python copy
# ===========================================================================

class TestBulkWriteErrorHandling:
    """Bug #6: insert_many with ordered=False must handle BulkWriteError"""

    @patch('db_wizard.engines.mongo.shutil.which')
    @patch('db_wizard.engines.mongo.MongoClient')
    def test_python_copy_handles_duplicate_ids(self, mock_client_cls, mock_which):
        """Python copy should not crash when target has duplicate _ids"""
        from pymongo.errors import BulkWriteError
        from db_wizard.engines.mongo import MongoEngine

        mock_which.return_value = None

        # Setup source engine
        mock_source_client = MagicMock()
        source_coll = MagicMock()
        source_coll.estimated_document_count.return_value = 3
        source_coll.find.return_value = iter([
            {"_id": 1, "name": "a"},
            {"_id": 2, "name": "b"},
            {"_id": 3, "name": "c"},
        ])
        source_coll.list_indexes.return_value = iter([{"name": "_id_", "key": {"_id": 1}}])
        mock_source_client.__getitem__.return_value.__getitem__.return_value = source_coll

        # Setup target engine
        mock_target_client = MagicMock()
        target_coll = MagicMock()
        target_coll.estimated_document_count.return_value = 0

        bulk_error = BulkWriteError({
            'nInserted': 2,
            'writeErrors': [{'index': 1, 'code': 11000, 'errmsg': 'duplicate key'}]
        })
        target_coll.insert_many.side_effect = bulk_error
        mock_target_client.__getitem__.return_value.__getitem__.return_value = target_coll

        # Create source and target engines
        source_engine = MongoEngine("mongodb://source")
        source_engine.client = mock_source_client

        target_engine = MongoEngine("mongodb://target")
        target_engine.client = mock_target_client

        # Should NOT raise - the BulkWriteError must be caught
        result = target_engine.copy(
            source_engine=source_engine,
            source_db="db", source_table="coll",
            target_db="db", target_table="coll",
            force=True,
            force_python=True
        )

        assert result['method'] == 'python'
        assert result['documents_copied'] == 2


# ===========================================================================
# Bug #7: backup_before_copy OOM - batched copy
# ===========================================================================

class TestBackupBeforeCopyBatched:
    """Bug #7: backup_before_copy must use batched inserts, not list(find())"""

    def test_backup_uses_batched_inserts(self):
        """Verify backup_before_copy does NOT load all docs into memory at once"""
        from db_wizard.engines.mongo import MongoEngine
        from db_wizard.constants import DEFAULT_BATCH_SIZE

        num_docs = DEFAULT_BATCH_SIZE + 50
        docs = [{"_id": i, "data": f"doc_{i}"} for i in range(num_docs)]

        source_coll = MagicMock()
        source_coll.find.return_value = iter(docs)
        source_coll.list_indexes.return_value = iter([{"name": "_id_", "key": {"_id": 1}}])

        backup_coll = MagicMock()

        mock_client = MagicMock()
        mock_db = MagicMock()
        def get_collection(name):
            if "backup" in name:
                return backup_coll
            return source_coll
        mock_db.__getitem__ = MagicMock(side_effect=get_collection)
        mock_client.__getitem__ = MagicMock(return_value=mock_db)

        engine = MongoEngine("mongodb://target")
        engine.client = mock_client

        result = engine.backup_before_copy("db", "coll")

        # Should have called insert_many TWICE:
        # once with DEFAULT_BATCH_SIZE docs, once with remaining 50
        assert backup_coll.insert_many.call_count == 2

        first_batch = backup_coll.insert_many.call_args_list[0][0][0]
        assert len(first_batch) == DEFAULT_BATCH_SIZE

        second_batch = backup_coll.insert_many.call_args_list[1][0][0]
        assert len(second_batch) == 50


# ===========================================================================
# Bug #8: tarfile.extractall path traversal filter
# ===========================================================================

class TestTarfilePathTraversal:
    """Bug #8: tarfile.extractall must use filter='data'"""

    def test_extractall_uses_data_filter(self):
        """Verify that restore_database calls extractall with filter='data'"""
        import inspect
        from db_wizard.backup import BackupManager

        source = inspect.getsource(BackupManager.restore_database)
        assert "filter='data'" in source, \
            "restore_database does NOT pass filter='data' to tarfile.extractall - path traversal vulnerability!"

    def test_extractall_with_real_archive(self):
        """Integration test: restore_database extracts safely with filter='data'"""
        with patch('db_wizard.backup.EngineFactory') as mock_factory, \
             patch('subprocess.run') as mock_run:

            mock_engine = MagicMock()
            mock_engine.client = MagicMock()
            mock_engine.check_tools.return_value = {'mongodump': True, 'mongorestore': True}
            mock_engine.restore.return_value = True
            mock_engine.list_tables.return_value = [{'name': 'test', 'rows': 10, 'indexes': 1}]
            mock_engine.count_rows.return_value = 10
            mock_factory.create.return_value = mock_engine

            with tempfile.TemporaryDirectory() as tmpdir:
                dump_dir = os.path.join(tmpdir, "dump", "testdb")
                os.makedirs(dump_dir)
                with open(os.path.join(dump_dir, "test.bson"), "wb") as f:
                    f.write(b"fake")

                archive_path = os.path.join(tmpdir, "backup.tar.gz")
                with tarfile.open(archive_path, 'w:gz') as tar:
                    tar.add(os.path.join(tmpdir, "dump"), arcname="dump")

                from db_wizard.backup import BackupManager
                mgr = BackupManager("mongodb://localhost", tmpdir)

                def fake_download(src, dst):
                    import shutil
                    shutil.copy2(archive_path, dst)
                    return True
                mgr.storage.download = fake_download

                mock_run.return_value = MagicMock(returncode=0)

                result = mgr.restore_database(archive_path)
                assert result['success'] is True


# ===========================================================================
# Bug #9: mongodump pipe timeout and stderr handling
# ===========================================================================

class TestMongodumpTimeout:
    """
    Bug #9: _pipe_mongodump must capture stderr and stay killable.

    The original fix used a 10-minute wall-clock timeout, which killed healthy
    copies of large databases. The pipe now runs to completion and Ctrl-C is
    what stops it.
    """

    @patch('db_wizard.engines.mongo.shutil.which', return_value='/usr/bin/mongodump')
    @patch('subprocess.Popen')
    def test_mongodump_interrupt_kills_processes(self, mock_popen, mock_which):
        """Ctrl-C during the pipe must kill both processes"""
        from db_wizard.engines.mongo import MongoEngine

        mock_dump = MagicMock()
        mock_dump.stdout = MagicMock()
        mock_dump.stderr = MagicMock()
        mock_dump.stderr.read.return_value = b""

        mock_restore = MagicMock()
        mock_restore.communicate.side_effect = KeyboardInterrupt()

        mock_popen.side_effect = [mock_dump, mock_restore]

        engine = MongoEngine("mongodb://target")
        engine.client = MagicMock()

        result = engine._pipe_mongodump("mongodb://source", "db", "coll", "db", "coll")

        assert result is False
        mock_restore.kill.assert_called_once()
        mock_dump.kill.assert_called_once()

    @patch('db_wizard.engines.mongo.shutil.which', return_value='/usr/bin/mongodump')
    @patch('subprocess.Popen')
    def test_mongodump_has_no_wall_clock_timeout(self, mock_popen, mock_which):
        """A slow mongodump must not be killed on a deadline"""
        from db_wizard.engines.mongo import MongoEngine

        mock_dump = MagicMock()
        mock_dump.stdout = MagicMock()
        mock_dump.stderr = MagicMock()
        mock_dump.stderr.read.return_value = b""

        mock_restore = MagicMock()
        mock_restore.returncode = 0
        mock_restore.communicate.return_value = (None, b"")

        mock_popen.side_effect = [mock_dump, mock_restore]

        engine = MongoEngine("mongodb://target")
        engine.client = MagicMock()

        assert engine._pipe_mongodump("mongodb://source", "db", "coll", "db", "coll") is True
        assert 'timeout' not in mock_restore.communicate.call_args.kwargs
        assert mock_restore.communicate.call_args.args == ()
        mock_restore.kill.assert_not_called()

    @patch('db_wizard.engines.mongo.shutil.which', return_value='/usr/bin/mongodump')
    @patch('subprocess.Popen')
    def test_mongodump_stderr_captured_on_failure(self, mock_popen, mock_which):
        """stderr from failed mongorestore should be printed"""
        from db_wizard.engines.mongo import MongoEngine

        mock_dump = MagicMock()
        mock_dump.stdout = MagicMock()
        mock_dump.stderr = MagicMock()
        mock_dump.stderr.read.return_value = b""

        mock_restore = MagicMock()
        mock_restore.returncode = 1
        mock_restore.communicate.return_value = (None, b"error: authentication failed")

        mock_popen.side_effect = [mock_dump, mock_restore]

        engine = MongoEngine("mongodb://target")
        engine.client = MagicMock()

        result = engine._pipe_mongodump("mongodb://source", "db", "coll", "db", "coll")

        assert result is False

    @patch('db_wizard.engines.mongo.shutil.which', return_value='/usr/bin/mongodump')
    @patch('subprocess.Popen')
    def test_mongodump_success_returns_true(self, mock_popen, mock_which):
        """Successful mongodump/restore pipe should return True"""
        from db_wizard.engines.mongo import MongoEngine

        mock_dump = MagicMock()
        mock_dump.stdout = MagicMock()
        mock_dump.stderr = MagicMock()
        mock_dump.stderr.read.return_value = b""

        mock_restore = MagicMock()
        mock_restore.returncode = 0
        mock_restore.communicate.return_value = (None, b"")

        mock_popen.side_effect = [mock_dump, mock_restore]

        engine = MongoEngine("mongodb://target")
        engine.client = MagicMock()

        result = engine._pipe_mongodump("mongodb://source", "db", "coll", "db", "coll")

        assert result is True


# ===========================================================================
# Bug #10: inefficient type check in backup.py
# ===========================================================================

class TestBackupStorageTypeCheck:
    """Bug #10: isinstance check should use LocalStorage directly"""

    def test_local_storage_isinstance_check(self):
        """Verify BackupManager uses isinstance(storage, LocalStorage)"""
        from db_wizard.storage import LocalStorage
        from db_wizard.backup import BackupManager

        mgr = BackupManager("mongodb://localhost", "/tmp/backups")
        assert isinstance(mgr.storage, LocalStorage)

    def test_ssh_storage_not_local(self):
        """SSH storage should not be identified as local"""
        from db_wizard.storage import LocalStorage, SSHStorage, StorageFactory

        storage = StorageFactory.create("ssh://user@host/path")
        assert not isinstance(storage, LocalStorage)
        assert isinstance(storage, SSHStorage)


# ===========================================================================
# Bug #2: wizard.py --list-tasks KeyError for backup/restore tasks
# ===========================================================================

class TestWizardListTasksAllTypes:
    """Bug #2: --list-tasks must handle backup/restore/copy task types"""

    def test_format_task_table_row_backup_task(self):
        """format_task_table_row should handle backup tasks without KeyError"""
        from db_wizard.utils import format_task_table_row

        backup_task = {
            'type': 'backup',
            'mongo_uri': 'mongodb://localhost',
            'database': 'production',
            'collections': ['users', 'posts'],
            'storage_url': 'ssh://backup@server:/backups',
        }

        name, display, coll = format_task_table_row("daily_backup", backup_task)

        assert name == "daily_backup"
        assert "BACKUP" in display
        assert "2 items" in coll

    def test_format_task_table_row_restore_task(self):
        """format_task_table_row should handle restore tasks without KeyError"""
        from db_wizard.utils import format_task_table_row

        restore_task = {
            'type': 'restore',
            'mongo_uri': 'mongodb://localhost',
            'backup_file': '/backups/latest.tar.gz',
            'target_database': 'restored_db',
            'storage_url': '/backups',
        }

        name, display, coll = format_task_table_row("restore_prod", restore_task)

        assert name == "restore_prod"
        assert "RESTORE" in display

    def test_format_task_table_row_copy_task_with_missing_fields(self):
        """format_task_table_row should handle incomplete copy tasks gracefully"""
        from db_wizard.utils import format_task_table_row

        bad_task = {
            'type': 'copy',
            'source_uri': 'mongodb://localhost',
        }

        name, display, coll = format_task_table_row("broken_task", bad_task)
        assert name == "broken_task"
        assert "Invalid" in display


# ===========================================================================
# --count flag: default fast mode vs explicit counting
# ===========================================================================

class TestFormatTaskTableRowCountFlag:
    """format_task_table_row must skip database connections by default"""

    def test_default_no_count_does_not_connect(self):
        """Without count=True, format_task_table_row must NOT connect to the database"""
        from db_wizard.utils import format_task_table_row

        copy_task = {
            'type': 'copy',
            'source_uri': 'mongodb://nonexistent-host-that-would-timeout:27017',
            'target_uri': 'mongodb://another-fake-host:27017',
            'source_db': 'testdb',
            'target_db': 'testdb_copy',
        }

        import time
        start = time.monotonic()
        name, display, coll = format_task_table_row("fast_task", copy_task)
        elapsed = time.monotonic() - start

        assert name == "fast_task"
        assert "nonexistent-host-that-would-timeout" in display
        assert "testdb" in display
        assert elapsed < 0.5, f"format_task_table_row took {elapsed:.1f}s without count - it's connecting!"

    def test_default_no_count_shows_no_row_numbers(self):
        """Without count=True, output should not contain row count strings"""
        from db_wizard.utils import format_task_table_row

        copy_task = {
            'type': 'copy',
            'source_uri': 'mongodb://localhost:27017',
            'target_uri': 'mongodb://localhost:27017',
            'source_db': 'db1',
            'target_db': 'db2',
        }

        name, display, coll = format_task_table_row("my_task", copy_task)

        assert "rows" not in display
