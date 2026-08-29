"""FastAPI application setup for the agent service."""

import asyncio
from fastapi import FastAPI, HTTPException, Response
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

import metrics
from config.settings import get_settings
from k8fy import dependency_miner
from k8fy.agent import get_chat_agent, refresh_pricing_from_backend
from k8fy.live_diagnostics import LIVE_DIAGNOSTIC_TOOLS
from k8fy.skills.router import get_skill_router
from k8fy.tools import process_tool_call
from models.response import AgentResponse, QueryRequest

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()

# Create FastAPI app
app = FastAPI(title="agentify-agent", version="0.1.0")

# Glue-based dependency miner (ADR 0029) — a periodic background task, not a
# request handler; module-level so the shutdown hook below can signal and
# await the same task the startup hook created.
_dependency_miner_shutdown = asyncio.Event()
_dependency_miner_task: Optional[asyncio.Task] = None


class ChatRequest(BaseModel):
    """Request body for the multi-turn chat endpoint."""
    messages: List[Dict[str, Any]]  # [{role: "user"|"assistant", content: "..."}]
    context: Dict[str, Any] = {}    # {namespace, service, ...}
    trace_id: Optional[str] = None


@app.on_event("startup")
async def startup_event():
    """Initialize skill router (and all sub-agents) on startup."""
    logger.info("Agent service starting up...")
    refresh_pricing_from_backend(settings.backend_url)
    get_skill_router()
    get_chat_agent()  # warm the chat agent singleton
    logger.info(f"Skill router initialized with model: {settings.claude_model}")

    global _dependency_miner_task
    athena_config = {
        "workgroup": settings.athena_workgroup,
        "database": settings.athena_database,
        "table": settings.athena_table,
        "region": settings.aws_region,
    }
    _dependency_miner_task = asyncio.create_task(
        dependency_miner.run_forever(
            settings.backend_url, athena_config, settings.dependency_mining_interval_seconds, _dependency_miner_shutdown,
        )
    )


@app.on_event("shutdown")
async def shutdown_event():
    """Signal the dependency miner's background task to stop and wait for
    its current cycle (if any) to finish — same graceful-shutdown
    convention as agentify-discovery's own SIGTERM handling."""
    _dependency_miner_shutdown.set()
    if _dependency_miner_task is not None:
        await _dependency_miner_task


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "agentify-agent"}


@app.get("/metrics")
async def metrics_endpoint():
    """Prometheus metrics: token usage + indicative cost (ADR 0011)."""
    body, content_type = metrics.exposition()
    return Response(content=body, media_type=content_type)


@app.post("/reason", response_model=AgentResponse)
async def reason(request: QueryRequest) -> AgentResponse:
    """Reason about a query and return an answer.

    Request body:
    {
      "intent": "health_check",
      "data": { ... },
      "context": { "namespace": "prod" }
    }

    Response:
    {
      "answer": "...",
      "status": "healthy",
      "confidence": 0.95,
      "sources": ["k8fy.live-state"],
      "details": { ... }
    }
    """
    # Log the propagated trace_id so the agent's reasoning correlates with the
    # backend's query.trace by the same id (spec 004).
    logger.info("reason request", extra={"trace_id": request.trace_id, "intent": request.intent})
    try:
        response = await get_skill_router().dispatch(request.intent, request.data, request.context)
        return response
    except Exception as e:
        logger.error(f"Reasoning error (trace_id=%s): %s", request.trace_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reason-chat", response_model=AgentResponse)
async def reason_chat(request: ChatRequest) -> AgentResponse:
    """Multi-turn conversational reasoning endpoint.

    Accepts the full conversation history and responds using the chat agent
    (free-form prose, agentic tool use, no JSON schema constraint).
    """
    logger.info(
        "chat request",
        extra={"trace_id": request.trace_id, "turns": len(request.messages)},
    )
    try:
        response = await get_chat_agent().reason_chat(request.messages, request.context)
        return response
    except Exception as e:
        logger.error("Chat reasoning error (trace_id=%s): %s", request.trace_id, e)
        raise HTTPException(status_code=500, detail=str(e))


class LiveToolCallRequest(BaseModel):
    """Request body for /live-tool-call — direct (non-LLM) invocation of one
    of the live-diagnostics read-only tools, used by the Chat UI's "Run"
    buttons on a recommended action."""
    tool: str
    arguments: Dict[str, Any] = {}


@app.post("/live-tool-call")
async def live_tool_call(request: LiveToolCallRequest) -> Dict[str, Any]:
    """Directly execute one live-diagnostics tool call — no LLM in the loop.

    Deliberately validates `tool` against LIVE_DIAGNOSTIC_TOOLS only (not the
    full TOOLS list) so this passthrough can never reach a mutating tool
    (e.g. rotate_vault_cert) even by accident — see live_diagnostics.py's
    module docstring for the full security rationale.
    """
    if request.tool not in LIVE_DIAGNOSTIC_TOOLS:
        raise HTTPException(status_code=400, detail=f"tool must be one of {sorted(LIVE_DIAGNOSTIC_TOOLS)}")
    logger.info("live tool call", extra={"tool": request.tool})
    result = await process_tool_call(request.tool, request.arguments, settings.backend_url)
    return {"tool": request.tool, "data": result}


class EmbedRequest(BaseModel):
    """Request body for the /embed endpoint."""
    text: str
    model: Optional[str] = None   # override settings.voyage_model if supplied


class EmbedResponse(BaseModel):
    embedding: Optional[List[float]] = None   # None when Voyage API key not set
    dim: int = 0
    model: str = ""
    available: bool = False


@app.post("/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest) -> EmbedResponse:
    """Return a vector embedding for the given text using Voyage AI.

    Used by the Go backend's async embed goroutine (P8 — semantic memory).
    Returns available=False when VOYAGE_API_KEY is not configured so the
    caller can skip vector storage without crashing.
    """
    if not settings.voyage_api_key:
        return EmbedResponse(available=False, model="", dim=0)
    try:
        import voyageai
        model = request.model or settings.voyage_model
        client = voyageai.Client(api_key=settings.voyage_api_key)
        result = client.embed([request.text], model=model)
        vec = result.embeddings[0]
        return EmbedResponse(embedding=vec, dim=len(vec), model=model, available=True)
    except Exception as exc:
        logger.warning("embed failed: %s", exc)
        return EmbedResponse(available=False, model="", dim=0)


@app.get("/")
async def root():
    """API root."""
    return {
        "service": "agentify-agent",
        "version": "0.1.0",
        "endpoints": [
            "/health",
            "/reason",
        ],
    }
