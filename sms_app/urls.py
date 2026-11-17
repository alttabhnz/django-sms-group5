from django.urls import path

from . import views

app_name = "sms_app"

urlpatterns = [
    path("", views.compose, name="compose"),
    path("history/", views.history, name="history"),
    path("history/<int:pk>/check-status/", views.check_status, name="check_status"),
    path("dashboard/", views.dashboard, name="dashboard"),
    # Liveness probe used by monitoring (returns 200 while the app is up).
    # Registered with and without the trailing slash: probes may use either.
    path("health/", views.health, name="health"),
    path("health", views.health),
    # Delivery receipts pushed by httpSMS (not a browser: CSRF-exempt, JWT-verified).
    path("webhooks/httpsms/", views.httpsms_webhook, name="httpsms_webhook"),
]
