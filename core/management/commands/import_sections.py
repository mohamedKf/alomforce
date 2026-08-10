"""Attach profile cross-section images from a folder, matched by filename.

Each image file is matched to a profile by its filename stem: ``04901.png``
attaches to profile ``04901``. This is the bulk path for section drawings we
already have as files; the desktop's upload button is the one-off path.

The files land wherever ``settings.STORAGES['default']`` points -- the local
``media/`` directory now, Cloudinary once its credentials are set -- so the same
command seeds either backend without change.

    python manage.py import_sections /path/to/sections
    python manage.py import_sections /path/to/sections --overwrite --dry-run
"""

from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from core.models import Profile

# Raster and vector formats a cross-section drawing is likely to arrive in.
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}


class Command(BaseCommand):
    help = (
        'Attach profile section images from a folder, matched by filename to '
        'profile number (04901.png -> profile 04901).'
    )

    def add_arguments(self, parser):
        parser.add_argument('folder', help='Folder of image files to import.')
        parser.add_argument(
            '--overwrite', action='store_true',
            help='Replace images on profiles that already have one.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would happen without writing anything.',
        )

    def handle(self, *args, **options):
        folder = Path(options['folder']).expanduser()
        if not folder.is_dir():
            raise CommandError(f'Not a folder: {folder}')

        overwrite = options['overwrite']
        dry_run = options['dry_run']

        # Walk subfolders too: the extractor lays images out as
        # <series>/<number>.png, and the profile number in the filename is
        # globally unique, so the series folder is only for humans.
        files = sorted(
            p for p in folder.rglob('*')
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        )
        if not files:
            self.stdout.write(self.style.WARNING(f'No image files in {folder}.'))
            return

        attached = skipped_existing = unmatched = 0
        for path in files:
            number = path.stem
            profile = Profile.objects.filter(number=number).first()
            if profile is None:
                self.stdout.write(
                    self.style.WARNING(f'  no profile for "{path.name}"')
                )
                unmatched += 1
                continue

            if profile.section_image and not overwrite:
                self.stdout.write(
                    f'  {number}: already has an image, skipping '
                    f'(use --overwrite)'
                )
                skipped_existing += 1
                continue

            if dry_run:
                verb = 'would replace' if profile.section_image else 'would attach'
                self.stdout.write(f'  {number}: {verb} {path.name}')
                attached += 1
                continue

            with path.open('rb') as fh:
                # save=True writes the file through the configured storage and
                # persists the field in one step.
                profile.section_image.save(path.name, File(fh), save=True)
            self.stdout.write(self.style.SUCCESS(f'  {number}: attached {path.name}'))
            attached += 1

        head = 'Dry run' if dry_run else 'Done'
        self.stdout.write(self.style.SUCCESS(
            f'{head}: {attached} attached, {skipped_existing} already had one, '
            f'{unmatched} unmatched.'
        ))
