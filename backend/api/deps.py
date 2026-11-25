from fastapi import HTTPException, Request

from backend.context import AppContext


def ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def require_device(c: AppContext, device_id: str) -> dict:
    d = c.devices.get(device_id)
    if not d:
        raise HTTPException(404, f"Unknown device '{device_id}'")
    return d
