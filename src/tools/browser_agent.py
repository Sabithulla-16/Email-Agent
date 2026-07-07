from playwright.async_api import async_playwright, Page, Browser
from src.core.config import settings
from src.core.logging import logger
from typing import Dict, List, Optional
import re

# 🔥 Browserless.io connection URL
BROWSERLESS_URL = f"wss://chrome.browserless.io?token={settings.BROWSERLESS_API_KEY}"

_browser: Optional[Browser] = None
_playwright = None

# 🔥 FIELD MAPPING SYNONYMS - Maps variations to profile keys
FIELD_SYNONYMS = {
    # Name variations
    'full name': 'full_name',
    'your name': 'full_name',
    'name': 'full_name',
    'applicant name': 'full_name',
    'candidate name': 'full_name',
    
    # Email variations
    'email address': 'email',
    'email': 'email',
    'e-mail': 'email',
    'mail': 'email',
    
    # Phone variations
    'phone number': 'phone',
    'phone': 'phone',
    'mobile number': 'phone',
    'mobile': 'phone',
    'cell phone': 'phone',
    'contact number': 'phone',
    'telephone': 'phone',
    'tel': 'phone',
    
    # GitHub variations
    'github profile': 'github_link',
    'github': 'github_link',
    'github username': 'github_link',
    'github link': 'github_link',
    'github url': 'github_link',
    
    # LinkedIn variations
    'linkedin profile': 'linkedin_link',
    'linkedin': 'linkedin_link',
    'linkedin url': 'linkedin_link',
    'linkedin link': 'linkedin_link',
    
    # College variations
    'college/university': 'college_name',
    'college': 'college_name',
    'university': 'college_name',
    'school': 'college_name',
    'institution': 'college_name',
    'education': 'college_name',
    
    # Resume variations
    'resume': 'resume_link',
    'cv': 'resume_link',
    'portfolio': 'resume_link',
    'website': 'resume_link',
    
    # Team variations
    'team name': 'team_name',
    'team': 'team_name',
    'group name': 'team_name',
}

async def get_browser() -> Browser:
    """Connects to Browserless cloud browser."""
    global _browser, _playwright
    
    if _browser is None or not _browser.is_connected():
        if _playwright is None:
            _playwright = await async_playwright().start()
        
        _browser = await _playwright.chromium.connect_over_cdp(BROWSERLESS_URL)
        logger.info("🌐 Connected to Browserless cloud browser!")
        
    return _browser

async def close_browser():
    """Closes the browser connection."""
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
    logger.info("🌐 Disconnected from Browserless")

async def open_form_page(url: str) -> tuple[Page, str]:
    """Opens a URL and returns the page object and HTML content."""
    browser = await get_browser()
    
    context = await browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    )
    page = await context.new_page()
    
    try:
        logger.info(f"🌐 Opening form: {url}")
        await page.goto(url, wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(3000)  # Wait for JS to render
        
        html_content = await page.content()
        return page, html_content
    except Exception as e:
        logger.error(f"Failed to open page {url}: {e}")
        await page.close()
        raise

def clean_field_label(label: str) -> str:
    """Cleans field labels by removing Google Forms artifacts."""
    # Remove "*Your answer" suffix
    label = re.sub(r'\s*\*?Your answer\s*$', '', label, flags=re.IGNORECASE)
    # Remove asterisks
    label = label.replace('*', '')
    # Clean up extra spaces
    label = ' '.join(label.split())
    return label.strip()

def normalize_field_name(label: str) -> str:
    """Normalizes field label to match against synonyms."""
    cleaned = clean_field_label(label)
    return cleaned.lower().strip()

def map_field_to_profile(field_label: str) -> Optional[str]:
    """Maps a field label to a profile key using synonyms."""
    normalized = normalize_field_name(field_label)
    
    # Direct match
    if normalized in FIELD_SYNONYMS:
        return FIELD_SYNONYMS[normalized]
    
    # Partial match - check if any synonym is contained in the field label
    for synonym, profile_key in FIELD_SYNONYMS.items():
        if synonym in normalized or normalized in synonym:
            return profile_key
    
    return None

async def extract_form_fields(page: Page) -> List[Dict]:
    """
    Extracts form fields with support for:
    - Text inputs
    - Radio buttons
    - Dropdowns/selects
    - Checkboxes
    - File uploads
    - Multi-page forms
    """
    try:
        fields = await page.evaluate('''() => {
            const fields = [];
            
            // 1. Try Google Forms specific structure first
            const formItems = document.querySelectorAll(
                '.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]'
            );
            
            formItems.forEach((item, index) => {
                // Extract question text
                let label = '';
                const titleSelectors = [
                    '.freebirdFormviewerViewItemsItemItemTitle',
                    '.exportItemTitle',
                    '.qSFzN',
                    '.freebirdFormviewerViewItemsItemItemTitleContainer'
                ];
                
                for (const selector of titleSelectors) {
                    const titleEl = item.querySelector(selector);
                    if (titleEl) {
                        label = titleEl.textContent.trim();
                        break;
                    }
                }
                
                // Check for different field types
                
                // A. Text inputs (text, email, tel, url)
                const textInput = item.querySelector(
                    'input[type="text"], input[type="email"], input[type="tel"], input[type="url"]'
                );
                
                if (textInput && label) {
                    fields.push({
                        name: textInput.name || `gform_field_${index}`,
                        id: textInput.id || '',
                        type: 'text',
                        inputType: textInput.type,
                        label: label,
                        placeholder: textInput.placeholder || '',
                        ariaLabel: textInput.getAttribute('aria-label') || '',
                        required: textInput.hasAttribute('required') || textInput.getAttribute('aria-required') === 'true'
                    });
                    return;
                }
                
                // B. Textarea
                const textarea = item.querySelector('textarea');
                if (textarea && label) {
                    fields.push({
                        name: textarea.name || `gform_field_${index}`,
                        id: textarea.id || '',
                        type: 'textarea',
                        label: label,
                        placeholder: textarea.placeholder || '',
                        ariaLabel: textarea.getAttribute('aria-label') || '',
                        required: textarea.hasAttribute('required')
                    });
                    return;
                }
                
                // C. Radio buttons
                const radioButtons = item.querySelectorAll('input[type="radio"]');
                if (radioButtons.length > 0 && label) {
                    const options = [];
                    radioButtons.forEach(radio => {
                        const radioLabel = radio.parentElement.textContent.trim();
                        options.push(radioLabel);
                    });
                    
                    fields.push({
                        name: radioButtons[0].name || `gform_radio_${index}`,
                        id: radioButtons[0].id || '',
                        type: 'radio',
                        label: label,
                        options: options,
                        required: radioButtons[0].hasAttribute('required')
                    });
                    return;
                }
                
                // D. Dropdown/Select
                const select = item.querySelector('select');
                if (select && label) {
                    const options = [];
                    select.querySelectorAll('option').forEach(opt => {
                        if (opt.value) { // Skip empty options
                            options.push(opt.textContent.trim());
                        }
                    });
                    
                    fields.push({
                        name: select.name || `gform_select_${index}`,
                        id: select.id || '',
                        type: 'select',
                        label: label,
                        options: options,
                        required: select.hasAttribute('required')
                    });
                    return;
                }
                
                // E. Checkboxes (multiple choice)
                const checkboxes = item.querySelectorAll('input[type="checkbox"]');
                if (checkboxes.length > 0 && label) {
                    const options = [];
                    checkboxes.forEach(cb => {
                        const cbLabel = cb.parentElement.textContent.trim();
                        options.push(cbLabel);
                    });
                    
                    fields.push({
                        name: checkboxes[0].name || `gform_checkbox_${index}`,
                        id: checkboxes[0].id || '',
                        type: 'checkbox',
                        label: label,
                        options: options,
                        multiple: true
                    });
                    return;
                }
                
                // F. File upload
                const fileInput = item.querySelector('input[type="file"]');
                if (fileInput && label) {
                    fields.push({
                        name: fileInput.name || `gform_file_${index}`,
                        id: fileInput.id || '',
                        type: 'file',
                        label: label,
                        required: fileInput.hasAttribute('required'),
                        accept: fileInput.getAttribute('accept') || ''
                    });
                    return;
                }
            });
            
            // 2. Fallback: Standard HTML form extraction
            if (fields.length === 0) {
                const inputs = document.querySelectorAll(
                    'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], textarea, select, input[type="radio"], input[type="checkbox"], input[type="file"]'
                );
                
                inputs.forEach((input, index) => {
                    let label = input.getAttribute('aria-label') || input.placeholder || '';
                    
                    if (!label) {
                        const parent = input.closest('div, p, li');
                        if (parent) {
                            const labelEl = parent.querySelector('label');
                            if (labelEl) label = labelEl.textContent.trim();
                        }
                    }
                    
                    if (!label) label = `Field ${index + 1}`;
                    
                    const fieldInfo = {
                        name: input.name || `field_${index}`,
                        id: input.id || '',
                        type: input.type || input.tagName.toLowerCase(),
                        label: label,
                        placeholder: input.placeholder || '',
                        ariaLabel: input.getAttribute('aria-label') || '',
                        required: input.hasAttribute('required')
                    };
                    
                    // Add options for select/radio/checkbox
                    if (input.tagName.toLowerCase() === 'select') {
                        fieldInfo.options = Array.from(input.querySelectorAll('option'))
                            .filter(opt => opt.value)
                            .map(opt => opt.textContent.trim());
                    }
                    
                    fields.push(fieldInfo);
                });
            }
            
            return fields;
        }''')
        
        # Clean up labels
        for field in fields:
            field['clean_label'] = clean_field_label(field['label'])
            field['profile_key'] = map_field_to_profile(field['label'])
        
        logger.info(f"🔍 Found {len(fields)} form fields")
        for field in fields:
            logger.info(f"  - {field['clean_label']} ({field['type']}) -> {field['profile_key'] or 'No match'}")
        
        return fields
        
    except Exception as e:
        logger.error(f"JavaScript field extraction failed: {e}")
        return []

async def fill_field(page: Page, field: Dict, value: str) -> bool:
    """Fills a single form field with support for different field types."""
    try:
        field_type = field.get('type', 'text')
        
        if field_type == 'text' or field_type == 'textarea':
            return await fill_text_field(page, field, value)
        elif field_type == 'radio':
            return await fill_radio_field(page, field, value)
        elif field_type == 'select':
            return await fill_select_field(page, field, value)
        elif field_type == 'checkbox':
            return await fill_checkbox_field(page, field, value)
        elif field_type == 'file':
            logger.warning(f"⚠️ File upload field detected: {field['clean_label']}. File uploads require user interaction.")
            return False
        else:
            logger.warning(f"Unsupported field type: {field_type}")
            return False
            
    except Exception as e:
        logger.error(f"Failed to fill field {field.get('clean_label')}: {e}")
        return False

async def fill_text_field(page: Page, field: Dict, value: str) -> bool:
    """Fills text/textarea fields."""
    try:
        element = None
        
        # Try by ID
        if field.get('id'):
            element = page.locator(f"#{field['id']}").first
            if await element.count() == 0:
                element = None
        
        # Try by name
        if not element and field.get('name') and not field['name'].startswith(('gform_field', 'field_')):
            element = page.locator(f"[name='{field['name']}']").first
            if await element.count() == 0:
                element = None
        
        # Try by aria-label
        if not element and field.get('ariaLabel'):
            safe_aria = field['ariaLabel'].replace("'", "\\'")
            element = page.locator(f"input[aria-label*='{safe_aria}' i], textarea[aria-label*='{safe_aria}' i]").first
            if await element.count() == 0:
                element = None
        
        # Try by label text using JavaScript
        if not element and field.get('label'):
            clean_label = field['clean_label'].lower().replace("'", "\\'")
            selector = await page.evaluate(f"""
                () => {{
                    const titles = document.querySelectorAll(
                        '.freebirdFormviewerViewItemsItemItemTitle, .exportItemTitle, .qSFzN'
                    );
                    
                    for (const title of titles) {{
                        const titleText = title.textContent.toLowerCase();
                        if (titleText.includes('{clean_label}')) {{
                            const container = title.closest('.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]');
                            if (container) {{
                                const input = container.querySelector('input[type="text"], input[type="email"], textarea');
                                if (input) {{
                                    if (input.id) return '#' + input.id;
                                    if (input.name) return '[name="' + input.name + '"]';
                                    return 'found';
                                }}
                            }}
                        }}
                    }}
                    return null;
                }}
            """)
            
            if selector == 'found':
                await page.evaluate(f"""
                    () => {{
                        const titles = document.querySelectorAll(
                            '.freebirdFormviewerViewItemsItemItemTitle, .exportItemTitle, .qSFzN'
                        );
                        
                        for (const title of titles) {{
                            const titleText = title.textContent.toLowerCase();
                            if (titleText.includes('{clean_label}')) {{
                                const container = title.closest('.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]');
                                if (container) {{
                                    const input = container.querySelector('input[type="text"], input[type="email"], textarea');
                                    if (input) {{
                                        input.value = '{value}';
                                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    }}
                                }}
                            }}
                        }}
                    }}
                """)
                logger.info(f"✅ Filled field '{field['clean_label']}' with '{value}' via JS")
                return True
            elif selector:
                element = page.locator(selector).first
                if await element.count() == 0:
                    element = None
        
        if element and await element.count() > 0:
            await element.wait_for(state='visible', timeout=5000)
            await element.fill(value)
            logger.info(f"✅ Filled field '{field['clean_label']}' with '{value}'")
            return True
        else:
            logger.warning(f"❌ Field not found: {field['clean_label']}")
            return False
        
    except Exception as e:
        logger.error(f"Failed to fill text field: {e}")
        return False

async def fill_radio_field(page: Page, field: Dict, value: str) -> bool:
    """Fills radio button fields by finding matching option."""
    try:
        # Try to find radio button by value/text
        options = field.get('options', [])
        
        for i, option_text in enumerate(options):
            if value.lower() in option_text.lower() or option_text.lower() in value.lower():
                # Found matching option, click it
                selector = await page.evaluate(f"""
                    () => {{
                        const titles = document.querySelectorAll(
                            '.freebirdFormviewerViewItemsItemItemTitle, .exportItemTitle, .qSFzN'
                        );
                        
                        for (const title of titles) {{
                            if (title.textContent.toLowerCase().includes('{field['clean_label'].lower().replace("'", "\\'")}')) {{
                                const container = title.closest('.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]');
                                if (container) {{
                                    const radios = container.querySelectorAll('input[type="radio"]');
                                    if (radios[{i}]) {{
                                        if (radios[{i}].id) return '#' + radios[{i}].id;
                                        return 'found';
                                    }}
                                }}
                            }}
                        }}
                        return null;
                    }}
                """)
                
                if selector == 'found':
                    await page.evaluate(f"""
                        () => {{
                            const titles = document.querySelectorAll(
                                '.freebirdFormviewerViewItemsItemItemTitle, .exportItemTitle, .qSFzN'
                            );
                            
                            for (const title of titles) {{
                                if (title.textContent.toLowerCase().includes('{field['clean_label'].lower().replace("'", "\\'")}')) {{
                                    const container = title.closest('.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]');
                                    if (container) {{
                                        const radios = container.querySelectorAll('input[type="radio"]');
                                        if (radios[{i}]) {{
                                            radios[{i}].click();
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    """)
                    logger.info(f"✅ Selected radio option '{option_text}' for field '{field['clean_label']}'")
                    return True
                elif selector:
                    await page.locator(selector).first.click()
                    logger.info(f"✅ Selected radio option '{option_text}' for field '{field['clean_label']}'")
                    return True
        
        logger.warning(f"❌ Could not find matching radio option for value: {value}")
        return False
        
    except Exception as e:
        logger.error(f"Failed to fill radio field: {e}")
        return False

async def fill_select_field(page: Page, field: Dict, value: str) -> bool:
    """Fills dropdown/select fields."""
    try:
        options = field.get('options', [])
        
        for option_text in options:
            if value.lower() in option_text.lower() or option_text.lower() in value.lower():
                # Found matching option
                selector = await page.evaluate(f"""
                    () => {{
                        const titles = document.querySelectorAll(
                            '.freebirdFormviewerViewItemsItemItemTitle, .exportItemTitle, .qSFzN'
                        );
                        
                        for (const title of titles) {{
                            if (title.textContent.toLowerCase().includes('{field['clean_label'].lower().replace("'", "\\'")}')) {{
                                const container = title.closest('.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]');
                                if (container) {{
                                    const select = container.querySelector('select');
                                    if (select) {{
                                        if (select.id) return '#' + select.id;
                                        if (select.name) return '[name="' + select.name + '"]';
                                        return 'found';
                                    }}
                                }}
                            }}
                        }}
                        return null;
                    }}
                """)
                
                if selector == 'found':
                    await page.evaluate(f"""
                        () => {{
                            const titles = document.querySelectorAll(
                                '.freebirdFormviewerViewItemsItemItemTitle, .exportItemTitle, .qSFzN'
                            );
                            
                            for (const title of titles) {{
                                if (title.textContent.toLowerCase().includes('{field['clean_label'].lower().replace("'", "\\'")}')) {{
                                    const container = title.closest('.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]');
                                    if (container) {{
                                        const select = container.querySelector('select');
                                        if (select) {{
                                            for (let i = 0; i < select.options.length; i++) {{
                                                if (select.options[i].text.toLowerCase().includes('{value.lower().replace("'", "\\'")}')) {{
                                                    select.selectedIndex = i;
                                                    select.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                                    break;
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    """)
                    logger.info(f"✅ Selected dropdown option '{option_text}' for field '{field['clean_label']}'")
                    return True
                elif selector:
                    await page.locator(selector).first.select_option(label=option_text)
                    logger.info(f"✅ Selected dropdown option '{option_text}' for field '{field['clean_label']}'")
                    return True
        
        logger.warning(f"❌ Could not find matching dropdown option for value: {value}")
        return False
        
    except Exception as e:
        logger.error(f"Failed to fill select field: {e}")
        return False

async def fill_checkbox_field(page: Page, field: Dict, value: str) -> bool:
    """Fills checkbox fields (for multiple choice)."""
    try:
        # For checkboxes, we might need to check multiple options
        # For now, just try to find and check the first matching option
        options = field.get('options', [])
        
        for i, option_text in enumerate(options):
            if value.lower() in option_text.lower() or option_text.lower() in value.lower():
                selector = await page.evaluate(f"""
                    () => {{
                        const titles = document.querySelectorAll(
                            '.freebirdFormviewerViewItemsItemItemTitle, .exportItemTitle, .qSFzN'
                        );
                        
                        for (const title of titles) {{
                            if (title.textContent.toLowerCase().includes('{field['clean_label'].lower().replace("'", "\\'")}')) {{
                                const container = title.closest('.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]');
                                if (container) {{
                                    const checkboxes = container.querySelectorAll('input[type="checkbox"]');
                                    if (checkboxes[{i}]) {{
                                        if (checkboxes[{i}].id) return '#' + checkboxes[{i}].id;
                                        return 'found';
                                    }}
                                }}
                            }}
                        }}
                        return null;
                    }}
                """)
                
                if selector == 'found':
                    await page.evaluate(f"""
                        () => {{
                            const titles = document.querySelectorAll(
                                '.freebirdFormviewerViewItemsItemItemTitle, .exportItemTitle, .qSFzN'
                            );
                            
                            for (const title of titles) {{
                                if (title.textContent.toLowerCase().includes('{field['clean_label'].lower().replace("'", "\\'")}')) {{
                                    const container = title.closest('.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]');
                                    if (container) {{
                                        const checkboxes = container.querySelectorAll('input[type="checkbox"]');
                                        if (checkboxes[{i}]) {{
                                            if (!checkboxes[{i}].checked) {{
                                                checkboxes[{i}].click();
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    """)
                    logger.info(f"✅ Checked option '{option_text}' for field '{field['clean_label']}'")
                    return True
                elif selector:
                    checkbox = page.locator(selector).first
                    if not await checkbox.is_checked():
                        await checkbox.check()
                    logger.info(f"✅ Checked option '{option_text}' for field '{field['clean_label']}'")
                    return True
        
        logger.warning(f"❌ Could not find matching checkbox option for value: {value}")
        return False
        
    except Exception as e:
        logger.error(f"Failed to fill checkbox field: {e}")
        return False

async def navigate_to_next_page(page: Page) -> bool:
    """Navigates to the next page in a multi-page form."""
    try:
        # Look for "Next" button
        next_selectors = [
            'div[role="button"]:has-text("Next")',
            'button:has-text("Next")',
            'input[type="button"][value="Next"]',
            '.freebirdFormviewerNavigationNextButton',
        ]
        
        for selector in next_selectors:
            try:
                button = page.locator(selector).first
                if await button.count() > 0 and await button.is_visible():
                    await button.click()
                    await page.wait_for_timeout(2000)  # Wait for page transition
                    logger.info("✅ Navigated to next page")
                    return True
            except:
                continue
        
        logger.info("ℹ️ No next page button found (likely last page)")
        return False
        
    except Exception as e:
        logger.error(f"Failed to navigate to next page: {e}")
        return False

async def click_submit_button(page: Page) -> bool:
    """Finds and clicks the submit button and WAITS for confirmation."""
    submit_selectors = [
        'div[role="button"][aria-label="Submit form"]',
        'span:has-text("Submit")',
        'div[role="button"]:has-text("Submit")',
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    
    for selector in submit_selectors:
        try:
            button = page.locator(selector).first
            if await button.count() > 0:
                await button.wait_for(state='visible', timeout=3000)
                await button.click()
                logger.info(f"✅ Clicked submit button: {selector}")
                
                # Wait for submission confirmation
                try:
                    await page.wait_for_selector('text="Your response has been recorded"', timeout=15000)
                    logger.info("✅ Form submission confirmed!")
                    return True
                except:
                    # Fallback: wait for network idle
                    await page.wait_for_load_state('networkidle', timeout=10000)
                    logger.info("✅ Form submission assumed successful")
                    return True
        except Exception as e:
            logger.warning(f"Failed to click {selector}: {e}")
            continue
    
    logger.warning("❌ Could not find submit button")
    return False