from django.urls import path

from . import views

app_name = "sms_app"

urlpatterns = [
    path("", views.compose, name="compose"),
    path("history/", views.history, name="history"),
    path("history/<int:pk>/check-status/", views.check_status, name="check_status"),
    path("dashboard/", views.dashboard, name="dashboard"),
]
