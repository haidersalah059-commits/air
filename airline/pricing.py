"""Dynamic pricing: the fare rises as departure gets closer and as the flight fills up."""
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

CLASS_MULTIPLIER = {"economy": Decimal("1"), "business": Decimal("2.5"), "first": Decimal("4")}
# (days before departure, multiplier) — first matching row wins.
TIME_FACTORS = ((3, Decimal("1.50")), (7, Decimal("1.30")), (14, Decimal("1.15")))
OCCUPANCY_WEIGHT = Decimal("0.5")  # a full flight costs 50% more than an empty one


def time_factor(departure, now=None):
    days = (departure - (now or timezone.now())).total_seconds() / 86400
    for limit, factor in TIME_FACTORS:
        if days < limit:
            return factor
    return Decimal("1")


def occupancy_factor(flight):
    return Decimal("1") + Decimal(str(round(flight.occupancy, 4))) * OCCUPANCY_WEIGHT


def current_price(flight, seat_class, now=None):
    price = (flight.base_price * CLASS_MULTIPLIER[str(seat_class)]
             * time_factor(flight.departure_time, now) * occupancy_factor(flight))
    return price.quantize(Decimal("0.01"), ROUND_HALF_UP)
