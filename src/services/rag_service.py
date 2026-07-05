from sentence_transformers import SentenceTransformer
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.core.config import settings
from src.db.client import supabase_client
from src.core.logging import logger

# 1. Initialize the LOCAL Embeddings model
# This will download the model (~80MB) the very first time you run it, 
# and then it will cache it locally for instant use.
logger.info("🧠 Loading local embedding model (all-MiniLM-L6-v2)...")
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

# 2. Initialize Groq for the final answer synthesis
groq_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.0,
    groq_api_key=settings.GROQ_API_KEY
)

def generate_embedding(text: str) -> list[float]:
    """Generates a 384-dimension vector using the local model."""
    # encode returns a numpy array, we convert it to a standard python list for Supabase
    embeddings = embedding_model.encode(text)
    return embeddings.tolist()

async def chat_with_emails(user_id: str, question: str) -> str:
    """Searches the user's email chunks for context and generates an answer."""
    logger.info(f"🔍 Searching email chunks for: '{question}'")
    
    # 1. Generate embedding for the user's question
    query_embedding = generate_embedding(question)
    
    # 2. Call the NEW Supabase SQL function to search chunks
    response = supabase_client.rpc(
        'match_chunks',
        {
            'query_embedding': query_embedding,
            'match_count': 5, # Fetch 5 specific chunks
            'filter_user_id': user_id
        }
    ).execute()
    
    context_chunks = response.data
    
    if not context_chunks:
        return "I couldn't find any information related to that topic in your emails or documents."

    # 3. Format the retrieved chunks into a single context string
    context_text = ""
    for chunk in context_chunks:
        context_text += f"--- Excerpt from email: '{chunk.get('subject')}' (From: {chunk.get('sender')}) ---\n"
        # Include the specific chunk text
        context_text += f"{chunk.get('chunk_text')}\n\n"

    # 4. Prompt Groq to answer based on the chunks
    rag_prompt = ChatPromptTemplate.from_template(
        """You are a helpful assistant answering questions based on the user's email history and attached documents.
        Use ONLY the provided context to answer the question. 
        
        FORMATTING RULES:
        - You are generating HTML for Telegram. Telegram ONLY supports these tags: <b>, <i>, <ul>, <ol>, <li>.
        - Use <b> for headers and important text.
        - Use <ul><li>item</li></ul> for lists.
        - Use \n for line breaks and spacing.
        - DO NOT use <p>, <br>, <h1>, <h2>, <div>, or any other HTML tags.
        - DO NOT use markdown symbols like #, *, or _.
        - DO NOT use the & symbol, write "and" instead.
        
        If the answer is not in the context, say "I couldn't find that information in your emails."
        
        Context:
        {context}
        
        Question: {question}
        
        Answer:"""
    )
    
    chain = rag_prompt | groq_llm
    result = chain.invoke({"context": context_text, "question": question})
    
    return result.content