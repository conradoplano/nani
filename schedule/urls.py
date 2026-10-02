from django.urls import path

from . import views

app_name = "schedule"

urlpatterns = [
    path("", views.home, name="home"),
    path("week/", views.week, name="week"),
    path("week/<str:day>/", views.week, name="week_of"),
    path("day/<str:day>/", views.day_edit, name="day"),
    path("extra/new/", views.extra_edit, name="extra_new"),
    path("extra/<int:pk>/", views.extra_edit, name="extra"),
    path("vacation/", views.vacation, name="vacation"),
    path("vacation/<int:pk>/", views.vacation_action, name="vacation_action"),
    path("trips/", views.trips, name="trips"),
    path("trips/<int:year>/<int:month>/", views.trips, name="trips_month"),
    path("trips/edit/<int:pk>/", views.trip_edit, name="trip"),
    path("plan/", views.plan, name="plan"),
    path("month/", views.month, name="month"),
    path("month/<int:year>/<int:month>/", views.month, name="month_of"),
]
