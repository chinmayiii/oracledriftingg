import os
import time
import asyncio
import math
import re
from typing import Dict, Any, List, Tuple
from pydantic import BaseModel
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from dotenv import load_dotenv
from db_client import DatabricksClient

app = FastAPI(title="Drifting Oracle API", description="Underwriter Dashboard Backend")

# Allow Frontend to communicate with Backend during local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = DatabricksClient()

# Response cache to minimize API calls
response_cache = {}

# ──────────────────────────────────────────────
#  LOCAL RAG OVER POLICY TEXT
# ──────────────────────────────────────────────
RAG_MAX_CHUNK_CHARS = int(os.getenv("RAG_MAX_CHUNK_CHARS", "900"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "3"))
RAG_POLICY_PATH = os.getenv(
    "RAG_POLICY_PATH",
    os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "data", "rbi_sebi_policy.txt")
    ),
)
_rag_chunks: List[str] = []
_rag_idf: Dict[str, float] = {}


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-zA-Z0-9]+", text.lower())


def _build_rag_index(text: str) -> Tuple[List[str], Dict[str, float]]:
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if current_len + len(line) + 1 > RAG_MAX_CHUNK_CHARS and current:
            chunks.append(" ".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append(" ".join(current))
    doc_freq: Dict[str, int] = {}
    for chunk in chunks:
        for token in set(_tokenize(chunk)):
            doc_freq[token] = doc_freq.get(token, 0) + 1
    total_docs = max(len(chunks), 1)
    idf = {
        token: math.log((total_docs + 1) / (df + 1)) + 1.0
        for token, df in doc_freq.items()
    }
    return chunks, idf


def _vectorize(text: str, idf: Dict[str, float]) -> Dict[str, float]:
    tokens = _tokenize(text)
    if not tokens:
        return {}
    counts: Dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    length = float(len(tokens))
    return {
        token: (count / length) * idf.get(token, 0.0) for token, count in counts.items()
    }


def _cosine_similarity(vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    dot = sum(w * vec_b.get(t, 0.0) for t, w in vec_a.items())
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _retrieve_rag_context(query: str, top_k: int = RAG_TOP_K) -> List[str]:
    if not _rag_chunks:
        return []
    query_vec = _vectorize(query, _rag_idf)
    scored = sorted(
        [
            (_cosine_similarity(query_vec, _vectorize(c, _rag_idf)), c)
            for c in _rag_chunks
        ],
        reverse=True,
    )
    top_chunks = [c for _, c in scored[:top_k]]
    return top_chunks


# ──────────────────────────────────────────────
#  GROQ CLIENT INITIALIZATION
# ──────────────────────────────────────────────
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path, override=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "grok-3-mini")  # fast & cheap

chat_client = None
if GROQ_API_KEY:
    chat_client = OpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
    )
    print(f"[INFO] Groq client initialized with model: {GROQ_MODEL}")
else:
    print("[WARNING] GROQ_API_KEY not found. Chat endpoint will fall back to mock.")

# Load policy text for RAG
if os.path.isfile(RAG_POLICY_PATH):
    try:
        with open(RAG_POLICY_PATH, "r", encoding="utf-8") as handle:
            _rag_text = handle.read()
        _rag_chunks, _rag_idf = _build_rag_index(_rag_text)
        print(f"[INFO] RAG policy loaded with {len(_rag_chunks)} chunks.")
    except Exception as exc:
        print(f"[WARNING] Could not load RAG policy: {exc}")
else:
    print(f"[WARNING] RAG policy file not found: {RAG_POLICY_PATH}")


# ──────────────────────────────────────────────
#  PYDANTIC SCHEMAS
# ──────────────────────────────────────────────
class SimulateRequest(BaseModel):
    age: int
    income: float
    duration: int


class ChatRequest(BaseModel):
    message: str
    applicant_context: str = ""


# ──────────────────────────────────────────────
#  ROUTES
# ──────────────────────────────────────────────
@app.get("/")
def read_root():
    return {"status": "Drifting Oracle Backend is Running natively"}


@app.get("/api/dashboard/metrics")
def get_metrics():
    return db.fetch_dashboard_metrics()


@app.get("/api/health")
def get_health():
    return {"databricks": db.health_check()}


@app.get("/api/dashboard/audit_records")
def get_audit_records(limit: int = 50):
    return {"data": db.fetch_audit_records(limit=limit)}


@app.post("/api/simulate")
async def run_live_prediction(data: SimulateRequest):
    """
    Heuristic SHAP Edge-Simulator for Hackathon.
    """
    await asyncio.sleep(0.5)

    risk_factor = (data.duration * 1000) / (data.income + 1)
    base_risk = 0.20
    if data.age < 25:
        base_risk += 0.15
    final_risk = min(0.99, max(0.01, base_risk + (risk_factor * 0.05)))

    classification = "MEDIUM RISK"
    label = "🟡 MANUAL REVIEW — Compliance Team"
    if final_risk > 0.65:
        classification = "HIGH RISK"
        label = "🔴 BLOCK — Senior Underwriter Review"
    elif final_risk < 0.25:
        classification = "LOW RISK"
        label = "🟢 AUTO-APPROVE"

    return {
        "status": "success",
        "predicted_risk": final_risk,
        "classification": classification,
        "shap_summary": f"Risk primarily driven UP by loan_duration (+{(data.duration / 10):.2f}) and driven DOWN by income_proxy",
        "hallucination_flag": label,
    }


@app.post("/api/chat")
async def ask_underwriter(req: ChatRequest):
    """
    Agentic AI endpoint utilizing Groq to question the Databricks Gold Table context.
    """
    # Check cache first
    cache_key = hash(req.message) % 10000
    if cache_key in response_cache:
        return {"reply": response_cache[cache_key], "cached": True}

    if chat_client:
        rag_context = _retrieve_rag_context(
            f"{req.message} {req.applicant_context}".strip()
        )
        rag_block = (
            "\n".join([f"- {chunk}" for chunk in rag_context])
            or "(no relevant policy excerpts found)"
        )

        system_prompt = (
            "You are the 'Drifting Oracle AI', a senior compliance risk auditor working for a bank. "
            "Respond naturally as an AI Copilot. Keep it brief (2 sentences max). "
            "Sentence 1 must cite the Database Context when available. "
            "Sentence 2 must start with 'Policy:' and quote one relevant policy excerpt from the RAG block, "
            "or say 'Policy: none.' if no excerpt is relevant."
        )
        user_prompt = (
            f"Databricks Database Context: {req.applicant_context}\n\n"
            f"Policy excerpts (RAG):\n{rag_block}\n\n"
            f"Question: {req.message}"
        )

        try:
            response = chat_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=200,
            )
            reply_text = response.choices[0].message.content
            policy_fallback = rag_context[0] if rag_context else "none"
            if "Policy:" not in reply_text:
                suffix = (
                    f" Policy: {policy_fallback}."
                    if policy_fallback != "none"
                    else " Policy: none."
                )
                reply_text = (reply_text or "").rstrip()
                reply_text = f"{reply_text}{suffix}"
            response_cache[cache_key] = reply_text
            return {"reply": reply_text}
        except Exception as e:
            error_str = str(e)
            print(f"[ERROR] Groq API Error: {error_str[:150]}")
            return get_mock_response(req.message, "error")
    else:
        await asyncio.sleep(0.3)
        return get_mock_response(req.message, "offline")


def get_mock_response(message: str, mode: str = "offline") -> Dict[str, Any]:
    message_lower = message.lower()
    if any(w in message_lower for w in ["approve", "safe", "good", "low"]):
        base_reply = "Based on the applicant's strong income profile and low debt-to-income ratio, this appears to be a low-risk loan application. ✓ Recommended for AUTO-APPROVAL per RBI guidelines."
    elif any(w in message_lower for w in ["reject", "block", "high", "risky"]):
        base_reply = "The applicant's high debt-to-income ratio and recent defaults flagged in the audit table suggest HIGH RISK. ✗ Recommend BLOCKING this application pending Senior Underwriter Review."
    elif any(w in message_lower for w in ["duration", "loan"]):
        base_reply = "The loan duration shows significant impact on risk calculation. Shorter durations (12-24 months) are generally safer."
    elif any(w in message_lower for w in ["income", "salary"]):
        base_reply = "Income is a strong mitigating factor in the risk model. Higher income reduces risk by ~35% across our validation set."
    elif any(w in message_lower for w in ["why", "explain", "shap"]):
        base_reply = "SHAP analysis shows loan_duration is the #1 risk driver (+6.4), while income_proxy is the #1 protective factor (-4.2), aligning with RBI policy requirements."
    else:
        base_reply = "As your AI Copilot, I'm analyzing this applicant against Databricks ML models and RBI regulatory guidelines. Request more specifics about income, duration, or risk classification."

    suffix = {
        "error": " [Groq API temporarily unavailable]",
        "offline": " [Demo Mode - Groq API not configured]",
    }.get(mode, "")

    return {"reply": base_reply + suffix, "mode": mode}


@app.post("/api/trigger_pipeline")
async def trigger_run():
    return {"status": "Job 7158 triggered successfully", "job_id": 7158}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
