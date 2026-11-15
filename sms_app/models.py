"""
Database model for the SMS app.

Every attempt to send an SMS is saved as one SMSMessage row, whether it
succeeded or failed. This powers the message history, status tracking and
dashboard features.
"""

from django.db import models


class SMSMessage(models.Model):
    """One SMS sending attempt."""

    # Our own simplified status. The provider's raw status (e.g. Twilio's
    # "queued", "sent", "delivered") is stored separately in provider_status.
    class Status(models.TextChoices):
        # The provider ACCEPTED the request. This does NOT mean the phone
        # received it yet.
        SUBMITTED = "submitted", "Submitted to provider"
        # The provider handed it to the carrier network.
        SENT = "sent", "Sent to carrier"
        # The provider CONFIRMED that the handset received it.
        DELIVERED = "delivered", "Delivered (confirmed)"
        # The carrier or provider could not deliver it after accepting it.
        UNDELIVERED = "undelivered", "Undelivered"
        # The request never reached the provider, or the provider rejected it.
        FAILED = "failed", "Failed"
        # Console/simulation mode: nothing was really sent.
        SIMULATED = "simulated", "Simulated (not really sent)"

    recipient = models.CharField(
        max_length=20, help_text="Phone number in international E.164 format, e.g. +639171234567."
    )
    body = models.TextField()
    provider = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=Status.choices)
    provider_status = models.CharField(
        max_length=30, blank=True, help_text="Raw status reported by the SMS provider."
    )
    provider_message_id = models.CharField(
        max_length=64, blank=True, help_text="Message ID returned by the provider (e.g. Twilio SID)."
    )
    error_code = models.CharField(max_length=20, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    segments = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "SMS message"

    def __str__(self):
        return f"{self.recipient} - {self.get_status_display()} ({self.created_at:%Y-%m-%d %H:%M})"

    # Statuses whose outcome is still unknown, so "Check status" is useful.
    PENDING_STATUSES = {Status.SUBMITTED, Status.SENT}

    @property
    def can_refresh_status(self):
        return bool(self.provider_message_id) and self.status in self.PENDING_STATUSES

    @property
    def badge_class(self):
        """Bootstrap colour for this status, used in the templates."""
        return {
            self.Status.SUBMITTED: "info",
            self.Status.SENT: "primary",
            self.Status.DELIVERED: "success",
            self.Status.UNDELIVERED: "warning",
            self.Status.FAILED: "danger",
            self.Status.SIMULATED: "secondary",
        }.get(self.status, "secondary")
