"""
Mock tools for JARVIS working on ./sandbox directory.
Provides modular registration and risk categorization: low / medium / high.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import json
import shutil

from verifiers import VERIFIERS, get_verifier

SANDBOX_DIR = Path(__file__).resolve().parent / "sandbox"
SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

# FAIL_ONCE flag: when True, download_file silently writes nothing on the first call
FAIL_ONCE = True


def set_fail_once(value: bool) -> None:
    """Set or reset the FAIL_ONCE flag for testing / demonstrations."""
    global FAIL_ONCE
    FAIL_ONCE = value


def get_fail_once() -> bool:
    """Return the current FAIL_ONCE state."""
    global FAIL_ONCE
    return FAIL_ONCE


@dataclass
class ToolDefinition:
    name: str
    func: Callable
    risk_level: str  # "low", "medium", "high"
    verifier_name: Optional[str] = None
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """Modular tool registry. Adding a new tool = one function + one registry entry."""

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        risk_level: str = "low",
        verifier_name: Optional[str] = None,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
    ):
        """Decorator to register a tool function in one line."""
        def decorator(func: Callable):
            self.add(
                name=name,
                func=func,
                risk_level=risk_level,
                verifier_name=verifier_name,
                description=description or (func.__doc__ or "").strip(),
                parameters=parameters or {},
            )
            return func
        return decorator

    def add(
        self,
        name: str,
        func: Callable,
        risk_level: str = "low",
        verifier_name: Optional[str] = None,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
    ):
        """Register a tool instance."""
        if risk_level not in ("low", "medium", "high"):
            raise ValueError(f"Invalid risk_level '{risk_level}'. Must be 'low', 'medium', or 'high'.")

        # Default verifier name to tool name if in VERIFIERS
        v_name = verifier_name or (name if name in VERIFIERS else None)

        self._tools[name] = ToolDefinition(
            name=name,
            func=func,
            risk_level=risk_level,
            verifier_name=v_name,
            description=description or (func.__doc__ or "").strip(),
            parameters=parameters or {},
        )

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def execute(self, name: str, args: Dict[str, Any]) -> Any:
        tool = self.get(name)
        if not tool:
            raise KeyError(f"Tool '{name}' is not registered.")
        return tool.func(**args)

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "risk_level": t.risk_level,
                "verifier": t.verifier_name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]


# Instantiate global registry
tool_registry = ToolRegistry()


# ==========================================
# Tool Implementations
# ==========================================

@tool_registry.register(
    name="find_file",
    risk_level="low",
    verifier_name="find_file",
    description="Search for files in the sandbox matching a query string. Returns matching filenames.",
    parameters={"query": {"type": "string", "description": "Substring or filename query"}}
)
def find_file(query: str) -> List[str]:
    """Search ./sandbox for files matching query."""
    matches = []
    q = query.strip().casefold()
    for path in SANDBOX_DIR.rglob("*"):
        if path.is_file():
            rel = path.relative_to(SANDBOX_DIR).as_posix()
            if q in rel.casefold():
                matches.append(rel)
    return sorted(matches)


@tool_registry.register(
    name="read_file",
    risk_level="low",
    verifier_name="read_file",
    description="Read and return the text content of a file in the sandbox.",
    parameters={"name": {"type": "string", "description": "File name or relative path inside the sandbox"}}
)
def read_file(name: str) -> str:
    """Read a text file from the sandbox, resolving its path case-insensitively."""
    requested = name.replace("\\", "/").strip("/").casefold()
    for path in SANDBOX_DIR.rglob("*"):
        if path.is_file() and path.relative_to(SANDBOX_DIR).as_posix().casefold() == requested:
            return path.read_text(encoding="utf-8", errors="replace")
    raise FileNotFoundError(f"File '{name}' does not exist in sandbox.")


@tool_registry.register(
    name="list_files",
    risk_level="low",
    verifier_name="list_files",
    description="List files in a folder inside the sandbox.",
    parameters={"folder": {"type": "string", "description": "Folder path inside the sandbox"}}
)
def list_files(folder: str) -> List[str]:
    """List files directly inside a sandbox folder."""
    target = (SANDBOX_DIR / folder).resolve()
    if not target.is_relative_to(SANDBOX_DIR.resolve()) or not target.is_dir():
        return []
    return sorted(path.name for path in target.iterdir() if path.is_file())


@tool_registry.register(
    name="download_file",
    risk_level="low",
    verifier_name="download_file",
    description="Download a mock file into the sandbox by name. Demonstrates FAIL_ONCE behavior.",
    parameters={"name": {"type": "string", "description": "Filename to download, e.g. 'summary.pdf'"}}
)
def download_file(name: str) -> Dict[str, Any]:
    """Mock download. If FAIL_ONCE is active, silently writes nothing on the first call."""
    global FAIL_ONCE
    target = SANDBOX_DIR / name

    if FAIL_ONCE:
        FAIL_ONCE = False
        # Silently write nothing: simulates network drop or partial download failure
        return {
            "status": "initiated",
            "message": f"Download initiated for '{name}'. (Simulated silent failure: no bytes written)",
            "filename": name,
        }

    # Second call / normal operation writes mock content
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"Mock downloaded content for: {name}\nTimestamp: {datetime.now(timezone.utc).isoformat()}\nStatus: Verified\n",
        encoding="utf-8"
    )
    return {
        "status": "success",
        "message": f"File '{name}' downloaded successfully to sandbox.",
        "filename": name,
        "path": str(target),
    }


@tool_registry.register(
    name="move_file",
    risk_level="medium",
    verifier_name="move_file",
    description="Move a file within sandbox to a target subfolder (requires confirmation).",
    parameters={
        "name": {"type": "string", "description": "Source filename"},
        "folder": {"type": "string", "description": "Destination folder name"}
    }
)
def move_file(name: str, folder: str) -> Dict[str, Any]:
    """Move a file from ./sandbox/name to ./sandbox/folder/name."""
    src = SANDBOX_DIR / name
    if not src.exists():
        raise FileNotFoundError(f"Source file '{name}' does not exist in sandbox.")

    dest_folder = SANDBOX_DIR / folder
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest = dest_folder / src.name

    shutil.move(str(src), str(dest))
    return {
        "status": "success",
        "message": f"Moved '{name}' to '{folder}/{src.name}'.",
        "source": str(src),
        "destination": str(dest)
    }


@tool_registry.register(
    name="create_reminder",
    risk_level="low",
    verifier_name="create_reminder",
    description="Create a reminder entry saved into ./sandbox/reminders.json.",
    parameters={
        "text": {"type": "string", "description": "Reminder description"},
        "time": {"type": "string", "description": "Reminder time, e.g. '17:00'"}
    }
)
def create_reminder(text: str, time: str) -> Dict[str, Any]:
    """Create reminder and append to ./sandbox/reminders.json."""
    reminders_file = SANDBOX_DIR / "reminders.json"
    reminders = []
    if reminders_file.exists():
        try:
            reminders = json.loads(reminders_file.read_text(encoding="utf-8"))
        except Exception:
            reminders = []

    entry = {
        "text": text,
        "time": time,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    reminders.append(entry)
    reminders_file.write_text(json.dumps(reminders, indent=2), encoding="utf-8")
    return {
        "status": "success",
        "message": f"Reminder created for '{text}' at {time}.",
        "reminder": entry
    }


@tool_registry.register(
    name="send_email",
    risk_level="high",
    verifier_name="send_email",
    description="Send an email to a recipient (high risk, requires explicit approval).",
    parameters={
        "to": {"type": "string", "description": "Recipient email address"},
        "body": {"type": "string", "description": "Email body content"}
    }
)
def send_email(to: str, body: str) -> Dict[str, Any]:
    """Simulate sending email and log to ./sandbox/sent_emails.json."""
    emails_file = SANDBOX_DIR / "sent_emails.json"
    emails = []
    if emails_file.exists():
        try:
            emails = json.loads(emails_file.read_text(encoding="utf-8"))
        except Exception:
            emails = []

    entry = {
        "to": to,
        "body": body,
        "sent_at": datetime.now(timezone.utc).isoformat()
    }
    emails.append(entry)
    emails_file.write_text(json.dumps(emails, indent=2), encoding="utf-8")
    return {
        "status": "success",
        "message": f"Email dispatched to '{to}'.",
        "email": entry
    }
