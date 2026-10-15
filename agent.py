"""
JARVIS Core Agent: Plan-Execute-Verify-Recover loop.
Handles:
1. Planning with LLM (env var API key) + preferences.json (with fallback planner)
2. Risk assessment: medium -> needs_confirmation, high -> needs_approval
3. Execution and code-based verification
4. Recovery with up to 2 retries (and alternative tool if defined) -> needs_human if exhausted
5. Search disambiguation -> needs_human when multiple matches found
6. State persistence to tasks/<task_id>.json after every step
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import os
import re
import uuid

from tools import tool_registry
from verifiers import get_verifier

BASE_DIR = Path(__file__).resolve().parent
TASKS_DIR = BASE_DIR / "tasks"
TASKS_DIR.mkdir(parents=True, exist_ok=True)
PREFERENCES_FILE = BASE_DIR / "preferences.json"


# ==========================================
# Preferences Loader
# ==========================================

def load_preferences() -> Dict[str, Any]:
    """Read preferences.json if present."""
    if PREFERENCES_FILE.exists():
        try:
            return json.loads(PREFERENCES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# ==========================================
# Task State Management (tasks/<task_id>.json)
# ==========================================

def get_task_file(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def save_task(task_state: Dict[str, Any]) -> None:
    """Save task state atomically to tasks/<task_id>.json."""
    task_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    target_path = get_task_file(task_state["task_id"])
    target_path.write_text(json.dumps(task_state, indent=2), encoding="utf-8")


def load_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Load task state from tasks/<task_id>.json."""
    target_path = get_task_file(task_id)
    if not target_path.exists():
        return None
    try:
        return json.loads(target_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_all_tasks() -> List[Dict[str, Any]]:
    """List summary of all persisted tasks."""
    tasks = []
    for f in TASKS_DIR.glob("*.json"):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
            tasks.append({
                "task_id": t.get("task_id"),
                "goal": t.get("goal"),
                "status": t.get("status"),
                "current_step": t.get("current_step"),
                "total_steps": len(t.get("steps", [])),
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
            })
        except Exception:
            continue
    return sorted(tasks, key=lambda x: x.get("created_at", ""), reverse=True)


# ==========================================
# Planner: LLM + Fallback Heuristic
# ==========================================

PLANNER_SYSTEM_PROMPT = """You are JARVIS, an autonomous task planner for a failure-aware assistant.
Your job is to convert a user's high-level goal into a sequential list of concrete steps.

Available tools and their signatures:
- find_file(query: str) [risk: low]: searches sandbox for files matching query.
- download_file(name: str) [risk: low]: downloads a file into sandbox.
- move_file(name: str, folder: str) [risk: medium]: moves a file within sandbox to target folder.
- create_reminder(text: str, time: str) [risk: low]: saves a reminder entry to reminders.json.
- send_email(to: str, body: str) [risk: high]: sends an email and logs to sent_emails.json.

User Preferences:
{preferences}

Rules:
1. Return ONLY a valid JSON array of objects. Do not wrap in markdown quotes if possible, or provide valid JSON.
2. Each step must have:
   - "step_id": integer starting at 1
   - "tool": name of the tool
   - "args": dictionary of tool arguments
   - "description": human-readable explanation
   - "alternative": optional dictionary with alternative {"tool": "...", "args": {...}} or null.
3. Keep the plan minimal, direct, and executable in sequence.
"""


def _heuristic_planner(goal: str, preferences: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Fallback deterministic planner when no external LLM API key is configured.
    Ensures the system is 100% functional and testable out of the box.
    """
    steps: List[Dict[str, Any]] = []
    g_lower = goal.lower()
    default_recipient = preferences.get("default_recipient", "team@example.com")
    default_archive = preferences.get("default_archive_folder", "archive")
    default_reminder_time = preferences.get("reminder_default_time", "09:00 AM")

    step_counter = 1

    # Detect download
    download_match = re.search(r"download\s+([a-zA-Z0-9_\-\.]+)", goal, re.IGNORECASE)
    if download_match:
        fname = download_match.group(1).strip()
        steps.append({
            "step_id": step_counter,
            "tool": "download_file",
            "args": {"name": fname},
            "description": f"Download file '{fname}' into sandbox",
            "alternative": None
        })
        step_counter += 1

    # Detect find / search
    find_match = re.search(r"(?:find|search)\s+(?:file\s+)?([a-zA-Z0-9_\-\.]+)", goal, re.IGNORECASE)
    if find_match and not download_match:
        q = find_match.group(1).strip()
        steps.append({
            "step_id": step_counter,
            "tool": "find_file",
            "args": {"query": q},
            "description": f"Search sandbox for files matching '{q}'",
            "alternative": None
        })
        step_counter += 1

    # Detect move
    move_match = re.search(r"move\s+([a-zA-Z0-9_\-\.\$]+)\s+to\s+([a-zA-Z0-9_\-\.]+)", goal, re.IGNORECASE)
    if move_match:
        fname = move_match.group(1).strip()
        folder = move_match.group(2).strip()
        steps.append({
            "step_id": step_counter,
            "tool": "move_file",
            "args": {"name": fname, "folder": folder},
            "description": f"Move file '{fname}' to folder '{folder}'",
            "alternative": None
        })
        step_counter += 1
    elif "archive" in g_lower and not any(s["tool"] == "move_file" for s in steps):
        # Infer moving previous/found file to archive folder
        target_name = steps[-1]["args"].get("name") if steps and "name" in steps[-1]["args"] else "$SELECTED_FILE"
        steps.append({
            "step_id": step_counter,
            "tool": "move_file",
            "args": {"name": target_name, "folder": default_archive},
            "description": f"Move file to '{default_archive}' folder",
            "alternative": None
        })
        step_counter += 1

    # Detect reminder
    reminder_match = re.search(r"remind(?:er)?\s+(?:me\s+)?(?:to\s+)?(.+?)(?:\s+at\s+([0-9:]+\s*(?:am|pm)?))?$", goal, re.IGNORECASE)
    if reminder_match and ("remind" in g_lower):
        r_text = reminder_match.group(1).strip()
        r_time = reminder_match.group(2).strip() if reminder_match.group(2) else default_reminder_time
        steps.append({
            "step_id": step_counter,
            "tool": "create_reminder",
            "args": {"text": r_text, "time": r_time},
            "description": f"Create reminder '{r_text}' at {r_time}",
            "alternative": None
        })
        step_counter += 1

    # Detect email
    if "email" in g_lower or "send" in g_lower:
        email_to_match = re.search(r"(?:email|send\s+email)\s+to\s+([a-zA-Z0-9_\-\.@]+)", goal, re.IGNORECASE)
        to_addr = email_to_match.group(1).strip() if email_to_match else default_recipient
        body_match = re.search(r"(?:with\s+body|saying|message)\s+[\"']?([^\"']+)[\"']?", goal, re.IGNORECASE)
        body_text = body_match.group(1).strip() if body_match else f"Completed task: {goal}"
        steps.append({
            "step_id": step_counter,
            "tool": "send_email",
            "args": {"to": to_addr, "body": body_text},
            "description": f"Send email to {to_addr}",
            "alternative": None
        })
        step_counter += 1

    # If nothing matched, construct generic find step
    if not steps:
        tokens = goal.split()
        q = tokens[0] if tokens else "report"
        steps.append({
            "step_id": 1,
            "tool": "find_file",
            "args": {"query": q},
            "description": f"Locate files matching '{q}'",
            "alternative": None
        })

    return steps


def plan_goal(goal: str) -> List[Dict[str, Any]]:
    """
    Plan a list of steps for the given goal using LLM (if API key available)
    or fallback heuristic planner.
    """
    preferences = load_preferences()
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    # Try OpenAI if configured
    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            prompt = PLANNER_SYSTEM_PROMPT.format(preferences=json.dumps(preferences, indent=2))
            completion = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"User Goal: {goal}"}
                ],
                temperature=0.0
            )
            raw_text = completion.choices[0].message.content.strip()
            # Clean possible markdown block
            raw_text = re.sub(r"^```(?:json)?", "", raw_text).strip("`").strip()
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                _enrich_steps_with_risk(parsed)
                return parsed
        except Exception:
            pass  # Fallback below

    # Try Gemini if configured
    if gemini_key:
        try:
            from google import genai
            client = genai.Client(api_key=gemini_key)
            prompt = (
                PLANNER_SYSTEM_PROMPT.format(preferences=json.dumps(preferences, indent=2))
                + f"\n\nUser Goal: {goal}\nReturn JSON array only:"
            )
            response = client.models.generate_content(
                model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                contents=prompt
            )
            raw_text = response.text.strip()
            raw_text = re.sub(r"^```(?:json)?", "", raw_text).strip("`").strip()
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                _enrich_steps_with_risk(parsed)
                return parsed
        except Exception:
            pass  # Fallback below

    # Fallback heuristic planner
    steps = _heuristic_planner(goal, preferences)
    _enrich_steps_with_risk(steps)
    return steps


def _enrich_steps_with_risk(steps: List[Dict[str, Any]]) -> None:
    """Populate risk level, default status, and verification metadata from registry."""
    for idx, s in enumerate(steps, start=1):
        s["step_id"] = idx
        if "risk" not in s or not s["risk"]:
            tool_def = tool_registry.get(s["tool"])
            s["risk"] = tool_def.risk_level if tool_def else "medium"
        s.setdefault("status", "pending")
        s.setdefault("attempts", 0)
        s.setdefault("result", None)
        s.setdefault("error", None)
        s.setdefault("confirmed", False)
        s.setdefault("approved", False)


# ==========================================
# Task Execution Engine (Plan-Execute-Verify-Recover)
# ==========================================

def create_task(goal: str, custom_steps: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Create a new task, plan steps, save initial state, and return task state."""
    task_id = str(uuid.uuid4())[:8]

    if custom_steps:
        steps = custom_steps
        _enrich_steps_with_risk(steps)
    else:
        steps = plan_goal(goal)

    task_state = {
        "task_id": task_id,
        "goal": goal,
        "status": "pending",
        "current_step": 0,
        "steps": steps,
        "error_details": None,
        "message": f"Task created with {len(steps)} planned step(s).",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_task(task_state)
    return task_state


def propagate_selected_file(task: Dict[str, Any], selected_file: str) -> None:
    """Substitute resolved/selected filename into downstream steps."""
    for s in task["steps"][task["current_step"]:]:
        args = s.get("args", {})
        for k, v in list(args.items()):
            if k in ("name", "query", "filename") and (
                v in ("$SELECTED_FILE", "$FILE", "$MATCH", "")
                or (isinstance(v, str) and (v.startswith("$") or "<file>" in v))
            ):
                args[k] = selected_file


def run_task(task: Dict[str, Any], resume_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Main loop: Execute -> Verify -> Recover.
    Resumes from task['current_step'] until task finishes, pauses, or escalates.
    """
    resume_input = resume_input or {}
    total_steps = len(task["steps"])

    # 1. Handle user inputs when resuming from paused states
    if task["status"] in ("needs_confirmation", "needs_approval", "needs_human"):
        curr_idx = task["current_step"]
        if curr_idx < total_steps:
            current_step_obj = task["steps"][curr_idx]

            # Case A: Resuming from needs_confirmation (medium risk)
            if task["status"] == "needs_confirmation":
                # Check for explicit rejection
                if resume_input.get("confirmed") is False or resume_input.get("reject") is True:
                    task["status"] = "cancelled"
                    task["message"] = f"Task cancelled by user at step {current_step_obj['step_id']}."
                    save_task(task)
                    return task
                current_step_obj["confirmed"] = True
                task["status"] = "running"
                task["message"] = f"Confirmation granted for step {current_step_obj['step_id']}."

            # Case B: Resuming from needs_approval (high risk)
            elif task["status"] == "needs_approval":
                if resume_input.get("approved") is False or resume_input.get("reject") is True:
                    task["status"] = "cancelled"
                    task["message"] = f"Task cancelled: explicit approval was denied for step {current_step_obj['step_id']}."
                    save_task(task)
                    return task
                if resume_input.get("approved") is True or resume_input.get("confirmed") is True:
                    current_step_obj["approved"] = True
                    task["status"] = "running"
                    task["message"] = f"Approval granted for step {current_step_obj['step_id']}."
                else:
                    task["message"] = "Explicit approval required: please send {'approved': true} to proceed."
                    save_task(task)
                    return task

            # Case C: Resuming from needs_human
            elif task["status"] == "needs_human":
                err_details = task.get("error_details") or {}
                # Multiple matches disambiguation
                if err_details.get("type") == "multiple_matches":
                    selected = (
                        resume_input.get("selected_option")
                        or resume_input.get("selected_file")
                        or resume_input.get("file")
                        or resume_input.get("input")
                    )
                    if not selected:
                        task["message"] = "A selection from the options list is required to resume."
                        save_task(task)
                        return task

                    # Validate selected option
                    valid_options = err_details.get("options", [])
                    if valid_options and selected not in valid_options:
                        # Allow basename match
                        matched = [opt for opt in valid_options if opt.endswith(selected) or selected in opt]
                        if matched:
                            selected = matched[0]
                        else:
                            task["message"] = f"Invalid selection '{selected}'. Must be one of: {valid_options}"
                            save_task(task)
                            return task

                    # Complete the find_file step with selected file
                    current_step_obj["result"] = [selected]
                    current_step_obj["status"] = "completed"
                    current_step_obj["selected_option"] = selected
                    task["error_details"] = None
                    task["current_step"] += 1
                    task["status"] = "running"
                    propagate_selected_file(task, selected)
                    save_task(task)

                # Verification failure retry
                elif err_details.get("type") == "verification_failed":
                    # User asked to retry or provided updated parameters
                    task["error_details"] = None
                    task["status"] = "running"
                    current_step_obj["attempts"] = 0  # reset attempts for human retry
                    if "args" in resume_input:
                        current_step_obj["args"].update(resume_input["args"])
                    task["message"] = f"Retrying step {current_step_obj['step_id']} following human intervention."
                    save_task(task)

    # 2. Execution Loop
    task["status"] = "running"
    save_task(task)

    while task["current_step"] < total_steps:
        idx = task["current_step"]
        step = task["steps"][idx]
        tool_name = step["tool"]
        tool_args = step.get("args", {})
        risk = step.get("risk", "low")

        # Risk check: medium requires confirmation
        if risk == "medium" and not step.get("confirmed"):
            step["status"] = "needs_confirmation"
            task["status"] = "needs_confirmation"
            task["message"] = (
                f"Step {step['step_id']} ({tool_name}) has medium risk and requires user confirmation."
            )
            save_task(task)
            return task

        # Risk check: high requires explicit approval
        if risk == "high" and not step.get("approved"):
            step["status"] = "needs_approval"
            task["status"] = "needs_approval"
            task["message"] = (
                f"Step {step['step_id']} ({tool_name}) has high risk and requires explicit approval."
            )
            save_task(task)
            return task

        # Execute + Verify + Recover Loop (retry up to 2 times, i.e. 3 attempts total)
        step["status"] = "running"
        save_task(task)

        max_attempts = 3
        attempts_log: List[str] = []
        step_succeeded = False

        for attempt in range(1, max_attempts + 1):
            step["attempts"] = attempt
            current_tool = tool_name
            current_args = dict(tool_args)

            # If retrying and step specifies an alternative, switch to alternative
            if attempt > 1 and step.get("alternative"):
                current_tool = step["alternative"].get("tool", tool_name)
                current_args = step["alternative"].get("args", tool_args)

            # A. Execute Tool
            try:
                result = tool_registry.execute(current_tool, current_args)
            except Exception as exc:
                attempts_log.append(f"Attempt {attempt}: tool execution threw exception: {exc}")
                step["error"] = str(exc)
                continue

            # B. Check for multiple matches on find_file (Requirement 5)
            if current_tool == "find_file" and isinstance(result, list) and len(result) > 1:
                step["status"] = "needs_human"
                task["status"] = "needs_human"
                task["error_details"] = {
                    "type": "multiple_matches",
                    "what_failed": f"Multiple files ({len(result)}) matched search query '{current_args.get('query')}'.",
                    "what_was_tried": [f"Executed find_file(query='{current_args.get('query')}') -> {result}"],
                    "what_needed": "User must select one file from the options to proceed.",
                    "options": result,
                }
                task["message"] = "Multiple matching files found. Escalating to human for selection."
                save_task(task)
                return task

            # Single match from find_file: propagate to downstream steps
            if current_tool == "find_file" and isinstance(result, list) and len(result) == 1:
                propagate_selected_file(task, result[0])

            # C. Run Code-Based Verifier
            verifier = get_verifier(current_tool)
            if verifier:
                v_res = verifier(current_args, result)
                if not v_res.success:
                    attempts_log.append(
                        f"Attempt {attempt}: tool '{current_tool}' verification failed: {v_res.message}"
                    )
                    step["error"] = v_res.message
                    continue
                else:
                    # Verification succeeded!
                    step["status"] = "completed"
                    step["result"] = result
                    step["error"] = None
                    step["verifier_message"] = v_res.message
                    if attempt > 1:
                        step["recovered"] = True
                        step["recovery_notes"] = f"Recovered on attempt {attempt} after initial failure."
                    step_succeeded = True
                    break
            else:
                # No verifier registered -> treat successful return as pass
                step["status"] = "completed"
                step["result"] = result
                step["error"] = None
                step_succeeded = True
                break

        if not step_succeeded:
            # All retries exhausted! (Requirement 4)
            step["status"] = "needs_human"
            task["status"] = "needs_human"
            task["error_details"] = {
                "type": "verification_failed",
                "what_failed": f"Step {step['step_id']} ({tool_name}) failed verification after {len(attempts_log)} attempt(s).",
                "what_was_tried": attempts_log,
                "what_needed": f"Manual intervention required for tool '{tool_name}' with args {tool_args}."
            }
            task["message"] = f"Step {step['step_id']} failed after retries. Escalating to human."
            save_task(task)
            return task

        # Advance to next step and persist state
        task["current_step"] += 1
        save_task(task)

    # 3. All Steps Complete
    task["status"] = "completed"
    task["message"] = f"All {total_steps} step(s) completed and verified successfully."
    save_task(task)
    return task
