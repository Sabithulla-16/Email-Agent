import json
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.core.config import settings
from src.core.logging import logger
from src.db.client import supabase_client
from src.tools.browser_agent import (
    open_form_page, extract_form_fields, fill_field, 
    click_submit_button, close_browser, navigate_to_next_page
)

groq_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.0,
    groq_api_key=settings.GROQ_API_KEY
)

# In-memory store for pending form sessions
pending_form_sessions: dict = {}

async def analyze_and_fill_form(form_url: str, user_uuid: str, reg_id: str) -> dict:
    """
    Main orchestrator for multi-page forms:
    1. Opens form
    2. Extracts fields from all pages
    3. Fills what it can from profile
    4. Navigates through pages
    5. Returns summary
    """
    try:
        # 1. Get user profile
        profile_response = supabase_client.table('user_profiles').select('*').eq('user_id', user_uuid).execute()
        if not profile_response.data:
            return {'success': False, 'error': 'No profile set. Use /profile command first.'}
        
        profile = profile_response.data[0]
        
        # 2. Open the form page
        page, html_content = await open_form_page(form_url)
        
        # 3. Extract fields from current page
        fields = await extract_form_fields(page)
        if not fields:
            await close_browser()
            return {'success': False, 'error': 'No form fields detected on this page.'}
        
        # 4. Check for file upload fields
        file_fields = [f for f in fields if f.get('type') == 'file']
        if file_fields:
            logger.warning(f"⚠️ Form has {len(file_fields)} file upload field(s). These require manual upload.")
        
        # 5. Use AI to match fields with profile data
        fields_json = json.dumps([{
            'label': f['clean_label'],
            'type': f['type'],
            'options': f.get('options', [])
        } for f in fields], indent=2)
        
        profile_json = json.dumps({
            k: v for k, v in profile.items() 
            if v and k not in ['id', 'user_id', 'updated_at']
        }, indent=2)
        
        matching_prompt = ChatPromptTemplate.from_template(
            """You are a form-filling AI. Match form fields with the user's profile data.
            
            USER PROFILE:
            {profile}
            
            FORM FIELDS:
            {fields}
            
            For each form field, determine:
            1. What the field is asking for
            2. Which profile field matches it best (use fuzzy matching for variations like "mobile number" vs "phone")
            3. The value to fill
            
            For radio/select fields, choose the best matching option from the available options.
            
            Return a JSON object with:
            {{
                "matched_fields": [
                    {{
                        "field_label": "the exact label from the form",
                        "profile_key": "which profile field to use",
                        "value": "the actual value to fill",
                        "field_type": "text/radio/select/checkbox"
                    }}
                ],
                "unmatched_fields": ["list of field labels that don't match any profile data"]
            }}
            
            Return ONLY valid JSON. No markdown."""
        )
        
        chain = matching_prompt | groq_llm
        result = chain.invoke({"profile": profile_json, "fields": fields_json})
        
        try:
            match_data = json.loads(result.content.replace('```json', '').replace('```', '').strip())
        except:
            await close_browser()
            return {'success': False, 'error': 'AI failed to parse form fields.'}
        
        # 6. Fill the form fields on current page
        filled_fields = {}
        file_upload_fields = []
        
        for match in match_data.get('matched_fields', []):
            field_label = match.get('field_label')
            value = match.get('value', '')
            field_type = match.get('field_type', 'text')
            
            # Skip file upload fields
            if field_type == 'file':
                file_upload_fields.append(field_label)
                continue
            
            if value:
                # Find the field definition
                field_def = next((f for f in fields if f.get('clean_label') == field_label), None)
                if field_def:
                    success = await fill_field(page, field_def, value)
                    if success:
                        filled_fields[field_label] = value
        
        # 7. Check if there are more pages
        has_next_page = await navigate_to_next_page(page)
        
        # 8. If there are more pages, recursively process them
        all_filled_fields = filled_fields.copy()
        all_unmatched = match_data.get('unmatched_fields', [])
        
        if has_next_page:
            logger.info("📄 Multi-page form detected. Processing next page...")
            # For now, we'll just note that there are more pages
            # In a full implementation, you'd recursively call analyze_and_fill_form
        
        # 9. Close browser
        await close_browser()
        
        # 10. Store session data
        pending_form_sessions[reg_id] = {
            'form_url': form_url,
            'user_uuid': user_uuid,
            'fields': fields,
            'filled_fields': all_filled_fields,
            'unmatched_fields': all_unmatched,
            'file_upload_fields': file_upload_fields,
            'has_more_pages': has_next_page,
            'status': 'awaiting_user_input' if all_unmatched or file_upload_fields else 'ready_to_submit'
        }
        
        return {
            'success': True,
            'filled_fields': all_filled_fields,
            'all_fields': fields,
            'unmatched_fields': all_unmatched,
            'file_upload_fields': file_upload_fields,
            'has_more_pages': has_next_page,
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
        if not session['unmatched_fields'] and not session.get('file_upload_fields'):
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
        
        # Reopen browser
        page, html_content = await open_form_page(session['form_url'])
        
        # Fill all saved fields
        for field_label, value in session['filled_fields'].items():
            field_def = next((f for f in session['fields'] if f.get('clean_label') == field_label), None)
            if field_def:
                await fill_field(page, field_def, value)
        
        # Navigate through all pages if multi-page form
        max_pages = 10  # Safety limit
        page_count = 1
        
        for _ in range(max_pages):
            has_next = await navigate_to_next_page(page)
            if not has_next:
                break
            page_count += 1
        
        if page_count > 1:
            logger.info(f"📄 Navigated through {page_count} pages")
        
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