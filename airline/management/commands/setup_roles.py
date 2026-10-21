from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

CRUD = ["view", "add", "change", "delete"]

# model -> list of actions (view/add/change/delete) or custom permission codenames
ROLES = {
    "Booking Agent": {
        "airport": ["view"], "aircraft": ["view"], "seat": ["view"], "flight": ["view"],
        "passenger": ["view", "add", "change"], "booking": ["view", "add", "change"],
        "ticket": ["view", "change"], "payment": ["view"], "notification": ["view"],
    },
    "Airport Staff": {
        "airport": ["view"], "seat": ["view"], "flight": ["view", "change"], "passenger": ["view"],
        "booking": ["view"], "ticket": ["view", "change", "check_in_ticket"],
        "baggage": ["view", "add", "change"], "notification": ["view"],
    },
    "Operations Manager": {
        "airport": CRUD, "aircraft": CRUD, "maintenance": CRUD, "seat": ["view"], "crew": CRUD,
        "flightcrew": CRUD, "flight": CRUD + ["manage_operations"], "passenger": ["view"],
        "booking": ["view", "view_reports"], "ticket": ["view"], "payment": ["view"], "baggage": ["view"],
        "notification": ["view"],
    },
}


class Command(BaseCommand):
    help = "Create the staff role groups with their permissions."

    def handle(self, *args, **options):
        for role, models in ROLES.items():
            group, _created = Group.objects.get_or_create(name=role)
            codenames = [f"{action}_{model}" if action in CRUD else action
                         for model, actions in models.items() for action in actions]
            perms = list(Permission.objects.filter(content_type__app_label="airline", codename__in=codenames))
            missing = set(codenames) - {p.codename for p in perms}
            if missing:
                self.stderr.write(f"{role}: unknown permissions {sorted(missing)}")
            group.permissions.set(perms)
            self.stdout.write(f"{role}: {len(perms)} permissions")

        # System administrators manage everything, including users and groups.
        admin_group, _created = Group.objects.get_or_create(name="System Admin")
        admin_group.permissions.set(Permission.objects.filter(content_type__app_label__in=["airline", "auth"]))
        self.stdout.write(self.style.SUCCESS("Roles ready."))
