# -*- coding: utf-8 -*-
"""
FastAPI application wrapping the existing Agent class.

The Agent is transport-agnostic: it owns sessions and controllers, and every
public method returns a dict. This module adds the HTTP/WebSocket surface that
the browser panel speaks to.

Design decisions:
- REST endpoints mirror the existing /v1/* routes so the CLI client still works.
- A WebSocket endpoint streams events in real time for the web panel, replacing
  the long-poll that was adequate at human speed but jarring in a browser.
- Static files for the SPA are mounted at the root.
- Bearer-token auth is enforced via a FastAPI dependency, identical to the
  original check but idiomatic.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import (
    Depends, FastAPI, HTTPException, Query, Request, WebSocket,
    WebSocketDisconnect, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from qat_recorder.agent.protocol import (
    AgentError, Busy, Command, PROTOCOL_VERSION, envelope,
)
from qat_recorder.agent.security import parse_bearer, token_matches

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class StartSessionRequest(BaseModel):
    app: str = ""
    lib: str = ""
    name: str = ""
    owner: str = ""


class CommandRequest(BaseModel):
    command: str = ""
    args: dict = {}


class KeepRequest(BaseModel):
    name: str = ""
    verify: bool = True


class ArtifactsRequest(BaseModel):
    custom_script: Optional[str] = None


class ReplayRequest(BaseModel):
    timeout: float = 0.0


class RunTestRequest(BaseModel):
    test: str = ""
    timeout: float = 0.0


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
_FRONTEND_DEV_DIR = Path(__file__).parent / "frontend"  # for dev reference


def create_app(agent, token: str = "") -> FastAPI:
    """Build a FastAPI app wired to a live Agent instance.

    `agent` is the same `qat_recorder.agent.server.Agent` that the old
    `AgentServer` used — all recording logic lives there, unchanged.
    """
    app = FastAPI(
        title="QAT Recorder",
        version="2.0.0",
        docs_url=None,      # Swagger UI not needed for this internal tool
        redoc_url=None,
    )
    app.state.agent = agent
    app.state.token = token

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- auth dependency ---------------------------------------------------

    async def verify_token(request: Request) -> None:
        header = request.headers.get("Authorization") or ""
        presented = parse_bearer(header)
        # If no token is configured (e.g. local-only mode), skip auth
        if app.state.token and not token_matches(app.state.token, presented):
            raise HTTPException(status_code=401, detail="unauthorised")

    Authenticated = Depends(verify_token)

    # -- helpers -----------------------------------------------------------

    def _envelope(payload: dict) -> dict:
        return envelope(payload)

    def _handle(func, *args, **kwargs):
        """Call an Agent method and translate AgentError to HTTP status."""
        try:
            return _envelope(func(*args, **kwargs))
        except Busy as error:
            raise HTTPException(status_code=409, detail={
                "error": str(error),
                "owner": error.owner,
                "since": error.since,
                "session_id": error.session_id,
            })
        except AgentError as error:
            raise HTTPException(
                status_code=error.status or 500,
                detail={"error": str(error), "detail": error.detail},
            )
        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail={"error": f"internal error: {error}"},
            )

    # -- REST endpoints (same /v1/* routes as the old server) --------------

    @app.get("/v1/health")
    async def health(_auth=Authenticated):
        return _handle(agent.health)

    @app.get("/v1/applications")
    async def applications(_auth=Authenticated):
        return _handle(agent.applications)

    @app.get("/v1/sessions")
    async def current_session(_auth=Authenticated):
        return _handle(agent.current)

    @app.post("/v1/sessions", status_code=201)
    async def start_session(body: StartSessionRequest, _auth=Authenticated):
        return _handle(
            agent.start_session,
            app=body.app, lib=body.lib, name=body.name, owner=body.owner,
        )

    @app.get("/v1/sessions/{session_id}/events")
    async def events(
        session_id: str,
        since: int = Query(0),
        wait: float = Query(5.0),
        _auth=Authenticated,
    ):
        # Run the potentially-blocking long poll in a thread
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: _handle(agent.events, session_id, since, wait)
        )

    @app.post("/v1/sessions/{session_id}/command")
    async def command(session_id: str, body: CommandRequest, _auth=Authenticated):
        return _handle(agent.command, session_id, body.command, body.args)

    @app.post("/v1/sessions/{session_id}/artifacts")
    async def artifacts(session_id: str, body: Optional[ArtifactsRequest] = None, _auth=Authenticated):
        custom_script = body.custom_script if body else None
        return _handle(agent.artifacts, session_id, custom_script=custom_script)

    @app.get("/v1/sessions/{session_id}/preview")
    async def preview(session_id: str, _auth=Authenticated):
        return _handle(agent.preview, session_id)

    @app.post("/v1/sessions/{session_id}/replay")
    async def replay(session_id: str, body: ReplayRequest, _auth=Authenticated):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: _handle(agent.replay, session_id, body.timeout)
        )

    @app.post("/v1/sessions/{session_id}/keep", status_code=201)
    async def keep(session_id: str, body: KeepRequest, _auth=Authenticated):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: _handle(agent.keep, session_id, body.name, body.verify)
        )

    @app.delete("/v1/sessions/{session_id}")
    async def release(session_id: str, _auth=Authenticated):
        return _handle(agent.release, session_id)

    @app.get("/v1/tests")
    async def tests(app_name: str = Query("", alias="app"), _auth=Authenticated):
        return _handle(agent.tests, app_name)

    @app.post("/v1/tests/run")
    async def run_test(body: RunTestRequest, _auth=Authenticated):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: _handle(agent.run_test, body.test, body.timeout)
        )

    # -- WebSocket for real-time event streaming ---------------------------

    @app.websocket("/ws/events/{session_id}")
    async def ws_events(ws: WebSocket, session_id: str):
        # Authenticate via query param (browsers can't set WS headers easily)
        ws_token = ws.query_params.get("token", "")
        if app.state.token and not token_matches(app.state.token, ws_token):
            await ws.close(code=4001, reason="unauthorised")
            return

        await ws.accept()
        cursor = 0
        try:
            while True:
                try:
                    data = agent.events(session_id, since=cursor, wait=1.0)
                except AgentError:
                    await asyncio.sleep(1.0)
                    continue
                except Exception:
                    await asyncio.sleep(2.0)
                    continue

                cursor = int(data.get("next", cursor))
                # Always send state so the UI stays in sync
                await ws.send_json({
                    "type": "events",
                    "state": data.get("state", ""),
                    "actions": data.get("actions", []),
                    "picked": data.get("picked"),
                    "errors": data.get("errors", []),
                    "summary": data.get("summary", {}),
                    "next": cursor,
                })
                await asyncio.sleep(0.15)
        except WebSocketDisconnect:
            pass
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass

    # -- Web panel page (SPA) ----------------------------------------------

    @app.get("/")
    async def index():
        return FileResponse(_STATIC_DIR / "index.html")

    # Mount static files AFTER explicit routes so they don't shadow them
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

def run_server(
    agent,
    token: str = "",
    host: str = "127.0.0.1",
    port: int = 8765,
    ngrok: bool = False,
    ngrok_authtoken: str = "",
    ngrok_domain: str = "",
    verbose: bool = False,
) -> None:
    """Start the FastAPI server, optionally with an ngrok tunnel."""
    import uvicorn

    app = create_app(agent, token=token)

    ngrok_url = None
    ngrok_tunnel = None

    if ngrok:
        try:
            from pyngrok import ngrok as pyngrok_module, conf

            if ngrok_authtoken:
                conf.get_default().auth_token = ngrok_authtoken

            ngrok_tunnel = pyngrok_module.connect(port, "http",
                                                   subdomain=ngrok_domain or None)
            ngrok_url = ngrok_tunnel.public_url
            # Ensure HTTPS
            if ngrok_url.startswith("http://"):
                ngrok_url = ngrok_url.replace("http://", "https://", 1)

            print(f"\n  ngrok tunnel: {ngrok_url}")
            print(f"  Open this URL in a browser to use the panel.\n")
        except ImportError:
            print("pyngrok is not installed. Install it with: pip install pyngrok",
                  flush=True)
            raise SystemExit(2)
        except Exception as error:
            print(f"ngrok tunnel failed: {error}", flush=True)
            raise SystemExit(2)

    print(f"qat-recorder-web on {host}:{port} (host {agent.host_name})")
    if not ngrok:
        print(f"  Open http://{host}:{port} in a browser to use the panel.")

    log_level = "info" if verbose else "warning"
    try:
        uvicorn.run(app, host=host, port=port, log_level=log_level)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        agent.shutdown()
        if ngrok_tunnel is not None:
            try:
                from pyngrok import ngrok as pyngrok_module
                pyngrok_module.disconnect(ngrok_tunnel.public_url)
            except Exception:
                pass
