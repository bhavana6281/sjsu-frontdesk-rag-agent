"""SJSU IT Service Desk - Front Desk Knowledge Assistant"""
import os
import json
from pathlib import Path
import streamlit as st

st.set_page_config(
    page_title="SJSU IT Front Desk Assistant",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

from agent import answer

# Build page_id -> title lookup from local ingestion output
@st.cache_resource
def load_page_titles():
    titles = {}
    output_dir = Path.home() / "sjsu-confluence-ingest" / "output"
    if output_dir.exists():
        for f in output_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                titles[str(data.get("page_id", ""))] = data.get("title", "")
            except Exception:
                pass
    return titles

PAGE_TITLES = load_page_titles()

def render_sources(sources):
    if not sources:
        return
    with st.expander("📚 Cited sources", expanded=False):
        for src in sources:
            uri = src.get("uri", "")
            fallback_title = src.get("title", "Source")
            if uri.startswith("gs://"):
                page_id = uri.split("/")[-1].replace(".json", "")
                pretty = PAGE_TITLES.get(page_id) or fallback_title.replace(".json", "")
                wiki_base = os.environ.get("DOMAIN_URL", "")
                wiki_space = os.environ.get("SPACE_KEY", "")
                wiki_url = f"{wiki_base}/wiki/spaces/{wiki_space}/pages/{page_id}"
                st.markdown(f"📄 **{pretty}** — [Open in Confluence ↗]({wiki_url})")
            else:
                st.markdown(f"📄 **{fallback_title}**")

st.markdown("""<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
.stApp {background-color: #FAFAFA;}
.sjsu-header {background: linear-gradient(135deg, #0055A2 0%, #003E76 100%); padding: 1.5rem 2rem; margin: -1rem -1rem 1.5rem -1rem; border-bottom: 4px solid #E5A823; color: white;}
.sjsu-header h1 {color: white !important; margin: 0; font-size: 1.8rem; font-weight: 600;}
.sjsu-header .subtitle {color: #CFE0F0; margin-top: 0.3rem; font-size: 0.95rem;}
.sjsu-header .badge {display: inline-block; background: #E5A823; color: #1A1A1A; padding: 0.15rem 0.6rem; border-radius: 10px; font-size: 0.7rem; font-weight: 600; margin-left: 0.5rem;}
section[data-testid="stSidebar"] {background-color: white;}
.stButton button {background-color: white; border: 1px solid #0055A2; color: #0055A2; font-size: 0.85rem; padding: 0.4rem 0.8rem; border-radius: 6px; text-align: left;}
.stButton button:hover {background-color: #0055A2; color: white;}
.latency-badge {display: inline-block; background: #F0F0F0; padding: 0.15rem 0.5rem; border-radius: 10px; font-size: 0.75rem; color: #666666;}
</style>""", unsafe_allow_html=True)

st.markdown("""<div class="sjsu-header">
<h1>🎓 SJSU IT Front Desk Assistant <span class="badge">BETA</span></h1>
<div class="subtitle">Powered by Confluence SDKB · Ask in natural language · Cited sources</div>
</div>""", unsafe_allow_html=True)

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.markdown('<h3 style="text-align:center; color:#0055A2; margin-bottom:0;">🎓 San José State University</h3>', unsafe_allow_html=True)
    st.markdown('<p style="text-align:center; color:#666; font-size:0.85rem; margin-top:0.2rem;">IT Service Desk · Front Desk Assistant</p>', unsafe_allow_html=True)
    st.divider()

    st.markdown("#### 💡 Quick Questions")
    examples = [
        ("🔐 Duo Fraud Report", "How do I respond to a customer whose Duo account got a fraudulent report?"),
        ("📧 Suspended Email", "What is the procedure when an SJSU email is suspended for spamming?"),
        ("🎯 Ticket Routing", "What is the corresponding queue for classroom design tickets?"),
        ("💻 Adobe Licensing", "Where do I route Adobe Creative Cloud licensing issues?"),
        ("📱 Cisco Jabber", "How do I help a customer with a Cisco Jabber error?"),
        ("🔑 Password Reset", "How does a customer reset their SJSU password?"),
        ("🌐 VPN Issues", "Who handles VPN connection issues?"),
    ]
    for label, q in examples:
        if st.button(label, key=f"ex_{hash(q)}", width="stretch", help=q):
            st.session_state.example_question = q

    st.divider()
    st.markdown("#### 📞 Escalation Contacts")
    st.markdown("**IT Service Desk**  \n📞 (408) 924-1530  \n📍 Diaz Compean Student Union 1300\n\n**Security Incidents**  \n✉️ security@sjsu.edu\n\n**Hours**  \nMon–Fri 8am–5pm")

for msg in st.session_state.messages:
    avatar = "👤" if msg["role"] == "user" else "🎓"
    with st.chat_message(msg["role"], avatar=avatar):
        st.markdown(msg["content"])
        render_sources(msg.get("sources", []))
        if msg.get("latency_ms"):
            st.markdown(f'<span class="latency-badge">⏱ {msg["latency_ms"]} ms</span>', unsafe_allow_html=True)

prompt = None
if "example_question" in st.session_state:
    prompt = st.session_state.example_question
    del st.session_state.example_question

user_input = st.chat_input("Ask a question...")
if user_input:
    prompt = user_input

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar="👤"):
        st.markdown(prompt)

    with st.chat_message("assistant", avatar="🎓"):
        # Stream the response token-by-token for fast perceived latency
        from agent import answer_stream

        placeholder = st.empty()
        full_text = ""
        final_sources = []
        final_latency = 0

        with st.spinner("🔍 Searching SDKB..."):
            for chunk_text, sources, latency_ms in answer_stream(prompt):
                if chunk_text:
                    full_text += chunk_text
                    placeholder.markdown(full_text + " ▌")
                if sources is not None:
                    final_sources = sources
                if latency_ms is not None:
                    final_latency = latency_ms

        placeholder.markdown(full_text)
        render_sources(final_sources)
        st.markdown(f'<span class="latency-badge">⏱ {final_latency} ms</span>', unsafe_allow_html=True)
        st.session_state.messages.append({
            "role": "assistant",
            "content": full_text,
            "sources": final_sources,
            "latency_ms": final_latency,
        })
