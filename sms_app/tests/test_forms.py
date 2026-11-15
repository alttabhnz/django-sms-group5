"""Tests for input validation (forms.py)."""

from django import forms
from django.test import SimpleTestCase

from sms_app.forms import MAX_MESSAGE_LENGTH, SMSForm, normalize_phone_number, sms_info


class PhoneNumberNormalizationTests(SimpleTestCase):
    def test_local_and_international_formats_are_converted(self):
        cases = {
            "09171234567": "+639171234567",
            "0917 123 4567": "+639171234567",
            "0917-123-4567": "+639171234567",
            "(0917) 123.4567": "+639171234567",
            "9171234567": "+639171234567",
            "639171234567": "+639171234567",
            "+639171234567": "+639171234567",
            "+14155550123": "+14155550123",  # other countries need a + sign
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone_number(raw), expected)

    def test_invalid_numbers_are_rejected(self):
        invalid = [
            "",                # empty
            "   ",             # only spaces
            "0917abc4567",     # letters
            "0917123",         # too short
            "091712345678",    # too long for a PH mobile
            "0281234567",      # Manila landline: cannot receive SMS
            "+63281234567",    # landline in international format
            "12345",           # no recognisable format
            "+0123456789",     # country code cannot start with 0
            "++639171234567",  # double plus
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(forms.ValidationError):
                    normalize_phone_number(raw)


class SMSFormTests(SimpleTestCase):
    def test_valid_form(self):
        form = SMSForm(data={"recipient": "09171234567", "message": "  Hello  "})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["recipient"], "+639171234567")
        self.assertEqual(form.cleaned_data["message"], "Hello")  # spaces trimmed

    def test_empty_phone_number(self):
        form = SMSForm(data={"recipient": "", "message": "Hello"})
        self.assertFalse(form.is_valid())
        self.assertIn("Please enter the recipient's phone number.", form.errors["recipient"])

    def test_empty_message(self):
        form = SMSForm(data={"recipient": "09171234567", "message": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("cannot be empty", form.errors["message"][0])

    def test_message_with_only_spaces_counts_as_empty(self):
        form = SMSForm(data={"recipient": "09171234567", "message": "     \n  "})
        self.assertFalse(form.is_valid())
        self.assertIn("message", form.errors)

    def test_invalid_phone_number(self):
        form = SMSForm(data={"recipient": "12345", "message": "Hello"})
        self.assertFalse(form.is_valid())
        self.assertIn("recipient", form.errors)

    def test_message_too_long(self):
        form = SMSForm(data={"recipient": "09171234567", "message": "a" * (MAX_MESSAGE_LENGTH + 1)})
        self.assertFalse(form.is_valid())
        self.assertIn("too long", form.errors["message"][0])


class CharacterCounterTests(SimpleTestCase):
    def test_plain_text_fits_160_characters(self):
        self.assertEqual(sms_info("a" * 160), ("GSM-7", 160, 1))
        self.assertEqual(sms_info("a" * 161), ("GSM-7", 161, 2))  # split into 153-char parts

    def test_extended_characters_count_double(self):
        self.assertEqual(sms_info("€"), ("GSM-7", 2, 1))

    def test_emoji_switches_to_unicode(self):
        encoding, length, segments = sms_info("Hi 😀")
        self.assertEqual(encoding, "Unicode")
        self.assertEqual(length, 5)  # the emoji counts as 2
        self.assertEqual(sms_info("é" * 70 + "“")[2], 2)  # 71 Unicode chars -> 2 parts

    def test_empty_message_has_no_parts(self):
        self.assertEqual(sms_info(""), ("GSM-7", 0, 0))
