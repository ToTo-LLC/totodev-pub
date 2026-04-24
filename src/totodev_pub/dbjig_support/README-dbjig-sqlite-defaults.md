# DbJig SQLite Defaults and Overrides

`DbJig` now applies SQLite concurrency-oriented defaults on every connection it opens.

## Default behavior

Unless overridden, `DbJig` applies these pragmas:

- `foreign_keys = ON`
- `journal_mode = WAL`
- `synchronous = NORMAL`
- `busy_timeout = 5000`

These settings are applied for:

- the managed connection returned by `DbJig.db()`
- ad-hoc connections from `DbJig.make_conn()`
- internal bootstrap/validation connections used during open/create flows

## Why this helps

- `WAL` allows readers and writers to proceed with less blocking than rollback journal mode.
- `busy_timeout` reduces immediate lock failures during short contention windows.
- `synchronous = NORMAL` is a common WAL pairing for improved write throughput.
- `foreign_keys = ON` keeps constraint enforcement enabled per connection.

## Overriding defaults

Pass `sqlite_pragmas` to the `DbJig` constructor. Caller values are merged over library defaults.

```python
from totodev_pub.dbjig import DbJig

dbj = DbJig(
    "app.sqlite",
    loadsources=["migrations/*"],
    sqlite_pragmas={
        "journal_mode": "DELETE",  # disable WAL
        "synchronous": "FULL",
        "busy_timeout": 10000,
    },
)
```

## Notes

- `journal_mode` is a file-level SQLite mode, so setting WAL affects the database file.
- `DbJig.copy()` preserves effective `sqlite_pragmas` so threaded copies keep the same behavior.
