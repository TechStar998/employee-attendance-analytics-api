"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc
)

db = client[os.getenv("MONGO_DB", "attendance_db")]

db.employees.create_index(
    [("emp_code", ASCENDING)],
    unique=True
)

db.attendance_logs.create_index(
    [("emp_code", ASCENDING), ("date", ASCENDING)],
    unique=True
)

db.attendance_logs.create_index(
    [("date", DESCENDING)]
)

db.attendance_logs.create_index(
    [("status", ASCENDING), ("date", DESCENDING)]
)
db.employees.create_index(
    [("department", ASCENDING), ("joined_on", ASCENDING)]
)

db.attendance_logs.create_index(
    [("date", DESCENDING), ("emp_code", ASCENDING)]
)


IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))

    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)

    punch_in = punch_in.astimezone(IST)

    start = punch_in.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    seconds_late = (punch_in - start).total_seconds()

    if seconds_late <= 10 * 60:
        return 0

    return int(seconds_late // 60)





def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    rounded_hours = hours.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )
    return float(rounded_hours)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str
) -> int:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    shift_date = datetime.fromisoformat(date_str).date()

    # A shift is overnight when its end time is at or before its start time.
    overnight = (end_h, end_m) <= (start_h, start_m)
    end_date = shift_date + timedelta(days=1) if overnight else shift_date

    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=IST)
    else:
        punch_out = punch_out.astimezone(IST)

    scheduled_end = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        end_h,
        end_m,
        tzinfo=IST
    )

    extra_minutes = int(
        (punch_out - scheduled_end).total_seconds() // 60
    )

    # Overtime is counted only when at least 30 full minutes are completed.
    if extra_minutes < 30:
        return 0

    return extra_minutes


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    return doc



@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    if body.status not in ["PRESENT", "ABSENT", "HALF_DAY"]:
        raise HTTPException(400, "invalid attendance status")

    if body.punched_at is not None:
        ts_utc = datetime.fromtimestamp(
            body.punched_at / 1000, tz=timezone.utc
        )
    else:
        ts_utc = datetime.now(timezone.utc)

    ts_ist = ts_utc.astimezone(IST)
    d = ts_ist.date().isoformat()

    if db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": d
    }):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts_ist, emp["shift_start"]
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    doc["id"] = str(res.inserted_id)

    doc.pop("_id", None)
    return doc
    
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        try:
            ts_out = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc
            )
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, "invalid punched_at timestamp")
    else:
        ts_out = datetime.now(timezone.utc)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts_out}
        },
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(404, "no matching punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    ts_in = record["punch_in"]

    # MongoDB may return a naive datetime; treat it as UTC.
    if ts_in.tzinfo is None:
        ts_in = ts_in.replace(tzinfo=timezone.utc)

    if ts_out <= ts_in:
        raise HTTPException(422, "punch-out must be after punch-in")

    if (ts_out - ts_in).total_seconds() > 24 * 60 * 60:
        raise HTTPException(422, "punch-out cannot be over 24 hours after punch-in")

    work_hours = compute_work_hours(ts_in, ts_out)

    overtime_minutes = compute_overtime(
        ts_out,
        emp["shift_start"],
        emp["shift_end"],
        record["date"]
    )

    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts_out,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        }
    )

    if result.modified_count != 1:
        raise HTTPException(409, "already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated["id"] = str(updated.pop("_id"))

    # Convert punch timestamps to epoch milliseconds for the API response.
    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    return updated




@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
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

    page_docs = list(
        db.attendance_logs.find(q)
        .sort("date", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for d in page_docs:
        d["id"] = str(d.pop("_id"))

    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}
    


def to_epoch_ms(value):
    """Convert a datetime to epoch milliseconds for API responses."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return int(value.timestamp() * 1000)


def attendance_response(record):
    """Prepare an attendance document for the API response."""
    record = dict(record)
    record["id"] = str(record.pop("_id"))

    for field in ("punch_in", "punch_out"):
        record[field] = to_epoch_ms(record.get(field))

    for entry in record.get("history", []):
        entry["at"] = to_epoch_ms(entry.get("at"))

        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = to_epoch_ms(change[side])

    return record


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest
):
    # Validate the requested calendar date.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })

    if not record:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set

    allowed_statuses = {
        "PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"
    }

    new_status = (
        body.status if "status" in supplied
        else record.get("status")
    )

    if new_status not in allowed_statuses:
        raise HTTPException(422, "invalid attendance status")

    # ABSENT and LEAVE cannot have punch timestamps.
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot include punch timestamps"
            )

        new_in = None
        new_out = None
    else:
        # Convert supplied epoch milliseconds to whole-second UTC instants.
        def parse_timestamp(field, old_value):
            if field not in supplied:
                return old_value

            value = getattr(body, field)

            if value is None:
                return None

            try:
                return datetime.fromtimestamp(
                    value // 1000,
                    tz=timezone.utc
                )
            except (ValueError, OverflowError, OSError):
                raise HTTPException(422, f"invalid {field} timestamp")

        new_in = parse_timestamp("punch_in", record.get("punch_in"))
        new_out = parse_timestamp("punch_out", record.get("punch_out"))

        if new_in is None:
            raise HTTPException(
                422,
                "presence status requires punch_in"
            )

        # The punch-in's IST calendar date must match this record.
        if new_in.astimezone(IST).date().isoformat() != date:
            raise HTTPException(
                422,
                "punch_in must fall on the attendance date in IST"
            )

        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(
                    422,
                    "punch_out must be after punch_in"
                )

            if (new_out - new_in).total_seconds() > 24 * 60 * 60:
                raise HTTPException(
                    422,
                    "punch_out cannot be over 24 hours after punch_in"
                )

    # Recalculate all derived fields from the corrected values.
    if new_status in {"ABSENT", "LEAVE"}:
        late_minutes = 0
        work_hours = None
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            new_in, emp["shift_start"]
        )

        if new_out is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            work_hours = compute_work_hours(new_in, new_out)
            overtime_minutes = compute_overtime(
                new_out,
                emp["shift_start"],
                emp["shift_end"],
                date
            )
            half_day = work_hours < 4.50

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    # Record only fields whose stored values actually change.
    changes = {}

    for field, new_value in new_values.items():
        old_value = record.get(field)

        if old_value != new_value:
            old_for_history = (
                to_epoch_ms(old_value)
                if field in {"punch_in", "punch_out"}
                else old_value
            )
            new_for_history = (
                to_epoch_ms(new_value)
                if field in {"punch_in", "punch_out"}
                else new_value
            )

            changes[field] = {
                "from": old_for_history,
                "to": new_for_history
            }

    if not changes:
        raise HTTPException(422, "request makes no changes")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Compare the original values during the update. If another request
    # has edited this record first, this update will not match.
    update_filter = {
        "_id": record["_id"],
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }

    if "history" in record:
        update_filter["history"] = record["history"]
    else:
        update_filter["history"] = {"$exists": False}

    result = db.attendance_logs.update_one(
        update_filter,
        {
            "$set": new_values,
            "$push": {"history": history_entry}
        }
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "attendance record was changed by another request; retry"
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})

    return attendance_response(updated)

    
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate month in YYYY-MM format.
    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")

    # Check that the employee exists.
    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    # Calculate the next month's first day.
    if month_start.month == 12:
        next_month = datetime(month_start.year + 1, 1, 1)
    else:
        next_month = datetime(
            month_start.year,
            month_start.month + 1,
            1
        )

    month_start_str = month_start.strftime("%Y-%m-%d")
    next_month_str = next_month.strftime("%Y-%m-%d")

    # Count working days from the later of the joining date
    # and the beginning of the requested month.
    working_pipeline = [
        {"$match": {"emp_code": emp_code}},
        {
            "$project": {
                "_id": 0,
                "working_days": {
                    "$let": {
                        "vars": {
                            "joined": {
                                "$dateFromString": {
                                    "dateString": "$joined_on"
                                }
                            },
                            "month_start": {
                                "$dateFromString": {
                                    "dateString": month_start_str
                                }
                            },
                            "month_end": {
                                "$dateFromString": {
                                    "dateString": next_month_str
                                }
                            }
                        },
                        "in": {
                            "$let": {
                                "vars": {
                                    "start": {
                                        "$max": [
                                            "$$joined",
                                            "$$month_start"
                                        ]
                                    }
                                },
                                "in": {
                                    "$size": {
                                        "$filter": {
                                            "input": {
                                                "$map": {
                                                    "input": {
                                                        "$range": [
                                                            0,
                                                            {
                                                                "$max": [
                                                                    0,
                                                                    {
                                                                        "$dateDiff": {
                                                                            "startDate": "$$start",
                                                                            "endDate": "$$month_end",
                                                                            "unit": "day"
                                                                        }
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    "as": "offset",
                                                    "in": {
                                                        "$dateAdd": {
                                                            "startDate": "$$start",
                                                            "unit": "day",
                                                            "amount": "$$offset"
                                                        }
                                                    }
                                                }
                                            },
                                            "as": "day",
                                            "cond": {
                                                "$not": [
                                                    {
                                                        "$in": [
                                                            {
                                                                "$dayOfWeek": "$$day"
                                                            },
                                                            [1, 7]
                                                        ]
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    ]

    working_result = list(
        db.employees.aggregate(working_pipeline)
    )

    working_days = (
        working_result[0]["working_days"]
        if working_result
        else 0
    )

    # Aggregate attendance statistics inside MongoDB.
    attendance_pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start_str,
                    "$lt": next_month_str
                }
            }
        },
        {
            "$set": {
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date"
                        }
                    }
                }
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
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {"$not": [
                                        {"$in": ["$day_of_week", [1, 7]]}
                                    ]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                }
            }
        }
    ]

    stats_result = list(
        db.attendance_logs.aggregate(attendance_pipeline)
    )

    stats = stats_result[0] if stats_result else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0
    }

    present_days = stats["present_days"]

    # Round the attendance percentage to 4 decimal places,
    # using half-up rounding as required by R8.
    if working_days == 0:
        attendance_pct = None
    else:
        pct = (
            Decimal(str(present_days))
            / Decimal(working_days)
            * Decimal("100")
        )
        attendance_pct = float(
            pct.quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None
):
    # Validate month format.
    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")

    # Calculate the first day of the next month.
    if month_start.month == 12:
        next_month = datetime(month_start.year + 1, 1, 1)
    else:
        next_month = datetime(
            month_start.year,
            month_start.month + 1,
            1
        )

    start_date = month_start.strftime("%Y-%m-%d")
    end_date = next_month.strftime("%Y-%m-%d")

    # Include employees who joined before the next month starts.
    employee_match = {"joined_on": {"$lt": end_date}}

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},

        # Find attendance records for each employee in this month.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lt": ["$date", end_date]}
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date"
                                    }
                                }
                            }
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
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY"
                                                        ]
                                                    ]
                                                },
                                                {
                                                    "$not": [
                                                        {
                                                            "$in": [
                                                                "$day_of_week",
                                                                [1, 7]
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1
                                            ]
                                        },
                                        0
                                    ]
                                }
                            },

                            # Sum hours and count qualifying records.
                            # This allows a correct department-wide average.
                            "total_work_hours": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY"
                                                        ]
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]}
                                            ]
                                        },
                                        "$work_hours",
                                        0
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY"
                                                        ]
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },

                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": "$late_minutes"
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "attendance_stats"
            }
        },

        # Keep employees even when they have no attendance records.
        {
            "$unwind": {
                "path": "$attendance_stats",
                "preserveNullAndEmptyArrays": True
            }
        },

        # Combine employee data into department totals.
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$ifNull": ["$attendance_stats.present_days", 0]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.total_work_hours",
                            0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.work_hours_count",
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$ifNull": ["$attendance_stats.late_count", 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.total_late_minutes",
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$ifNull": ["$attendance_stats.leave_count", 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$ifNull": ["$attendance_stats.on_duty_count", 0]
                    }
                }
            }
        },

        {"$sort": {"_id": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))

    items = []

    for row in results:
        count = row["work_hours_count"]

        if count == 0:
            avg_work_hours = None
        else:
            average = (
                Decimal(str(row["total_work_hours"]))
                / Decimal(count)
            )
            avg_work_hours = float(
                average.quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )
            )

        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"]
        })

    return {
        "month": month,
        "items": items
    }

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None
):
    # Validate inputs.
    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")

    if limit < 1 or limit > 50:
        raise HTTPException(422, "limit must be between 1 and 50")

    # Get the first day of the next month.
    if month_start.month == 12:
        next_month = datetime(month_start.year + 1, 1, 1)
    else:
        next_month = datetime(
            month_start.year,
            month_start.month + 1,
            1
        )

    start_date = month_start.strftime("%Y-%m-%d")
    end_date = next_month.strftime("%Y-%m-%d")

    pipeline = [
        # Only examine attendance records in the requested month.
        {
            "$match": {
                "date": {
                    "$gte": start_date,
                    "$lt": end_date
                }
            }
        },

        # Calculate total late minutes for each employee.
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                }
            }
        },

        # Ignore employees who were not late.
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },

        # Get employee name and department.
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"},

        # Filter department before ranking, if requested.
        *(
            [{"$match": {"employee.department": department}}]
            if department is not None else []
        ),

        # Rank by total late minutes. Equal totals receive equal ranks.
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },

        # Apply the limit AFTER ranking, preserving ties.
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },

        # Final display order.
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },

        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "rank": 1
            }
        }
    ]

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items
    }

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    # Validate both dates.
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()

        if start.isoformat() != from_date or end.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "from and to must be YYYY-MM-DD")

    if end < start:
        raise HTTPException(422, "to must not be before from")

    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    # Confirm that the department exists.
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    start_str = start.isoformat()
    end_str = end.isoformat()

    pipeline = [
        {"$match": {"department": department}},

        # Keep employee codes and joining dates for headcount calculations.
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on"
                    }
                }
            }
        },

        # Generate every calendar day inside MongoDB.
        {
            "$set": {
                "days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": start_str
                                                    }
                                                },
                                                "endDate": {
                                                    "$dateFromString": {
                                                        "dateString": end_str
                                                    }
                                                },
                                                "unit": "day"
                                            }
                                        },
                                        1
                                    ]
                                }
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": {
                                    "$dateFromString": {
                                        "dateString": start_str
                                    }
                                },
                                "unit": "day",
                                "amount": "$$offset"
                            }
                        }
                    }
                }
            }
        },
        {"$unwind": "$days"},

        # Calculate date, weekday status, and headcount.
        {
            "$set": {
                "date_str": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$days"
                    }
                },
                "is_working_day": {
                    "$not": [
                        {
                            "$in": [
                                {"$dayOfWeek": "$days"},
                                [1, 7]
                            ]
                        }
                    ]
                }
            }
        },
        {
            "$set": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "emp",
                            "cond": {
                                "$lte": [
                                    "$$emp.joined_on",
                                    "$date_str"
                                ]
                            }
                        }
                    }
                }
            }
        },

        # Find attendance records for this department and day.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$date_str",
                    "codes": {
                        "$map": {
                            "input": "$employees",
                            "as": "emp",
                            "in": "$$emp.emp_code"
                        }
                    }
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },

        # Present count includes half days as 0.5.
        {
            "$set": {
                "present_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$$this.status",
                                                ["PRESENT", "WFH", "ON_DUTY"]
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$$this.half_day", True]},
                                                0.5,
                                                1
                                            ]
                                        },
                                        0
                                    ]
                                }
                            ]
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": ["$$log.late_minutes", 0]
                            }
                        }
                    }
                }
            }
        },

        # Attendance rate is null on weekends or when headcount is zero.
        # For nonnegative values, floor(x * 10000 + 0.5) / 10000
        # implements four-decimal half-up rounding.
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_count",
                                                            "$headcount"
                                                        ]
                                                    },
                                                    10000
                                                ]
                                            },
                                            0.5
                                        ]
                                    }
                                },
                                10000
                            ]
                        },
                        None
                    ]
                }
            }
        },

        # Average the current rate and up to six preceding rates.
        {
            "$setWindowFields": {
                "sortBy": {"date_str": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }
        },

        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_raw", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_raw",
                                                    10000
                                                ]
                                            },
                                            0.5
                                        ]
                                    }
                                },
                                10000
                            ]
                        }
                    ]
                }
            }
        },

        {"$sort": {"date_str": 1}},

        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        }
    ]

    items = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "items": items
    }
from fastapi.encoders import jsonable_encoder


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    supported = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in supported:
        raise HTTPException(422, "unsupported endpoint")

    if page < 1 or page_size < 1 or page_size > 100 or limit < 1:
        raise HTTPException(422, "invalid pagination or limit")

    collection = "attendance_logs"
    pipeline = None
    query = {}
    sort = {}

    if endpoint == "attendance_list":
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "date_from must not exceed date_to")

        sort = {"date": -1}
        explain_command = {
            "find": collection,
            "filter": query,
            "sort": sort,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        try:
            start = datetime.strptime(month, "%Y-%m")
            if start.strftime("%Y-%m") != month:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "month must be YYYY-MM")

        if start.month == 12:
            end = datetime(start.year + 1, 1, 1)
        else:
            end = datetime(start.year, start.month + 1, 1)

        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start.strftime("%Y-%m-%d"),
                    "$lt": end.strftime("%Y-%m-%d"),
                },
            }},
            {"$group": {
                "_id": None,
                "records": {"$sum": 1},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }},
        ]

        explain_command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        try:
            start = datetime.strptime(month, "%Y-%m")
            if start.strftime("%Y-%m") != month:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "month must be YYYY-MM")

        if start.month == 12:
            end = datetime(start.year + 1, 1, 1)
        else:
            end = datetime(start.year, start.month + 1, 1)

        employee_match = {
            "joined_on": {"$lt": end.strftime("%Y-%m-%d")}
        }
        if department:
            employee_match["department"] = department

        collection = "employees"
        pipeline = [{"$match": employee_match}]
        explain_command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        try:
            start = datetime.strptime(month, "%Y-%m")
            if start.strftime("%Y-%m") != month:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "month must be YYYY-MM")

        if start.month == 12:
            end = datetime(start.year + 1, 1, 1)
        else:
            end = datetime(start.year, start.month + 1, 1)

        pipeline = [
            {"$match": {
                "date": {
                    "$gte": start.strftime("%Y-%m-%d"),
                    "$lt": end.strftime("%Y-%m-%d"),
                }
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$limit": limit},
        ]

        if department:
            pipeline.insert(1, {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            })

        explain_command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    else:  # department_trend
        if not department or not from_date or not to_date:
            raise HTTPException(
                422, "department, from and to are required"
            )
        try:
            first = datetime.strptime(from_date, "%Y-%m-%d").date()
            last = datetime.strptime(to_date, "%Y-%m-%d").date()
            if first.isoformat() != from_date or last.isoformat() != to_date:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "from and to must be YYYY-MM-DD")

        if first > last:
            raise HTTPException(422, "from must not exceed to")

        collection = "employees"
        pipeline = [
            {"$match": {"department": department}},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", from_date]},
                        {"$lte": ["$date", to_date]},
                    ]}}},
                ],
                "as": "attendance",
            }},
        ]
        explain_command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    try:
        result = db.command(
            "explain",
            explain_command,
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": collection,
            "explain": jsonable_encoder(result),
        }
    except Exception as exc:
        raise HTTPException(
            500, f"MongoDB explain failed: {str(exc)}"
        )











# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
