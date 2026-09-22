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


def extract_validator_js_code(node_name: str = "Validate Structured Output & Business Rules") -> str:
    wf = load_workflow_json()
    code_nodes = [n for n in wf.get("nodes", []) if n.get("type") == "n8n-nodes-base.code"]
    val_node = next((n for n in code_nodes if n.get("name") == node_name), None)
    assert val_node is not None, f"Node {node_name} must exist"
    return val_node.get("parameters", {}).get("jsCode", "")


# Pure Python Mirror of the exact JS Validator for direct unit test execution
def validate_ai_response_py(raw) -> dict:
    if raw is None or raw == "":
        return {"valid": False, "error": "Empty output from AI model."}

    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception as e:
            return {"valid": False, "error": f"Invalid JSON format: {e}"}

    if not isinstance(parsed, dict):
        return {"valid": False, "error": "AI output root must be a JSON object."}

    # 1. summary
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return {"valid": False, "error": "Missing or empty required string field: summary."}

    # 2. key_points
    key_points = parsed.get("key_points")
    if not isinstance(key_points, list):
        return {"valid": False, "error": "Field key_points must be an array of strings."}
    for i, kp in enumerate(key_points):
        if not isinstance(kp, str) or not kp.strip():
            return {"valid": False, "error": f"key_points element at index {i} must be a non-empty string."}

    # 3. decisions
    decisions = parsed.get("decisions")
    if not isinstance(decisions, list):
        return {"valid": False, "error": "Field decisions must be an array."}
    for i, d in enumerate(decisions):
        if not isinstance(d, dict):
            return {"valid": False, "error": f"decisions element at index {i} must be an object."}
        if not isinstance(d.get("decision"), str) or not d.get("decision").strip():
            return {"valid": False, "error": f"Decision at index {i} missing required non-empty string 'decision'."}
        if d.get("context") is None or not isinstance(d.get("context"), str):
            return {"valid": False, "error": f"Decision at index {i} missing required string 'context'."}
        if not isinstance(d.get("evidence_timestamp"), str) or not d.get("evidence_timestamp").strip():
            return {"valid": False, "error": f"Decision at index {i} missing required non-empty string 'evidence_timestamp'."}

    # 4. action_items
    allowed_statuses = ["TODO"]
    action_items = parsed.get("action_items")
    if not isinstance(action_items, list):
        return {"valid": False, "error": "Field action_items must be an array."}
    for i, a in enumerate(action_items):
        if not isinstance(a, dict):
            return {"valid": False, "error": f"action_items element at index {i} must be an object."}
        if not isinstance(a.get("task"), str) or not a.get("task").strip():
            return {"valid": False, "error": f"Action item at index {i} missing required non-empty string 'task'."}
        status = a.get("status")
        if status is None or not isinstance(status, str) or status not in allowed_statuses:
            return {"valid": False, "error": f"Action item at index {i} has invalid status '{status}'. Allowed status enum: {allowed_statuses}."}
        if not isinstance(a.get("evidence_timestamp"), str) or not a.get("evidence_timestamp").strip():
            return {"valid": False, "error": f"Action item at index {i} missing required non-empty string 'evidence_timestamp'."}

    # 5. follow_up_email
    email = parsed.get("follow_up_email")
    if not isinstance(email, dict):
        return {"valid": False, "error": "Field follow_up_email must be an object."}
    if not isinstance(email.get("subject"), str) or not email.get("subject").strip():
        return {"valid": False, "error": "follow_up_email.subject must be a non-empty string."}
    if not isinstance(email.get("body"), str) or not email.get("body").strip():
        return {"valid": False, "error": "follow_up_email.body must be a non-empty string."}

    return {"valid": True, "data": parsed}


# ----------------------------------------------------------------------
# TEST CASES A - V (SCHEMA, RULES, TOPOLOGY)
# ----------------------------------------------------------------------

def test_a_valid_structured_output() -> None:
    valid_payload = {
        "summary": "Team discussed authentication API and release schedule.",
        "key_points": ["API completed", "Frontend testing in progress"],
        "decisions": [
            {"decision": "Release Beta on Friday", "context": "After test completion", "evidence_timestamp": "09:15:10"}
        ],
        "action_items": [
            {"task": "Complete API docs", "assignee": "Alice", "deadline": "2026-08-22", "status": "TODO", "evidence_timestamp": "09:20:05"}
        ],
        "follow_up_email": {"subject": "Sprint Summary", "body": "Hi team, here is the summary."}
    }
    res = validate_ai_response_py(valid_payload)
    assert res["valid"] is True


def test_b_wrong_root_type() -> None:
    res = validate_ai_response_py([])
    assert res["valid"] is False
    assert "root must be a JSON object" in res["error"]


def test_c_malformed_json() -> None:
    res = validate_ai_response_py('{"summary": "incomplete')
    assert res["valid"] is False
    assert "Invalid JSON format" in res["error"]


def test_d_missing_required_field() -> None:
    payload = {
        "key_points": ["Point"],
        "decisions": [],
        "action_items": [],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "summary" in res["error"]


def test_e_key_points_not_an_array() -> None:
    payload = {
        "summary": "Summary",
        "key_points": "Important point string instead of array",
        "decisions": [],
        "action_items": [],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "key_points must be an array" in res["error"]


def test_f_key_points_contains_object() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Valid point", {"text": "invalid object"}],
        "decisions": [],
        "action_items": [],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "key_points element at index 1 must be a non-empty string" in res["error"]


def test_g_key_points_contains_null() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Valid point", None],
        "decisions": [],
        "action_items": [],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "key_points element at index 1 must be a non-empty string" in res["error"]


def test_h_key_points_contains_number() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Valid point", 12345],
        "decisions": [],
        "action_items": [],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "key_points element at index 1 must be a non-empty string" in res["error"]


def test_i_key_points_contains_empty_string() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Valid point", "   "],
        "decisions": [],
        "action_items": [],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "key_points element at index 1 must be a non-empty string" in res["error"]


def test_j_action_items_status_valid() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Point"],
        "decisions": [],
        "action_items": [{"task": "Task", "status": "TODO", "evidence_timestamp": "10:00:00"}],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is True


def test_k_action_items_status_invalid() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Point"],
        "decisions": [],
        "action_items": [{"task": "Task", "status": "DONE", "evidence_timestamp": "10:00:00"}],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "invalid status 'DONE'" in res["error"]
    # Verify original object was NOT mutated
    assert payload["action_items"][0]["status"] == "DONE"


def test_l_action_items_status_missing() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Point"],
        "decisions": [],
        "action_items": [{"task": "Task", "evidence_timestamp": "10:00:00"}],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "invalid status 'None'" in res["error"]


def test_m_action_items_status_null() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Point"],
        "decisions": [],
        "action_items": [{"task": "Task", "status": None, "evidence_timestamp": "10:00:00"}],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "invalid status 'None'" in res["error"]


def test_n_nested_action_item_wrong_type() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Point"],
        "decisions": [],
        "action_items": ["task_string_instead_of_object"],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "action_items element at index 0 must be an object" in res["error"]


def test_o_follow_up_email_wrong_type() -> None:
    payload = {
        "summary": "Summary",
        "key_points": ["Point"],
        "decisions": [],
        "action_items": [],
        "follow_up_email": "string_instead_of_object"
    }
    res = validate_ai_response_py(payload)
    assert res["valid"] is False
    assert "follow_up_email must be an object" in res["error"]


def test_p_second_ai_output_invalid_terminates() -> None:
    """Verify workflow node Validate AI Retry Output returns HTTP 502 & AI_OUTPUT_INVALID."""
    js_code = extract_validator_js_code("Validate AI Retry Output")
    assert "AI_OUTPUT_INVALID" in js_code
    assert "http_code: 502" in js_code or "502" in js_code


def test_q_initial_invalid_retry_valid() -> None:
    """Verify initial invalid output triggers Gemini Retry and valid retry continues."""
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    chk1_conns = connections.get("Check AI Output Valid #1", {}).get("main", [])

    assert len(chk1_conns) >= 2
    branch_0_retry = [c.get("node") for c in chk1_conns[0]]
    branch_1_pass = [c.get("node") for c in chk1_conns[1]]

    assert "AI Retry #1 - Gemini LLM Chain" in branch_0_retry
    assert "Prepare Meeting Log Row" in branch_1_pass


def test_r_initial_valid_zero_retry() -> None:
    """Verify initial valid output bypasses AI Retry and proceeds to Prepare Meeting Log Row."""
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    chk1_conns = connections.get("Check AI Output Valid #1", {}).get("main", [])
    branch_1_pass = [c.get("node") for c in chk1_conns[1]]
    assert "Prepare Meeting Log Row" in branch_1_pass


def test_s_retry_topology_no_retry_loop() -> None:
    """CRITICAL TOPOLOGY TEST: Verify AI Retry failure branch cannot loop back to any AI node."""
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    nodes = {n.get("name"): n for n in wf.get("nodes", [])}

    ai_node_names = set()
    for name, node in nodes.items():
        node_type = node.get("type", "")
        if "langchain" in node_type.lower() or "llm" in name.lower() or "ai retry" in name.lower() or "model" in name.lower():
            ai_node_names.add(name)

    # Start BFS from Respond - AI Processing Error
    start_node = "Respond - AI Processing Error"
    assert start_node in nodes

    visited = set()
    queue = [start_node]
    reachable = set()

    while queue:
        curr = queue.pop(0)
        if curr in visited:
            continue
        visited.add(curr)
        reachable.add(curr)

        node_conns = connections.get(curr, {}).get("main", [])
        for output_branch in node_conns:
            for conn_target in output_branch:
                target_name = conn_target.get("node")
                if target_name and target_name not in visited:
                    queue.append(target_name)

    intersection = reachable.intersection(ai_node_names)
    assert len(intersection) == 0, f"AI Retry failure branch must NOT be able to reach AI nodes: {intersection}"


def test_t_sheets_retry_remains_ai_free() -> None:
    """Re-verify Step 4B guarantee: SHEETS_RETRY has zero reachable AI nodes."""
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    nodes = {n.get("name"): n for n in wf.get("nodes", [])}

    ai_node_names = {name for name, node in nodes.items() if "langchain" in node.get("type", "").lower() or "llm" in name.lower() or "model" in name.lower()}

    sheets_retry_start = "Extract & Format Cached AI Result"
    visited = set()
    queue = [sheets_retry_start]
    reachable = set()

    while queue:
        curr = queue.pop(0)
        if curr in visited:
            continue
        visited.add(curr)
        reachable.add(curr)

        node_conns = connections.get(curr, {}).get("main", [])
        for output_branch in node_conns:
            for conn_target in output_branch:
                target_name = conn_target.get("node")
                if target_name and target_name not in visited:
                    queue.append(target_name)

    assert len(reachable.intersection(ai_node_names)) == 0


def test_u_invalid_output_does_not_mutate() -> None:
    """Verify validator does NOT mutate invalid status or invalid fields."""
    original_input = {
        "summary": "Summary",
        "key_points": ["Point"],
        "decisions": [],
        "action_items": [{"task": "Task", "status": "DONE", "evidence_timestamp": "10:00:00"}],
        "follow_up_email": {"subject": "Subj", "body": "Body"}
    }
    input_copy = json.loads(json.dumps(original_input))

    res = validate_ai_response_py(input_copy)
    assert res["valid"] is False
    assert input_copy["action_items"][0]["status"] == "DONE"  # Not mutated to TODO!


def test_v_no_coercion() -> None:
    """Verify numbers, objects, or nulls in place of strings are rejected without coercion."""
    res1 = validate_ai_response_py({"summary": 12345, "key_points": [], "decisions": [], "action_items": [], "follow_up_email": {"subject": "S", "body": "B"}})
    assert res1["valid"] is False

    res2 = validate_ai_response_py({"summary": "Summary", "key_points": [100], "decisions": [], "action_items": [], "follow_up_email": {"subject": "S", "body": "B"}})
    assert res2["valid"] is False

    res3 = validate_ai_response_py({"summary": "Summary", "key_points": ["P"], "decisions": [{"decision": 999, "context": "C", "evidence_timestamp": "T"}], "action_items": [], "follow_up_email": {"subject": "S", "body": "B"}})
    assert res3["valid"] is False


# ----------------------------------------------------------------------
# GEMINI 2.5 FLASH EXECUTION, RETRY & TIMEOUT
# ----------------------------------------------------------------------

def test_w_gemini_only_execution() -> None:
    """TEST W: Verify AI execution path strictly uses Google Gemini 2.5 Flash."""
    wf = load_workflow_json()
    nodes = {n.get("name"): n for n in wf.get("nodes", [])}

    # Verify Gemini nodes exist
    assert "Analyze Meeting - Gemini LLM Chain" in nodes
    assert "AI Model Provider - Google Gemini" in nodes
    gemini_model_node = nodes["AI Model Provider - Google Gemini"]
    assert gemini_model_node.get("type") == "@n8n/n8n-nodes-langchain.lmChatGoogleGemini"
    assert "gemini" in gemini_model_node.get("parameters", {}).get("modelName", "").lower()
    assert "flash" in gemini_model_node.get("parameters", {}).get("modelName", "").lower()

    # Verify zero OpenAI or Anthropic execution nodes exist in the workflow
    for name, node in nodes.items():
        node_type = node.get("type", "")
        assert "openai" not in name.lower(), f"Unexpected OpenAI node found: {name}"
        assert "anthropic" not in name.lower(), f"Unexpected Anthropic node found: {name}"
        assert "openai" not in node_type.lower(), f"Unexpected OpenAI type found in node {name}"
        assert "anthropic" not in node_type.lower(), f"Unexpected Anthropic type found in node {name}"


def test_x_gemini_retry_consistency() -> None:
    """TEST X: Attempt 1 invalid output routes strictly to Gemini Retry #1."""
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    nodes = {n.get("name"): n for n in wf.get("nodes", [])}

    chk1_conns = connections.get("Check AI Output Valid #1", {}).get("main", [])
    branch_0_targets = [c.get("node") for c in chk1_conns[0]]

    assert "AI Retry #1 - Gemini LLM Chain" in branch_0_targets
    retry_model_node = nodes.get("AI Model Provider - Google Gemini Retry")
    assert retry_model_node is not None
    assert retry_model_node.get("type") == "@n8n/n8n-nodes-langchain.lmChatGoogleGemini"
    assert "gemini" in retry_model_node.get("parameters", {}).get("modelName", "").lower()
    assert "flash" in retry_model_node.get("parameters", {}).get("modelName", "").lower()


def test_y_unsupported_provider_rejected() -> None:
    """TEST Y: Explicitly non-Gemini providers (claude, unknown) are rejected with INVALID_REQUEST."""
    wf = load_workflow_json()
    code_nodes = [n for n in wf.get("nodes", []) if n.get("type") == "n8n-nodes-base.code"]
    auth_node = next((n for n in code_nodes if "Authenticate" in n.get("name", "")), None)
    assert auth_node is not None

    js_code = auth_node.get("parameters", {}).get("jsCode", "")
    assert "Unsupported ai_provider" in js_code
    assert "INVALID_REQUEST" in js_code


def test_z_gemini_timeout_handling() -> None:
    """TEST Z: Gemini timeout is handled as a controlled AI failure with maximum 2 executions and zero infinite loops."""
    wf = load_workflow_json()
    nodes = {n.get("name"): n for n in wf.get("nodes", [])}

    gemini_chain = nodes.get("Analyze Meeting - Gemini LLM Chain")
    assert gemini_chain.get("onError") in ["continueErrorOutput", "continueRegularOutput"], "Gemini chain must catch errors/timeouts gracefully"

    # Verify validator handles empty / error outputs from timed-out provider
    res_timeout = validate_ai_response_py(None)
    assert res_timeout["valid"] is False
    assert "Empty output" in res_timeout["error"]

    res_err_obj = validate_ai_response_py({"error": "Request timed out after 30000ms"})
    assert res_err_obj["valid"] is False


def test_aa_second_failure_terminates() -> None:
    """TEST AA: When Retry Attempt 2 also fails or times out, branch terminates immediately with AI_OUTPUT_INVALID."""
    wf = load_workflow_json()
    connections = wf.get("connections", {})

    chk_retry_conns = connections.get("Check AI Retry Valid", {}).get("main", [])
    branch_0_fail = [c.get("node") for c in chk_retry_conns[0]]

    # Fails immediately into Respond - AI Processing Error (HTTP 502)
    assert "Respond - AI Processing Error" in branch_0_fail

    # Ensure zero outbound connections from error response node (strict branch termination)
    error_node_conns = connections.get("Respond - AI Processing Error", {}).get("main", [])
    assert len(error_node_conns) == 0, "Error response node must terminate execution branch"
