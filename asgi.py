"""
URL configuration for sms_project.

    /            -> SMS compose form (sms_app)
    /history/    -> message history and status tracking
    /dashboard/  -> summary of sending attempts
    /admin/      -> Django admin
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("sms_app.urls")),
]
