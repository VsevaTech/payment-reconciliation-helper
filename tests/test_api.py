"""End-to-end through FastAPI: upload → map → reconcile → export, plus JSON API."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = json.loads((ROOT / "demo-data" / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def demo_files() -> dict:
    return {
        "orders": ("orders.csv", (ROOT / "demo-data" / "orders.csv").read_bytes(), "text/csv"),
        "payments": (
            "payments.csv",
            (ROOT / "demo-data" / "payments.csv").read_bytes(),
            "text/csv",
        ),
    }


def test_health(client: TestClient):
    assert client.get("/health").json()["status"] == "ok"


def test_index_renders(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200 and "Upload files" in r.text


def test_full_ui_flow(client: TestClient):
    r = client.post("/upload", files=demo_files(), follow_redirects=False)
    assert r.status_code == 303
    map_url = r.headers["location"]
    session_id = re.search(r"/s/([0-9a-f]+)/map", map_url).group(1)

    page = client.get(map_url)
    assert page.status_code == 200
    # auto-guessed mapping is pre-selected
    assert '<option value="Order ID" selected>' in page.text
    assert '<option value="merchant_reference" selected>' in page.text
    assert "<code>utf-8</code>" in page.text
    assert "<code>;</code>" in page.text

    form = {
        "orders_reference": "Order ID",
        "orders_amount": "Amount",
        "orders_currency": "Currency",
        "orders_date": "Date",
        "payments_reference": "merchant_reference",
        "payments_amount": "amount",
        "payments_currency": "currency",
        "payments_date": "payment_date",
        "tolerance": "0",
        "window_days": "3",
    }
    r = client.post(f"/s/{session_id}/reconcile", data=form, follow_redirects=False)
    assert r.status_code == 303

    result = client.get(f"/s/{session_id}/result")
    assert result.status_code == 200
    for cat, count in EXPECTED["summary"].items():
        tile = f'<div class="n">{count}</div><div class="k"><span class="pill {cat}">'
        assert tile in result.text

    partial = client.get(
        f"/s/{session_id}/result?category=AMOUNT_MISMATCH", headers={"HX-Request": "true"}
    )
    assert "<html" not in partial.text
    assert partial.text.count('class="pill AMOUNT_MISMATCH"') == 3

    csv_resp = client.get(f"/s/{session_id}/export.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-disposition"].endswith('filename="reconciliation.csv"')
    exceptions_total = sum(v for k, v in EXPECTED["summary"].items() if k != "MATCHED")
    assert len(csv_resp.content.decode("utf-8-sig").strip().splitlines()) == exceptions_total + 1

    xlsx_resp = client.get(f"/s/{session_id}/export.xlsx")
    assert xlsx_resp.status_code == 200
    wb = load_workbook(io.BytesIO(xlsx_resp.content))
    assert wb["Exceptions"].max_row == exceptions_total + 1
    assert wb["All rows"].max_row == sum(EXPECTED["summary"].values()) + 1


def test_upload_rejects_invalid_csv(client: TestClient):
    files = demo_files()
    files["payments"] = ("payments.csv", b"\x00\x01\x02\x03garbage", "text/csv")
    r = client.post("/upload", files=files)
    assert r.status_code == 200
    assert "payments:" in r.text and "binary" in r.text


def test_reconcile_with_bad_mapping_shows_error(client: TestClient):
    r = client.post("/upload", files=demo_files(), follow_redirects=False)
    session_id = re.search(r"/s/([0-9a-f]+)/map", r.headers["location"]).group(1)
    r = client.post(
        f"/s/{session_id}/reconcile",
        data={
            "orders_reference": "Nope",
            "orders_amount": "Amount",
            "payments_reference": "merchant_reference",
            "payments_amount": "amount",
        },
    )
    assert r.status_code == 200 and "does not exist" in r.text


def test_unknown_session_404(client: TestClient):
    assert client.get("/s/deadbeef/result").status_code == 404


def test_json_api(client: TestClient):
    r = client.post(
        "/api/reconcile",
        files=demo_files(),
        data={
            "orders_reference": "Order ID",
            "orders_amount": "Amount",
            "orders_currency": "Currency",
            "orders_date": "Date",
            "payments_reference": "merchant_reference",
            "payments_amount": "amount",
            "payments_currency": "currency",
            "payments_date": "payment_date",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == EXPECTED["summary"]
    assert body["detected"]["payments"]["delimiter"] == ";"
    assert len(body["rows"]) == sum(EXPECTED["summary"].values())
    assert all(isinstance(row["order_amount"], str) for row in body["rows"])  # never float


def test_json_api_invalid_tolerance(client: TestClient):
    r = client.post(
        "/api/reconcile",
        files=demo_files(),
        data={
            "orders_reference": "Order ID",
            "orders_amount": "Amount",
            "payments_reference": "merchant_reference",
            "payments_amount": "amount",
            "tolerance": "lots",
        },
    )
    assert r.status_code == 400
