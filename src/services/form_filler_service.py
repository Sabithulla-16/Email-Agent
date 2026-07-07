import asyncio
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

async def analyze_and_fill_form(form_url: str, user_uuid: str, reg_id: str) -> dict:
    """
    Main orchestrator: Opens form, detects fields, fills them, returns summary.
    """
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
            1. What the field is asking for (based on label, name, placeholder)
            2. Which profile field matches it best
            3. The value to fill
            
            Return a JSON object with:
            {{
                "matched_fields": [
                    {{
                        "field_name": "name attribute of form field",
                        "field_label": "human readable label",
                        "profile_key": "which profile field to use",
                        "value": "the actual value to fill"
                    }}
                ],
                "unmatched_fields": ["list of field labels that don't match any profile data"]
            }}
            
            Be smart about matching. For example:
            - "Full Name" → full_name
            - "Email" → email
            - "Phone" → phone
            - "GitHub" → github_link
            - "College/University" → college_name
            
            Return ONLY valid JSON. No markdown."""
        )
        
        chain = matching_prompt | groq_llm
        result = chain.invoke({"profile": profile_json, "fields": fields_json})
        
        try:
            match_data = json.loads(result.content.replace('```json', '').replace('```', '').strip())
        except:
            return {'success': False, 'error': 'AI failed to parse form fields.'}
        
        # 5. Fill the form fields
        filled_fields = {}
        for match in match_data.get('matched_fields', []):
            field_name = match.get('field_name')
            value = match.get('value', '')
            
            if value:
                # Find the original field definition
                field_def = next((f for f in fields if f['name'] == field_name or f['id'] == field_name), None)
                if field_def:
                    success = await fill_field(page, field_def, value)
                    if success:
                        filled_fields[match.get('field_label', field_name)] = value
        
        # 6. Save to database
        supabase_client.table('registrations').update({
            'filled_fields': filled_fields,
            'status': 'Awaiting Approval'
        }).eq('id', reg_id).execute()
        
        return {
            'success': True,
            'filled_fields': filled_fields,
            'unmatched_fields': match_data.get('unmatched_fields', []),
            'reg_id': reg_id
        }
        
    except Exception as e:
        logger.error(f"Form filling failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        # Don't close browser yet - we need it for submission
        pass

async def submit_form(reg_id: str) -> dict:
    """Submits the form after user approval."""
    try:
        # Get registration data
        reg_response = supabase_client.table('registrations').select('*').eq('id', reg_id).execute()
        if not reg_response.data:
            return {'success': False, 'error': 'Registration not found.'}
        
        reg = reg_response.data[0]
        form_url = reg['form_url']
        
        # Re-open the form and fill again (browser state is lost)
        page, html_content = await open_form_page(form_url)
        fields = await extract_form_fields(page)
        
        # Fill all saved fields
        for field_label, value in reg['filled_fields'].items():
            # Find matching field
            for field in fields:
                if field.get('label') == field_label or field.get('name') == field_label:
                    await fill_field(page, field, value)
                    break
        
        # Click submit
        submitted = await click_submit_button(page)
        
        if submitted:
            supabase_client.table('registrations').update({
                'status': 'Submitted'
            }).eq('id', reg_id).execute()
            return {'success': True, 'message': 'Form submitted successfully!'}
        else:
            return {'success': False, 'error': 'Could not find submit button.'}
            
    except Exception as e:
        logger.error(f"Form submission failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        await close_browser()

async def cancel_form(reg_id: str):
    """Cancels the form filling process."""
    supabase_client.table('registrations').update({
        'status': 'Cancelled'
    }).eq('id', reg_id).execute()
    await close_browser()

async def edit_field(reg_id: str, field_label: str, new_value: str) -> dict:
    """Edits a single field in the registration."""
    try:
        reg_response = supabase_client.table('registrations').select('filled_fields').eq('id', reg_id).execute()
        if not reg_response.data:
            return {'success': False, 'error': 'Registration not found.'}
        
        filled_fields = reg_response.data[0]['filled_fields']
        filled_fields[field_label] = new_value
        
        supabase_client.table('registrations').update({
            'filled_fields': filled_fields
        }).eq('id', reg_id).execute()
        
        return {'success': True, 'filled_fields': filled_fields}
    except Exception as e:
        return {'success': False, 'error': str(e)}