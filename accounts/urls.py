from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_request, name="login"),
    path("login/code/", views.login_verify, name="verify"),
    path("logout/", views.logout_view, name="logout"),
]
