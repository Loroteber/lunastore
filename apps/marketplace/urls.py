# url configuration for marketplace app
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home_redirect, name="home"),
    path("index.php", views.marketplace, name="index"),
    path("store.php", views.store_listing, name="store_listing"),
    path("category.php", views.category, name="category"),
    path("app.php", views.app, name="app"),
    path("app_add.php", views.app_add, name="app_add"),
    path("settings_apps.php", views.settings_apps, name="settings_apps"),
    path(
        "edit_app_info.php/<int:pk>/", views.application_edit_info, name="edit_app_info"
    ),
    path("app_stats.php/<int:pk>/", views.application_stats, name="app_stats"),
    path("search.php", views.search, name="search"),
    path("report_app.php", views.report_app, name="report_app"),
    path("report_problem.php", views.report_problem, name="report_problem"),
    path("download.php", views.download_list, name="download"),
    path("distributions.php", views.manage_distributions, name="manage_distributions"),
    path(
        "distribution_edit.php/<int:dist_pk>/",
        views.distribution_edit,
        name="distribution_edit",
    ),
    path(
        "distribution_delete.php/<int:dist_pk>/",
        views.distribution_delete,
        name="distribution_delete",
    ),
    path(
        "distribution_create.php/<int:app_id>/",
        views.distribution_create,
        name="distribution_create",
    ),
    path("get_dist_file/<int:dist_pk>/", views.get_file_action, name="download_action"),
    path("rate_app.php", views.rate_app, name="rate_app"),
    path("delete_review.php", views.delete_review, name="delete_review"),
    path("reply_review.php", views.reply_review, name="reply_review"),
    path("delete_review_reply.php", views.delete_review_reply, name="delete_review_reply"),
    path("collections.php", views.collections, name="collections"),
]
