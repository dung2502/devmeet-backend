"""Phase 6A — Preflight Suite Unit & Integration Tests

Validates the Phase 6A environment preflight logic, settings check,
fastapi health, postgres schema inspection, n8n workflow inspection,
and secret exclusion.
"""

from app.preflight import (
    check_environment_settings,
    check_extension_security_and_manifest,
    check_fastapi_health,
    check_google_sheets_configuration,
    check_n8n_workflow_file,
    check_postgres_health,
    mask_secret,
    run_full_preflight,
)


def test_mask_secret_masks_properly() -> None:
    assert mask_secret("") == "<not-set>"
    assert mask_secret(None) == "<not-set>"
    assert mask_secret("12345") == "*** (len: 5)"
    masked = mask_secret("super-secret-key-for-devmeet")
    assert "super" not in masked or masked.startswith("sup")
    assert "len: 28" in masked


def test_check_environment_settings_does_not_leak_raw_secrets() -> None:
    items = check_environment_settings()
    assert len(items) >= 8

    # Ensure no raw secret is present in details or evidence
    for item in items:
        assert "your-backend-secret" not in item.evidence
        assert "your-google-client-secret" not in item.evidence


def test_check_fastapi_health() -> None:
    item = check_fastapi_health()
    assert item.status == "PASS"
    assert item.name == "FastAPI Backend Health"


def test_check_postgres_health() -> None:
    item = check_postgres_health()
    assert item.status == "PASS"
    assert "meetings" in item.details
    assert "live_sessions" in item.details


def test_check_n8n_workflow_file() -> None:
    items = check_n8n_workflow_file()
    assert len(items) >= 4

    item_names = {i.name: i for i in items}
    assert item_names["Workflow JSON Existence"].status == "PASS"
    assert item_names["Webhook Node & Path"].status == "PASS"
    assert item_names["Action Support (AI_PROCESS & SHEETS_RETRY)"].status == "PASS"
    assert item_names["Zero PostgreSQL Invariant"].status == "PASS"


def test_check_google_sheets_configuration() -> None:
    item = check_google_sheets_configuration()
    assert item.status in ("CONFIGURED", "PASS", "PARTIAL")
    assert "Meetings" in item.details
    assert "Decisions" in item.details


def test_check_extension_security_and_manifest() -> None:
    items = check_extension_security_and_manifest()
    assert len(items) >= 2

    item_names = {i.name: i for i in items}
    assert item_names["Manifest MV3 Validation"].status == "PASS"
    assert item_names["Extension Secret Exclusion"].status == "PASS"


def test_run_full_preflight_passes() -> None:
    report = run_full_preflight()
    assert len(report.items) >= 15
    assert report.all_passed is True
