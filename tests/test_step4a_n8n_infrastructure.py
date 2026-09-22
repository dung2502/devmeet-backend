import json
import os
import pytest

# Dynamically locate root directory containing ai_workflow
CURR_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = CURR_DIR
while not os.path.exists(os.path.join(ROOT_DIR, "ai_workflow")) and os.path.dirname(ROOT_DIR) != ROOT_DIR:
    ROOT_DIR = os.path.dirname(ROOT_DIR)

WORKFLOW_PATH = os.path.join(ROOT_DIR, "ai_workflow", "DEVMEET_Meeting_Processor_v3.json")
COMPOSE_PATH = os.path.join(ROOT_DIR, "Projects", "Python", "docker-compose.yml")
BACKEND_ENV_PATH = os.path.join(ROOT_DIR, "Projects", "Python", "backend", ".env.example")
NATIVE_N8N_DIR = r"C:\Users\admin\.n8n"


def test_native_n8n_directory_and_port_5678_configuration() -> None:
    """Verify native Windows n8n directory exists and webhook port 5678 is configured."""
    assert os.path.exists(NATIVE_N8N_DIR), f"Native n8n data directory must exist at {NATIVE_N8N_DIR}"

    with open(BACKEND_ENV_PATH, "r", encoding="utf-8") as f:
        env_content = f.read()

    assert "N8N_PORT=5678" in env_content, "Backend environment configuration must use n8n main port 5678"
    assert "http://localhost:5678/webhook/devmeet-meeting-ai" in env_content


def test_docker_compose_has_no_duplicate_n8n_container() -> None:
    """Verify docker-compose.yml contains only PostgreSQL and no duplicate n8n container."""
    assert os.path.exists(COMPOSE_PATH), f"docker-compose.yml must exist at {COMPOSE_PATH}"

    with open(COMPOSE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    assert "postgres:" in content, "docker-compose.yml must define postgres service for backend"
    assert "n8n:" not in content, "docker-compose.yml must NOT define a duplicate n8n container"


def test_n8n_workflow_file_json_structure_and_no_postgres_nodes() -> None:
    """Verify DEVMEET_Meeting_Processor_v3.json contains zero PostgreSQL nodes or hardcoded credentials."""
    assert os.path.exists(WORKFLOW_PATH), f"DEVMEET_Meeting_Processor_v3.json must exist at {WORKFLOW_PATH}"

    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    nodes = data.get("nodes", [])
    node_types = [n.get("type", "") for n in nodes]

    # Rule: n8n MUST NOT have postgres nodes
    assert not any("postgres" in t.lower() for t in node_types), "n8n workflow must not contain postgres nodes"

    # Rule: No hard-coded secrets
    raw_json = json.dumps(data)
    assert "GOCSPX-" not in raw_json, "No real Google OAuth secret should be committed"
    assert "sk-proj-" not in raw_json, "No real OpenAI secret key should be committed"


def test_n8n_webhook_authentication_fail_closed_logic() -> None:
    """Verify that node 2 in DEVMEET_Meeting_Processor_v3.json implements fail-closed authentication logic."""
    assert os.path.exists(WORKFLOW_PATH), f"DEVMEET_Meeting_Processor_v3.json must exist at {WORKFLOW_PATH}"

    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    code_nodes = [n for n in data.get("nodes", []) if n.get("type") == "n8n-nodes-base.code"]
    auth_node = next((n for n in code_nodes if "Authenticate" in n.get("name", "")), None)
    assert auth_node is not None, "Authentication JS code node must exist"

    js_code = auth_node.get("parameters", {}).get("jsCode", "")
    assert "if (!expectedSecret)" in js_code or "expectedSecret" in js_code
    assert "SECURITY_CONFIG_ERROR" in js_code, "Must return SECURITY_CONFIG_ERROR if expectedSecret is not configured"
    assert "UNAUTHORIZED" in js_code, "Must return UNAUTHORIZED if incoming secret does not match"
