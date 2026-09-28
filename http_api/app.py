"""HTTP adapter — the third transport over the same tool core.

WHY THIS EXISTS

cloudprice-mcp already speaks two protocols, and both are thin wrappers around
exactly one thing:

    cloudprice_mcp.dispatch.call(tool_name, arguments)
      ├── server.py              stdio      (Claude Desktop, Cursor)
      └── gateway_lambda/handler AWS Lambda (Bedrock AgentCore gateway)

Kubernetes needs a third, because the stdio server cannot be a Deployment in any
useful sense. A stdio MCP server is spawned per client as a subprocess and talks
over stdin/stdout; it listens on nothing. A Service in front of it would have no
port to route to, and a liveness probe would have nothing to ask. So rather than
forcing that shape into a pod, this adds an HTTP adapter of the same kind as the
Lambda one: no business logic, just transport.

WHAT IS DELIBERATELY NOT HERE

No pricing logic, no catalogue handling, no tool implementations. If a behaviour
needs changing it belongs in dispatch or below, so that all three transports get
it at once. The moment this file starts making decisions, the three transports
start disagreeing — which is the exact failure the four portfolio chatbots hit
when each one carried its own copy of the system prompt.

It also does NOT import `mcp`. dispatch has no MCP dependency, so the container
ships without the SDK: a smaller image and a smaller attack surface for
something that will sit on a public-ish cluster.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cloudprice_mcp import dispatch

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cloudprice.http")

app = FastAPI(
    title="cloudprice-mcp HTTP API",
    description=(
        "HTTP transport over the cloudprice-mcp tool core. Same tools as the "
        "stdio MCP server and the AWS Lambda gateway."
    ),
    version=os.environ.get("APP_VERSION", "0.0.0-dev"),
)

_STARTED = time.time()


class ToolCall(BaseModel):
    """A single tool invocation."""

    tool: str = Field(..., description="Tool name, as returned by GET /tools")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Tool arguments"
    )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness. Cheap on purpose.

    Answers only "is this process alive". It must NOT touch the catalogue or do
    any real work: a liveness probe that exercises the app will, under load,
    time out and have Kubernetes restart a pod that was merely busy. That turns
    a slow service into a crash loop.
    """
    return {"status": "ok", "uptime_seconds": round(time.time() - _STARTED, 1)}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness. Deliberately DOES do real work.

    This is the probe that should fail when the pod cannot serve, so Kubernetes
    takes it out of the Service rather than sending it traffic it will error on.
    Loading the catalogue is the one thing that must work before any tool call
    can succeed, and it is the thing most likely to be broken in a bad image -
    the price data is packaged data files, and a Dockerfile that copies source
    but misses the data directory produces a container that starts happily and
    fails every request.
    """
    try:
        catalog = dispatch.load_catalog()
        if not catalog:
            raise RuntimeError("catalogue loaded but empty")
    except Exception:
        # The detail goes to the LOG, not to the response body.
        #
        # An earlier version returned str(exc) to the caller, and CodeQL was
        # right to flag it (py/stack-trace-exposure): the exception text from a
        # failed catalogue load carries filesystem paths and package internals,
        # and /readyz is reachable by anything that can reach the pod. An
        # operator reading `kubectl logs` gets the whole traceback; a caller
        # gets only the fact that the pod cannot serve, which is all a probe
        # needs to decide anything.
        log.exception("readiness check failed")
        return JSONResponse(
            status_code=503,
            content={"status": "not-ready", "error": "catalogue unavailable"},
        )
    return JSONResponse(content={"status": "ready", "tools": len(dispatch.tool_names())})


@app.get("/tools")
def tools() -> dict[str, Any]:
    """Every tool this build can run."""
    names = dispatch.tool_names()
    return {"count": len(names), "tools": names}


@app.post("/call")
def call(req: ToolCall) -> Any:
    """Run one tool.

    An unknown tool is a 404 rather than dispatch's {"error": ...} payload,
    because an HTTP caller has status codes and should get one. dispatch keeps
    returning a payload for the MCP and Lambda paths, where a model reads the
    text and a stack trace would be worse than a message.

    A tool that raises is NOT swallowed into a friendly 200. Every chatbot in
    this account was at some point broken while reporting success, because each
    wrapped its handler in `except Exception` and answered "something went
    wrong" - two of them were dead for weeks without incrementing an error
    metric. Here a failure is a 500, so it shows up in probes, logs and metrics.
    """
    if req.tool not in dispatch.tool_names():
        raise HTTPException(
            status_code=404,
            detail={"error": f"Unknown tool: {req.tool}", "available": dispatch.tool_names()},
        )

    started = time.perf_counter()
    result = dispatch.call(req.tool, req.arguments)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    log.info("tool=%s ok elapsed_ms=%s", req.tool, elapsed_ms)
    return result


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "cloudprice-mcp",
        "transport": "http",
        "version": app.version,
        "docs": "/docs",
        "endpoints": ["/healthz", "/readyz", "/tools", "/call"],
    }
