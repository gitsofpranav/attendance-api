"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    # employees indexes
    db.employees.create_index([("emp_code", pymongo.ASCENDING)], unique=True)
    db.employees.create_index([("department", pymongo.ASCENDING), ("emp_code", pymongo.ASCENDING)])
    db.employees.create_index([("joined_on", pymongo.ASCENDING)])

    # attendance_logs indexes
    # Natural key unique compound index
    db.attendance_logs.create_index(
        [("emp_code", pymongo.ASCENDING), ("date", pymongo.ASCENDING)],
        unique=True,
    )
    # Fast attendance list sorting (date desc, emp_code asc)
    db.attendance_logs.create_index(
        [("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)]
    )
    # Employee date lookup
    db.attendance_logs.create_index(
        [("emp_code", pymongo.ASCENDING), ("date", pymongo.DESCENDING)]
    )
    # Date range and status filtering
    db.attendance_logs.create_index(
        [("date", pymongo.ASCENDING), ("status", pymongo.ASCENDING)]
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R5)
# --------------------------------------------------------------------------- #
def truncate_epoch_to_seconds(epoch_ms: int) -> int:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    return (epoch_ms // 1000) * 1000


def epoch_to_utc_datetime(epoch_ms: int) -> datetime:
    """Convert truncated epoch ms to UTC timezone-aware datetime."""
    truncated = truncate_epoch_to_seconds(epoch_ms)
    return datetime.fromtimestamp(truncated / 1000.0, tz=timezone.utc)


def utc_datetime_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def determine_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): punch-in strictly earlier than shift_end
    belongs to the shift that started the previous day.
    """
    punch_ist = punch_in_dt.astimezone(IST)
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))

    if (h_end, m_end) <= (h_start, m_start):  # overnight shift
        if punch_ist.time() < time(h_end, m_end):
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Late. Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start, not since grace ended.
    """
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(date_str).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    diff_seconds = (punch_in_dt - shift_start_dt).total_seconds()
    if diff_seconds > 600:  # strictly more than 10 minutes
        return int(diff_seconds // 60)
    return 0


def compute_overtime(
    punch_out_dt: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    d = datetime.fromisoformat(date_str).date()
    if (h_end, m_end) <= (h_start, m_start):  # overnight shift ends on next calendar day
        d = d + timedelta(days=1)
    shift_end_dt = datetime.combine(d, time(h_end, m_end)).replace(tzinfo=IST)
    diff_seconds = (punch_out_dt - shift_end_dt).total_seconds()
    if diff_seconds >= 1800:  # at least 30 minutes
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: Work hours. (punch_out - punch_in) in seconds / 3600,
    rounded to 2 decimals half-up.
    """
    sec = Decimal(str((punch_out_dt - punch_in_dt).total_seconds()))
    hours = sec / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: Half day. After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = utc_datetime_to_epoch(from_val)
            if isinstance(to_val, datetime):
                to_val = utc_datetime_to_epoch(to_val)
        changes[field] = {"from": from_val, "to": to_val}
    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = utc_datetime_to_epoch(at_val)
    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def format_attendance_doc(doc: dict) -> dict:
    """Format MongoDB attendance document to OpenAPI AttendanceRecord response."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_datetime_to_epoch(doc.get("punch_in")),
        "punch_out": utc_datetime_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [format_history_entry(h) for h in doc.get("history", [])],
    }


def format_employee_doc(doc: dict) -> dict:
    """Format MongoDB employee document to OpenAPI Employee response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": utc_datetime_to_epoch(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Pydantic Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_employee(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            datetime.strptime(self.joined_on, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, ge=100000000000, le=4102444800000
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created_at = datetime.now(timezone.utc)
    doc = body.model_dump()
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", pymongo.ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        now_dt = datetime.now(timezone.utc)
        raw_ms = int(now_dt.timestamp() * 1000)
    else:
        raw_ms = body.punched_at

    truncated_ms = truncate_epoch_to_seconds(raw_ms)
    punch_in_dt = epoch_to_utc_datetime(truncated_ms)

    date = determine_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], date)

    doc = {
        "emp_code": body.emp_code,
        "date": date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None, pattern="^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - Remaining endpoints to implement in subsequent steps:
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
