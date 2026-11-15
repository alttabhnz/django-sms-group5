"""
Forms and input validation for the SMS app.

SMSForm checks the recipient phone number and the message BEFORE anything is
sent to the SMS provider, and gives the user clear error messages.
"""

import re

from django import forms
from django.conf import settings

# Twilio's maximum message length. Longer messages are rejected by the API.
MAX_MESSAGE_LENGTH = 1600

# ---------------------------------------------------------------------------
# SMS segment counting (used by the character counter feature)
# ---------------------------------------------------------------------------
# Plain SMS uses the GSM-7 alphabet: 160 characters fit in one SMS.
# If a message contains any character outside that alphabet (for example an
# emoji or curly quotes “ ”), the whole message switches to UCS-2 (Unicode),
# and only 70 characters fit in one SMS. Long messages are split into parts:
# 153 characters per part for GSM-7 and 67 per part for UCS-2.
GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# These GSM-7 characters take up 2 character spaces each.
GSM7_EXTENDED = set("^{}\\[~]|€\f")


def sms_info(text):
    """Return (encoding, length, segments) for a message.

    The same rules are used by the live JavaScript counter in compose.html,
    so the counter and the saved history always agree.
    """
    if all(ch in GSM7_BASIC or ch in GSM7_EXTENDED for ch in text):
        encoding = "GSM-7"
        length = sum(2 if ch in GSM7_EXTENDED else 1 for ch in text)
        single, multi = 160, 153
    else:
        encoding = "Unicode"
        # Characters outside the Basic Multilingual Plane (most emoji) use 2 units.
        length = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)
        single, multi = 70, 67
    if length == 0:
        segments = 0
    elif length <= single:
        segments = 1
    else:
        segments = -(-length // multi)  # ceiling division
    return encoding, length, segments


# ---------------------------------------------------------------------------
# Phone number normalisation
# ---------------------------------------------------------------------------
E164_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
PH_MOBILE_PATTERN = re.compile(r"^\+639\d{9}$")


def normalize_phone_number(raw, country_code=None):
    """Convert a phone number to international E.164 format.

    Accepted examples (Philippines, country code 63):
        09171234567       -> +639171234567
        0917 123 4567     -> +639171234567
        0917-123-4567     -> +639171234567
        9171234567        -> +639171234567
        639171234567      -> +639171234567
        +639171234567     -> +639171234567
    Other countries must be typed with a "+" and country code, e.g. +14155550123.

    Raises forms.ValidationError with a user-friendly message if invalid.
    """
    country_code = country_code or settings.SMS_DEFAULT_COUNTRY_CODE
    number = re.sub(r"[\s\-().]", "", raw or "")

    if not number:
        raise forms.ValidationError("Please enter the recipient's phone number.", code="required")

    if not re.fullmatch(r"\+?\d+", number):
        raise forms.ValidationError(
            "The phone number can only contain digits, spaces, dashes and a leading +.",
            code="invalid_characters",
        )

    if number.startswith("+"):
        e164 = number
    elif number.startswith("0"):
        # Local format: drop the leading 0 and add the country code.
        e164 = f"+{country_code}{number[1:]}"
    elif number.startswith(country_code):
        e164 = f"+{number}"
    elif country_code == "63" and len(number) == 10 and number.startswith("9"):
        e164 = f"+63{number}"
    else:
        raise forms.ValidationError(
            "Enter the number in local format (e.g. 09171234567) or international "
            "format with a + sign (e.g. +639171234567).",
            code="invalid_format",
        )

    if not E164_PATTERN.match(e164):
        raise forms.ValidationError(
            "This phone number has the wrong number of digits.", code="invalid_length"
        )

    # Philippine numbers must be mobile numbers (+639XXXXXXXXX); landlines
    # cannot receive SMS.
    if e164.startswith("+63") and not PH_MOBILE_PATTERN.match(e164):
        raise forms.ValidationError(
            "Philippine mobile numbers have 11 digits and start with 09 "
            "(e.g. 09171234567).",
            code="invalid_ph_mobile",
        )

    return e164


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------
class SMSForm(forms.Form):
    recipient = forms.CharField(
        label="Recipient phone number",
        max_length=25,
        error_messages={"required": "Please enter the recipient's phone number."},
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-lg",
                "placeholder": "09171234567",
                "autocomplete": "tel",
                "inputmode": "tel",
            }
        ),
        help_text="Local (09171234567) or international (+639171234567) format.",
    )
    message = forms.CharField(
        label="Message",
        max_length=MAX_MESSAGE_LENGTH,
        # strip=True (the default) removes spaces at both ends, so a message
        # made only of spaces counts as empty.
        error_messages={
            "required": "Please type a message. It cannot be empty or only spaces.",
            "max_length": f"The message is too long. The maximum is {MAX_MESSAGE_LENGTH} characters.",
        },
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": "Type your message here...",
                "maxlength": MAX_MESSAGE_LENGTH,
            }
        ),
    )

    def clean_recipient(self):
        return normalize_phone_number(self.cleaned_data["recipient"])
