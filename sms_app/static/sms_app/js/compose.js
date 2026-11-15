/*
 * compose.js - live features of the Send SMS page:
 *   1. Character counter with SMS part (segment) count
 *   2. Message preview showing the number in international format
 *   3. Prevents double-sending by disabling the button after clicking Send
 *
 * The counting rules match sms_info() in sms_app/forms.py.
 */
(function () {
  "use strict";

  // GSM-7 alphabet: 160 characters per SMS. Extended characters use 2 spaces.
  const GSM7_BASIC =
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?" +
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà";
  const GSM7_EXTENDED = "^{}\\[~]|€\f";

  function smsInfo(text) {
    const chars = Array.from(text); // splits emoji correctly
    const isGsm = chars.every((ch) => GSM7_BASIC.includes(ch) || GSM7_EXTENDED.includes(ch));
    let length, single, multi;
    if (isGsm) {
      length = chars.reduce((n, ch) => n + (GSM7_EXTENDED.includes(ch) ? 2 : 1), 0);
      single = 160;
      multi = 153;
    } else {
      length = chars.reduce((n, ch) => n + (ch.codePointAt(0) > 0xffff ? 2 : 1), 0);
      single = 70;
      multi = 67;
    }
    let segments = 0;
    let perPart = single;
    if (length > 0 && length <= single) {
      segments = 1;
    } else if (length > single) {
      segments = Math.ceil(length / multi);
      perPart = multi;
    }
    const left = segments === 0 ? single : segments * perPart - length;
    return { encoding: isGsm ? "GSM-7" : "Unicode", length, segments, left };
  }

  // Same rules as normalize_phone_number() in forms.py, for the preview only.
  // The server always re-validates the number.
  function previewNumber(raw) {
    const n = raw.replace(/[\s\-().]/g, "");
    if (!n) return "—";
    if (/^\+\d+$/.test(n)) return n;
    if (/^0\d+$/.test(n)) return "+63" + n.slice(1);
    if (/^63\d+$/.test(n)) return "+" + n;
    if (/^9\d{9}$/.test(n)) return "+63" + n;
    return n + "  (check format)";
  }

  const form = document.getElementById("sms-form");
  if (!form) return;
  const recipient = form.querySelector("[name=recipient]");
  const message = form.querySelector("[name=message]");
  const button = document.getElementById("send-btn");
  const el = (id) => document.getElementById(id);

  function update() {
    const info = smsInfo(message.value);
    el("char-count").textContent = info.length;
    el("encoding").textContent = info.encoding;
    el("segment-count").textContent = info.segments;
    el("chars-left").textContent = info.left;

    const counter = el("char-counter");
    counter.classList.toggle("text-warning", info.segments > 1);
    counter.classList.toggle("text-body-secondary", info.segments <= 1);

    el("preview-to").textContent = previewNumber(recipient.value);
    const preview = el("preview-text");
    if (message.value.trim()) {
      preview.textContent = message.value; // textContent: no HTML injection
    } else {
      preview.innerHTML = '<span class="text-body-secondary">Your message will appear here.</span>';
    }
  }

  // Once the user starts fixing a field, remove its red error highlight.
  function clearError(event) {
    event.target.classList.remove("is-invalid");
  }

  recipient.addEventListener("input", update);
  message.addEventListener("input", update);
  recipient.addEventListener("input", clearError);
  message.addEventListener("input", clearError);
  update();

  form.addEventListener("submit", function () {
    button.disabled = true;
    button.querySelector(".spinner-border").classList.remove("d-none");
    button.lastChild.textContent = " Sending...";
  });
})();
