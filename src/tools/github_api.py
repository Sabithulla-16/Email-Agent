import re
import httpx
from src.db.client import supabase_client
from src.core.logging import logger

async def enrich_email_with_github_data(email_text: str, user_uuid: str) -> str:
    """Scans email for GitHub links and appends real-time status."""
    # Regex for github.com/owner/repo/issues/123 or pull/123
    pattern = r"github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)"
    matches = re.findall(pattern, email_text)
    
    if not matches:
        return email_text

    # Get user's GitHub token from DB
    user_data = supabase_client.table('users').select('github_access_token').eq('id', user_uuid).execute()
    if not user_data.data or not user_data.data[0].get('github_access_token'):
        logger.info("No GitHub token found for user, skipping enrichment.")
        return email_text 

    token = user_data.data[0]['github_access_token']
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    
    context_addition = "\n--- GITHUB CONTEXT (Live) ---\n"
    
    async with httpx.AsyncClient() as client:
        for owner, repo, issue_num in matches:
            try:
                resp = await client.get(f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    status = "Open" if not data.get('pull_request') and not data.get('closed_at') else "Closed"
                    if data.get('pull_request'): 
                        status = "Merged" if data.get('merged_at') else "Open PR"
                    context_addition += f"• {owner}/{repo}#{issue_num}: {data.get('title')} [{status}]\n"
            except Exception as e:
                logger.error(f"GitHub API error: {e}")
                
    return email_text + context_addition