"""Webhook routes extracted from app.api."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from zero.adapters.messaging import UnsupportedUpdateError, WebhookAuthError
from zero.app.interface_transport_service import (
    InterfaceScopeError,
    InterfaceTransportNotConfigured,
)
from zero.app.routers.interface import _interface_event_payload
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)
from zero.domain.interfaces import InterfaceBindingId


def register_webhook_routes(app: FastAPI, services: Services) -> None:
    @app.post("/webhooks/{platform}/{project_id}/{binding_id}", tags=["webhooks"])
    async def receive_webhook(
        platform: str,
        project_id: str,
        binding_id: str,
        request: Request,
    ) -> JSONResponse:
        transport_service = services.interface_transports
        if transport_service is None:
            raise HTTPException(status_code=503, detail="interface transport unavailable")
        if platform not in {"telegram", "discord"}:
            raise HTTPException(status_code=404, detail="interface platform not found")
        body = await request.body()
        try:
            result = await run_in_threadpool(
                transport_service.process_webhook,
                platform=platform,  # type: ignore[arg-type]
                project_id=ProjectId(project_id),
                binding_id=InterfaceBindingId(binding_id),
                body=body,
                headers=dict(request.headers),
            )
        except WebhookAuthError as exc:
            raise HTTPException(status_code=401, detail="webhook authentication failed") from exc
        except InterfaceTransportNotConfigured as exc:
            raise HTTPException(
                status_code=503, detail="webhook verification is not configured"
            ) from exc
        except InterfaceScopeError as exc:
            raise HTTPException(status_code=404, detail="interface binding not found") from exc
        except UnsupportedUpdateError as exc:
            raise HTTPException(status_code=400, detail="unsupported webhook payload") from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="interface binding not found") from exc
        if isinstance(result, dict):
            return JSONResponse(content=result)
        if result is None:
            return JSONResponse(content={"status": "accepted"})
        return JSONResponse(content=_interface_event_payload(result))
