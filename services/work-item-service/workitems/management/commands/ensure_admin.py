"""Create the Django admin account platform startup signs an operator in
with, idempotently.

Django's own `createsuperuser --noinput` cannot do this job: run a second
time with the same username it exits non-zero, so re-running startup
against an already-initialized installation would fail on a step that has
nothing left to do. This command creates the account the first time and,
after that, only makes sure the existing account still has admin access.

It deliberately never changes an existing account's password. Re-running
startup must not silently reset the credential an operator is already
using; changing it is a separate, explicit act.

Reads three environment variables, the same three the repository-root
.env declares:

    AIGANG_ADMIN_USER
    AIGANG_ADMIN_EMAIL     — required because this service uses Django's
                             default user model
    AIGANG_ADMIN_PASSWORD

Nothing it prints contains the password.
"""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or confirm the Django admin account from AIGANG_ADMIN_* environment variables."

    def handle(self, *args, **options):
        username = (os.environ.get('AIGANG_ADMIN_USER') or '').strip()
        email = (os.environ.get('AIGANG_ADMIN_EMAIL') or '').strip()
        password = os.environ.get('AIGANG_ADMIN_PASSWORD') or ''

        missing = [
            name for name, value in (
                ('AIGANG_ADMIN_USER', username),
                ('AIGANG_ADMIN_EMAIL', email),
                ('AIGANG_ADMIN_PASSWORD', password),
            ) if not value
        ]
        if missing:
            raise CommandError(
                'missing required environment variable(s): ' + ', '.join(missing)
            )

        User = get_user_model()
        user = User.objects.filter(**{User.USERNAME_FIELD: username}).first()

        if user is None:
            User.objects.create_superuser(username, email, password)
            self.stdout.write(f'created Django admin account "{username}"')
            return

        changed = []
        if not user.is_staff:
            user.is_staff = True
            changed.append('is_staff')
        if not user.is_superuser:
            user.is_superuser = True
            changed.append('is_superuser')
        if not user.is_active:
            user.is_active = True
            changed.append('is_active')
        if changed:
            user.save(update_fields=changed)
            self.stdout.write(
                f'Django admin account "{username}" already existed; restored {", ".join(changed)}'
            )
        else:
            self.stdout.write(f'Django admin account "{username}" already exists — nothing to do')
