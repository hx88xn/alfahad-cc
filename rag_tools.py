import os
import time
import asyncio
from pinecone import Pinecone
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv

load_dotenv(override=True)

PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "alfardan-callcenter")
PINECONE_NAMESPACE = os.getenv("PINECONE_NAMESPACE", "alfardan-data")

pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
index = pc.Index(PINECONE_INDEX_NAME)

embeddings = OpenAIEmbeddings(model="text-embedding-3-small", dimensions=1024)

_embedding_cache = {}
MAX_CACHE_SIZE = 100


def _get_cached_embedding(query: str):
    cache_key = query.strip().lower()

    if cache_key in _embedding_cache:
        return _embedding_cache[cache_key]

    query_vector = embeddings.embed_query(query)

    if len(_embedding_cache) >= MAX_CACHE_SIZE:
        oldest_key = next(iter(_embedding_cache))
        del _embedding_cache[oldest_key]

    _embedding_cache[cache_key] = query_vector
    return query_vector


def _sync_embed_and_query(query: str, top_k: int):
    query_vector = _get_cached_embedding(query)
    results = index.query(
        vector=query_vector,
        top_k=top_k,
        include_metadata=True,
        namespace=PINECONE_NAMESPACE
    )
    return results


def retrieve_context(query: str, top_k: int = 3, min_score: float = 0.35) -> str:
    try:
        query_vector = _get_cached_embedding(query)
        results = index.query(
            vector=query_vector,
            top_k=top_k,
            include_metadata=True,
            namespace=PINECONE_NAMESPACE
        )

        relevant_matches = [m for m in results.matches if m.score >= min_score]

        if not relevant_matches:
            return ""

        context_chunks = []
        seen_content = set()

        for match in relevant_matches:
            metadata = match.metadata or {}
            text_content = metadata.get("text", "")
            category = metadata.get("category", "General")
            subcategory = metadata.get("subcategory", "")

            content_hash = hash(text_content[:100])
            if content_hash in seen_content:
                continue
            seen_content.add(content_hash)

            if text_content:
                context_chunks.append(
                    f"[{category} - {subcategory}]\n{text_content}"
                )

        return "\n\n---\n\n".join(context_chunks) if context_chunks else ""

    except Exception as e:
        print(f"Error retrieving context: {e}")
        return ""


async def search_knowledge_base(query: str, top_k: int = 4, min_score: float = 0.35) -> dict:
    try:
        start_time = time.time()
        print(f"\n🔍 RAG SEARCH: '{query}'")

        results = await asyncio.to_thread(_sync_embed_and_query, query, top_k)

        elapsed = time.time() - start_time
        print(f"🔍 RAG SEARCH completed in {elapsed:.2f}s")

        if not results.matches:
            return {
                "success": False,
                "message": "No relevant information found in the knowledge base.",
                "results": []
            }

        for match in results.matches:
            print(f"  📊 Score: {match.score:.3f} - {match.metadata.get('category', 'N/A')}/{match.metadata.get('subcategory', 'N/A')}")

        relevant_matches = [m for m in results.matches if m.score >= min_score]

        if not relevant_matches:
            print(f"⚠️ RAG: All {len(results.matches)} results below min_score threshold ({min_score})")
            return {
                "success": False,
                "message": "No relevant information found for this query. The topic may not exist in our knowledge base.",
                "results": []
            }

        knowledge_results = []
        seen_content = set()

        for match in relevant_matches:
            metadata = match.metadata or {}
            text_content = metadata.get("text", "")
            category = metadata.get("category", "General")
            subcategory = metadata.get("subcategory", "")

            content_hash = hash(text_content[:100])
            if content_hash in seen_content:
                continue
            seen_content.add(content_hash)

            knowledge_results.append({
                "text": text_content,
                "category": category,
                "subcategory": subcategory,
                "score": match.score
            })

        context_chunks = []
        total_chars = 0
        MAX_TOTAL_CHARS = 600

        for r in knowledge_results:
            if r["text"] and total_chars < MAX_TOTAL_CHARS:
                remaining = MAX_TOTAL_CHARS - total_chars
                text = r["text"][:min(300, remaining)]
                total_chars += len(text)
                context_chunks.append(f"[{r['category']}]\n{text}")

        combined_context = "\n---\n".join(context_chunks)

        print(f"✅ RAG: Found {len(knowledge_results)} results ({total_chars} chars) in {elapsed:.2f}s")

        return {
            "success": True,
            "message": "Found relevant information.",
            "context": combined_context,
            "num_results": len(knowledge_results)
        }

    except Exception as e:
        print(f"⚠️ search_knowledge_base error: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred while searching the knowledge base."
        }
