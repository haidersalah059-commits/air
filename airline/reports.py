from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import Booking, Flight, Payment, Ticket

ACTIVE_TICKETS = ~Q(tickets__status=Ticket.Status.CANCELLED)


def net_revenue(since=None):
    payments = Payment.objects.all()
    if since:
        payments = payments.filter(paid_at__gte=since)
    sums = dict(payments.values("status").annotate(total=Sum("amount")).values_list("status", "total"))
    return (sums.get(Payment.Status.PAID) or Decimal("0")) - (sums.get(Payment.Status.REFUND) or Decimal("0"))


def monthly_revenue(months=6):
    since = (timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
             - timedelta(days=31 * (months - 1))).replace(day=1)
    rows = (Payment.objects.filter(paid_at__gte=since, status__in=[Payment.Status.PAID, Payment.Status.REFUND])
            .annotate(month=TruncMonth("paid_at")).values("month", "status").annotate(total=Sum("amount")))
    data = {}
    for row in rows:
        key = row["month"].strftime("%Y-%m")
        sign = 1 if row["status"] == Payment.Status.PAID else -1
        data[key] = data.get(key, Decimal("0")) + sign * row["total"]
    result = [{"month": k, "total": v} for k, v in sorted(data.items())]
    peak = max((r["total"] for r in result), default=0) or 1
    for r in result:
        r["pct"] = max(0, round(float(r["total"]) / float(peak) * 100))
    return result


def flight_occupancy(limit=25):
    flights = (Flight.objects.exclude(status=Flight.Status.CANCELLED)
               .filter(departure_time__gte=timezone.now() - timedelta(days=7))
               .select_related("origin", "destination", "aircraft")
               .annotate(sold_count=Count("tickets", filter=ACTIVE_TICKETS))
               .order_by("departure_time")[:limit])
    return [{"flight": f, "sold": f.sold_count, "total": f.aircraft.total_seats,
             "pct": round(f.occupancy * 100)} for f in flights]


def average_occupancy(flights_rows):
    total = sum(r["total"] for r in flights_rows)
    return round(sum(r["sold"] for r in flights_rows) / total * 100, 1) if total else 0


def top_destinations(limit=5):
    return list(Ticket.objects.exclude(status=Ticket.Status.CANCELLED)
                .values("flight__destination__code", "flight__destination__city")
                .annotate(count=Count("id")).order_by("-count")[:limit])


def cancellation_rate():
    counts = dict(Booking.objects.exclude(status=Booking.Status.PENDING).values("status")
                  .annotate(n=Count("id")).values_list("status", "n"))
    total = sum(counts.values())
    cancelled = counts.get(Booking.Status.CANCELLED, 0)
    return {"total": total, "cancelled": cancelled, "expired": counts.get(Booking.Status.EXPIRED, 0),
            "pct": round(cancelled / total * 100, 1) if total else 0}


def dashboard_stats():
    now = timezone.localtime()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = Flight.objects.filter(departure_time__gte=day_start, departure_time__lt=day_start + timedelta(days=1))
    upcoming = flight_occupancy(limit=50)
    return {
        "flights_today": today.count(),
        "delayed_today": today.filter(status=Flight.Status.DELAYED).count(),
        "cancelled_today": today.filter(status=Flight.Status.CANCELLED).count(),
        "next_departures": (Flight.objects.filter(departure_time__gte=timezone.now())
                            .exclude(status=Flight.Status.CANCELLED)
                            .select_related("origin", "destination")[:6]),
        "bookings_today": Booking.objects.filter(booking_date__gte=day_start,
                                                 status=Booking.Status.CONFIRMED).count(),
        "pending_holds": Booking.objects.filter(status=Booking.Status.PENDING).count(),
        "revenue_month": net_revenue(since=day_start.replace(day=1)),
        "avg_occupancy": average_occupancy(upcoming),
    }
