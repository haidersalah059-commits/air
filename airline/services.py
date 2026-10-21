"""Booking workflow: seat holds, payment, cancellation/refunds, check-in and flight updates."""
import secrets
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.utils import timezone
from django.utils.translation import gettext as _

from . import notifications
from .models import Booking, Flight, Passenger, Payment, Seat, Ticket
from .pricing import current_price


class BookingError(Exception):
    pass


def release_expired_holds():
    """Free the seats of unpaid bookings whose hold has run out."""
    expired = list(Booking.objects.filter(status=Booking.Status.PENDING, hold_expires_at__lt=timezone.now())
                   .values_list("pk", flat=True))
    if expired:
        with transaction.atomic():
            Ticket.objects.filter(booking_id__in=expired).exclude(status=Ticket.Status.CANCELLED).update(
                status=Ticket.Status.CANCELLED)
            Booking.objects.filter(pk__in=expired).update(status=Booking.Status.EXPIRED)
    return len(expired)


def available_seats(flight, seat_class=None):
    release_expired_holds()
    taken = Ticket.objects.filter(flight=flight).exclude(status=Ticket.Status.CANCELLED).values("seat_id")
    seats = Seat.objects.filter(aircraft_id=flight.aircraft_id).exclude(pk__in=taken)
    if seat_class:
        seats = seats.filter(seat_class=seat_class)
    return seats


def search_flights(origin, destination, day):
    start = timezone.make_aware(datetime.combine(day, time.min))
    return (Flight.objects.filter(origin=origin, destination=destination, departure_time__gte=max(start, timezone.now()),
                                  departure_time__lt=start + timedelta(days=1), status__in=Flight.BOOKABLE)
            .select_related("origin", "destination", "aircraft").order_by("departure_time"))


def miles_for(ticket):
    """Loyalty programme: one mile per currency unit paid."""
    return int(ticket.price)


@transaction.atomic
def create_held_booking(*, user, contact_email, legs, passengers, seat_ids):
    """Create a PENDING booking whose seats are held for SEAT_HOLD_MINUTES.

    legs: [(flight, seat_class)], passengers: [Passenger], seat_ids[leg_index][passenger_index] = seat pk.
    """
    release_expired_holds()
    # Lock the flights so concurrent bookings on them are serialised.
    locked = {f.pk: f for f in Flight.objects.select_for_update().select_related("aircraft")
              .filter(pk__in=[flight.pk for flight, _cls in legs])}
    booking = Booking.objects.create(
        created_by=user if user is not None and user.is_authenticated else None,
        contact_email=contact_email,
        hold_expires_at=timezone.now() + timedelta(minutes=settings.SEAT_HOLD_MINUTES),
    )
    total = Decimal("0")
    for leg_index, (flight, seat_class) in enumerate(legs):
        flight = locked[flight.pk]
        if not flight.is_bookable:
            raise BookingError(_("Flight %(flight)s is no longer open for booking.") % {"flight": flight.flight_no})
        price = current_price(flight, seat_class)
        for pax_index, passenger in enumerate(passengers):
            seat = Seat.objects.filter(pk=seat_ids[leg_index][pax_index], aircraft_id=flight.aircraft_id,
                                       seat_class=seat_class).first()
            if seat is None:
                raise BookingError(_("Invalid seat selected for flight %(flight)s.") % {"flight": flight.flight_no})
            try:
                with transaction.atomic():
                    Ticket.objects.create(booking=booking, passenger=passenger, flight=flight, seat=seat,
                                          seat_class=seat_class, price=price, status=Ticket.Status.HELD)
            except IntegrityError:
                raise BookingError(
                    _("Seat %(seat)s on flight %(flight)s was just taken, or %(name)s already has a seat on this "
                      "flight. Please choose again.")
                    % {"seat": seat.seat_number, "flight": flight.flight_no, "name": passenger.full_name})
            total += price
    booking.total_amount = total
    booking.save(update_fields=["total_amount"])
    return booking


def pay_booking(booking, *, method, reference="", user=None):
    release_expired_holds()
    return _pay(booking, method, reference, user)


@transaction.atomic
def _pay(booking, method, reference, user):
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if booking.status == Booking.Status.EXPIRED:
        raise BookingError(_("The seat hold expired. Please make a new booking."))
    if booking.status != Booking.Status.PENDING:
        raise BookingError(_("This booking is not awaiting payment."))
    Payment.objects.create(booking=booking, amount=booking.total_amount, method=method, status=Payment.Status.PAID,
                           reference=reference or f"TEST-{secrets.token_hex(4).upper()}",
                           recorded_by=user if user is not None and user.is_authenticated else None)
    booking.status = Booking.Status.CONFIRMED
    booking.hold_expires_at = None
    booking.save(update_fields=["status", "hold_expires_at"])
    for ticket in booking.tickets.filter(status=Ticket.Status.HELD):
        ticket.status = Ticket.Status.ISSUED
        ticket.save(update_fields=["status"])
        Passenger.objects.filter(pk=ticket.passenger_id).update(miles=F("miles") + miles_for(ticket))
    transaction.on_commit(lambda: notifications.booking_confirmed(booking))
    return booking


def refund_ratio(ticket, now=None):
    """Refund policy: 100% if cancelled 72h+ before departure, 50% if 24h+, otherwise nothing.
    Tickets on a flight the airline cancelled are always refunded in full."""
    if ticket.flight.status == Flight.Status.CANCELLED:
        return Decimal("1")
    hours = (ticket.flight.departure_time - (now or timezone.now())).total_seconds() / 3600
    if hours >= 72:
        return Decimal("1")
    if hours >= 24:
        return Decimal("0.5")
    return Decimal("0")


@transaction.atomic
def cancel_tickets(tickets, *, full_refund=False, user=None, notify=True):
    """Cancel tickets, refund confirmed ones per policy. Returns {booking: refund}."""
    now = timezone.now()
    by_booking = defaultdict(list)
    for ticket in (Ticket.objects.select_for_update().select_related("booking", "flight", "passenger", "seat")
                   .filter(pk__in=[t.pk for t in tickets])):
        if ticket.status == Ticket.Status.CANCELLED:
            continue
        if ticket.status == Ticket.Status.BOARDED:
            raise BookingError(_("Ticket %(ticket)s is already boarded and cannot be cancelled.")
                               % {"ticket": ticket.ticket_no})
        by_booking[ticket.booking].append(ticket)

    results = {}
    for booking, items in by_booking.items():
        refund = Decimal("0")
        for ticket in items:
            if booking.status == Booking.Status.CONFIRMED:
                ratio = Decimal("1") if full_refund else refund_ratio(ticket, now)
                refund += (ticket.price * ratio).quantize(Decimal("0.01"))
                Passenger.objects.filter(pk=ticket.passenger_id).update(
                    miles=Greatest(F("miles") - miles_for(ticket), Value(0)))
            ticket.status = Ticket.Status.CANCELLED
            ticket.save(update_fields=["status"])
        if refund > 0:
            last = booking.payments.filter(status=Payment.Status.PAID).order_by("-paid_at").first()
            Payment.objects.create(booking=booking, amount=refund, status=Payment.Status.REFUND,
                                   method=last.method if last else Payment.Method.CASH,
                                   reference=f"RF-{secrets.token_hex(4).upper()}",
                                   recorded_by=user if user is not None and user.is_authenticated else None)
        if not booking.tickets.exclude(status=Ticket.Status.CANCELLED).exists():
            booking.status = Booking.Status.CANCELLED
            booking.hold_expires_at = None
            booking.save(update_fields=["status", "hold_expires_at"])
        results[booking] = refund
        if notify:
            transaction.on_commit(lambda b=booking, i=items, r=refund: notifications.booking_cancelled(b, i, r))
    return results


def cancel_booking(booking, *, user=None):
    if booking.status in (Booking.Status.CANCELLED, Booking.Status.EXPIRED):
        raise BookingError(_("Booking %(pnr)s is already closed.") % {"pnr": booking.pnr})
    with transaction.atomic():
        results = cancel_tickets(list(booking.tickets.exclude(status=Ticket.Status.CANCELLED)), user=user)
        Booking.objects.filter(pk=booking.pk).update(status=Booking.Status.CANCELLED, hold_expires_at=None)
    return sum(results.values(), Decimal("0"))


def check_in(ticket, *, enforce_window=True):
    ticket = Ticket.objects.select_related("booking", "flight").get(pk=ticket.pk)
    flight = ticket.flight
    if ticket.status != Ticket.Status.ISSUED or ticket.booking.status != Booking.Status.CONFIRMED:
        raise BookingError(_("Ticket %(ticket)s cannot be checked in (status: %(status)s).")
                           % {"ticket": ticket.ticket_no, "status": ticket.get_status_display()})
    if flight.status not in (Flight.Status.SCHEDULED, Flight.Status.DELAYED, Flight.Status.BOARDING):
        raise BookingError(_("Flight %(flight)s is %(status)s.")
                           % {"flight": flight.flight_no, "status": flight.get_status_display()})
    now = timezone.now()
    opens = flight.departure_time - timedelta(hours=settings.CHECKIN_OPENS_HOURS)
    closes = flight.departure_time - timedelta(minutes=settings.CHECKIN_CLOSES_MINUTES)
    if enforce_window and not opens <= now <= closes:
        raise BookingError(_("Check-in for flight %(flight)s is open from %(opens)s until %(closes)s.") % {
            "flight": flight.flight_no,
            "opens": timezone.localtime(opens).strftime("%Y-%m-%d %H:%M"),
            "closes": timezone.localtime(closes).strftime("%Y-%m-%d %H:%M")})
    ticket.status = Ticket.Status.CHECKED_IN
    ticket.checked_in_at = now
    ticket.save(update_fields=["status", "checked_in_at"])
    return ticket


def board(ticket):
    if ticket.status != Ticket.Status.CHECKED_IN:
        raise BookingError(_("Ticket %(ticket)s must be checked in before boarding.") % {"ticket": ticket.ticket_no})
    ticket.status = Ticket.Status.BOARDED
    ticket.save(update_fields=["status"])
    return ticket


TRACKED_FLIGHT_FIELDS = ("status", "gate", "departure_time")


def flight_snapshot(flight):
    return {f: getattr(flight, f) for f in TRACKED_FLIGHT_FIELDS}


def flight_updated(flight, before, *, user=None):
    """Call after saving a flight. Cancels/refunds tickets if the flight was cancelled and notifies passengers."""
    tickets = list(flight.tickets.exclude(status__in=[Ticket.Status.CANCELLED, Ticket.Status.HELD])
                   .select_related("booking", "passenger"))
    changes = []
    if before["status"] != flight.status:
        changes.append(_("Status: %(status)s") % {"status": flight.get_status_display()})
        if flight.status == Flight.Status.CANCELLED:
            cancel_tickets(list(flight.tickets.exclude(status=Ticket.Status.CANCELLED)), full_refund=True,
                           user=user, notify=False)
            changes.append(_("Your ticket has been cancelled and fully refunded."))
    if before["gate"] != flight.gate and flight.gate:
        changes.append(_("Gate: %(gate)s") % {"gate": flight.gate})
    if before["departure_time"] != flight.departure_time:
        changes.append(_("New departure time: %(time)s")
                       % {"time": timezone.localtime(flight.departure_time).strftime("%Y-%m-%d %H:%M")})
    if changes and tickets:
        transaction.on_commit(lambda: notifications.flight_changed(flight, changes, tickets))
    return changes


@transaction.atomic
def set_flight_status(flight, status, *, user=None):
    before = flight_snapshot(flight)
    flight.status = status
    flight.save(update_fields=["status"])
    return flight_updated(flight, before, user=user)
