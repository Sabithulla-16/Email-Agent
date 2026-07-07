import json
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.core.config import settings
from src.core.logging import logger
from src.db.client import supabase_client
from src.tools.browser_agent import (
    open_form_page, extract_form_fields, fill_field, 
    click_submit_button, close_browser
)

groq_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.0,
    groq_api_key=settings.GROQ_API_KEY
)

# 🔥 In-memory store for pending form sessions
pending_form_sessions: dict = {}

async def analyze_and_fill_form(form_url: str, user_uuid: str, reg_id: str) -> dict:
    """Main orchestrator: Opens form, detects fields, fills what it can, returns summary."""
    try:
        # 1. Get user profile
        profile_response = supabase_client.table('user_profiles').select('*').eq('user_id', user_uuid).execute()
        if not profile_response.data:
            return {'success': False, 'error': 'No profile set. Use /profile command first.'}
        
        profile = profile_response.data[0]
        
        # 2. Open the form page
        page, html_content = await open_form_page(form_url)
        
        # 3. Extract form fields
        fields = await extract_form_fields(page)
        if not fields:
            await close_browser()
            return {'success': False, 'error': 'No form fields detected on this page.'}
        
        # 4. Use AI to match fields with profile data
        fields_json = json.dumps(fields, indent=2)
        profile_json = json.dumps({k: v for k, v in profile.items() if v and k not in ['id', 'user_id', 'updated_at']}, indent=2)
        
        matching_prompt = ChatPromptTemplate.from_template(
            """You are a form-filling AI. Match form fields with the user's profile data.
            
            USER PROFILE:
            {profile}
            
            FORM FIELDS:
            {fields}
            
            For each form field, determine:
            1. What the field is asking for
            2. Which profile field matches it best
            3. The value to fill
            
            Return a JSON object with:
            {{
                "matched_fields": [
                    {{
                        "field_label": "the exact label from the form",
                        "profile_key": "which profile field to use",
                        "value": "the actual value to fill"
                    }}
                ],
                "unmatched_fields": ["list of field labels that don't match any profile data"]
            }}
            
            Be smart about matching. Return ONLY valid JSON. No markdown."""
        )
        
        chain = matching_prompt | groq_llm
        result = chain.invoke({"profile": profile_json, "fields": fields_json})
        
        try:
            match_data = json.loads(result.content.replace('```json', '').replace('```', '').strip())
        except:
            await close_browser()
            return {'success': False, 'error': 'AI failed to parse form fields.'}
        
        # 5. Fill the form fields
        filled_fields = {}
        for match in match_data.get('matched_fields', []):
            field_label = match.get('field_label')
            value = match.get('value', '')
            
            if value:
                # Find the field definition
                field_def = next((f for f in fields if f.get('label') == field_label), None)
                if field_def:
                    success = await fill_field(page, field_def, value)
                    if success:
                        filled_fields[field_label] = value
        
        # 6. Close browser
        await close_browser()
        
        # 7. Store session data
        unmatched_fields = match_data.get('unmatched_fields', [])
        pending_form_sessions[reg_id] = {
            'form_url': form_url,
            'user_uuid': user_uuid,
            'fields': fields,
            'filled_fields': filled_fields,
            'unmatched_fields': unmatched_fields,
            'status': 'awaiting_user_input' if unmatched_fields else 'ready_to_submit'
        }
        
        return {
            'success': True,
            'filled_fields': filled_fields,
            'all_fields': fields,  # 🔥 Return all fields for summary
            'unmatched_fields': unmatched_fields,
            'reg_id': reg_id
        }
        
    except Exception as e:
        logger.error(f"Form filling failed: {e}")
        await close_browser()
        return {'success': False, 'error': str(e)}

async def fill_additional_fields(reg_id: str, user_responses: dict) -> dict:
    """Fills additional fields provided by the user."""
    try:
        session = pending_form_sessions.get(reg_id)
        if not session:
            return {'success': False, 'error': 'Form session not found.'}
        
        # Update filled fields with user responses
        session['filled_fields'].update(user_responses)
        
        # Remove from unmatched list
        for field_label in user_responses.keys():
            if field_label in session['unmatched_fields']:
                session['unmatched_fields'].remove(field_label)
        
        # Update session status
        if not session['unmatched_fields']:
            session['status'] = 'ready_to_submit'
        
        return {
            'success': True,
            'filled_fields': session['filled_fields'],
            'unmatched_fields': session['unmatched_fields']
        }
        
    except Exception as e:
        logger.error(f"Failed to fill additional fields: {e}")
        return {'success': False, 'error': str(e)}

async def submit_form(reg_id: str) -> dict:
    """Submits the form after user approval."""
    try:
        session = pending_form_sessions.get(reg_id)
        if not session:
            return {'success': False, 'error': 'Form session not found.'}
        
        # Reopen browser and fill all fields
        page, html_content = await open_form_page(session['form_url'])
        
        for field_label, value in session['filled_fields'].items():
            field_def = next((f for f in session['fields'] if f.get('label') == field_label or f.get('name') == field_label), None)
            if field_def:
                await fill_field(page, field_def, value)
        
        # Click submit
        submitted = await click_submit_button(page)
        
        if submitted:
            supabase_client.table('registrations').update({
                'status': 'Submitted',
                'filled_fields': session['filled_fields']
            }).eq('id', reg_id).execute()
            
            # Clean up session
            pending_form_sessions.pop(reg_id, None)
            
            return {'success': True, 'message': 'Form submitted successfully!'}
        else:
            return {'success': False, 'error': 'Could not find submit button.'}
            
    except Exception as e:
        logger.error(f"Form submission failed: {e}")
        await close_browser()
        return {'success': False, 'error': str(e)}

async def cancel_form(reg_id: str):
    """Cancels the form filling process."""
    supabase_client.table('registrations').update({
        'status': 'Cancelled'
    }).eq('id', reg_id).execute()
    pending_form_sessions.pop(reg_id, None)
    await close_browser()