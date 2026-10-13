from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.mark.parametrize(
    ("overrides", "invalid_field"),
    [
        ({"emp_code": "string"}, "emp_code"),
        ({"emp_code": "EMP0001", "joined_on": "string"}, "joined_on"),
    ],
)
def test_create_employee_rejects_invalid_contract_fields(overrides, invalid_field):
    payload = {
        "emp_code": "EMP0001",
        "name": "Test Employee",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-10-13",
        **overrides,
    }

    response = TestClient(main.app).post("/employees", json=payload)

    assert response.status_code == 422
    assert any(error["loc"][-1] == invalid_field for error in response.json()["detail"])


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, *args, **kwargs):
        return self.documents


def test_department_summary_skips_malformed_employees(monkeypatch, caplog):
    employees = [
        {
            "emp_code": "EMP0001",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
        {
            "emp_code": "EMP0002",
            "department": "Engineering",
            "joined_on": "string",
        },
        {
            "emp_code": "EMP0003",
            "department": "Engineering",
            "joined_on": "2026-02-30",
        },
        {
            "emp_code": "string",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    ]
    attendance = [
        {
            "emp_code": emp_code,
            "date": "2026-10-01",
            "status": "PRESENT",
            "work_hours": 8.0,
            "late_minutes": 30,
        }
        for emp_code in ("EMP0002", "EMP0003", "string")
    ]
    fake_db = SimpleNamespace(
        employees=FakeCollection(employees),
        attendance_logs=FakeCollection(attendance),
    )
    monkeypatch.setattr(main, "db", fake_db)

    result = main.department_summary("2026-10")

    row = result["items"][0]
    assert row["headcount"] == 1
    assert row["present_days"] == 0
    assert row["late_count"] == 0
    assert row["total_late_minutes"] == 0
    assert row["avg_work_hours"] is None
    assert "invalid joined_on" in caplog.text
    assert "invalid emp_code" in caplog.text