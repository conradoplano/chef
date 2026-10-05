from django.core.management.base import BaseCommand, CommandError

from accounts.models import User


class Command(BaseCommand):
    help = "Create (or update) a user who can log in with a code sent to their email."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--name", default="")
        parser.add_argument("--admin", action="store_true", help="Grant access to the admin pages; their household may use AI.")
        parser.add_argument("--join", metavar="EMAIL", help="Join the household of this existing user (default: a new household).")

    def handle(self, *args, email, name, admin, join, **options):
        email = email.strip().lower()
        if "@" not in email:
            raise CommandError(f"'{email}' is not a valid email address.")
        household = None
        if join:
            other = User.objects.filter(email=join.strip().lower()).first()
            if other is None or other.household is None:
                raise CommandError(f"There's no user {join} with a household to join.")
            household = other.household

        user = User.objects.filter(email=email).first()
        created = user is None
        if created:
            user = User.objects.create_user(email=email, household=household)
        elif household:
            user.household = household
        if name:
            user.name = name
        if admin:
            user.is_staff = user.is_superuser = True
            user.household.ai_approved = True
            user.household.save(update_fields=["ai_approved"])
        user.is_active = True
        user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} user {email} in {user.household}"))
