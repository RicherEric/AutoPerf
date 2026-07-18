from django.urls import path

from . import views

urlpatterns = [
    path("devices", views.devices, name="devices"),
    path("devices/refresh", views.devices_refresh, name="devices_refresh"),
    path("devices/connect", views.devices_connect, name="devices_connect"),
    path("devices/pair", views.devices_pair, name="devices_pair"),
    path("devices/<str:serial>/nickname", views.device_nickname, name="device_nickname"),
    path("runs", views.runs, name="runs"),
    path("runs/<str:run_id>", views.run_detail, name="run_detail"),
    path("runs/<str:run_id>/cancel", views.run_cancel, name="run_cancel"),
    path("runs/<str:run_id>/samples", views.run_samples, name="run_samples"),
    path("runs/<str:run_id>/comparison", views.run_comparison, name="run_comparison"),
    path("devices/<str:serial>/baseline", views.baseline, name="baseline"),
    path("youtube-scenarios", views.youtube_scenarios_list, name="youtube_scenarios_list"),
    path("queue", views.queue_status, name="queue_status"),
    path("suites", views.suites, name="suites"),
    path("stats", views.stats, name="stats"),
]
