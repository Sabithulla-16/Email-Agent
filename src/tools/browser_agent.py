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
    🔥 RELIABLE METHOD: Uses JavaScript to extract form fields.
    This works for Google Forms, Typeform, and any JS-heavy forms.
    """
    try:
        # 🔥 Execute JavaScript in the browser context to find form fields
        fields = await page.evaluate('''() => {
            const fields = [];
            
            // Find all input, textarea, and select elements
            const inputs = document.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"], input[type="url"], textarea, select');
            
            inputs.forEach((input, index) => {
                // Skip hidden, submit, button, checkbox, radio
                const type = input.type || 'text';
                if (['hidden', 'submit', 'button', 'checkbox', 'radio'].includes(type)) {
                    return;
                }
                
                // Find associated label
                let label = '';
                const id = input.id;
                if (id) {
                    const labelElem = document.querySelector(`label[for="${id}"]`);
                    if (labelElem) label = labelElem.textContent.trim();
                }
                
                // Try to find label in parent or sibling
                if (!label) {
                    const parent = input.closest('div, p, li');
                    if (parent) {
                        const labelElem = parent.querySelector('label');
                        if (labelElem) label = labelElem.textContent.trim();
                        
                        // Try to get text before the input
                        if (!label) {
                            const siblings = parent.childNodes;
                            for (let node of siblings) {
                                if (node === input) break;
                                if (node.nodeType === 3 && node.textContent.trim()) {
                                    label = node.textContent.trim();
                                    break;
                                }
                            }
                        }
                    }
                }
                
                // Check if required
                const required = input.hasAttribute('required') || 
                               input.getAttribute('aria-required') === 'true';
                
                fields.push({
                    name: input.name || `field_${index}`,
                    id: input.id || '',
                    type: type,
                    label: label || input.placeholder || `Field ${index + 1}`,
                    placeholder: input.placeholder || '',
                    required: required
                });
            });
            
            return fields;
        }''')
        
        logger.info(f"🔍 Found {len(fields)} form fields via JavaScript")
        return fields
        
    except Exception as e:
        logger.error(f"JavaScript field extraction failed: {e}")
        return []

async def fill_field(page: Page, field: Dict, value: str) -> bool:
    """Fills a single form field."""
    try:
        # Try to find the field by various selectors
        selector = None
        
        if field.get('id'):
            selector = f"#{field['id']}"
        elif field.get('name'):
            selector = f"[name='{field['name']}']"
        elif field.get('label'):
            # Try to find by label text
            selector = await page.evaluate(f'''() => {{
                const inputs = document.querySelectorAll('input, textarea, select');
                for (const input of inputs) {{
                    const label = input.placeholder || input.getAttribute('aria-label') || '';
                    if (label.toLowerCase().includes('{field['label'].lower()}')) {{
                        if (input.id) return `#${{input.id}}`;
                        if (input.name) return `[name='${{input.name}}']`;
                    }}
                }}
                return null;
            }}''')
        
        if not selector:
            logger.warning(f"Could not find selector for field: {field.get('label')}")
            return False
        
        element = page.locator(selector).first
        
        # Check if element exists and is visible
        if await element.count() > 0:
            await element.wait_for(state='visible', timeout=5000)
            
            if field.get('type') == 'select':
                await element.select_option(label=value)
            else:
                await element.fill(value)
            
            logger.info(f"✅ Filled field '{field.get('label')}' with '{value}'")
            return True
        else:
            logger.warning(f"Field not found: {field.get('label')}")
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