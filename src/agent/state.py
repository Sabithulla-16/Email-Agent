from typing import TypedDict, List, Optional
from pydantic import BaseModel, Field

class Meeting(BaseModel):
    summary: str
    start_time: Optional[str] = Field(description="ISO 8601 format, e.g., 2026-07-05T10:00:00Z")
    end_time: Optional[str] = Field(description="ISO 8601 format")
    description: str = ""

class Task(BaseModel):
    title: str
    due_date: Optional[str] = Field(description="ISO 8601 format, or null if not specified")
    notes: str = ""

class Expense(BaseModel):
    vendor: str = Field(description="The company or person being paid (e.g., AWS, Adobe)")
    amount: float = Field(description="The total amount of the invoice")
    currency: str = Field(description="Currency code, e.g., USD, INR")
    expense_date: str = Field(description="Date of the invoice in YYYY-MM-DD format")
    category: str = Field(description="Category like Software, Travel, Food, Utilities")

class AgentState(TypedDict):
    email_text: str
    category: Optional[str]          # Urgent, Normal, Spam, None
    summary: Optional[str]
    meetings: List[Meeting]
    tasks: List[Task]
    needs_reply: Optional[bool]  
    expenses: List[Expense] 
    error: Optional[str]