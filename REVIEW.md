# REVIEW.md

While reviewing the attendance API, I checked the leaderboard implementation and the MongoDB index setup. A duplicate definition of `late_leaderboard()` can cause one implementation to override another, making the actual behavior difficult to track. I checked for repeated definitions and kept the correct implementation where necessary.

I also reviewed the indexes used by employee and attendance queries. Without suitable indexes, frequently filtered or sorted queries can become slower as the database grows. I added indexes for employee lookup, attendance record uniqueness, date and status filtering, and department-related queries where needed. The query plans can be checked using MongoDB's execution statistics to verify whether the expected indexes are being used.

I also checked how the API handles attendance rates when there are no working-day records. A zero rate or a null rate can be valid, depending on the calculation and available data, so these cases should not automatically be treated as bugs. Similarly, the unique compound index on `emp_code` and `date` is intentional because an employee should have at most one attendance record per day.

These notes describe the issues I reviewed and the reasoning behind the changes. Any issue listed as fixed should be confirmed against the original starter code and the final implementation.