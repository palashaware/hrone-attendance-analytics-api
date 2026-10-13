# Decisions

1. **Indexes.** I created unique and query-friendly indexes on employee codes and attendance day keys. The most important are the unique index on employees.emp_code, the unique composite index on attendance_logs.emp_code + date, and the date-based index for sorting and filtering. I did not keep a broad, low-selectivity index on status alone because it would not help the main queries enough to justify the extra write cost.

2. **Punch-in race.** Two identical punch-in requests hit at the same moment. The code tries a single insert into the attendance_logs collection with a unique compound key on employee plus attendance date. If the first insert succeeds, the second raises a duplicate-key error and returns 409. That gives exactly one 201 and the rest 409 without relying on a brittle check-then-insert flow.

3. **Ties.** In the late leaderboard, employees with the same total late minutes share the same rank, and the next rank is skipped. The code keeps the standard competition ranking by comparing totals and assigning the same rank to ties. This means a limit like 10 can return more than 10 rows if several people tie at the cutoff.

4. **Headcount.** Department summary starts from the employee list for the month and counts every employee whose joined_on is on or before the end of the month, even if they have no attendance log. This makes zero-log employees visible in headcount, while attendance records only contribute to present/late aggregates when they exist.

5. **If serving 100x the data.** I would move the most expensive analytical logic into MongoDB aggregation pipelines and add a small pre-aggregation summary collection for month-level statistics. That reduces Python-side scanning and keeps the database doing the heavy lifting for large datasets.
