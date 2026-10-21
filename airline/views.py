"""Custom admin pages: booking wizard, payment, reports, e-ticket and boarding pass."""
from datetime import timedelta
from functools import wraps

import segno
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from . import reports, services
from .forms import ContactForm, FlightSearchForm, PassengerFormSet, PaymentForm
from .models import Booking, Flight, SeatClass, Ticket
from .pricing import current_price

CART_KEY = "booking_cart"
BOARDING_STARTS_MINUTES = 40


def require_perm(perm):
    def decorator(view):
        @wraps(view)
        def inner(request, *args, **kwargs):
            if not request.user.has_perm(perm):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return inner
    return decorator


def _context(request, title, **extra):
    return {**admin.site.each_context(request), "title": title, "currency": settings.AIRLINE_CURRENCY, **extra}


def _get_cart(request):
    return request.session.get(CART_KEY) or {"passengers": None, "legs": []}


def _save_cart(request, cart):
    request.session[CART_KEY] = cart


def _cart_legs(cart):
    """Resolve the cart into flights, dropping legs that can no longer be booked."""
    flights = Flight.objects.select_related("origin", "destination", "aircraft").in_bulk(
        [leg["flight"] for leg in cart["legs"]])
    legs = []
    for leg in cart["legs"]:
        flight = flights.get(leg["flight"])
        if flight and flight.is_bookable:
            legs.append({"flight": flight, "seat_class": leg["seat_class"],
                         "class_label": SeatClass(leg["seat_class"]).label,
                         "price": current_price(flight, leg["seat_class"])})
    return legs


@require_perm("airline.add_booking")
def book_search(request):
    cart = _get_cart(request)
    initial = {}
    if "return_from" in request.GET:  # "add return flight" shortcut from the cart
        initial = {"origin": request.GET["return_from"], "destination": request.GET.get("return_to"),
                   "seat_class": request.GET.get("seat_class", SeatClass.ECONOMY)}
    bound = request.GET if "origin" in request.GET else None
    form = FlightSearchForm(bound, initial=initial, fixed_passengers=cart["passengers"])
    results = []
    if bound is not None and form.is_valid():
        data = form.cleaned_data
        passengers = cart["passengers"] or data["passengers"]
        for flight in services.search_flights(data["origin"], data["destination"], data["date"]):
            available = services.available_seats(flight, data["seat_class"]).count()
            results.append({"flight": flight, "available": available, "enough": available >= passengers,
                            "price": current_price(flight, data["seat_class"])})
    return render(request, "admin/airline/book_search.html", _context(
        request, _("New booking – search flights"), form=form, results=results, cart_legs=_cart_legs(cart),
        cart=cart, searched=bound is not None and form.is_valid()))


@require_POST
@require_perm("airline.add_booking")
def book_add_leg(request):
    cart = _get_cart(request)
    flight = get_object_or_404(Flight, pk=request.POST.get("flight"))
    seat_class = request.POST.get("seat_class")
    if seat_class not in SeatClass.values or not flight.is_bookable:
        messages.error(request, _("This flight cannot be booked."))
        return redirect("admin:airline_book_search")
    if not cart["passengers"]:
        try:
            cart["passengers"] = max(1, min(9, int(request.POST.get("passengers", 1))))
        except ValueError:
            cart["passengers"] = 1
    if any(leg["flight"] == flight.pk for leg in cart["legs"]):
        messages.warning(request, _("This flight is already in the trip."))
    else:
        cart["legs"].append({"flight": flight.pk, "seat_class": seat_class})
        _save_cart(request, cart)
    return redirect("admin:airline_book_cart")


@require_perm("airline.add_booking")
def book_cart(request):
    cart = _get_cart(request)
    if request.method == "POST":
        if "clear" in request.POST:
            request.session.pop(CART_KEY, None)
            return redirect("admin:airline_book_search")
        if "remove" in request.POST:
            index = int(request.POST["remove"]) if request.POST["remove"].isdigit() else -1
            if 0 <= index < len(cart["legs"]):
                cart["legs"].pop(index)
            if not cart["legs"]:
                cart["passengers"] = None
            _save_cart(request, cart)
        return redirect("admin:airline_book_cart")
    legs = _cart_legs(cart)
    if len(legs) != len(cart["legs"]):
        messages.warning(request, _("Some flights are no longer open for booking and were removed."))
        cart["legs"] = [{"flight": leg["flight"].pk, "seat_class": leg["seat_class"]} for leg in legs]
        _save_cart(request, cart)
    passengers = cart["passengers"] or 0
    total = sum((leg["price"] for leg in legs), 0) * passengers
    return render(request, "admin/airline/book_cart.html", _context(
        request, _("New booking – your trip"), legs=legs, passengers=passengers, total=total,
        last_leg=legs[-1] if legs else None))


@require_perm("airline.add_booking")
def book_passengers(request):
    cart = _get_cart(request)
    legs = _cart_legs(cart)
    if not legs or len(legs) != len(cart["legs"]):
        return redirect("admin:airline_book_cart")
    for leg in legs:
        flight = leg["flight"]
        leg["label"] = f"{flight.flight_no} {flight.origin.code}→{flight.destination.code}"
        leg["seat_choices"] = [(str(s.pk), s.seat_number)
                               for s in services.available_seats(flight, leg["seat_class"])]
    count = cart["passengers"]
    data = request.POST if request.method == "POST" else None
    formset = PassengerFormSet(data, initial=[{}] * count, form_kwargs={"legs": legs})
    contact = ContactForm(data)
    if data is not None and formset.is_valid() and contact.is_valid() and len(formset.forms) == count:
        try:
            with transaction.atomic():
                passengers = [form.save_passenger() for form in formset.forms]
                seat_ids = [[int(form.cleaned_data[f"seat_{i}"]) for form in formset.forms]
                            for i in range(len(legs))]
                booking = services.create_held_booking(
                    user=request.user, contact_email=contact.cleaned_data["contact_email"],
                    legs=[(leg["flight"], leg["seat_class"]) for leg in legs],
                    passengers=passengers, seat_ids=seat_ids)
        except services.BookingError as exc:
            messages.error(request, str(exc))
            return redirect("admin:airline_book_passengers")
        request.session.pop(CART_KEY, None)
        messages.success(request, _("Seats held for %(minutes)s minutes. Booking reference: %(pnr)s")
                         % {"minutes": settings.SEAT_HOLD_MINUTES, "pnr": booking.pnr})
        return redirect("admin:airline_booking_pay", pk=booking.pk)
    return render(request, "admin/airline/book_passengers.html", _context(
        request, _("New booking – passengers and seats"), formset=formset, contact=contact, legs=legs,
        hold_minutes=settings.SEAT_HOLD_MINUTES))


@require_perm("airline.add_booking")
def booking_pay(request, pk):
    services.release_expired_holds()
    booking = get_object_or_404(Booking, pk=pk)
    change_url = reverse("admin:airline_booking_change", args=[booking.pk])
    if booking.status != Booking.Status.PENDING:
        messages.info(request, _("Booking %(pnr)s is %(status)s.")
                      % {"pnr": booking.pnr, "status": booking.get_status_display()})
        return redirect(change_url)
    form = PaymentForm(request.POST or None)
    if request.method == "POST":
        if "cancel" in request.POST:
            services.cancel_booking(booking, user=request.user)
            messages.warning(request, _("Booking %(pnr)s was cancelled and the seats released.")
                             % {"pnr": booking.pnr})
            return redirect(change_url)
        if form.is_valid():
            try:
                services.pay_booking(booking, method=form.cleaned_data["method"],
                                     reference=form.cleaned_data["reference"], user=request.user)
            except services.BookingError as exc:
                messages.error(request, str(exc))
                return redirect(change_url)
            messages.success(request, _("Payment received. Booking %(pnr)s is confirmed and the e-tickets were "
                                        "emailed.") % {"pnr": booking.pnr})
            return redirect(change_url)
    tickets = booking.tickets.select_related("passenger", "flight__origin", "flight__destination", "seat")
    return render(request, "admin/airline/book_pay.html", _context(
        request, _("Payment for booking %(pnr)s") % {"pnr": booking.pnr}, booking=booking, tickets=tickets,
        form=form))


@require_perm("airline.view_reports")
def reports_view(request):
    occupancy = reports.flight_occupancy()
    return render(request, "admin/airline/reports.html", _context(
        request, _("Reports"), occupancy=occupancy, avg_occupancy=reports.average_occupancy(occupancy),
        monthly=reports.monthly_revenue(), total_revenue=reports.net_revenue(),
        destinations=reports.top_destinations(), cancellations=reports.cancellation_rate()))


@require_perm("airline.view_booking")
def itinerary(request, pk):
    booking = get_object_or_404(Booking, pk=pk)
    if booking.status != Booking.Status.CONFIRMED:
        messages.error(request, _("E-tickets are only available for confirmed bookings."))
        return redirect("admin:airline_booking_change", booking.pk)
    tickets = booking.tickets.exclude(status=Ticket.Status.CANCELLED).select_related(
        "passenger", "flight__origin", "flight__destination", "seat")
    return render(request, "airline/itinerary.html", {
        "booking": booking, "tickets": tickets, "payments": booking.payments.all(),
        "airline": settings.AIRLINE_NAME, "currency": settings.AIRLINE_CURRENCY})


@require_perm("airline.view_ticket")
def boarding_pass(request, pk):
    ticket = get_object_or_404(Ticket.objects.select_related(
        "booking", "passenger", "flight__origin", "flight__destination", "seat"), pk=pk)
    if ticket.status not in (Ticket.Status.CHECKED_IN, Ticket.Status.BOARDED):
        messages.error(request, _("The passenger must check in before a boarding pass can be printed."))
        return redirect("admin:airline_ticket_change", ticket.pk)
    payload = "|".join([ticket.booking.pnr, ticket.ticket_no, ticket.flight.flight_no,
                        ticket.flight.departure_time.strftime("%Y%m%d"), ticket.seat.seat_number,
                        ticket.passenger.last_name.upper(), ticket.passenger.first_name.upper()])
    qr_svg = mark_safe(segno.make(payload, error="m").svg_inline(scale=4))
    return render(request, "airline/boarding_pass.html", {
        "ticket": ticket, "qr_svg": qr_svg, "airline": settings.AIRLINE_NAME,
        "boarding_time": ticket.flight.departure_time - timedelta(minutes=BOARDING_STARTS_MINUTES)})
