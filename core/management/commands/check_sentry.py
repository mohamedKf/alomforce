"""Prove that error reporting actually reaches Sentry.

Turning reporting on is two variables and a redeploy, and the failure mode is
silence -- exactly the same silence as everything working. So this sends one
deliberate event and tells you whether it went.

    manage.py check_sentry              # send a test event
    manage.py check_sentry --status     # just say what is configured

The apps read their own DSN from /api/config/, which serves
SENTRY_DSN_CLIENTS, so that one is reported here too even though this process
never uses it.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from decouple import config


class Command(BaseCommand):
    help = 'Send a test event to Sentry, or report what is configured.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--status', action='store_true',
            help='Report configuration without sending anything.',
        )

    def handle(self, *args, **options):
        server = config('SENTRY_DSN', default='')
        clients = config('SENTRY_DSN_CLIENTS', default='')

        def state(value):
            if not value:
                return self.style.WARNING('not set')
            # Never print the key itself; the project id is enough to tell two
            # projects apart in a log somebody might paste into a chat.
            tail = value.rstrip('/').rsplit('/', 1)[-1]
            return self.style.SUCCESS(f'set (project {tail})')

        self.stdout.write(f'  server errors  SENTRY_DSN          {state(server)}')
        self.stdout.write(f'  app crashes    SENTRY_DSN_CLIENTS  {state(clients)}')
        self.stdout.write(
            f'  environment    {config("RAILWAY_ENVIRONMENT_NAME", default="local")}')

        if options['status']:
            return

        if not server:
            self.stdout.write(self.style.ERROR(
                '\nSENTRY_DSN is not set, so nothing would be sent. Add it to the '
                'server environment and redeploy, then run this again.'))
            return

        try:
            import sentry_sdk
        except ImportError:
            self.stdout.write(self.style.ERROR('sentry-sdk is not installed.'))
            return

        # sentry-sdk 2.x: Hub is deprecated, get_client() is the way to ask
        # whether init() actually ran.
        client = sentry_sdk.get_client()
        if client is None or not client.is_active():
            self.stdout.write(self.style.ERROR(
                '\nThe DSN is set but Sentry did not start. It is read once at '
                'startup, so a variable added since then needs a redeploy.'))
            return

        event_id = sentry_sdk.capture_message(
            'AlomForce test event -- error reporting is working.', level='info')
        sentry_sdk.flush(timeout=10)

        if event_id:
            self.stdout.write(self.style.SUCCESS(
                f'\nSent event {event_id}. It should appear in Sentry within a '
                'few seconds; if it does not, the DSN belongs to another project.'))
        else:
            self.stdout.write(self.style.ERROR(
                '\nSentry accepted no event. Check the DSN is the whole URL.'))
