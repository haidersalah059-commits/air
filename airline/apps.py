from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig
from django.utils.translation import gettext_lazy as _


class AirlineAdminConfig(AdminConfig):
    default_site = "airline.admin_site.AirlineAdminSite"


class AirlineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "airline"
    verbose_name = _("Airline operations")
