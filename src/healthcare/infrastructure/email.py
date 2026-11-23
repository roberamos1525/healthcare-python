"""Account emails: composed on request, queued in memory, sent by a background worker via SMTP, files or nowhere.

Messages never contain health information, and recipients are never logged.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Protocol

import aiosmtplib

from healthcare.application.security import escape_data_string
from healthcare.infrastructure.config import EmailSettings

_LOGGER = logging.getLogger("healthcare.email")


@dataclass(frozen=True, slots=True)
class Message:
    """A plain-text email."""

    to: str
    subject: str
    body: str


class Sender(Protocol):
    """Delivers one email."""

    async def send(self, message: Message) -> None:
        """Deliver the message or raise."""
        ...


def build_message(message: Message, settings: EmailSettings) -> EmailMessage:
    """Render the MIME message (plain text, UTF-8)."""
    mime = EmailMessage()
    mime["From"] = f'"{settings.from_name}" <{settings.from_address}>'
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime["Date"] = format_datetime(datetime.now(UTC))
    host = settings.from_address.rpartition("@")[2] or "localhost"
    mime["Message-ID"] = f"<{uuid.uuid4().hex}@{host}>"
    mime.set_content(message.body, charset="utf-8", cte="quoted-printable")
    return mime


class SmtpSender:
    """Sends through an SMTP relay (for example Amazon SES SMTP, or Mailpit in development)."""

    def __init__(self, settings: EmailSettings) -> None:
        """Create the sender."""
        self._settings = settings

    async def send(self, message: Message) -> None:
        """Connect, upgrade with STARTTLS when configured, authenticate when credentials are set, and send."""
        settings = self._settings
        await aiosmtplib.send(
            build_message(message, settings),
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            start_tls=settings.smtp_use_start_tls,
            use_tls=False,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            timeout=30,
        )


class FileSender:
    """Writes each email as an ``.eml`` file, for local development without an SMTP server."""

    def __init__(self, settings: EmailSettings) -> None:
        """Create the sender."""
        self._settings = settings

    async def send(self, message: Message) -> None:
        """Save the message to the pickup directory."""
        directory = Path(self._settings.pickup_directory)
        name = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex}.eml"
        data = build_message(message, self._settings).as_bytes()
        await asyncio.to_thread(_write_file, directory, name, data)


def _write_file(directory: Path, name: str, data: bytes) -> None:
    """Create the directory and write one file (blocking)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(data)


class NullSender:
    """Discards email when no delivery method is configured."""

    async def send(self, message: Message) -> None:
        """Log that a message was suppressed, without its recipient or content."""
        _LOGGER.info("Email delivery is disabled; a message was not sent")


def create_sender(settings: EmailSettings) -> Sender:
    """Select the delivery method: ``Smtp``, ``File`` or anything else for none."""
    match settings.provider.strip().upper():
        case "SMTP":
            return SmtpSender(settings)
        case "FILE":
            return FileSender(settings)
        case _:
            return NullSender()


class MailQueue:
    """The bounded in-memory queue between request handling and the sender (not durable by design)."""

    def __init__(self, capacity: int = 1000) -> None:
        """Create the queue."""
        self.messages: asyncio.Queue[Message] = asyncio.Queue(capacity)

    def enqueue(self, message: Message) -> None:
        """Add a message, dropping it (and logging) when the queue is full."""
        try:
            self.messages.put_nowait(message)
        except asyncio.QueueFull:
            _LOGGER.error("The email queue is full; a message was dropped")


class QueueMailer:
    """Composes the account emails and places them on the queue."""

    def __init__(self, queue: MailQueue, frontend_base_url: str) -> None:
        """Create the mailer."""
        self._queue = queue
        self._base = frontend_base_url.rstrip("/")

    def _link(self, page: str, token: str) -> str:
        """Build a link to a page of the web application carrying a single-use token."""
        return f"{self._base}/{page}?token={escape_data_string(token)}"

    def queue_email_verification(self, email: str, first_name: str, token: str) -> None:
        """Queue the email verification link."""
        self._queue.enqueue(
            Message(
                email,
                "Verify your email address",
                (
                    f"Hello {first_name},\n\nPlease confirm your email address to finish setting up your account:\n\n"
                    f"{self._link('verify-email', token)}\n\n"
                    "This link expires in 24 hours. If you did not create an account, you can ignore this message."
                ),
            )
        )

    def queue_password_reset(self, email: str, first_name: str, token: str) -> None:
        """Queue the password reset link."""
        self._queue.enqueue(
            Message(
                email,
                "Reset your password",
                (
                    f"Hello {first_name},\n\nUse the link below to choose a new password:\n\n"
                    f"{self._link('reset-password', token)}\n\n"
                    "This link expires in 60 minutes. If you did not ask to reset your password, you can ignore this message."
                ),
            )
        )

    def queue_account_already_exists(self, email: str, first_name: str) -> None:
        """Queue the notice sent when someone registers with an existing address."""
        self._queue.enqueue(
            Message(
                email,
                "You already have an account",
                (
                    f"Hello {first_name},\n\nSomeone tried to create an account with this email address, but one already "
                    f"exists. If this was you, sign in or reset your password:\n\n{self._base}/login\n\n"
                    "If it was not you, no action is needed."
                ),
            )
        )

    def queue_staff_invitation(self, email: str, organization_name: str, token: str) -> None:
        """Queue a staff invitation."""
        self._queue.enqueue(
            Message(
                email,
                f"You have been invited to join {organization_name}",
                (
                    f"Hello,\n\nYou have been invited to join {organization_name}. Sign in or create an account with this "
                    f"email address, then accept the invitation:\n\n{self._link('accept-invitation', token)}\n\n"
                    "This invitation expires in 7 days."
                ),
            )
        )


class EmailWorker:
    """Drains the queue and sends each message with up to three attempts."""

    def __init__(self, queue: MailQueue, sender: Sender) -> None:
        """Create the worker."""
        self._queue = queue
        self._sender = sender

    async def run(self) -> None:
        """Send queued messages until cancelled."""
        while True:
            message = await self._queue.messages.get()
            await self._send_with_retry(message)

    async def _send_with_retry(self, message: Message) -> None:
        """Attempt delivery three times with a growing delay; only the error type is logged."""
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                await self._sender.send(message)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Relay replies can echo the recipient address, so the message text is not logged.
                _LOGGER.warning(
                    "Email delivery attempt failed",
                    extra={
                        "attempt": attempt,
                        "maxAttempts": attempts,
                        "errorType": type(error).__name__,
                    },
                )
                if attempt < attempts:
                    await asyncio.sleep(2 * attempt)
        _LOGGER.error("An email could not be delivered after all attempts", extra={"maxAttempts": attempts})
