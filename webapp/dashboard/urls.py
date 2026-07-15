from django.urls import path

from . import views

urlpatterns = [
    path("devices", views.devices, name="devices"),
    path("devices/refresh", views.devices_refresh, name="devices_refresh"),
    path("runs", views.runs, name="runs"),
    path("runs/<str:run_id>", views.run_detail, name="run_detail"),
    path("runs/<str:run_id>/samples", views.run_samples, name="run_samples"),
]
