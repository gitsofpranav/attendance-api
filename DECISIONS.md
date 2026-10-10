# DECISIONS.md

### 1. Indexes
We created:
- `employees`: Unique index `{ emp_code: 1 }` ensuring identity uniqueness; compound `{ department: 1, joined_on: 1 }` and `{ department: 1, emp_code: 1 }` to accelerate department lookups and sorting.
- `attendance_logs`: Unique compound `{ emp_code: 1, date: 1 }` guaranteeing one record per employee-day and backing monthly aggregations; `{ date: -1, emp_code: 1 }` for list pagination sorting; `{ date: 1, late_minutes: 1 }` for late leaderboard scans; `{ date: 1, status: 1 }` for status filtering.
- *Rejected*: A standalone index on `{ status: 1 }` because its low cardinality provides poor index selectivity compared to date-compound filtering.

### 2. Punch-in race
Both concurrent requests derive the same attendance date and attempt `db.attendance_logs.insert_one()`. Because of the unique compound index on `(emp_code, date)`, MongoDB's internal document lock ensures only one insert succeeds (yielding HTTP 201). The second concurrent insert violates the unique constraint, raising `pymongo.errors.DuplicateKeyError`, which our handler catches to return HTTP 409. No check-then-insert race condition exists.

### 3. Ties
The leaderboard computes standard competition ranking via MongoDB's `$setWindowFields` with the `$rank` operator ordered by `total_late_minutes` descending. When employees tie at the cutoff (e.g., ranks 1, 2, 2), both receive rank 2 (skipping 3). The filter `{ rank: { $lte: limit } }` executes *after* ranking, so both tied employees are returned (yielding more rows than `limit`), ordered secondarily by `emp_code` ascending.

### 4. Headcount
The department summary aggregation originates on the `employees` collection filtered by `{ joined_on: { $lte: end_date } }` rather than `attendance_logs`. We count headcount directly as `{ $sum: 1 }` per employee and join their attendance via a correlated `$lookup` subpipeline. Employees with zero attendance logs simply receive an empty logs array (`[]`), ensuring they are fully counted.

### 5. One thing you would change for 100x the data
At 100x data (~10 million logs), running multi-stage aggregations on demand adds significant latency. I would implement an incremental daily rollup / materialized summary collection updated during punch-out and regularization events, turning analytical summaries into sub-millisecond point queries.
