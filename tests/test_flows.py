"""
Tests for interactive wizard flows.

Three levels:
1. Smoke tests  -- every flow module imports without error
2. Invariants   -- GoHome is the same class everywhere, _common exports work
3. Unit tests   -- mock prompts, verify flow logic calls the right things
"""

import pytest
from unittest.mock import patch, MagicMock, call


# ===========================================================================
# 1. Smoke tests -- would have caught the broken `from .engine` imports
# ===========================================================================

class TestFlowImports:
    """Every flow module must import cleanly. No runtime ImportErrors."""

    def test_import_common(self):
        from db_wizard.flows._common import GoHome, _ask, _test_connection

    def test_import_selection(self):
        from db_wizard.flows.selection import SelectionFlow

    def test_import_copy_flow(self):
        from db_wizard.flows.copy_flow import CopyWizardFlow

    def test_import_backup_flow(self):
        from db_wizard.flows.backup_flow import BackupWizardFlow

    def test_import_restore_flow(self):
        from db_wizard.flows.restore_flow import RestoreWizardFlow

    def test_import_browse_flow(self):
        from db_wizard.flows.browse_flow import BrowseWizardFlow

    def test_import_manage_hosts(self):
        from db_wizard.flows.manage_hosts import ManageHostsFlow

    def test_import_manage_storages(self):
        from db_wizard.flows.manage_storages import ManageStoragesFlow

    def test_import_manage_tasks(self):
        from db_wizard.flows.manage_tasks import ManageTasksFlow

    def test_import_wizard(self):
        from db_wizard.wizard import DbWizard


# ===========================================================================
# 2. Invariants -- GoHome identity, _common contract
# ===========================================================================

class TestGoHomeIdentity:
    """GoHome must be the exact same class everywhere.
    If it's not, `except GoHome` in the wizard main loop won't catch
    exceptions raised by flows -- the bug we fixed."""

    def test_gohome_is_same_class_wizard_and_common(self):
        from db_wizard.flows._common import GoHome as G1
        from db_wizard.wizard import GoHome as G2
        assert G1 is G2

    def test_gohome_is_same_class_in_manage_hosts(self):
        """manage_hosts imports GoHome from _common -- verify it's the same."""
        from db_wizard.flows._common import GoHome as G1
        from db_wizard.flows.manage_hosts import GoHome as G3
        assert G1 is G3

    def test_gohome_is_same_class_manage_hosts_and_common(self):
        from db_wizard.flows._common import GoHome as G1
        from db_wizard.flows.manage_hosts import GoHome as G4
        assert G1 is G4

    def test_gohome_is_catchable_across_modules(self):
        """Simulate the real scenario: selection raises, wizard catches."""
        from db_wizard.flows._common import GoHome as CommonGoHome
        from db_wizard.wizard import GoHome as WizardGoHome

        with pytest.raises(WizardGoHome):
            raise CommonGoHome()

    def test_ask_raises_gohome_on_x(self):
        """_ask must raise GoHome when user types 'x'."""
        from db_wizard.flows._common import _ask, GoHome

        with patch("db_wizard.flows._common.Prompt.ask", return_value="x"):
            with pytest.raises(GoHome):
                _ask("Pick something")

    def test_ask_returns_value_normally(self):
        from db_wizard.flows._common import _ask

        with patch("db_wizard.flows._common.Prompt.ask", return_value="hello"):
            assert _ask("Pick something") == "hello"

    def test_test_connection_delegates_to_engine(self):
        """_test_connection must create an engine and call test_connection on it."""
        from db_wizard.flows._common import _test_connection

        mock_engine = MagicMock()
        mock_engine.test_connection.return_value = (True, "OK (3 databases)")

        # EngineFactory is imported locally inside _test_connection, patch at source
        with patch("db_wizard.engine.EngineFactory.create", return_value=mock_engine) as mock_create:
            success, msg = _test_connection("mongodb://localhost")

        assert success is True
        assert "OK" in msg
        mock_create.assert_called_once_with("mongodb://localhost")

    def test_test_connection_handles_bad_uri(self):
        from db_wizard.flows._common import _test_connection

        with patch("db_wizard.engine.EngineFactory.create", side_effect=ValueError("Unsupported scheme: foobar")):
            success, msg = _test_connection("foobar://localhost")

        assert success is False
        assert "Unsupported" in msg


# ===========================================================================
# 3. Unit tests -- flow logic with mocked prompts
# ===========================================================================

class TestSelectionFlow:
    """Test SelectionFlow with mocked prompts and engines."""

    def _make_wizard(self):
        """Create a minimal mocked wizard context."""
        wizard = MagicMock()
        wizard.settings_manager = MagicMock()
        wizard.settings_manager.list_hosts.return_value = {}
        return wizard

    def _make_engine(self, databases=None, tables=None):
        """Create a mocked DatabaseEngine."""
        engine = MagicMock()
        engine.table_term = "collection"
        engine.table_term_plural = "collections"
        engine.scheme = "mongodb"
        engine.list_databases.return_value = databases or []
        engine.list_tables.return_value = tables or []
        return engine

    def test_select_database_returns_chosen(self):
        from db_wizard.flows.selection import SelectionFlow

        wizard = self._make_wizard()
        engine = self._make_engine(databases=[
            {'name': 'users_db', 'tables_count': 5, 'size_mb': 120.0},
            {'name': 'orders_db', 'tables_count': 12, 'size_mb': 450.0},
            {'name': 'logs_db', 'tables_count': 1, 'size_mb': 2000.0},
        ])

        # User picks option 2 -> orders_db
        with patch("db_wizard.flows.selection._ask", return_value="2"):
            result = SelectionFlow(wizard).select_database(engine, "source")

        assert result == "orders_db"
        engine.list_databases.assert_called_once()

    def test_select_database_first_option(self):
        from db_wizard.flows.selection import SelectionFlow

        wizard = self._make_wizard()
        engine = self._make_engine(databases=[
            {'name': 'alpha', 'tables_count': 1, 'size_mb': 10.0},
            {'name': 'beta', 'tables_count': 2, 'size_mb': 20.0},
        ])

        with patch("db_wizard.flows.selection._ask", return_value="1"):
            result = SelectionFlow(wizard).select_database(engine, "target")

        assert result == "alpha"

    def test_select_database_manual_when_empty(self):
        """When no databases found, user enters name manually."""
        from db_wizard.flows.selection import SelectionFlow

        wizard = self._make_wizard()
        engine = self._make_engine(databases=[])

        with patch("db_wizard.flows.selection._ask", return_value="my_custom_db"):
            result = SelectionFlow(wizard).select_database(engine, "source")

        assert result == "my_custom_db"

    def test_select_collection_single(self):
        from db_wizard.flows.selection import SelectionFlow

        wizard = self._make_wizard()
        engine = self._make_engine(tables=[
            {'name': 'users', 'rows': 5000, 'indexes': 3},
            {'name': 'orders', 'rows': 10000, 'indexes': 5},
            {'name': 'logs', 'rows': 100, 'indexes': 1},
        ])

        # User picks option 1 -> logs (sorted alphabetically: logs, orders, users)
        with patch("db_wizard.flows.selection._ask", return_value="1"):
            result = SelectionFlow(wizard).select_collection(
                engine, "mydb", "source", allow_all=True, allow_multiple=False
            )

        assert result == "logs"  # First alphabetically

    def test_select_collection_all_returns_none(self):
        from db_wizard.flows.selection import SelectionFlow

        wizard = self._make_wizard()
        engine = self._make_engine(tables=[
            {'name': 'users', 'rows': 100, 'indexes': 1},
        ])

        # User picks 0 -> ALL
        with patch("db_wizard.flows.selection._ask", return_value="0"):
            result = SelectionFlow(wizard).select_collection(
                engine, "mydb", "source", allow_all=True, allow_multiple=False
            )

        assert result is None  # None means "all collections"

    def test_select_collection_multiple(self):
        from db_wizard.flows.selection import SelectionFlow

        wizard = self._make_wizard()
        engine = self._make_engine(tables=[
            {'name': 'users', 'rows': 5000, 'indexes': 3},
            {'name': 'orders', 'rows': 10000, 'indexes': 5},
            {'name': 'logs', 'rows': 100, 'indexes': 1},
        ])

        # User picks "1,3" -> logs and users (sorted alphabetically: logs=1, orders=2, users=3)
        with patch("db_wizard.flows.selection.Prompt.ask", return_value="1,3"):
            result = SelectionFlow(wizard).select_collection(
                engine, "mydb", "source", allow_all=True, allow_multiple=True
            )

        assert isinstance(result, list)
        assert len(result) == 2
        assert "logs" in result
        assert "users" in result

    def test_select_collection_range(self):
        from db_wizard.flows.selection import SelectionFlow

        wizard = self._make_wizard()
        engine = self._make_engine(tables=[
            {'name': 'a', 'rows': 300, 'indexes': 1},
            {'name': 'b', 'rows': 200, 'indexes': 1},
            {'name': 'c', 'rows': 100, 'indexes': 1},
        ])

        # User picks "1-3" -> all three (sorted by rows: a=1, b=2, c=3)
        with patch("db_wizard.flows.selection.Prompt.ask", return_value="1-3"):
            result = SelectionFlow(wizard).select_collection(
                engine, "mydb", "source", allow_all=True, allow_multiple=True
            )

        assert isinstance(result, list)
        assert len(result) == 3

    @patch('db_wizard.flows.selection._ask', return_value="mongodb://myhost:27017")
    def test_select_or_add_host_manual_uri(self, mock_ask):
        """User picks 'enter URI manually' option."""
        from db_wizard.flows.selection import SelectionFlow

        wizard = self._make_wizard()
        # No saved hosts -> option 1 = add new, option 2 = manual URI
        with patch("db_wizard.flows.selection._ask", side_effect=["2", "mongodb://myhost:27017"]):
            result = SelectionFlow(wizard).select_or_add_host("source")

        assert result['resolved_uri'] == "mongodb://myhost:27017"
        assert result['raw_uri'] == "mongodb://myhost:27017"
        assert result['ssh_tunnel'] is None

    def test_select_or_add_host_saved_host_online(self):
        """User picks a saved host that's online."""
        from db_wizard.flows.selection import SelectionFlow

        wizard = self._make_wizard()
        wizard.settings_manager.list_hosts.return_value = {
            "production": "mongodb://prod:27017"
        }
        wizard._get_host_uri.return_value = "mongodb://prod:27017"
        wizard._host_display.return_value = "production [dim](MongoDB)[/dim]"
        wizard._resolve_host.return_value = "mongodb://prod:27017"

        with patch("db_wizard.flows.selection._ask", return_value="1"), \
             patch("db_wizard.flows.selection._test_connection", return_value=(True, "OK (5 databases)")):
            result = SelectionFlow(wizard).select_or_add_host("source")

        assert result['resolved_uri'] == "mongodb://prod:27017"
        assert result['raw_uri'] == "mongodb://prod:27017"
        assert result['ssh_tunnel'] is None


class TestCopyWizardFlow:
    """Test CopyWizardFlow with fully mocked wizard context."""

    def _make_wizard(self):
        wizard = MagicMock()
        wizard.settings_manager = MagicMock()
        return wizard

    def _make_engine(self, scheme="mongodb"):
        engine = MagicMock()
        engine.scheme = scheme
        engine.table_term = "collection"
        engine.table_term_plural = "collections"
        engine.count_rows.return_value = 1000
        engine.supports_incremental = True
        engine.list_tables.return_value = [
            {'name': 'users', 'rows': 1000, 'indexes': 2}
        ]
        engine.copy.return_value = {
            'documents_copied': 1000, 'indexes_created': 2, 'method': 'mongodump'
        }
        return engine

    def test_copy_single_collection(self):
        from db_wizard.flows.copy_flow import CopyWizardFlow

        wizard = self._make_wizard()
        source_engine = self._make_engine()
        target_engine = self._make_engine()

        wizard.select_or_add_host.side_effect = [
            {'resolved_uri': "mongodb://source:27017", 'raw_uri': "mongodb://source:27017", 'ssh_tunnel': None},
            {'resolved_uri': "mongodb://target:27017", 'raw_uri': "mongodb://target:27017", 'ssh_tunnel': None},
        ]
        wizard.select_database.return_value = "mydb"
        wizard.select_collection.return_value = "users"
        wizard.source_engine = source_engine
        wizard.target_engine = target_engine
        wizard.source_uri = "mongodb://source:27017"
        wizard.target_uri = "mongodb://target:27017"

        with patch("db_wizard.flows.copy_flow.EngineFactory") as mock_factory, \
             patch("db_wizard.flows.copy_flow.Confirm.ask", side_effect=[
                 False,  # "select multiple?"
                 True,   # "same db name?"
                 True,   # "same collection name?"
                 False,  # "drop target?"
                 False,  # "incremental copy?"
                 True,   # "copy indexes?"
                 True,   # "verify?"
                 True,   # "proceed?"
                 False,  # "save task?"
             ]), \
             patch("db_wizard.flows.copy_flow.Prompt.ask", return_value=""):
            mock_factory.create.side_effect = [source_engine, target_engine]

            flow = CopyWizardFlow(wizard)
            flow.run()

        # Verify copy was called
        target_engine.copy.assert_called_once()
        copy_kwargs = target_engine.copy.call_args
        assert copy_kwargs.kwargs.get('source_db') or copy_kwargs[1].get('source_db') == "mydb"

        # Verify cleanup
        source_engine.close.assert_called()
        target_engine.close.assert_called()

    def test_copy_all_collections(self):
        from db_wizard.flows.copy_flow import CopyWizardFlow

        wizard = self._make_wizard()
        source_engine = self._make_engine()
        target_engine = self._make_engine()
        target_engine.copy.return_value = {
            'users': {'documents_copied': 1000},
            'orders': {'documents_copied': 5000},
        }

        wizard.select_or_add_host.side_effect = [
            {'resolved_uri': "mongodb://source:27017", 'raw_uri': "mongodb://source:27017", 'ssh_tunnel': None},
            {'resolved_uri': "mongodb://target:27017", 'raw_uri': "mongodb://target:27017", 'ssh_tunnel': None},
        ]
        wizard.select_database.return_value = "mydb"
        wizard.select_collection.return_value = None  # ALL collections
        wizard.source_engine = source_engine
        wizard.target_engine = target_engine
        wizard.source_uri = "mongodb://source:27017"
        wizard.target_uri = "mongodb://target:27017"

        # For ALL collections path (source_collection=None, is_mongo=True):
        # 1. "select multiple?" -> False
        # 2. "same db name?" -> True
        # (no "same collection name?" -- source_collection is None)
        # 3. "drop target?" -> False
        # 4. "copy indexes?" -> True (is_mongo)
        # 5. "verify?" -> False (is_mongo)
        # 6. "proceed?" -> True
        # 7. "save task?" -> False
        with patch("db_wizard.flows.copy_flow.EngineFactory") as mock_factory, \
             patch("db_wizard.flows.copy_flow.Confirm.ask", side_effect=[
                 False,  # "select multiple?"
                 True,   # "same db name?"
                 False,  # "drop target?"
                 False,  # "incremental copy?"
                 True,   # "copy indexes?"
                 False,  # "verify?"
                 True,   # "proceed?"
                 False,  # "save task?"
             ]), \
             patch("db_wizard.flows.copy_flow.Prompt.ask", return_value=""):
            mock_factory.create.side_effect = [source_engine, target_engine]

            flow = CopyWizardFlow(wizard)
            flow.run()

        # Copy called with source_table=None (all collections)
        target_engine.copy.assert_called_once()
        call_kwargs = target_engine.copy.call_args[1]
        assert call_kwargs['source_table'] is None
        assert call_kwargs['target_table'] is None

    def test_copy_cancelled_by_user(self):
        from db_wizard.flows.copy_flow import CopyWizardFlow

        wizard = self._make_wizard()
        source_engine = self._make_engine()
        target_engine = self._make_engine()

        wizard.select_or_add_host.side_effect = [
            {'resolved_uri': "mongodb://source:27017", 'raw_uri': "mongodb://source:27017", 'ssh_tunnel': None},
            {'resolved_uri': "mongodb://target:27017", 'raw_uri': "mongodb://target:27017", 'ssh_tunnel': None},
        ]
        wizard.select_database.return_value = "mydb"
        wizard.select_collection.return_value = "users"
        wizard.source_engine = source_engine
        wizard.target_engine = target_engine
        wizard.source_uri = "mongodb://source:27017"
        wizard.target_uri = "mongodb://target:27017"

        with patch("db_wizard.flows.copy_flow.EngineFactory") as mock_factory, \
             patch("db_wizard.flows.copy_flow.Confirm.ask", side_effect=[
                 False,  # "select multiple?"
                 True,   # "same db name?"
                 True,   # "same collection name?"
                 False,  # "drop target?"
                 False,  # "incremental copy?"
                 True,   # "copy indexes?"
                 True,   # "verify?"
                 False,  # "proceed?" -> NO, user cancels
             ]), \
             patch("db_wizard.flows.copy_flow.Prompt.ask", return_value=""):
            mock_factory.create.side_effect = [source_engine, target_engine]

            flow = CopyWizardFlow(wizard)
            flow.run()

        # Copy should NOT have been called
        target_engine.copy.assert_not_called()


class TestManageHostsFlow:
    """Test ManageHostsFlow."""

    def test_add_host_delegates_to_wizard(self):
        from db_wizard.flows.manage_hosts import ManageHostsFlow

        wizard = MagicMock()
        wizard.settings_manager.list_hosts.return_value = {}

        # User picks "1" (add new host), then "0" (back)
        with patch("db_wizard.flows.manage_hosts.Prompt.ask", side_effect=["1", "0"]):
            flow = ManageHostsFlow(wizard)
            flow.run()

        wizard._add_new_host.assert_called_once()

    def test_delete_host(self):
        from db_wizard.flows.manage_hosts import ManageHostsFlow

        wizard = MagicMock()
        wizard.settings_manager.list_hosts.return_value = {
            "staging": "mongodb://staging:27017",
            "production": "mongodb://prod:27017",
        }

        # Prompt.ask calls: "2" (delete), "1" (host number), "" (press enter), then "0" (back)
        with patch("db_wizard.flows.manage_hosts.Prompt.ask", side_effect=["2", "1", "", "0"]), \
             patch("db_wizard.flows.manage_hosts.Confirm.ask", return_value=True):
            flow = ManageHostsFlow(wizard)
            flow.run()

        wizard.settings_manager.delete_host.assert_called_once_with("staging")

    def test_gohome_during_add_host_is_caught(self):
        """GoHome raised during _add_new_host must not crash the flow."""
        from db_wizard.flows.manage_hosts import ManageHostsFlow
        from db_wizard.flows._common import GoHome

        wizard = MagicMock()
        wizard.settings_manager.list_hosts.return_value = {}
        wizard._add_new_host.side_effect = GoHome()

        # User picks "1" (add), GoHome raised, then "0" (back)
        with patch("db_wizard.flows.manage_hosts.Prompt.ask", side_effect=["1", "0"]):
            flow = ManageHostsFlow(wizard)
            # Should NOT raise -- GoHome is caught internally
            flow.run()


class TestWizardMainLoop:
    """Test the DbWizard main loop and GoHome handling."""

    def test_gohome_returns_to_menu(self):
        """GoHome from any sub-flow should return to main menu, not crash."""
        from db_wizard.wizard import DbWizard
        from db_wizard.flows._common import GoHome

        wizard = DbWizard.__new__(DbWizard)
        wizard.settings_manager = MagicMock()

        # Simulate: user picks "1" (copy), copy raises GoHome, then "0" (exit)
        with patch.object(wizard, 'main_menu', side_effect=["1", "0"]), \
             patch.object(wizard, 'copy_wizard', side_effect=GoHome()):
            wizard.run()

        # If we get here without exception, GoHome was caught correctly

    def test_exit_breaks_loop(self):
        from db_wizard.wizard import DbWizard

        wizard = DbWizard.__new__(DbWizard)
        wizard.settings_manager = MagicMock()

        with patch.object(wizard, 'main_menu', return_value="0"):
            wizard.run()  # Should exit cleanly

    def test_all_menu_options_dispatch(self):
        """Verify each menu option calls the right method."""
        from db_wizard.wizard import DbWizard

        wizard = DbWizard.__new__(DbWizard)
        wizard.settings_manager = MagicMock()

        method_map = {
            "1": "copy_wizard",
            "2": "run_saved_task",
            "3": "manage_hosts",
            "4": "manage_tasks",
            "5": "manage_storages",
            "6": "browse_database",
            "7": "backup_wizard",
            "8": "restore_wizard",
        }

        for option, method_name in method_map.items():
            with patch.object(wizard, 'main_menu', side_effect=[option, "0"]), \
                 patch.object(wizard, method_name, create=True) as mock_method:
                wizard.run()
                mock_method.assert_called_once(), f"Menu option {option} should call {method_name}"


class TestRunSavedTask:
    """Test run_saved_task delegates to task_runner properly."""

    def test_run_copy_task_delegates(self):
        from db_wizard.wizard import DbWizard

        wizard = DbWizard.__new__(DbWizard)
        wizard.settings_manager = MagicMock()
        wizard.settings_manager.get_task.return_value = {
            'type': 'copy',
            'source_uri': 'mongodb://source',
            'target_uri': 'mongodb://target',
            'source_db': 'mydb',
            'target_db': 'mydb',
        }

        with patch("db_wizard.wizard.Confirm.ask", return_value=True), \
             patch("db_wizard.wizard.Prompt.ask", return_value=""), \
             patch("db_wizard.task_runner.run_task", return_value=True) as mock_run, \
             patch("db_wizard.task_runner.display_task_summary"):
            wizard.run_saved_task("daily_copy")

        mock_run.assert_called_once()

    def test_run_nonexistent_task(self):
        from db_wizard.wizard import DbWizard

        wizard = DbWizard.__new__(DbWizard)
        wizard.settings_manager = MagicMock()
        wizard.settings_manager.get_task.return_value = None

        # Should not raise, just print error
        wizard.run_saved_task("does_not_exist")

    def test_run_task_cancelled_by_user(self):
        from db_wizard.wizard import DbWizard

        wizard = DbWizard.__new__(DbWizard)
        wizard.settings_manager = MagicMock()
        wizard.settings_manager.get_task.return_value = {
            'type': 'copy',
            'source_uri': 'mongodb://source',
            'target_uri': 'mongodb://target',
            'source_db': 'db1',
            'target_db': 'db1',
        }

        with patch("db_wizard.wizard.Confirm.ask", return_value=False), \
             patch("db_wizard.task_runner.run_task") as mock_run, \
             patch("db_wizard.task_runner.display_task_summary"):
            wizard.run_saved_task("my_task")

        # User said no -> task_runner should NOT be called
        mock_run.assert_not_called()
