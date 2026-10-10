"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
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
    db.employees.create_index([("department", pymongo.ASCENDING), ("joined_on", pymongo.ASCENDING)])
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
    # Date range and late minutes filtering for late leaderboard
    db.attendance_logs.create_index(
        [("date", pymongo.ASCENDING), ("late_minutes", pymongo.ASCENDING)]
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


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: Reported numbers rounded half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = Decimal("1" if decimals == 0 else "0." + "0" * decimals)
    return float(d.quantize(pattern, rounding=ROUND_HALF_UP))


def calculate_working_days(joined_on: str, month: str) -> int:
    """R7: Monday - Friday, no holiday calendar; counted from joined_on for mid-month joiners."""
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    month_start = date(y, m, 1)
    month_end = date(y, m, last_day)

    try:
        joined_date = date.fromisoformat(joined_on)
    except ValueError:
        return 0

    if joined_date > month_end:
        return 0

    start_date = max(month_start, joined_date)
    working_days = 0
    cur = start_date
    while cur <= month_end:
        if cur.weekday() < 5:  # Monday to Friday
            working_days += 1
        cur += timedelta(days=1)
    return working_days


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


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, ge=100000000000, le=4102444800000
    )


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


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


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        now_dt = datetime.now(timezone.utc)
        raw_ms = int(now_dt.timestamp() * 1000)
    else:
        raw_ms = body.punched_at

    truncated_ms = truncate_epoch_to_seconds(raw_ms)
    punch_out_dt = epoch_to_utc_datetime(truncated_ms)

    # Closes employee's most recent record whose punch_in is at or before punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", pymongo.DESCENDING)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_ms = utc_datetime_to_epoch(rec["punch_in"])
    if truncated_ms <= punch_in_ms:
        if rec.get("punch_out") is None:
            raise HTTPException(
                status_code=422,
                detail="punched_at must be strictly after punch_in and within 24 hours",
            )
        else:
            raise HTTPException(status_code=409, detail="Record already punched out")

    if (truncated_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    work_hours = compute_work_hours(rec["punch_in"], punch_out_dt)
    overtime_minutes = compute_overtime(
        punch_out_dt, emp["shift_start"], emp["shift_end"], rec["date"]
    )
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return format_attendance_doc(updated)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Rule: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422.
    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
        )

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in_dt = None
        target_punch_out_dt = None
    else:
        # Presence status
        if body.punch_in is not None:
            target_punch_in_dt = epoch_to_utc_datetime(body.punch_in)
        else:
            target_punch_in_dt = rec.get("punch_in")

        if body.punch_out is not None:
            target_punch_out_dt = epoch_to_utc_datetime(body.punch_out)
        else:
            target_punch_out_dt = rec.get("punch_out")

    # Rule: a presence status requires a punch_in (supplied or already stored)
    if target_status in ("PRESENT", "WFH", "ON_DUTY"):
        if target_punch_in_dt is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires punch_in",
            )

    # Rule: punch_in must stay on the record's attendance date (R1)
    if target_punch_in_dt is not None:
        calc_date = determine_attendance_date(
            target_punch_in_dt, emp["shift_start"], emp["shift_end"]
        )
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in does not belong to the record's attendance date",
            )

    # Rule: punch_out must be after punch_in and within 24 hours
    if target_punch_out_dt is not None:
        if target_punch_in_dt is None:
            raise HTTPException(
                status_code=422,
                detail="punch_out requires punch_in",
            )
        in_ms = utc_datetime_to_epoch(target_punch_in_dt)
        out_ms = utc_datetime_to_epoch(target_punch_out_dt)
        if out_ms <= in_ms or (out_ms - in_ms) > 24 * 3600 * 1000:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be strictly after punch_in and within 24 hours",
            )

    # Recompute derived fields per R2-R5
    if target_status in ("ABSENT", "LEAVE"):
        target_late_minutes = 0
        target_work_hours = None
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_late_minutes = compute_late_minutes(
            target_punch_in_dt, emp["shift_start"], date
        )
        if target_punch_out_dt is not None:
            target_work_hours = compute_work_hours(
                target_punch_in_dt, target_punch_out_dt
            )
            target_overtime_minutes = compute_overtime(
                target_punch_out_dt, emp["shift_start"], emp["shift_end"], date
            )
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Track only fields that actually changed
    old_status = rec.get("status")
    old_punch_in = rec.get("punch_in")
    old_punch_out = rec.get("punch_out")
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    changes = {}
    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if old_punch_in != target_punch_in_dt:
        changes["punch_in"] = {"from": old_punch_in, "to": target_punch_in_dt}
    if old_punch_out != target_punch_out_dt:
        changes["punch_out"] = {"from": old_punch_out, "to": target_punch_out_dt}
    if old_work_hours != target_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": target_work_hours}
    if old_late_minutes != target_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": target_late_minutes}
    if old_overtime_minutes != target_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": old_overtime_minutes,
            "to": target_overtime_minutes,
        }
    if old_half_day != target_half_day:
        changes["half_day"] = {"from": old_half_day, "to": target_half_day}

    # Rule: a request that changes nothing is 422
    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_dt = datetime.now(timezone.utc)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking on history length to prevent concurrent edits from losing audit entries
    current_history = rec.get("history") or []
    history_len = len(current_history)

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {
                "$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]
            },
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in_dt,
                "punch_out": target_punch_out_dt,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=409, detail="Concurrent modification conflict"
        )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Pipeline Builders (Shared between endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.dow", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "valid_work_hours": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$l.work_hours", None]},
                                            ]
                                        },
                                        "$$l.work_hours",
                                        None,
                                    ]
                                },
                            }
                        },
                        "as": "wh",
                        "cond": {"$ne": ["$$wh", None]},
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "all_work_hours": {"$push": "$valid_work_hours"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "flat_work_hours": {
                    "$reduce": {
                        "input": "$all_work_hours",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$flat_work_hours"}, 0]},
                        {"$avg": "$flat_work_hours"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    if department is not None:
        pipeline.append({"$match": {"department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
    dept_employees: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    start_dt = datetime.combine(d_from, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(d_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    emp_codes = [e["emp_code"] for e in dept_employees]

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date_dt": start_dt}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}
                },
                "dow": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", emp_codes]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": dept_employees,
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = calculate_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        row = res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = (
        round_half_up((present_days / working_days) * 100.0, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    pipeline = build_department_summary_pipeline(month, department)
    docs = list(db.employees.aggregate(pipeline))

    items = []
    for d in docs:
        avg_wh = round_half_up(d.get("avg_work_hours"), 2)
        items.append({
            "department": d["department"],
            "headcount": int(d["headcount"]),
            "present_days": round_half_up(d.get("present_days", 0.0), 2),
            "avg_work_hours": avg_wh,
            "late_count": int(d.get("late_count", 0)),
            "total_late_minutes": int(d.get("total_late_minutes", 0)),
            "leave_count": int(d.get("leave_count", 0)),
            "on_duty_count": int(d.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    docs = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for d in docs:
        items.append({
            "rank": int(d["rank"]),
            "emp_code": d["emp_code"],
            "name": d["name"],
            "department": d["department"],
            "total_late_minutes": int(d["total_late_minutes"]),
            "late_count": int(d["late_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from or (d_to - d_from).days > 92:
        raise HTTPException(
            status_code=422,
            detail="to must be >= from and range must not exceed 92 days",
        )

    dept_employees = list(
        db.employees.find(
            {"department": department},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
    )
    if not dept_employees:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(
        department, from_date, to_date, dept_employees
    )
    docs = list(db.employees.aggregate(pipeline))

    items = []
    for d in docs:
        rate = round_half_up(d.get("attendance_rate"), 4)
        mavg = round_half_up(d.get("moving_avg_7d"), 4)
        items.append({
            "date": d["date"],
            "is_working_day": bool(d["is_working_day"]),
            "headcount": int(d["headcount"]),
            "present_count": round_half_up(d.get("present_count", 0.0), 2),
            "late_count": int(d.get("late_count", 0)),
            "attendance_rate": rate,
            "moving_avg_7d": mavg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None),
    department: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: Optional[int] = Query(default=1, ge=1),
    page_size: Optional[int] = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
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

        skip = ((page or 1) - 1) * (page_size or 20)
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size or 20,
        }
        raw_explain = db.command({"explain": find_cmd, "verbosity": "executionStats"})
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command({"explain": agg_cmd, "verbosity": "executionStats"})
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary",
            )
        pipeline = build_department_summary_pipeline(month, department)
        agg_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command({"explain": agg_cmd, "verbosity": "executionStats"})
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command({"explain": agg_cmd, "verbosity": "executionStats"})
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend",
            )
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(
                status_code=422,
                detail="to must be >= from and range must not exceed 92 days",
            )

        dept_employees = list(
            db.employees.find(
                {"department": department},
                {"_id": 0, "emp_code": 1, "joined_on": 1},
            )
        )
        pipeline = build_department_trend_pipeline(
            department, from_date, to_date, dept_employees
        )
        agg_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command({"explain": agg_cmd, "verbosity": "executionStats"})
        collection = "employees"

    clean_explain = json.loads(
        json_util.dumps(raw_explain, json_options=json_util.RELAXED_JSON_OPTIONS)
    )
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": clean_explain,
    }


