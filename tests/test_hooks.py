"""
Tests for before_command / after_command task hooks.

Hooks run real shell commands (true, false, touch into tmp_path): that is the
behaviour under test, and they need no database. The task itself is mocked.
"""

import pytest
from unittest.mock import MagicMock, patch

from db_wizard.hooks import AFTER, BEFORE, describe_hooks, hook_commands, run_hook


class TestHookCommands:
    def test_missing_and_empty_mean_no_hook(self):
        assert hook_commands({}, BEFORE) == []
        assert hook_commands({BEFORE: None}, BEFORE) == []
        assert hook_commands({BEFORE: ''}, BEFORE) == []
        assert hook_commands({BEFORE: []}, BEFORE) == []

    def test_string_becomes_one_command(self):
        assert hook_commands({AFTER: ' flat_dev_pass '}, AFTER) == ['flat_dev_pass']

    def test_list_keeps_order_and_drops_blanks(self):
        assert hook_commands({AFTER: ['a', ' ', 'b']}, AFTER) == ['a', 'b']

    @pytest.mark.parametrize('value', [42, {'cmd': 'x'}, ['ok', 3]])
    def test_broken_value_raises(self, value):
        with pytest.raises(ValueError, match='string or a list of strings'):
            hook_commands({AFTER: value}, AFTER)


class TestRunHook:
    def test_no_hook_succeeds(self):
        assert run_hook({}, AFTER) is True

    def test_commands_run_through_the_shell(self, tmp_path):
        marker = tmp_path / 'done.txt'
        # Redirects and && need a shell: proves shell=True.
        assert run_hook({AFTER: f'echo hi > {marker} && test -s {marker}'}, AFTER) is True
        assert marker.read_text().strip() == 'hi'

    def test_failure_stops_the_list(self, tmp_path):
        marker = tmp_path / 'never.txt'
        assert run_hook({AFTER: ['false', f'touch {marker}']}, AFTER) is False
        assert not marker.exists()

    def test_shell_alias_is_not_available(self):
        # Documented limitation: ~/.zshrc aliases do not exist in sh -c.
        assert run_hook({AFTER: 'surely_not_a_command_dbw_test'}, AFTER) is False


class TestDescribeHooks:
    def test_lists_every_command_in_order(self):
        rows = describe_hooks({BEFORE: 'a', AFTER: ['b', 'c']})
        assert rows == [('Before', 'a'), ('After', 'b'), ('After', 'c')]

    def test_broken_value_is_shown_not_raised(self):
        rows = describe_hooks({AFTER: 42})
        assert rows[0][0] == 'After' and 'invalid' in rows[0][1]


class TestRunTaskWithHooks:
    """run_task wraps every task type with the hooks."""

    def _run(self, task, task_result=True, task_type='copy'):
        from db_wizard import task_runner

        runner = {'copy': 'run_copy_task', 'backup': 'run_backup_task', 'restore': 'run_restore_task'}[task_type]
        task = dict(task, type=task_type)
        with patch.object(task_runner, runner, return_value=task_result) as mocked:
            ok = task_runner.run_task(task, assume_yes=True)
        return ok, mocked

    def test_before_then_task_then_after(self, tmp_path):
        log = tmp_path / 'log'
        ok, mocked = self._run({BEFORE: f'echo before >> {log}', AFTER: f'echo after >> {log}'})
        assert ok is True
        mocked.assert_called_once()
        assert log.read_text().split() == ['before', 'after']

    def test_failing_before_skips_task_and_after(self, tmp_path):
        marker = tmp_path / 'after'
        ok, mocked = self._run({BEFORE: 'false', AFTER: f'touch {marker}'})
        assert ok is False
        mocked.assert_not_called()
        assert not marker.exists()

    def test_failed_task_skips_after(self, tmp_path):
        marker = tmp_path / 'after'
        ok, _ = self._run({AFTER: f'touch {marker}'}, task_result=False)
        assert ok is False
        assert not marker.exists()

    def test_failing_after_fails_the_task(self):
        ok, mocked = self._run({AFTER: 'false'})
        assert ok is False
        mocked.assert_called_once()

    def test_broken_hook_value_fails_before_the_task(self):
        ok, mocked = self._run({BEFORE: 42})
        assert ok is False
        mocked.assert_not_called()

    @pytest.mark.parametrize('task_type', ['backup', 'restore'])
    def test_hooks_wrap_every_task_type(self, tmp_path, task_type):
        marker = tmp_path / 'after'
        ok, _ = self._run({AFTER: f'touch {marker}'}, task_type=task_type)
        assert ok is True
        assert marker.exists()

    def test_task_without_hooks_is_unchanged(self):
        ok, mocked = self._run({})
        assert ok is True
        mocked.assert_called_once()


class TestEditHooksFlow:
    def _edit(self, task, answers):
        from db_wizard.flows.manage_tasks import ManageTasksFlow
        wizard = MagicMock()
        flow = ManageTasksFlow(wizard)
        # 1st prompt picks the task, then Before, After, then "Press Enter".
        with patch('db_wizard.flows.manage_tasks.Prompt.ask', side_effect=['1', *answers, '']):
            flow._edit_hooks([('CMP', task)])
        return wizard.settings_manager.add_task

    def test_sets_both_hooks(self):
        task = {}
        save = self._edit(task, ['echo a', 'flat_dev_pass'])
        save.assert_called_once_with('CMP', {BEFORE: 'echo a', AFTER: 'flat_dev_pass'})

    def test_dash_removes_a_hook(self):
        task = {BEFORE: 'echo a', AFTER: 'b'}
        save = self._edit(task, ['-', 'b'])
        save.assert_called_once_with('CMP', {AFTER: 'b'})

    def test_keeping_a_list_does_not_rewrite_it(self):
        # Enter returns the default (the list joined): the list must survive.
        task = {AFTER: ['a', 'b']}
        save = self._edit(task, ['', 'a && b'])
        save.assert_not_called()
        assert task[AFTER] == ['a', 'b']
