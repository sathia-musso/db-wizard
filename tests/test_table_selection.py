"""
Tests for table selection: glob patterns, exclusions and source/target pairing.

All tests mock the engines - no real database server needed.
"""

import pytest
from unittest.mock import MagicMock, patch

from db_wizard.table_selection import (
    has_patterns,
    is_pattern,
    pair_tables,
    resolve_tables,
    resolve_task_tables,
)

AVAILABLE = [
    'users', 'audit', 'audit_weeks', 'audit_groups',
    'compliance_audit', 'compliance_geo_rules', 'compliance_kind_backup',
    'customs_jobs', 'Compliance_Upper',
]


class TestIsPattern:
    def test_plain_name_is_not_a_pattern(self):
        assert not is_pattern('users')
        assert not is_pattern('events-2024')

    def test_glob_chars_make_a_pattern(self):
        assert is_pattern('audit*')
        assert is_pattern('audit_?')
        assert is_pattern('audit_[wg]*')

    def test_exclusion_is_a_pattern(self):
        assert is_pattern('!users')

    def test_has_patterns_on_string_and_list(self):
        assert has_patterns('compliance_*')
        assert not has_patterns('users')
        assert has_patterns(['users', 'audit*'])
        assert not has_patterns(['users', 'audit'])
        assert not has_patterns(None)


class TestResolveTables:
    def test_prefix_pattern_expands_sorted(self):
        assert resolve_tables(['audit_*'], AVAILABLE) == ['audit_groups', 'audit_weeks']

    def test_star_without_underscore_includes_the_bare_table(self):
        assert resolve_tables(['audit*'], AVAILABLE) == ['audit', 'audit_groups', 'audit_weeks']

    def test_order_follows_entries_and_dedupes(self):
        result = resolve_tables(['users', 'audit*', 'audit_weeks'], AVAILABLE)
        assert result == ['users', 'audit', 'audit_groups', 'audit_weeks']

    def test_exclusion_removes_matches_wherever_it_sits(self):
        result = resolve_tables(['!*_backup', 'compliance_*'], AVAILABLE)
        assert result == ['compliance_audit', 'compliance_geo_rules']

    def test_exclusion_only_starts_from_everything(self):
        result = resolve_tables(['!audit*', '!compliance_*', '!Compliance_*'], AVAILABLE)
        assert result == ['customs_jobs', 'users']

    def test_matching_is_case_sensitive(self):
        assert 'Compliance_Upper' not in resolve_tables(['compliance_*'], AVAILABLE)

    def test_exact_name_passes_through_even_if_missing(self):
        # Unchanged behaviour: exact names are not validated here.
        assert resolve_tables(['not_there'], AVAILABLE) == ['not_there']

    def test_pattern_matching_nothing_raises(self):
        with pytest.raises(ValueError, match="matches no table"):
            resolve_tables(['complaince_*'], AVAILABLE)

    def test_empty_source_listing_raises(self):
        # MySQL list_tables() returns [] on error: must not become "copied nothing".
        with pytest.raises(ValueError):
            resolve_tables(['compliance_*'], [])


class TestPairTables:
    def test_none_target_keeps_names(self):
        assert pair_tables(['a', 'b'], None) == [('a', 'a'), ('b', 'b')]

    def test_same_length_renames_positionally(self):
        assert pair_tables(['a', 'b'], ['x', 'y']) == [('a', 'x'), ('b', 'y')]

    def test_length_mismatch_raises_instead_of_truncating(self):
        # The old zip() copied 9 of 10 tables and reported success.
        source = [f't{i}' for i in range(10)]
        with pytest.raises(ValueError, match="10 entries but target_collection has 9"):
            pair_tables(source, source[:9])


class TestResolveTaskTables:
    def test_plain_lists_do_not_list_tables(self):
        loader = MagicMock()
        assert resolve_task_tables(['a'], ['a'], loader) == [('a', 'a')]
        loader.assert_not_called()

    def test_patterns_use_loader(self):
        pairs = resolve_task_tables(['audit_*'], ['audit_*'], lambda: AVAILABLE)
        assert pairs == [('audit_groups', 'audit_groups'), ('audit_weeks', 'audit_weeks')]

    def test_pattern_string_is_a_one_entry_list(self):
        pairs = resolve_task_tables('audit_*', None, lambda: AVAILABLE)
        assert [src for src, _ in pairs] == ['audit_groups', 'audit_weeks']

    def test_patterns_refuse_a_renaming_target(self):
        with pytest.raises(ValueError, match="must be omitted"):
            resolve_task_tables(['audit_*'], ['x'], lambda: AVAILABLE)

    def test_everything_excluded_raises(self):
        with pytest.raises(ValueError, match="no table after exclusions"):
            resolve_task_tables(['audit_*', '!audit_*'], None, lambda: AVAILABLE)

    def test_no_expansion_for_key_pattern_engines(self):
        # Redis: 'user:*' is a key pattern for the engine, not a glob to expand.
        loader = MagicMock()
        assert resolve_task_tables(['user:*'], None, loader, expand_patterns=False) == [('user:*', 'user:*')]
        loader.assert_not_called()


def _engine(tables, supports_patterns=True):
    """A fake engine whose copy() records the (source, target) pairs it was asked for."""
    engine = MagicMock()
    engine.supports_table_patterns = supports_patterns
    engine.table_term = 'table'
    engine.table_term_plural = 'tables'
    engine.list_tables.return_value = [{'name': t, 'rows': 1, 'indexes': 0} for t in tables]
    engine.copy.return_value = {'documents_copied': 1, 'method': 'mysqldump'}
    return engine


def _run(task, source, target):
    from db_wizard import task_runner

    info = MagicMock(has_tunnel=False, uri='mysql://x')
    with patch.object(task_runner, 'resolve_host', return_value=info), \
         patch.object(task_runner.EngineFactory, 'create', side_effect=[source, target]), \
         patch.object(task_runner.EngineFactory, 'check_same_engine'):
        return task_runner.run_copy_task(task, assume_yes=True)


def _task(**overrides):
    task = {
        'source_uri': 'mysql://s', 'target_uri': 'mysql://t',
        'source_db': 'scriba', 'target_db': 'scriba', 'drop_target': True,
    }
    task.update(overrides)
    return task


class TestRunCopyTask:
    def test_patterns_copy_the_resolved_tables(self):
        source, target = _engine(AVAILABLE), _engine([])
        task = _task(source_collection=['users', 'audit_*'], target_collection=['users', 'audit_*'])
        assert _run(task, source, target) is True
        copied = [c.kwargs['source_table'] for c in target.copy.call_args_list]
        assert copied == ['users', 'audit_groups', 'audit_weeks']

    def test_length_mismatch_aborts_before_any_copy(self):
        source, target = _engine(AVAILABLE), _engine([])
        task = _task(source_collection=['a', 'b', 'c'], target_collection=['a', 'b'])
        assert _run(task, source, target) is False
        target.copy.assert_not_called()

    def test_pattern_matching_nothing_aborts_before_any_copy(self):
        source, target = _engine(AVAILABLE), _engine([])
        task = _task(source_collection=['users', 'complaince_*'])
        assert _run(task, source, target) is False
        target.copy.assert_not_called()

    def test_a_failed_table_fails_the_task(self):
        source, target = _engine(AVAILABLE), _engine([])
        target.copy.side_effect = [
            {'documents_copied': 1, 'method': 'mysqldump'},
            {'documents_copied': 0, 'method': 'mysqldump_failed'},
        ]
        task = _task(source_collection=['users', 'audit'])
        assert _run(task, source, target) is False

    def test_redis_entries_are_not_expanded(self):
        source, target = _engine(['*'], supports_patterns=False), _engine([], supports_patterns=False)
        task = _task(source_collection=['user:*'])
        assert _run(task, source, target) is True
        assert target.copy.call_args.kwargs['source_table'] == 'user:*'


class TestWizardParsing:
    def _parse(self, text, names=AVAILABLE, **kw):
        from db_wizard.flows.selection import SelectionFlow
        return SelectionFlow._parse_multi_selection(text, names, **kw)

    def test_numbers_and_ranges_become_names(self):
        assert self._parse('1,3-4') == ['users', 'audit_weeks', 'audit_groups']

    def test_patterns_are_kept_verbatim(self):
        assert self._parse('1, compliance_*, !*_backup') == ['users', 'compliance_*', '!*_backup']

    def test_dashed_name_is_not_a_range(self):
        assert self._parse('events-2024', names=['events-2024']) == ['events-2024']

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="not a table"):
            self._parse('userz')

    def test_pattern_matching_nothing_raises(self):
        with pytest.raises(ValueError, match="matches no table"):
            self._parse('complaince_*')

    def test_redis_accepts_any_token(self):
        assert self._parse('user:*', names=['*'], expand_patterns=False) == ['user:*']


class TestCountableTables:
    """--count must count what a pattern matches, not 0 rows for the pattern itself."""

    def test_plain_names_pass_through_without_listing(self):
        from db_wizard.utils import _countable_tables
        engine = _engine(AVAILABLE)
        assert _countable_tables(engine, 'scriba', ['users', 'audit']) == ['users', 'audit']
        engine.list_tables.assert_not_called()

    def test_patterns_resolve_against_the_engine(self):
        from db_wizard.utils import _countable_tables
        engine = _engine(AVAILABLE)
        assert _countable_tables(engine, 'scriba', ['users', 'audit_*']) == ['users', 'audit_groups', 'audit_weeks']

    def test_pattern_matching_nothing_counts_as_nothing(self):
        # A listing must not abort because one task has a dead pattern.
        from db_wizard.utils import _countable_tables
        assert _countable_tables(_engine(AVAILABLE), 'scriba', ['nope_*']) == []


class TestTaskListingDisplay:
    def _display(self, collection):
        from db_wizard.utils import format_task_table_row
        task = _task(source_collection=collection)
        info = MagicMock(name='host')
        info.name = 'host'
        with patch('db_wizard.utils.resolve_host', return_value=info):
            return format_task_table_row('t', task)[2]

    def test_plain_list_shows_count(self):
        assert self._display(['users', 'audit']) == '2 items'

    def test_short_pattern_list_shows_patterns(self):
        assert self._display(['users', 'audit*']) == 'users, audit*'

    def test_long_pattern_list_says_patterns(self):
        assert self._display(['a', 'b', 'c', 'd*']) == '4 entries (patterns)'


class TestCopyWizardWithPatterns:
    """The interactive copy copies today's matches but saves the patterns."""

    def _run_flow(self, selection, save=True):
        from db_wizard.flows.copy_flow import CopyWizardFlow

        source, target = _engine(AVAILABLE), _engine(['users'])
        for engine in (source, target):
            engine.scheme = 'mysql'
            engine.supports_incremental = True
            engine.count_rows.return_value = 1

        wizard = MagicMock()
        wizard.select_or_add_host.side_effect = [
            {'resolved_uri': 'mysql://s', 'raw_uri': 'mysql://s', 'ssh_tunnel': None},
            {'resolved_uri': 'mysql://t', 'raw_uri': 'mysql://t', 'ssh_tunnel': None},
        ]
        wizard.select_database.return_value = 'scriba'
        wizard.select_collection.return_value = selection

        # MySQL, multiple tables: no "same collection name?", no mongo-only
        # questions (copy indexes, verify).
        answers = [
            True,   # "select multiple?"
            True,   # "same db name?"
            False,  # "drop target?"
            False,  # "schema only?"
            False,  # "incremental copy?"
            True,   # "proceed?"
            save,   # "save task?"
        ]
        with patch('db_wizard.flows.copy_flow.EngineFactory') as factory, \
             patch('db_wizard.flows.copy_flow.Confirm.ask', side_effect=answers), \
             patch('db_wizard.flows.copy_flow.Prompt.ask', return_value='CMP'):
            factory.create.side_effect = [source, target]
            CopyWizardFlow(wizard).run()
        return wizard, target

    def test_copies_the_resolved_tables(self):
        _, target = self._run_flow(['users', 'audit_*'], save=False)
        copied = [c.kwargs['source_table'] for c in target.copy.call_args_list]
        assert copied == ['users', 'audit_groups', 'audit_weeks']

    def test_saves_the_patterns_not_the_snapshot(self):
        wizard, _ = self._run_flow(['users', 'audit_*', '!audit_weeks'])
        name, config = wizard.settings_manager.add_task.call_args.args
        assert name == 'CMP'
        assert config['source_collection'] == ['users', 'audit_*', '!audit_weeks']
        # Identical to the source: accepted by the runner as "same names".
        assert resolve_task_tables(config['source_collection'], config['target_collection'],
                                   lambda: AVAILABLE) == [('users', 'users'), ('audit_groups', 'audit_groups')]


class TestSelectionPromptWithPatterns:
    def _select(self, typed, confirm=True):
        from db_wizard.flows.selection import SelectionFlow
        flow = SelectionFlow(MagicMock())
        with patch('db_wizard.flows.selection.Prompt.ask', side_effect=typed), \
             patch('db_wizard.flows.selection.Confirm.ask', side_effect=confirm):
            return flow.select_collection(_engine(AVAILABLE), 'scriba', allow_multiple=True)

    def test_pattern_selection_is_returned_verbatim(self):
        assert self._select(['users,compliance_*'], confirm=[True]) == ['users', 'compliance_*']

    def test_rejected_preview_asks_again(self):
        # Declining the preview re-opens the prompt instead of returning.
        assert self._select(['compliance_*', 'audit'], confirm=[False]) == ['audit']

    def test_typo_is_refused(self):
        assert self._select(['complaince_*'], confirm=[]) == []
