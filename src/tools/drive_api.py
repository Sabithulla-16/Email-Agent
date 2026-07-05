import base64
import io
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from pypdf import PdfReader
from src.core.logging import logger

def extract_text_from_pdf(pdf_bytes: bytes, max_pages: int = 10) -> str:
    """Extracts text from a PDF, limiting to the first X pages to save memory/tokens."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        
        # 🔥 SAFEGUARD: Only read up to max_pages (default 10)
        pages_to_read = min(len(reader.pages), max_pages)
        
        for i in range(pages_to_read):
            page_text = reader.pages[i].extract_text()
            if page_text:
                text += page_text + "\n"
                
        if len(reader.pages) > max_pages:
            text += f"\n[... PDF truncated. Only the first {max_pages} pages were read. ...]\n"
            
        return text
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return ""

def get_attachment_content(creds: Credentials, message_id: str) -> str:
    """Fetches standard attachments (PDFs, TXT) from an email and extracts their text."""
    try:
        gmail_service = build('gmail', 'v1', credentials=creds)
        msg = gmail_service.users().messages().get(userId='me', id=message_id, format='full').execute()
        parts = msg.get('payload', {}).get('parts', [])
        
        full_attachment_text = ""
        
        for part in parts:
            filename = part.get('filename', '')
            mime_type = part.get('mimeType', '')
            
            # Check if this part has an attachment ID
            if part.get('body', {}).get('attachmentId'):
                attachment_id = part['body']['attachmentId']
                
                # 🔥 SAFEGUARD: Check file size (Skip if larger than 5MB to keep things fast)
                file_size = part.get('body', {}).get('size', 0)
                if file_size > 5_000_000: 
                    logger.info(f"Skipping large attachment: {filename} ({file_size} bytes)")
                    full_attachment_text += f"\n[Skipped large attachment: {filename}]\n"
                    continue

                # We only process PDFs and Text files (ignores images/banners)
                if mime_type == 'application/pdf' or mime_type == 'text/plain':
                    try:
                        attachment = gmail_service.users().messages().attachments().get(
                            userId='me', messageId=message_id, id=attachment_id
                        ).execute()
                        
                        file_data = base64.urlsafe_b64decode(attachment['data'])
                        full_attachment_text += f"\n--- START ATTACHMENT: {filename} ---\n"
                        
                        if mime_type == 'application/pdf':
                            full_attachment_text += extract_text_from_pdf(file_data)
                        elif mime_type == 'text/plain':
                            # Limit text files to first 10,000 characters
                            decoded = file_data.decode('utf-8', errors='ignore')
                            full_attachment_text += decoded[:10000] 
                            
                        full_attachment_text += f"\n--- END ATTACHMENT: {filename} ---\n"
                        
                    except Exception as e:
                        logger.error(f"Failed to download attachment {filename}: {e}")
                        
        return full_attachment_text
        
    except Exception as e:
        logger.error(f"Error fetching attachments for message {message_id}: {e}")
        return ""