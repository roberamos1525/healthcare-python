"""Publishing of outbox events: to a durable RabbitMQ topic exchange with publisher confirms, or to the log."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

_LOGGER = logging.getLogger("healthcare.messaging")


@dataclass(frozen=True, slots=True)
class Event:
    """One outbox event to publish."""

    id: UUID
    event_type: str
    payload: bytes
    occurred_at: datetime


class Publisher(Protocol):
    """Publishes one event; raises when the broker does not confirm it."""

    async def publish(self, event: Event) -> None:
        """Publish the event."""
        ...

    async def close(self) -> None:
        """Release the broker connection."""
        ...


class LogPublisher:
    """Acknowledges events and logs their type and identifier only; payloads are never logged."""

    async def publish(self, event: Event) -> None:
        """Log the event type and identifier."""
        _LOGGER.info("Dispatched event", extra={"eventType": event.event_type, "eventId": str(event.id)})

    async def close(self) -> None:
        """Release nothing."""


class RabbitMqPublisher:
    """Publishes persistent JSON messages; the event type is the routing key and the event id the message id."""

    def __init__(self, uri: str, exchange: str) -> None:
        """Create the publisher; the connection is opened on first use."""
        self._uri = uri
        self._exchange_name = exchange
        self._connection: Any = None
        self._exchange: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_exchange(self) -> Any:
        """Connect, open a confirming channel and declare the exchange on first use."""
        if self._exchange is not None and self._connection is not None and not self._connection.is_closed:
            return self._exchange
        if not self._uri.strip():
            raise RuntimeError("Messaging:RabbitMqUri must be configured when the provider is RabbitMq.")
        import aio_pika

        self._connection = await aio_pika.connect_robust(
            self._uri, client_properties={"connection_name": "healthcare-api"}
        )
        channel = await self._connection.channel(publisher_confirms=True)
        self._exchange = await channel.declare_exchange(self._exchange_name, aio_pika.ExchangeType.TOPIC, durable=True)
        _LOGGER.info("Connected to the message broker")
        return self._exchange

    async def publish(self, event: Event) -> None:
        """Send the event and wait for the broker's confirmation; a failed connection is re-opened next time."""
        import aio_pika

        async with self._lock:
            try:
                exchange = await self._ensure_exchange()
                await exchange.publish(
                    aio_pika.Message(
                        body=event.payload,
                        message_id=str(event.id),
                        type=event.event_type,
                        content_type="application/json",
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        timestamp=event.occurred_at,
                    ),
                    routing_key=event.event_type,
                )
            except Exception:
                await self._reset()
                raise

    async def _reset(self) -> None:
        """Drop the current connection after a failure."""
        connection, self._connection, self._exchange = self._connection, None, None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                _LOGGER.debug("Closing a failed broker connection raised an error")

    async def close(self) -> None:
        """Close the connection during shutdown."""
        async with self._lock:
            await self._reset()


def create_publisher(provider: str, uri: str, exchange: str) -> Publisher:
    """Select the publisher: ``RabbitMq`` publishes to a broker; anything else logs only."""
    if provider.strip().lower() == "rabbitmq":
        return RabbitMqPublisher(uri, exchange)
    return LogPublisher()
