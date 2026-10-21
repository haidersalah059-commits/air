from django.contrib import admin, messages
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext, gettext_lazy as _

from . import services
from .models import (
    Aircraft, Airport, Baggage, Booking, Crew, Flight, FlightCrew, Maintenance, Notification, Passenger, Payment,
    Seat, SeatClass, Ticket,
)
from .pricing import current_price

STATUS_COLORS = {
    "scheduled": "#2e7d32", "delayed": "#ef6c00", "boarding": "#1565c0", "departed": "#6a1b9a",
    "arrived": "#455a64", "cancelled": "#c62828", "pending": "#ef6c00", "confirmed": "#2e7d32",
    "expired": "#757575", "held": "#ef6c00", "issued": "#1565c0", "checked_in": "#2e7d32", "boarded": "#6a1b9a",
    "active": "#2e7d32", "maintenance": "#ef6c00", "retired": "#757575", "paid": "#2e7d32", "refund": "#6a1b9a",
    "failed": "#c62828", "checked": "#1565c0", "loaded": "#6a1b9a", "delivered": "#455a64", "lost": "#c62828",
}
CLASS_COLORS = {SeatClass.FIRST: "#b8860b", SeatClass.BUSINESS: "#1565c0", SeatClass.ECONOMY: "#2e7d32"}


def badge(value, label):
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 9px;border-radius:10px;font-size:11px;'
        'white-space:nowrap">{}</span>', STATUS_COLORS.get(value, "#757575"), label)


def seat_map_html(aircraft, taken=frozenset()):
    rows = {}
    for seat in aircraft.seats.all():
        rows.setdefault(seat.row, []).append(seat)
    lines = []
    for row, seats in rows.items():
        aisle = len(seats) // 2
        cells = []
        for i, seat in enumerate(seats):
            if i == aisle:
                cells.append(mark_safe('<span style="display:inline-block;width:18px"></span>'))
            color = "#bdbdbd" if seat.pk in taken else CLASS_COLORS[seat.seat_class]
            cells.append(format_html(
                '<span title="{}" style="display:inline-block;width:34px;margin:2px;padding:3px 0;text-align:center;'
                'border-radius:5px 5px 2px 2px;color:#fff;font-size:11px;background:{}">{}</span>',
                seat.get_seat_class_display(), color, seat.seat_number))
        lines.append(format_html('<div style="white-space:nowrap" dir="ltr">{}</div>', mark_safe("".join(cells))))
    legend = " ".join(format_html('<span style="color:{}">■</span> {}', CLASS_COLORS[c], c.label)
                      for c in SeatClass)
    return format_html('<div>{}</div><div style="margin-top:6px">{}</div>', mark_safe("".join(lines)),
                       mark_safe(legend))


# ---------------------------------------------------------------- reference data

@admin.register(Airport)
class AirportAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "city", "country")
    search_fields = ("code", "name", "city", "country")
    list_filter = ("country",)


class MaintenanceInline(admin.TabularInline):
    model = Maintenance
    extra = 0


@admin.register(Aircraft)
class AircraftAdmin(admin.ModelAdmin):
    list_display = ("registration", "model", "first_count", "business_count", "economy_count", "total_display",
                    "status_badge")
    list_filter = ("status",)
    search_fields = ("registration", "model")
    readonly_fields = ("seat_map",)
    inlines = [MaintenanceInline]
    fieldsets = (
        (None, {"fields": ("registration", "model", "status")}),
        (_("Seat layout"), {
            "fields": (("first_rows", "first_seats_per_row"), ("business_rows", "business_seats_per_row"),
                       ("economy_rows", "economy_seats_per_row")),
            "description": _("Seats are generated automatically from rows × seats per row."),
        }),
        (_("Seat map"), {"fields": ("seat_map",)}),
    )

    @admin.display(description=_("First"))
    def first_count(self, obj):
        return obj.seats_in_class(SeatClass.FIRST)

    @admin.display(description=_("Business"))
    def business_count(self, obj):
        return obj.seats_in_class(SeatClass.BUSINESS)

    @admin.display(description=_("Economy"))
    def economy_count(self, obj):
        return obj.seats_in_class(SeatClass.ECONOMY)

    @admin.display(description=_("total seats"))
    def total_display(self, obj):
        return obj.total_seats

    @admin.display(description=_("status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description=_("Seat map"))
    def seat_map(self, obj):
        return seat_map_html(obj) if obj.pk else "-"


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ("seat_number", "aircraft", "seat_class")
    list_filter = ("aircraft", "seat_class")
    search_fields = ("seat_number", "aircraft__registration")

    def has_add_permission(self, request):
        return False  # seats come from the aircraft layout

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Crew)
class CrewAdmin(admin.ModelAdmin):
    list_display = ("name", "role", "license_no", "hours_this_month", "max_monthly_hours", "active")
    list_filter = ("role", "active")
    search_fields = ("name", "license_no")

    @admin.display(description=_("hours this month"))
    def hours_this_month(self, obj):
        now = timezone.localtime()
        return f"{obj.flight_hours(now.year, now.month):.1f}"


# ---------------------------------------------------------------- flights

class FlightCrewInline(admin.TabularInline):
    model = FlightCrew
    extra = 0
    autocomplete_fields = ("crew",)


def _status_action(status, description):
    @admin.action(description=description, permissions=["change"])
    def action(modeladmin, request, queryset):
        for flight in queryset:
            services.set_flight_status(flight, status, user=request.user)
        modeladmin.message_user(request, gettext("%(count)d flight(s) updated; passengers were notified.")
                                % {"count": len(queryset)}, messages.SUCCESS)
    action.__name__ = f"mark_{status}"
    return action


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = ("flight_no", "route", "departure_time", "arrival_time", "status_badge", "gate", "aircraft",
                    "occupancy_display", "economy_fare")
    list_filter = ("status", "origin", "destination", "aircraft")
    list_select_related = ("origin", "destination", "aircraft")
    date_hierarchy = "departure_time"
    search_fields = ("flight_no", "origin__code", "destination__code", "origin__city", "destination__city")
    autocomplete_fields = ("aircraft", "origin", "destination")
    inlines = [FlightCrewInline]
    fieldsets = (
        (None, {"fields": ("flight_no", ("origin", "destination"), ("departure_time", "arrival_time"), "aircraft")}),
        (_("Operations"), {"fields": ("status", "gate", "base_price")}),
    )
    actions = [
        _status_action(Flight.Status.SCHEDULED, _("Mark as on time")),
        _status_action(Flight.Status.DELAYED, _("Mark as delayed")),
        _status_action(Flight.Status.BOARDING, _("Mark as boarding")),
        _status_action(Flight.Status.DEPARTED, _("Mark as departed")),
        _status_action(Flight.Status.ARRIVED, _("Mark as arrived")),
        "cancel_flights",
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            sold_count=Count("tickets", filter=~Q(tickets__status=Ticket.Status.CANCELLED)))

    def _can_manage(self, request):
        return request.user.has_perm("airline.manage_operations")

    def has_manage_operations_permission(self, request):
        return self._can_manage(request)

    def get_readonly_fields(self, request, obj=None):
        if self._can_manage(request):
            return ()
        # Airport staff may only update the gate and the status.
        return ("flight_no", "origin", "destination", "departure_time", "arrival_time", "aircraft", "base_price")

    def save_model(self, request, obj, form, change):
        before = services.flight_snapshot(Flight.objects.get(pk=obj.pk)) if change else None
        super().save_model(request, obj, form, change)
        if before:
            changes = services.flight_updated(obj, before, user=request.user)
            if changes:
                self.message_user(request, gettext("Passengers were notified: %(changes)s")
                                  % {"changes": "; ".join(changes)}, messages.INFO)

    @admin.display(description=_("route"))
    def route(self, obj):
        return f"{obj.origin.code} → {obj.destination.code}"

    @admin.display(description=_("status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description=_("occupancy"))
    def occupancy_display(self, obj):
        pct = round(obj.occupancy * 100)
        return format_html('<span title="{} / {}">{}%</span>', obj.sold_seats, obj.aircraft.total_seats, pct)

    @admin.display(description=_("fare from"))
    def economy_fare(self, obj):
        for seat_class in (SeatClass.ECONOMY, SeatClass.BUSINESS, SeatClass.FIRST):
            if obj.aircraft.seats_in_class(seat_class):
                return current_price(obj, seat_class)
        return "-"

    @admin.action(description=_("Cancel flights (refund all passengers)"), permissions=["manage_operations"])
    def cancel_flights(self, request, queryset):
        count = 0
        for flight in queryset.exclude(status=Flight.Status.CANCELLED):
            services.set_flight_status(flight, Flight.Status.CANCELLED, user=request.user)
            count += 1
        self.message_user(request, gettext("%(count)d flight(s) cancelled; tickets refunded and passengers notified.")
                          % {"count": count}, messages.WARNING)


# ---------------------------------------------------------------- passengers & bookings

@admin.register(Passenger)
class PassengerAdmin(admin.ModelAdmin):
    list_display = ("full_name", "passport_no", "nationality", "email", "phone", "miles", "tier", "tickets_link")
    search_fields = ("first_name", "last_name", "passport_no", "email", "phone")
    list_filter = ("nationality",)

    @admin.display(description=_("name"), ordering="last_name")
    def full_name(self, obj):
        return obj.full_name

    @admin.display(description=_("tier"))
    def tier(self, obj):
        return obj.tier

    @admin.display(description=_("tickets"))
    def tickets_link(self, obj):
        url = reverse("admin:airline_ticket_changelist") + f"?passenger__id__exact={obj.pk}"
        return format_html('<a href="{}">{}</a>', url, gettext("View"))


class TicketInline(admin.TabularInline):
    model = Ticket
    fields = ("ticket_no", "passenger", "flight", "seat", "seat_class", "price", "status")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class PaymentInline(admin.TabularInline):
    model = Payment
    fields = ("paid_at", "status", "method", "amount", "reference", "recorded_by")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("pnr", "contact_email", "booking_date", "status_badge", "total_amount", "paid_display",
                    "links")
    list_filter = ("status", "booking_date")
    search_fields = ("pnr", "contact_email", "tickets__passenger__last_name", "tickets__passenger__passport_no",
                     "tickets__ticket_no")
    fields = ("pnr", "status", "contact_email", "total_amount", "paid_display", "hold_expires_at", "created_by",
              "booking_date", "links")
    readonly_fields = ("pnr", "status", "total_amount", "paid_display", "hold_expires_at", "created_by",
                       "booking_date", "links")
    inlines = [TicketInline, PaymentInline]
    actions = ["cancel_bookings"]

    def get_queryset(self, request):
        services.release_expired_holds()
        return super().get_queryset(request)

    def add_view(self, request, form_url="", extra_context=None):
        return redirect("admin:airline_book_search")  # new bookings go through the booking wizard

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description=_("status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description=_("paid"))
    def paid_display(self, obj):
        return obj.paid_total

    @admin.display(description=_("actions"))
    def links(self, obj):
        if obj.status == Booking.Status.PENDING:
            return format_html('<a class="button" href="{}">{}</a>',
                               reverse("admin:airline_booking_pay", args=[obj.pk]), gettext("Pay"))
        if obj.status == Booking.Status.CONFIRMED:
            return format_html('<a class="button" href="{}" target="_blank">{}</a>',
                               reverse("admin:airline_itinerary", args=[obj.pk]), gettext("E-ticket / invoice"))
        return "-"

    @admin.action(description=_("Cancel selected bookings (refund per policy)"), permissions=["change"])
    def cancel_bookings(self, request, queryset):
        for booking in queryset:
            try:
                refund = services.cancel_booking(booking, user=request.user)
                self.message_user(request, gettext("%(pnr)s cancelled, refund %(amount)s.")
                                  % {"pnr": booking.pnr, "amount": refund}, messages.SUCCESS)
            except services.BookingError as exc:
                self.message_user(request, str(exc), messages.ERROR)


class BaggageInline(admin.TabularInline):
    model = Baggage
    fields = ("tag_no", "weight", "status")
    readonly_fields = ("tag_no",)
    extra = 0


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("ticket_no", "pnr", "passenger", "flight", "seat", "seat_class", "status_badge", "baggage_info",
                    "boarding_pass_link")
    list_filter = ("status", "seat_class", ("flight__departure_time", admin.DateFieldListFilter))
    list_select_related = ("booking", "passenger", "flight__origin", "flight__destination", "seat")
    search_fields = ("ticket_no", "booking__pnr", "passenger__first_name", "passenger__last_name",
                     "passenger__passport_no", "flight__flight_no")
    fields = ("ticket_no", "booking", "passenger", "flight", "seat", "seat_class", "price", "status",
              "checked_in_at", "baggage_info", "boarding_pass_link")
    inlines = [BaggageInline]
    actions = ["check_in_tickets", "board_tickets", "cancel_tickets"]

    def has_add_permission(self, request):
        return False  # tickets are issued by the booking wizard

    def get_readonly_fields(self, request, obj=None):
        readonly = [f for f in self.fields if f != "seat"]
        if (obj is None or obj.status not in (Ticket.Status.HELD, Ticket.Status.ISSUED)
                or not request.user.has_perm("airline.change_booking")):
            readonly.append("seat")
        return readonly

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is not None and "seat" in form.base_fields:
            # Seat change: offer only free seats of the same class on this flight.
            free = services.available_seats(obj.flight, obj.seat_class).values("pk")
            form.base_fields["seat"].queryset = Seat.objects.filter(Q(pk__in=free) | Q(pk=obj.seat_id))
        return form

    def has_check_in_permission(self, request):
        return request.user.has_perm("airline.check_in_ticket")

    def has_cancel_permission(self, request):
        return request.user.has_perm("airline.change_booking")

    @admin.display(description=_("PNR"), ordering="booking__pnr")
    def pnr(self, obj):
        return format_html('<a href="{}">{}</a>', reverse("admin:airline_booking_change", args=[obj.booking_id]),
                           obj.booking.pnr)

    @admin.display(description=_("status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description=_("baggage"))
    def baggage_info(self, obj):
        weight, allowance = obj.baggage_weight, obj.baggage_allowance
        if not weight:
            return f"0 / {allowance} kg"
        color = "#c62828" if weight > allowance else "inherit"
        return format_html('<span style="color:{}">{} / {} kg</span>', color, weight, allowance)

    @admin.display(description=_("boarding pass"))
    def boarding_pass_link(self, obj):
        if obj.status in (Ticket.Status.CHECKED_IN, Ticket.Status.BOARDED):
            return format_html('<a href="{}" target="_blank">{}</a>',
                               reverse("admin:airline_boarding_pass", args=[obj.pk]), gettext("Print"))
        return "-"

    def _run(self, request, queryset, func, done_message):
        done = 0
        for ticket in queryset:
            try:
                func(ticket)
                done += 1
            except services.BookingError as exc:
                self.message_user(request, str(exc), messages.ERROR)
        if done:
            self.message_user(request, done_message % {"count": done}, messages.SUCCESS)

    @admin.action(description=_("Check in selected passengers"), permissions=["check_in"])
    def check_in_tickets(self, request, queryset):
        self._run(request, queryset, services.check_in, gettext("%(count)d passenger(s) checked in."))

    @admin.action(description=_("Mark selected passengers as boarded"), permissions=["check_in"])
    def board_tickets(self, request, queryset):
        self._run(request, queryset, services.board, gettext("%(count)d passenger(s) boarded."))

    @admin.action(description=_("Cancel selected tickets (refund per policy)"), permissions=["cancel"])
    def cancel_tickets(self, request, queryset):
        try:
            results = services.cancel_tickets(list(queryset), user=request.user)
        except services.BookingError as exc:
            self.message_user(request, str(exc), messages.ERROR)
            return
        refund = sum(results.values())
        self.message_user(request, gettext("Tickets cancelled, total refund %(amount)s.") % {"amount": refund},
                          messages.SUCCESS)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("paid_at", "booking", "status_badge", "method", "amount", "reference", "recorded_by")
    list_filter = ("status", "method", "paid_at")
    search_fields = ("booking__pnr", "reference")
    list_select_related = ("booking", "recorded_by")

    @admin.display(description=_("status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def _baggage_action(status, description):
    @admin.action(description=description, permissions=["change"])
    def action(modeladmin, request, queryset):
        count = queryset.update(status=status, updated_at=timezone.now())
        modeladmin.message_user(request, gettext("%(count)d bag(s) updated.") % {"count": count}, messages.SUCCESS)
    action.__name__ = f"mark_{status}"
    return action


@admin.register(Baggage)
class BaggageAdmin(admin.ModelAdmin):
    list_display = ("tag_no", "ticket", "passenger", "flight", "weight", "status_badge", "updated_at")
    list_filter = ("status",)
    search_fields = ("tag_no", "ticket__ticket_no", "ticket__booking__pnr", "ticket__passenger__last_name")
    list_select_related = ("ticket__passenger", "ticket__flight__origin", "ticket__flight__destination")
    autocomplete_fields = ("ticket",)
    readonly_fields = ("tag_no",)
    actions = [
        _baggage_action(Baggage.Status.LOADED, _("Mark as loaded")),
        _baggage_action(Baggage.Status.ARRIVED, _("Mark as arrived")),
        _baggage_action(Baggage.Status.DELIVERED, _("Mark as delivered")),
        _baggage_action(Baggage.Status.LOST, _("Mark as lost")),
    ]

    @admin.display(description=_("passenger"))
    def passenger(self, obj):
        return obj.ticket.passenger.full_name

    @admin.display(description=_("flight"))
    def flight(self, obj):
        return obj.ticket.flight

    @admin.display(description=_("status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "channel", "recipient", "subject", "sent")
    list_filter = ("channel", "sent")
    search_fields = ("recipient", "subject", "booking__pnr")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
