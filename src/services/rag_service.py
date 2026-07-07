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
    
    # 2. Call the Supabase SQL function to search chunks
    response = supabase_client.rpc(
        'match_chunks',
        {
            'query_embedding': query_embedding,
            'match_count': 5,
            'filter_user_id': user_id
        }
    ).execute()
    context_chunks = response.data
    
    # 🔥 FALLBACK: If vector search fails, try basic keyword search on the main emails table
    if not context_chunks:
        logger.info("⚠️ Vector search found nothing. Falling back to keyword search...")
        keyword_response = supabase_client.table('emails').select(
            'subject, sender, body_text'
        ).eq(
            'user_id', user_id
        ).ilike(
            'body_text', f'%{question}%'
        ).limit(3).execute()
        
        if keyword_response.data:
            # Format keyword results to look like chunks
            context_chunks = [
                {
                    'subject': row['subject'],
                    'sender': row['sender'],
                    'chunk_text': row['body_text'][:500]
                } for row in keyword_response.data
            ]
        else:
            return "I couldn't find any information related to that topic in your emails."

    # 3. Format the retrieved chunks into a single context string
    context_text = ""
    for chunk in context_chunks:
        context_text += f"--- Excerpt from email: '{chunk.get('subject')}' (From: {chunk.get('sender')}) ---\n"
        context_text += f"{chunk.get('chunk_text')}\n\n"

    # 4. Prompt Groq to answer based on the chunks
    rag_prompt = ChatPromptTemplate.from_template(
        """You are a helpful assistant answering questions based on the user's email history.
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