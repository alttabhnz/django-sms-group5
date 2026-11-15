"""
Views (application logic) for the SMS app.

Workflow for sending an SMS (compose view):
    1. The user opens the page (GET) and sees an empty form.
    2. The user enters a recipient number and a message and clicks Send (POST).
    3. SMSForm validates the inputs. If they are invalid, the form is shown
       again with error messages and nothing is sent.
    4. If they are valid, services.send_sms() submits the message to the
       configured SMS provider.
    5. The attempt is saved to the database (history), whatever the result.
    6. The user is redirected back to the form and sees a success or failure
       message (Post/Redirect/Get, so refreshing the page never re-sends).
"""

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from . import services
from .forms import SMSForm, sms_info
from .models import SMSMessage


@require_http_methods(["GET", "POST"])
def compose(request):
    """Show the SMS form and handle sending."""
    if request.method == "POST":
        form = SMSForm(request.POST)
        if form.is_valid():
            recipient = form.cleaned_data["recipient"]
            body = form.cleaned_data["message"]

            result = services.send_sms(recipient, body)

            SMSMessage.objects.create(
                recipient=recipient,
                body=body,
                provider=settings.SMS_PROVIDER,
                status=result.status,
                provider_status=result.provider_status,
                provider_message_id=result.message_id,
                error_code=result.error_code,
                error_message="" if result.ok else result.user_message[:255],
                segments=sms_info(body)[2],
            )

            if not result.ok:
                messages.error(request, result.user_message)
            elif result.status == SMSMessage.Status.SIMULATED:
                messages.warning(request, result.user_message)
            else:
                messages.success(request, f"To {recipient}: {result.user_message}")
            return redirect("sms_app:compose")

        messages.error(request, "Please fix the highlighted fields. The message was not sent.")
    else:
        form = SMSForm()

    return render(
        request,
        "sms_app/compose.html",
        {
            "form": form,
            "provider_name": services.provider_display_name(),
            "recent": SMSMessage.objects.all()[:5],
        },
    )


def history(request):
    """List all sending attempts, newest first, with an optional status filter."""
    status_filter = request.GET.get("status", "")
    queryset = SMSMessage.objects.all()
    if status_filter in SMSMessage.Status.values:
        queryset = queryset.filter(status=status_filter)
    else:
        status_filter = ""

    page = Paginator(queryset, 10).get_page(request.GET.get("page"))
    return render(
        request,
        "sms_app/history.html",
        {
            "page": page,
            "status_filter": status_filter,
            "status_choices": SMSMessage.Status.choices,
        },
    )


@require_POST
def check_status(request, pk):
    """Ask the provider for the latest delivery status of one message."""
    sms = get_object_or_404(SMSMessage, pk=pk)
    if not sms.can_refresh_status:
        messages.info(request, "This message's status is already final.")
        return redirect(_back_to_history(request))

    result = services.refresh_status(sms.provider, sms.provider_message_id)
    if result.message_id:
        # The provider answered with the message's current status.
        sms.status = result.status
        sms.provider_status = result.provider_status
        if result.error_code:
            sms.error_code = result.error_code
        sms.save()
        level = messages.SUCCESS if result.ok else messages.WARNING
        messages.add_message(request, level, f"Status for {sms.recipient}: {result.user_message}")
    else:
        # The status check itself failed (e.g. no internet). Keep the old status.
        messages.error(request, f"Could not check the status: {result.user_message}")
    return redirect(_back_to_history(request))


def dashboard(request):
    """Summary of all sending attempts."""
    Status = SMSMessage.Status
    totals = SMSMessage.objects.aggregate(
        total=Count("id"),
        delivered=Count("id", filter=Q(status=Status.DELIVERED)),
        in_progress=Count("id", filter=Q(status__in=[Status.SUBMITTED, Status.SENT])),
        failed=Count("id", filter=Q(status__in=[Status.FAILED, Status.UNDELIVERED])),
        simulated=Count("id", filter=Q(status=Status.SIMULATED)),
    )
    real_attempts = totals["total"] - totals["simulated"]
    accepted = real_attempts - SMSMessage.objects.filter(status=Status.FAILED).count()
    today = timezone.localdate()

    context = {
        "totals": totals,
        "acceptance_rate": round(accepted * 100 / real_attempts) if real_attempts else None,
        "today_count": SMSMessage.objects.filter(created_at__date=today).count(),
        "by_status": [
            {"label": label, "value": value,
             "count": SMSMessage.objects.filter(status=value).count(),
             "badge": SMSMessage(status=value).badge_class}
            for value, label in Status.choices
        ],
        "recent": SMSMessage.objects.all()[:5],
        "provider_name": services.provider_display_name(),
    }
    return render(request, "sms_app/dashboard.html", context)


def _back_to_history(request):
    """Return to the history page the user came from (keeping filters and page)."""
    next_url = request.POST.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "sms_app:history"
