# SJSU IT Front Desk RAG Agent

> A Retrieval-Augmented Generation (RAG) agent that gives SJSU IT Service Desk
> student assistants instant, cited answers from the SDKB knowledge base —
> replacing manual searches across 5 separate knowledge systems.

---

## What this does

The SJSU IT Service Desk's three student assistants handle 12+ calls and
walk-ins per hour. Their answers live across 5 systems:

- Confluence SDKB (admin-controlled, ~1 week update lag)
- Brain
- iSupport
- Gravity
- ad-hoc Google Drive documents

A single user question often requires searching 2-3 systems. Each lookup
costs 30-90 seconds.

This agent unifies that knowledge into a single grounded, cited answer.

**Why not Confluence Rovo?** Rovo only sees Confluence. This agent additionally
indexes a Google Sheet maintained by front-desk leads, who can update Q&A
answers in real time without waiting on the weekly admin Confluence cycle.

---

## Architecture

Two pipelines:
INGESTION (runs daily)
Confluence SDKB ─┐
├─► ingest.py ─► GCS bucket ─► Vertex AI RAG corpus
Google Sheet ────┘
QUERY (per user question)
Streamlit UI ─► agent.py ─► Vertex RAG retrieval ─► Gemini 2.5 Flash
▲                                                    │
└──────── cited answer streamed back ◄───────────────┘
**Tech stack:** Python · Streamlit · Vertex AI RAG Engine · Gemini 2.5 Flash · Google Cloud Storage · Application Integration · Google Sheets API · Cloud Run (intended deployment target).

---

## Repo structure
sjsu-frontdesk-rag-agent/
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
└── (.env, venv/             ← local-only, gitignored)
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
# Edit .env with your values (see HANDOFF.md for reference values)

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Run ingestion (one-time, populates RAG corpus)
python ingest.py
# Or with mock Sheet data:
SHEET_TEST_MODE=true python ingest.py

# 4. Configure & run agent (in a new terminal)
cd ../sjsu-it-agent
cp ../.env.example .env
# Edit .env (same values minus Sheet vars)

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

## Roadmap

| Phase    | Status           | Description                                            |
|----------|------------------|--------------------------------------------------------|
| **v1**   | Demoed           | RAG agent, 129 pages indexed, Sheet integration code   |
| **v1.1** | Pending handoff  | Cloud Run deploy, daily auto-sync, real Sheet auth     |
| **v2**   | Planned          | Embed in Gravity / ServiceNow (single source for SAs)  |
| **v3**   | Planned          | Analytics dashboard, feedback loop, KB gap detection   |

---

## Status

- ✅ End-to-end ingestion pipeline (Confluence + Sheet + corpus refresh)
- ✅ Idempotent daily sync (delete + reimport pattern)
- ✅ Grounded answers with strict citation rules
- ✅ Streamlit UI with streaming responses (sub-2s perceived latency)
- ✅ Sheet integration validated via `SHEET_TEST_MODE` mock entry
- ⏳ Production Cloud Run deployment (handoff to senior dev)
- ⏳ Real Sheets API auth (requires service account from GCP admin)
- ⏳ Cloud Scheduler daily trigger

---

## Built by

**Saibhavana Aluri** · SJSU IT Service Desk · May 2026
