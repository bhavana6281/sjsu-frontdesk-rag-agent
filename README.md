# SJSU IT Front Desk RAG Agent

> A Retrieval-Augmented Generation (RAG) agent that gives SJSU IT Service Desk
> student assistants instant, cited answers from the SDKB knowledge base —
> replacing manual searches across 5 separate knowledge systems.

---

## What this does

The SJSU IT Service Desk's three student assistants handle 12+ calls and walk-ins per hour. Their answers live across 5 systems:

- Confluence SDKB (admin-controlled, ~1 week update lag)
- Brain
- iSupport
- Gravity
- ad-hoc Google Drive documents

A single user question often requires searching 2-3 systems. Each lookup costs 30-90 seconds.

This agent unifies that knowledge into a single grounded, cited answer in 3-5 seconds.

**Why not Confluence Rovo?** Rovo only sees Confluence. This agent additionally indexes a Google Sheet maintained by front-desk leads, who can update Q&A answers in real time without waiting on the weekly admin Confluence cycle.

---

## Features

- **Grounded answers** — every response cites the source SDKB page; no hallucinated facts.
- **Sub-second first token** — streaming responses give SAs visible feedback in under 1 second.
- **Daily auto-sync** — idempotent ingestion picks up wiki edits and Sheet updates with no manual intervention.
- **Google Sheet override layer** — leads can add unique Q&A pairs that bypass the slow wiki update cycle.
- **Strict citation rules** — system prompt forbids inventing page titles or URLs.
- **Title resolution** — citations show real page names (e.g. "Sophos Antivirus") not numeric IDs.
- **Production-ready security** — read-only Sheets scope, env-driven config, no hardcoded credentials, no service-account keys in repo.
- **Idempotent corpus refresh** — delete + reimport pattern propagates wiki deletions and Sheet row deactivations cleanly.
- **Rate-limit-aware** — exponential backoff retry handles Vertex RAG's 60-deletes/min cap.

---

## Architecture

Two pipelines:
```INGESTION (runs daily)Confluence SDKB ─┐
├─► ingest.py ─► GCS bucket ─► Vertex AI RAG corpus
Google Sheet ────┘QUERY (per user question)Streamlit UI ─► agent.py ─► Vertex RAG retrieval ─► Gemini 2.5 Flash
▲                                                    │
└──────── cited answer streamed back ◄───────────────┘```
### Ingestion pipeline (daily, ~5-6 minutes)

1. **Pull from Confluence** via the Application Integration connector. Filter to the SDKB space, return ~148 pages.
2. **Clean HTML to markdown** using BeautifulSoup + markdownify. Strip nav widgets, preserve heading hierarchy, drop nav-only pages under 50 chars. Yields ~129 usable pages.
3. **Pull Q&A overrides from Google Sheet** via the Sheets API (read-only scope). One row per question, written as a dedicated JSON document.
4. **Upload to GCS bucket** as JSON files, one per page or Sheet row. Deletes orphaned files for pages no longer in Confluence.
5. **Refresh RAG corpus** — delete all existing files in the corpus, reimport from GCS. Vertex generates embeddings automatically. Rate-limit-aware: 1.2s sleep between deletes + exponential backoff retry to stay under the 60-per-minute cap.

### Query pipeline (3-5 seconds per question)

1. **Streamlit** captures the user's question and posts it to the agent service.
2. **`agent.py`** invokes Gemini with a RAG retrieval tool attached.
3. **Vertex RAG Engine** embeds the question, performs vector similarity search against the corpus, returns the top chunks.
4. **Gemini 2.5 Flash** receives the question + retrieved chunks + system prompt forbidding hallucination.
5. **Response streams back** through Streamlit token-by-token. First token arrives in under 1 second; total response 3-5 seconds.
6. **Citations are extracted** from Gemini's grounding metadata and rendered as clickable links back to the source Confluence page.

**Tech stack:** Python · Streamlit · Vertex AI RAG Engine · Gemini 2.5 Flash · Google Cloud Storage · Application Integration · Google Sheets API · Cloud Run (deployment target).

---

## How latency stays under 5 seconds

A grounded RAG response involves three serial steps that could each be slow. The architecture is tuned so the perceived total stays under 5 seconds and the user sees the first words almost immediately.

### Retrieval tuning

```python
top_k=12
vector_distance_threshold=0.6
```

The retrieval engine returns the top 12 chunks per query, filtered to those with vector distance ≤ 0.6 (Vertex's default). These defaults balance recall against precision:

- **`top_k=12`** is enough to capture multi-page topics (overview + steps + troubleshooting) without bloating Gemini's input. Going higher (e.g. 30) adds ~1 second of latency without meaningful recall gain on a 130-document corpus.
- **`threshold=0.6`** is strict enough to filter loosely-related noise but loose enough to handle paraphrased questions (user phrasing rarely matches wiki titles word-for-word).

Retrieval itself runs in ~200ms. The bulk of latency comes from Gemini.

### Streaming responses

Gemini's full response takes 3-5 seconds, but Streamlit displays tokens as they arrive — not after the full response completes. The user sees the first word within ~1 second of asking. Perceived latency is dominated by time-to-first-token, not time-to-completion.

### Model choice: Gemini 2.5 Flash

Flash is Google's fast, cost-efficient model. Compared to Pro/Ultra:

- **3-5x faster** on similar tasks
- **~10x cheaper** per token
- **Quality is sufficient** for grounded Q&A (the model isn't doing creative reasoning, it's synthesizing retrieved facts)

For a task that's literally "summarize these chunks accurately," Flash hits the sweet spot.

### Pre-processed JSON (not raw HTML)

Vertex RAG could index Confluence URLs directly, but the project pre-processes pages into clean JSON first. This means:

- Embeddings are higher quality (no HTML noise like navigation menus, TOC widgets)
- Page hierarchy is preserved via `parent_id`
- Sheet content merges seamlessly with wiki content in the same corpus

Cleaner inputs → better retrieval → fewer wasted Gemini cycles on irrelevant chunks.

### Idempotent daily refresh, not real-time sync

The corpus updates once a day, not on every Sheet edit. Real-time sync would require complex change-detection logic and constant API calls. Daily sync covers >99% of use cases (front desk content doesn't change minute-to-minute) at a fraction of the engineering effort.

---

## Repo structure
```sjsu-frontdesk-rag-agent/
├── README.md                    ← you are here
├── .gitignore                   ← what NOT to commit
├── .env.example                 ← template for environment variables
│
├── sjsu-confluence-ingest/      ← daily sync pipeline
│   ├── ingest.py                ←   main orchestrator (Confluence + Sheet + corpus refresh)
│   ├── sheet_sync.py            ←   Google Sheet integration module
│   ├── requirements.txt         ←   pinned dependencies
│   └── (.env, output/, venv/    ← local-only, gitignored)
│
└── sjsu-it-agent/               ← live query service
├── agent.py                 ←   RAG agent + Gemini wrapper
├── frontend.py              ←   Streamlit UI
├── requirements.txt         ←   pinned dependencies
└── (.env, venv/             ← local-only, gitignored)```
---

## Quick start (local development)

**Prerequisites:** Python 3.11+, `gcloud` CLI authenticated to a GCP project with Vertex AI + GCS + Sheets APIs enabled.

```bash
# 1. Clone the repo
git clone https://github.com/bhavana6281/sjsu-frontdesk-rag-agent.git
cd sjsu-frontdesk-rag-agent

# 2. Configure environment for ingestion
cd sjsu-confluence-ingest
cp ../.env.example .env
# Edit .env with your values

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Run ingestion
python ingest.py
# Or with mock Sheet data (no Sheets API auth needed):
SHEET_TEST_MODE=true python ingest.py

# 4. Configure & run agent (in a new terminal)
cd ../sjsu-it-agent
cp ../.env.example .env
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
streamlit run frontend.py
```

The Streamlit UI opens at `http://localhost:8501`.

---

## Knowledge sources

The agent indexes two sources, in priority order:

1. **Google Sheet (front desk leads)** — Authoritative, edited any time. When the Sheet and Confluence cover the same topic, the Sheet wins.

2. **Confluence SDKB** — 129 wiki pages, admin-controlled, ~1 week update cadence.

Sheet schema (Sheet1):

| Column | Header       | Purpose              |
|--------|--------------|----------------------|
| A      | Category     | Grouping/topic       |
| B      | Question     | What users ask       |
| C      | Answer       | Authoritative answer |
| D      | Last Updated | Date                 |
| E      | Updated By   | Audit trail          |

---

## Built by

**Saibhavana Aluri** · SJSU IT Service Desk · May 2026
