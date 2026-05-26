"""SJSU IT Service Desk RAG Agent."""

import logging
import os
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv
from vertexai import rag
from vertexai.generative_models import GenerativeModel, Tool
import vertexai

load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID", "")
LOCATION = os.getenv("LOCATION", "us-west1")
RAG_CORPUS_ID = os.getenv("RAG_CORPUS_ID")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

CORPUS_NAME = f"projects/{PROJECT_ID}/locations/{LOCATION}/ragCorpora/{RAG_CORPUS_ID}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


SYSTEM_INSTRUCTION = """You are an AI assistant for the SJSU IT Service Desk front desk staff.
Your job is to help them find accurate answers from the SDKB knowledge base.

CRITICAL RULES:
1. You MUST use the RAG corpus to answer EVERY question. Do not answer from general knowledge.
2. Reproduce wiki content VERBATIM when it provides canned responses, queue names, phone numbers, URLs, or error codes.
3. If the RAG corpus does not contain the answer, say: "I couldn't find this in the SDKB knowledge base. Please check with the team lead or escalate to security@sjsu.edu if urgent."
4. NEVER suggest generic resources like "check your service catalog" or "ask your manager."
5. For routing questions, provide the EXACT queue name verbatim from the wiki.
6. If SDKB only points to a Google Drive folder or physical location for the actual answer, be honest: say "The detailed instructions are in [linked resource]" and provide the exact link.

CITATION RULES:
- End your answer with "Source: [Exact Page Title]" using the page title from retrieved chunks.
- Never invent or paraphrase page titles.
- If no clean title is identifiable, write "Source: SDKB Knowledge Base".

RESPONSE FORMAT (keep concise — under 200 words):

**Direct Answer:**
1-2 sentences. Include specifics: queue names, phone numbers, links.

**Steps / Details:** (only if there are concrete steps)
- Bullet points
- Each bullet under 25 words
- Quote canned responses VERBATIM

**Important Notes:** (only if there are warnings or edge cases)

Source: [Exact Page Title]

TONE: Concise. Bullets over paragraphs. Bold key terms. Don't pad with generic advice.
"""


@dataclass
class AgentResponse:
    answer: str
    sources: list = field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None


vertexai.init(project=PROJECT_ID, location=LOCATION)

rag_retrieval_tool = Tool.from_retrieval(
    retrieval=rag.Retrieval(
        source=rag.VertexRagStore(
            rag_resources=[rag.RagResource(rag_corpus=CORPUS_NAME)],
            rag_retrieval_config=rag.RagRetrievalConfig(
                top_k=12,
                filter=rag.Filter(vector_distance_threshold=0.6),
            ),
        )
    )
)

model = GenerativeModel(
    model_name=GEMINI_MODEL,
    system_instruction=SYSTEM_INSTRUCTION,
    tools=[rag_retrieval_tool],
)


def _extract_sources(candidate):
    sources = []
    if hasattr(candidate, "grounding_metadata") and candidate.grounding_metadata:
        chunks = getattr(candidate.grounding_metadata, "grounding_chunks", []) or []
        seen_uris = set()
        for chunk in chunks:
            if hasattr(chunk, "retrieved_context") and chunk.retrieved_context:
                uri = chunk.retrieved_context.uri or ""
                title = chunk.retrieved_context.title or uri
                if uri and uri not in seen_uris:
                    sources.append({"title": title, "uri": uri})
                    seen_uris.add(uri)
    return sources


def answer(question: str) -> AgentResponse:
    """Non-streaming version: waits for full response."""
    start = time.time()
    try:
        response = model.generate_content(question)
        answer_text = response.text
        sources = _extract_sources(response.candidates[0])
        latency_ms = int((time.time() - start) * 1000)
        logger.info(f"Q answered in {latency_ms}ms with {len(sources)} sources")
        return AgentResponse(answer=answer_text, sources=sources, latency_ms=latency_ms)
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        logger.exception(f"Error: {e}")
        return AgentResponse(answer="", sources=[], latency_ms=latency_ms, error=str(e))


def answer_stream(question: str):
    """Streaming version: yields (chunk_text, sources_or_none, latency_or_none).

    Each yield is one of:
      - (text, None, None) for streaming text chunks
      - ("", sources, latency_ms) once at the end with final metadata
    """
    start = time.time()
    try:
        response_stream = model.generate_content(question, stream=True)
        final_response = None
        for chunk in response_stream:
            if hasattr(chunk, "text") and chunk.text:
                yield chunk.text, None, None
            final_response = chunk

        sources = []
        if final_response and final_response.candidates:
            sources = _extract_sources(final_response.candidates[0])

        latency_ms = int((time.time() - start) * 1000)
        yield "", sources, latency_ms

    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        logger.exception(f"Error: {e}")
        yield f"\n\n⚠️ Error: {e}", [], latency_ms


if __name__ == "__main__":
    for q in [
        "How do I respond to a customer whose Duo account got a fraudulent report?",
        "What is the corresponding queue for classroom design tickets?",
    ]:
        print(f"\n{'='*70}\nQ: {q}\n{'='*70}")
        result = answer(q)
        print(f"\n{result.answer}\n")
        print(f"Sources: {len(result.sources)} | Latency: {result.latency_ms}ms")
