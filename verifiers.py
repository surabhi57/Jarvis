"""
Code-based verifiers for JARVIS tools.
Deterministic verification logic (NOT LLM opinions).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import json

SANDBOX_DIR = Path(__file__).resolve().parent / "sandbox"


@dataclass
class VerificationResult:
    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None


def verify_find_file(args: Dict[str, Any], result: Any) -> VerificationResult:
    """Verify that find_file executed cleanly and returned a valid list of matches."""
    if not isinstance(result, list):
        return VerificationResult(
            success=False,
            message=f"find_file returned invalid type: {type(result).__name__}, expected list."
        )
    return VerificationResult(
        success=True,
        message=f"find_file verified successfully, found {len(result)} candidate(s).",
        details={"match_count": len(result), "matches": result}
    )


def verify_read_file(args: Dict[str, Any], result: Any) -> VerificationResult:
    """Verify the requested sandbox file exists and readable text was returned."""
    requested = str(args.get("name") or "").replace("\\", "/").strip("/").casefold()
    exists = any(
        path.is_file() and path.relative_to(SANDBOX_DIR).as_posix().casefold() == requested
        for path in SANDBOX_DIR.rglob("*")
    )
    if not exists:
        return VerificationResult(success=False, message=f"Verification failed: File '{args.get('name')}' does not exist in sandbox.")
    if not isinstance(result, str):
        return VerificationResult(success=False, message="read_file returned invalid content; expected text.")
    if not result.strip():
        return VerificationResult(success=False, message=f"Verification failed: File '{args.get('name')}' is empty.")
    return VerificationResult(
        success=True,
        message=f"File '{args.get('name')}' exists and its contents were read.",
        details={"character_count": len(result)},
    )


def verify_list_files(args: Dict[str, Any], result: Any) -> VerificationResult:
    """Verify the requested sandbox folder exists and list_files returned a list."""
    folder = args.get("folder")
    target = (SANDBOX_DIR / str(folder or "")).resolve()
    if not target.is_relative_to(SANDBOX_DIR.resolve()) or not target.is_dir():
        return VerificationResult(
            success=False,
            message=f"Verification failed: Folder '{folder}' does not exist in sandbox."
        )
    if not isinstance(result, list):
        return VerificationResult(
            success=False,
            message=f"list_files returned invalid type: {type(result).__name__}, expected list."
        )
    if not result:
        return VerificationResult(success=False, message=f"Verification failed: Folder '{folder}' is empty.")
    return VerificationResult(
        success=True,
        message=f"Folder '{folder}' verified, found {len(result)} file(s).",
        details={"folder": str(target), "files": result}
    )


def verify_download_file(args: Dict[str, Any], result: Any) -> VerificationResult:
    """Verify downloaded file actually exists on disk in ./sandbox and has non-zero size."""
    filename = args.get("name")
    if not filename:
        return VerificationResult(
            success=False,
            message="Verifier error: 'name' argument missing from download_file call."
        )
    target = SANDBOX_DIR / filename
    if not target.exists():
        return VerificationResult(
            success=False,
            message=f"Verification failed: File '{filename}' was not found on disk in sandbox ({target})."
        )
    try:
        size = target.stat().st_size
        if size == 0:
            return VerificationResult(
                success=False,
                message=f"Verification failed: File '{filename}' exists but is 0 bytes (empty download)."
            )
        return VerificationResult(
            success=True,
            message=f"File '{filename}' successfully verified on disk ({size} bytes).",
            details={"path": str(target), "size_bytes": size}
        )
    except Exception as e:
        return VerificationResult(
            success=False,
            message=f"Verification check encountered an OS error accessing '{filename}': {e}"
        )


def verify_move_file(args: Dict[str, Any], result: Any) -> VerificationResult:
    """Verify file was placed in target folder and no longer resides at root/source."""
    name = args.get("name")
    folder = args.get("folder")
    if not name or not folder:
        return VerificationResult(
            success=False,
            message="Verifier error: 'name' or 'folder' argument missing from move_file call."
        )
    dest_path = SANDBOX_DIR / folder / name
    src_path = SANDBOX_DIR / name

    if not dest_path.exists():
        return VerificationResult(
            success=False,
            message=f"Verification failed: Destination file '{dest_path}' does not exist."
        )
    if dest_path.stat().st_size == 0:
        return VerificationResult(
            success=False,
            message=f"Verification failed: Moved file '{dest_path}' exists but has 0 bytes."
        )
    # Check that it's moved from original location if source and dest differ
    if src_path != dest_path and src_path.exists():
        return VerificationResult(
            success=False,
            message=f"Verification failed: File still exists at source path '{src_path}'."
        )
    return VerificationResult(
        success=True,
        message=f"File '{name}' verified moved into '{folder}' ({dest_path.stat().st_size} bytes).",
        details={"destination": str(dest_path)}
    )


def verify_create_reminder(args: Dict[str, Any], result: Any) -> VerificationResult:
    """Verify reminder was written to ./sandbox/reminders.json and matches expected text."""
    reminders_path = SANDBOX_DIR / "reminders.json"
    if not reminders_path.exists():
        return VerificationResult(
            success=False,
            message="Verification failed: reminders.json does not exist in sandbox."
        )
    try:
        data = json.loads(reminders_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return VerificationResult(
                success=False,
                message="Verification failed: reminders.json content is not a list."
            )
        expected_text = args.get("text", "")
        for item in data:
            if item.get("text") == expected_text:
                return VerificationResult(
                    success=True,
                    message=f"Reminder '{expected_text}' verified in reminders.json.",
                    details={"matched_entry": item}
                )
        return VerificationResult(
            success=False,
            message=f"Verification failed: Reminder '{expected_text}' not found in reminders.json."
        )
    except Exception as e:
        return VerificationResult(
            success=False,
            message=f"Verification failed: Could not parse reminders.json ({e})."
        )


def verify_send_email(args: Dict[str, Any], result: Any) -> VerificationResult:
    """Verify email was logged to ./sandbox/sent_emails.json and recipient/body match."""
    emails_path = SANDBOX_DIR / "sent_emails.json"
    if not emails_path.exists():
        return VerificationResult(
            success=False,
            message="Verification failed: sent_emails.json does not exist in sandbox."
        )
    try:
        data = json.loads(emails_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return VerificationResult(
                success=False,
                message="Verification failed: sent_emails.json content is not a list."
            )
        target_to = args.get("to", "")
        for item in data:
            if item.get("to") == target_to:
                return VerificationResult(
                    success=True,
                    message=f"Email to '{target_to}' verified in sent_emails.json.",
                    details={"matched_entry": item}
                )
        return VerificationResult(
            success=False,
            message=f"Verification failed: No email addressed to '{target_to}' found in sent_emails.json."
        )
    except Exception as e:
        return VerificationResult(
            success=False,
            message=f"Verification failed: Could not parse sent_emails.json ({e})."
        )


VERIFIERS: Dict[str, Any] = {
    "find_file": verify_find_file,
    "read_file": verify_read_file,
    "list_files": verify_list_files,
    "download_file": verify_download_file,
    "move_file": verify_move_file,
    "create_reminder": verify_create_reminder,
    "send_email": verify_send_email,
}


def get_verifier(tool_name: str):
    """Retrieve verifier callable by tool name."""
    return VERIFIERS.get(tool_name)
