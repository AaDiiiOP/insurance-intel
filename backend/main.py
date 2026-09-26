"""
Insurance Intelligence — Backend (Phase 1)

Phase 1 does three things:
  1. Accept uploaded insurer / broker documents and parse them to text.
  2. Summarise them with a local model into a structured briefing —
     company overview, requirements, plans, exclusions, key facts.
  3. Store all of it in Postgres, and serve it to the UI.

Everything uploaded here is the CARRIER's own paperwork. Real customer data
does not enter this system until Phase 2, and will arrive through its own
tables and a tokenisation layer rather than through this upload path.

This module is now just the FastAPI app shell: CORS config, startup schema
init, and wiring together the routers defined under api/. Every endpoint's
actual logic lives in api/<feature>.py; see that package for the carrier
briefing, client, matching, gap-analysis, drafting, feedback and document
intelligence routes.
"""
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import db
from api import (
    ask,
    carriers,
    clients,
    doc_intelligence,
    drafting,
    feedback,
    gaps,
    health,
    knowledge,
    matching,
    organizations,
    submissions,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Create tables on startup.

    Postgres may still be booting when the backend starts, so this retries
    rather than crashing the container on the first failed connection.
    """
    for attempt in range(1, 11):
        try:
            db.init_schema()
            logger.info("Database schema ready")
            break
        except Exception as exc:
            logger.warning("Schema init attempt %s/10 failed: %s", attempt, exc)
            time.sleep(2)
    else:
        logger.error("Could not initialise schema — /health will show the error")
    yield


app = FastAPI(title="Insurance Intelligence API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Read from the environment so a deployed instance can allow its own
    # public address without a code change. A fixed localhost-only list
    # works locally but silently blocks every request once the app is
    # reachable from a real IP or domain — which looks exactly like a
    # backend outage from the browser's side, even though /health is fine.
    #
    # Comma-separated, so a server can allow both an IP and a domain during
    # a DNS cutover.
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS", "http://localhost:5183,http://localhost:3000"
        ).split(",")
        if origin.strip()
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(organizations.router)
app.include_router(carriers.router)
app.include_router(clients.router)
app.include_router(matching.router)
app.include_router(gaps.router)
app.include_router(drafting.router)
app.include_router(feedback.router)
app.include_router(ask.router)
app.include_router(submissions.router)
app.include_router(doc_intelligence.router)
app.include_router(knowledge.router)
