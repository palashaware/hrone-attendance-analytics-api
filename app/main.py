
import logging
import os
import re
import calendar
from collections import deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from pymongo.collection import ReturnDocument

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer in epoch milliseconds")
    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "Timestamp is outside the permitted range")
    return datetime.fromtimestamp(value // 1000, UTC)


def datetime_to_epoch_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def trunc_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_ist(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def round_half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_datetime(day: date, shift_time: str) -> datetime:
    hour, minute = map(int, shift_time.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def shift_start_for_punch(punch_in: datetime, employee: dict) -> datetime:
    local = as_ist(punch_in)
    day = local.date()
    start_text = employee["shift_start"]
    end_text = employee["shift_end"]
    start_h, start_m = map(int, start_text.split(":"))
    end_h, end_m = map(int, end_text.split(":"))
    overnight = (end_h, end_m) <= (start_h, start_m)

    if overnight and (local.hour, local.minute) < (end_h, end_m):
        day -= timedelta(days=1)

    return shift_datetime(day, start_text)


def attendance_date_for_punch(punch_in: datetime, employee: dict) -> str:
    local = as_ist(punch_in)
    start_h, start_m = map(int, employee["shift_start"].split(":"))
    end_h, end_m = map(int, employee["shift_end"].split(":"))
    overnight = (end_h, end_m) <= (start_h, start_m)

    day = local.date()
    if overnight and (local.hour, local.minute) < (end_h, end_m):
        day -= timedelta(days=1)

    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    local = as_ist(punch_in)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    overnight = (end_h, end_m) <= (start_h, start_m)

    day = local.date()
    if overnight and (local.hour, local.minute) < (end_h, end_m):
        day -= timedelta(days=1)

    start = shift_datetime(day, shift_start)
    if local <= start + timedelta(minutes=10):
        return 0

    elapsed_seconds = (local - start).total_seconds()
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: str,
                     shift_start: str) -> int:
    day = date.fromisoformat(attendance_day)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_day = day + (timedelta(days=1) if (eh, em) <= (sh, sm) else timedelta())
    end = shift_datetime(end_day, shift_end).astimezone(UTC)
    seconds = int((as_utc(punch_out) - end).total_seconds())
    return seconds // 60 if seconds >= 1800 else 0


def recompute_fields(doc: dict, employee: dict) -> dict:
    pin = doc.get("punch_in")
    pout = doc.get("punch_out")

    if pin is not None:
        doc["late_minutes"] = compute_late_minutes(
            pin, employee["shift_start"], employee["shift_end"]
        )
    else:
        doc["late_minutes"] = 0

    if pin is not None and pout is not None:
        hours = compute_work_hours(pin, pout)
        doc["work_hours"] = hours
        doc["overtime_minutes"] = compute_overtime(
            pout, employee["shift_end"], doc["date"], employee["shift_start"]
        )
        doc["half_day"] = hours < 4.50
    else:
        doc["work_hours"] = None
        doc["overtime_minutes"] = 0
        doc["half_day"] = False

    return doc


def attendance_response(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        result[key] = datetime_to_epoch_ms(result.get(key))

    for entry in result.get("history", []):
        entry["at"] = datetime_to_epoch_ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = datetime_to_epoch_ms(change[side])
    return result


def employee_response(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = datetime_to_epoch_ms(result["created_at"])
    return result


def date_range(month: str):
    first = date.fromisoformat(month + "-01")
    last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    return first, last


def weekdays_between(first: date, last: date) -> int:
    count = 0
    current = first
    while current <= last:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def is_present_status(status: str) -> bool:
    return status in ("PRESENT", "WFH", "ON_DUTY")


def valid_month(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    return date_range(month)


def validate_epoch(value):
    if value is not None:
        epoch_ms_to_datetime(value)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: PresenceStatus = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        validate_epoch(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        validate_epoch(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamp(cls, value):
        validate_epoch(value)
        return value


# ---------------------------------------------------------------------------
# Startup / indexes
# ---------------------------------------------------------------------------

@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)]
    )


# ---------------------------------------------------------------------------
# System and employees
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = doc["joined_on"].isoformat()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING)
    docs = docs.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_response(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    ts = trunc_seconds(epoch_ms_to_datetime(body.punched_at)) if body.punched_at is not None else now_utc()
    attendance_day = attendance_date_for_punch(ts, emp)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date")
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    ts = trunc_seconds(epoch_ms_to_datetime(body.punched_at)) if body.punched_at is not None else now_utc()

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "This record is already punched out")

    elapsed = (ts - as_utc(record["punch_in"])).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")

    updated = dict(record)
    updated["punch_out"] = ts
    recompute_fields(updated, emp)

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": updated["work_hours"],
            "overtime_minutes": updated["overtime_minutes"],
            "half_day": updated["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "This record was punched out concurrently")
    return attendance_response(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()

    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    docs = docs.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [attendance_response(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(emp_code: str, attendance_date: date, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": attendance_date.isoformat()}
    )
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    changes_requested = body.model_dump(exclude={"reason", "regularized_by"}, exclude_unset=True)
    if not changes_requested:
        raise HTTPException(422, "No changes supplied")

    old = dict(record)
    new = dict(record)
    for field, value in changes_requested.items():
        if field in ("punch_in", "punch_out"):
            value = epoch_ms_to_datetime(value) if value is not None else None
        if value is not None or field in body.model_fields_set:
            new[field] = value

    if new.get("status") in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_requested or "punch_out" in changes_requested:
            raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
        new["punch_in"] = None
        new["punch_out"] = None
    elif is_present_status(new.get("status", "")) and new.get("punch_in") is None:
        raise HTTPException(422, "Presence status requires punch_in")

    if new.get("punch_in") is not None:
        expected_date = attendance_date_for_punch(new["punch_in"], emp)
        if expected_date != attendance_date.isoformat():
            raise HTTPException(422, "punch_in must belong to the record's attendance date")

    if new.get("punch_out") is not None:
        if new.get("punch_in") is None:
            raise HTTPException(422, "punch_out requires punch_in")
        seconds = (as_utc(new["punch_out"]) - as_utc(new["punch_in"])).total_seconds()
        if seconds <= 0 or seconds > 24 * 3600:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    recompute_fields(new, emp)

    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
               "overtime_minutes", "half_day")
    actual_changes = {}
    for field in tracked:
        if old.get(field) != new.get(field):
            actual_changes[field] = {
                "from": old.get(field),
                "to": new.get(field),
            }

    if not actual_changes:
        raise HTTPException(422, "Request does not change the record")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": actual_changes,
    }

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        {
            "$set": {
                "status": new["status"],
                "punch_in": new.get("punch_in"),
                "punch_out": new.get("punch_out"),
                "work_hours": new["work_hours"],
                "late_minutes": new["late_minutes"],
                "overtime_minutes": new["overtime_minutes"],
                "half_day": new["half_day"],
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "Record changed concurrently; retry the request")
    return attendance_response(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def month_records(month: str, emp_code=None):
    first, last = valid_month(month)
    q = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}
    if emp_code is not None:
        q["emp_code"] = emp_code
    return first, last, list(db.attendance_logs.find(q, {"_id": 0}))


def employee_present_days(records):
    total = 0.0
    for rec in records:
        try:
            day = date.fromisoformat(rec["date"])
        except Exception:
            continue
        if day.weekday() < 5 and is_present_status(rec.get("status", "")):
            total += 0.5 if rec.get("half_day", False) else 1.0
    return total


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last = valid_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    joined = date.fromisoformat(emp["joined_on"])
    working_start = max(first, joined)
    working_days = weekdays_between(working_start, last) if working_start <= last else 0

    records = list(db.attendance_logs.find({
        "emp_code": emp_code,
        "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
    }, {"_id": 0}))

    present_days = employee_present_days(records)
    leave_days = sum(r.get("status") == "LEAVE" for r in records)
    late_count = sum(r.get("late_minutes", 0) > 0 for r in records)
    late_minutes = sum(r.get("late_minutes", 0) for r in records)
    overtime = sum(r.get("overtime_minutes", 0) for r in records)
    pct = round_half_up(present_days / working_days * 100, 2) if working_days else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": late_minutes,
        "total_overtime_minutes": overtime,
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = valid_month(month)
    employee_filter = {}
    if department is not None:
        employee_filter["department"] = department

    employees = list(db.employees.find(employee_filter, {"_id": 0}))
    records = list(db.attendance_logs.find({
        "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}
    }, {"_id": 0}))

    valid_employees = []
    for emp in employees:
        emp_code = emp.get("emp_code")
        department_name = emp.get("department")
        joined_on = emp.get("joined_on")

        if not isinstance(emp_code, str) or not re.fullmatch(r"EMP\d{4,6}", emp_code):
            logger.warning("Skipping employee with invalid emp_code in department summary")
            continue
        if not isinstance(department_name, str) or not department_name:
            logger.warning("Skipping employee %s with invalid department", emp_code)
            continue
        if not isinstance(joined_on, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", joined_on):
            logger.warning("Skipping employee %s with invalid joined_on", emp_code)
            continue
        try:
            joined = date.fromisoformat(joined_on)
        except ValueError:
            logger.warning("Skipping employee %s with invalid joined_on", emp_code)
            continue

        valid_employees.append((emp, joined))

    by_code = {emp["emp_code"]: emp for emp, _ in valid_employees}
    grouped = {}

    for emp, joined in valid_employees:
        if joined <= last:
            grouped.setdefault(emp["department"], {
                "department": emp["department"], "headcount": 0,
                "present_days": 0.0, "hours": [], "late_count": 0,
                "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0,
            })
            grouped[emp["department"]]["headcount"] += 1

    for rec in records:
        emp = by_code.get(rec.get("emp_code"))
        if not emp:
            continue
        dept = emp["department"]
        if department is not None and dept != department:
            continue
        if dept not in grouped:
            continue
        row = grouped[dept]
        try:
            weekday = date.fromisoformat(rec["date"]).weekday() < 5
        except Exception:
            weekday = False

        if weekday and is_present_status(rec.get("status", "")):
            row["present_days"] += 0.5 if rec.get("half_day", False) else 1.0
        if rec.get("work_hours") is not None and is_present_status(rec.get("status", "")):
            row["hours"].append(rec["work_hours"])
        if rec.get("late_minutes", 0) > 0:
            row["late_count"] += 1
            row["total_late_minutes"] += rec["late_minutes"]
        if rec.get("status") == "LEAVE":
            row["leave_count"] += 1
        if rec.get("status") == "ON_DUTY":
            row["on_duty_count"] += 1

    items = []
    for dept in sorted(grouped):
        row = grouped[dept]
        hours = row.pop("hours")
        row["avg_work_hours"] = round_half_up(sum(hours) / len(hours), 2) if hours else None
        items.append(row)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = valid_month(month)
    employees = list(db.employees.find({}, {"_id": 0}))
    employee_by_code = {e["emp_code"]: e for e in employees}

    query = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
             "late_minutes": {"$gt": 0}}
    records = list(db.attendance_logs.find(query, {"_id": 0}))

    totals = {}
    for rec in records:
        emp = employee_by_code.get(rec.get("emp_code"))
        if emp is None:
            continue
        if department is not None and emp.get("department") != department:
            continue
        code = emp["emp_code"]
        row = totals.setdefault(code, {
            "emp_code": code, "name": emp["name"], "department": emp["department"],
            "total_late_minutes": 0, "late_count": 0,
        })
        row["total_late_minutes"] += rec.get("late_minutes", 0)
        row["late_count"] += 1

    ranked = sorted(totals.values(), key=lambda r: (-r["total_late_minutes"], r["emp_code"]))
    last_total = None
    rank = 0
    for index, row in enumerate(ranked, start=1):
        if row["total_late_minutes"] != last_total:
            rank = index
            last_total = row["total_late_minutes"]
        row["rank"] = rank

    return {"month": month, "items": [r for r in ranked if r["rank"] <= limit]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: date = Query(..., alias="from"),
                     to_date: date = Query(..., alias="to")):
    if to_date < from_date:
        raise HTTPException(422, "'to' must be on or after 'from'")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    employees = list(db.employees.find({"department": department}, {"_id": 0}))
    if not employees:
        raise HTTPException(404, "Department not found")

    emp_codes = [e["emp_code"] for e in employees]
    pipeline = [
        {"$match": {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": ["$half_day", 0.5, 1.0]}, 0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    day_rows = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    items = []
    window = deque(maxlen=7)
    current = from_date
    while current <= to_date:
        key = current.isoformat()
        working = current.weekday() < 5
        headcount = sum(date.fromisoformat(e["joined_on"]) <= current for e in employees)
        raw = day_rows.get(key, {})
        present_count = raw.get("present_count", 0.0)
        late_count = raw.get("late_count", 0)
        rate = round_half_up(present_count / headcount, 4) if working and headcount > 0 else None
        window.append(rate)

        valid_window = [v for v in window if v is not None]
        moving = round_half_up(sum(valid_window) / len(valid_window), 4) if valid_window else None

        items.append({
            "date": key,
            "is_working_day": working,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": moving,
        })
        current += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin explain endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            q["status"] = status
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from.isoformat()
            if date_to:
                q["date"]["$lte"] = date_to.isoformat()
        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first, last = valid_month(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {
                "$gte": first.isoformat(), "$lte": last.isoformat()
            }}},
            {"$sort": {"date": 1}},
        ]
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = valid_month(month)
        pipeline = [{"$match": {"date": {
            "$gte": first.isoformat(), "$lte": last.isoformat()
        }}}]
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = valid_month(month)
        pipeline = [{"$match": {
            "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
            "late_minutes": {"$gt": 0},
        }}]
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        employees = list(db.employees.find({"department": department}, {"emp_code": 1}))
        codes = [e["emp_code"] for e in employees]
        pipeline = [{"$match": {
            "emp_code": {"$in": codes},
            "date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()},
        }}]
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    raise HTTPException(422, "Unknown endpoint")
