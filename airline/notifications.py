"""Email/SMS notifications. Every message is logged in the Notification table.

Emails go through Django's email backend (console by default). SMS messages are
logged as unsent until an SMS gateway is wired into ``send_sms``.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import Notification

logger = logging.getLogger(__name__)


def send_sms(phone, text):
    """Hook for an SMS gateway (Twilio, etc.). Returns True when sent."""
    return False


def notify(subject, body, *, email="", phone="", booking=None, flight=None):
    if email:
        note = Notification(channel=Notification.Channel.EMAIL, recipient=email, subject=subject, body=body,
                            booking=booking, flight=flight)
        try:
            send_mail(subject, body, None, [email])
            note.sent = True
        except Exception as exc:  # never let a mail failure break a booking
            logger.exception("Email to %s failed", email)
            note.error = str(exc)[:500]
        note.save()
    if phone:
        sent = send_sms(phone, body)
        Notification.objects.create(channel=Notification.Channel.SMS, recipient=phone, subject=subject, body=body,
                                    booking=booking, flight=flight, sent=sent,
                                    error="" if sent else _("No SMS gateway configured"))


def _fmt(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M")


def _ticket_lines(tickets):
    return "\n".join(
        f"- {t.passenger.full_name}: {t.flight.flight_no} {t.flight.origin.code}→{t.flight.destination.code} "
        f"{_fmt(t.flight.departure_time)} — {_('Seat')} {t.seat.seat_number} ({t.get_seat_class_display()}) "
        f"— {_('Ticket')} {t.ticket_no}"
        for t in tickets
    )


def booking_confirmed(booking):
    tickets = list(booking.tickets.select_related("passenger", "flight__origin", "flight__destination", "seat"))
    subject = _("Booking %(pnr)s confirmed") % {"pnr": booking.pnr}
    body = "\n\n".join([
        _("Thank you for flying with %(airline)s.") % {"airline": settings.AIRLINE_NAME},
        _("Booking reference (PNR): %(pnr)s") % {"pnr": booking.pnr},
        _ticket_lines(tickets),
        _("Total paid: %(amount)s %(currency)s") % {"amount": booking.total_amount,
                                                     "currency": settings.AIRLINE_CURRENCY},
        _("Online check-in opens %(hours)s hours before departure.") % {"hours": settings.CHECKIN_OPENS_HOURS},
    ])
    phone = next((t.passenger.phone for t in tickets if t.passenger.phone), "")
    notify(subject, body, email=booking.contact_email, phone=phone, booking=booking)


def booking_cancelled(booking, tickets, refund):
    subject = _("Booking %(pnr)s cancelled") % {"pnr": booking.pnr}
    body = "\n\n".join([
        _("The following tickets were cancelled:"),
        _ticket_lines(tickets),
        _("Refund: %(amount)s %(currency)s") % {"amount": refund, "currency": settings.AIRLINE_CURRENCY},
    ])
    notify(subject, body, email=booking.contact_email, booking=booking)


def flight_changed(flight, changes, tickets):
    subject = _("Update for flight %(flight)s") % {"flight": flight.flight_no}
    body = "\n".join([
        _("Flight %(flight)s %(origin)s→%(destination)s on %(date)s has been updated:") % {
            "flight": flight.flight_no, "origin": flight.origin.code, "destination": flight.destination.code,
            "date": _fmt(flight.departure_time)},
        *[f"- {c}" for c in changes],
    ])
    seen = set()
    for t in tickets:
        email = t.passenger.email or t.booking.contact_email
        key = (email, t.passenger.phone)
        if key in seen:
            continue
        seen.add(key)
        notify(subject, body, email=email, phone=t.passenger.phone, booking=t.booking, flight=flight)
