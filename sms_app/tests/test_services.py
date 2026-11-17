"""Tests for the SMS provider integration (services.py).

The real Twilio, Semaphore and Traccar SMS Gateway APIs are replaced with
mocks, so these tests never send a real SMS, never need real credentials and
work offline.
"""

from types import SimpleNamespace
from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings
from twilio.base.exceptions import TwilioRestException

from sms_app import services
from sms_app.models import SMSMessage

Status = SMSMessage.Status

FAKE_TOKEN = "fake-auth-token-should-never-be-shown"

TWILIO_SETTINGS = {
    "SMS_PROVIDER": "twilio",
    "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "TWILIO_AUTH_TOKEN": FAKE_TOKEN,
    "TWILIO_FROM_NUMBER": "+15005550006",
    "TWILIO_MESSAGING_SERVICE_SID": "",
}


def twilio_error(code, status=400):
    return TwilioRestException(status, "https://api.twilio.com/...", msg="error", code=code)


@override_settings(**TWILIO_SETTINGS)
class TwilioProviderTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch("sms_app.services.TwilioClient")
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.create = self.client_class.return_value.messages.create

    def test_successful_request_is_submitted_not_delivered(self):
        self.create.return_value = SimpleNamespace(sid="SM123", status="queued")
        result = services.send_sms("+639171234567", "Hello")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, Status.SUBMITTED)
        self.assertEqual(result.message_id, "SM123")
        self.assertIn("not confirmed", result.user_message)
        self.assertNotIn("Delivered", result.user_message)
        self.create.assert_called_once_with(to="+639171234567", body="Hello", from_="+15005550006")

    @override_settings(TWILIO_MESSAGING_SERVICE_SID="MG123")
    def test_messaging_service_is_used_when_configured(self):
        self.create.return_value = SimpleNamespace(sid="SM1", status="accepted")
        services.send_sms("+639171234567", "Hi")
        self.create.assert_called_once_with(to="+639171234567", body="Hi", messaging_service_sid="MG123")

    def test_wrong_credentials(self):
        self.create.side_effect = twilio_error(20003, status=401)
        result = services.send_sms("+639171234567", "Hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, Status.FAILED)
        self.assertIn("credentials", result.user_message)
        self.assertNotIn(FAKE_TOKEN, result.user_message)

    def test_provider_rejects_invalid_number(self):
        self.create.side_effect = twilio_error(21211)
        result = services.send_sms("+639171234567", "Hello")
        self.assertFalse(result.ok)
        self.assertIn("not valid", result.user_message)
        self.assertEqual(result.error_code, "21211")

    def test_unverified_number_on_trial_account(self):
        self.create.side_effect = twilio_error(21608)
        result = services.send_sms("+639171234567", "Hello")
        self.assertIn("verified", result.user_message)

    def test_unknown_provider_error_is_still_explained(self):
        self.create.side_effect = twilio_error(99999)
        result = services.send_sms("+639171234567", "Hello")
        self.assertFalse(result.ok)
        self.assertIn("99999", result.user_message)

    def test_network_error(self):
        self.create.side_effect = requests.exceptions.ConnectionError("no internet")
        result = services.send_sms("+639171234567", "Hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "network")
        self.assertIn("internet", result.user_message)

    def test_timeout(self):
        self.create.side_effect = requests.exceptions.ReadTimeout()
        result = services.send_sms("+639171234567", "Hello")
        self.assertEqual(result.error_code, "timeout")

    def test_status_mapping(self):
        fetch = self.client_class.return_value.messages.return_value.fetch
        expected = {
            "queued": Status.SUBMITTED,
            "sent": Status.SENT,
            "delivered": Status.DELIVERED,
            "undelivered": Status.UNDELIVERED,
            "failed": Status.FAILED,
        }
        for twilio_status, our_status in expected.items():
            with self.subTest(twilio_status=twilio_status):
                fetch.return_value = SimpleNamespace(sid="SM1", status=twilio_status, error_code=None)
                result = services.refresh_status("twilio", "SM1")
                self.assertEqual(result.status, our_status)
        fetch.return_value = SimpleNamespace(sid="SM1", status="undelivered", error_code=30003)
        self.assertEqual(services.refresh_status("twilio", "SM1").error_code, "30003")


class ConfigurationTests(SimpleTestCase):
    @override_settings(SMS_PROVIDER="twilio", TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="",
                       TWILIO_FROM_NUMBER="", TWILIO_MESSAGING_SERVICE_SID="")
    def test_missing_twilio_credentials(self):
        result = services.send_sms("+639171234567", "Hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "not_configured")
        self.assertIn("not configured", result.user_message)

    @override_settings(SMS_PROVIDER="semaphore", SEMAPHORE_API_KEY="")
    def test_missing_semaphore_key(self):
        self.assertEqual(services.send_sms("+639171234567", "Hi").error_code, "not_configured")

    @override_settings(SMS_PROVIDER="carrier-pigeon")
    def test_unknown_provider(self):
        self.assertEqual(services.send_sms("+639171234567", "Hi").error_code, "not_configured")

    @override_settings(SMS_PROVIDER="console")
    def test_console_mode_never_claims_real_sending(self):
        result = services.send_sms("+639171234567", "Hello")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, Status.SIMULATED)
        self.assertIn("NOT really sent", result.user_message)


def fake_response(status_code, payload):
    response = mock.Mock(status_code=status_code, ok=200 <= status_code < 300)
    response.json.return_value = payload
    return response


@override_settings(SMS_PROVIDER="semaphore", SEMAPHORE_API_KEY="secret-key", SEMAPHORE_SENDER_NAME="")
class SemaphoreProviderTests(SimpleTestCase):
    @mock.patch("sms_app.services.requests.post")
    def test_success_uses_local_number_format(self, post):
        post.return_value = fake_response(200, [{"message_id": 42, "status": "Pending"}])
        result = services.send_sms("+639171234567", "Hello")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, Status.SUBMITTED)
        self.assertEqual(result.message_id, "42")
        self.assertEqual(post.call_args.kwargs["data"]["number"], "09171234567")

    @mock.patch("sms_app.services.requests.post")
    def test_sent_status_is_not_reported_as_delivered(self, post):
        post.return_value = fake_response(200, [{"message_id": 1, "status": "Sent"}])
        self.assertEqual(services.send_sms("+639171234567", "Hi").status, Status.SENT)

    @mock.patch("sms_app.services.requests.post")
    def test_bad_api_key(self, post):
        post.return_value = fake_response(401, {"message": "unauthorized"})
        result = services.send_sms("+639171234567", "Hello")
        self.assertFalse(result.ok)
        self.assertIn("credentials", result.user_message)
        self.assertNotIn("secret-key", result.user_message)

    @mock.patch("sms_app.services.requests.post")
    def test_validation_error_from_provider(self, post):
        post.return_value = fake_response(200, {"number": ["The number format is invalid."]})
        result = services.send_sms("+639171234567", "Hello")
        self.assertFalse(result.ok)
        self.assertIn("phone number", result.user_message)

    @mock.patch("sms_app.services.requests.post")
    def test_non_philippine_number_is_rejected_before_calling_api(self, post):
        result = services.send_sms("+14155550123", "Hello")
        self.assertFalse(result.ok)
        post.assert_not_called()

    @mock.patch("sms_app.services.requests.post")
    def test_message_starting_with_test_is_blocked(self, post):
        result = services.send_sms("+639171234567", "test message")
        self.assertFalse(result.ok)
        self.assertIn("TEST", result.user_message)
        post.assert_not_called()

    @mock.patch("sms_app.services.requests.post", side_effect=requests.exceptions.ConnectionError)
    def test_network_error(self, post):
        self.assertEqual(services.send_sms("+639171234567", "Hi").error_code, "network")


TRACCAR_SETTINGS = {
    "SMS_PROVIDER": "traccar",
    "TRACCAR_GATEWAY_URL": "http://192.168.1.245:8082",
    "TRACCAR_API_KEY": "secret-gateway-key-should-never-be-shown",
    "TRACCAR_SLOT": "",
}


def traccar_response(status_code, text=""):
    """The gateway answers 200 with an empty body, or 500 with plain text."""
    response = mock.Mock(status_code=status_code, ok=200 <= status_code < 300, text=text)
    return response


@override_settings(**TRACCAR_SETTINGS)
class TraccarProviderTests(SimpleTestCase):
    @mock.patch("sms_app.services.requests.post")
    def test_successful_send_posts_json_to_the_phone(self, post):
        post.return_value = traccar_response(200)
        result = services.send_sms("+639171234567", "Load 50 confirmed.")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, Status.SUBMITTED)
        self.assertEqual(result.provider_status, "accepted")
        # No message ID and no delivery receipt: "Check status" must not appear.
        self.assertEqual(result.message_id, "")
        self.assertIn("does not report delivery confirmations", result.user_message)
        self.assertNotIn("Delivered", result.user_message)

        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://192.168.1.245:8082")
        self.assertEqual(
            kwargs["json"], {"to": "+639171234567", "message": "Load 50 confirmed."}
        )
        self.assertEqual(kwargs["headers"]["Authorization"], TRACCAR_SETTINGS["TRACCAR_API_KEY"])
        self.assertEqual(kwargs["timeout"], 15)

    @override_settings(TRACCAR_API_KEY="")
    @mock.patch("sms_app.services.requests.post")
    def test_api_key_is_omitted_when_not_configured(self, post):
        post.return_value = traccar_response(200)
        services.send_sms("+639171234567", "Hi")
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    @mock.patch("sms_app.services.requests.post")
    def test_missing_url_is_reported_as_not_configured(self, post):
        with override_settings(TRACCAR_GATEWAY_URL=""), \
                self.assertLogs("sms_app.services", level="ERROR") as logs:
            result = services.send_sms("+639171234567", "Hi")
        self.assertEqual(result.error_code, "not_configured")
        self.assertIn("not configured", result.user_message)
        # The variable name is only logged for the administrator, never shown.
        self.assertIn("TRACCAR_GATEWAY_URL", logs.output[0])
        post.assert_not_called()

    @mock.patch("sms_app.services.requests.post")
    def test_url_without_scheme_is_rejected(self, post):
        with override_settings(TRACCAR_GATEWAY_URL="192.168.1.245:8082"):
            result = services.send_sms("+639171234567", "Hi")
        self.assertEqual(result.error_code, "not_configured")
        post.assert_not_called()

    @mock.patch("sms_app.services.requests.post")
    def test_wrong_api_key(self, post):
        post.return_value = traccar_response(401)
        result = services.send_sms("+639171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, Status.FAILED)
        self.assertIn("credentials", result.user_message)
        self.assertNotIn(TRACCAR_SETTINGS["TRACCAR_API_KEY"], result.user_message)

    @mock.patch("sms_app.services.requests.post")
    def test_gateway_reports_missing_fields(self, post):
        post.return_value = traccar_response(500, "Missing phone or message")
        result = services.send_sms("+639171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertIn("did not receive a phone number", result.user_message)

    @mock.patch("sms_app.services.requests.post")
    def test_gateway_error_reason_is_not_shown_to_the_user(self, post):
        post.return_value = traccar_response(500, "SecurityException: some internal detail")
        result = services.send_sms("+639171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, Status.FAILED)
        self.assertNotIn("SecurityException", result.user_message)
        self.assertEqual(result.error_code, "500")

    @mock.patch("sms_app.services.requests.post")
    def test_number_must_be_in_international_format(self, post):
        result = services.send_sms("09171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "not_e164")
        self.assertIn("international format", result.user_message)
        post.assert_not_called()

    @override_settings(TRACCAR_SLOT="1")
    @mock.patch("sms_app.services.requests.post")
    def test_sim_slot_is_sent_when_configured(self, post):
        post.return_value = traccar_response(200)
        services.send_sms("+639171234567", "Hi")
        self.assertEqual(post.call_args.kwargs["json"]["slot"], 1)

    @override_settings(TRACCAR_SLOT="two")
    @mock.patch("sms_app.services.requests.post")
    def test_non_numeric_slot_is_rejected(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertEqual(result.error_code, "not_configured")
        post.assert_not_called()

    @override_settings(TRACCAR_SLOT="9")
    @mock.patch("sms_app.services.requests.post")
    def test_slot_out_of_range_is_rejected(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertEqual(result.error_code, "not_configured")
        post.assert_not_called()

    @mock.patch("sms_app.services.requests.post", side_effect=requests.exceptions.ReadTimeout)
    def test_timeout(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "timeout")

    @mock.patch("sms_app.services.requests.post", side_effect=requests.exceptions.ConnectionError)
    def test_phone_unreachable_explains_the_local_network(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "network")
        self.assertIn("same Wi-Fi network", result.user_message)

    def test_status_check_explains_that_there_are_no_receipts(self):
        result = services.refresh_status("traccar", "")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, Status.SUBMITTED)
        self.assertIn("does not report delivery", result.user_message)

    @override_settings(TRACCAR_API_KEY="")
    def test_display_name(self):
        self.assertEqual(services.provider_display_name(), "Traccar SMS Gateway")


HTTPSMS_SETTINGS = {
    "SMS_PROVIDER": "httpsms",
    "HTTPSMS_API_KEY": "uk_secret-key-should-never-be-shown",
    "HTTPSMS_FROM_NUMBER": "639483538946",
    "HTTPSMS_SERVER_URL": "https://api.httpsms.com",
}


def httpsms_response(status_code, payload=None, text=""):
    """httpSMS answers with a JSON envelope: {"data": …, "message": …, "status": …}."""
    response = mock.Mock(status_code=status_code, ok=200 <= status_code < 300, text=text)
    response.json.return_value = payload if payload is not None else {}
    return response


@override_settings(**HTTPSMS_SETTINGS)
class HttpSmsProviderTests(SimpleTestCase):
    @mock.patch("sms_app.services.requests.post")
    def test_successful_send_returns_message_id(self, post):
        post.return_value = httpsms_response(
            200,
            {
                "data": {"id": "32343a19-da5e", "status": "pending"},
                "message": "Request handled successfully",
                "status": "success",
            },
        )
        result = services.send_sms("+639171234567", "Load 50 confirmed.")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, Status.SUBMITTED)
        self.assertEqual(result.provider_status, "pending")
        self.assertEqual(result.message_id, "32343a19-da5e")
        # An ID means the existing "Check status" button becomes available.
        self.assertIn("Check status", result.user_message)

        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.httpsms.com/v1/messages/send")
        self.assertEqual(
            kwargs["json"],
            {"content": "Load 50 confirmed.", "from": "+639483538946", "to": "+639171234567"},
        )
        self.assertEqual(kwargs["headers"]["x-api-key"], HTTPSMS_SETTINGS["HTTPSMS_API_KEY"])
        self.assertEqual(kwargs["timeout"], 15)

    @override_settings(HTTPSMS_FROM_NUMBER="09483538946")
    @mock.patch("sms_app.services.requests.post")
    def test_local_from_number_is_converted_to_e164(self, post):
        post.return_value = httpsms_response(200, {"data": {"id": "1", "status": "pending"}})
        services.send_sms("+639171234567", "Hi")
        self.assertEqual(post.call_args.kwargs["json"]["from"], "+639483538946")

    @override_settings(HTTPSMS_API_KEY="")
    @mock.patch("sms_app.services.requests.post")
    def test_missing_api_key(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertEqual(result.error_code, "not_configured")
        # The user sees the safe generic message; the exact variable name is
        # only written to the server log (line 738 of services.py).
        self.assertEqual(result.user_message, services.MSG_NOT_CONFIGURED)
        post.assert_not_called()

    @override_settings(HTTPSMS_FROM_NUMBER="")
    @mock.patch("sms_app.services.requests.post")
    def test_missing_from_number(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertEqual(result.error_code, "not_configured")
        post.assert_not_called()

    @override_settings(HTTPSMS_FROM_NUMBER="not-a-number")
    @mock.patch("sms_app.services.requests.post")
    def test_invalid_from_number(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertEqual(result.error_code, "not_configured")
        post.assert_not_called()

    @override_settings(HTTPSMS_SERVER_URL="ftp://api.httpsms.com")
    @mock.patch("sms_app.services.requests.post")
    def test_server_url_without_http_scheme(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertEqual(result.error_code, "not_configured")
        post.assert_not_called()

    @mock.patch("sms_app.services.requests.post")
    def test_wrong_api_key(self, post):
        post.return_value = httpsms_response(401, {"status": "unauthorized"})
        result = services.send_sms("+639171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, Status.FAILED)
        self.assertIn("credentials", result.user_message)
        self.assertNotIn(HTTPSMS_SETTINGS["HTTPSMS_API_KEY"], result.user_message)

    @mock.patch("sms_app.services.requests.post")
    def test_service_rejects_invalid_recipient(self, post):
        post.return_value = httpsms_response(
            422, {"data": {"to": ["The phone number is invalid."]}, "status": "failed"}
        )
        result = services.send_sms("+639171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_to")
        self.assertIn("phone number is not valid", result.user_message)

    @mock.patch("sms_app.services.requests.post")
    def test_service_rejects_sender_number(self, post):
        post.return_value = httpsms_response(
            422, {"data": {"from": ["number not registered"]}, "status": "failed"}
        )
        result = services.send_sms("+639171234567", "Hi")
        self.assertEqual(result.error_code, "invalid_from")
        self.assertIn("HTTPSMS_FROM_NUMBER", result.user_message)

    @mock.patch("sms_app.services.requests.post")
    def test_non_international_number_is_rejected_before_calling_api(self, post):
        result = services.send_sms("09171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "not_e164")
        post.assert_not_called()

    @mock.patch("sms_app.services.requests.get")
    def test_status_check_reports_delivered(self, get):
        get.return_value = httpsms_response(
            200, {"data": {"id": "32343a19", "status": "delivered"}, "status": "success"}
        )
        result = services.refresh_status("httpsms", "32343a19")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, Status.DELIVERED)
        self.assertEqual(result.message_id, "32343a19")
        self.assertIn("Delivered", result.user_message)
        self.assertEqual(
            get.call_args.args[0], "https://api.httpsms.com/v1/messages/32343a19"
        )

    @mock.patch("sms_app.services.requests.get")
    def test_status_check_reports_sent(self, get):
        get.return_value = httpsms_response(
            200, {"data": {"id": "x", "status": "sent"}, "status": "success"}
        )
        self.assertEqual(services.refresh_status("httpsms", "x").status, Status.SENT)

    @mock.patch("sms_app.services.requests.get")
    def test_status_check_reports_failed_with_reason(self, get):
        get.return_value = httpsms_response(
            200,
            {"data": {"id": "x", "status": "failed", "failure_reason": "UNKNOWN"},
             "status": "success"},
        )
        result = services.refresh_status("httpsms", "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, Status.FAILED)
        self.assertEqual(result.error_code, "UNKNOWN")

    @mock.patch("sms_app.services.requests.get")
    def test_status_check_for_unknown_message(self, get):
        get.return_value = httpsms_response(404, {"status": "failed"})
        result = services.refresh_status("httpsms", "missing-id")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "not_found")
        self.assertEqual(result.message_id, "")
        self.assertIn("not found", result.user_message)

    @mock.patch("sms_app.services.requests.post", side_effect=requests.exceptions.ReadTimeout)
    def test_timeout(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "timeout")

    @mock.patch("sms_app.services.requests.post", side_effect=requests.exceptions.ConnectionError)
    def test_network_error(self, post):
        result = services.send_sms("+639171234567", "Hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "network")
        self.assertIn("internet", result.user_message)

    @override_settings(HTTPSMS_API_KEY="")
    def test_display_name(self):
        self.assertEqual(services.provider_display_name(), "httpSMS")
