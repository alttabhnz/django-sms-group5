"""Tests for the SMS provider integration (services.py).

The real Twilio and Semaphore APIs are replaced with mocks, so these tests
never send a real SMS, never need real credentials and work offline.
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
