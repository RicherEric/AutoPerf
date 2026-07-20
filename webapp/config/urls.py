from django.conf import settings
from django.urls import include, path
from django.views.static import serve as serve_static

urlpatterns = [
    path("api/", include("dashboard.urls")),
]

if settings.DEBUG:
    # Dev-only static serving for run screen recordings (see RECORDINGS_ROOT
    # in settings.py) -- this project already assumes DEBUG=True throughout
    # (see settings.py's own comments), and django.views.static.serve
    # supports HTTP Range requests out of the box, which <video> seeking needs.
    urlpatterns += [
        path("recordings/<path:path>", serve_static, {"document_root": settings.RECORDINGS_ROOT}),
    ]
