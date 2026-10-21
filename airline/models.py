import secrets
import string
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

SEAT_LETTERS = "ABCDEFGHJK"
PNR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _unique_code(model, field, make):
    while True:
        code = make()
        if not model.objects.filter(**{field: code}).exists():
            return code


class SeatClass(models.TextChoices):
    FIRST = "first", _("First")
    BUSINESS = "business", _("Business")
    ECONOMY = "economy", _("Economy")


class Airport(models.Model):
    code = models.CharField(_("IATA code"), max_length=3, unique=True)
    name = models.CharField(_("name"), max_length=120)
    city = models.CharField(_("city"), max_length=80)
    country = models.CharField(_("country"), max_length=80)

    class Meta:
        ordering = ["code"]
        verbose_name = _("airport")
        verbose_name_plural = _("airports")

    def __str__(self):
        return f"{self.code} – {self.city}"

    def save(self, *args, **kwargs):
        self.code = self.code.upper()
        super().save(*args, **kwargs)


class Aircraft(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        MAINTENANCE = "maintenance", _("In maintenance")
        RETIRED = "retired", _("Retired")

    LAYOUT_FIELDS = (
        "first_rows", "first_seats_per_row",
        "business_rows", "business_seats_per_row",
        "economy_rows", "economy_seats_per_row",
    )

    registration = models.CharField(_("registration"), max_length=10, unique=True)
    model = models.CharField(_("model"), max_length=60)
    first_rows = models.PositiveSmallIntegerField(_("first class rows"), default=0)
    first_seats_per_row = models.PositiveSmallIntegerField(
        _("first class seats per row"), default=4, validators=[MaxValueValidator(len(SEAT_LETTERS))])
    business_rows = models.PositiveSmallIntegerField(_("business rows"), default=0)
    business_seats_per_row = models.PositiveSmallIntegerField(
        _("business seats per row"), default=4, validators=[MaxValueValidator(len(SEAT_LETTERS))])
    economy_rows = models.PositiveSmallIntegerField(_("economy rows"), default=20)
    economy_seats_per_row = models.PositiveSmallIntegerField(
        _("economy seats per row"), default=6, validators=[MaxValueValidator(len(SEAT_LETTERS))])
    status = models.CharField(_("status"), max_length=12, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        ordering = ["registration"]
        verbose_name = _("aircraft")
        verbose_name_plural = _("aircraft")

    def __str__(self):
        return f"{self.registration} ({self.model})"

    def _layout(self):
        return tuple(getattr(self, f) for f in self.LAYOUT_FIELDS)

    def layout_changed(self):
        if not self.pk:
            return True
        stored = Aircraft.objects.filter(pk=self.pk).values_list(*self.LAYOUT_FIELDS).first()
        return stored != self._layout()

    def class_layout(self):
        return [
            (SeatClass.FIRST, self.first_rows, self.first_seats_per_row),
            (SeatClass.BUSINESS, self.business_rows, self.business_seats_per_row),
            (SeatClass.ECONOMY, self.economy_rows, self.economy_seats_per_row),
        ]

    def seats_in_class(self, seat_class):
        for cls, rows, per_row in self.class_layout():
            if cls == seat_class:
                return rows * per_row
        return 0

    @property
    def total_seats(self):
        return sum(rows * per_row for _cls, rows, per_row in self.class_layout())

    def clean(self):
        if self.total_seats == 0:
            raise ValidationError(_("The aircraft must have at least one seat."))
        if self.pk and self.layout_changed() and Ticket.objects.filter(seat__aircraft=self).exists():
            raise ValidationError(_("The seat layout cannot be changed after tickets were issued on this aircraft."))

    def save(self, *args, **kwargs):
        regenerate = self.layout_changed()
        super().save(*args, **kwargs)
        if regenerate:
            self.generate_seats()

    def generate_seats(self):
        self.seats.all().delete()
        seats = []
        row = 1
        for seat_class, rows, per_row in self.class_layout():
            for _row in range(rows):
                for letter in SEAT_LETTERS[:per_row]:
                    seats.append(Seat(aircraft=self, row=row, seat_number=f"{row}{letter}", seat_class=seat_class))
                row += 1
        Seat.objects.bulk_create(seats)


class Seat(models.Model):
    aircraft = models.ForeignKey(Aircraft, on_delete=models.CASCADE, related_name="seats", verbose_name=_("aircraft"))
    row = models.PositiveSmallIntegerField(_("row"))
    seat_number = models.CharField(_("seat number"), max_length=4)
    seat_class = models.CharField(_("class"), max_length=10, choices=SeatClass.choices)

    class Meta:
        ordering = ["aircraft", "row", "seat_number"]
        verbose_name = _("seat")
        verbose_name_plural = _("seats")
        constraints = [models.UniqueConstraint(fields=["aircraft", "seat_number"], name="unique_seat_per_aircraft")]

    def __str__(self):
        return f"{self.seat_number} ({self.get_seat_class_display()})"


class Maintenance(models.Model):
    aircraft = models.ForeignKey(Aircraft, on_delete=models.CASCADE, related_name="maintenance",
                                 verbose_name=_("aircraft"))
    start = models.DateTimeField(_("start"))
    end = models.DateTimeField(_("end"))
    description = models.CharField(_("description"), max_length=200)
    completed = models.BooleanField(_("completed"), default=False)

    class Meta:
        ordering = ["-start"]
        verbose_name = _("maintenance window")
        verbose_name_plural = _("maintenance schedule")

    def __str__(self):
        return f"{self.aircraft.registration}: {self.description}"

    def clean(self):
        if self.start and self.end and self.end <= self.start:
            raise ValidationError({"end": _("The end must be after the start.")})


class Crew(models.Model):
    class Role(models.TextChoices):
        CAPTAIN = "captain", _("Captain")
        FIRST_OFFICER = "first_officer", _("First officer")
        PURSER = "purser", _("Purser")
        ATTENDANT = "attendant", _("Flight attendant")

    PILOT_ROLES = (Role.CAPTAIN, Role.FIRST_OFFICER)

    name = models.CharField(_("name"), max_length=120)
    role = models.CharField(_("role"), max_length=15, choices=Role.choices)
    license_no = models.CharField(_("license number"), max_length=30, blank=True)
    phone = models.CharField(_("phone"), max_length=30, blank=True)
    max_monthly_hours = models.PositiveSmallIntegerField(_("max flight hours per month"), default=90)
    active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["role", "name"]
        verbose_name = _("crew member")
        verbose_name_plural = _("crew")

    def __str__(self):
        return f"{self.name} ({self.get_role_display()})"

    def clean(self):
        if self.role in self.PILOT_ROLES and not self.license_no:
            raise ValidationError({"license_no": _("Pilots must have a license number.")})

    def flight_hours(self, year, month, exclude_flight_id=None):
        flights = self.flights.filter(departure_time__year=year, departure_time__month=month).exclude(
            status=Flight.Status.CANCELLED)
        if exclude_flight_id:
            flights = flights.exclude(pk=exclude_flight_id)
        return sum(f.duration_hours for f in flights)


class Flight(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", _("On time")
        DELAYED = "delayed", _("Delayed")
        BOARDING = "boarding", _("Boarding")
        DEPARTED = "departed", _("Departed")
        ARRIVED = "arrived", _("Arrived")
        CANCELLED = "cancelled", _("Cancelled")

    BOOKABLE = (Status.SCHEDULED, Status.DELAYED)

    flight_no = models.CharField(_("flight number"), max_length=10)
    aircraft = models.ForeignKey(Aircraft, on_delete=models.PROTECT, related_name="flights", verbose_name=_("aircraft"))
    origin = models.ForeignKey(Airport, on_delete=models.PROTECT, related_name="departures", verbose_name=_("from"))
    destination = models.ForeignKey(Airport, on_delete=models.PROTECT, related_name="arrivals", verbose_name=_("to"))
    departure_time = models.DateTimeField(_("departure"))
    arrival_time = models.DateTimeField(_("arrival"))
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    gate = models.CharField(_("gate"), max_length=6, blank=True)
    base_price = models.DecimalField(_("base price"), max_digits=10, decimal_places=2,
                                     validators=[MinValueValidator(Decimal("1"))])
    crew = models.ManyToManyField(Crew, through="FlightCrew", related_name="flights", blank=True,
                                  verbose_name=_("crew"))

    class Meta:
        ordering = ["departure_time"]
        verbose_name = _("flight")
        verbose_name_plural = _("flights")
        constraints = [models.UniqueConstraint(fields=["flight_no", "departure_time"], name="unique_flight_departure")]
        indexes = [models.Index(fields=["origin", "destination", "departure_time"])]
        permissions = [("manage_operations", _("Can manage flight operations"))]

    def __str__(self):
        when = timezone.localtime(self.departure_time).strftime("%Y-%m-%d %H:%M") if self.departure_time else ""
        return f"{self.flight_no} {self.origin.code}→{self.destination.code} {when}"

    @property
    def duration_hours(self):
        return (self.arrival_time - self.departure_time).total_seconds() / 3600

    @property
    def is_bookable(self):
        closes = timezone.now() + timedelta(minutes=settings.CHECKIN_CLOSES_MINUTES)
        return self.status in self.BOOKABLE and self.departure_time > closes

    @property
    def sold_seats(self):
        # Admin list views annotate "sold_count" to avoid one query per row.
        if "sold_count" in self.__dict__:
            return self.sold_count
        return self.tickets.exclude(status=Ticket.Status.CANCELLED).count()

    @property
    def occupancy(self):
        total = self.aircraft.total_seats
        return self.sold_seats / total if total else 0

    def clean(self):
        errors = {}
        if self.origin_id and self.origin_id == self.destination_id:
            errors["destination"] = _("Origin and destination must be different.")
        if self.departure_time and self.arrival_time and self.arrival_time <= self.departure_time:
            errors["arrival_time"] = _("Arrival must be after departure.")
        if errors:
            raise ValidationError(errors)
        if not (self.aircraft_id and self.departure_time and self.arrival_time):
            return
        if self.pk:
            old_aircraft = Flight.objects.filter(pk=self.pk).values_list("aircraft_id", flat=True).first()
            if old_aircraft != self.aircraft_id and self.tickets.exclude(status=Ticket.Status.CANCELLED).exists():
                raise ValidationError({"aircraft": _("The aircraft cannot be changed after seats were sold.")})
        if self.status == self.Status.CANCELLED:
            return
        if self.aircraft.status == Aircraft.Status.RETIRED:
            raise ValidationError({"aircraft": _("This aircraft is retired.")})
        if self.aircraft.maintenance.filter(completed=False, start__lt=self.arrival_time,
                                            end__gt=self.departure_time).exists():
            raise ValidationError({"aircraft": _("The aircraft is scheduled for maintenance at this time.")})
        clash = Flight.objects.filter(aircraft_id=self.aircraft_id, departure_time__lt=self.arrival_time,
                                      arrival_time__gt=self.departure_time).exclude(
            status=self.Status.CANCELLED).exclude(pk=self.pk).first()
        if clash:
            raise ValidationError({"aircraft": _("The aircraft is already assigned to flight %(flight)s at this time.")
                                   % {"flight": clash.flight_no}})


class FlightCrew(models.Model):
    flight = models.ForeignKey(Flight, on_delete=models.CASCADE, verbose_name=_("flight"))
    crew = models.ForeignKey(Crew, on_delete=models.PROTECT, verbose_name=_("crew member"))

    class Meta:
        verbose_name = _("crew assignment")
        verbose_name_plural = _("crew assignments")
        constraints = [models.UniqueConstraint(fields=["flight", "crew"], name="unique_crew_per_flight")]

    def __str__(self):
        return str(self.crew)

    def clean(self):
        try:
            flight, crew = self.flight, self.crew
        except ObjectDoesNotExist:
            return
        if not crew.active:
            raise ValidationError(_("%(name)s is not active.") % {"name": crew.name})
        if not (flight.departure_time and flight.arrival_time):
            return
        clash = FlightCrew.objects.filter(
            crew=crew, flight__departure_time__lt=flight.arrival_time, flight__arrival_time__gt=flight.departure_time,
        ).exclude(flight__status=Flight.Status.CANCELLED)
        if flight.pk:
            clash = clash.exclude(flight_id=flight.pk)
        clash = clash.select_related("flight").first()
        if clash:
            raise ValidationError(_("%(name)s is already on flight %(flight)s at this time.")
                                  % {"name": crew.name, "flight": clash.flight.flight_no})
        local = timezone.localtime(flight.departure_time)
        hours = crew.flight_hours(local.year, local.month, exclude_flight_id=flight.pk) + flight.duration_hours
        if hours > crew.max_monthly_hours:
            raise ValidationError(
                _("%(name)s would fly %(hours).1f hours this month (limit %(limit)s).")
                % {"name": crew.name, "hours": hours, "limit": crew.max_monthly_hours})


class Passenger(models.Model):
    TIERS = ((100000, _("Platinum")), (50000, _("Gold")), (25000, _("Silver")), (0, _("Blue")))

    first_name = models.CharField(_("first name"), max_length=60)
    last_name = models.CharField(_("last name"), max_length=60)
    passport_no = models.CharField(_("passport number"), max_length=20, unique=True)
    nationality = models.CharField(_("nationality"), max_length=60)
    email = models.EmailField(_("email"), blank=True)
    phone = models.CharField(_("phone"), max_length=30, blank=True)
    birth_date = models.DateField(_("date of birth"), null=True, blank=True)
    miles = models.PositiveIntegerField(_("loyalty miles"), default=0)

    class Meta:
        ordering = ["last_name", "first_name"]
        verbose_name = _("passenger")
        verbose_name_plural = _("passengers")

    def __str__(self):
        return f"{self.full_name} ({self.passport_no})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def tier(self):
        for threshold, label in self.TIERS:
            if self.miles >= threshold:
                return label


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Awaiting payment")
        CONFIRMED = "confirmed", _("Confirmed")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")

    pnr = models.CharField(_("PNR"), max_length=6, unique=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="bookings", verbose_name=_("created by"))
    contact_email = models.EmailField(_("contact email"))
    booking_date = models.DateTimeField(_("booking date"), auto_now_add=True)
    total_amount = models.DecimalField(_("total amount"), max_digits=12, decimal_places=2, default=0)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.PENDING)
    hold_expires_at = models.DateTimeField(_("seats held until"), null=True, blank=True)

    class Meta:
        ordering = ["-booking_date"]
        verbose_name = _("booking")
        verbose_name_plural = _("bookings")
        permissions = [("view_reports", _("Can view reports"))]

    def __str__(self):
        return self.pnr

    def save(self, *args, **kwargs):
        if not self.pnr:
            self.pnr = _unique_code(Booking, "pnr", lambda: "".join(secrets.choice(PNR_ALPHABET) for _i in range(6)))
        super().save(*args, **kwargs)

    @property
    def paid_total(self):
        sums = dict(self.payments.values("status").annotate(total=Sum("amount")).values_list("status", "total"))
        return (sums.get(Payment.Status.PAID) or Decimal("0")) - (sums.get(Payment.Status.REFUND) or Decimal("0"))


class Ticket(models.Model):
    class Status(models.TextChoices):
        HELD = "held", _("Seat held")
        ISSUED = "issued", _("Issued")
        CHECKED_IN = "checked_in", _("Checked in")
        BOARDED = "boarded", _("Boarded")
        CANCELLED = "cancelled", _("Cancelled")

    ticket_no = models.CharField(_("ticket number"), max_length=13, unique=True, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="tickets", verbose_name=_("booking"))
    passenger = models.ForeignKey(Passenger, on_delete=models.PROTECT, related_name="tickets",
                                  verbose_name=_("passenger"))
    flight = models.ForeignKey(Flight, on_delete=models.PROTECT, related_name="tickets", verbose_name=_("flight"))
    seat = models.ForeignKey(Seat, on_delete=models.PROTECT, related_name="tickets", verbose_name=_("seat"))
    seat_class = models.CharField(_("class"), max_length=10, choices=SeatClass.choices)
    price = models.DecimalField(_("price"), max_digits=10, decimal_places=2)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.HELD)
    checked_in_at = models.DateTimeField(_("checked in at"), null=True, blank=True)

    class Meta:
        ordering = ["flight__departure_time", "seat__row", "seat__seat_number"]
        verbose_name = _("ticket")
        verbose_name_plural = _("tickets")
        permissions = [("check_in_ticket", _("Can check in and board passengers"))]
        constraints = [
            # The database itself refuses to sell the same seat twice (double-booking guard).
            models.UniqueConstraint(fields=["flight", "seat"], condition=~Q(status="cancelled"),
                                    name="unique_active_seat_per_flight"),
            models.UniqueConstraint(fields=["flight", "passenger"], condition=~Q(status="cancelled"),
                                    name="one_active_ticket_per_passenger_per_flight"),
        ]

    def __str__(self):
        return self.ticket_no

    def save(self, *args, **kwargs):
        if not self.ticket_no:
            prefix = settings.AIRLINE_TICKET_PREFIX
            self.ticket_no = _unique_code(Ticket, "ticket_no",
                                          lambda: prefix + "".join(secrets.choice(string.digits) for _i in range(10)))
        super().save(*args, **kwargs)

    def clean(self):
        if self.seat_id and self.flight_id:
            if self.seat.aircraft_id != self.flight.aircraft_id:
                raise ValidationError({"seat": _("This seat is not on the flight's aircraft.")})
            if self.seat.seat_class != self.seat_class:
                raise ValidationError({"seat": _("This seat is not in the ticket's class.")})

    @property
    def baggage_allowance(self):
        return settings.BAGGAGE_ALLOWANCE_KG.get(self.seat_class, 0)

    @property
    def baggage_weight(self):
        return self.baggage.aggregate(total=Sum("weight"))["total"] or Decimal("0")


class Payment(models.Model):
    class Method(models.TextChoices):
        CARD = "card", _("Credit card")
        WALLET = "wallet", _("E-wallet")
        CASH = "cash", _("Cash")
        TRANSFER = "transfer", _("Bank transfer")

    class Status(models.TextChoices):
        PAID = "paid", _("Paid")
        REFUND = "refund", _("Refund")
        FAILED = "failed", _("Failed")

    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="payments", verbose_name=_("booking"))
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    method = models.CharField(_("method"), max_length=10, choices=Method.choices)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices)
    reference = models.CharField(_("reference"), max_length=40, blank=True)
    paid_at = models.DateTimeField(_("date"), default=timezone.now)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    verbose_name=_("recorded by"))

    class Meta:
        ordering = ["-paid_at"]
        verbose_name = _("payment")
        verbose_name_plural = _("payments")

    def __str__(self):
        return f"{self.booking.pnr} {self.get_status_display()} {self.amount}"


class Baggage(models.Model):
    class Status(models.TextChoices):
        CHECKED = "checked", _("Checked in")
        LOADED = "loaded", _("Loaded")
        ARRIVED = "arrived", _("Arrived")
        DELIVERED = "delivered", _("Delivered")
        LOST = "lost", _("Lost")

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="baggage", verbose_name=_("ticket"))
    weight = models.DecimalField(_("weight (kg)"), max_digits=5, decimal_places=1,
                                 validators=[MinValueValidator(Decimal("0.1")), MaxValueValidator(Decimal("45"))])
    tag_no = models.CharField(_("tag number"), max_length=10, unique=True, editable=False)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.CHECKED)
    updated_at = models.DateTimeField(_("last update"), auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = _("bag")
        verbose_name_plural = _("baggage")

    def __str__(self):
        return self.tag_no

    def save(self, *args, **kwargs):
        if not self.tag_no:
            code = settings.AIRLINE_CODE
            self.tag_no = _unique_code(Baggage, "tag_no",
                                       lambda: code + "".join(secrets.choice(string.digits) for _i in range(6)))
        super().save(*args, **kwargs)

    def clean(self):
        try:
            ticket = self.ticket
        except ObjectDoesNotExist:
            return
        if not self.pk and ticket.status not in (Ticket.Status.CHECKED_IN, Ticket.Status.BOARDED):
            raise ValidationError(_("Bags can only be added after the passenger has checked in."))


class Notification(models.Model):
    class Channel(models.TextChoices):
        EMAIL = "email", _("Email")
        SMS = "sms", _("SMS")

    channel = models.CharField(_("channel"), max_length=5, choices=Channel.choices)
    recipient = models.CharField(_("recipient"), max_length=120)
    subject = models.CharField(_("subject"), max_length=200)
    body = models.TextField(_("message"))
    booking = models.ForeignKey(Booking, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("booking"))
    flight = models.ForeignKey(Flight, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("flight"))
    sent = models.BooleanField(_("sent"), default=False)
    error = models.CharField(_("error"), max_length=500, blank=True)
    created_at = models.DateTimeField(_("date"), auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("notification")
        verbose_name_plural = _("notifications")

    def __str__(self):
        return f"{self.get_channel_display()} → {self.recipient}"
