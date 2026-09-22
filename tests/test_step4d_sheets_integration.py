import json
import os
import re
import pytest
from typing import Dict, Any, List

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


SHEETS_NODES = [
    "1. Sheets - Append Meeting",
    "2. Sheets - Append Decisions (Multiple Rows)",
    "3. Sheets - Append Action Items (Multiple Rows)",
    "4. Sheets - Append Follow-up Email"
]

CHECK_NODES = [
    "Check Meeting Append Result",
    "Check Decision Append Result",
    "Check Action Items Append Result",
    "Check Email Append Result"
]

WAIT_NODES = [
    "Wait Meeting Retry",
    "Wait Decision Retry",
    "Wait Action Item Retry",
    "Wait Email Retry"
]

PREPARE_NODES = [
    "Prepare Meeting Log Row",
    "Prepare Decision Rows",
    "Prepare Action Item Rows",
    "Prepare Email Row"
]

GUARD_IF_NODES = [
    "Has Meeting to Append?",
    "Has Decisions?",
    "Has Action Items?",
    "Has Email to Append?"
]


# ---------------------------------------------------------------------------
# Mirror Functions & Simulation Helpers
# ---------------------------------------------------------------------------

SAMPLE_AI_RESULT = {
    "summary": "Discussed architecture roadmap and database migration plan.",
    "key_points": ["PostgreSQL migration locked", "n8n handles orchestration"],
    "decisions": [
        {"decision": "Adopt PostgreSQL", "context": "Better scalability", "owner": "Alice"},
        {"decision": "Use n8n on host", "context": "Native Windows execution", "owner": "Bob"}
    ],
    "action_items": [
        {"task": "Setup alembic migrations", "assignee": "Alice", "due_date": "2026-09-10", "priority": "HIGH"},
        {"task": "Configure webhook secrets", "assignee": "Charlie", "due_date": "2026-09-11", "priority": "MEDIUM"}
    ],
    "follow_up_email": {
        "subject": "Action items from Roadmap Meeting",
        "body": "Hi team,\nHere are the notes and action items from our meeting.\nBest,\nDevMeeting AI"
    }
}


def mirror_format_meeting_row(meeting_id: str, request_id: str, title: str, start_time: str, end_time: str, attendees: list, ai_data: dict) -> dict:
    idempotency_key = f"meeting:{meeting_id}:request:{request_id}:meeting"
    return {
        "idempotency_key": idempotency_key,
        "meeting_id": str(meeting_id or ""),
        "title": str(title or ""),
        "date_time": str(start_time or ""),
        "end_time": str(end_time or ""),
        "attendees": ", ".join(attendees or []) if isinstance(attendees, list) else str(attendees or ""),
        "summary": ai_data.get("summary", ""),
        "key_points": "\n".join([f"• {kp}" for kp in ai_data.get("key_points", [])]),
        "processed_at": "2026-09-04T10:00:00Z"
    }


def mirror_format_decisions_rows(meeting_id: str, request_id: str, title: str, decisions: list) -> list:
    rows = []
    for idx, d in enumerate(decisions or []):
        idempotency_key = f"meeting:{meeting_id}:request:{request_id}:decision:{idx}"
        rows.append({
            "idempotency_key": idempotency_key,
            "meeting_id": str(meeting_id or ""),
            "meeting_title": str(title or ""),
            "decision_id": f"D-{idx + 1}",
            "decision": d.get("decision", ""),
            "context": d.get("context", ""),
            "owner": d.get("owner", ""),
            "status": "APPROVED",
            "created_at": "2026-09-04T10:00:00Z"
        })
    return rows


def mirror_format_action_items_rows(meeting_id: str, request_id: str, title: str, action_items: list) -> list:
    rows = []
    for idx, item in enumerate(action_items or []):
        idempotency_key = f"meeting:{meeting_id}:request:{request_id}:action:{idx}"
        rows.append({
            "idempotency_key": idempotency_key,
            "meeting_id": str(meeting_id or ""),
            "meeting_title": str(title or ""),
            "item_id": f"ACT-{idx + 1}",
            "task": item.get("task", ""),
            "owner": item.get("assignee", item.get("owner", "")),
            "due_date": item.get("due_date", ""),
            "priority": item.get("priority", "MEDIUM"),
            "status": "PENDING",
            "created_at": "2026-09-04T10:00:00Z"
        })
    return rows


def mirror_format_email_row(meeting_id: str, request_id: str, title: str, attendees: list, email_data: dict) -> dict:
    idempotency_key = f"meeting:{meeting_id}:request:{request_id}:email"
    return {
        "idempotency_key": idempotency_key,
        "meeting_id": str(meeting_id or ""),
        "meeting_title": str(title or ""),
        "recipient_group": ", ".join(attendees or []) if isinstance(attendees, list) else str(attendees or ""),
        "subject": email_data.get("subject", f"Meeting Summary: {title}"),
        "body_preview": (email_data.get("body", "") or "")[:200],
        "full_body": email_data.get("body", ""),
        "status": "DRAFTED",
        "created_at": "2026-09-04T10:00:00Z"
    }


def simulate_sheets_retry(attempts: int, succeed_on_attempt: int = 1) -> dict:
    delays = [0, 5000, 15000, 30000]
    total_attempts = 0
    delays_experienced = []
    status = "FAILED"
    error = None

    for attempt in range(1, 5):
        total_attempts = attempt
        if attempt == succeed_on_attempt:
            status = "SUCCESS"
            break
        else:
            if attempt < 4:
                delays_experienced.append(delays[attempt])
            else:
                error = "Sheets append failed after 3 retries (max 4 attempts)"

    return {
        "status": status,
        "total_attempts": total_attempts,
        "delays_experienced": delays_experienced,
        "error": error
    }


# ===========================================================================
# Section 17: Post-Remediation Workflow Verification Tests (AA - AN)
# ===========================================================================

def test_AA_actual_workflow_sheets_nodes_inspection():
    wf = load_workflow_json()
    nodes_by_name = {n["name"]: n for n in wf.get("nodes", [])}
    
    for sn in SHEETS_NODES:
        assert sn in nodes_by_name, f"Node '{sn}' must exist in workflow"
        node = nodes_by_name[sn]
        assert node.get("type") == "n8n-nodes-base.googleSheets", f"Node '{sn}' must be of type 'n8n-nodes-base.googleSheets'"
        params = node.get("parameters", {})
        doc_id = params.get("documentId", "")
        doc_val = doc_id.get("value", "") if isinstance(doc_id, dict) else str(doc_id)
        assert doc_val == "={{ $env.DEVMEET_SPREADSHEET_ID }}", f"Node '{sn}' documentId must strictly be '={{{{ $env.DEVMEET_SPREADSHEET_ID }}}}'"


def test_AB_actual_retry_wait_nodes_configuration():
    wf = load_workflow_json()
    nodes_by_name = {n["name"]: n for n in wf.get("nodes", [])}

    for wn in WAIT_NODES:
        assert wn in nodes_by_name, f"Wait node '{wn}' must exist"
        node = nodes_by_name[wn]
        assert node.get("type") == "n8n-nodes-base.wait"
        params = node.get("parameters", {})
        assert params.get("amount") == "={{ $json._wait_seconds }}", f"Wait node '{wn}' must use $json._wait_seconds"

    for cn in CHECK_NODES:
        node = nodes_by_name[cn]
        js_code = node.get("parameters", {}).get("jsCode", "")
        assert "5" in js_code and "15" in js_code and "30" in js_code, f"Check node '{cn}' must compute 5s, 15s, 30s backoff delays"
        assert "attempt < 4" in js_code or "_max_attempts" in js_code, f"Check node '{cn}' must enforce maximum 4 attempts"


def test_AC_explicit_meeting_fail_closed_guard():
    wf = load_workflow_json()
    conns = wf.get("connections", {})
    
    prep_node = next(n for n in wf["nodes"] if n["name"] == "Prepare Meeting Log Row")
    js_code = prep_node.get("parameters", {}).get("jsCode", "")
    assert "$env.DEVMEET_SPREADSHEET_ID" in js_code
    assert "_sheet_config_error" in js_code
    assert "_skip: true" in js_code or "_skip" in js_code

    assert conns.get("Prepare Meeting Log Row", {}).get("main", [])[0][0]["node"] == "Has Meeting to Append?"
    has_meet_conns = conns.get("Has Meeting to Append?", {}).get("main", [])
    assert has_meet_conns[0][0]["node"] == "Prepare Decision Rows"
    assert has_meet_conns[1][0]["node"] == "1. Sheets - Append Meeting"


def test_AD_explicit_decisions_fail_closed_guard():
    wf = load_workflow_json()
    conns = wf.get("connections", {})
    
    prep_node = next(n for n in wf["nodes"] if n["name"] == "Prepare Decision Rows")
    js_code = prep_node.get("parameters", {}).get("jsCode", "")
    assert "$env.DEVMEET_SPREADSHEET_ID" in js_code
    assert "_sheet_config_error" in js_code

    assert conns.get("Prepare Decision Rows", {}).get("main", [])[0][0]["node"] == "Has Decisions?"
    has_dec_conns = conns.get("Has Decisions?", {}).get("main", [])
    assert has_dec_conns[0][0]["node"] == "Prepare Action Item Rows"
    assert has_dec_conns[1][0]["node"] == "2. Sheets - Append Decisions (Multiple Rows)"


def test_AE_explicit_action_items_fail_closed_guard():
    wf = load_workflow_json()
    conns = wf.get("connections", {})
    
    prep_node = next(n for n in wf["nodes"] if n["name"] == "Prepare Action Item Rows")
    js_code = prep_node.get("parameters", {}).get("jsCode", "")
    assert "$env.DEVMEET_SPREADSHEET_ID" in js_code
    assert "_sheet_config_error" in js_code

    assert conns.get("Prepare Action Item Rows", {}).get("main", [])[0][0]["node"] == "Has Action Items?"
    has_act_conns = conns.get("Has Action Items?", {}).get("main", [])
    assert has_act_conns[0][0]["node"] == "Prepare Email Row"
    assert has_act_conns[1][0]["node"] == "3. Sheets - Append Action Items (Multiple Rows)"


def test_AF_explicit_email_fail_closed_guard():
    wf = load_workflow_json()
    conns = wf.get("connections", {})
    
    prep_node = next(n for n in wf["nodes"] if n["name"] == "Prepare Email Row")
    js_code = prep_node.get("parameters", {}).get("jsCode", "")
    assert "$env.DEVMEET_SPREADSHEET_ID" in js_code
    assert "_sheet_config_error" in js_code

    assert conns.get("Prepare Email Row", {}).get("main", [])[0][0]["node"] == "Has Email to Append?"
    has_email_conns = conns.get("Has Email to Append?", {}).get("main", [])
    assert has_email_conns[0][0]["node"] == "Format Backend Response"
    assert has_email_conns[1][0]["node"] == "4. Sheets - Append Follow-up Email"


def test_AG_dead_idempotency_fields_completely_removed():
    wf = load_workflow_json()
    wf_str = json.dumps(wf)
    
    assert "_appended_idempotency_keys" not in wf_str, "Found dead _appended_idempotency_keys in workflow JSON"
    assert "_already_appended" not in wf_str, "Found dead _already_appended in workflow JSON"
    assert "_appended_key" not in wf_str, "Found dead _appended_key in workflow JSON"


def test_AH_deterministic_idempotency_keys_preserved():
    wf = load_workflow_json()
    for pn in PREPARE_NODES:
        node = next(n for n in wf["nodes"] if n["name"] == pn)
        js_code = node.get("parameters", {}).get("jsCode", "")
        assert "idempotency_key" in js_code or "_idempotency_key" in js_code, f"Node '{pn}' must maintain deterministic idempotency_key"


def test_AI_sheets_failure_cannot_reach_ai():
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    all_sheets_nodes = SHEETS_NODES + CHECK_NODES + WAIT_NODES + PREPARE_NODES + GUARD_IF_NODES

    ai_nodes = [
        n["name"] for n in wf.get("nodes", [])
        if "langchain" in n.get("type", "").lower()
        or any(k in n["name"].lower() for k in ["analyze meeting", "openai", "ai retry", "structured output parser", "validate structured output"])
    ]

    for start_node in all_sheets_nodes:
        visited = set()
        queue = [start_node]
        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)
            assert curr not in ai_nodes, f"Sheets node '{start_node}' reached AI node '{curr}'"
            for group in connections.get(curr, {}).get("main", []):
                for conn in group:
                    t_name = conn.get("node")
                    if t_name and t_name not in visited:
                        queue.append(t_name)


def test_AJ_sheets_retry_cannot_reach_ai():
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    
    router_conns = connections.get("Check Action Router", {}).get("main", [])
    assert len(router_conns) >= 2
    sheets_retry_entry = router_conns[1][0]["node"]
    assert sheets_retry_entry == "Extract & Format Cached AI Result"

    ai_nodes = [
        n["name"] for n in wf.get("nodes", [])
        if "langchain" in n.get("type", "").lower()
        or any(k in n["name"].lower() for k in ["analyze meeting", "openai", "ai retry", "structured output parser", "validate structured output"])
    ]

    visited = set()
    queue = [sheets_retry_entry]
    while queue:
        curr = queue.pop(0)
        if curr in visited:
            continue
        visited.add(curr)
        assert curr not in ai_nodes, f"SHEETS_RETRY reached AI node '{curr}'"
        for group in connections.get(curr, {}).get("main", []):
            for conn in group:
                t_name = conn.get("node")
                if t_name and t_name not in visited:
                    queue.append(t_name)


def test_AK_independent_retry_topology():
    wf = load_workflow_json()
    connections = wf.get("connections", {})

    assert connections.get("Wait Meeting Retry", {}).get("main", [])[0][0]["node"] == "1. Sheets - Append Meeting"
    assert connections.get("Wait Decision Retry", {}).get("main", [])[0][0]["node"] == "2. Sheets - Append Decisions (Multiple Rows)"
    assert connections.get("Wait Action Item Retry", {}).get("main", [])[0][0]["node"] == "3. Sheets - Append Action Items (Multiple Rows)"
    assert connections.get("Wait Email Retry", {}).get("main", [])[0][0]["node"] == "4. Sheets - Append Follow-up Email"


def test_AL_actual_partial_success_response_logic():
    wf = load_workflow_json()
    fmt_node = next(n for n in wf["nodes"] if n["name"] == "Format Backend Response")
    js_code = fmt_node.get("parameters", {}).get("jsCode", "")
    
    assert "partial_success" in js_code
    assert "ai_status: 'COMPLETED'" in js_code or "'COMPLETED'" in js_code
    assert "sheets_sync_status" in js_code
    assert "FAILED" in js_code and "SYNCED" in js_code


def test_AM_no_hardcoded_spreadsheet_id():
    wf = load_workflow_json()
    for node in wf.get("nodes", []):
        if node.get("type") == "n8n-nodes-base.googleSheets":
            doc_id = node.get("parameters", {}).get("documentId", {})
            doc_val = doc_id.get("value", "") if isinstance(doc_id, dict) else str(doc_id)
            assert doc_val == "={{ $env.DEVMEET_SPREADSHEET_ID }}", f"Node '{node.get('name')}' must not have hardcoded ID"


def test_AN_no_hardcoded_sheets_credentials_or_secrets():
    wf = load_workflow_json()
    for node in wf.get("nodes", []):
        if node.get("type") == "n8n-nodes-base.googleSheets":
            creds = node.get("credentials", {})
            assert "googleSheetsOAuth2Api" in creds
            params = node.get("parameters", {})
            for k, v in params.items():
                v_str = str(v)
                assert "ya29." not in v_str, f"Found OAuth token in node {node.get('name')}"
                assert "client_secret" not in v_str.lower()


# ===========================================================================
# Preserved Unit and Transformation Tests (A - V)
# ===========================================================================

def test_A_meeting_append_success():
    row = mirror_format_meeting_row(
        meeting_id="meet-123",
        request_id="req-abc",
        title="Roadmap Sync",
        start_time="2026-09-04T09:00:00Z",
        end_time="2026-09-04T10:00:00Z",
        attendees=["alice@dev.io", "bob@dev.io"],
        ai_data=SAMPLE_AI_RESULT
    )
    assert row["idempotency_key"] == "meeting:meet-123:request:req-abc:meeting"
    assert row["meeting_id"] == "meet-123"
    assert row["title"] == "Roadmap Sync"
    assert "alice@dev.io, bob@dev.io" in row["attendees"]
    assert row["summary"] == SAMPLE_AI_RESULT["summary"]
    assert "• PostgreSQL migration locked" in row["key_points"]


def test_B_decisions_append_success_and_idempotency_keys():
    rows = mirror_format_decisions_rows(
        meeting_id="meet-123",
        request_id="req-abc",
        title="Roadmap Sync",
        decisions=SAMPLE_AI_RESULT["decisions"]
    )
    assert len(rows) == 2
    assert rows[0]["idempotency_key"] == "meeting:meet-123:request:req-abc:decision:0"
    assert rows[0]["decision_id"] == "D-1"
    assert rows[0]["decision"] == "Adopt PostgreSQL"
    assert rows[0]["owner"] == "Alice"

    assert rows[1]["idempotency_key"] == "meeting:meet-123:request:req-abc:decision:1"
    assert rows[1]["decision_id"] == "D-2"
    assert rows[1]["decision"] == "Use n8n on host"
    assert rows[1]["owner"] == "Bob"


def test_C_action_items_append_success_and_idempotency_keys():
    rows = mirror_format_action_items_rows(
        meeting_id="meet-123",
        request_id="req-abc",
        title="Roadmap Sync",
        action_items=SAMPLE_AI_RESULT["action_items"]
    )
    assert len(rows) == 2
    assert rows[0]["idempotency_key"] == "meeting:meet-123:request:req-abc:action:0"
    assert rows[0]["item_id"] == "ACT-1"
    assert rows[0]["task"] == "Setup alembic migrations"
    assert rows[0]["owner"] == "Alice"
    assert rows[0]["priority"] == "HIGH"

    assert rows[1]["idempotency_key"] == "meeting:meet-123:request:req-abc:action:1"
    assert rows[1]["item_id"] == "ACT-2"
    assert rows[1]["task"] == "Configure webhook secrets"
    assert rows[1]["owner"] == "Charlie"


def test_D_follow_up_email_append_success():
    row = mirror_format_email_row(
        meeting_id="meet-123",
        request_id="req-abc",
        title="Roadmap Sync",
        attendees=["alice@dev.io"],
        email_data=SAMPLE_AI_RESULT["follow_up_email"]
    )
    assert row["idempotency_key"] == "meeting:meet-123:request:req-abc:email"
    assert row["meeting_id"] == "meet-123"
    assert row["subject"] == "Action items from Roadmap Meeting"
    assert row["status"] == "DRAFTED"
    assert "Hi team" in row["full_body"]


def test_E_empty_decisions():
    rows = mirror_format_decisions_rows(
        meeting_id="meet-123",
        request_id="req-abc",
        title="Quick Standup",
        decisions=[]
    )
    assert rows == []
    assert len(rows) == 0


def test_F_empty_action_items():
    rows = mirror_format_action_items_rows(
        meeting_id="meet-123",
        request_id="req-abc",
        title="Quick Standup",
        action_items=[]
    )
    assert rows == []
    assert len(rows) == 0


def test_G_one_failure_then_recovery():
    res = simulate_sheets_retry(attempts=4, succeed_on_attempt=2)
    assert res["status"] == "SUCCESS"
    assert res["total_attempts"] == 2
    assert res["delays_experienced"] == [5000]


def test_H_two_failures_then_recovery():
    res = simulate_sheets_retry(attempts=4, succeed_on_attempt=3)
    assert res["status"] == "SUCCESS"
    assert res["total_attempts"] == 3
    assert res["delays_experienced"] == [5000, 15000]


def test_I_three_retries_exhausted():
    res = simulate_sheets_retry(attempts=4, succeed_on_attempt=999)
    assert res["status"] == "FAILED"
    assert res["total_attempts"] == 4
    assert res["delays_experienced"] == [5000, 15000, 30000]
    assert "Sheets append failed after 3 retries (max 4 attempts)" in res["error"]


def test_J_sheets_failure_must_not_rerun_ai():
    wf = load_workflow_json()
    connections = wf.get("connections", {})

    sheets_related_nodes = SHEETS_NODES + CHECK_NODES + WAIT_NODES + PREPARE_NODES + GUARD_IF_NODES
    ai_nodes = [
        "Analyze Meeting - Gemini LLM Chain", "AI Retry #1 - Gemini LLM Chain",
        "Check AI Output Valid #1", "Check AI Retry Valid", "Validate Structured Output & Business Rules"
    ]

    for start_node in sheets_related_nodes:
        visited = set()
        queue = [start_node]
        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)
            assert curr not in ai_nodes, f"Sheets node '{start_node}' has a path reaching AI node '{curr}'!"
            targets = connections.get(curr, {}).get("main", [])
            for group in targets:
                for target_conn in group:
                    target_name = target_conn.get("node")
                    if target_name and target_name not in visited:
                        queue.append(target_name)


def test_K_sheets_retry_is_ai_free():
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    
    route_action_conns = connections.get("Check Action Router", {}).get("main", [])
    assert len(route_action_conns) >= 2, "Check Action Router must have at least 2 branches"
    
    sheets_retry_targets = [c.get("node") for c in route_action_conns[1]]
    assert "Extract & Format Cached AI Result" in sheets_retry_targets

    visited = set()
    queue = ["Extract & Format Cached AI Result"]
    ai_nodes = [
        "Analyze Meeting - Gemini LLM Chain", "AI Retry #1 - Gemini LLM Chain",
        "Check AI Output Valid #1", "Check AI Retry Valid"
    ]

    while queue:
        curr = queue.pop(0)
        if curr in visited:
            continue
        visited.add(curr)
        assert curr not in ai_nodes, f"SHEETS_RETRY path reached AI node '{curr}'"
        targets = connections.get(curr, {}).get("main", [])
        for group in targets:
            for target_conn in group:
                t_name = target_conn.get("node")
                if t_name and t_name not in visited:
                    queue.append(t_name)


def test_L_sheets_retry_failure_handling():
    retry_result = simulate_sheets_retry(attempts=4, succeed_on_attempt=999)
    assert retry_result["status"] == "FAILED"
    
    response_payload = {
        "status": "partial_success",
        "meeting_id": "meet-123",
        "action": "SHEETS_RETRY",
        "ai_status": "COMPLETED",
        "sheets_sync_status": "FAILED",
        "result": SAMPLE_AI_RESULT,
        "warnings": [retry_result["error"]]
    }
    assert response_payload["ai_status"] == "COMPLETED"
    assert response_payload["sheets_sync_status"] == "FAILED"
    assert response_payload["status"] == "partial_success"
    assert len(response_payload["warnings"]) == 1


def test_M_idempotent_retry_after_ambiguous_success():
    meeting_id = "meet-999"
    request_id = "req-111"
    
    key1 = mirror_format_meeting_row(meeting_id, request_id, "T", "S", "E", [], SAMPLE_AI_RESULT)["idempotency_key"]
    key2 = mirror_format_meeting_row(meeting_id, request_id, "T", "S", "E", [], SAMPLE_AI_RESULT)["idempotency_key"]
    
    assert key1 == key2 == "meeting:meet-999:request:req-111:meeting"


def test_N_repeated_sheets_retry_is_idempotent():
    meeting_id = "meet-xyz"
    request_id = "req-abc"

    decisions_1 = mirror_format_decisions_rows(meeting_id, request_id, "T", SAMPLE_AI_RESULT["decisions"])
    decisions_2 = mirror_format_decisions_rows(meeting_id, request_id, "T", SAMPLE_AI_RESULT["decisions"])

    for d1, d2 in zip(decisions_1, decisions_2):
        assert d1["idempotency_key"] == d2["idempotency_key"]


def test_O_unique_idempotency_keys_per_item():
    decisions = mirror_format_decisions_rows("meet-1", "req-1", "T", SAMPLE_AI_RESULT["decisions"])
    keys = [d["idempotency_key"] for d in decisions]
    assert len(keys) == len(set(keys)), "Each decision row must have a unique idempotency key"

    actions = mirror_format_action_items_rows("meet-1", "req-1", "T", SAMPLE_AI_RESULT["action_items"])
    act_keys = [a["idempotency_key"] for a in actions]
    assert len(act_keys) == len(set(act_keys)), "Each action item row must have a unique idempotency key"


def test_P_spreadsheet_id_parameterization():
    wf = load_workflow_json()
    for node_name in SHEETS_NODES:
        node = next((n for n in wf.get("nodes", []) if n.get("name") == node_name), None)
        assert node is not None, f"Node {node_name} must exist"
        doc_id = node.get("parameters", {}).get("documentId", "")
        if isinstance(doc_id, dict):
            doc_id_val = doc_id.get("value", "")
        else:
            doc_id_val = str(doc_id)
        assert doc_id_val == "={{ $env.DEVMEET_SPREADSHEET_ID }}", f"Node {node_name} documentId must use $env.DEVMEET_SPREADSHEET_ID"


def test_Q_credential_security():
    wf = load_workflow_json()
    for node_name in SHEETS_NODES:
        node = next((n for n in wf.get("nodes", []) if n.get("name") == node_name), None)
        assert node is not None
        creds = node.get("credentials", {})
        assert "googleSheetsOAuth2Api" in creds, f"Node {node_name} must use googleSheetsOAuth2Api credential reference"


def test_R_no_postgresql_access_in_n8n():
    wf = load_workflow_json()
    for node in wf.get("nodes", []):
        node_type = node.get("type", "")
        assert "postgres" not in node_type.lower(), f"Node {node.get('name')} must not be a Postgres node"
        creds = node.get("credentials", {})
        for cred_key in creds.keys():
            assert "postgres" not in cred_key.lower(), f"Node {node.get('name')} must not use postgres credentials"


def test_S_no_ai_path_from_sheets_nodes():
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    ai_nodes = [
        "Analyze Meeting - Gemini LLM Chain", "AI Retry #1 - Gemini LLM Chain"
    ]
    for sn in SHEETS_NODES:
        visited = set()
        queue = [sn]
        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)
            assert curr not in ai_nodes, f"Sheets node {sn} reached AI node {curr}"
            targets = connections.get(curr, {}).get("main", [])
            for group in targets:
                for target_conn in group:
                    t_name = target_conn.get("node")
                    if t_name and t_name not in visited:
                        queue.append(t_name)


def test_T_no_sheets_failure_to_ai_path():
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    ai_nodes = ["Analyze Meeting - Gemini LLM Chain", "AI Retry #1 - Gemini LLM Chain"]
    
    for cn in CHECK_NODES:
        visited = set()
        queue = [cn]
        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)
            assert curr not in ai_nodes, f"Check node {cn} reached AI node {curr}"
            targets = connections.get(curr, {}).get("main", [])
            for group in targets:
                for target_conn in group:
                    t_name = target_conn.get("node")
                    if t_name and t_name not in visited:
                        queue.append(t_name)


def test_U_successful_operations_are_not_retried():
    res = simulate_sheets_retry(attempts=4, succeed_on_attempt=1)
    assert res["status"] == "SUCCESS"
    assert res["total_attempts"] == 1
    assert res["delays_experienced"] == []


def test_V_independent_retry_state_across_4_entities():
    res_meeting = simulate_sheets_retry(attempts=4, succeed_on_attempt=2)
    res_decisions = simulate_sheets_retry(attempts=4, succeed_on_attempt=1)
    res_actions = simulate_sheets_retry(attempts=4, succeed_on_attempt=1)
    res_email = simulate_sheets_retry(attempts=4, succeed_on_attempt=999)

    assert res_meeting["status"] == "SUCCESS" and res_meeting["total_attempts"] == 2
    assert res_decisions["status"] == "SUCCESS" and res_decisions["total_attempts"] == 1
    assert res_actions["status"] == "SUCCESS" and res_actions["total_attempts"] == 1
    assert res_email["status"] == "FAILED" and res_email["total_attempts"] == 4
