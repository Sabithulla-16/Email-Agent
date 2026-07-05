from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from src.core.config import settings
from src.db.client import supabase_client
from src.core.logging import logger

# The scopes define what permissions we are asking the user for
SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify', # Read/Send/Manage Emails
    'https://www.googleapis.com/auth/calendar',     # Read/Create Calendar Events
    'https://www.googleapis.com/auth/tasks',         # Read/Create Tasks
    'https://www.googleapis.com/auth/drive.readonly' # Read Attachments
]

def get_auth_url(telegram_id: str) -> str:
    """Generates the Google OAuth login URL to send to the user."""
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.GOOGLE_REDIRECT_URI]
            }
        },
        scopes=SCOPES,
        redirect_uri=settings.GOOGLE_REDIRECT_URI
    )
    
    # Pass the telegram_id as the 'state' parameter so we know who is logging in
    authorization_url, state = flow.authorization_url(
        access_type='offline', 
        include_granted_scopes='true',
        state=telegram_id,
        prompt='consent'
    )
    return authorization_url

def exchange_code_for_tokens(code: str, user_id: str) -> bool:
    """Exchanges the auth code for tokens, saves them, and sets up Gmail push."""
    try:
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [settings.GOOGLE_REDIRECT_URI]
                }
            },
            scopes=SCOPES,
            redirect_uri=settings.GOOGLE_REDIRECT_URI
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        # 1. Get the user's email address AND NAME
        service = build('gmail', 'v1', credentials=creds)
        profile = service.users().getProfile(userId='me').execute()
        user_email = profile.get('emailAddress')
        
        # 🔥 Extract name from email or Google profile
        # Google People API would be better, but we can extract from email as fallback
        user_name = profile.get('displayName', '')
        if not user_name:
            # Fallback: extract name from email (e.g., "sabithullasharieff16@gmail.com" -> "Sabithulla")
            user_name = user_email.split('@')[0].replace('.', ' ').title()
        
        logger.info(f"Retrieved profile for user: {user_email}, Name: {user_name}")

        # 2. Save tokens, email, AND NAME to Supabase
        supabase_client.table('users').update({
            'google_access_token': creds.token,
            'google_refresh_token': creds.refresh_token,
            'email': user_email,
            'full_name': user_name  # 🔥 ADD THIS
        }).eq('id', user_id).execute()
        
        # 3. Setup Gmail Push Notification (Watch)
        try:
            request = {
                'labelIds': ['INBOX'],
                'topicName': settings.GMAIL_PUBSUB_TOPIC
            }
            service.users().watch(userId='me', body=request).execute()
            logger.info(f"✅ Gmail watch setup successfully for {user_email}")
        except Exception as watch_err:
            logger.error(f"Failed to setup Gmail watch: {watch_err}")
        
        logger.info(f"✅ Successfully saved Google tokens for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to exchange code for tokens: {e}")
        return False

def get_valid_credentials(user_id: str) -> Credentials | None:
    """Fetches tokens from Supabase and refreshes them if expired."""
    response = supabase_client.table('users').select('google_access_token, google_refresh_token').eq('id', user_id).execute()
    
    if not response.data:
        return None
        
    user_data = response.data[0]
    access_token = user_data.get('google_access_token')
    refresh_token = user_data.get('google_refresh_token')

    if not access_token or not refresh_token:
        return None

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES
    )

    # The google-auth library automatically handles refreshing if the token is expired
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        # Update the new access token in Supabase
        supabase_client.table('users').update({
            'google_access_token': creds.token
        }).eq('id', user_id).execute()
        logger.info("🔄 Refreshed expired Google access token.")

    return creds

def exchange_code_for_tokens(code: str, user_id: str) -> bool:
    """Exchanges the auth code for tokens, saves them, and sets up Gmail push."""
    try:
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [settings.GOOGLE_REDIRECT_URI]
                }
            },
            scopes=SCOPES,
            redirect_uri=settings.GOOGLE_REDIRECT_URI
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        # 1. Get the user's email address AND NAME from Google
        service = build('gmail', 'v1', credentials=creds)
        profile = service.users().getProfile(userId='me').execute()
        user_email = profile.get('emailAddress')
        
        # 🔥 Extract name - try displayName first, fallback to email parsing
        user_name = profile.get('displayName', '').strip()
        if not user_name:
            # Fallback: extract from email (e.g., "valtryfreefire@gmail.com" -> "Valtryfreefire")
            user_name = user_email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
        
        logger.info(f"Retrieved profile: {user_email}, Name: {user_name}")

        # 2. Save tokens, email, AND NAME to Supabase
        supabase_client.table('users').update({
            'google_access_token': creds.token,
            'google_refresh_token': creds.refresh_token,
            'email': user_email,
            'full_name': user_name  # 🔥 Save the name
        }).eq('id', user_id).execute()
        
        # 3. Setup Gmail Push Notification (Watch)
        try:
            request = {
                'labelIds': ['INBOX'],
                'topicName': settings.GMAIL_PUBSUB_TOPIC
            }
            service.users().watch(userId='me', body=request).execute()
            logger.info(f"✅ Gmail watch setup successfully for {user_email}")
        except Exception as watch_err:
            logger.error(f"Failed to setup Gmail watch: {watch_err}")
        
        logger.info(f"✅ Successfully saved Google tokens and name for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to exchange code for tokens: {e}")
        return False