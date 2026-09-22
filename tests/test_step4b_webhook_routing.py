import json
import os
import pytest

# Locate root directory containing ai_workflow
CURR_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = CURR_DIR
while not os.path.exists(os.path.join(ROOT_DIR, "ai_workflow")) and os.path.dirname(ROOT_DIR) != ROOT_DIR:
    ROOT_DIR = os.path.dirname(ROOT_DIR)

WORKFLOW_PATH = os.path.join(ROOT_DIR, "ai_workflow", "DEVMEET_Meeting_Processor_v3.json")


def load_workflow_json() -> dict:
    assert os.path.exists(WORKFLOW_PATH), f"Workflow file must exist at {WORKFLOW_PATH}"
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_topology_sheets_retry_has_zero_path_to_ai_nodes() -> None:
    """CRITICAL TOPOLOGY TEST: Verify SHEETS_RETRY branch has NO graph path to any AI node."""
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    nodes = {n.get("name"): n for n in wf.get("nodes", [])}

    # Identify all AI nodes
    ai_node_names = set()
    for name, node in nodes.items():
        node_type = node.get("type", "")
        if "langchain" in node_type.lower() or "llm" in name.lower() or "ai retry" in name.lower() or "model" in name.lower():
            ai_node_names.add(name)

    assert "Analyze Meeting - Gemini LLM Chain" in ai_node_names
    assert "AI Retry #1 - Gemini LLM Chain" in ai_node_names

    # Start graph traversal from SHEETS_RETRY branch node
    sheets_retry_start = "Extract & Format Cached AI Result"
    assert sheets_retry_start in nodes, "Node 'Extract & Format Cached AI Result' must exist in workflow"

    # Breadth-first search (BFS) downstream from SHEETS_RETRY start node
    visited = set()
    queue = [sheets_retry_start]

    reachable_from_sheets_retry = set()

    while queue:
        curr = queue.pop(0)
        if curr in visited:
            continue
        visited.add(curr)
        reachable_from_sheets_retry.add(curr)

        # Get downstream connections
        node_conns = connections.get(curr, {}).get("main", [])
        for output_branch in node_conns:
            for conn_target in output_branch:
                target_name = conn_target.get("node")
                if target_name and target_name not in visited:
                    queue.append(target_name)

    # Hard Assertion: Zero AI nodes must be reachable from SHEETS_RETRY path
    intersection = reachable_from_sheets_retry.intersection(ai_node_names)
    assert len(intersection) == 0, f"SHEETS_RETRY branch must NOT be able to reach AI nodes: {intersection}"


def test_action_a_ai_process_valid_contract() -> None:
    """Test A: AI_PROCESS action contract validation."""
    wf = load_workflow_json()
    code_nodes = [n for n in wf.get("nodes", []) if n.get("type") == "n8n-nodes-base.code"]
    auth_node = next((n for n in code_nodes if "Authenticate" in n.get("name", "")), None)
    assert auth_node is not None

    js_code = auth_node.get("parameters", {}).get("jsCode", "")
    assert "action === 'AI_PROCESS'" in js_code
    assert "transcript_prompt_text" in js_code
    assert "selected_source" in js_code


def test_action_b_sheets_retry_valid_contract() -> None:
    """Test B: SHEETS_RETRY action contract validation."""
    wf = load_workflow_json()
    code_nodes = [n for n in wf.get("nodes", []) if n.get("type") == "n8n-nodes-base.code"]
    auth_node = next((n for n in code_nodes if "Authenticate" in n.get("name", "")), None)
    assert auth_node is not None

    js_code = auth_node.get("parameters", {}).get("jsCode", "")
    assert "action === 'SHEETS_RETRY'" in js_code
    assert "cached_ai_result" in js_code


def test_action_d_ai_process_routes_to_ai_path() -> None:
    """Test D: AI_PROCESS action routes to AI branch in Check Action Router."""
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    router_conns = connections.get("Check Action Router", {}).get("main", [])

    assert len(router_conns) >= 2, "Check Action Router must have at least 2 branches"
    branch_0_targets = [c.get("node") for c in router_conns[0]]
    assert "Analyze Meeting - Gemini LLM Chain" in branch_0_targets, "Branch 0 (AI_PROCESS) must route to Gemini LLM Chain"


def test_action_e_invalid_action_rejected() -> None:
    """Test E: Invalid action rejected with INVALID_ACTION code."""
    wf = load_workflow_json()
    code_nodes = [n for n in wf.get("nodes", []) if n.get("type") == "n8n-nodes-base.code"]
    auth_node = next((n for n in code_nodes if "Authenticate" in n.get("name", "")), None)
    assert auth_node is not None

    js_code = auth_node.get("parameters", {}).get("jsCode", "")
    assert "INVALID_ACTION" in js_code


def test_action_f_g_h_missing_required_fields_rejected() -> None:
    """Test F, G, H: Missing request_id, meeting_id, or transcript_prompt_text rejected with INVALID_REQUEST."""
    wf = load_workflow_json()
    code_nodes = [n for n in wf.get("nodes", []) if n.get("type") == "n8n-nodes-base.code"]
    auth_node = next((n for n in code_nodes if "Authenticate" in n.get("name", "")), None)
    assert auth_node is not None

    js_code = auth_node.get("parameters", {}).get("jsCode", "")
    assert "INVALID_REQUEST" in js_code
    assert "request_id is required" in js_code
    assert "meeting_id is required" in js_code


def test_action_i_sheets_retry_missing_cached_result_rejected() -> None:
    """Test I: SHEETS_RETRY missing cached_ai_result rejected with CACHED_AI_RESULT_REQUIRED."""
    wf = load_workflow_json()
    code_nodes = [n for n in wf.get("nodes", []) if n.get("type") == "n8n-nodes-base.code"]
    auth_node = next((n for n in code_nodes if "Authenticate" in n.get("name", "")), None)
    assert auth_node is not None

    js_code = auth_node.get("parameters", {}).get("jsCode", "")
    assert "CACHED_AI_RESULT_REQUIRED" in js_code


def test_action_j_k_fail_closed_authentication() -> None:
    """Test J & K: Webhook secret authentication is fail-closed."""
    wf = load_workflow_json()
    code_nodes = [n for n in wf.get("nodes", []) if n.get("type") == "n8n-nodes-base.code"]
    auth_node = next((n for n in code_nodes if "Authenticate" in n.get("name", "")), None)
    assert auth_node is not None

    js_code = auth_node.get("parameters", {}).get("jsCode", "")
    assert "SECURITY_CONFIG_ERROR" in js_code
    assert "UNAUTHORIZED" in js_code


def test_action_l_postgresql_isolation() -> None:
    """Test L: Workflow contains zero PostgreSQL nodes or database connection credentials."""
    wf = load_workflow_json()
    nodes = wf.get("nodes", [])
    node_types = [n.get("type", "") for n in nodes]

    assert not any("postgres" in t.lower() for t in node_types), "Zero postgres nodes permitted in n8n"
    raw_json = json.dumps(wf)
    assert "POSTGRES" not in raw_json, "Zero postgres env vars or strings permitted in n8n workflow"
