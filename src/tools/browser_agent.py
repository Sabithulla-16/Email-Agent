from playwright.async_api import async_playwright, Page, Browser
from src.core.config import settings
from src.core.logging import logger
from typing import Dict, List, Optional

# 🔥 Browserless.io connection URL
BROWSERLESS_URL = f"wss://chrome.browserless.io?token={settings.BROWSERLESS_API_KEY}"

_browser: Optional[Browser] = None
_playwright = None

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

async def extract_form_fields(page: Page) -> List[Dict]:
    """
    Extracts form fields with proper label detection for Google Forms.
    """
    try:
        fields = await page.evaluate('''() => {
            const fields = [];
            
            // Try Google Forms specific structure first
            const formItems = document.querySelectorAll(
                '.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params]'
            );
            
            if (formItems.length > 0) {
                formItems.forEach((item, index) => {
                    // Extract question text using multiple selectors
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
                    
                    // If no label found, try to extract from container text
                    if (!label) {
                        const clone = item.cloneNode(true);
                        const inputs = clone.querySelectorAll('input, textarea, select');
                        inputs.forEach(el => el.remove());
                        const text = clone.textContent.trim();
                        if (text && text.length < 200) {
                            label = text;
                        }
                    }
                    
                    // Find the input element
                    const input = item.querySelector(
                        'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], textarea, select'
                    );
                    
                    if (input) {
                        // Use actual label, or fallback to aria-label/placeholder
                        const finalLabel = label || 
                                          input.getAttribute('aria-label') || 
                                          input.placeholder || 
                                          `Question ${index + 1}`;
                        
                        fields.push({
                            name: input.name || input.id || `gform_field_${index}`,
                            id: input.id || '',
                            type: input.tagName.toLowerCase() === 'textarea' ? 'textarea' : (input.type || 'text'),
                            label: finalLabel,
                            placeholder: input.placeholder || '',
                            ariaLabel: input.getAttribute('aria-label') || '',
                            required: input.hasAttribute('required') || input.getAttribute('aria-required') === 'true'
                        });
                    }
                });
            }
            
            // Fallback for non-Google Forms
            if (fields.length === 0) {
                const inputs = document.querySelectorAll(
                    'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], textarea, select'
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
                    
                    if (!label) label = `Question ${index + 1}`;
                    
                    fields.push({
                        name: input.name || input.id || `field_${index}`,
                        id: input.id || '',
                        type: input.type || 'text',
                        label: label,
                        placeholder: input.placeholder || '',
                        ariaLabel: input.getAttribute('aria-label') || '',
                        required: input.hasAttribute('required')
                    });
                });
            }
            
            return fields;
        }''')
        
        logger.info(f"🔍 Found {len(fields)} form fields: {[f['label'] for f in fields]}")
        return fields
        
    except Exception as e:
        logger.error(f"JavaScript field extraction failed: {e}")
        return []

async def fill_field(page: Page, field: Dict, value: str) -> bool:
    """Fills a single form field with robust fallbacks for Google Forms."""
    try:
        element = None
        
        # 1. Try by ID (most reliable)
        if field.get('id'):
            element = page.locator(f"#{field['id']}").first
            if await element.count() == 0:
                element = None
        
        # 2. Try by name (if not generic)
        if not element and field.get('name') and not field['name'].startswith(('gform_field', 'field_')):
            element = page.locator(f"[name='{field['name']}']").first
            if await element.count() == 0:
                element = None
        
        # 3. Try by aria-label (very common in Google Forms)
        if not element and field.get('ariaLabel'):
            safe_aria = field['ariaLabel'].replace("'", "\\'")
            element = page.locator(f"input[aria-label*='{safe_aria}' i], textarea[aria-label*='{safe_aria}' i]").first
            if await element.count() == 0:
                element = None
        
        # 4. 🔥 CRITICAL FIX: Use JavaScript to find input by traversing from question title
        if not element and field.get('label'):
            # Extract just the question text (remove "*Your answer" suffix)
            question_text = field['label'].split('*Your answer')[0].split('Your answer')[0].strip()
            safe_label = question_text.replace("'", "\\'")
            
            # Use JavaScript to find the input by looking for the question title
            selector = await page.evaluate(f"""
                () => {{
                    // Find all question titles/labels
                    const titles = document.querySelectorAll(
                        '.freebirdFormviewerViewItemsItemItemTitle, .exportItemTitle, .qSFzN, [role="heading"]'
                    );
                    
                    for (const title of titles) {{
                        const titleText = title.textContent.trim();
                        // Check if this title matches our question (case-insensitive)
                        if (titleText.toLowerCase().includes('{safe_label.toLowerCase()}')) {{
                            // Find the container for this question
                            const container = title.closest('.freebirdFormviewerViewItemsItemItem, .M7eMe, [data-params], [role="listitem"]');
                            if (container) {{
                                // Find the input within this container
                                const input = container.querySelector('input[type="text"], input[type="email"], input[type="tel"], input[type="url"], textarea');
                                if (input) {{
                                    if (input.id) return '#' + input.id;
                                    if (input.name) return '[name="' + input.name + '"]';
                                    
                                    // Direct fill via JavaScript if we can't get a selector
                                    input.value = '{value}';
                                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    return 'filled';
                                }}
                            }}
                        }}
                    }}
                    return null;
                }}
            """)
            
            if selector == 'filled':
                logger.info(f"✅ Filled field '{field.get('label')}' with '{value}' via JS")
                return True
            elif selector:
                element = page.locator(selector).first
                if await element.count() == 0:
                    element = None
        
        # If we found the element, fill it
        if element and await element.count() > 0:
            await element.wait_for(state='visible', timeout=5000)
            
            if field.get('type') == 'select':
                await element.select_option(label=value)
            else:
                await element.fill(value)
            
            logger.info(f"✅ Filled field '{field.get('label')}' with '{value}'")
            return True
        else:
            logger.warning(f"❌ Field not found: {field.get('label')} (ID: {field.get('id')}, Name: {field.get('name')})")
            return False
        
    except Exception as e:
        logger.error(f"Failed to fill field {field.get('label')}: {e}")
        return False

async def click_submit_button(page: Page) -> bool:
    """Finds and clicks the submit button."""
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit")',
        'button:has-text("Register")',
        'button:has-text("Sign Up")',
        'button:has-text("Apply")',
        'button:has-text("Send")',
        '[role="button"]:has-text("Submit")',
    ]
    
    for selector in submit_selectors:
        try:
            button = page.locator(selector).first
            if await button.count() > 0:
                await button.wait_for(state='visible', timeout=3000)
                await button.click()
                logger.info(f"✅ Clicked submit button: {selector}")
                await page.wait_for_timeout(3000)
                return True
        except Exception:
            continue
    
    logger.warning("❌ Could not find submit button")
    return False