import os
import json
import base64
import requests
from celery import Celery
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ── Celery ────────────────────────────────────────────────────────────────────
celery_app = Celery(
    "automation_workers",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
)
celery_app.conf.update(
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    worker_max_tasks_per_child=1000,
)

# ── Config ────────────────────────────────────────────────────────────────────
CLICKUP_API_TOKEN = os.getenv("CLICKUP_TOKEN")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN      = os.getenv("GITHUB_TOKEN")
GITHUB_REPO       = os.getenv("GITHUB_REPO")          # e.g. "akash2/clickup_github"
GITHUB_BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main")


# ── GitHub Helpers ────────────────────────────────────────────────────────────

def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_base_branch_sha() -> str:
    """Get the latest commit SHA of the base branch."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/git/ref/heads/{GITHUB_BASE_BRANCH}"
    resp = requests.get(url, headers=_github_headers())
    resp.raise_for_status()
    return resp.json()["object"]["sha"]


def _create_branch(branch_name: str, sha: str):
    """Create a new branch from the given SHA."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/git/refs"
    payload = {"ref": f"refs/heads/{branch_name}", "sha": sha}
    resp = requests.post(url, headers=_github_headers(), json=payload)
    if resp.status_code == 422:
        print(f"⚠️  Branch '{branch_name}' already exists — reusing it.")
    else:
        resp.raise_for_status()
        print(f"🌿 Created branch: {branch_name}")


def _push_file_to_github(branch_name: str, filepath: str, content: str, commit_message: str):
    """Push a single file directly to GitHub via Contents API (no local disk write)."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filepath}"

    # Check if file already exists on this branch (need its SHA to update)
    existing = requests.get(url, headers=_github_headers(), params={"ref": branch_name})
    payload = {
        "message": commit_message,
        "content": encoded,
        "branch": branch_name,
    }
    if existing.status_code == 200:
        payload["sha"] = existing.json()["sha"]  # required for updates

    resp = requests.put(url, headers=_github_headers(), json=payload)
    resp.raise_for_status()
    print(f"📄 Pushed: {filepath} → {branch_name}")


# ── Celery Task ───────────────────────────────────────────────────────────────

@celery_app.task(name="tasks.process_clickup_task")
def process_clickup_task(task_id: str):
    print(f"--- Processing ClickUp Task ID: {task_id} ---")

    # 1. Fetch task details from ClickUp
    resp = requests.get(
        f"https://api.clickup.com/api/v2/task/{task_id}",
        headers={"Authorization": CLICKUP_API_TOKEN},
    )
    if resp.status_code != 200:
        print(f"Error fetching task from ClickUp: {resp.text}")
        return False

    task_data     = resp.json()
    task_name     = task_data.get("name", "GeneratedModule")
    task_description = task_data.get("description", "No requirements provided.")

    # Extract who moved the task (for branch naming)
    assignees = task_data.get("assignees", [])
    username  = assignees[0].get("username", "unknown").replace(" ", "-") if assignees else "unknown"

    # 2. Dynamic branch name: feature/clickup-task-{task_id} or dev-{username}/task-{task_id}
    branch_name = f"feature/clickup-task-{task_id}"
    # Uncomment below to use username-based branches instead:
    # branch_name = f"dev-{username}/task-{task_id}"

    print(f"🌿 Target branch: {branch_name}")

    # 3. Generate code with Gemini
    client = OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )

    system_instruction = (
        "You are an expert backend engineer specializing in Python and Django REST Framework. "
        "Your task is to read the product requirements provided by the user and generate production-grade code. "
        "You must output a valid JSON array ONLY. Each element must have the keys 'filename' and 'code_content'. "
        "CRITICAL: The entire response must be parseable by Python's json.loads(). "
        "Do NOT wrap output in markdown code fences. Do NOT use triple-quoted strings. "
        "All double quotes inside 'code_content' strings must be escaped as \\\" — "
        "use single-line strings with \\n for newlines. "
        "Output raw JSON only, nothing else."
    )

    user_prompt = f"Task Title: {task_name}\nRequirements Description:\n{task_description}"
    print("Sending requirements to Gemini...")

    try:
        message = client.chat.completions.create(
            model="gemini-2.5-flash",
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt},
            ],
        )

        raw_output = message.choices[0].message.content.strip()
        if raw_output.startswith("```"):
            raw_output = raw_output.split("\n", 1)[1]
            raw_output = raw_output.rsplit("```", 1)[0].strip()

        files = json.loads(raw_output)
        if isinstance(files, dict):
            files = [files]

        print(f"✅ Gemini generated {len(files)} file(s)")

        # 4. Create isolated GitHub branch from base
        base_sha = _get_base_branch_sha()
        _create_branch(branch_name, base_sha)

        # 5. Push each file directly to GitHub — no local disk write
        for f_info in files:
            filename     = f_info.get("filename", "output.py")
            code_content = f_info.get("code_content", "")
            # Use just the basename to keep things flat, or keep full path for structure
            filepath = os.path.basename(filename)
            _push_file_to_github(
                branch_name  = branch_name,
                filepath     = f"scaffolded/{task_id}/{filepath}",
                content      = code_content,
                commit_message = f"feat: scaffold {filepath} for ClickUp task {task_id}",
            )

        print(f"\n🎉 All files pushed to GitHub branch '{branch_name}'")
        print(f"🔗 https://github.com/{GITHUB_REPO}/tree/{branch_name}")
        return True

    except json.JSONDecodeError:
        print("Error: Gemini failed to return valid JSON.")
        print(f"Raw Output: {raw_output}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred: {str(e)}")
        return False
