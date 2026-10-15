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
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import os
import re
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

from tools import SANDBOX_DIR, tool_registry
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


def record_event(task: Dict[str, Any], event_type: str, message: str, step_id: Optional[int] = None) -> None:
    events = task.setdefault("events", [])
    if events and events[-1].get("message") == message:
        return
    event = {
        "type": event_type,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if step_id is not None:
        event["step_id"] = step_id
    events.append(event)


def record_failure(task: Dict[str, Any], message: str) -> None:
    failures = task.setdefault("failures_handled", [])
    if message not in failures:
        failures.append(message)


def _template_final_summary(task: Dict[str, Any]) -> str:
    completed = [step for step in task.get("steps", []) if step.get("status") == "completed"]
    actions = ", ".join(step.get("description") or step.get("tool", "a task step") for step in completed)
    actions = actions or "the requested task steps"
    verified = sum(bool(step.get("verifier_passed", step.get("verifier_message"))) for step in completed)
    failures = task.get("failures_handled", [])
    recovery = f"JARVIS handled {len(failures)} issue(s) during the task." if failures else "No failures needed recovery during this task."
    return (
        f"JARVIS completed the goal: {task.get('goal', 'your request')}. "
        f"It carried out {actions}. "
        f"Verification confirmed {verified} step(s). "
        f"{recovery}"
    )


def generate_final_answer(task: Dict[str, Any]) -> str:
    output = []
    for step in task.get("steps", []):
        if step.get("status") != "completed":
            continue
        tool = step.get("tool")
        result = step.get("result")
        if tool == "read_file" and isinstance(result, str):
            output.append(f"File content ({step.get('args', {}).get('name', 'file')}):\n{result}")
        elif tool == "list_files" and isinstance(result, list):
            folder = step.get("args", {}).get("folder", "folder")
            names = "\n".join(f"- {name}" for name in result)
            first = f"\nFirst file name: {result[0]}" if result else ""
            output.append(f"Files in {folder}:\n{names}{first}")
        elif tool == "find_file" and isinstance(result, list) and result:
            first = f"\nFirst file name: {result[0]}"
            output.append("Matching files:\n" + "\n".join(f"- {name}" for name in result) + first)
        elif result is not None:
            output.append(f"{step.get('description', 'Result')}: {result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)}")
    return "\n\n".join(output) or task.get("message", "The task finished without a text result.")


def generate_final_summary(task: Dict[str, Any]) -> str:
    context = json.dumps({
        "goal": task.get("goal"),
        "steps": [{"description": s.get("description"), "status": s.get("status"), "result": s.get("result"), "verified": s.get("verifier_message")} for s in task.get("steps", [])],
        "failures_handled": task.get("failures_handled", []),
    }, ensure_ascii=False)
    prompt = "Write a plain-language summary in exactly 3 or 4 short sentences. State the goal, what was done, what was verified, and how any issue was handled. Do not invent details. Task data: " + context
    try:
        if os.getenv("OPENAI_API_KEY"):
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            response = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            summary = response.choices[0].message.content.strip()
            if 3 <= len(re.findall(r"[^.!?]+[.!?]+", summary)) <= 5:
                return summary
        elif os.getenv("GEMINI_API_KEY"):
            from google import genai
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            response = client.models.generate_content(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), contents=prompt)
            summary = response.text.strip()
            if 3 <= len(re.findall(r"[^.!?]+[.!?]+", summary)) <= 5:
                return summary
    except Exception:
        pass
    return _template_final_summary(task)


def load_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Load task state from tasks/<task_id>.json."""
    target_path = get_task_file(task_id)
    if not target_path.exists():
        return None
    try:
        task = json.loads(target_path.read_text(encoding="utf-8"))
        task.setdefault("events", [])
        task.setdefault("failures_handled", [])
        return task
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

Available registered tools and their signatures:
{tools}

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
3. Split multi-part goals into one step per requested part and preserve the requested order (for example, find a file, then read it, then list files).
4. Keep the plan minimal, direct, and executable in sequence.
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

    list_request = re.search(r"\b(?:find|search)\s+(.+?)\s+and\s+(?:list|see)\s+(?:all\s+)?(?:of\s+)?(?:its|their|the)\s+files\b", goal, re.IGNORECASE)
    first_name_request = re.search(r"\b(?:find|search)\s+(.+?)\s+and\s+(?:the\s+)?first\s+file\s+name\b", goal, re.IGNORECASE)
    content_request = re.search(r"\b(?:find|search)\s+(.+?)\s+and\s+(?:(?:(?:tell|give)\s+me\s+(?:its|their|the)\s+content)|(?:(?:read|show|open|give|tell)(?:\s+me)?\s+(?:(?:its|their|the)\s+)?(?:file\s+)?content))", goal, re.IGNORECASE)
    target_match = re.search(r"\b(?:find|search)\s+(.+?)(?=\s+and\s+(?:list|see|tell|read|show|open|give|the\s+first)\b)", goal, re.IGNORECASE)
    if list_request or first_name_request or content_request:
        match = list_request or first_name_request or content_request
        target = (target_match or match).group(1).strip().strip("'\"")
        steps.append({"step_id": step_counter, "tool": "find_file", "args": {"query": target}, "description": f"Find '{target}'", "alternative": None})
        step_counter += 1
        if content_request:
            steps.append({"step_id": step_counter, "tool": "read_file", "args": {"name": "$SELECTED_FILE"}, "description": f"Read the content of '{target}'", "alternative": None})
            step_counter += 1
        if list_request or first_name_request:
            steps.append({"step_id": step_counter, "tool": "list_files", "args": {"folder": target}, "description": f"List files in '{target}'", "alternative": None})
            step_counter += 1

    read_match = re.search(r"\b(?:read|open|show)\s+(?:me\s+)?(?:the\s+)?(?:file\s+)?(?:content\s+of\s+)?([A-Za-z0-9_.\-/]+)", goal, re.IGNORECASE)
    if not read_match:
        read_match = re.search(r"\b(?:tell\s+me\s+(?:its|the)\s+content(?:\s+of)?|show\s+me\s+(?:its|the)\s+content(?:\s+of)?)\s+([A-Za-z0-9_.\-/]+)", goal, re.IGNORECASE)
    if not read_match:
        read_match = re.search(r"\b(?:content\s+of)\s+([A-Za-z0-9_.\-/]+)", goal, re.IGNORECASE)
    if read_match and not steps:
        fname = read_match.group(1).strip()
        steps.append({
            "step_id": step_counter,
            "tool": "read_file",
            "args": {"name": fname},
            "description": f"Read the contents of '{fname}'",
            "alternative": None
        })
        step_counter += 1

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
    if find_match and not download_match and not read_match and not steps:
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


def plan_goal(goal: str, planner_info: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """
    Plan a list of steps for the given goal using LLM (if API key available)
    or fallback heuristic planner.
    """
    preferences = load_preferences()
    tool_lines = []
    for tool in tool_registry.list_tools():
        signature = ", ".join("{}: {}".format(key, schema.get("type", "any")) for key, schema in tool["parameters"].items())
        tool_lines.append("- {}({}) [risk: {}]: {}".format(tool["name"], signature, tool["risk_level"], tool["description"]))
    tools_prompt = "\n".join(tool_lines)
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    # Try OpenAI if configured
    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            prompt = PLANNER_SYSTEM_PROMPT.format(preferences=json.dumps(preferences, indent=2), tools=tools_prompt)
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
                if planner_info is not None:
                    planner_info["planner"] = "openai"
                return parsed
        except Exception as exc:
            print(f"OpenAI planner failed: {exc}")
            if planner_info is not None:
                planner_info["planner_error"] = f"OpenAI planner failed: {exc}"

    # Try Gemini if configured
    if gemini_key:
        try:
            from google import genai
            client = genai.Client(api_key=gemini_key)
            prompt = (
                PLANNER_SYSTEM_PROMPT.format(preferences=json.dumps(preferences, indent=2), tools=tools_prompt)
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
                if planner_info is not None:
                    planner_info["planner"] = "gemini"
                return parsed
        except Exception as exc:
            print(f"Gemini planner failed: {exc}")
            if planner_info is not None:
                previous = planner_info.get("planner_error")
                current = f"Gemini planner failed: {exc}"
                planner_info["planner_error"] = f"{previous}; {current}" if previous else current

    # Fallback heuristic planner
    steps = _heuristic_planner(goal, preferences)
    _enrich_steps_with_risk(steps)
    if planner_info is not None:
        planner_info["planner"] = "heuristic"
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

def create_task(goal: str, custom_steps: Optional[List[Dict[str, Any]]] = None, defer_planning: bool = False) -> Dict[str, Any]:
    """Create a task and optionally defer LLM planning to a background worker."""
    task_id = str(uuid.uuid4())[:8]

    if defer_planning:
        steps = []
        planner = None
        planner_error = None
    elif custom_steps:
        steps = custom_steps
        _enrich_steps_with_risk(steps)
        planner = "heuristic"
        planner_error = None
    else:
        planner_info: Dict[str, str] = {}
        steps = plan_goal(goal, planner_info)
        planner = planner_info.get("planner", "heuristic")
        planner_error = planner_info.get("planner_error")

    task_state = {
        "task_id": task_id,
        "goal": goal,
        "planner": planner,
        "planner_error": planner_error,
        "status": "running" if defer_planning else "pending",
        "current_step": 0,
        "steps": steps,
        "events": [],
        "failures_handled": [],
        "final_summary": None,
        "error_details": None,
        "message": "Planning task." if defer_planning else f"Task created with {len(steps)} planned step(s).",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if defer_planning:
        record_event(task_state, "plan", "Task planning started.")
    else:
        record_event(task_state, "plan", f"Plan created with {len(steps)} step(s) using the {planner} planner.")
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


def _goal_requirements(goal: str) -> List[str]:
    lower = goal.casefold()
    required = []
    if re.search(r"\b(find|search)\b", lower): required.append("find_file")
    if re.search(r"\b(read|open|show|tell|give)\b.{0,50}\b(content|contents)\b|\b(content|contents)\b", lower): required.append("read_file")
    if re.search(r"\b(list|files|file names?|first file name)\b", lower): required.append("list_files")
    for pattern, tool in ((r"\bdownload\b", "download_file"), (r"\b(move|archive)\b", "move_file"), (r"\b(remind|reminder)\b", "create_reminder"), (r"\b(email|send email)\b", "send_email")):
        if re.search(pattern, lower): required.append(tool)
    return list(dict.fromkeys(required))

def _keyword_goal_gaps(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    steps = task.get("steps", [])
    completed = [step for step in steps if step.get("status") == "completed"]
    gaps = []
    labels = {"find_file": "finding the requested item", "read_file": "reading the requested content", "list_files": "listing the requested file names", "download_file": "downloading the requested file", "move_file": "moving the requested file", "create_reminder": "creating the requested reminder", "send_email": "sending the requested email"}
    for tool in _goal_requirements(task.get("goal", "")):
        matches = [step for step in completed if step.get("tool") == tool and step.get("result") not in (None, [], "")]
        if tool == "list_files" and not matches:
            matches = [step for step in completed if step.get("tool") == "find_file" and step.get("result")]
        if not matches:
            gaps.append({"tool": tool, "label": labels[tool]})
    return gaps


def _llm_missing_tools(task: Dict[str, Any]) -> Optional[List[str]]:
    context = json.dumps({
        "goal": task.get("goal"),
        "steps": [{"tool": step.get("tool"), "status": step.get("status"), "result": step.get("result")} for step in task.get("steps", [])],
        "available_tools": [tool["name"] for tool in tool_registry.list_tools()],
    }, ensure_ascii=False)
    prompt = "Compare the goal with completed step results. Return only JSON as {\"missing_tools\":[...]} using only available tool names. An empty array means every requested part is covered. Do not treat failed or pending steps as complete. Context: " + context
    try:
        if os.getenv("OPENAI_API_KEY"):
            from openai import OpenAI
            response = OpenAI(api_key=os.getenv("OPENAI_API_KEY")).chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            raw = response.choices[0].message.content.strip()
        elif os.getenv("GEMINI_API_KEY"):
            from google import genai
            response = genai.Client(api_key=os.getenv("GEMINI_API_KEY")).models.generate_content(
                model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), contents=prompt
            )
            raw = response.text.strip()
        else:
            return None
        raw = raw.strip().strip(chr(96)).strip()
        data = json.loads(raw)
        missing = data.get("missing_tools")
        if isinstance(missing, list):
            allowed = {tool["name"] for tool in tool_registry.list_tools()}
            return [name for name in missing if name in allowed]
    except Exception as exc:
        print(f"Goal coverage LLM failed: {exc}")
    return None

def _goal_gaps(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    gaps = _keyword_goal_gaps(task)
    llm_missing = _llm_missing_tools(task)
    if llm_missing is None:
        return gaps
    existing = {gap["tool"] for gap in gaps}
    labels = {"find_file": "finding the requested item", "read_file": "reading the requested content", "list_files": "listing the requested file names", "download_file": "downloading the requested file", "move_file": "moving the requested file", "create_reminder": "creating the requested reminder", "send_email": "sending the requested email"}
    present = {step.get("tool") for step in task.get("steps", []) if step.get("status") == "completed" and step.get("result") not in (None, [], "")}
    for tool in llm_missing:
        if tool not in existing and tool not in present:
            gaps.append({"tool": tool, "label": labels[tool]})
    return gaps

def _missing_goal_steps(task: Dict[str, Any], gaps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not gaps: return []
    planned = _heuristic_planner(task.get("goal", ""), load_preferences())
    existing = {step.get("tool") for step in task.get("steps", []) if step.get("status") in ("completed", "pending", "running")}
    gap_tools = {gap["tool"] for gap in gaps}
    return [step for step in planned if step.get("tool") in gap_tools and step.get("tool") not in existing]


def _finish_partially(task: Dict[str, Any], missing_parts: List[str]) -> Dict[str, Any]:
    task["status"] = "partially_complete"
    task["missing_parts"] = list(dict.fromkeys(missing_parts))
    task["error_details"] = None
    task["message"] = "Task finished with some requested parts still incomplete."
    task["final_answer"] = generate_final_answer(task)
    missing_text = "; ".join(task["missing_parts"]) or "some requested parts"
    task["final_summary"] = f"JARVIS completed the available parts of your goal. The completed steps and their results are shown above. The following parts remain incomplete: {missing_text}. No further supported steps could be completed."
    save_task(task)
    return task

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
        if curr_idx < total_steps or (task["status"] == "needs_human" and (task.get("error_details") or {}).get("type") == "goal_coverage"):
            current_step_obj = task["steps"][curr_idx] if curr_idx < total_steps else {}

            # Case A: Resuming from needs_confirmation (medium risk)
            if task["status"] == "needs_confirmation":
                confirm = resume_input.get("confirm", resume_input.get("confirmed"))
                # Check for explicit rejection
                if confirm is False or resume_input.get("reject") is True:
                    task["status"] = "cancelled"
                    task["missing_parts"] = [gap["label"] for gap in _goal_gaps(task)]
                    task["message"] = f"Task cancelled by user at step {current_step_obj['step_id']}."
                    save_task(task)
                    return task
                current_step_obj["confirmed"] = True
                task["status"] = "running"
                task["message"] = f"Confirmation granted for step {current_step_obj['step_id']}."

            # Case B: Resuming from needs_approval (high risk)
            elif task["status"] == "needs_approval":
                confirm = resume_input.get("confirm", resume_input.get("confirmed"))
                if resume_input.get("approved") is False or confirm is False or resume_input.get("reject") is True:
                    task["status"] = "cancelled"
                    task["missing_parts"] = [gap["label"] for gap in _goal_gaps(task)]
                    task["message"] = f"Task cancelled: explicit approval was denied for step {current_step_obj['step_id']}."
                    save_task(task)
                    return task
                if resume_input.get("approved") is True or confirm is True:
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
                if err_details.get("type") == "goal_coverage":
                    if resume_input.get("confirm") is False:
                        return _finish_partially(task, err_details.get("missing_parts", []))
                    if resume_input.get("confirm") is True:
                        additions = err_details.get("missing_steps", [])
                    else:
                        instruction = (resume_input.get("input") or "").strip()
                        planner_info: Dict[str, str] = {}
                        additions = plan_goal(instruction, planner_info) if instruction else []
                        if not additions:
                            return _finish_partially(task, err_details.get("missing_parts", []))
                    offset = len(task["steps"])
                    _enrich_steps_with_risk(additions)
                    for i, addition in enumerate(additions, start=1):
                        addition["step_id"] = offset + i
                    if not additions:
                        return _finish_partially(task, err_details.get("missing_parts", []))
                    task["steps"].extend(additions)
                    task["missing_parts"] = []
                    task["error_details"] = None
                    task["status"] = "running"
                    task["message"] = "Continuing with the missing parts of your request." if resume_input.get("confirm") is True else "Adding your new instruction to the task."
                    record_event(task, "recover", task["message"])
                    save_task(task)
                    propagate_selected_file(task, next((s.get("result", [None])[0] for s in task["steps"] if s.get("tool") == "find_file" and s.get("result")), ""))
                elif err_details.get("type") == "empty_matches":
                    selected = resume_input.get("selected_file") or resume_input.get("selected_option")
                    if selected:
                        valid_options = err_details.get("options", [])
                        if valid_options and selected not in valid_options:
                            task["message"] = f"Invalid selection '{selected}'. Must be one of: {valid_options}"
                            save_task(task)
                            return task
                        current_step_obj["result"] = [selected]
                        current_step_obj["status"] = "completed"
                        current_step_obj["selected_option"] = selected
                        current_step_obj["error"] = None
                        task["error_details"] = None
                        task["current_step"] += 1
                        task["status"] = "running"
                        task["message"] = f"Selected '{selected}' from the close matches."
                        propagate_selected_file(task, selected)
                        record_event(task, "recover", task["message"], current_step_obj.get("step_id"))
                        save_task(task)
                    else:
                        query = (resume_input.get("input") or "").strip()
                        if not query:
                            task["message"] = "A new search query is required to resume."
                            save_task(task)
                            return task
                        stop_words = {"find", "and", "list", "its", "files", "the", "tell", "me", "content", "show", "open", "read", "what", "all", "are", "there", "in", "it", "give", "also", "first", "name"}
                        words = set(re.findall(r"[a-z0-9]+", query.casefold())) - stop_words
                        candidates = [path.relative_to(SANDBOX_DIR).as_posix() for path in SANDBOX_DIR.rglob("*")]
                        recommended = []
                        for name in candidates:
                            name_words = set(re.findall(r"[a-z0-9]+", name.casefold()))
                            score = max((SequenceMatcher(None, word, name.casefold()).ratio() for word in words), default=0)
                            if words.intersection(name_words) or score > 0.6:
                                recommended.append(name)
                        if recommended:
                            task["error_details"]["options"] = sorted(recommended, key=lambda name: name.casefold())[:3]
                            task["message"] = f"I found related names for '{query}'. Choose one, or enter another search query."
                            save_task(task)
                            return task
                        current_step_obj["args"]["query"] = query
                        current_step_obj["error"] = None
                        current_step_obj["attempts"] = 0
                        task["error_details"] = None
                        task["status"] = "running"
                        task["message"] = f"Retrying file search with query '{query}'."
                        record_event(task, "retry", task["message"], current_step_obj.get("step_id"))
                        save_task(task)
                # Multiple matches disambiguation
                elif err_details.get("type") == "multiple_matches":
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
                    record_event(task, "recover", f"User selected '{selected}'.", current_step_obj.get("step_id"))
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
                    record_event(task, "recover", task["message"], current_step_obj.get("step_id"))
                    save_task(task)

    # 2. Execution Loop
    total_steps = len(task["steps"])
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
            record_event(task, "escalate", task["message"], step.get("step_id"))
            save_task(task)
            return task

        # Risk check: high requires explicit approval
        if risk == "high" and not step.get("approved"):
            step["status"] = "needs_approval"
            task["status"] = "needs_approval"
            task["message"] = (
                f"Step {step['step_id']} ({tool_name}) has high risk and requires explicit approval."
            )
            record_event(task, "escalate", task["message"], step.get("step_id"))
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
            step["status"] = "running"
            current_tool = tool_name
            current_args = dict(tool_args)

            # If retrying and step specifies an alternative, switch to alternative
            if attempt > 1 and step.get("alternative"):
                current_tool = step["alternative"].get("tool", tool_name)
                current_args = step["alternative"].get("args", tool_args)

            # A. Execute Tool
            record_event(task, "execute", f"Executing {current_tool}.", step.get("step_id"))
            save_task(task)
            try:
                result = tool_registry.execute(current_tool, current_args)
            except Exception as exc:
                failure = f"Attempt {attempt}: tool execution threw exception: {exc}"
                attempts_log.append(failure)
                record_failure(task, failure)
                step["error"] = str(exc)
                if attempt < max_attempts:
                    step["status"] = "failed_retrying"
                    record_event(task, "retry", failure, step.get("step_id"))
                save_task(task)
                continue

            step["execution_result"] = result
            save_task(task)
            time.sleep(0.8)
            step["status"] = "verifying"
            save_task(task)

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
                record_event(task, "verify", f"Search found {len(result)} matching files.", step.get("step_id"))
                record_event(task, "escalate", task["message"], step.get("step_id"))
                save_task(task)
                return task

            if current_tool == "find_file" and isinstance(result, list) and len(result) == 0:
                query = current_args.get("query", "")
                all_names = [path.relative_to(SANDBOX_DIR).as_posix() for path in SANDBOX_DIR.rglob("*")]
                query_words = set(re.findall(r"[a-z0-9]+", query.casefold()))
                suggestions = []
                for name in all_names:
                    name_words = set(re.findall(r"[a-z0-9]+", name.casefold()))
                    similar = SequenceMatcher(None, query.casefold(), name.casefold()).ratio() > 0.6
                    if query_words.intersection(name_words) or similar:
                        suggestions.append(name)
                suggestions = sorted(suggestions, key=lambda name: SequenceMatcher(None, query.casefold(), name.casefold()).ratio(), reverse=True)[:3]
                record_event(task, "verify", f"Search found no files matching '{query}'.", step.get("step_id"))
                record_failure(task, f"No files matched search query '{query}'.")
                step["status"] = "needs_human"
                step["error"] = f"No files matched '{query}'."
                task["status"] = "needs_human"
                task["error_details"] = {
                    "type": "empty_matches",
                    "what_failed": f"No files matched '{query}'.",
                    "what_was_tried": [f"Executed find_file(query='{query}') -> []"],
                    "what_needed": "Choose a close match or enter a new search query.",
                    "options": suggestions,
                }
                task["message"] = f"No files matched '{query}'. Choose a close match or enter another search query."
                record_event(task, "escalate", task["message"], step.get("step_id"))
                save_task(task)
                return task

            # Single match from find_file: propagate to downstream steps
            if current_tool == "find_file" and isinstance(result, list) and len(result) == 1:
                propagate_selected_file(task, result[0])

            # C. Run Code-Based Verifier
            verifier = get_verifier(current_tool)
            if verifier:
                v_res = verifier(current_args, result)
                record_event(task, "verify", v_res.message, step.get("step_id"))
                if not v_res.success:
                    failure = f"Attempt {attempt}: tool '{current_tool}' verification failed: {v_res.message}"
                    attempts_log.append(failure)
                    record_failure(task, failure)
                    step["error"] = v_res.message
                    if attempt < max_attempts:
                        step["status"] = "failed_retrying"
                        record_event(task, "retry", failure, step.get("step_id"))
                    save_task(task)
                    continue
                else:
                    # Verification succeeded!
                    step["status"] = "completed"
                    step["result"] = result
                    step["error"] = None
                    step["verifier_message"] = v_res.message
                    step["verifier_passed"] = True
                    if attempt > 1:
                        step["recovered"] = True
                        step["recovery_notes"] = f"Recovered on attempt {attempt} after initial failure."
                        recovery = f"Step {step['step_id']} recovered on attempt {attempt}."
                        record_failure(task, f"{recovery} Previous issue: {attempts_log[-1]}")
                        record_event(task, "recover", recovery, step.get("step_id"))
                    step_succeeded = True
                    break
            else:
                # No verifier registered -> treat successful return as pass
                record_event(task, "verify", "No verifier was registered; tool result accepted.", step.get("step_id"))
                step["status"] = "completed"
                step["result"] = result
                step["error"] = None
                step["verifier_passed"] = False
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
            record_event(task, "escalate", task["message"], step.get("step_id"))
            save_task(task)
            return task

        # Advance to next step and persist state
        task["current_step"] += 1
        save_task(task)

    # 3. Check that every requested part is covered before completion.
    task["coverage_checks"] = task.get("coverage_checks", 0) + 1
    gaps = _goal_gaps(task)
    if gaps:
        missing_parts = [gap["label"] for gap in gaps]
        missing_steps = _missing_goal_steps(task, gaps)
        if task["coverage_checks"] >= 2 or not missing_steps:
            return _finish_partially(task, missing_parts)
        task["status"] = "needs_human"
        task["missing_parts"] = missing_parts
        task["error_details"] = {
            "type": "goal_coverage",
            "what_failed": "Some parts of the goal are not covered yet.",
            "what_was_tried": [step.get("description", step.get("tool", "Task step")) for step in task["steps"] if step.get("status") == "completed"],
            "what_needed": "Continue with the missing parts, or stop here.",
            "missing_parts": missing_parts,
            "missing_steps": missing_steps,
        }
        task["message"] = "I only completed part of your request: " + ", ".join(missing_parts) + ". Want me to continue?"
        record_event(task, "escalate", task["message"])
        save_task(task)
        return task

    # 4. All requested parts are covered.
    task["status"] = "completed"
    task["missing_parts"] = []
    task["message"] = f"All {total_steps} step(s) completed and verified successfully."
    task["final_answer"] = generate_final_answer(task)
    task["final_summary"] = generate_final_summary(task)
    save_task(task)
    return task
