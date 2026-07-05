import re
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from src.core.logging import logger
from datetime import datetime

def get_tasks_service(creds: Credentials):
    return build('tasks', 'v1', credentials=creds)

def create_task(creds: Credentials, title: str, notes: str = "", due_date: str = None):
    """Creates a Google Task with strict validation for due dates."""
    
    # 1. Validate title (Google Tasks requires a non-empty title)
    if not title or not title.strip():
        logger.warning("Skipping task creation: Title is empty.")
        return None

    try:
        service = get_tasks_service(creds)
        
        task = {
            'title': title[:1000], # Google Tasks has a title character limit
            'notes': notes
        }
        
        # 2. Validate and format due_date for Google Tasks API (must be RFC3339)
        if due_date:
            clean_date = due_date.strip()
            # If it's just a date like "2026-07-10", convert it to RFC3339
            if re.match(r'^\d{4}-\d{2}-\d{2}$', clean_date):
                task['due'] = f"{clean_date}T23:59:59Z"
            # If it already looks like a full ISO string with T and Z, use it
            elif 'T' in clean_date and 'Z' in clean_date:
                task['due'] = clean_date
            # Otherwise, ignore the due_date to prevent 400 Bad Request errors
            else:
                logger.warning(f"Ignoring invalid due_date format for task '{title}': {due_date}")
            
        result = service.tasks().insert(tasklist='@default', body=task).execute()
        logger.info(f"✅ Task created: {result.get('title')}")
        return result.get('id')
    except Exception as e:
        logger.error(f"❌ Error creating task: {e}")
        return None


def get_pending_tasks(creds: Credentials):
    """Fetches pending (incomplete) tasks from Google Tasks."""
    try:
        service = get_tasks_service(creds)
        
        # Fetch tasks from the default task list
        results = service.tasks().list(
            tasklist='@default',
            maxResults=10,
            showCompleted=False,
            showHidden=False
        ).execute()
        
        tasks = results.get('items', [])
        
        formatted_tasks = []
        for task in tasks:
            due_date = ""
            if task.get('due'):
                due_date = datetime.fromisoformat(task['due'].replace('Z', '+00:00')).strftime('%b %d, %H:%M')
            
            formatted_tasks.append({
                'title': task.get('title', 'No Title'),
                'notes': task.get('notes', ''),
                'due_date': due_date
            })
        
        return formatted_tasks
    except Exception as e:
        logger.error(f"❌ Error fetching tasks: {e}")
        return []