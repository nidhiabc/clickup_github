# Celery app is now defined in tasks.py
# Import from there to avoid circular imports
from tasks import celery_app
