"""The single multiplexed WebSocket transport."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.ws.broker import (
    BrokerConnection,
    EventBroker,
    InvalidTopicError,
    ReplayGapError,
)

router = APIRouter()


class SubscribeFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["subscribe"]
    topic: str
    stream: str | None = None
    after: int | None = Field(default=None, ge=0)


class UnsubscribeFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["unsubscribe"]
    topic: str


ClientFrame = Annotated[SubscribeFrame | UnsubscribeFrame, Field(discriminator="type")]
client_frame_adapter: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)


@router.websocket("/ws")
async def websocket_events(websocket: WebSocket) -> None:
    """Receive topic controls while a single task serializes all output."""
    await websocket.accept()
    broker: EventBroker = websocket.app.state.broker
    async with broker.connection() as connection:
        sender = asyncio.create_task(_send_frames(websocket, connection))
        try:
            while True:
                try:
                    raw = await websocket.receive_json()
                    frame = client_frame_adapter.validate_python(raw)
                    if isinstance(frame, SubscribeFrame):
                        await broker.subscribe(
                            connection,
                            frame.topic,
                            stream=frame.stream,
                            after=frame.after,
                        )
                    else:
                        await broker.unsubscribe(connection, frame.topic)
                except (ValidationError, InvalidTopicError, ValueError) as error:
                    await broker.notify(
                        connection,
                        {
                            "type": "error",
                            "code": "invalid_frame",
                            "message": str(error),
                        },
                    )
                except ReplayGapError as error:
                    await broker.notify(
                        connection,
                        {
                            "type": "error",
                            "code": "history_gap",
                            "message": str(error),
                        },
                    )
        except WebSocketDisconnect:
            pass
        finally:
            await broker.disconnect(connection)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                await sender


async def _send_frames(websocket: WebSocket, connection: BrokerConnection) -> None:
    # Kept separate so receiver and publisher never concurrently call send_json.
    while True:
        message = await connection.receive()
        if message is None:
            await websocket.close(code=1013, reason="subscriber queue overflow")
            return
        await websocket.send_json(message)


__all__ = ["router"]
