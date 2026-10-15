# JARVIS: Failure-Aware Agent Backend

JARVIS is a FastAPI backend implementing an autonomous, failure-aware agent architecture built around a **Plan -> Execute -> Verify -> Recover** loop with human-in-the-loop escalation.

---

## 🏛️ Architecture Overview

Unlike traditional LLM agents that assume tool execution succeeded, JARVIS uses **deterministic code-based verifiers (NOT LLM opinions)** to validate the real-world side effects of actions.

```
       +--------------------+
       |   User Goal / API  |
       +---------+----------+
                 |
                 v
       +--------------------+
       |      PLANNER       | <--- preferences.json
       | (LLM or Heuristic) |
       +---------+----------+
                 |
                 v
       +--------------------+
  +--> |   RISK ASSESSMENT  |
  |    +---------+----------+
  |              |
  |      +-------+-------+
  |      |               |
  |   [Medium]        [High]
  |      |               |
  |      v               v
  |  Pause with       Pause with
  |  "needs_confirm"  "needs_approval"
  |      |               |
  |      +-------+-------+
  |              | (Resumed via /tasks/{id}/resume)
  |              v
  |    +-------------------+
  |    |   EXECUTE TOOL    |
  |    +---------+---------+
  |              |
  |              v
  |    +-------------------+
  |    |   CODE VERIFIER   | (Inspects disk, reminders.json, etc.)
  |    +---------+---------+
  |              |
  |        +-----+-----+
  |        |           |
  |    [Passed]     [Failed]
  |        |           |
  |        |           v
  |        |    +-------------------+
  |        |    | RECOVER (Retries) | (Retry up to 2 times or try alternative)
  |        |    +---------+---------+
  |        |              |
  |        |      +-------+-------+
  |        |      |               |
  |        |  [Recovered]   [Still Failing / Ambiguous Search]
  |        |      |               |
  |        |      |               v
  |        |      |       +------------------------------------+
  |        |      |       |        "needs_human" Status        |
  |        |      |       | - what failed                      |
  |        |      |       | - what was tried                   |
  |        |      |       | - what's needed                    |
  |        |      |       | - options (for ambiguous matches)  |
  |        |      |       +------------------------------------+
  |        v      v
  +-- Advance Step (Saved to tasks/<task_id>.json)
           |
      [All Done]
           v
       Completed
```

---

## 📁 Repository Structure

```
.
├── main.py              # FastAPI application, routes, and CORS setup
├── agent.py             # Planner, execution loop, recovery, and persistence
├── tools.py             # Mock tools operating on ./sandbox with risk levels & FAIL_ONCE
├── verifiers.py         # Deterministic code-based verification checks
├── preferences.json     # User preferences injected into the planner
├── requirements.txt     # Python dependencies
├── test_jarvis.py       # Comprehensive pytest suite
├── sandbox/             # Sandbox folder for isolated file operations
│   ├── financial_report_q1.pdf  # Seed file
│   ├── financial_report_q2.pdf  # Seed file (similar name for disambiguation)
│   └── meeting_notes.txt        # Seed file
└── tasks/               # Persisted task state JSON files (<task_id>.json)
```

---

## 🚀 Quickstart

### 1. Requirements & Setup

Make sure you have Python 3.10+ installed.

```bash
# Optional: create a virtual environment
python -m venv venv
venv\Scripts\activate      # On Windows
source venv/bin/activate    # On Linux/macOS

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure LLM API Key (Optional)

JARVIS supports **OpenAI**, **Gemini**, or a deterministic **Heuristic Fallback Planner**.

```bash
# For OpenAI
set OPENAI_API_KEY=sk-...
# or export OPENAI_API_KEY=sk-...

# Or for Google Gemini
set GEMINI_API_KEY=AIzaSy...
# or export GEMINI_API_KEY=AIzaSy...
```

> **Note:** If no API key is set, JARVIS will automatically use its built-in rule-based heuristic planner. It works immediately out of the box with zero setup!

### 3. Run the Backend

Run with `uvicorn`:

```bash
python -m uvicorn main:app --reload --host 0.0.0.0
```

The server starts at `http://127.0.0.1:8000`.  
Interactive Swagger documentation is available at `http://127.0.0.1:8000/docs`.

### 4. Serve the UI

From the project directory, run:

```bash
python -m http.server 5500
```

Then open `http://localhost:5500` in a browser.

---

## 🧩 Modular Tool Registry

Adding a new tool takes **one function + one registry entry**.

```python
from tools import tool_registry

# Option A: Decorator
@tool_registry.register(
    name="backup_database",
    risk_level="high",               # "low" | "medium" | "high"
    verifier_name="verify_backup",   # maps to verifiers.py
    description="Back up database to backup directory"
)
def backup_database(destination: str):
    ...

# Option B: Direct Registry Add
tool_registry.add(
    name="backup_database",
    func=backup_database,
    risk_level="high",
    verifier_name="verify_backup",
    description="Back up database to backup directory"
)
```

### Registered Tools & Risk Levels

| Tool | Risk Level | Description | Code-Based Verifier |
|---|---|---|---|
| `find_file(query)` | `low` | Searches `./sandbox` for matching files | Checks valid match list; escalates if > 1 match |
| `read_file(name)` | `low` | Reads text content from a sandbox file | Checks file exists and is non-empty |
| `list_files(folder)` | `low` | Lists files in a sandbox folder | Checks that the folder exists |
| `download_file(name)` | `low` | Downloads mock file (features `FAIL_ONCE`) | Checks file exists on disk and `size > 0` |
| `create_reminder(text, time)` | `low` | Appends reminder | Reads `./sandbox/reminders.json` to verify entry |
| `move_file(name, folder)` | `medium` | Moves file to target subfolder | Checks destination exists and source is moved |
| `send_email(to, body)` | `high` | Dispatches email | Reads `./sandbox/sent_emails.json` to verify entry |

---

## 🛡️ Failure & Recovery Capabilities

### 1. `FAIL_ONCE` Silent Failure & Auto-Recovery
The `download_file` tool has a `FAIL_ONCE` flag. On the first invocation, it silently writes nothing to disk.
- **Execute**: tool returns without an exception.
- **Verify**: `verify_download_file` inspects `./sandbox/<name>` on disk, finds it missing, and marks verification as **failed**.
- **Recover**: Agent triggers retry #1. `FAIL_ONCE` is now disabled. The tool executes and writes the file.
- **Verify**: Code check passes! The step finishes with `attempts: 2` and `recovered: true`.

### 2. Search Disambiguation (Multiple Matches)
When searching for files (e.g. `query="financial_report"`):
- The sandbox contains both `financial_report_q1.pdf` and `financial_report_q2.pdf`.
- JARVIS detects multiple candidate files and pauses with status `"needs_human"`.
- Response details what failed, what was tried, and the list of available `options`.
- The user calls `POST /tasks/{id}/resume` with `{"selected_option": "financial_report_q1.pdf"}` to proceed.

### 3. Risk-Based Human Confirmation & Approval
- **Medium Risk (`move_file`)**: Pauses with status `"needs_confirmation"`. Resumed with `{"confirmed": true}`.
- **High Risk (`send_email`)**: Pauses with status `"needs_approval"`. Resumed with `{"approved": true}`.

### 4. Recovery Retries & "needs_human" Escalation
If a step continues to fail after 2 retries (3 total attempts), the task pauses with status `"needs_human"`, providing:
- `what_failed`: Clear explanation of the failure
- `what_was_tried`: Chronological attempt history with error messages
- `what_needed`: What action or resource is required from a human

### 5. Persistent State
After every step, the complete task graph (goal, steps, attempts, verifier output, current step) is written to:
```
tasks/<task_id>.json
```
If the server restarts, any task can be retrieved or resumed using its `task_id`.

---

## 📡 API Endpoints

### 1. `POST /tasks`
Create a new task; planning and execution continue in the background. Poll `GET /tasks/{task_id}` for live progress.

```bash
curl -X POST http://127.0.0.1:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"goal": "Download summary.pdf and remind me at 18:00"}'
```

Response:
```json
{
  "task_id": "a1b2c3d4",
  "status": "completed",
  "current_step": 2,
  "total_steps": 2,
  "message": "All 2 step(s) completed and verified successfully.",
  "state": { ... }
}
```

### 2. `GET /tasks/{task_id}`
Poll the current task state from `tasks/<task_id>.json`.

```bash
curl http://127.0.0.1:8000/tasks/a1b2c3d4
```

### 3. `POST /tasks/{task_id}/resume`
Resume a paused task by providing human input, confirmation, or disambiguation choice.

```bash
# For medium risk confirmation:
curl -X POST http://127.0.0.1:8000/tasks/a1b2c3d4/resume \
  -H "Content-Type: application/json" \
  -d '{"confirmed": true}'

# For high risk approval:
curl -X POST http://127.0.0.1:8000/tasks/a1b2c3d4/resume \
  -H "Content-Type: application/json" \
  -d '{"approved": true}'

# For multiple match selection:
curl -X POST http://127.0.0.1:8000/tasks/a1b2c3d4/resume \
  -H "Content-Type: application/json" \
  -d '{"selected_option": "financial_report_q1.pdf"}'
```

### 4. `GET /tools`
List all registered tools, their schemas, risk categories, and verifiers.

```bash
curl http://127.0.0.1:8000/tools
```

### 5. `POST /sandbox/reset`
Resets `./sandbox` to the 3 seed files and resets `FAIL_ONCE = True` for repeatable demos.

```bash
curl -X POST http://127.0.0.1:8000/sandbox/reset
```

---

## 🧪 Running Tests

To run the automated test suite:

```bash
python -m pytest -v test_jarvis.py
```
# JARVIS Web Console

The single-page frontend is plain HTML, CSS, and JavaScript. Run the backend with `python -m uvicorn main:app --reload --host 0.0.0.0` and serve the UI with `python -m http.server 5500`. Open `http://localhost:5500` in a browser. It connects to port 8000 on the page's host. To view a task on another device, open the same page with its `?task=<id>` URL; the phone must be able to reach the machine running the backend over the network.

