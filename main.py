from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
import os
import json
import subprocess
from datetime import datetime

load_dotenv()

app = FastAPI(title="ClickUp Webhook Receiver")

CLICKUP_WEBHOOK_SECRET = os.getenv("CLICKUP_WEBHOOK_SECRET", "")

# Log file to record all incoming webhooks
LOG_FILE = "webhook_log.json"


def append_to_log(event: dict):
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            try:
                logs = json.load(f)
            except json.JSONDecodeError:
                logs = []
    logs.append(event)
    with open(LOG_FILE, "w") as f:
        json.dump(logs, f, indent=2)


def git_commit_and_push(message: str):
    """Stage the log file, commit, and push to git."""
    try:
        subprocess.run(["git", "add", LOG_FILE], check=True, cwd=".")
        subprocess.run(["git", "commit", "-m", message], check=True, cwd=".")
        subprocess.run(["git", "push"], check=True, cwd=".")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Git error: {e}")
        return False


@app.get("/")
def health():
    return {"status": "running", "message": "ClickUp webhook receiver is live"}


@app.post("/webhook/clickup")
async def clickup_webhook(request: Request):
    payload = await request.json()

    event_type = payload.get("event", "unknown")
    task_id    = payload.get("task_id", "N/A")
    history    = payload.get("history_items", [{}])
    changed_by = history[0].get("user", {}).get("username", "unknown") if history else "unknown"

    print(f"\n📥 Received event: {event_type} | Task: {task_id} | By: {changed_by}")

    # Build a log entry
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "event":     event_type,
        "task_id":   task_id,
        "changed_by": changed_by,
        "raw": payload,
    }

    # 1. Save to log file
    append_to_log(log_entry)
    print(f"✅ Logged to {LOG_FILE}")

    # 2. Commit & push to git
    commit_msg = f"webhook: {event_type} on task {task_id} by {changed_by}"
    pushed = git_commit_and_push(commit_msg)
    if pushed:
        print(f"🚀 Pushed to git: {commit_msg}")
    else:
        print("⚠️  Git push skipped (no remote or nothing to commit)")

    return {"status": "ok", "event": event_type, "task_id": task_id}
