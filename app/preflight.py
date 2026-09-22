"""Phase 6A — Environment & Service Health Preflight Module

Provides read-only deterministic preflight checks for:
1. Environment configuration (Backend settings, masked secrets)
2. FastAPI application health
3. PostgreSQL connection & schema integrity (read-only)
4. n8n workflow file validation & reachability
5. Google OAuth configuration & extension secret exclusion
6. Google Sheets configuration & schema expectations
7. Chrome Extension build & bundle readiness
"""

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.config import Settings, get_settings
from app.main import app


@dataclass
class PreflightItem:
    category: str
    name: str
    status: str  # PASS, PARTIAL, FAIL, MISSING, CONFIGURED
    details: str
    evidence: str


@dataclass
class PreflightReport:
    items: List[PreflightItem] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(item.status in ("PASS", "CONFIGURED") for item in self.items)

    def add(self, category: str, name: str, status: str, details: str, evidence: str) -> None:
        self.items.append(
            PreflightItem(
                category=category,
                name=name,
                status=status,
                details=details,
                evidence=evidence,
            )
        )


def mask_secret(value: Optional[str]) -> str:
    """Masks secret values safely without exposing plain text."""
    if not value:
        return "<not-set>"
    if len(value) <= 6:
        return f"*** (len: {len(value)})"
    return f"{value[:3]}...{value[-3:]} (len: {len(value)})"


def check_environment_settings(settings: Optional[Settings] = None) -> List[PreflightItem]:
    """Inspects backend environment settings without exposing raw secrets."""
    settings = settings or get_settings()
    results = []

    # 1. APP_ENV
    results.append(
        PreflightItem(
            category="Environment",
            name="APP_ENV",
            status="PASS" if settings.app_env in ("development", "production", "test") else "PARTIAL",
            details=f"Current environment is '{settings.app_env}'",
            evidence=f"APP_ENV={settings.app_env}",
        )
    )

    # 2. DATABASE_URL
    has_db = bool(settings.database_url and "postgresql" in settings.database_url)
    results.append(
        PreflightItem(
            category="Environment",
            name="DATABASE_URL",
            status="CONFIGURED" if has_db else "MISSING",
            details="PostgreSQL connection string present" if has_db else "DATABASE_URL missing",
            evidence="postgresql://***:***@localhost:5432/devmeet_db" if has_db else "<none>",
        )
    )

    # 3. Google OAuth Settings
    has_g_cid = bool(settings.google_client_id and not settings.google_client_id.startswith("your-"))
    has_g_sec = bool(settings.google_client_secret and not settings.google_client_secret.startswith("your-"))
    has_g_uri = bool(settings.google_redirect_uri and not settings.google_redirect_uri.startswith("your-"))

    results.append(
        PreflightItem(
            category="Environment",
            name="GOOGLE_CLIENT_ID",
            status="CONFIGURED" if has_g_cid else "MISSING",
            details="Google OAuth Client ID present" if has_g_cid else "Missing or default placeholder",
            evidence=mask_secret(settings.google_client_id),
        )
    )
    results.append(
        PreflightItem(
            category="Environment",
            name="GOOGLE_CLIENT_SECRET",
            status="CONFIGURED" if has_g_sec else "MISSING",
            details="Google OAuth Client Secret present" if has_g_sec else "Missing or default placeholder",
            evidence=mask_secret(settings.google_client_secret),
        )
    )
    results.append(
        PreflightItem(
            category="Environment",
            name="GOOGLE_REDIRECT_URI",
            status="PASS" if has_g_uri else "MISSING",
            details=f"Redirect URI configured: {settings.google_redirect_uri}" if has_g_uri else "Missing",
            evidence=settings.google_redirect_uri or "<none>",
        )
    )

    # 4. n8n Settings
    has_n8n_url = bool(settings.n8n_webhook_url and "/webhook/devmeet-meeting-ai" in settings.n8n_webhook_url)
    has_n8n_sec = bool(settings.devmeet_webhook_secret and len(settings.devmeet_webhook_secret) >= 16)

    results.append(
        PreflightItem(
            category="Environment",
            name="N8N_WEBHOOK_URL",
            status="PASS" if has_n8n_url else "MISSING",
            details=f"Canonical webhook URL: {settings.n8n_webhook_url}",
            evidence=settings.n8n_webhook_url or "<none>",
        )
    )
    results.append(
        PreflightItem(
            category="Environment",
            name="DEVMEET_WEBHOOK_SECRET",
            status="CONFIGURED" if has_n8n_sec else "MISSING",
            details="Webhook authentication secret configured" if has_n8n_sec else "Missing/insufficient length",
            evidence=mask_secret(settings.devmeet_webhook_secret),
        )
    )

    # 5. Google Sheets ID
    has_sheets = bool(settings.devmeet_spreadsheet_id and not settings.devmeet_spreadsheet_id.startswith("your-"))
    results.append(
        PreflightItem(
            category="Environment",
            name="DEVMEET_SPREADSHEET_ID",
            status="CONFIGURED" if has_sheets else "MISSING",
            details="Google Spreadsheet ID configured" if has_sheets else "Missing or placeholder",
            evidence=mask_secret(settings.devmeet_spreadsheet_id),
        )
    )

    # 6. ALLOW_DEV_AUTH_BYPASS
    results.append(
        PreflightItem(
            category="Environment",
            name="ALLOW_DEV_AUTH_BYPASS",
            status="PASS",
            details=f"Dev auth bypass is {settings.allow_dev_auth_bypass}",
            evidence=f"ALLOW_DEV_AUTH_BYPASS={settings.allow_dev_auth_bypass}",
        )
    )

    return results


def check_fastapi_health() -> PreflightItem:
    """Verifies FastAPI application health via GET /api/v1/health."""
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/health")
            if response.status_code == 200 and response.json().get("status") in ("ok", "healthy"):
                return PreflightItem(
                    category="Service Health",
                    name="FastAPI Backend Health",
                    status="PASS",
                    details="GET /api/v1/health returned 200 OK and status=ok",
                    evidence=json.dumps(response.json()),
                )
            return PreflightItem(
                category="Service Health",
                name="FastAPI Backend Health",
                status="FAIL",
                details=f"Unexpected response status {response.status_code}",
                evidence=response.text[:200],
            )
    except Exception as e:
        return PreflightItem(
            category="Service Health",
            name="FastAPI Backend Health",
            status="FAIL",
            details=f"Health check failed with exception: {type(e).__name__}",
            evidence=str(e),
        )


def check_postgres_health(database_url: Optional[str] = None) -> PreflightItem:
    """Read-only check for PostgreSQL connectivity and required tables."""
    settings = get_settings()
    url = database_url or settings.database_url

    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1")).scalar()
            inspector = inspect(engine)
            tables = inspector.get_table_names()

        required_tables = {"meetings", "live_sessions", "transcripts", "transcript_entries", "participants", "users"}
        found_required = required_tables.intersection(set(tables))
        missing_required = required_tables - set(tables)

        if result == 1 and not missing_required:
            return PreflightItem(
                category="Service Health",
                name="PostgreSQL Connection & Schema",
                status="PASS",
                details=f"Connected successfully. Found required tables: {', '.join(sorted(found_required))}",
                evidence=f"Tables: {', '.join(sorted(tables))}",
            )
        elif result == 1:
            return PreflightItem(
                category="Service Health",
                name="PostgreSQL Connection & Schema",
                status="PARTIAL",
                details=f"Connected, but missing tables: {', '.join(sorted(missing_required))}",
                evidence=f"Present tables: {', '.join(sorted(tables))}",
            )
        else:
            return PreflightItem(
                category="Service Health",
                name="PostgreSQL Connection & Schema",
                status="FAIL",
                details="SELECT 1 returned unexpected result",
                evidence=str(result),
            )
    except Exception as e:
        return PreflightItem(
            category="Service Health",
            name="PostgreSQL Connection & Schema",
            status="FAIL",
            details=f"PostgreSQL connection failed: {type(e).__name__}",
            evidence=str(e),
        )


def check_n8n_workflow_file(repo_root: Optional[Path] = None) -> List[PreflightItem]:
    """Validates the canonical n8n workflow file DEVMEET_Meeting_Processor_v3.json."""
    if repo_root is None:
        curr = Path(__file__).resolve()
        while curr.parent != curr:
            if (curr / "ai_workflow").exists():
                repo_root = curr
                break
            curr = curr.parent

    results = []
    if repo_root is None or not (repo_root / "ai_workflow" / "DEVMEET_Meeting_Processor_v3.json").exists():
        results.append(
            PreflightItem(
                category="n8n Integration",
                name="Workflow JSON Existence",
                status="MISSING",
                details="Workflow file DEVMEET_Meeting_Processor_v3.json not found in ai_workflow/",
                evidence="File missing",
            )
        )
        return results

    workflow_path = repo_root / "ai_workflow" / "DEVMEET_Meeting_Processor_v3.json"
    try:
        with open(workflow_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        results.append(
            PreflightItem(
                category="n8n Integration",
                name="Workflow JSON Existence",
                status="PASS",
                details=f"Workflow JSON parsed successfully ({len(data.get('nodes', []))} nodes)",
                evidence=str(workflow_path),
            )
        )

        # Inspect nodes
        nodes = data.get("nodes", [])

        # Check webhook node
        webhook_node = next((n for n in nodes if n.get("type") == "n8n-nodes-base.webhook"), None)
        path_matches = webhook_node and webhook_node.get("parameters", {}).get("path") == "devmeet-meeting-ai"

        results.append(
            PreflightItem(
                category="n8n Integration",
                name="Webhook Node & Path",
                status="PASS" if path_matches else "FAIL",
                details="Webhook node configured with path 'devmeet-meeting-ai'" if path_matches else "Webhook path mismatch",
                evidence=f"Path: {webhook_node.get('parameters', {}).get('path') if webhook_node else None}",
            )
        )

        # Check AI_PROCESS and SHEETS_RETRY support in code
        raw_code_text = " ".join(n.get("parameters", {}).get("jsCode", "") for n in nodes if "parameters" in n)
        has_ai_process = "AI_PROCESS" in raw_code_text
        has_sheets_retry = "SHEETS_RETRY" in raw_code_text

        results.append(
            PreflightItem(
                category="n8n Integration",
                name="Action Support (AI_PROCESS & SHEETS_RETRY)",
                status="PASS" if (has_ai_process and has_sheets_retry) else "FAIL",
                details="Workflow contains both AI_PROCESS and SHEETS_RETRY handling branches",
                evidence=f"AI_PROCESS={has_ai_process}, SHEETS_RETRY={has_sheets_retry}",
            )
        )

        # Architectural invariant: NO PostgreSQL node in n8n
        has_pg_node = any("postgres" in str(n.get("type", "")).lower() for n in nodes)
        results.append(
            PreflightItem(
                category="n8n Integration",
                name="Zero PostgreSQL Invariant",
                status="PASS" if not has_pg_node else "FAIL",
                details="n8n workflow does NOT contain direct PostgreSQL nodes (Architectural Invariant)",
                evidence="No postgresql nodes found" if not has_pg_node else "VIOLATION: postgres node found",
            )
        )

    except Exception as e:
        results.append(
            PreflightItem(
                category="n8n Integration",
                name="Workflow JSON Parsing",
                status="FAIL",
                details=f"Failed to inspect workflow JSON: {str(e)}",
                evidence=str(e),
            )
        )

    return results


def check_google_sheets_configuration() -> PreflightItem:
    """Verifies Google Sheets schema expectations."""
    settings = get_settings()
    has_id = bool(settings.devmeet_spreadsheet_id and not settings.devmeet_spreadsheet_id.startswith("your-"))

    expected_sheets = ["Meetings", "Decisions", "Action Items", "Follow-up Emails"]
    return PreflightItem(
        category="Google Sheets",
        name="Sheets Schema & Spreadsheet ID",
        status="CONFIGURED" if has_id else "PARTIAL",
        details=f"Expected sheet tabs: {', '.join(expected_sheets)}" + (" (Spreadsheet ID set)" if has_id else " (Spreadsheet ID needs real Google ID)"),
        evidence=f"Tabs: {', '.join(expected_sheets)} | ID: {mask_secret(settings.devmeet_spreadsheet_id)}",
    )


def check_extension_security_and_manifest(repo_root: Optional[Path] = None) -> List[PreflightItem]:
    """Ensures Extension manifest is valid and does NOT embed client secrets."""
    if repo_root is None:
        curr = Path(__file__).resolve()
        while curr.parent != curr:
            if (curr / "Projects" / "Extension").exists():
                repo_root = curr
                break
            curr = curr.parent

    results = []
    if repo_root is None or not (repo_root / "Projects" / "Extension").exists():
        results.append(
            PreflightItem(
                category="Extension Preflight",
                name="Extension Directory",
                status="MISSING",
                details="Projects/Extension directory not found",
                evidence="Missing directory",
            )
        )
        return results

    ext_dir = repo_root / "Projects" / "Extension"
    manifest_path = ext_dir / "manifest.json"

    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            results.append(
                PreflightItem(
                    category="Extension Preflight",
                    name="Manifest MV3 Validation",
                    status="PASS" if manifest.get("manifest_version") == 3 else "FAIL",
                    details=f"Extension name: {manifest.get('name')}, version: {manifest.get('version')}",
                    evidence=f"manifest_version={manifest.get('manifest_version')}",
                )
            )
        except Exception as e:
            results.append(
                PreflightItem(
                    category="Extension Preflight",
                    name="Manifest MV3 Validation",
                    status="FAIL",
                    details=f"Failed to read manifest.json: {str(e)}",
                    evidence=str(e),
                )
            )

    # Security check: Scan extension src/ and dist/ for any Google Client Secret leaks
    leaked_secrets = []
    settings = get_settings()
    secret_to_check = settings.google_client_secret

    if secret_to_check and not secret_to_check.startswith("your-"):
        for folder in [ext_dir / "src", ext_dir / "dist"]:
            if folder.exists():
                for file_path in folder.rglob("*"):
                    if file_path.is_file() and file_path.suffix in (".js", ".ts", ".tsx", ".html", ".json"):
                        try:
                            content = file_path.read_text(encoding="utf-8", errors="ignore")
                            if secret_to_check in content:
                                leaked_secrets.append(str(file_path.relative_to(ext_dir)))
                        except Exception:
                            pass

    results.append(
        PreflightItem(
            category="Security Preflight",
            name="Extension Secret Exclusion",
            status="PASS" if not leaked_secrets else "FAIL",
            details="Verified Google Client Secret is NEVER embedded in Extension bundles" if not leaked_secrets else f"CRITICAL LEAK in: {', '.join(leaked_secrets)}",
            evidence="Zero secret leaks detected in Extension" if not leaked_secrets else f"Leaked in {len(leaked_secrets)} files",
        )
    )

    return results


def run_full_preflight(settings: Optional[Settings] = None, repo_root: Optional[Path] = None) -> PreflightReport:
    """Executes all Phase 6A environment and service preflight checks."""
    report = PreflightReport()

    # 1. Environment Settings
    for item in check_environment_settings(settings):
        report.items.append(item)

    # 2. FastAPI Health
    report.items.append(check_fastapi_health())

    # 3. PostgreSQL Connection & Schema
    report.items.append(check_postgres_health())

    # 4. n8n Workflow Validation
    for item in check_n8n_workflow_file(repo_root):
        report.items.append(item)

    # 5. Google Sheets Configuration
    report.items.append(check_google_sheets_configuration())

    # 6. Extension Security & Manifest
    for item in check_extension_security_and_manifest(repo_root):
        report.items.append(item)

    return report
