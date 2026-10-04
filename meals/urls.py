from django.urls import path

from . import views

app_name = "meals"

urlpatterns = [
    path("", views.home, name="home"),
    path("week/", views.menu, name="menu"),
    path("week/<str:day>/", views.menu, name="menu_of"),
    path("meal/new/", views.meal_edit, name="meal_new"),
    path("meal/<int:pk>/", views.meal_edit, name="meal"),
    path("meal/<int:pk>/feedback/", views.feedback, name="feedback"),
    path("meal/<int:pk>/replace/", views.meal_replace, name="meal_replace"),
    path("meal/<int:pk>/feedback/quick/", views.feedback_quick, name="feedback_quick"),
    path("shopping/", views.shopping, name="shopping"),
    path("shopping/<str:day>/", views.shopping, name="shopping_of"),
    path("shopping/<str:day>/state/", views.shopping_state, name="shopping_state"),
    path("shopping/<str:day>/toggle/", views.shopping_toggle, name="shopping_toggle"),
    path("shopping/<str:day>/extra/", views.extra_add, name="extra_add"),
    path("shopping/extra/<int:pk>/delete/", views.extra_delete, name="extra_delete"),
    path("shopping/extra/<int:pk>/restore/", views.extra_restore, name="extra_restore"),
    path("dish/<int:pk>/ingredients/", views.ingredients, name="ingredients"),
    path("family/", views.family, name="family"),
    path("family/member/new/", views.member_edit, name="member_new"),
    path("family/member/<int:pk>/", views.member_edit, name="member"),
    path("family/rule/<int:pk>/", views.rule_edit, name="rule"),
    path("family/rule/<int:pk>/toggle/", views.rule_toggle, name="rule_toggle"),
    path("family/household/", views.household_edit, name="household"),
    path("family/usual-week/", views.usual_week, name="usual_week"),
    path("week/<str:day>/create/", views.menu_create, name="menu_create"),
    path("plan/<int:pk>/", views.menu_request, name="menu_request"),
    path("plan/<int:pk>/status/", views.menu_request_status, name="menu_request_status"),
]
