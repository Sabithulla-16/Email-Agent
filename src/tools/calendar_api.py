from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from src.core.logging import logger
from datetime import datetime, timezone, timedelta

def get_calendar_service(creds: Credentials):
    return build('calendar', 'v3', credentials=creds)

def create_calendar_event(creds: Credentials, summary: str, start_time: str, end_time: str, description: str = ""):
    """
    Creates a Google Calendar event.
    start_time and end_time should be in RFC3339 format (e.g., '2026-07-05T10:00:00-07:00')
    """
    try:
        service = get_calendar_service(creds)
        event = {
            'summary': summary,
            'description': description,
            'start': {'dateTime': start_time, 'timeZone': 'UTC'},
            'end': {'dateTime': end_time, 'timeZone': 'UTC'},
        }
        
        created_event = service.events().insert(calendarId='primary', body=event).execute()
        logger.info(f"✅ Calendar event created: {created_event.get('htmlLink')}")
        return created_event.get('id')
    except Exception as e:
        logger.error(f"❌ Error creating calendar event: {e}")
        return None

def get_todays_events(creds: Credentials):
    """Fetches today's events from Google Calendar."""
    try:
        service = get_calendar_service(creds)
        
        # Get today's date in RFC3339 format
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
        
        # Fetch events for today
        events_result = service.events().list(
            calendarId='primary',
            timeMin=today_start,
            timeMax=today_end,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        formatted_events = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))
            
            # Format time for display
            start_time = ""
            if 'T' in start:
                start_time = datetime.fromisoformat(start.replace('Z', '+00:00')).strftime('%H:%M')
            else:
                start_time = "All day"
            
            formatted_events.append({
                'summary': event.get('summary', 'No Title'),
                'start_time': start_time,
                'description': event.get('description', '')
            })
        
        return formatted_events
    except Exception as e:
        logger.error(f"❌ Error fetching calendar events: {e}")
        return []

def get_availability(creds: Credentials, start_date: str, days: int = 3) -> dict:
    """
    Checks calendar availability for the next X days.
    
    Args:
        creds: Google credentials
        start_date: ISO format date (e.g., "2026-07-05")
        days: Number of days to check (default 3)
    
    Returns:
        Dict with available time slots for each day
    """
    try:
        service = build('calendar', 'v3', credentials=creds)
        
        start_dt = datetime.fromisoformat(start_date)
        end_dt = start_dt + timedelta(days=days)
        
        # Get busy times
        body = {
            "timeMin": start_dt.isoformat() + "Z",
            "timeMax": end_dt.isoformat() + "Z",
            "items": [{"id": "primary"}]
        }
        
        freebusy_result = service.freebusy().query(body=body).execute()
        busy_times = freebusy_result.get('calendars', {}).get('primary', {}).get('busy', [])
        
        # Parse busy times into date-based structure
        availability = {}
        current_date = start_dt.date()
        end_date = end_dt.date()
        
        while current_date <= end_date:
            date_str = current_date.isoformat()
            
            # Define work hours (9 AM - 6 PM)
            work_start = datetime.combine(current_date, datetime.min.time().replace(hour=9))
            work_end = datetime.combine(current_date, datetime.min.time().replace(hour=18))
            
            # Find busy slots on this day
            day_busy = []
            for busy in busy_times:
                busy_start = datetime.fromisoformat(busy['start'].replace('Z', '+00:00'))
                busy_end = datetime.fromisoformat(busy['end'].replace('Z', '+00:00'))
                
                # Check if busy slot overlaps with work hours
                if busy_start.date() == current_date or busy_end.date() == current_date:
                    day_busy.append({
                        'start': busy_start.strftime('%H:%M'),
                        'end': busy_end.strftime('%H:%M')
                    })
            
            # Calculate free slots (simplified - just find gaps)
            free_slots = []
            if not day_busy:
                # No busy times, whole work day is free
                free_slots = ["9:00 AM - 6:00 PM"]
            else:
                # Find gaps between busy times
                sorted_busy = sorted(day_busy, key=lambda x: x['start'])
                last_end = "09:00"
                
                for busy in sorted_busy:
                    if busy['start'] > last_end:
                        free_slots.append(f"{last_end} - {busy['start']}")
                    last_end = max(last_end, busy['end'])
                
                if last_end < "18:00":
                    free_slots.append(f"{last_end} - 6:00 PM")
            
            availability[date_str] = {
                'free_slots': free_slots[:3],  # Limit to 3 slots per day
                'busy_count': len(day_busy)
            }
            
            current_date += timedelta(days=1)
        
        return availability
        
    except Exception as e:
        logger.error(f"Error checking availability: {e}")
        return {}