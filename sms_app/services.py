"""
SMS provider integration.

This module is the only part of the app that talks to an SMS gateway. The
views call send_sms() and refresh_status() and never deal with provider
details or credentials directly.

Supported providers (chosen with SMS_PROVIDER in .env):
    twilio     - Twilio Programmable Messaging (https://www.twilio.com)
    semaphore  - Semaphore, a Philippine SMS gateway (https://semaphore.co)
    console    - simulation mode for development: prints the SMS to the
                 terminal and does NOT send anything

Security rules followed here:
    * Credentials are read from Django settings, which load them from .env.
    * Credentials are never shown to the user or written to the logs.
    * Error messages shown to the user are written by us, not copied from the
      provider, so nothing sensitive can leak through them.

Submitted vs delivered:
    A successful API call only means the provider ACCEPTED the message. The
    app saves it as "submitted" and only marks it "delivered" when the
    provider later confirms delivery (see refresh_status()).
"""

import logging
import uuid
from dataclasses import dataclass

import requests
from django.conf import settings
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client as TwilioClient

from .models import SMSMessage

logger = logging.getLogger(__name__)

Status = SMSMessage.Status


@dataclass
class SendResult:
    """The outcome of one call to an SMS provider."""

    ok: bool
    status: str
    user_message: str
    provider_status: str = ""
    message_id: str = ""
    error_code: str = ""


class ProviderNotConfigured(Exception):
    """Raised when required settings for the chosen provider are missing."""


# ---------------------------------------------------------------------------
# User-facing error messages (safe to display: no credentials, no internals)
# ---------------------------------------------------------------------------
MSG_NOT_CONFIGURED = (
    "The SMS service is not configured yet, so the message was not sent. "
    "The administrator needs to set the provider credentials in the .env file."
)
MSG_BAD_CREDENTIALS = (
    "The SMS provider rejected the app's credentials, so the message was not sent. "
    "The administrator needs to check the API credentials in the .env file."
)
MSG_NETWORK = (
    "Could not connect to the SMS provider. Please check your internet "
    "connection and try again."
)
MSG_TIMEOUT = "The SMS provider took too long to respond. Please try again in a moment."
MSG_UNEXPECTED = "Something went wrong while sending the message. Please try again."


# ---------------------------------------------------------------------------
# Twilio
# ---------------------------------------------------------------------------
# Twilio error codes we explain in plain language.
# Reference: https://www.twilio.com/docs/api/errors
TWILIO_ERROR_MESSAGES = {
    20003: MSG_BAD_CREDENTIALS,
    20429: "Too many messages are being sent right now. Please wait a moment and try again.",
    21211: "The SMS provider says this phone number is not valid. Please check it and try again.",
    21212: "The sender number configured for this app is not valid. Please contact the administrator.",
    21408: "Sending SMS to this country is not enabled on the provider account.",
    21606: "The sender number configured for this app cannot send SMS. Please contact the administrator.",
    21608: (
        "This number is not verified. Twilio trial accounts can only send to "
        "verified phone numbers. Verify the number in the Twilio Console first."
    ),
    21610: "This recipient has unsubscribed (replied STOP), so the message cannot be sent.",
    21612: "The SMS provider cannot route a message to this number.",
    21614: "This number cannot receive SMS. It may be a landline.",
    21617: "The message is too long for the SMS provider.",
}

# How Twilio's message statuses map to our own statuses.
# https://www.twilio.com/docs/messaging/api/message-resource#message-status-values
TWILIO_STATUS_MAP = {
    "accepted": Status.SUBMITTED,
    "scheduled": Status.SUBMITTED,
    "queued": Status.SUBMITTED,
    "sending": Status.SUBMITTED,
    "sent": Status.SENT,
    "delivered": Status.DELIVERED,
    "read": Status.DELIVERED,
    "undelivered": Status.UNDELIVERED,
    "failed": Status.FAILED,
    "canceled": Status.FAILED,
}


class TwilioProvider:
    name = "twilio"

    def __init__(self):
        sid = settings.TWILIO_ACCOUNT_SID
        token = settings.TWILIO_AUTH_TOKEN
        if not sid or not token or not (
            settings.TWILIO_FROM_NUMBER or settings.TWILIO_MESSAGING_SERVICE_SID
        ):
            raise ProviderNotConfigured(
                "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER "
                "(or TWILIO_MESSAGING_SERVICE_SID)."
            )
        http_client = TwilioHttpClient(timeout=settings.SMS_REQUEST_TIMEOUT)
        self.client = TwilioClient(sid, token, http_client=http_client)

    def send(self, to, body):
        params = {"to": to, "body": body}
        if settings.TWILIO_MESSAGING_SERVICE_SID:
            params["messaging_service_sid"] = settings.TWILIO_MESSAGING_SERVICE_SID
        else:
            params["from_"] = settings.TWILIO_FROM_NUMBER

        message = self.client.messages.create(**params)
        return self._result_from_status(message.sid, message.status)

    def fetch_status(self, message_id):
        message = self.client.messages(message_id).fetch()
        result = self._result_from_status(message.sid, message.status)
        if message.error_code:
            result.error_code = str(message.error_code)
        return result

    @staticmethod
    def _result_from_status(sid, provider_status):
        provider_status = (provider_status or "").lower()
        status = TWILIO_STATUS_MAP.get(provider_status, Status.SUBMITTED)
        return SendResult(
            ok=status not in (Status.FAILED, Status.UNDELIVERED),
            status=status,
            user_message=status_message(status),
            provider_status=provider_status,
            message_id=sid,
        )

    @staticmethod
    def explain_error(exc):
        """Turn a Twilio exception into a safe SendResult."""
        if isinstance(exc, TwilioRestException):
            logger.warning("Twilio rejected the request: HTTP %s, code %s", exc.status, exc.code)
            if exc.status == 401:
                text = MSG_BAD_CREDENTIALS
            else:
                text = TWILIO_ERROR_MESSAGES.get(
                    exc.code,
                    f"The SMS provider rejected the message (error {exc.code}). Please try again.",
                )
            return SendResult(False, Status.FAILED, text, error_code=str(exc.code or exc.status))
        return None


# ---------------------------------------------------------------------------
# Semaphore (Philippine SMS gateway)
# ---------------------------------------------------------------------------
# Docs: https://api.semaphore.co/docs
SEMAPHORE_API_URL = "https://api.semaphore.co/api/v4/messages"

# Semaphore statuses: Queued, Pending, Sent (handed to the network),
# Failed, Refunded. Semaphore does not report handset delivery, so its best
# status is "sent to carrier", never "delivered".
SEMAPHORE_STATUS_MAP = {
    "queued": Status.SUBMITTED,
    "pending": Status.SUBMITTED,
    "sent": Status.SENT,
    "failed": Status.FAILED,
    "refunded": Status.FAILED,
}


class SemaphoreError(Exception):
    def __init__(self, user_message, code=""):
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


class SemaphoreProvider:
    name = "semaphore"

    def __init__(self):
        if not settings.SEMAPHORE_API_KEY:
            raise ProviderNotConfigured("Set SEMAPHORE_API_KEY.")
        self.api_key = settings.SEMAPHORE_API_KEY
        self.sender_name = settings.SEMAPHORE_SENDER_NAME

    def send(self, to, body):
        if not to.startswith("+63"):
            raise SemaphoreError("Semaphore can only send to Philippine mobile numbers.", "not_ph")
        if body.lstrip().upper().startswith("TEST"):
            # Semaphore silently drops messages that start with "TEST".
            raise SemaphoreError(
                'Semaphore ignores messages that start with the word "TEST". '
                "Please reword the beginning of your message.",
                "starts_with_test",
            )
        data = {"apikey": self.api_key, "number": "0" + to[3:], "message": body}
        if self.sender_name:
            data["sendername"] = self.sender_name
        response = requests.post(SEMAPHORE_API_URL, data=data, timeout=settings.SMS_REQUEST_TIMEOUT)
        return self._parse(response)

    def fetch_status(self, message_id):
        response = requests.get(
            f"{SEMAPHORE_API_URL}/{message_id}",
            params={"apikey": self.api_key},
            timeout=settings.SMS_REQUEST_TIMEOUT,
        )
        return self._parse(response)

    @staticmethod
    def _parse(response):
        if response.status_code in (401, 403):
            raise SemaphoreError(MSG_BAD_CREDENTIALS, str(response.status_code))
        try:
            payload = response.json()
        except ValueError:
            payload = None

        # Success: a list with one object per recipient.
        if response.ok and isinstance(payload, list) and payload and isinstance(payload[0], dict):
            item = payload[0]
            provider_status = str(item.get("status", "")).lower()
            status = SEMAPHORE_STATUS_MAP.get(provider_status, Status.SUBMITTED)
            return SendResult(
                ok=status != Status.FAILED,
                status=status,
                user_message=status_message(status),
                provider_status=provider_status,
                message_id=str(item.get("message_id", "")),
            )

        # Anything else is an error. Semaphore returns validation errors as a
        # JSON object, e.g. {"number": ["The number format is invalid."]}.
        logger.warning("Semaphore rejected the request: HTTP %s", response.status_code)
        text = "The SMS provider rejected the message."
        if isinstance(payload, dict):
            fields = ", ".join(k for k in payload if k != "apikey")
            if "apikey" in payload:
                raise SemaphoreError(MSG_BAD_CREDENTIALS, "apikey")
            if "sendername" in payload:
                text = "The sender name configured for this app is not approved yet."
            elif "number" in payload:
                text = "The SMS provider says this phone number is not valid."
            elif fields:
                text = f"The SMS provider rejected the message (problem with: {fields})."
        raise SemaphoreError(text, str(response.status_code))


# ---------------------------------------------------------------------------
# Console (simulation)
# ---------------------------------------------------------------------------
class ConsoleProvider:
    """Development mode: prints the SMS instead of sending it."""

    name = "console"

    def send(self, to, body):
        message_id = f"SIM-{uuid.uuid4().hex[:12]}"
        logger.info("[SIMULATED SMS] to=%s id=%s body=%r", to, message_id, body)
        return SendResult(
            ok=True,
            status=Status.SIMULATED,
            user_message=(
                "Simulation mode: the message was NOT really sent. It was printed in "
                "the server terminal. Set SMS_PROVIDER in .env to send real SMS."
            ),
            provider_status="simulated",
            message_id=message_id,
        )

    def fetch_status(self, message_id):
        return SendResult(True, Status.SIMULATED, "Simulated messages have no delivery status.",
                          provider_status="simulated", message_id=message_id)


PROVIDERS = {
    "twilio": TwilioProvider,
    "semaphore": SemaphoreProvider,
    "console": ConsoleProvider,
}


def status_message(status):
    """A plain-language explanation of each status for the user."""
    return {
        Status.SUBMITTED: (
            "Message submitted to the SMS provider. Delivery is not confirmed yet. "
            "Use “Check status” in the history to see if it was delivered."
        ),
        Status.SENT: (
            "The provider has passed the message to the mobile carrier. Delivery to "
            "the phone is not confirmed yet."
        ),
        Status.DELIVERED: "Delivered: the provider confirmed the phone received the message.",
        Status.UNDELIVERED: "The carrier could not deliver the message to the phone.",
        Status.FAILED: "The message could not be sent.",
        Status.SIMULATED: "Simulated message: nothing was really sent.",
    }.get(status, "")


def get_provider(name=None):
    """Create a provider. Defaults to the one chosen in settings.SMS_PROVIDER."""
    name = name or settings.SMS_PROVIDER
    provider_class = PROVIDERS.get(name)
    if provider_class is None:
        raise ProviderNotConfigured(
            f"Unknown SMS_PROVIDER {name!r}. Use one of: {', '.join(PROVIDERS)}."
        )
    return provider_class()


def _call_provider(action, provider_name=None):
    """Run a provider call and convert every kind of failure into a SendResult.

    `action` is a function that receives the provider and returns a SendResult.
    """
    try:
        provider = get_provider(provider_name)
        return action(provider)
    except ProviderNotConfigured as exc:
        logger.error("SMS provider not configured: %s", exc)
        return SendResult(False, Status.FAILED, MSG_NOT_CONFIGURED, error_code="not_configured")
    except TwilioRestException as exc:
        return TwilioProvider.explain_error(exc)
    except SemaphoreError as exc:
        return SendResult(False, Status.FAILED, exc.user_message, error_code=exc.code)
    except requests.exceptions.Timeout:
        logger.warning("SMS provider timed out")
        return SendResult(False, Status.FAILED, MSG_TIMEOUT, error_code="timeout")
    except requests.exceptions.ConnectionError:
        logger.warning("Could not connect to the SMS provider")
        return SendResult(False, Status.FAILED, MSG_NETWORK, error_code="network")
    except (TwilioException, requests.exceptions.RequestException) as exc:
        # Only the exception type is logged: request URLs can contain API keys.
        logger.warning("SMS provider request failed: %s", type(exc).__name__)
        return SendResult(False, Status.FAILED, MSG_UNEXPECTED, error_code="provider_error")


def send_sms(to, body):
    """Send one SMS. Always returns a SendResult; never raises provider errors."""
    return _call_provider(lambda provider: provider.send(to, body))


def refresh_status(provider_name, message_id):
    """Ask the provider that sent a message for its latest status."""
    return _call_provider(lambda provider: provider.fetch_status(message_id), provider_name)


def provider_display_name():
    return {"twilio": "Twilio", "semaphore": "Semaphore", "console": "Simulation mode"}.get(
        settings.SMS_PROVIDER, settings.SMS_PROVIDER
    )
