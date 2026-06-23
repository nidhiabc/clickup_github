import hmac
import hashlib
import os
from fastapi import FastAPI, Request, HTTPException, Header
from dotenv import load_dotenv
from tasks import process_clickup_task

load_dotenv()

app = FastAPI(title="ClickUp Automation Gateway")

CLICKUP_WEBHOOK_SECRET = os.getenv("CLICKUP_WEBHOOK_SECRET", "your_webhook_secret_here")


@app.get("/")
def health():
    return {"status": "running", "message": "ClickUp Automation Gateway is live"}


@app.post("/webhook/clickup")
async def clickup_webhook(
    request: Request,
    x_signature: str = Header(None),
):
    # 1. Read the raw payload body
    payload = await request.body()

    # 2. Security: Validate signature only if ClickUp sends one
    if x_signature:
        computed_signature = hmac.new(
            CLICKUP_WEBHOOK_SECRET.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(computed_signature, x_signature):
            raise HTTPException(status_code=403, detail="Invalid cryptographic signature")
    else:
        print("⚠️  No X-Signature header — webhook secret not configured in ClickUp")

    # 3. Parse JSON data
    data = await request.json()

    # 4. Push to Redis queue via Celery — non-blocking
    clickup_task_id = data.get("task_id")
    process_clickup_task.delay(clickup_task_id)

    # 5. Immediate response back to ClickUp
    return {"status": "accepted", "message": "Task queued for processing"}
