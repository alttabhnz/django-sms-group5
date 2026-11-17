"""
SMS provider integration.

This module is the only part of the app that talks to an SMS gateway. The
views call send_sms() and refresh_status() and never deal with provider
details or credentials directly.

Supported providers (chosen with SMS_PROVIDER in .env):
    twilio     - Twilio Programmable Messaging (https://www.twilio.com)
    semaphore  - Semaphore, a Philippine SMS gateway (https://semaphore.co)
    traccar    - Traccar SMS Gateway: an Android app that exposes a small
                 HTTP API on the phone and sends the SMS with the phone's
                 own SIM card (https://www.traccar.org/sms-gateway/)
    httpsms    - httpSMS: a cloud API that tells your Android phone to send
                 the SMS with its own SIM card, and reports the real
                 delivery status (https://docs.httpsms.com/)
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
import re
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
# Traccar SMS Gateway (Android app HTTP API)
# ---------------------------------------------------------------------------
# Docs: https://www.traccar.org/sms-gateway/
#
# The Traccar SMS Gateway Android app runs a small HTTP server on the phone
# (default port 8082) and sends the SMS with the phone's own SIM card:
#
#     POST http://<phone-ip>:8082/
#     Authorization: <API key set inside the phone app>   (optional)
#     {"to": "+639171234567", "message": "Your message", "slot": 0}
#
# Answers:
#     200 - the phone accepted the message and is sending it (empty body)
#     401 - the Authorization header does not match the app's API key
#     500 - could not send; the body is a short plain-text reason
#
# The gateway has NO delivery receipts and returns no message ID, so a
# successful send stays "Submitted" and "Check status" is not offered.

MSG_TRACCAR_UNREACHABLE = (
    "Could not reach the SMS gateway on the phone. Check that the phone and "
    "this computer are on the same Wi-Fi network, that the Traccar SMS "
    "Gateway app is open and running, and that TRACCAR_GATEWAY_URL in the "
    ".env file is correct."
)
MSG_TRACCAR_ACCEPTED = (
    "The SMS gateway on the phone accepted the message and is sending it "
    "with the phone's SIM card. This gateway does not report delivery "
    "confirmations, so the status stays “Submitted”."
)
MSG_TRACCAR_NO_RECEIPTS = (
    "The Traccar SMS Gateway does not report delivery confirmations, so the "
    "status cannot be checked. Watch the phone's message app to confirm the "
    "SMS was sent."
)

# The Android API uses 0-based SIM slot indexes (0 = first SIM).
TRACCAR_MAX_SLOT = 4


class TraccarError(Exception):
    def __init__(self, user_message, code=""):
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


class TraccarProvider:
    name = "traccar"

    def __init__(self):
        url = settings.TRACCAR_GATEWAY_URL
        if not url:
            raise ProviderNotConfigured(
                "Set TRACCAR_GATEWAY_URL to the phone's gateway address "
                "(e.g. http://192.168.1.245:8082)."
            )
        if not url.lower().startswith(("http://", "https://")):
            raise ProviderNotConfigured(
                "TRACCAR_GATEWAY_URL must start with http:// or https:// "
                "(e.g. http://192.168.1.245:8082)."
            )
        self.url = url
        self.api_key = settings.TRACCAR_API_KEY
        self.slot = self._parse_slot(settings.TRACCAR_SLOT)

    @staticmethod
    def _parse_slot(raw):
        """Validate the optional SIM slot index from .env."""
        if raw in ("", None):
            return None
        try:
            slot = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ProviderNotConfigured("TRACCAR_SLOT must be a number, e.g. 0 or 1.")
        if not 0 <= slot <= TRACCAR_MAX_SLOT:
            raise ProviderNotConfigured(
                f"TRACCAR_SLOT must be between 0 and {TRACCAR_MAX_SLOT} "
                "(0 = first SIM). Leave it blank to use the default SIM."
            )
        return slot

    def send(self, to, body):
        if not to.startswith("+"):
            # The phone app dials whatever it receives: only E.164 numbers.
            raise TraccarError(
                "The SMS gateway expects the number in international format, "
                "starting with + (e.g. +639171234567).",
                "not_e164",
            )

        payload = {"to": to, "message": body}
        if self.slot is not None:
            payload["slot"] = self.slot

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # The app compares this header, as-is, with the API key configured
            # inside the phone app (no "Bearer" prefix).
            headers["Authorization"] = self.api_key

        try:
            response = requests.post(
                self.url,
                json=payload,
                headers=headers,
                timeout=settings.SMS_REQUEST_TIMEOUT,
            )
        except requests.exceptions.Timeout:
            logger.warning("Traccar SMS Gateway timed out")
            raise TraccarError(MSG_TIMEOUT, "timeout")
        except requests.exceptions.ConnectionError:
            logger.warning("Could not connect to the Traccar SMS Gateway")
            raise TraccarError(MSG_TRACCAR_UNREACHABLE, "network")
        except requests.exceptions.RequestException as exc:
            # Only the exception type is logged: the URL can hold a LAN address.
            logger.warning("Traccar SMS Gateway request failed: %s", type(exc).__name__)
            raise TraccarError(MSG_UNEXPECTED, "provider_error")

        return self._parse(response)

    @staticmethod
    def _parse(response):
        if response.status_code in (401, 403):
            raise TraccarError(MSG_BAD_CREDENTIALS, str(response.status_code))
        if response.status_code == 200:
            return SendResult(
                ok=True,
                status=Status.SUBMITTED,
                user_message=MSG_TRACCAR_ACCEPTED,
                provider_status="accepted",
            )

        # Anything else is an error. The gateway answers with a short
        # plain-text reason, which is logged but never shown to the user.
        reason = (response.text or "").strip()[:200]
        logger.warning(
            "Traccar SMS Gateway rejected the request: HTTP %s reason=%r",
            response.status_code,
            reason,
        )
        if "Missing phone or message" in reason:
            raise TraccarError(
                "The SMS gateway did not receive a phone number or a message.",
                "missing_fields",
            )
        raise TraccarError(
            "The SMS gateway on the phone could not send the message. "
            "Check that the phone has signal and that the app is allowed to "
            "send SMS, then try again.",
            str(response.status_code),
        )

    @staticmethod
    def fetch_status(message_id):
        # The gateway has no status endpoint; keep the current status and say why.
        return SendResult(
            ok=True,
            status=Status.SUBMITTED,
            user_message=MSG_TRACCAR_NO_RECEIPTS,
            provider_status="accepted",
            message_id=message_id,
        )


# ---------------------------------------------------------------------------
# httpSMS (cloud API that triggers your Android phone to send the SMS)
# ---------------------------------------------------------------------------
# Docs: https://docs.httpsms.com/
#
# POST https://api.httpsms.com/v1/messages/send
#     x-api-key: <API key from httpsms.com/settings>
#     {"content": "Your message", "from": "+639483538946", "to": "+63917…"}
#     -> 200 {"data": {"id": "…", "status": "pending"}}
#
# GET https://api.httpsms.com/v1/messages/<id>      (status check)
#     -> 200 {"data": {"status": "sent" | "delivered" | "failed", …}}
#
# Answers: 200 = accepted, 401 = wrong API key,
#          400/422 = validation error ({"data": {"to": [...]}}), 404 = unknown id
#
# Unlike the Traccar gateway, httpSMS reports the real delivery status, so a
# message can reach "Delivered" through the existing Check status button.

HTTPSMS_API_URL = "https://api.httpsms.com"

MSG_HTTPSMS_UNREACHABLE = (
    "Could not connect to the httpSMS service. Please check your internet "
    "connection and try again."
)

# httpSMS message statuses -> our statuses.
HTTPSMS_STATUS_MAP = {
    "pending": Status.SUBMITTED,
    "queued": Status.SUBMITTED,
    "scheduled": Status.SUBMITTED,
    "created": Status.SUBMITTED,
    "sending": Status.SUBMITTED,
    "sent": Status.SENT,
    "delivered": Status.DELIVERED,
    "undelivered": Status.UNDELIVERED,
    "failed": Status.FAILED,
    "expired": Status.FAILED,
}

# ---------------------------------------------------------------------------
# httpSMS webhooks (delivery receipts pushed to /webhooks/httpsms/)
# ---------------------------------------------------------------------------
# httpSMS POSTs a CloudEvents document for every event we subscribed to:
#     {"type": "message.phone.delivered", "data": {"id": "…", "contact": "+639…"}}
# The events below are the ones that change a message's status; any other
# event (incoming SMS, heartbeats, missed calls) is acknowledged and ignored.
# Docs: https://docs.httpsms.com/webhooks/events.html

HTTPSMS_WEBHOOK_EVENTS = {
    # The phone handed the SMS to the carrier.
    "message.phone.sent": Status.SENT,
    # The carrier confirmed the recipient's handset received it. This is the
    # "Delivered (confirmed)" receipt the dashboard reports.
    "message.phone.delivered": Status.DELIVERED,
    # The phone app could not send it (e.g. MOBILE_APP_INACTIVE).
    "message.send.failed": Status.FAILED,
    # The phone never sent it before it expired (e.g. it stayed powered off).
    "message.send.expired": Status.FAILED,
}

# How far each status has progressed. A receipt may only move a message
# forward: a late "sent" event can never undo "Delivered".
HTTPSMS_WEBHOOK_RANK = {
    Status.SUBMITTED: 0,
    Status.SENT: 1,
    Status.UNDELIVERED: 2,
    Status.FAILED: 2,
    Status.DELIVERED: 3,
    Status.SIMULATED: 0,
}


def apply_httpsms_webhook(event_type, data):
    """Apply one httpSMS webhook event to the matching message in the database.

    Returns a short outcome string for the HTTP response and the server log:
    "updated", "ignored" (unknown/irrelevant event), "not_found" or
    "no_change". Never raises: a malformed payload must not make httpSMS retry
    forever, and a receipt must never move a status backwards.
    """
    from .models import SMSMessage  # imported here: models need Django to be ready

    new_status = HTTPSMS_WEBHOOK_EVENTS.get(str(event_type or ""))
    if new_status is None:
        return "ignored"

    if not isinstance(data, dict):
        return "ignored"

    # "delivered"/"sent"/"failed" carry data.id; "expired"/"received" use
    # data.message_id.
    message_id = str(data.get("id") or data.get("message_id") or "").strip()
    if not message_id:
        return "ignored"

    sms = SMSMessage.objects.filter(
        provider="httpsms", provider_message_id=message_id
    ).first()
    if sms is None:
        logger.info("httpSMS webhook: no message with id %s", message_id[:64])
        return "not_found"

    if HTTPSMS_WEBHOOK_RANK[new_status] <= HTTPSMS_WEBHOOK_RANK.get(sms.status, 0):
        return "no_change"

    sms.status = new_status
    sms.provider_status = str(event_type)[:30]
    if new_status == Status.FAILED:
        # The raw reason (e.g. MOBILE_APP_INACTIVE) is a code, not a sentence:
        # store it as the error code and write our own user-facing message.
        sms.error_code = str(data.get("error_message") or "send_failed")[:20]
        sms.error_message = "The SMS service could not send this message."
    sms.save(update_fields=["status", "provider_status", "error_code",
                            "error_message", "updated_at"])
    logger.info("httpSMS webhook: message %s -> %s", message_id[:64], new_status)
    return "updated"


class HttpSmsError(Exception):
    def __init__(self, user_message, code=""):
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


class HttpSmsProvider:
    name = "httpsms"

    def __init__(self):
        if not settings.HTTPSMS_API_KEY:
            raise ProviderNotConfigured(
                "Set HTTPSMS_API_KEY (copy it from httpsms.com/settings)."
            )
        if not settings.HTTPSMS_FROM_NUMBER:
            raise ProviderNotConfigured(
                "Set HTTPSMS_FROM_NUMBER to the phone number that sends the SMS."
            )
        url = settings.HTTPSMS_SERVER_URL or HTTPSMS_API_URL
        if not url.lower().startswith(("http://", "https://")):
            raise ProviderNotConfigured(
                "HTTPSMS_SERVER_URL must start with http:// or https:// "
                f"(e.g. {HTTPSMS_API_URL})."
            )
        from_number = self._to_e164(settings.HTTPSMS_FROM_NUMBER)
        if from_number is None:
            raise ProviderNotConfigured(
                "HTTPSMS_FROM_NUMBER must be a phone number, e.g. 09483538946 "
                "or +639483538946."
            )
        self.url = url.rstrip("/")
        self.api_key = settings.HTTPSMS_API_KEY
        self.from_number = from_number

    @staticmethod
    def _to_e164(raw):
        """'09483538946', '639483538946' or '+639483538946' -> '+639483538946'."""
        cleaned = re.sub(r"[\s\-()]", "", str(raw or ""))
        if not cleaned:
            return None
        if cleaned.startswith("+"):
            number = cleaned
        elif cleaned.startswith("0"):
            number = "+" + settings.SMS_DEFAULT_COUNTRY_CODE + cleaned[1:]
        else:
            number = "+" + cleaned
        return number if re.fullmatch(r"\+\d{8,15}", number) else None

    def send(self, to, body):
        # The form already converts to "+63…"; only international numbers pass.
        recipient = self._to_e164(to) if str(to).strip().startswith("+") else None
        if recipient is None:
            raise HttpSmsError(
                "The SMS service expects the number in international format, "
                "starting with + (e.g. +639171234567).",
                "not_e164",
            )

        payload = {"content": body, "from": self.from_number, "to": recipient}
        data = self._request("POST", "/v1/messages/send", json=payload)

        provider_status = str(data.get("status", "")).lower()
        status = HTTPSMS_STATUS_MAP.get(provider_status, Status.SUBMITTED)
        return SendResult(
            ok=status not in (Status.FAILED, Status.UNDELIVERED),
            status=status,
            user_message=status_message(status),
            provider_status=provider_status,
            message_id=str(data.get("id", "")),
        )

    def fetch_status(self, message_id):
        data = self._request("GET", f"/v1/messages/{message_id}")
        provider_status = str(data.get("status", "")).lower()
        status = HTTPSMS_STATUS_MAP.get(provider_status, Status.SUBMITTED)
        result = SendResult(
            ok=status not in (Status.FAILED, Status.UNDELIVERED),
            status=status,
            user_message=status_message(status),
            provider_status=provider_status,
            message_id=str(data.get("id") or message_id),
        )
        if status in (Status.FAILED, Status.UNDELIVERED):
            result.error_code = str(data.get("failure_reason") or "")[:20]
        return result

    def _request(self, method, path, **kwargs):
        """Call the API and return the `data` object of a successful answer."""
        headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        url = self.url + path
        try:
            if method == "POST":
                response = requests.post(url, headers=headers,
                                         timeout=settings.SMS_REQUEST_TIMEOUT, **kwargs)
            else:
                response = requests.get(url, headers=headers,
                                        timeout=settings.SMS_REQUEST_TIMEOUT, **kwargs)
        except requests.exceptions.Timeout:
            logger.warning("httpSMS timed out")
            raise HttpSmsError(MSG_TIMEOUT, "timeout")
        except requests.exceptions.ConnectionError:
            logger.warning("Could not connect to httpSMS")
            raise HttpSmsError(MSG_HTTPSMS_UNREACHABLE, "network")
        except requests.exceptions.RequestException as exc:
            # Only the exception type is logged: the URL can hold the API key path.
            logger.warning("httpSMS request failed: %s", type(exc).__name__)
            raise HttpSmsError(MSG_UNEXPECTED, "provider_error")

        return self._parse(response)

    @staticmethod
    def _parse(response):
        if response.status_code in (401, 403):
            raise HttpSmsError(MSG_BAD_CREDENTIALS, str(response.status_code))

        try:
            payload = response.json()
        except ValueError:
            payload = None
        data = payload.get("data") if isinstance(payload, dict) else None

        if 200 <= response.status_code < 300:
            if isinstance(data, dict):
                return data
            logger.warning("httpSMS returned an unexpected body: HTTP %s", response.status_code)
            raise HttpSmsError(
                "The SMS service returned an unexpected response. Please try again.",
                "bad_response",
            )

        # Validation errors answer 400/422 with {"data": {"field": ["reason"]}}.
        if response.status_code in (400, 422) and isinstance(data, dict):
            logger.warning("httpSMS rejected the request: HTTP %s fields=%s",
                           response.status_code, ", ".join(map(str, data))[:100])
            if "to" in data:
                raise HttpSmsError(
                    "The SMS service says this phone number is not valid.", "invalid_to"
                )
            if "from" in data:
                raise HttpSmsError(
                    "The sender number configured for this app was rejected by the "
                    "SMS service. The administrator needs to check HTTPSMS_FROM_NUMBER "
                    "in the .env file.",
                    "invalid_from",
                )
            if "content" in data:
                raise HttpSmsError(
                    "The SMS service rejected the message text. Please change it and "
                    "try again.",
                    "invalid_content",
                )
            fields = ", ".join(map(str, data))[:60]
            raise HttpSmsError(
                f"The SMS service rejected the request (problem with: {fields}).",
                str(response.status_code),
            )

        if response.status_code == 404:
            raise HttpSmsError(
                "This message was not found on the httpSMS service, so its status "
                "cannot be checked.",
                "not_found",
            )

        logger.warning("httpSMS rejected the request: HTTP %s", response.status_code)
        raise HttpSmsError(
            "The SMS service could not accept the message. Please try again.",
            str(response.status_code),
        )


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
    "traccar": TraccarProvider,
    "httpsms": HttpSmsProvider,
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
    except TraccarError as exc:
        return SendResult(False, Status.FAILED, exc.user_message, error_code=exc.code)
    except HttpSmsError as exc:
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
    return {
        "twilio": "Twilio",
        "semaphore": "Semaphore",
        "traccar": "Traccar SMS Gateway",
        "httpsms": "httpSMS",
        "console": "Simulation mode",
    }.get(settings.SMS_PROVIDER, settings.SMS_PROVIDER)
