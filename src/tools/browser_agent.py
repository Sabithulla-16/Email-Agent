import asyncio
from playwright.async_api import async_playwright, Page, Browser
from bs4 import BeautifulSoup
from src.core.logging import logger
from typing import Dict, List, Optional

# Global browser instance (singleton pattern)
_browser: Optional[Browser] = None
_playwright = None

async def get_browser() -> Browser:
    """Get or create a headless Chromium browser instance."""
    global _browser, _playwright
    if _browser is None or not _browser.is_connected():
        if _playwright is None:
            _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        logger.info("🌐 Browser instance started")
    return _browser

async def close_browser():
    """Close the browser instance."""
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
    logger.info("🌐 Browser instance closed")

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
        await page.wait_for_timeout(2000)  # Wait for JS to render
        
        html_content = await page.content()
        return page, html_content
    except Exception as e:
        logger.error(f"Failed to open page {url}: {e}")
        await page.close()
        raise

async def extract_form_fields(html_content: str) -> List[Dict]:
    """Extracts form fields from HTML using BeautifulSoup."""
    soup = BeautifulSoup(html_content, 'html.parser')
    fields = []
    
    # Find all input, textarea, and select elements
    for element in soup.find_all(['input', 'textarea', 'select']):
        field_type = element.name
        input_type = element.get('type', 'text')
        
        # Skip hidden, submit, and button fields
        if input_type in ['hidden', 'submit', 'button', 'checkbox', 'radio']:
            continue
            
        field_info = {
            'name': element.get('name', ''),
            'id': element.get('id', ''),
            'type': input_type if field_type == 'input' else field_type,
            'placeholder': element.get('placeholder', ''),
            'label': '',
            'required': element.has_attr('required'),
            'options': []  # For select fields
        }
        
        # Try to find associated label
        if field_info['id']:
            label = soup.find('label', {'for': field_info['id']})
            if label:
                field_info['label'] = label.get_text(strip=True)
        
        # For select fields, get options
        if field_type == 'select':
            for option in element.find_all('option'):
                field_info['options'].append(option.get_text(strip=True))
        
        # Only add if we have some identifier
        if field_info['name'] or field_info['id'] or field_info['label']:
            fields.append(field_info)
    
    logger.info(f"🔍 Found {len(fields)} form fields")
    return fields

async def fill_field(page: Page, field: Dict, value: str):
    """Fills a single form field."""
    selector = None
    if field.get('id'):
        selector = f"#{field['id']}"
    elif field.get('name'):
        selector = f"[name='{field['name']}']"
    
    if not selector:
        return False
    
    try:
        element = page.locator(selector).first
        if await element.is_visible():
            if field['type'] == 'select':
                await element.select_option(label=value)
            else:
                await element.fill(value)
            logger.info(f"✅ Filled field '{field.get('label', field.get('name'))}' with '{value}'")
            return True
    except Exception as e:
        logger.warning(f"Could not fill field {field.get('name')}: {e}")
    
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
    ]
    
    for selector in submit_selectors:
        try:
            button = page.locator(selector).first
            if await button.is_visible():
                await button.click()
                logger.info(f"✅ Clicked submit button: {selector}")
                await page.wait_for_timeout(3000)  # Wait for submission
                return True
        except:
            continue
    
    logger.warning("❌ Could not find submit button")
    return False