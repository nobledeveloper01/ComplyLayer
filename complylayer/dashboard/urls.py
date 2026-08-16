"""Dashboard routes.

Mounted from the management settings module, never the decision one — the
dashboard is management-side work and has no business on a worker holding the
latency contract (D7).
"""

from django.urls import path

from complylayer.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("sign-in", views.sign_in, name="sign-in"),
    path("verify", views.verify, name="verify"),
    path("enrol", views.enrol, name="enrol"),
    path("sign-out", views.sign_out_view, name="sign-out"),
    path("", views.rules, name="rules"),
    path("new", views.builder, name="builder"),
    path("preview", views.preview, name="preview"),
    path("validate", views.validate_expression, name="validate"),
    path("rules/<str:rule_id>", views.approval, name="approval"),
    path("queue", views.queue, name="queue"),
]
