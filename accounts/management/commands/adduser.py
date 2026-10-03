from django.core.management.base import BaseCommand, CommandError

from accounts.models import User


class Command(BaseCommand):
    help = "Create (or update) a user who can log in with a code sent to their email."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--name", default="")
        parser.add_argument("--admin", action="store_true", help="Grant access to the Django admin.")

    def handle(self, *args, email, name, admin, **options):
        email = email.strip().lower()
        if "@" not in email:
            raise CommandError(f"'{email}' is not a valid email address.")

        user = User.objects.filter(email=email).first()
        created = user is None
        if created:
            user = User.objects.create_user(email=email)
        if name:
            user.name = name
        if admin:
            user.is_staff = user.is_superuser = True
        user.is_active = True
        user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} user {email}"))
