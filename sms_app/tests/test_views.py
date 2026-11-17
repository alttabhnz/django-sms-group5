"""End-to-end tests of the web pages (views.py, urls.py and templates).

These follow the test table in the assignment (Step 6). The SMS provider is
mocked, so no real SMS is sent.
"""

import json
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from sms_app.models import SMSMessage
from sms_app.services import SendResult

Status = SMSMessage.Status
COMPOSE = reverse("sms_app:compose")


def submitted(message_id="SM123"):
    return SendResult(True, Status.SUBMITTED,
                      "Message submitted to the SMS provider. Delivery is not confirmed yet.",
                      provider_status="queued", message_id=message_id)


@override_settings(SMS_PROVIDER="twilio")
class ComposePageTests(TestCase):
    def test_form_page_shows_all_fields_and_csrf_token(self):
        response = self.client.get(COMPOSE)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="recipient"')
        self.assertContains(response, 'name="message"')
        self.assertContains(response, "Send SMS")
        self.assertContains(response, "csrfmiddlewaretoken")

    @mock.patch("sms_app.views.services.send_sms")
    def test_valid_number_and_message(self, send_sms):
        send_sms.return_value = submitted()
        response = self.client.post(COMPOSE, {"recipient": "0917 123 4567", "message": "Hello"}, follow=True)

        send_sms.assert_called_once_with("+639171234567", "Hello")
        sms = SMSMessage.objects.get()
        self.assertEqual(sms.status, Status.SUBMITTED)
        self.assertEqual(sms.provider_message_id, "SM123")
        # Confirms submission without falsely claiming delivery.
        self.assertContains(response, "Delivery is not confirmed yet")
        self.assertNotContains(response, "Delivered:")
        self.assertRedirects(response, COMPOSE)

    @mock.patch("sms_app.views.services.send_sms")
    def test_empty_phone_number_is_rejected(self, send_sms):
        response = self.client.post(COMPOSE, {"recipient": "", "message": "Hello"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter the recipient&#x27;s phone number.")
        send_sms.assert_not_called()
        self.assertFalse(SMSMessage.objects.exists())

    @mock.patch("sms_app.views.services.send_sms")
    def test_empty_message_is_rejected(self, send_sms):
        response = self.client.post(COMPOSE, {"recipient": "09171234567", "message": "   "})
        self.assertContains(response, "cannot be empty")
        send_sms.assert_not_called()

    @mock.patch("sms_app.views.services.send_sms")
    def test_invalid_phone_number_is_rejected(self, send_sms):
        response = self.client.post(COMPOSE, {"recipient": "0917-ABC", "message": "Hello"})
        self.assertContains(response, "is-invalid")
        self.assertContains(response, "The message was not sent.")
        send_sms.assert_not_called()

    def test_form_keeps_what_the_user_typed_after_an_error(self):
        response = self.client.post(COMPOSE, {"recipient": "", "message": "Keep me"})
        self.assertContains(response, "Keep me")

    @override_settings(TWILIO_ACCOUNT_SID="AC" + "1" * 32, TWILIO_AUTH_TOKEN="super-secret-token",
                       TWILIO_FROM_NUMBER="+15005550006", TWILIO_MESSAGING_SERVICE_SID="")
    @mock.patch("sms_app.services.TwilioClient")
    def test_incorrect_credentials_do_not_expose_secrets(self, client_class):
        from twilio.base.exceptions import TwilioRestException
        client_class.return_value.messages.create.side_effect = TwilioRestException(
            401, "https://api.twilio.com/2010-04-01/Accounts/AC.../Messages.json",
            msg="Authenticate", code=20003)

        response = self.client.post(COMPOSE, {"recipient": "09171234567", "message": "Hi"}, follow=True)
        self.assertContains(response, "rejected the app&#x27;s credentials")
        self.assertNotContains(response, "super-secret-token")
        self.assertNotContains(response, "AC" + "1" * 32)
        self.assertEqual(SMSMessage.objects.get().status, Status.FAILED)

    @override_settings(TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="", TWILIO_FROM_NUMBER="")
    def test_missing_credentials(self):
        response = self.client.post(COMPOSE, {"recipient": "09171234567", "message": "Hi"}, follow=True)
        self.assertContains(response, "not configured")
        self.assertEqual(SMSMessage.objects.get().error_code, "not_configured")

    @mock.patch("sms_app.views.services.send_sms")
    def test_provider_or_network_error(self, send_sms):
        send_sms.return_value = SendResult(False, Status.FAILED,
                                           "Could not connect to the SMS provider.", error_code="network")
        response = self.client.post(COMPOSE, {"recipient": "09171234567", "message": "Hi"}, follow=True)
        self.assertContains(response, "Could not connect to the SMS provider.")
        self.assertContains(response, "alert-danger")

    @mock.patch("sms_app.views.services.send_sms")
    def test_refreshing_after_sending_does_not_resend(self, send_sms):
        send_sms.return_value = submitted()
        response = self.client.post(COMPOSE, {"recipient": "09171234567", "message": "Hi"})
        self.assertEqual(response.status_code, 302)  # Post/Redirect/Get
        self.client.get(COMPOSE)
        self.assertEqual(send_sms.call_count, 1)

    def test_csrf_protection_is_enforced(self):
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(COMPOSE, {"recipient": "09171234567", "message": "Hi"})
        self.assertEqual(response.status_code, 403)

    def test_message_text_is_escaped(self):
        SMSMessage.objects.create(recipient="+639171234567", body="<script>alert(1)</script>",
                                  provider="twilio", status=Status.SUBMITTED)
        response = self.client.get(reverse("sms_app:history"))
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")


class HistoryAndStatusTests(TestCase):
    def setUp(self):
        self.sms = SMSMessage.objects.create(
            recipient="+639171234567", body="Hello", provider="twilio",
            status=Status.SUBMITTED, provider_status="queued", provider_message_id="SM1")

    def test_history_lists_messages_with_check_status_button(self):
        response = self.client.get(reverse("sms_app:history"))
        self.assertContains(response, "+639171234567")
        self.assertContains(response, "Check status")

    def test_history_status_filter(self):
        SMSMessage.objects.create(recipient="+639180000000", body="Bye", provider="twilio", status=Status.FAILED)
        response = self.client.get(reverse("sms_app:history"), {"status": "failed"})
        self.assertContains(response, "+639180000000")
        self.assertNotContains(response, "+639171234567")

    @mock.patch("sms_app.views.services.refresh_status")
    def test_check_status_updates_to_delivered(self, refresh):
        refresh.return_value = SendResult(True, Status.DELIVERED, "Delivered: confirmed.",
                                          provider_status="delivered", message_id="SM1")
        response = self.client.post(reverse("sms_app:check_status", args=[self.sms.pk]), follow=True)
        refresh.assert_called_once_with("twilio", "SM1")
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.DELIVERED)
        self.assertContains(response, "Delivered")

    @mock.patch("sms_app.views.services.refresh_status")
    def test_failed_status_check_keeps_old_status(self, refresh):
        refresh.return_value = SendResult(False, Status.FAILED, "No internet.", error_code="network")
        response = self.client.post(reverse("sms_app:check_status", args=[self.sms.pk]), follow=True)
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.SUBMITTED)
        self.assertContains(response, "Could not check the status")

    def test_check_status_requires_post(self):
        response = self.client.get(reverse("sms_app:check_status", args=[self.sms.pk]))
        self.assertEqual(response.status_code, 405)

    def test_check_status_ignores_unsafe_redirect(self):
        with mock.patch("sms_app.views.services.refresh_status") as refresh:
            refresh.return_value = SendResult(True, Status.SENT, "Sent.", message_id="SM1")
            response = self.client.post(reverse("sms_app:check_status", args=[self.sms.pk]),
                                        {"next": "//evil.example.com/"})
        self.assertEqual(response["Location"], reverse("sms_app:history"))


class DashboardTests(TestCase):
    def test_dashboard_counts(self):
        for status in [Status.DELIVERED, Status.SUBMITTED, Status.FAILED, Status.SIMULATED]:
            SMSMessage.objects.create(recipient="+639171234567", body="x", provider="twilio", status=status)
        response = self.client.get(reverse("sms_app:dashboard"))
        self.assertEqual(response.status_code, 200)
        totals = response.context["totals"]
        self.assertEqual(totals["total"], 4)
        self.assertEqual(totals["delivered"], 1)
        self.assertEqual(totals["in_progress"], 1)
        self.assertEqual(totals["failed"], 1)
        # 3 real attempts, 2 accepted by the provider -> 67%
        self.assertEqual(response.context["acceptance_rate"], 67)

    def test_empty_dashboard(self):
        response = self.client.get(reverse("sms_app:dashboard"))
        self.assertContains(response, "Nothing sent yet")


WEBHOOK_URL = reverse("sms_app:httpsms_webhook")
WEBHOOK_KEY = "test-signing-key-please-dont-use-in-prod"
WEBHOOK_SETTINGS = {"HTTPSMS_WEBHOOK_SIGNING_KEY": WEBHOOK_KEY}


class HealthEndpointTests(TestCase):
    def test_health_returns_ok_without_trailing_slash(self):
        # Monitoring tools poll /health (no slash) every few seconds.
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_health_returns_ok_with_trailing_slash(self):
        response = self.client.get(reverse("sms_app:health"))
        self.assertEqual(response.status_code, 200)

    @override_settings(SMS_PROVIDER="httpsms")
    def test_health_reports_the_provider(self):
        response = self.client.get("/health")
        self.assertEqual(response.json()["provider"], "httpSMS")

    def test_health_rejects_post(self):
        self.assertEqual(self.client.post("/health").status_code, 405)


def webhook_token(payload=None, key=WEBHOOK_KEY, algorithm="HS256"):
    """Build the JWT that httpSMS puts in the Authorization header."""
    import jwt as pyjwt
    return pyjwt.encode(payload if payload is not None else {"sub": "httpSMS"},
                        key, algorithm=algorithm)


@override_settings(**WEBHOOK_SETTINGS)
class HttpSmsWebhookTests(TestCase):
    """Delivery receipts pushed by httpSMS to /webhooks/httpsms/."""

    def setUp(self):
        self.sms = SMSMessage.objects.create(
            recipient="+639128355485", body="Hello", provider="httpsms",
            status=Status.SUBMITTED, provider_status="pending",
            provider_message_id="5be8f09e-7007-4fe9-86b6-591d63fd38ad")

    def post_event(self, event_type, data, token=None, raw=None):
        body = raw if raw is not None else {"type": event_type, "data": data}
        return self.client.post(
            WEBHOOK_URL,
            data=body if isinstance(body, str) else json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token if token is not None else webhook_token()}",
        )

    def test_delivered_receipt_marks_message_delivered(self):
        response = self.post_event(
            "message.phone.delivered",
            {"id": self.sms.provider_message_id, "contact": "+639128355485"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "updated")
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.DELIVERED)

    def test_delivered_receipt_updates_dashboard_totals(self):
        self.post_event("message.phone.delivered",
                        {"id": self.sms.provider_message_id})
        totals = self.client.get(reverse("sms_app:dashboard")).context["totals"]
        self.assertEqual(totals["delivered"], 1)
        self.assertEqual(totals["in_progress"], 0)

    def test_sent_receipt_moves_submitted_to_sent(self):
        self.post_event("message.phone.sent", {"id": self.sms.provider_message_id})
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.SENT)

    def test_failed_receipt_records_our_own_message(self):
        self.post_event("message.send.failed",
                        {"id": self.sms.provider_message_id,
                         "error_message": "MOBILE_APP_INACTIVE"})
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.FAILED)
        self.assertEqual(self.sms.error_code, "MOBILE_APP_INACTIVE")
        # The provider's code is stored, but the user sees our own wording.
        self.assertNotIn("MOBILE_APP_INACTIVE", self.sms.error_message)

    def test_expired_receipt_marks_message_failed(self):
        # "expired" events carry message_id instead of id.
        self.post_event("message.send.expired",
                        {"message_id": self.sms.provider_message_id})
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.FAILED)

    def test_late_sent_receipt_cannot_undo_delivered(self):
        self.sms.status = Status.DELIVERED
        self.sms.save(update_fields=["status"])
        self.post_event("message.phone.sent", {"id": self.sms.provider_message_id})
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.DELIVERED)

    def test_irrelevant_event_is_acknowledged_and_ignored(self):
        response = self.post_event("message.phone.received",
                                   {"message_id": "whatever", "content": "hi"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.SUBMITTED)

    def test_unknown_message_id_is_acknowledged(self):
        response = self.post_event("message.phone.delivered", {"id": "no-such-id"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_found")

    def test_missing_token_is_rejected(self):
        response = self.client.post(WEBHOOK_URL, data=json.dumps(
            {"type": "message.phone.delivered", "data": {"id": self.sms.provider_message_id}}),
            content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.SUBMITTED)

    def test_wrong_signature_is_rejected(self):
        token = webhook_token(key="some-other-key")
        self.post_event("message.phone.delivered",
                        {"id": self.sms.provider_message_id}, token=token)
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.SUBMITTED)

    @override_settings(HTTPSMS_WEBHOOK_SIGNING_KEY="")
    def test_unconfigured_signing_key_rejects_everything(self):
        response = self.post_event("message.phone.delivered",
                                   {"id": self.sms.provider_message_id})
        self.assertEqual(response.status_code, 401)
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.SUBMITTED)

    def test_malformed_json_is_acknowledged_so_it_is_not_retried(self):
        response = self.post_event(None, None, raw="{not json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(WEBHOOK_URL).status_code, 405)

    def test_csrf_is_not_required_for_the_provider(self):
        # httpSMS is not a browser: it cannot pass a CSRF token.
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            WEBHOOK_URL, data=json.dumps(
                {"type": "message.phone.delivered",
                 "data": {"id": self.sms.provider_message_id}}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {webhook_token()}")
        self.assertEqual(response.status_code, 200)
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.status, Status.DELIVERED)

    def test_webhook_only_touches_its_own_provider(self):
        other = SMSMessage.objects.create(
            recipient="+639171234567", body="x", provider="twilio",
            status=Status.SUBMITTED, provider_message_id=self.sms.provider_message_id)
        self.post_event("message.phone.delivered", {"id": other.provider_message_id})
        other.refresh_from_db()
        self.assertEqual(other.status, Status.SUBMITTED)
