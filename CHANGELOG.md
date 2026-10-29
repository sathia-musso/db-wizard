# Changelog

All notable changes to mongo-wizard will be documented in this file.

## [2.6.0] - 2026-10-29

### Added
- Glob patterns in a task's `source_collection`: `"compliance_*"`, `"audit*"`,
  `"audit_[wg]*"`. They are saved as typed and expanded against the source's
  tables on every run, so tables created after the task was saved are copied
  without editing the settings file. Matching is case-sensitive.
- Exclusions with a leading `!`: `["workforce_*", "!*_backup"]`. An exclusion
  removes its matches whatever entry added them; a list made only of
  exclusions starts from all the source's tables.
- The interactive multi-selection accepts names and patterns next to numbers
  and ranges (`1-3,users,compliance_*,!*_backup`), previews what the patterns
  match today and saves the patterns, not the snapshot.
- `--list-tasks` shows patterns, and `--count` counts the tables they match
  (it used to count a pattern as zero rows).
- `DatabaseEngine.supports_table_patterns`: Redis turns it off, because its
  "tables" are already key patterns and must reach the engine untouched.
- Task hooks: `before_command` and `after_command` (a string or a list of
  strings) run shell commands around any saved task (copy, backup, restore),
  e.g. resetting the passwords of copied users or touching a marker file.
  Before failing stops the task; a failed task skips after; after failing
  fails the task. Commands run with `/bin/sh -c`, so `~/.zshrc` aliases are
  not available. The task summary lists them, and **Manage saved tasks → Edit
  before/after commands** sets them without editing the JSON.

### Fixed
- Source and target lists of different lengths were paired with `zip()`,
  which silently dropped the extra tables and still reported success (a task
  with 10 source tables and 9 target names never copied the 10th). The task
  now refuses to start and names both lengths.
- A pattern matching nothing, or a selection emptied by its exclusions, aborts
  the task before the first copy. With `-y` this used to be indistinguishable
  from success; it also catches a source whose table listing failed.
- A table whose copy failed inside a multi-table task no longer hides behind
  the total: it is listed as skipped and the task exits with failure.

## [2.5.1] - 2026-08-13

### Fixed
- Removed the 10-minute wall-clock timeout on `dump | restore` pipes. A large
  database legitimately takes hours to copy, and a fixed deadline cannot tell
  "still working" from "hung": it killed healthy transfers halfway through and
  left the target truncated. Affected every engine (MySQL, MongoDB,
  PostgreSQL).
- Pipes now run to completion. Ctrl-C kills and reaps both ends, and the copy is
  reported as interrupted (`mysqldump_interrupted`, `interrupted`) instead of
  being announced as a successful copy of zero rows. Callers wanting a hard
  limit can wrap the CLI, e.g. `timeout 3h db-wizard ...`.
- `pg_restore` stderr is decoded with `errors='replace'`, so non-UTF8 output no
  longer raises while reporting an unrelated failure.

## [2.5.0] - 2026-07-31

### Added
- `--incremental` flag for copy operations: copies only the rows above the
  target's watermark, for append-only data. It is NOT a diff — updates and
  deletes on already-copied rows are never detected.
  - MySQL: `mysqldump --where='<col> > N' --no-create-info --insert-ignore`
  - MongoDB: filtered `find()` + `insert_many(ordered=False)`
  - PostgreSQL and Redis are unsupported (`pg_dump` cannot filter rows; Redis
    keys are unordered) and abort with a clear message.
- `--since-column` to override the watermark column, which otherwise defaults to
  the `AUTO_INCREMENT` column (MySQL) or `_id` (MongoDB).
- The resume point steps back by 10 rows / 5 seconds so rows committed out of id
  order are not skipped; duplicates are discarded on insert, making the copy
  idempotent.
- Incremental is selectable in the interactive wizard and persisted as a task
  property (`incremental`, `since_column`), so saved jobs append on every run.
  Existing tasks can be flipped via *Manage saved tasks → Toggle incremental
  copy*, and are marked `⏩ incremental` in `--list-tasks`.
- `--incremental` on `--task <name>` forces it for that single run.
- New engine abstraction: `supports_incremental`, `detect_watermark_column()`
  and `max_watermark()` on `DatabaseEngine`.
- Documentation: `docs/INCREMENTAL_COPY.md`.

### Changed
- Copy refusals and failures are no longer reported as a successful copy of zero
  rows: the CLI now exits non-zero and saved tasks report failure. Multi-table
  copies list the tables that were skipped.

## [2.4.1] - 2026-06-26

### Added
- Task confirmation summaries now show `[OVER SSH host]` when a connection uses an SSH tunnel, making it clear which side is remote even when both URIs point to `localhost`.
- Saved host names are shown as `host:database` prefix (e.g. `claim:performerclaim`) alongside the masked URI in copy, backup, and restore confirmations.

## [2.4.0] - 2026-06-22

### Added
- `--schema-only` flag for copy operations: copies structure/DDL only, no data.
  - MySQL: `mysqldump --no-data` (CREATE TABLE statements + indexes, zero rows)
  - PostgreSQL: `pg_dump --schema-only`
  - Schemaless engines (MongoDB, Redis) are unsupported: the operation aborts
    with a clear alert instead of silently copying all data.
- Schema-only is selectable in the interactive wizard (MySQL/Postgres only) and
  persisted as a task property (`schema_only`), so saved jobs can run schema-only
  on their own.
- `--schema-only` on `--task <name>` forces schema-only for that single run, even
  if the saved task was created as a full copy.

## [1.0.0] - 2024-09-26

### Added
- Initial release
- Interactive wizard mode for guided operations
- Saved hosts and connection management
- Task automation with `-y` flag for cron jobs
- Automatic mongodump/mongorestore preference for speed
- Python fallback when MongoDB tools unavailable
- `--force-python` flag for explicit Python mode
- Progress tracking for all operations
- Integrity verification after copy
- Automatic backups before destructive operations
- Multiple collection selection with range syntax
- Settings persistence in ~/.mongo_wizard_settings.json

### Features
- Copy single collections or entire databases
- Preserve indexes and metadata
- Support for authentication and SSL
- Dry-run mode for testing
- Comprehensive test suite

### Performance
- 10-100x faster than Python for large collections (using mongodump)
- Batch processing for optimal memory usage
- Connection pooling for efficiency