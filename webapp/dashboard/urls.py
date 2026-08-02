from django.urls import path

from . import views

urlpatterns = [
    path("devices", views.devices, name="devices"),
    path("devices/refresh", views.devices_refresh, name="devices_refresh"),
    path("devices/<str:serial>/control", views.device_control, name="device_control"),
    path("devices/mdns", views.devices_mdns, name="devices_mdns"),
    path("devices/connect", views.devices_connect, name="devices_connect"),
    path("devices/connect-discovered", views.devices_connect_discovered, name="devices_connect_discovered"),
    path("devices/pair", views.devices_pair, name="devices_pair"),
    path("devices/<str:serial>/nickname", views.device_nickname, name="device_nickname"),
    path("runs", views.runs, name="runs"),
    path("runs/<str:run_id>", views.run_detail, name="run_detail"),
    path("runs/<str:run_id>/cancel", views.run_cancel, name="run_cancel"),
    path("runs/<str:run_id>/events", views.run_events, name="run_events"),
    path("runs/<str:run_id>/samples", views.run_samples, name="run_samples"),
    path("runs/<str:run_id>/series", views.run_series, name="run_series"),
    path("runs/<str:run_id>/comparison", views.run_comparison, name="run_comparison"),
    path("runs/<str:run_id>/recording", views.run_recording, name="run_recording"),
    path("devices/<str:serial>/baseline", views.baseline, name="baseline"),
    path("youtube-scenarios", views.youtube_scenarios_list, name="youtube_scenarios_list"),
    path("selector-targets", views.selector_targets, name="selector_targets"),
    path("preflights", views.preflights, name="preflights"),
    path("preflights/<str:preflight_id>", views.preflight_detail, name="preflight_detail"),
    path("campaigns", views.campaigns, name="campaigns"),
    path("campaigns/<str:campaign_id>", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<str:campaign_id>/cancel", views.campaign_cancel, name="campaign_cancel"),
    path("queue", views.queue_status, name="queue_status"),
    path("suites", views.suites, name="suites"),
    path("stats", views.stats, name="stats"),
]
