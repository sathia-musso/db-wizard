# db-wizard

Advanced database copy and migration tool with interactive wizard. Supports **MongoDB**, **MySQL**, **PostgreSQL**, and **Redis**.

## Why?

- Copying databases between servers means remembering long `mongodump`/`mysqldump`/`pg_dump` commands with connection strings
- Shell aliases with plaintext passwords end up in bash history
- No progress tracking, no error handling, no saved configurations
- Repetitive tasks require typing the same commands every time

db-wizard fixes all of this with saved hosts, saved tasks, SSH tunnels, interactive wizard, and proper error handling.

## Quick Start

### Installation

You can install `db-wizard` globally using `pipx` (recommended) or `pip`:

```bash
# Recommended: Install globally in an isolated environment
pipx install db-wizard

# Alternative: Install via pip
pip install db-wizard
```

### Usage

```bash
# Launch interactive wizard
dbw
```

## Key Features

- **Multi-database**: MongoDB, MySQL, PostgreSQL, and Redis through a single interface
- **Auto-detect engine**: URI scheme determines the engine (`mongodb://`, `mysql://`, `postgres://`, `redis://`)
- **Interactive wizard**: Guided menu for all operations
- **SSH tunnels**: Built-in tunnel for hosts behind SSH (auto port-forward)
- **Saved hosts**: Store connection configs with optional SSH tunnel
- **Saved tasks**: Create and execute repeatable copy/backup/restore tasks
- **Full automation**: `-y` flag for unattended cron jobs
- **Backup & restore**: Complete backup/restore with compression to local, SSH, or FTP storage
- **Password masking**: Credentials never shown in UI output
- **Progress tracking**: Real-time feedback during operations
- **Smart copy**: MongoDB uses mongodump/mongorestore (10-100x faster) with Python fallback; MySQL uses mysqldump/mysql pipe

## Usage

### Interactive Mode (Recommended)

```bash
dbw
```

The wizard guides you through:
- Select/create saved hosts (MongoDB, MySQL, PostgreSQL, or Redis, with optional SSH tunnel)
- Choose database and tables/collections
- Copy options (drop target, backup before drop, verify)
- Save as reusable task

### Saved Tasks

```bash
# List tasks
dbw --list-tasks

# Run task with confirmation
dbw --task daily_backup

# Run automated (for cron)
dbw --task daily_backup -y

# Run a saved job copying structure only, no data (MySQL/Postgres)
dbw --task daily_backup --schema-only

# Run a saved job copying only new rows (append-only data)
dbw --task nightly_events --incremental
```

Schema-only can be saved as part of a task (set in the interactive wizard for
MySQL/Postgres), or forced for a single run with `--schema-only`.

Incremental works the same way: save it on the task (interactive wizard, or
**Manage saved tasks → Toggle incremental copy**) so every run only appends new
rows, or force it for a single run with `--incremental`. Tasks marked
incremental show a `⏩ incremental` marker in `--list-tasks`.

### Selecting tables with patterns

A task's `source_collection` can mix exact names and shell-style patterns:

```json
"source_collection": ["users", "audit*", "compliance_*", "!*_backup"]
```

- `*`, `?` and `[...]` are globs, matched case-sensitively against the
  source's tables **at run time**: the task keeps the pattern, so new tables
  are picked up on the next run without editing the settings.
- `!pattern` excludes its matches, wherever it sits in the list. A list made
  only of exclusions means "everything except".
- Matched tables keep their names on the target: leave `target_collection`
  out (or identical to `source_collection`). Positional renaming only works
  with plain names, and both lists must have the same length.
- A pattern that matches nothing aborts the run before anything is copied
  (usually a typo, or a source that returned no tables).
- The interactive wizard accepts the same syntax in multi-selection mode,
  next to numbers and ranges: `1-3,users,compliance_*,!*_backup`.
- Redis is the exception: its entries are key patterns and are passed to the
  engine as they are.

### Running commands before and after a task

A saved task can run shell commands around itself:

```json
"CMP": {
  "...": "...",
  "before_command": "echo starting",
  "after_command": ["reset-dev-passwords", "touch ~/cmp.done"]
}
```

- Each key takes a string or a list of strings; works for copy, backup and
  restore tasks.
- `before_command` fails (non-zero exit) → the task does not run.
- The task fails → `after_command` is skipped (a half-copied target is not
  something to post-process), and dbw says so.
- `after_command` fails → the task fails, so `dbw --task X -y && next` stops.
- In a list, the first failing command stops the rest.
- Commands run with `/bin/sh -c` in the current directory, output straight to
  the terminal, no timeout. **Shell aliases from `~/.zshrc` do not exist
  there**: put the command in a script on your PATH, or write it in full.
- The task summary shown before confirming lists every hook. They can also be
  set from **Manage saved tasks → Edit before/after commands**.

### Direct Command Line

```bash
# MongoDB copy
dbw -s mongodb://source-server -t mongodb://target-server \
    --source-db myapp --drop-target -y

# MySQL copy
dbw -s mysql://user:pass@remote:3306/production \
    -t mysql://root@localhost/production \
    --source-db production --drop-target -y

# Schema only (structure/DDL, no data) - MySQL/Postgres
dbw -s mysql://user:pass@remote:3306/shop \
    -t mysql://root@localhost/shop \
    --source-db shop --schema-only -y

# Incremental: only rows above the target watermark - MySQL/MongoDB
# For append-only data. Does NOT detect updates or deletes.
dbw -s mysql://user:pass@remote:3306/shop \
    -t mysql://root@localhost/shop \
    --source-db shop --source-collection events --incremental -y
```

See [docs/INCREMENTAL_COPY.md](docs/INCREMENTAL_COPY.md) for how the watermark
is chosen, why the resume point overlaps, and what the flag refuses to do.

### Backup & Restore

```bash
# Backup to local directory
dbw --backup mongodb://localhost/myapp --backup-to /var/backups

# Backup to SSH storage
dbw --backup mongodb://localhost/production --backup-to ssh://backup@server:/backups

# Restore
dbw --restore /backups/2025_01_15-production.tar.gz --restore-to mongodb://localhost
```

## CLI Commands

| Command | Alias |
|---------|-------|
| `db-wizard` | Full name |
| `dbw` | Primary short alias |

## Main Options

| Option | Description |
|--------|-------------|
| `-s, --source` | Source database URI (`mongodb://` or `mysql://`) |
| `-t, --target` | Target database URI |
| `--source-db` | Source database name |
| `--target-db` | Target database name (defaults to source-db) |
| `--source-collection` | Specific collection/table (omit for all) |
| `--drop-target` | Drop target before copying |
| `--schema-only` | Copy only structure/DDL, no data (MySQL/Postgres only; aborts on MongoDB/Redis) |
| `--incremental` | Copy only rows above the target watermark, for append-only data (MySQL/MongoDB). Does NOT detect updates or deletes |
| `--since-column` | Watermark column for `--incremental` (default: `AUTO_INCREMENT` column / `_id`) |
| `-y, --yes` | Full automation without prompts |
| `--verify` | Verify integrity after copy (MongoDB) |
| `--force-python` | Force Python copy instead of mongodump (MongoDB) |
| `--list-tasks` | Show saved tasks |
| `--list-hosts` | Show saved hosts |
| `-c, --count` | Count rows when listing tasks/hosts (slow on remote) |
| `--task <name>` | Execute saved task |
| `--verify-connection` | Test connection to a URI |
| `--backup` | Backup database |
| `--backup-to` | Backup destination (path or `ssh://` or `ftp://`) |
| `--restore` | Restore from backup file |
| `--restore-to` | Restore target URI |

## SSH Tunnels

Hosts behind SSH can be reached automatically. When adding a host in the wizard:

1. Enter the database URI (e.g., `mysql://user:pass@localhost:3306/db`)
2. Enable SSH tunnel
3. Enter SSH hostname (from `~/.ssh/config`) or full `user@host` details

The tunnel opens automatically when you select the host, forwards a random local port, and closes when db-wizard exits.

Tunnel config is saved with the host:
```json
{
  "hosts": {
    "production": {
      "uri": "mysql://user:pass@localhost:3306/mydb",
      "ssh_tunnel": "production-server"
    }
  }
}
```

## Configuration

Settings are saved in `~/.db_wizard_settings.json` (auto-migrated from `~/.mongo_wizard_settings.json`).

File permissions are set to 600 (owner-only) because it contains credentials.

```json
{
  "hosts": {
    "local_mongo": "mongodb://localhost:27017",
    "local_mysql": "mysql://root@localhost:3306",
    "remote_db": {
      "uri": "mysql://user:pass@localhost:3306/db",
      "ssh_tunnel": "myserver"
    }
  },
  "tasks": {
    "sync_staging": {
      "source_uri": "mongodb://production:27017",
      "target_uri": "mongodb://staging:27017",
      "source_db": "app",
      "target_db": "app",
      "drop_target": true
    }
  },
  "storages": {
    "backup_server": {
      "type": "ssh",
      "host": "backup.server.com",
      "user": "backup",
      "port": 22,
      "path": "/backups"
    }
  }
}
```

## System Requirements

- Python 3.11+
- For MongoDB: `mongodump`/`mongorestore` (install with `brew install mongodb-database-tools`)
- For MySQL: `mysqldump`/`mysql` CLI tools (install with `brew install mysql-client`)
- For PostgreSQL: `pg_dump`/`pg_restore`/`psql` CLI tools (install with `brew install postgresql`)
- For Redis: `redis-cli` (install with `brew install redis`)

The wizard checks requirements at startup and shows what's available.

## Testing

```bash
# All unit tests (no database server needed)
pytest tests/ --ignore=tests/test_integration.py --ignore=tests/test_full_integration.py -v

# With coverage
pytest --cov=db_wizard --cov-report=html tests/

# Integration tests (requires MongoDB on localhost:27017)
pytest tests/test_full_integration.py -v
```

162 unit tests covering: engine abstraction, MongoDB engine, MySQL engine, SSH tunnels, settings migration, storage backends, formatting, utilities, bug fixes.

## Architecture

```
db_wizard/
    engine.py           # DatabaseEngine ABC + EngineFactory
    engines/
        mongo.py        # MongoEngine (pymongo + mongodump/mongorestore)
        mysql.py        # MySQLEngine (mysqldump/mysql CLI)
        postgres.py     # PostgresEngine (pg_dump/psql CLI)
        redis.py        # RedisEngine (redis-cli)
    flows/              # Wizard interaction flows (copy, backup, etc.)
    tunnel.py           # SSH tunnel manager (auto port-forward)
    wizard.py           # Interactive wizard entrypoint (engine-agnostic)
    cli.py              # Click CLI (auto-detects engine from URI)
    backup.py           # Backup/restore manager
    task_runner.py      # Saved task executor
    settings.py         # JSON config manager (~/.db_wizard_settings.json)
    storage.py          # Storage backends (Local, SSH/SCP, FTP)
```

The engine abstraction (`DatabaseEngine` ABC) allows adding new database engines by implementing a single interface. The wizard, CLI, backup manager, and task runner are all engine-agnostic.
