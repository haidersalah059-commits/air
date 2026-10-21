from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services
from .models import Aircraft, Airport, Booking, Crew, Flight, FlightCrew, Passenger, Payment, SeatClass, Ticket
from .pricing import current_price


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", LANGUAGE_CODE="en")
class AirlineTestCase(TestCase):
    def setUp(self):
        self.bgw = Airport.objects.create(code="BGW", name="Baghdad", city="Baghdad", country="Iraq")
        self.dxb = Airport.objects.create(code="DXB", name="Dubai", city="Dubai", country="UAE")
        self.aircraft = Aircraft.objects.create(registration="YI-T01", model="A320", business_rows=1,
                                                business_seats_per_row=4, economy_rows=2, economy_seats_per_row=6)
        self.flight = self.make_flight(days=30)
        self.pax = [Passenger.objects.create(first_name=f"P{i}", last_name="Test", passport_no=f"X{i}",
                                             nationality="Iraqi", email=f"p{i}@example.com") for i in range(3)]

    def make_flight(self, days=30, hours=0, no="DA1", aircraft=None):
        dep = timezone.now() + timedelta(days=days, hours=hours)
        return Flight.objects.create(flight_no=no, aircraft=aircraft or self.aircraft, origin=self.bgw,
                                     destination=self.dxb, departure_time=dep, arrival_time=dep + timedelta(hours=3),
                                     base_price=Decimal("100"))

    def book(self, passengers, flight=None, seat_class=SeatClass.ECONOMY, pay=True):
        flight = flight or self.flight
        seats = list(services.available_seats(flight, seat_class).values_list("pk", flat=True)[:len(passengers)])
        booking = services.create_held_booking(user=None, contact_email="c@example.com",
                                               legs=[(flight, seat_class)], passengers=passengers, seat_ids=[seats])
        if pay:
            services.pay_booking(booking, method="card")
            booking.refresh_from_db()
        return booking


class AircraftTests(AirlineTestCase):
    def test_seats_generated_from_layout(self):
        self.assertEqual(self.aircraft.seats.count(), 16)
        self.assertEqual(self.aircraft.seats.filter(seat_class=SeatClass.BUSINESS).count(), 4)
        self.assertTrue(self.aircraft.seats.filter(seat_number="3F").exists())

    def test_layout_locked_after_tickets(self):
        self.book(self.pax[:1])
        self.aircraft.economy_rows = 5
        with self.assertRaises(ValidationError):
            self.aircraft.full_clean()


class FlightValidationTests(AirlineTestCase):
    def test_aircraft_cannot_overlap(self):
        clash = Flight(flight_no="DA2", aircraft=self.aircraft, origin=self.dxb, destination=self.bgw,
                       departure_time=self.flight.departure_time + timedelta(hours=1),
                       arrival_time=self.flight.arrival_time + timedelta(hours=2), base_price=100)
        with self.assertRaises(ValidationError):
            clash.full_clean()

    def test_crew_monthly_hours_limit(self):
        crew = Crew.objects.create(name="Pilot", role=Crew.Role.CAPTAIN, license_no="L1", max_monthly_hours=5)
        FlightCrew.objects.create(flight=self.flight, crew=crew)
        second = self.make_flight(days=30, hours=6, no="DA3")
        with self.assertRaises(ValidationError):
            FlightCrew(flight=second, crew=crew).full_clean()


class PricingTests(AirlineTestCase):
    def test_price_rises_close_to_departure_and_by_class(self):
        soon = self.make_flight(days=2, no="DA9", aircraft=Aircraft.objects.create(
            registration="YI-T02", model="A320", economy_rows=2, economy_seats_per_row=6))
        self.assertEqual(current_price(self.flight, SeatClass.ECONOMY), Decimal("100.00"))
        self.assertEqual(current_price(soon, SeatClass.ECONOMY), Decimal("150.00"))
        self.assertEqual(current_price(self.flight, SeatClass.BUSINESS), Decimal("250.00"))

    def test_price_rises_with_occupancy(self):
        self.book(self.pax)
        self.assertGreater(current_price(self.flight, SeatClass.ECONOMY), Decimal("100.00"))


class BookingFlowTests(AirlineTestCase):
    def test_pay_confirms_issues_awards_miles_and_emails(self):
        with self.captureOnCommitCallbacks(execute=True):
            booking = self.book(self.pax[:2])
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(booking.tickets.filter(status=Ticket.Status.ISSUED).count(), 2)
        self.assertEqual(booking.paid_total, Decimal("200.00"))
        self.pax[0].refresh_from_db()
        self.assertEqual(self.pax[0].miles, 100)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(booking.pnr, mail.outbox[0].subject)

    def test_same_seat_cannot_be_sold_twice(self):
        first = self.book(self.pax[:1], pay=False)
        seat = first.tickets.get().seat
        with self.assertRaises(services.BookingError):
            services.create_held_booking(user=None, contact_email="c@example.com",
                                         legs=[(self.flight, SeatClass.ECONOMY)], passengers=[self.pax[1]],
                                         seat_ids=[[seat.pk]])

    def test_expired_hold_releases_seat_and_blocks_payment(self):
        booking = self.book(self.pax[:1], pay=False)
        seat = booking.tickets.get().seat
        Booking.objects.filter(pk=booking.pk).update(hold_expires_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(services.BookingError):
            services.pay_booking(booking, method="card")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.EXPIRED)
        self.assertIn(seat, services.available_seats(self.flight))

    def test_cancel_refund_policy(self):
        booking = self.book(self.pax[:1])  # 30 days out: full refund
        self.assertEqual(services.cancel_booking(booking), Decimal("100.00"))
        late = self.make_flight(days=2, no="DA5", aircraft=Aircraft.objects.create(
            registration="YI-T03", model="A320", economy_rows=2, economy_seats_per_row=6))
        booking = self.book(self.pax[1:2], flight=late)  # 48h out: 50%
        price = booking.total_amount
        self.assertEqual(services.cancel_booking(booking), (price / 2).quantize(Decimal("0.01")))
        self.assertTrue(Payment.objects.filter(booking=booking, status=Payment.Status.REFUND).exists())

    def test_flight_cancellation_refunds_everyone(self):
        booking = self.book(self.pax[:2])
        services.set_flight_status(self.flight, Flight.Status.CANCELLED)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(booking.paid_total, Decimal("0"))

    def test_check_in_window_and_boarding(self):
        booking = self.book(self.pax[:1])
        ticket = booking.tickets.get()
        with self.assertRaises(services.BookingError):
            services.check_in(ticket)  # 30 days out: window closed
        ticket = services.check_in(ticket, enforce_window=False)
        self.assertEqual(ticket.status, Ticket.Status.CHECKED_IN)
        services.board(ticket)
        with self.assertRaises(services.BookingError):
            services.cancel_tickets([ticket])


@override_settings(LANGUAGE_CODE="en")
class AdminWizardTests(AirlineTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("root", password="pw-for-tests")
        self.client.force_login(self.admin)

    def test_full_booking_wizard(self):
        date = timezone.localtime(self.flight.departure_time).date().isoformat()
        response = self.client.get(reverse("admin:airline_book_search"), {
            "origin": self.bgw.pk, "destination": self.dxb.pk, "date": date, "seat_class": "economy",
            "passengers": 1})
        self.assertContains(response, "DA1")
        self.client.post(reverse("admin:airline_book_add"),
                         {"flight": self.flight.pk, "seat_class": "economy", "passengers": 1})
        seat = services.available_seats(self.flight, "economy").first()
        response = self.client.post(reverse("admin:airline_book_passengers"), {
            "form-TOTAL_FORMS": 1, "form-INITIAL_FORMS": 1, "form-MIN_NUM_FORMS": 0, "form-MAX_NUM_FORMS": 1000,
            "form-0-first_name": "New", "form-0-last_name": "Person", "form-0-passport_no": "n123",
            "form-0-nationality": "Iraqi", "form-0-seat_0": seat.pk, "contact_email": "new@example.com"})
        booking = Booking.objects.get(contact_email="new@example.com")
        self.assertRedirects(response, reverse("admin:airline_booking_pay", args=[booking.pk]))
        self.client.post(reverse("admin:airline_booking_pay", args=[booking.pk]), {"method": "cash"})
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertTrue(Passenger.objects.filter(passport_no="N123").exists())

    def test_pages_render(self):
        booking = self.book(self.pax[:1])
        ticket = services.check_in(booking.tickets.get(), enforce_window=False)
        for url in [reverse("admin:index"), reverse("admin:airline_reports"),
                    reverse("admin:airline_itinerary", args=[booking.pk]),
                    reverse("admin:airline_boarding_pass", args=[ticket.pk]),
                    reverse("admin:airline_flight_changelist"), reverse("admin:airline_ticket_changelist"),
                    reverse("admin:airline_aircraft_change", args=[self.aircraft.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_booking_agent_cannot_see_reports(self):
        from django.core.management import call_command
        from django.contrib.auth.models import Group
        call_command("setup_roles", verbosity=0)
        agent = User.objects.create_user("a", password="pw-for-tests", is_staff=True)
        agent.groups.add(Group.objects.get(name="Booking Agent"))
        self.client.force_login(agent)
        self.assertEqual(self.client.get(reverse("admin:airline_reports")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:airline_book_search")).status_code, 200)
