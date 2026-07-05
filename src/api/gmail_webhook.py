import base64
import json
from fastapi import APIRouter, Request, BackgroundTasks
from src.db.client import supabase_client
from src.tools.google_auth import get_valid_credentials
from src.tools.gmail_api import get_gmail_service
from src.services.ingestion import process_new_email
from src.core.logging import logger

router = APIRouter()

@router.post("/gmail")
async def gmail_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives push notifications from Gmail via Google Cloud Pub/Sub."""
    try:
        data = await request.json()
        
        # Pub/Sub wraps the message in a 'message' object with base64 encoded 'data'
        if 'message' in data and 'data' in data['message']:
            message_data = data['message']['data']
            decoded_data = json.loads(base64.b64decode(message_data).decode('utf-8'))
            
            email_address = decoded_data.get('emailAddress')
            logger.info(f"📩 Received Gmail push notification for: {email_address}")
            
            # Run processing in the background so we can return 200 OK to Pub/Sub immediately
            background_tasks.add_task(process_gmail_push, email_address)
            
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Gmail webhook error: {e}")
        return {"status": "error", "message": str(e)}

async def process_gmail_push(email_address: str):
    """Background task to fetch and process the new email."""
    try:
        # 1. Find user by email
        response = supabase_client.table('users').select('id').eq('email', email_address).execute()
        if not response.data:
            logger.warning(f"User not found for email: {email_address}")
            return
            
        user_uuid = response.data[0]['id']
        creds = get_valid_credentials(user_uuid)
        if not creds:
            logger.error(f"Could not get credentials for {email_address}")
            return
            
        # 2. Fetch the absolute latest email from the inbox
        service = get_gmail_service(creds)
        results = service.users().messages().list(userId='me', maxResults=1, labelIds=['INBOX']).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return
            
        msg = messages[0]
        
        # Check if already exists in DB
        existing = supabase_client.table('emails').select('id').eq('message_id', msg['id']).execute()
        if existing.data:
            logger.info(f"Email {msg['id']} already exists. Skipping.")
            return
            
        # Fetch full details
        msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        headers = msg_data['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), 'No Subject')
        sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), 'Unknown Sender')
        
        body_text = ""
        if 'parts' in msg_data['payload']:
            for part in msg_data['payload']['parts']:
                if part['mimeType'] == 'text/plain':
                    body_text = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                    break
        elif 'data' in msg_data['payload'].get('body', {}):
            body_text = base64.urlsafe_b64decode(msg_data['payload']['body']['data']).decode('utf-8')
            
        email_data = {
            'id': msg['id'],
            'threadId': msg_data.get('threadId'),
            'subject': subject,
            'sender': sender,
            'snippet': msg_data.get('snippet', ''),
            'body_text': body_text
        }
        
        # 3. Process and save to DB
        await process_new_email(user_uuid, creds, email_data)
        logger.info(f"✅ Successfully processed new email via push: {subject}")
        
    except Exception as e:
        logger.error(f"Error processing push email: {e}")