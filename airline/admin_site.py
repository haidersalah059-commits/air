from django.conf import settings
from django.contrib import admin
from django.urls import path
from django.utils.translation import gettext_lazy as _


class AirlineAdminSite(admin.AdminSite):
    site_header = _("Airline Management System")
    site_title = _("Airline admin")
    index_title = _("Dashboard")

    def get_urls(self):
        from . import views

        custom = [
            path("book/", self.admin_view(views.book_search), name="airline_book_search"),
            path("book/add/", self.admin_view(views.book_add_leg), name="airline_book_add"),
            path("book/trip/", self.admin_view(views.book_cart), name="airline_book_cart"),
            path("book/passengers/", self.admin_view(views.book_passengers), name="airline_book_passengers"),
            path("book/<int:pk>/pay/", self.admin_view(views.booking_pay), name="airline_booking_pay"),
            path("bookings/<int:pk>/e-ticket/", self.admin_view(views.itinerary), name="airline_itinerary"),
            path("tickets/<int:pk>/boarding-pass/", self.admin_view(views.boarding_pass),
                 name="airline_boarding_pass"),
            path("reports/", self.admin_view(views.reports_view), name="airline_reports"),
        ]
        return custom + super().get_urls()

    def index(self, request, extra_context=None):
        from .reports import dashboard_stats

        extra_context = {**(extra_context or {}), "stats": dashboard_stats(),
                         "currency": settings.AIRLINE_CURRENCY}
        return super().index(request, extra_context)
