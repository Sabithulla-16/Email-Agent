from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from datetime import datetime, timezone
from src.core.logging import logger

def check_vacation_mode(creds: Credentials) -> dict:
    """
    Checks if the user has any "Out of Office" events in their calendar.
    Returns vacation status and return date if active.
    """
    try:
        service = build('calendar', 'v3', credentials=creds)
        
        # Get current time
        now = datetime.now(timezone.utc)
        
        # Fetch events for the next 30 days
        events_result = service.events().list(
            calendarId='primary',
            timeMin=now.isoformat(),
            maxResults=50,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        # Look for Out of Office events
        for event in events:
            summary = event.get('summary', '').lower()
            
            # Check for common vacation/OOO keywords
            ooo_keywords = ['out of office', 'ooo', 'vacation', 'holiday', 'leave', 'away', 'traveling', 'travelling']
            
            if any(keyword in summary for keyword in ooo_keywords):
                # Extract return date from event end time
                end_time = event.get('end', {}).get('dateTime') or event.get('end', {}).get('date')
                
                if end_time:
                    try:
                        # Parse the end date
                        if 'T' in end_time:
                            return_date = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                        else:
                            return_date = datetime.strptime(end_time, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                        
                        # Check if we're currently within the vacation period
                        start_time = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
                        if start_time:
                            if 'T' in start_time:
                                start_date = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                            else:
                                start_date = datetime.strptime(start_time, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                            
                            # If current time is between start and end, vacation mode is active
                            if start_date <= now <= return_date:
                                logger.info(f"🏖️ Vacation mode detected! User is away until {return_date.strftime('%Y-%m-%d')}")
                                return {
                                    'is_vacation': True,
                                    'return_date': return_date.strftime('%Y-%m-%d'),
                                    'event_summary': event.get('summary', 'Out of Office')
                                }
                    except Exception as e:
                        logger.error(f"Failed to parse vacation dates: {e}")
                        continue
        
        logger.info("✅ No active vacation mode detected")
        return {'is_vacation': False, 'return_date': None, 'event_summary': None}
        
    except Exception as e:
        logger.error(f"Failed to check vacation mode: {e}")
        return {'is_vacation': False, 'return_date': None, 'event_summary': None}