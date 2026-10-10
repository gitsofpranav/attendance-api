# Employee Attendance & Analytics API

A FastAPI and MongoDB service for employee attendance tracking, regularization audit trails, and aggregation-driven analytics.

## Tech Stack
- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+ (Atlas or local instance)

## Setup & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables:**
   Configure `MONGO_URI` and `MONGO_DB` (falls back to `mongodb://localhost:27017` and `attendance_db` by default):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Start the API server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   All database indexes are created idempotently on startup.

4. **Verify Health:**
   ```bash
   curl http://localhost:8000/health
   ```

## Key Highlights & Implementation Details
- **Part A (Review & Corrections):** Identified and resolved 20 defects across helpers, validation models, pagination, concurrency checks, and memory management. Documented in `REVIEW.md`.
- **Part B (Attendance Workflows & Business Rules):** Full implementation of Rules R1–R5 including overnight shifts across midnight, exact Decimal round-half-up work hours, overtime thresholds, and optimistic-locking audit trail regularization.
- **Part C (Database-Level Analytics & Index Optimization):**
  - Cross-collection department summaries accounting for zero-log employees (R9).
  - Late leaderboard with standard competition ranking (`$rank`) and tie-handling.
  - Department trend series with `$densify` calendar gap-filling and 7-day moving averages.
  - `/admin/explain/{endpoint}` running `executionStats` proving `IXSCAN` operations with zero `COLLSCAN`.
- Design decisions and architecture rationales are documented in `DECISIONS.md`.
