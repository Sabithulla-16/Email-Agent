import base64
from email.message import EmailMessage
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from src.core.logging import logger

def get_gmail_service(creds: Credentials):
    """Builds the Gmail API service."""
    return build('gmail', 'v1', credentials=creds)

def fetch_latest_emails(creds: Credentials, max_results: int = 10):
    """Fetches the latest emails from the user's inbox, sorted by date."""
    try:
        service = get_gmail_service(creds)
        results = service.users().messages().list(userId='me', maxResults=max_results).execute()
        messages = results.get('messages', [])
        
        email_details = []
        for msg in messages:
            # Fetch full details for each message
            msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
            
            # Extract headers
            headers = msg_data['payload']['headers']
            subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), 'No Subject')
            sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), 'Unknown Sender')
            
            # Extract snippet (short preview of the email)
            snippet = msg_data.get('snippet', '')
            
            # Extract internalDate (milliseconds since epoch) for sorting
            internal_date = int(msg_data.get('internalDate', 0))
            
            # Convert milliseconds to a readable format (e.g., "Jul 04, 14:30")
            readable_date = ""
            if internal_date > 0:
                readable_date = datetime.fromtimestamp(internal_date / 1000).strftime('%b %d, %H:%M')
            
            email_details.append({
                'id': msg['id'],
                'threadId': msg_data.get('threadId'),
                'subject': subject,
                'sender': sender,
                'snippet': snippet,
                'internalDate': internal_date,
                'readable_date': readable_date
            })
            
        email_details.sort(key=lambda x: x['internalDate'], reverse=True)
        
        return email_details
    except Exception as e:
        logger.error(f"❌ Error fetching emails: {e}")
        return []
        
def fetch_latest_emails(creds: Credentials, max_results: int = 10):
    """Fetches the latest emails from the user's inbox, sorted by date."""
    try:
        service = get_gmail_service(creds)
        
        # 🔥 FIX: Add query to fetch only from INBOX and recent emails
        # This ensures we get the newest emails from the inbox
        results = service.users().messages().list(
            userId='me',
            maxResults=max_results,
            labelIds=['INBOX'],  # Only fetch from inbox
            q='newer_than:1d'    # Only emails from the last 24 hours
        ).execute()
        
        messages = results.get('messages', [])
        
        email_details = []
        for msg in messages:
            # Fetch full details for each message
            msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
            
            # Extract headers
            headers = msg_data['payload']['headers']
            subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), 'No Subject')
            sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), 'Unknown Sender')
            
            # Extract snippet (short preview of the email)
            snippet = msg_data.get('snippet', '')
            
            # Extract internalDate (milliseconds since epoch) for sorting
            internal_date = int(msg_data.get('internalDate', 0))
            
            # Convert milliseconds to a readable format (e.g., "Jul 04, 14:30")
            readable_date = ""
            if internal_date > 0:
                readable_date = datetime.fromtimestamp(internal_date / 1000).strftime('%b %d, %H:%M')
            
            email_details.append({
                'id': msg['id'],
                'threadId': msg_data.get('threadId'),
                'subject': subject,
                'sender': sender,
                'snippet': snippet,
                'internalDate': internal_date,
                'readable_date': readable_date
            })
            
        # 🌟 SORT THE LIST: Sort by internalDate in descending order (newest first)
        email_details.sort(key=lambda x: x['internalDate'], reverse=True)
        
        return email_details
    except Exception as e:
        logger.error(f"❌ Error fetching emails: {e}")
        return []

def send_email(creds: Credentials, to: str, subject: str, body: str):
    """Sends an email via Gmail API."""
    try:
        service = get_gmail_service(creds)
        
        message = EmailMessage()
        message.set_content(body)
        message['To'] = to
        message['From'] = 'me'
        message['Subject'] = subject

        # Encode the message in base64
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': encoded_message}

        # Send the email
        sent_message = service.users().messages().send(userId='me', body=create_message).execute()
        logger.info(f"✅ Email sent successfully! Message Id: {sent_message['id']}")
        return sent_message['id']
    except Exception as e:
        logger.error(f"❌ Error sending email: {e}")
        return None

def archive_thread(creds: Credentials, thread_id: str) -> bool:
    """Removes the INBOX label from a Gmail thread, effectively archiving it."""
    try:
        service = get_gmail_service(creds)
        
        # Modify the thread to remove the INBOX label
        service.users().threads().modify(
            userId='me',
            id=thread_id,
            body={'removeLabelIds': ['INBOX']}
        ).execute()
        
        logger.info(f"🗄️ Successfully archived thread: {thread_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to archive thread {thread_id}: {e}")
        return False