"""
Tests for incremental copy (--incremental).

All tests mock subprocess/pymongo - no real database server needed.
The point of these tests is the *decision logic*: which column is the
watermark, where the resume point lands, and when the copy must refuse.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId

from db_wizard.constants import INCREMENTAL_OVERLAP_ROWS, INCREMENTAL_OVERLAP_SECONDS
from db_wizard.engines.mysql import MySQLEngine, _build_incremental_where
from db_wizard.engines.mongo import MongoEngine
from db_wizard.engines.postgres import PostgresEngine
from db_wizard.engines.redis import RedisEngine


class TestBuildIncrementalWhere:
    """The WHERE clause is the whole feature: it decides what gets copied."""

    def test_numeric_watermark_steps_back_by_overlap(self):
        # A row with a lower id can commit after a higher one, so we re-read a
        # few rows rather than resuming exactly at MAX(id).
        where = _build_incremental_where('id', '1000')
        assert where == f"`id` > {1000 - INCREMENTAL_OVERLAP_ROWS}"

    def test_non_numeric_watermark_uses_inclusive_comparison(self):
        # Timestamps can repeat, so >= keeps rows sharing the boundary value.
        where = _build_incremental_where('created_at', '2026-07-31 10:00:00')
        assert where == "`created_at` >= '2026-07-31 10:00:00'"

    def test_string_watermark_is_escaped(self):
        where = _build_incremental_where('code', "O'Brien")
        assert "O''Brien" in where

    def test_column_name_is_backquoted(self):
        # Reserved words and odd column names must survive.
        assert _build_incremental_where('order', '5').startswith('`order`')


class TestMySQLWatermark:

    def _engine(self):
        return MySQLEngine("mysql://root@localhost/test")

    def test_detect_returns_auto_increment_column(self):
        engine = self._engine()
        with patch('db_wizard.engines.mysql.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="id\n")
            assert engine.detect_watermark_column('mydb', 'events') == 'id'

    def test_detect_returns_none_without_auto_increment(self):
        engine = self._engine()
        with patch('db_wizard.engines.mysql.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="\n")
            assert engine.detect_watermark_column('mydb', 'events') is None

    def test_max_watermark_returns_value(self):
        engine = self._engine()
        with patch('db_wizard.engines.mysql.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="4321\n")
            assert engine.max_watermark('mydb', 'events', 'id') == '4321'

    def test_max_watermark_none_on_empty_table(self):
        # MAX() over an empty table is SQL NULL: nothing to resume from.
        engine = self._engine()
        with patch('db_wizard.engines.mysql.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="NULL\n")
            assert engine.max_watermark('mydb', 'events', 'id') is None

    def test_max_watermark_none_when_table_missing(self):
        # The query fails outright; the caller treats it as "copy everything".
        engine = self._engine()
        with patch('db_wizard.engines.mysql.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no such table")
            assert engine.max_watermark('mydb', 'ghost', 'id') is None

    def test_engine_declares_support(self):
        assert self._engine().supports_incremental is True


class TestMySQLIncrementalGuards:
    """Flag combinations that would silently do the wrong thing must raise."""

    def _engines(self):
        source = MySQLEngine("mysql://root@source/test")
        target = MySQLEngine("mysql://root@target/test")
        return source, target

    def test_incremental_with_drop_target_raises(self):
        source, target = self._engines()
        with patch('db_wizard.engines.mysql.shutil.which', return_value='/usr/bin/mysqldump'):
            with pytest.raises(ValueError, match="drop-target"):
                target.copy(source, 'db', 'events', 'db', 'events',
                            drop_target=True, incremental=True)

    def test_incremental_with_schema_only_raises(self):
        source, target = self._engines()
        with patch('db_wizard.engines.mysql.shutil.which', return_value='/usr/bin/mysqldump'):
            with pytest.raises(ValueError, match="mutually exclusive"):
                target.copy(source, 'db', 'events', 'db', 'events',
                            incremental=True, schema_only=True)

    def test_incremental_refuses_table_rename(self):
        # --no-create-info dumps INSERTs naming the source table; there is
        # nothing to rewrite them against.
        source, target = self._engines()
        with patch('db_wizard.engines.mysql.shutil.which', return_value='/usr/bin/mysqldump'):
            result = target.copy(source, 'db', 'events', 'db', 'events_copy', incremental=True)
        assert result['method'] == 'incremental_rename_unsupported'
        assert result['documents_copied'] == 0

    def test_incremental_refuses_without_watermark_column(self):
        source, target = self._engines()
        with patch('db_wizard.engines.mysql.shutil.which', return_value='/usr/bin/mysqldump'), \
             patch('db_wizard.engines.mysql.subprocess.run') as mock_run:
            # No AUTO_INCREMENT column anywhere.
            mock_run.return_value = MagicMock(returncode=0, stdout="\n")
            result = target.copy(source, 'db', 'events', 'db', 'events', incremental=True)
        assert result['method'] == 'incremental_no_watermark'
        assert result['documents_copied'] == 0


class TestMongoOverlap:
    """The overlap protects against documents arriving out of _id order."""

    def test_objectid_steps_back_in_time(self):
        now = datetime(2026, 7, 31, 12, 0, 0)
        watermark = ObjectId.from_datetime(now)
        value, operator = MongoEngine._apply_overlap(watermark)
        assert operator == '$gt'
        assert value.generation_time.replace(tzinfo=None) == now - timedelta(
            seconds=INCREMENTAL_OVERLAP_SECONDS
        )

    def test_int_steps_back_by_rows(self):
        value, operator = MongoEngine._apply_overlap(500)
        assert (value, operator) == (500 - INCREMENTAL_OVERLAP_ROWS, '$gt')

    def test_datetime_steps_back_by_seconds(self):
        now = datetime(2026, 7, 31, 12, 0, 0)
        value, operator = MongoEngine._apply_overlap(now)
        assert (value, operator) == (now - timedelta(seconds=INCREMENTAL_OVERLAP_SECONDS), '$gt')

    def test_unknown_type_falls_back_to_inclusive(self):
        # No safe arithmetic on a string: re-read the boundary value itself.
        value, operator = MongoEngine._apply_overlap("abc")
        assert (value, operator) == ("abc", '$gte')

    def test_bool_is_not_treated_as_int(self):
        # bool subclasses int; arithmetic on it would be nonsense.
        value, operator = MongoEngine._apply_overlap(True)
        assert (value, operator) == (True, '$gte')


class TestMongoWatermark:

    def _engine_with_last_doc(self, doc):
        engine = MongoEngine("mongodb://localhost:27017")
        collection = MagicMock()
        collection.find_one.return_value = doc
        engine.client = {'mydb': {'events': collection}}
        return engine

    def test_objectid_id_is_monotonic(self):
        engine = self._engine_with_last_doc({'_id': ObjectId()})
        assert engine.detect_watermark_column('mydb', 'events') == '_id'

    def test_int_id_is_monotonic(self):
        engine = self._engine_with_last_doc({'_id': 42})
        assert engine.detect_watermark_column('mydb', 'events') == '_id'

    def test_string_id_is_refused(self):
        # UUIDs and slugs have no ordering: resuming on them would copy a
        # random subset of the collection.
        engine = self._engine_with_last_doc({'_id': 'a3f-user-slug'})
        assert engine.detect_watermark_column('mydb', 'events') is None

    def test_empty_collection_defaults_to_id(self):
        engine = self._engine_with_last_doc(None)
        assert engine.detect_watermark_column('mydb', 'events') == '_id'

    def test_max_watermark_reads_highest_value(self):
        oid = ObjectId()
        engine = self._engine_with_last_doc({'_id': oid})
        assert engine.max_watermark('mydb', 'events', '_id') == oid

    def test_max_watermark_none_on_empty_collection(self):
        engine = self._engine_with_last_doc(None)
        assert engine.max_watermark('mydb', 'events', '_id') is None

    def test_engine_declares_support(self):
        assert MongoEngine("mongodb://localhost").supports_incremental is True

    def test_incremental_with_drop_target_raises(self):
        target = MongoEngine("mongodb://target")
        with pytest.raises(ValueError, match="drop-target"):
            target.copy(MagicMock(), 'db', 'events', 'db', 'events',
                        drop_target=True, incremental=True)


class TestSavedTaskIncremental:
    """A saved task carrying incremental: true must run incrementally."""

    def _task(self, **overrides):
        task = {
            'type': 'copy',
            'source_uri': 'mysql://root@source/db',
            'target_uri': 'mysql://root@target/db',
            'source_db': 'src', 'target_db': 'tgt',
            'source_collection': 'events', 'target_collection': 'events',
            'drop_target': False,
        }
        task.update(overrides)
        return task

    def _run(self, task):
        """Run a copy task against mocked engines, return the copy() kwargs."""
        from db_wizard.task_runner import run_copy_task

        engine = MagicMock()
        engine.scheme = 'mysql'
        engine.copy.return_value = {'documents_copied': 5, 'method': 'mysqldump_incremental'}
        with patch('db_wizard.task_runner.EngineFactory') as factory:
            factory.create.return_value = engine
            factory.detect_scheme.return_value = 'mysql'
            ok = run_copy_task(task, assume_yes=True)
        return ok, engine

    def test_saved_flag_enables_incremental(self):
        ok, engine = self._run(self._task(incremental=True))
        assert ok is True
        assert engine.copy.call_args.kwargs['incremental'] is True

    def test_saved_since_column_is_passed_through(self):
        ok, engine = self._run(self._task(incremental=True, since_column='created_at'))
        assert engine.copy.call_args.kwargs['since_column'] == 'created_at'

    def test_task_without_flag_copies_in_full(self):
        ok, engine = self._run(self._task())
        assert engine.copy.call_args.kwargs['incremental'] is False

    def test_cli_flag_enables_incremental_on_a_full_task(self):
        from db_wizard.task_runner import run_copy_task

        engine = MagicMock()
        engine.scheme = 'mysql'
        engine.copy.return_value = {'documents_copied': 5, 'method': 'mysqldump_incremental'}
        with patch('db_wizard.task_runner.EngineFactory') as factory:
            factory.create.return_value = engine
            factory.detect_scheme.return_value = 'mysql'
            run_copy_task(self._task(), assume_yes=True, incremental=True)
        assert engine.copy.call_args.kwargs['incremental'] is True

    def test_incremental_task_that_drops_target_is_refused(self):
        # Dropping erases the watermark; running it anyway would silently
        # turn every run into a full copy.
        ok, engine = self._run(self._task(incremental=True, drop_target=True))
        assert ok is False
        engine.copy.assert_not_called()

    def test_incremental_task_on_unsupported_engine_is_refused(self):
        from db_wizard.task_runner import run_copy_task

        engine = MagicMock()
        with patch('db_wizard.task_runner.EngineFactory') as factory:
            factory.create.return_value = engine
            factory.detect_scheme.return_value = 'postgres'
            ok = run_copy_task(
                self._task(incremental=True,
                           source_uri='postgres://postgres@source/db',
                           target_uri='postgres://postgres@target/db'),
                assume_yes=True
            )
        assert ok is False
        engine.copy.assert_not_called()

    def test_refusal_result_is_not_reported_as_success(self):
        from db_wizard.task_runner import run_copy_task

        engine = MagicMock()
        engine.scheme = 'mysql'
        # The engine refused: no watermark column on that table.
        engine.copy.return_value = {'documents_copied': 0, 'method': 'incremental_no_watermark'}
        with patch('db_wizard.task_runner.EngineFactory') as factory:
            factory.create.return_value = engine
            factory.detect_scheme.return_value = 'mysql'
            ok = run_copy_task(self._task(incremental=True), assume_yes=True)
        assert ok is False


class TestToggleIncrementalOnSavedTask:
    """The manage-tasks flow can flip an existing task to incremental."""

    def _flow_with_task(self, task):
        from db_wizard.flows.manage_tasks import ManageTasksFlow

        wizard = MagicMock()
        flow = ManageTasksFlow(wizard)
        return flow, wizard, [('nightly', task)]

    def _mysql_task(self, **overrides):
        task = {
            'type': 'copy',
            'source_uri': 'mysql://root@source/db',
            'target_uri': 'mysql://root@target/db',
            'source_db': 'src', 'target_db': 'tgt',
            'drop_target': False,
        }
        task.update(overrides)
        return task

    def test_enabling_sets_the_flag(self):
        flow, wizard, tasks = self._flow_with_task(self._mysql_task())
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', '', '']):
            flow._toggle_incremental(tasks)
        saved = wizard.settings_manager.add_task.call_args[0][1]
        assert saved['incremental'] is True
        assert 'since_column' not in saved

    def test_enabling_stores_the_watermark_column(self):
        flow, wizard, tasks = self._flow_with_task(self._mysql_task())
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', 'created_at', '']):
            flow._toggle_incremental(tasks)
        saved = wizard.settings_manager.add_task.call_args[0][1]
        assert saved['since_column'] == 'created_at'

    def test_enabling_turns_off_drop_target(self):
        # Dropping the target erases the watermark, so it must go.
        flow, wizard, tasks = self._flow_with_task(self._mysql_task(drop_target=True))
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', '', '']), \
             patch('db_wizard.flows.manage_tasks.Confirm.ask', return_value=True):
            flow._toggle_incremental(tasks)
        saved = wizard.settings_manager.add_task.call_args[0][1]
        assert saved['incremental'] is True
        assert saved['drop_target'] is False

    def test_declining_the_drop_warning_changes_nothing(self):
        flow, wizard, tasks = self._flow_with_task(self._mysql_task(drop_target=True))
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', '', '']), \
             patch('db_wizard.flows.manage_tasks.Confirm.ask', return_value=False):
            flow._toggle_incremental(tasks)
        wizard.settings_manager.add_task.assert_not_called()

    def test_toggling_twice_leaves_the_task_as_found(self):
        # Enabling switches drop_target off; disabling must give it back.
        flow, wizard, tasks = self._flow_with_task(self._mysql_task(drop_target=True))
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', '', '']), \
             patch('db_wizard.flows.manage_tasks.Confirm.ask', return_value=True):
            flow._toggle_incremental(tasks)
        enabled = wizard.settings_manager.add_task.call_args[0][1]
        assert enabled['drop_target'] is False

        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', '']):
            flow._toggle_incremental([('nightly', enabled)])
        restored = wizard.settings_manager.add_task.call_args[0][1]
        assert restored['incremental'] is False
        assert restored['drop_target'] is True
        assert 'drop_target_before_incremental' not in restored

    def test_disabling_removes_the_flag(self):
        flow, wizard, tasks = self._flow_with_task(
            self._mysql_task(incremental=True, since_column='id')
        )
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', '']):
            flow._toggle_incremental(tasks)
        saved = wizard.settings_manager.add_task.call_args[0][1]
        assert saved['incremental'] is False
        assert 'since_column' not in saved

    def test_unsupported_engine_is_refused(self):
        flow, wizard, tasks = self._flow_with_task(self._mysql_task(
            source_uri='postgres://postgres@source/db',
            target_uri='postgres://postgres@target/db',
        ))
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', '']):
            flow._toggle_incremental(tasks)
        wizard.settings_manager.add_task.assert_not_called()

    def test_backup_task_is_refused(self):
        flow, wizard, tasks = self._flow_with_task({'type': 'backup', 'db_uri': 'mysql://root@host/db'})
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', '']):
            flow._toggle_incremental(tasks)
        wizard.settings_manager.add_task.assert_not_called()

    def test_schema_only_task_is_refused(self):
        # Schema-only copies no rows at all: there is nothing to append.
        flow, wizard, tasks = self._flow_with_task(self._mysql_task(schema_only=True))
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', '']):
            flow._toggle_incremental(tasks)
        wizard.settings_manager.add_task.assert_not_called()


class TestCopyFailedDetection:
    """A refusal must never be announced as a successful copy of zero rows."""

    def test_refusals_and_failures_are_detected(self):
        from db_wizard.utils import copy_failed

        for method in ('incremental_no_watermark', 'incremental_rename_unsupported',
                       'incremental_failed', 'mysqldump_failed', 'mysqldump_interrupted',
                       'interrupted', 'error', 'unsupported'):
            assert copy_failed({'method': method}) is True, method

    def test_real_copies_are_not_flagged(self):
        from db_wizard.utils import copy_failed

        for method in ('mysqldump', 'mysqldump_incremental', 'mongodump',
                       'python', 'python_incremental', 'mysqldump_schema'):
            assert copy_failed({'method': method}) is False, method


class TestUnsupportedEngines:
    """Engines that cannot filter rows must say so, not copy everything."""

    def test_postgres_refuses_incremental(self):
        target = PostgresEngine("postgres://postgres@target/db")
        with patch('db_wizard.engines.postgres.shutil.which', return_value='/usr/bin/pg_dump'):
            with pytest.raises(ValueError, match="not supported for PostgreSQL"):
                target.copy(MagicMock(), 'db', None, 'db', None, incremental=True)

    def test_redis_refuses_incremental(self):
        target = RedisEngine("redis://target")
        with pytest.raises(ValueError, match="not supported for Redis"):
            target.copy(MagicMock(), '0', None, '0', None, incremental=True)

    def test_postgres_does_not_declare_support(self):
        assert PostgresEngine("postgres://localhost/db").supports_incremental is False

    def test_redis_does_not_declare_support(self):
        assert RedisEngine("redis://localhost").supports_incremental is False
