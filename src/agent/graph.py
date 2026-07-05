from langgraph.graph import StateGraph, END
from src.core.logging import logger
from src.agent.state import AgentState
from src.agent.nodes import triage_node, extract_node

def build_email_processing_graph():
    logger.info("🕸️ Compiling LangGraph workflow...")
    workflow = StateGraph(AgentState)
    
    # Add nodes to the graph
    workflow.add_node("triage", triage_node)
    workflow.add_node("extract", extract_node)
    
    # Define the flow
    workflow.set_entry_point("triage")
    workflow.add_edge("triage", "extract")
    workflow.add_edge("extract", END)
    
    return workflow.compile()

# Export the compiled graph
email_agent_graph = build_email_processing_graph()