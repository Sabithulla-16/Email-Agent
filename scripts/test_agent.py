import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent.graph import email_agent_graph
from src.core.logging import logger
import json

def test_agent_flow():
    test_email = """Subject: Project Sync & Q3 Deadline
From: alice@company.com
Hi Valtry,
Can we schedule a quick sync tomorrow at 2:00 PM EST to discuss the Q3 marketing deliverables? 
Also, please finalize the budget spreadsheet by Friday EOD. Let me know if you can make the meeting.
Best, Alice"""

    logger.info("🚀 Starting Agent Flow Test...")
    
    # Run the graph
    initial_state = {"email_text": test_email, "category": None, "summary": None, "meetings": [], "tasks": [], "error": None}
    final_state = email_agent_graph.invoke(initial_state)
    
    logger.info("\n📊 AGENT RESULTS:")
    logger.info(f"Category: {final_state.get('category')}")
    logger.info(f"Summary: {final_state.get('summary')}")
    logger.info(f"Meetings: {[m.model_dump() for m in final_state.get('meetings', [])]}")
    logger.info(f"Tasks: {[t.model_dump() for t in final_state.get('tasks', [])]}")
    logger.info("✅ Test completed successfully!")

if __name__ == "__main__":
    test_agent_flow()