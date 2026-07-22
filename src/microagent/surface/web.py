"""Minimal FastAPI web server for MicroAgent.

Requires: pip install microagent[web] (fastapi + uvicorn)

Usage: microagent-web
"""

from __future__ import annotations

import json
import os
import sys

from ..agent import Agent
from ..core.types import Message, TurnComplete, TurnFailed, TextDelta
from ..llm.client import LLMConfig


def create_app(agent: Agent):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import StreamingResponse
    except ImportError:
        print("fastapi not installed. Install with: pip install microagent[web]")
        sys.exit(1)

    app = FastAPI(title="MicroAgent")

    @app.post("/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: dict):
        prompt = body.get("content", "")
        if not prompt:
            raise HTTPException(status_code=400, detail="content is required")

        messages = [Message.user(prompt)]

        async def stream_response():
            async for event in agent.runner.run_turn(messages):
                if isinstance(event, TextDelta):
                    yield f"data: {json.dumps({'type': 'delta', 'text': event.text})}\n\n"
                elif isinstance(event, TurnComplete):
                    yield f"data: {json.dumps({'type': 'complete', 'content': event.content})}\n\n"
                elif isinstance(event, TurnFailed):
                    yield f"data: {json.dumps({'type': 'error', 'reason': event.reason})}\n\n"

        return StreamingResponse(stream_response(), media_type="text/event-stream")

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        return {"session_id": session_id, "status": "active"}

    return app


def main():
    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. Install with: pip install microagent[web]")
        sys.exit(1)

    base_url = os.environ.get("MICROAGENT_BASE_URL", "https://api.openai.com/v1")
    api_key = os.environ.get("MICROAGENT_API_KEY", "")
    model = os.environ.get("MICROAGENT_MODEL", "gpt-4o")

    config = LLMConfig(base_url=base_url, api_key=api_key, model=model)
    agent = Agent.from_config(config)
    app = create_app(agent)

    port = int(os.environ.get("MICROAGENT_WEB_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
