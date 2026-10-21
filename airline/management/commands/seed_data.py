from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from airline import services
from airline.models import Aircraft, Airport, Crew, Flight, FlightCrew, Passenger, SeatClass

AIRPORTS = [
    ("BGW", "Baghdad International Airport", "Baghdad", "Iraq"),
    ("BSR", "Basra International Airport", "Basra", "Iraq"),
    ("EBL", "Erbil International Airport", "Erbil", "Iraq"),
    ("NJF", "Al Najaf International Airport", "Najaf", "Iraq"),
    ("DXB", "Dubai International Airport", "Dubai", "United Arab Emirates"),
    ("IST", "Istanbul Airport", "Istanbul", "Turkey"),
    ("CAI", "Cairo International Airport", "Cairo", "Egypt"),
    ("AMM", "Queen Alia International Airport", "Amman", "Jordan"),
    ("BEY", "Beirut–Rafic Hariri International Airport", "Beirut", "Lebanon"),
    ("DOH", "Hamad International Airport", "Doha", "Qatar"),
]

# registration, model, layout (first rows/per row, business rows/per row, economy rows/per row)
AIRCRAFT = [
    ("YI-DAA", "Airbus A320", (0, 4, 3, 4, 25, 6)),
    ("YI-DAB", "Boeing 737-800", (0, 4, 4, 4, 22, 6)),
    ("YI-DAC", "Boeing 787-9", (2, 4, 6, 6, 30, 9)),
    ("YI-DAD", "Embraer E190", (0, 4, 0, 4, 25, 4)),
]

# aircraft index -> daily rotation: (flight no, from, to, departure HH:MM, minutes, base price)
ROTATIONS = {
    0: [("101", "BGW", "DXB", "08:00", 150, 180), ("102", "DXB", "BGW", "12:00", 165, 180),
        ("111", "BGW", "DOH", "16:00", 120, 170), ("112", "DOH", "BGW", "19:30", 125, 170)],
    1: [("201", "BGW", "IST", "07:00", 210, 220), ("202", "IST", "BGW", "12:30", 210, 220)],
    2: [("301", "BGW", "CAI", "09:00", 180, 200), ("302", "CAI", "BGW", "14:00", 180, 200),
        ("311", "BGW", "AMM", "18:00", 105, 140), ("312", "AMM", "BGW", "21:00", 105, 140)],
    3: [("401", "BGW", "BSR", "07:30", 60, 60), ("402", "BSR", "BGW", "09:30", 60, 60),
        ("411", "BGW", "EBL", "12:00", 60, 55), ("412", "EBL", "BGW", "14:00", 60, 55),
        ("421", "BGW", "NJF", "17:00", 50, 45), ("422", "NJF", "BGW", "19:00", 50, 45)],
}

CREW = [
    ("Ali Hassan", Crew.Role.CAPTAIN, "ATPL-IQ-1001"), ("Omar Kareem", Crew.Role.FIRST_OFFICER, "CPL-IQ-2001"),
    ("Zainab Salim", Crew.Role.PURSER, ""), ("Noor Adel", Crew.Role.ATTENDANT, ""),
    ("Hussein Jabbar", Crew.Role.CAPTAIN, "ATPL-IQ-1002"), ("Mustafa Qasim", Crew.Role.FIRST_OFFICER, "CPL-IQ-2002"),
    ("Sara Mahdi", Crew.Role.PURSER, ""), ("Huda Faleh", Crew.Role.ATTENDANT, ""),
    ("Karim Abbas", Crew.Role.CAPTAIN, "ATPL-IQ-1003"), ("Yasir Natiq", Crew.Role.FIRST_OFFICER, "CPL-IQ-2003"),
    ("Maryam Saad", Crew.Role.PURSER, ""), ("Rusul Hamid", Crew.Role.ATTENDANT, ""),
    ("Ahmed Latif", Crew.Role.CAPTAIN, "ATPL-IQ-1004"), ("Bilal Hadi", Crew.Role.FIRST_OFFICER, "CPL-IQ-2004"),
    ("Aya Majid", Crew.Role.PURSER, ""), ("Fatima Riyadh", Crew.Role.ATTENDANT, ""),
]

# Demo staff accounts — test credentials for local use only; change them before any real deployment.
USERS = [
    ("admin", "admin123", "System Admin", True),
    ("agent", "agent123", "Booking Agent", False),
    ("airport", "airport123", "Airport Staff", False),
    ("ops", "ops123", "Operations Manager", False),
]

PASSENGERS = [
    ("Mohammed", "Jawad", "A1234567", "Iraqi", "mohammed@example.com", "+9647700000001"),
    ("Layla", "Mohammed", "A2345678", "Iraqi", "layla@example.com", "+9647700000002"),
    ("Ahmed", "Ali", "B3456789", "Iraqi", "ahmed@example.com", "+9647700000003"),
    ("Sara", "Hussein", "C4567890", "Jordanian", "sara@example.com", "+962790000004"),
    ("Yousef", "Kamal", "D5678901", "Egyptian", "yousef@example.com", "+201000000005"),
]


class Command(BaseCommand):
    help = "Load demo airports, aircraft, flights, crew, staff accounts and bookings."

    def handle(self, *args, **options):
        call_command("setup_roles", verbosity=0)
        self._users()
        if Flight.objects.exists():
            self.stdout.write("Flights already exist — skipping demo data.")
            return
        with transaction.atomic():
            airports = {code: Airport.objects.create(code=code, name=name, city=city, country=country)
                        for code, name, city, country in AIRPORTS}
            fleet = []
            for registration, model, layout in AIRCRAFT:
                fields = dict(zip(Aircraft.LAYOUT_FIELDS, layout))
                fleet.append(Aircraft.objects.create(registration=registration, model=model, **fields))
            crew = [Crew.objects.create(name=n, role=r, license_no=lic) for n, r, lic in CREW]
            flights = self._flights(airports, fleet)
            self._crew(flights, fleet, crew)
        self._bookings()
        self.stdout.write(self.style.SUCCESS(
            f"Created {Airport.objects.count()} airports, {len(fleet)} aircraft, {Flight.objects.count()} flights, "
            f"{len(crew)} crew members and demo bookings."))

    def _users(self):
        for username, password, group, superuser in USERS:
            if User.objects.filter(username=username).exists():
                continue
            user = User.objects.create_user(username, password=password, is_staff=True, is_superuser=superuser)
            user.groups.add(Group.objects.get(name=group))
            self.stdout.write(f"User {username} ({group})")

    def _flights(self, airports, fleet):
        now = timezone.now()
        today = timezone.localdate()
        flights = []
        for day_offset in range(-3, 22):
            day = today + timedelta(days=day_offset)
            for index, rotation in ROTATIONS.items():
                for number, origin, destination, hhmm, minutes, price in rotation:
                    hour, minute = map(int, hhmm.split(":"))
                    departure = timezone.make_aware(datetime.combine(day, time(hour, minute)))
                    arrival = departure + timedelta(minutes=minutes)
                    if arrival < now:
                        status = Flight.Status.ARRIVED
                    elif departure < now:
                        status = Flight.Status.DEPARTED
                    else:
                        status = Flight.Status.SCHEDULED
                    flights.append(Flight(
                        flight_no=f"DA{number}", aircraft=fleet[index], origin=airports[origin],
                        destination=airports[destination], departure_time=departure, arrival_time=arrival,
                        status=status, base_price=Decimal(price),
                        gate=f"{'AB'[index % 2]}{index + 1}" if day_offset in (0, 1) else ""))
        Flight.objects.bulk_create(flights)
        upcoming = Flight.objects.filter(departure_time__gt=now).order_by("departure_time")
        delayed = upcoming[2:3].first()
        if delayed:
            delayed.status = Flight.Status.DELAYED
            delayed.save(update_fields=["status"])
        return list(Flight.objects.all())

    def _crew(self, flights, fleet, crew):
        # Assign each aircraft its own crew team for the next 3 days.
        now = timezone.now()
        assignments = []
        for flight in flights:
            if not now - timedelta(days=1) < flight.departure_time < now + timedelta(days=3):
                continue
            team = crew[fleet.index(flight.aircraft) * 4:fleet.index(flight.aircraft) * 4 + 4]
            assignments += [FlightCrew(flight=flight, crew=member) for member in team]
        FlightCrew.objects.bulk_create(assignments)

    def _bookings(self):
        passengers = [Passenger.objects.create(first_name=f, last_name=l, passport_no=p, nationality=n, email=e,
                                               phone=ph, birth_date=datetime(1990, 1, 1).date())
                      for f, l, p, n, e, ph in PASSENGERS]
        upcoming = list(Flight.objects.filter(status__in=Flight.BOOKABLE, departure_time__gt=timezone.now()
                                              + timedelta(hours=1)).order_by("departure_time")[:12])
        plans = [
            ([upcoming[0]], passengers[:2], SeatClass.ECONOMY, True),
            ([upcoming[1]], passengers[2:3], SeatClass.BUSINESS, True),
            ([upcoming[4], upcoming[8]], passengers[3:5], SeatClass.ECONOMY, False),
        ]
        agent = User.objects.filter(username="agent").first()
        for flights, people, seat_class, check_in in plans:
            legs = [(f, seat_class if f.aircraft.seats_in_class(seat_class) else SeatClass.ECONOMY) for f in flights]
            seat_ids = [list(services.available_seats(f, cls).values_list("pk", flat=True)[:len(people)])
                        for f, cls in legs]
            booking = services.create_held_booking(user=agent, contact_email=people[0].email, legs=legs,
                                                   passengers=people, seat_ids=seat_ids)
            services.pay_booking(booking, method="card", user=agent)
            if check_in:
                for ticket in booking.tickets.all():
                    services.check_in(ticket, enforce_window=False)
        # One unpaid booking that is still holding its seats.
        flight = upcoming[6]
        seat = services.available_seats(flight, SeatClass.ECONOMY).first()
        services.create_held_booking(user=agent, contact_email=passengers[0].email,
                                     legs=[(flight, SeatClass.ECONOMY)], passengers=[passengers[0]],
                                     seat_ids=[[seat.pk]])
