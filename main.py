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
import shutil

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
)
from tools import tool_registry, set_fail_once, get_fail_once, SANDBOX_DIR

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
    confirmed: Optional[bool] = Field(None, description="Confirm medium risk action")
    approved: Optional[bool] = Field(None, description="Approve high risk action")
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

    task = create_task(goal=payload.goal.strip(), custom_steps=payload.custom_steps)
    executed_task = run_task(task)

    return {
        "task_id": executed_task["task_id"],
        "status": executed_task["status"],
        "current_step": executed_task["current_step"],
        "total_steps": len(executed_task["steps"]),
        "message": executed_task.get("message"),
        "state": executed_task,
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
    updated_task = run_task(task, resume_input=resume_data)

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

    # 2. Reset sandbox contents
    if SANDBOX_DIR.exists():
        shutil.rmtree(SANDBOX_DIR)
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

    # 3. Seed files
    (SANDBOX_DIR / "financial_report_q1.pdf").write_text(
        "Q1 Financial Summary\nRevenue: $1.2M\nExpenses: $850k\nNet: $350k\nStatus: Approved\n",
        encoding="utf-8"
    )
    (SANDBOX_DIR / "financial_report_q2.pdf").write_text(
        "Q2 Financial Summary\nRevenue: $1.5M\nExpenses: $920k\nNet: $580k\nStatus: Draft\n",
        encoding="utf-8"
    )
    (SANDBOX_DIR / "meeting_notes.txt").write_text(
        "Team sync notes:\n- Reviewed project roadmap\n- Scheduled deployment window for Friday 18:00\n",
        encoding="utf-8"
    )

    return {
        "status": "success",
        "message": "Sandbox reset with 3 seed files. FAIL_ONCE set to True.",
        "files": [f.name for f in SANDBOX_DIR.iterdir()],
        "fail_once": get_fail_once()
    }
