# Incremental Copy (`--incremental`)

Copies only the rows the target does not have yet, instead of transferring the
whole table on every run. Built for **append-only** data: event logs, metrics,
audit trails, anything where rows are inserted and never touched again.

## What it is not

This is **not a diff**. It never detects `UPDATE`s or `DELETE`s on rows that
were already copied. A real diff would have to read both sides and compare them
row by row, which costs as much as copying everything — the saving would be
bandwidth only, not time.

If your data mutates after insertion, use a full copy, or replication at the
binlog/WAL level. If you enable `--incremental` on a mutating table, the target
will silently drift out of sync and nothing will warn you.

## How it works

Two questions, asked per table:

1. **Which column grows?** The `AUTO_INCREMENT` primary key on MySQL, `_id` on
   MongoDB. Override with `--since-column`.
2. **How far did the target get?** `MAX(column)` on the **target** side.

Everything above that value is dumped from the source and appended to the
target. On MySQL that is `mysqldump --where='id > N' --no-create-info
--insert-ignore`; on MongoDB it is a filtered `find()` with
`insert_many(ordered=False)`.

### The overlap

The resume point is deliberately set slightly **below** `MAX(column)`:

| Watermark type | Step back | Comparison |
|----------------|-----------|------------|
| Integer (`AUTO_INCREMENT`, numeric `_id`) | 10 rows | `>` |
| ObjectId | 5 seconds | `>` |
| Datetime | 5 seconds | `>` |
| Anything else (strings) | none | `>=` |

This is not paranoia. With `AUTO_INCREMENT`, ids are handed out at statement
time but rows become visible at commit time, so a row with id 105 can appear
*after* id 107 is already committed. Resuming exactly at `MAX(id)` would skip
that row permanently. With MongoDB the same happens for a different reason:
ObjectIds are generated client-side, so clock skew between application servers
reorders them.

The re-read rows collide on the primary key and are discarded — `INSERT IGNORE`
on MySQL, a duplicate `_id` on MongoDB. The copy is therefore **idempotent**:
running it twice in a row copies zero rows the second time.

## Usage

```bash
# Single table
dbw -s mysql://user:pass@remote/shop -t mysql://root@localhost/shop \
    --source-db shop --source-collection events --incremental -y

# Every table in the database (each gets its own watermark)
dbw -s mysql://user:pass@remote/shop -t mysql://root@localhost/shop \
    --source-db shop --incremental -y

# Resume on a timestamp instead of the primary key
dbw -s mysql://user:pass@remote/shop -t mysql://root@localhost/shop \
    --source-db shop --source-collection events \
    --incremental --since-column created_at -y

# MongoDB
dbw -s mongodb://source -t mongodb://target \
    --source-db analytics --source-collection pageviews --incremental -y
```

### Saved tasks

A task can carry the flag permanently, so every run is incremental:

```bash
dbw --task nightly_events        # runs incrementally if the task has the flag
dbw --task nightly_events --incremental   # force it for this run only
```

To flip an existing task: **Manage saved tasks → Toggle incremental copy**. The
interactive copy wizard also offers it when creating a new task. Tasks marked
incremental are listed with a `⏩ incremental` marker in `--list-tasks`.

Enabling incremental on a task that drops the target turns dropping **off** —
the two cannot coexist (see below).

## Rules and refusals

The flag refuses to run rather than doing something surprising:

| Situation | Behaviour |
|-----------|-----------|
| `--drop-target` also set | Refused. Dropping erases the watermark, turning every run into a full copy. |
| `--schema-only` also set | Refused. Schema-only copies no rows at all. |
| PostgreSQL | Refused. `pg_dump` cannot filter rows; it would need a separate `COPY (SELECT ... WHERE ...)` pathway. |
| Redis | Refused. Keys are unordered, so there is no watermark. |
| No `AUTO_INCREMENT` column and no `--since-column` | Table skipped with an explicit error. |
| MongoDB `_id` is a UUID or string | Collection skipped: string ids have no ordering, so resuming on them would copy a random subset. |
| Target table renamed (`events` → `events_copy`) | Refused. The dump emits `INSERT`s naming the source table and there is no `CREATE TABLE` to rewrite them against. |
| Target table missing or empty | Falls back to a **full copy** of that table. The next run resumes incrementally. |

When copying a whole database, a table that gets skipped does not abort the
others; the summary reports which ones were skipped.

## Choosing a watermark column

Auto-detection only accepts columns the database itself guarantees to grow.
Anything else must be named explicitly with `--since-column`, and it is on you
to make sure it is monotonic:

- **Good**: `AUTO_INCREMENT` ids, `created_at` set by the database on insert,
  ObjectIds.
- **Bad**: `updated_at` (moves backwards relative to insertion order),
  application-supplied timestamps from multiple machines, UUIDs.

## Engine support

| Engine | Supported | Mechanism |
|--------|-----------|-----------|
| MySQL | Yes | `mysqldump --where` + `--insert-ignore` |
| MongoDB | Yes | filtered `find()` + `insert_many(ordered=False)` |
| PostgreSQL | No | `pg_dump` cannot filter rows |
| Redis | No | keys are unordered |

## Implementation notes

The abstraction lives on `DatabaseEngine` (`db_wizard/engine.py`):

```python
supports_incremental: bool = False
def detect_watermark_column(self, database, table) -> str | None
def max_watermark(self, database, table, column) -> Any | None
```

Engines that can filter rows override the three; the rest inherit the defaults
and the copy layer refuses the flag. `copy()` receives `incremental=` and
`since_column=` through the same kwargs channel as `schema_only`.

Overlap sizes are `INCREMENTAL_OVERLAP_ROWS` and `INCREMENTAL_OVERLAP_SECONDS`
in `db_wizard/constants.py`.
