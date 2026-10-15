# DECISIONS.md  (about 150-200 words in total - plain language, your own words)


1. Indexes: I created a unique index on `emp_code` so an employee cannot be registered twice. A unique compound index on `emp_code` and `date` prevents duplicate attendance records for the same employee on the same day. I added indexes on `date`, `status` with `date`, `department` with `joined_on`, and `date` with `emp_code` to support attendance filters, department queries, sorting, and analytics. I considered adding more indexes for every filter combination, but rejected that because extra indexes increase storage and write costs.

2. Punch-in race: Two identical requests can arrive before either finishes. The unique compound attendance index is the database-level protection against two records for the same employee and date. The exact response to the losing request depends on how the endpoint handles duplicate-key errors.

3. Ties: The leaderboard sorts by total late minutes in descending order and uses employee code as a secondary sort key. It returns up to the requested limit, giving tied employees a deterministic order rather than including every employee tied at the cutoff.

4. Headcount: The department summary starts from employees and looks up their attendance logs. Employees without matching logs remain in the aggregation and can still be counted.

5. One improvement: For 100 times more data, I would review query plans with realistic data, optimize aggregation pipelines, and add pagination or caching where appropriate.