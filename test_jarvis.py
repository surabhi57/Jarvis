"""
Comprehensive test suite for JARVIS failure-aware agent.
Tests:
- Plan-Execute-Verify-Recover loop
- FAIL_ONCE automatic recovery for download_file
- Medium risk pausing with needs_confirmation
- High risk pausing with needs_approval
- Multiple file search escalation with needs_human and options
- Persistence to tasks/<task_id>.json
- Resume endpoint
- Modular tool registry
"""

import json
from pathlib import Path
import shutil
import pytest
from fastapi.testclient import TestClient

from main import app
from tools import set_fail_once, get_fail_once, SANDBOX_DIR, tool_registry
from agent import TASKS_DIR, load_task

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_environment():
    """Ensure clean sandbox and tasks directory before each test."""
    set_fail_once(True)

    # Clean sandbox
    if SANDBOX_DIR.exists():
        shutil.rmtree(SANDBOX_DIR)
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

    # Re-seed files
    (SANDBOX_DIR / "financial_report_q1.pdf").write_text("Q1 content", encoding="utf-8")
    (SANDBOX_DIR / "financial_report_q2.pdf").write_text("Q2 content", encoding="utf-8")
    (SANDBOX_DIR / "meeting_notes.txt").write_text("Meeting notes", encoding="utf-8")

    yield


def test_root_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert "Plan -> Execute -> Verify -> Recover" in data["loop"]


def test_download_fail_once_auto_recovery():
    """
    Test FAIL_ONCE: download_file writes nothing initially, verifier fails,
    agent retries, second attempt writes the file and verifier passes.
    """
    set_fail_once(True)
    target_file = "downloaded_test_file.pdf"
    file_path = SANDBOX_DIR / target_file
    if file_path.exists():
        file_path.unlink()

    payload = {
        "goal": f"download {target_file}",
        "custom_steps": [
            {
                "tool": "download_file",
                "args": {"name": target_file},
                "risk": "low"
            }
        ]
    }
    response = client.post("/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    task_id = data["task_id"]

    # Verify task completed
    assert data["status"] == "completed"

    # Verify state from GET endpoint
    get_res = client.get(f"/tasks/{task_id}")
    assert get_res.status_code == 200
    task_state = get_res.json()

    step_info = task_state["steps"][0]
    assert step_info["status"] == "completed"
    # Should have taken 2 attempts (first failed verification due to FAIL_ONCE, second recovered!)
    assert step_info["attempts"] == 2
    assert step_info.get("recovered") is True

    # Verify physical file exists and is not empty
    assert file_path.exists()
    assert file_path.stat().st_size > 0


def test_medium_risk_pause_and_resume():
    """
    Test that move_file (medium risk) pauses with status 'needs_confirmation'
    and resumes only after user provides confirmation.
    """
    payload = {
        "goal": "move meeting_notes.txt to archive",
        "custom_steps": [
            {
                "tool": "move_file",
                "args": {"name": "meeting_notes.txt", "folder": "archive"},
                "risk": "medium"
            }
        ]
    }
    response = client.post("/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    task_id = data["task_id"]

    # Should pause with needs_confirmation
    assert data["status"] == "needs_confirmation"
    assert data["current_step"] == 0

    # Verify file is not moved yet
    assert (SANDBOX_DIR / "meeting_notes.txt").exists()
    assert not (SANDBOX_DIR / "archive" / "meeting_notes.txt").exists()

    # Now resume with confirmed=True
    resume_res = client.post(f"/tasks/{task_id}/resume", json={"confirmed": True})
    assert resume_res.status_code == 200
    res_data = resume_res.json()
    assert res_data["status"] == "completed"

    # Verify file has moved
    assert not (SANDBOX_DIR / "meeting_notes.txt").exists()
    assert (SANDBOX_DIR / "archive" / "meeting_notes.txt").exists()


def test_high_risk_pause_and_explicit_approval():
    """
    Test that send_email (high risk) pauses with status 'needs_approval'
    and requires explicit approval to resume.
    """
    payload = {
        "goal": "send email to lead@example.com with body deployment ready",
        "custom_steps": [
            {
                "tool": "send_email",
                "args": {"to": "lead@example.com", "body": "deployment ready"},
                "risk": "high"
            }
        ]
    }
    response = client.post("/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    task_id = data["task_id"]

    # Should pause with needs_approval
    assert data["status"] == "needs_approval"

    # If resumed without approval
    res_unapproved = client.post(f"/tasks/{task_id}/resume", json={})
    assert res_unapproved.json()["status"] == "needs_approval"

    # If resumed with explicit approval
    res_approved = client.post(f"/tasks/{task_id}/resume", json={"approved": True})
    assert res_approved.status_code == 200
    assert res_approved.json()["status"] == "completed"

    # Verify sent_emails.json
    sent_emails_file = SANDBOX_DIR / "sent_emails.json"
    assert sent_emails_file.exists()
    emails = json.loads(sent_emails_file.read_text(encoding="utf-8"))
    assert any(e["to"] == "lead@example.com" and "deployment ready" in e["body"] for e in emails)


def test_multiple_search_matches_escalates_to_human():
    """
    Test requirement 5: If multiple files match a search, escalate with 'needs_human' and options.
    """
    payload = {
        "goal": "find financial_report",
        "custom_steps": [
            {
                "tool": "find_file",
                "args": {"query": "financial_report"},
                "risk": "low"
            }
        ]
    }
    response = client.post("/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    task_id = data["task_id"]

    # Must escalate to needs_human
    assert data["status"] == "needs_human"
    task_state = data["state"]
    assert task_state["error_details"]["type"] == "multiple_matches"
    options = task_state["error_details"]["options"]
    assert "financial_report_q1.pdf" in options
    assert "financial_report_q2.pdf" in options

    # Resume with user selection
    resume_res = client.post(f"/tasks/{task_id}/resume", json={"selected_option": "financial_report_q1.pdf"})
    assert resume_res.status_code == 200
    res_data = resume_res.json()
    assert res_data["status"] == "completed"
    assert res_data["state"]["steps"][0]["result"] == ["financial_report_q1.pdf"]


def test_create_reminder_verifier():
    """
    Test create_reminder tool and its verifier reading from reminders.json.
    """
    payload = {
        "goal": "remind me to review budget at 15:00",
        "custom_steps": [
            {
                "tool": "create_reminder",
                "args": {"text": "review budget", "time": "15:00"},
                "risk": "low"
            }
        ]
    }
    response = client.post("/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "completed"

    # Verify reminders.json on disk
    reminders_file = SANDBOX_DIR / "reminders.json"
    assert reminders_file.exists()
    reminders = json.loads(reminders_file.read_text(encoding="utf-8"))
    assert any(r["text"] == "review budget" and r["time"] == "15:00" for r in reminders)


def test_state_persistence_to_tasks_folder():
    """
    Test requirement 6: Save task state after every step to tasks/<task_id>.json.
    """
    payload = {"goal": "remind me to call accountant at 10:00"}
    response = client.post("/tasks", json=payload)
    assert response.status_code == 201
    task_id = response.json()["task_id"]

    task_file = TASKS_DIR / f"{task_id}.json"
    assert task_file.exists()

    disk_data = json.loads(task_file.read_text(encoding="utf-8"))
    assert disk_data["task_id"] == task_id
    assert disk_data["goal"] == payload["goal"]
    assert disk_data["status"] == "completed"


def test_modular_tool_registry():
    """
    Test that adding a new tool = one function + one registry entry.
    """
    @tool_registry.register(
        name="calculate_tax",
        risk_level="low",
        description="Calculate simple sales tax."
    )
    def calculate_tax(amount: float, rate: float = 0.1) -> dict:
        return {"amount": amount, "tax": amount * rate, "total": amount * (1 + rate)}

    tool = tool_registry.get("calculate_tax")
    assert tool is not None
    assert tool.risk_level == "low"

    result = tool_registry.execute("calculate_tax", {"amount": 100.0, "rate": 0.05})
    assert result["tax"] == 5.0
    assert result["total"] == 105.0

    # Verify tool appears in /tools endpoint
    tools_res = client.get("/tools")
    assert tools_res.status_code == 200
    names = [t["name"] for t in tools_res.json()["tools"]]
    assert "calculate_tax" in names


def test_retry_with_alternative_tool():
    """
    Test recovery: if primary tool fails, retry uses alternative tool from step definition.
    """
    set_fail_once(False)
    payload = {
        "goal": "try primary or fallback",
        "custom_steps": [
            {
                "tool": "move_file",
                "args": {"name": "does_not_exist.txt", "folder": "backup"},
                "risk": "low",
                "alternative": {
                    "tool": "download_file",
                    "args": {"name": "recovered_file.pdf"}
                }
            }
        ]
    }
    response = client.post("/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "completed"
    step_info = data["state"]["steps"][0]
    assert step_info["attempts"] == 2
    assert step_info.get("recovered") is True
    assert (SANDBOX_DIR / "recovered_file.pdf").exists()


def test_exhausted_retries_escalates_to_needs_human():
    """
    Test requirement 4: If verify still fails after retries,
    set status 'needs_human' with: what failed, what was tried, what's needed.
    """
    payload = {
        "goal": "move missing file",
        "custom_steps": [
            {
                "tool": "move_file",
                "args": {"name": "ghost_file.txt", "folder": "archive"},
                "risk": "low"  # bypass confirmation to test retry exhaustion
            }
        ]
    }
    response = client.post("/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "needs_human"
    err = data["state"]["error_details"]
    assert err["type"] == "verification_failed"
    assert "what_failed" in err
    assert "what_was_tried" in err
    assert len(err["what_was_tried"]) == 3
    assert "what_needed" in err


