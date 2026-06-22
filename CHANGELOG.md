# Changelog

All notable changes to mongo-wizard will be documented in this file.

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