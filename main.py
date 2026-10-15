"""
JARVIS FastAPI Backend
Failure-aware agent with Plan-Execute-Verify-Recover loop.

Endpoints:
- POST /tasks: create and run task for a given goal
- GET /tasks/{id}: fetch full task state (poll-friendly)
- POST /tasks/{id}/resume: resume paused task with optional input/confirmation
- GET /tasks: list all tasks
- GET /tools: inspect registered tools and risk levels
- POST /sandbox/reset: reset sandbox files and FAIL_ONCE flag
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
import os
import threading

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)


def _clean_api_key(value: Optional[str]) -> str:
    value = (value or "").strip().lstrip("\ufeff").rstrip("\r\n").strip()
    while value and value[0] in "\ufeff\"'":
        value = value[1:].lstrip()
    while value and value[-1] in "\ufeff\"'\r\n":
        value = value[:-1].rstrip()
    return value.strip()


GEMINI_API_KEY = _clean_api_key(os.getenv("GEMINI_API_KEY"))
OPENAI_API_KEY = _clean_api_key(os.getenv("OPENAI_API_KEY"))
os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
print(f"Gemini key loaded: {bool(GEMINI_API_KEY)}, length={len(GEMINI_API_KEY)}, starts with {GEMINI_API_KEY[:4] if GEMINI_API_KEY else 'none'}")
GEMINI_STARTUP_CLIENT = None


def _safe_startup_error(error: Exception) -> str:
    message = str(error).replace("\r", " ").replace("\n", " ")
    for secret in (GEMINI_API_KEY, OPENAI_API_KEY):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:240] or error.__class__.__name__


if GEMINI_API_KEY:
    try:
        from google import genai
        GEMINI_STARTUP_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
        GEMINI_STARTUP_CLIENT.models.generate_content(
            model=GEMINI_MODEL,
            contents="Reply with OK",
        )
        GEMINI_HEALTH = "ok"
        print("Gemini test: OK")
    except Exception as exc:
        GEMINI_HEALTH = _safe_startup_error(exc)
        print(f"Gemini test: {GEMINI_HEALTH}")
else:
    GEMINI_HEALTH = "GEMINI_API_KEY is not configured"
    print(f"Gemini test: {GEMINI_HEALTH}")

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent import (
    create_task,
    load_task,
    list_all_tasks,
    run_task,
    save_task,
    load_preferences,
    TASKS_DIR,
    plan_goal,
    _enrich_steps_with_risk,
    _applied_preferences,
    record_event,
    _safe_error_message,
)
from tools import tool_registry, set_fail_once, get_fail_once, SANDBOX_DIR

_running_tasks = set()
_running_tasks_lock = threading.Lock()

def _run_task_in_background(task_id: str, resume_input=None):
    with _running_tasks_lock:
        if task_id in _running_tasks:
            return False
        _running_tasks.add(task_id)
    def worker():
        try:
            task = load_task(task_id)
            if task:
                if task.get("planner") is None:
                    planner_info = {}
                    task["steps"] = plan_goal(task["goal"], planner_info)
                    _enrich_steps_with_risk(task["steps"])
                    task["planner"] = planner_info.get("planner", "heuristic")
                    task["planner_error"] = planner_info.get("planner_error")
                    task["preferences_used"] = _applied_preferences(task["steps"], load_preferences())
                    task["message"] = f"Task created with {len(task['steps'])} planned step(s)."
                    record_event(task, "plan", f"Plan created with {len(task['steps'])} step(s) using the {task['planner']} planner.")
                    save_task(task)
                run_task(task, resume_input=resume_input)
        except Exception as exc:
            print(f"Background task {task_id} failed: {_safe_error_message(exc)}")
        finally:
            with _running_tasks_lock:
                _running_tasks.discard(task_id)
    threading.Thread(target=worker, name=f"jarvis-{task_id}", daemon=True).start()
    return True

app = FastAPI(
    title="JARVIS Agent Backend",
    description="Failure-aware agent with Plan-Execute-Verify-Recover loop and human-in-the-loop escalation.",
    version="1.0.0"
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"gemini": GEMINI_HEALTH}


# ==========================================
# Pydantic Schemas
# ==========================================

class CreateTaskRequest(BaseModel):
    goal: str = Field(..., examples=["Find financial report and archive it"])
    custom_steps: Optional[List[Dict[str, Any]]] = Field(
        None,
        description="Optional pre-defined step list to bypass planner for testing"
    )


class ResumeTaskRequest(BaseModel):
    confirm: Optional[bool] = Field(None, description="Confirm or reject a paused action")
    confirmed: Optional[bool] = Field(None, description="Confirm medium risk action")
    approved: Optional[bool] = Field(None, description="Approve high risk action")
    selected_file: Optional[str] = Field(None, description="File selected from search suggestions")
    selected_option: Optional[str] = Field(None, description="Option selected for disambiguation")
    input: Optional[str] = Field(None, description="Generic input or option")
    args: Optional[Dict[str, Any]] = Field(None, description="Optional updated args for retry")


# ==========================================
# Endpoints
# ==========================================

@app.get("/")
def read_root():
    """Service status and endpoint index."""
    return {
        "service": "JARVIS Agent Backend",
        "status": "online",
        "loop": "Plan -> Execute -> Verify -> Recover",
        "fail_once_flag": get_fail_once(),
        "endpoints": {
            "POST /tasks": "Create a new task from goal",
            "GET /tasks/{id}": "Poll task state and history",
            "POST /tasks/{id}/resume": "Resume paused task with human input",
            "GET /tasks": "List all tasks",
            "GET /tools": "List registered tools and risk levels",
            "POST /sandbox/reset": "Reset sandbox files and fail_once flag",
        }
    }


@app.post("/tasks", status_code=status.HTTP_201_CREATED)
def create_new_task(payload: CreateTaskRequest):
    """
    Create a new task from goal.
    Plans steps via LLM/Heuristic planner and begins execution loop.
    Returns task_id and state (which may be completed or paused for confirmation/approval/human).
    """
    if not payload.goal.strip():
        raise HTTPException(status_code=400, detail="Goal cannot be empty.")

    task = create_task(goal=payload.goal.strip(), custom_steps=payload.custom_steps, defer_planning=not payload.custom_steps)
    _run_task_in_background(task["task_id"])
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "current_step": task["current_step"],
        "total_steps": len(task["steps"]),
        "message": task.get("message"),
        "state": task,
    }


@app.get("/tasks/{task_id}")
def get_task_state(task_id: str):
    """
    Poll-friendly endpoint returning full state of a task from tasks/<task_id>.json.
    """
    task = load_task(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with id '{task_id}' was not found."
        )
    return task


@app.post("/tasks/{task_id}/resume")
def resume_task_endpoint(task_id: str, payload: Optional[ResumeTaskRequest] = None):
    """
    Resume execution of a paused task.
    Accepts confirmation (medium risk), explicit approval (high risk),
    or option selection (disambiguation for multiple search matches).
    """
    task = load_task(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with id '{task_id}' was not found."
        )

    if task["status"] in ("completed", "cancelled"):
        return {
            "task_id": task_id,
            "status": task["status"],
            "message": f"Task is already {task['status']}. No action taken.",
            "state": task,
        }

    resume_data = payload.model_dump(exclude_unset=True) if payload else {}
    _run_task_in_background(task_id, resume_input=resume_data)
    updated_task = task

    return {
        "task_id": updated_task["task_id"],
        "status": updated_task["status"],
        "current_step": updated_task["current_step"],
        "total_steps": len(updated_task["steps"]),
        "message": updated_task.get("message"),
        "state": updated_task,
    }


@app.get("/tasks")
def list_tasks():
    """List all persisted tasks."""
    return {
        "count": len(list_all_tasks()),
        "tasks": list_all_tasks()
    }


@app.get("/tools")
def list_tools():
    """List all registered tools, their risk levels, verifiers, and schemas."""
    return {
        "count": len(tool_registry.list_tools()),
        "tools": tool_registry.list_tools()
    }


@app.post("/sandbox/reset")
def reset_sandbox():
    """
    Utility endpoint: Resets the sandbox folder with seed files and resets FAIL_ONCE = True.
    Useful for repeatability and testing demo scenarios.
    """
    # 1. Reset FAIL_ONCE
    set_fail_once(True)

    # 2. Ensure demo files exist without removing user files.
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

    def ensure_seed(relative_path: str, content: str) -> None:
        target = SANDBOX_DIR / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(content, encoding="utf-8")

    ensure_seed("financial_report_q1.pdf", "Q1 Financial Summary\nRevenue: $1.2M\nExpenses: $850k\nNet: $350k\nStatus: Approved\n")
    ensure_seed("financial_report_q2.pdf", "Q2 Financial Summary\nRevenue: $1.5M\nExpenses: $920k\nNet: $580k\nStatus: Draft\n")
    ensure_seed("meeting_notes.txt", "Team sync notes:\n- Reviewed project roadmap\n- Scheduled deployment window for Friday 18:00\n")
    ensure_seed("Agenda.txt", "JARVIS Demo Agenda\n- Review COSMOS mission files\n- Check database assignment files\n- Summarize next steps\n")
    ensure_seed("COSMOS/COSMOS_Overview.txt", "COSMOS Mission Overview\nA survey of nearby exoplanets and observation targets.\n")
    ensure_seed("COSMOS/Exoplanet_Catalog.txt", "Kepler-452b — candidate Earth-like planet\nTRAPPIST-1e — rocky planet in the habitable zone\n")
    ensure_seed("COSMOS/Mission_Log.txt", "Mission log: telescope calibration complete.\nNext observation window: 2026-10-20.\n")
    ensure_seed("DBMS_Assignment_1.pdf", "Database Management Systems — Assignment 1\nTopics: relational algebra and normalization.\n")
    ensure_seed("DBMS_Assignment_1_final.pdf", "Database Management Systems — Assignment 1 Final\nSubmission copy.\n")

    return {
        "status": "success",
        "message": "Sandbox sample files verified. Existing files were preserved. FAIL_ONCE set to True.",
        "files": [f.relative_to(SANDBOX_DIR).as_posix() for f in SANDBOX_DIR.rglob("*") if f.is_file()],
        "fail_once": get_fail_once()
    }
