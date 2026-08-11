"""AlomForce — all models.

Single models module shared by every client: the desktop office app, the
worker app (warehouse + drivers) and the client app. Nothing here is
client-specific; the API layer decides what each role is allowed to see.

Sections:
    1. Users and authentication
    2. Catalog     (families, series, profiles)
    3. Stock       (warehouses, locations, movements)
    4. Sales       (clients, orders, documents, deliveries)
"""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# ---------------------------------------------------------------------------
# 1. Users and authentication
# ---------------------------------------------------------------------------

# How recently a user must have made a request to count as "online".
ONLINE_WINDOW = timedelta(minutes=5)

# last_seen is only rewritten when it is older than this, so presence tracking
# costs at most one small UPDATE per user per minute rather than one per request.
LAST_SEEN_THROTTLE = timedelta(minutes=1)


def validate_israeli_id(value):
    """Validate an Israeli ID number (תעודת זהות) including its check digit.

    Nine digits, right-padded with leading zeros. Each digit is multiplied
    alternately by 1 and 2; products above 9 have 9 subtracted; the total must
    be divisible by 10.

    Worth enforcing rather than accepting any 9 digits: the ID is the login
    identifier and the link to payroll records, so a typo creates a duplicate
    person rather than a failed login.

    Under settings.RELAXED_AUTH the check digit is skipped so testing accounts
    can use arbitrary numbers. The format rules (digits only, max 9) still
    apply -- those keep the column usable rather than protecting anything.
    """
    if not value.isdigit():
        raise ValidationError(_('ID number must contain digits only.'))
    if len(value) > 9:
        raise ValidationError(_('ID number cannot be longer than 9 digits.'))

    if getattr(settings, 'RELAXED_AUTH', False):
        return

    padded = value.zfill(9)
    total = 0
    for index, char in enumerate(padded):
        digit = int(char) * (1 if index % 2 == 0 else 2)
        total += digit if digit < 10 else digit - 9

    if total % 10 != 0:
        raise ValidationError(_('%(value)s is not a valid ID number.'), params={'value': value})


def normalise_phone(value):
    """Strip formatting so 050-123 4567 and 0501234567 are the same number.

    Clients sign in with their phone, so it has to match regardless of how they
    typed it. Kept deliberately simple -- no country-code rewriting, because
    guessing wrong would silently merge two different people's accounts.
    """
    if not value:
        return value
    return ''.join(ch for ch in str(value) if ch.isdigit())


class Role(models.TextChoices):
    """Which app a person signs into, and what the API hands them."""

    OFFICE = 'office', _('Office')                  # desktop: everything
    WAREHOUSE = 'warehouse', _('Warehouse worker')  # worker app: stock, picking
    DRIVER = 'driver', _('Delivery driver')         # worker app: delivery runs
    CLIENT = 'client', _('Client')                  # client app: own data only
    MANAGER = 'manager', _('Manager')               # desktop: everything + staff admin


class PayBasis(models.TextChoices):
    """How a worker's pay is reckoned."""

    HOURLY = 'hourly', _('By hour')
    DAILY = 'daily', _('By day')
    MONTHLY = 'monthly', _('Monthly salary')


# Full-time monthly hours used to derive an hourly rate from a monthly salary
# (Israel, since 2018: a 42-hour week ≈ 182 monthly hours).
MONTHLY_HOURS = Decimal('182')


class UserManager(BaseUserManager):
    """Creates users keyed on ID number (staff) or phone number (clients)."""

    use_in_migrations = True

    def _create_user(self, id_number, password, **extra_fields):
        email = extra_fields.pop('email', '')
        if id_number:
            id_number = str(id_number).strip().zfill(9)

        user = self.model(
            id_number=id_number or None,
            email=self.normalize_email(email) if email else '',
            **extra_fields,
        )
        user.full_clean(exclude=['password'])
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, id_number=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        if not id_number and extra_fields.get('role') != Role.CLIENT:
            raise ValueError(_('An ID number is required for staff accounts.'))
        return self._create_user(id_number, password, **extra_fields)

    def create_client_user(self, phone, password=None, **extra_fields):
        """A client contact. Signs in with their phone number, not an ID."""
        if not phone:
            raise ValueError(_('A phone number is required for client accounts.'))
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        extra_fields['role'] = Role.CLIENT
        extra_fields['phone'] = phone
        return self._create_user(None, password, **extra_fields)

    def get_by_natural_key(self, username):
        """Resolve a login identifier to a user.

        Staff type an ID number, clients type a phone number. Both arrive in the
        same field, so try the ID first (zero-padded) and fall back to a
        normalised phone match.
        """
        if username is None:
            raise self.model.DoesNotExist

        raw = str(username).strip()
        if raw.isdigit() and len(raw) <= 9:
            try:
                return self.get(id_number=raw.zfill(9))
            except self.model.DoesNotExist:
                pass

        return self.get(phone_normalised=normalise_phone(raw))

    def create_superuser(self, id_number, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', Role.MANAGER)

        if extra_fields.get('is_staff') is not True:
            raise ValueError(_('Superuser must have is_staff=True.'))
        if extra_fields.get('is_superuser') is not True:
            raise ValueError(_('Superuser must have is_superuser=True.'))

        return self._create_user(id_number, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """A person who signs in — staff or client contact.

    Login is ID number + password. There is no separate username: the ID
    number is the one identifier every worker already knows by heart, and it
    is what the business already keys its records on.
    """

    # Staff sign in with their ID number; client contacts sign in with their
    # phone. Exactly one of the two identifies any given user, so id_number is
    # nullable and clean() enforces which one is required per role.
    id_number = models.CharField(
        _('ID number'),
        max_length=9,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        validators=[validate_israeli_id],
        help_text=_('9-digit ID number (תעודת זהות). Staff sign in with this.'),
    )

    first_name = models.CharField(_('first name'), max_length=100)
    last_name = models.CharField(_('last name'), max_length=100)
    email = models.EmailField(_('email'), blank=True)
    phone = models.CharField(
        _('phone'),
        max_length=20,
        validators=[
            RegexValidator(
                r'^[\d\-\+\s()]{7,20}$',
                message=_('Enter a valid phone number.'),
            )
        ],
    )
    # Digits-only copy of phone, so a client typing 050-123 4567 reaches the
    # same account as 0501234567. Unique because it is a login identifier.
    phone_normalised = models.CharField(
        _('phone (normalised)'),
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        db_index=True,
    )
    date_of_birth = models.DateField(_('date of birth'), null=True, blank=True)
    address = models.TextField(_('address'), blank=True)

    role = models.CharField(
        _('role'), max_length=20, choices=Role.choices, default=Role.WAREHOUSE
    )
    language = models.CharField(
        _('language'),
        max_length=5,
        choices=[('en', _('English')), ('he', _('Hebrew')), ('ar', _('Arabic'))],
        default='he',
    )

    # Employment details, for staff roles.
    hired_on = models.DateField(_('hired on'), null=True, blank=True)
    emergency_contact = models.CharField(_('emergency contact'), max_length=150, blank=True)
    emergency_phone = models.CharField(_('emergency phone'), max_length=20, blank=True)

    # Payroll. Which rate applies depends on pay_basis; the others are ignored.
    # overtime_enabled toggles the Israeli overtime premiums (125% / 150%) on the
    # hours worked past the daily norm.
    pay_basis = models.CharField(
        _('pay basis'), max_length=10, choices=PayBasis.choices,
        default=PayBasis.HOURLY)
    hourly_rate = models.DecimalField(
        _('hourly rate'), max_digits=8, decimal_places=2, null=True, blank=True)
    daily_rate = models.DecimalField(
        _('daily rate'), max_digits=10, decimal_places=2, null=True, blank=True)
    monthly_salary = models.DecimalField(
        _('monthly salary'), max_digits=10, decimal_places=2, null=True, blank=True)
    overtime_enabled = models.BooleanField(_('pay overtime'), default=True)
    daily_regular_hours = models.DecimalField(
        _('regular hours per day'), max_digits=4, decimal_places=2,
        default=Decimal('8.00'),
        help_text=_('Hours before overtime starts; 8 for a 6-day week, 8.6 for 5-day.'))

    # Client users act on behalf of a company. Staff roles leave this null.
    client = models.ForeignKey(
        'Client',
        verbose_name=_('client'),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='users',
    )

    is_active = models.BooleanField(_('active'), default=True)
    is_staff = models.BooleanField(
        _('admin access'), default=False, help_text=_('Can sign into Django admin.')
    )
    date_joined = models.DateTimeField(_('date joined'), default=timezone.now)

    # Bumped on each authenticated request, at most once a minute. "Online"
    # means seen within a short window. This is a presence signal, not shift
    # tracking -- the clock in/out system will record real attendance later.
    last_seen = models.DateTimeField(_('last seen'), null=True, blank=True)

    # Accounts are created by a manager, who sets a starting password and tells
    # the person. Until they replace it, the manager knows their password, so
    # the API refuses everything except changing it.
    must_change_password = models.BooleanField(
        _('must change password'), default=False,
        help_text=_('Blocks all other API access until the password is changed.'),
    )
    created_by = models.ForeignKey(
        'self', verbose_name=_('created by'), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='users_created',
    )

    objects = UserManager()

    USERNAME_FIELD = 'id_number'
    REQUIRED_FIELDS = ['first_name', 'last_name', 'phone']

    class Meta:
        verbose_name = _('user')
        verbose_name_plural = _('users')
        ordering = ['last_name', 'first_name']

    def __str__(self):
        return f'{self.full_name} ({self.id_number})'

    def clean(self):
        super().clean()
        # Normalise here rather than only in save(): full_clean() runs clean()
        # before validate_unique(), so this is what lets a duplicate phone be
        # reported as a field error instead of surfacing as a database
        # IntegrityError once the INSERT is attempted.
        self.phone_normalised = normalise_phone(self.phone) or None

        # A client contact must be attached to a company; staff must not be.
        if self.role == Role.CLIENT and self.client_id is None:
            raise ValidationError({'client': _('Client users must be linked to a client.')})
        if self.role != Role.CLIENT and self.client_id is not None:
            raise ValidationError({'client': _('Only client users can be linked to a client.')})

        # Whichever identifier that role signs in with must be present.
        if self.role == Role.CLIENT:
            if not self.phone:
                raise ValidationError({'phone': _('Client users sign in with their phone number.')})
        elif not self.id_number:
            raise ValidationError({'id_number': _('Staff users sign in with their ID number.')})

    def save(self, *args, **kwargs):
        self.id_number = str(self.id_number).strip().zfill(9) if self.id_number else None
        self.phone_normalised = normalise_phone(self.phone) or None
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.first_name

    @property
    def is_staff_role(self):
        return self.role in {Role.OFFICE, Role.MANAGER, Role.WAREHOUSE, Role.DRIVER}

    @property
    def is_online(self):
        if self.last_seen is None:
            return False
        return (timezone.now() - self.last_seen) <= ONLINE_WINDOW

    @property
    def can_manage_staff(self):
        return self.role == Role.MANAGER


# ---------------------------------------------------------------------------
# 2. Catalog
# ---------------------------------------------------------------------------


class ProfileRole(models.TextChoices):
    """What the profile does in an assembled window.

    Derived from the catalog group header (כנפיים, מסילות, ...) plus the row
    description, since the source encodes the role in the grouping.
    """

    FRAME = 'frame', _('Frame')                        # משקוף
    SASH = 'sash', _('Sash')                           # כנף
    TRACK = 'track', _('Track')                        # מסילה
    MULLION = 'mullion', _('Mullion')                  # חציץ
    GLAZING_BEAD = 'glazing_bead', _('Glazing bead')   # סרגל זיגוג
    SHUTTER = 'shutter', _('Shutter')                  # תריס
    TRIM = 'trim', _('Trim')                           # הלבשה
    ADAPTER = 'adapter', _('Adapter')                  # מעבר / מתאם
    ACCESSORY = 'accessory', _('Accessory')            # פרופילי עזר
    # Curtain-wall members. The מנהטן 8100/8300 pages separate these from
    # חציץ, so they are distinct roles rather than folded into MULLION:
    # a post and a transom are not interchangeable on site.
    POST = 'post', _('Post')                           # עמוד (vertical)
    BEAM = 'beam', _('Beam')                           # קורה (horizontal)
    SEAL = 'seal', _('Seal / gasket')                  # אף שור, גומי
    RAILING = 'railing', _('Railing')                  # מעקה
    MESH = 'mesh', _('Insect screen')                  # רשת
    PANEL = 'panel', _('Panel')                        # פנל
    OTHER = 'other', _('Other')


class Family(models.Model):
    """A product line: קלאסי, אופיס, בלגי, מנהטן, נוף."""

    name = models.CharField(_('name'), max_length=100, unique=True)
    name_en = models.CharField(_('name (English)'), max_length=100, blank=True)
    slug = models.SlugField(_('slug'), max_length=100, unique=True)
    description = models.TextField(_('description'), blank=True)

    class Meta:
        verbose_name = _('family')
        verbose_name_plural = _('families')
        ordering = ['name']

    def __str__(self):
        return self.name


class Series(models.Model):
    """A numbered system such as 7000, or an unnumbered one such as תריס גלילה."""

    code = models.CharField(_('code'), max_length=20, unique=True)
    name = models.CharField(_('name'), max_length=150)
    name_en = models.CharField(_('name (English)'), max_length=150, blank=True)
    family = models.ForeignKey(
        Family,
        verbose_name=_('family'),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='series',
    )
    manufacturer = models.CharField(_('manufacturer'), max_length=100, default='Klil')
    catalog_page = models.PositiveSmallIntegerField(_('catalog page'), null=True, blank=True)
    # Aluminium is sold by weight, so a profile's price is its weight times the
    # metal price for its series. Kept per series rather than one global number
    # because alloy and finish -- and therefore price per kilo -- differ between
    # systems. Null means "not priced yet"; the catalog shows no price for it.
    price_per_kg = models.DecimalField(
        _('price per kg'), max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=_('Metal price per kilogram for this series. Drives catalog pricing.'),
    )
    is_active = models.BooleanField(_('active'), default=True)

    class Meta:
        verbose_name = _('series')
        verbose_name_plural = _('series')
        ordering = ['code']

    def __str__(self):
        return f'{self.name} {self.code}'.strip()


class Profile(models.Model):
    """One physical extrusion, identified by its manufacturer profile number.

    A profile is NOT owned by a series. The same extrusion is listed under
    several series -- the whole סרגלי זיגוג block is shared between 7300 and
    7500, and 06122/06130 appear under both 5500 and 5500D. Physically that is
    one bar on one rack, so Profile is the unique thing and SeriesProfile is
    the membership.
    """

    number = models.CharField(_('profile number'), max_length=20, unique=True, db_index=True)
    description = models.CharField(_('description'), max_length=255, blank=True)
    description_en = models.CharField(_('description (English)'), max_length=255, blank=True)

    weight_g_per_m = models.PositiveIntegerField(
        _('weight (g/m)'),
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text=_('Grams per metre, as printed in the catalog.'),
    )

    # The little cross-section drawing next to each catalog row. This is what a
    # warehouse worker actually recognises a profile by, so it matters on mobile.
    section_image = models.ImageField(
        _('cross-section drawing'), upload_to='catalog/sections/', blank=True, null=True
    )

    series = models.ManyToManyField(
        Series,
        verbose_name=_('series'),
        through='SeriesProfile',
        related_name='profiles',
    )

    is_active = models.BooleanField(_('active'), default=True)
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('updated at'), auto_now=True)

    class Meta:
        verbose_name = _('profile')
        verbose_name_plural = _('profiles')
        ordering = ['number']

    def __str__(self):
        return f'{self.number} — {self.description}' if self.description else self.number

    @property
    def weight_kg_per_m(self):
        if self.weight_g_per_m is None:
            return None
        return self.weight_g_per_m / 1000

    def weight_kg_for_length(self, metres):
        """Bar weight for a cut length — used for delivery loads and pricing."""
        if self.weight_g_per_m is None:
            return None
        return (self.weight_g_per_m * metres) / 1000


class SeriesProfile(models.Model):
    """Membership of a profile in a series, with that series' own grouping.

    The same profile can sit under a different group header in each series it
    belongs to, so role, glazing range and track count live here rather than on
    Profile.
    """

    series = models.ForeignKey(
        Series, verbose_name=_('series'), on_delete=models.CASCADE,
        related_name='series_profiles',
    )
    profile = models.ForeignKey(
        Profile, verbose_name=_('profile'), on_delete=models.CASCADE,
        related_name='series_profiles',
    )

    group_header = models.CharField(
        _('catalog group'), max_length=255, blank=True,
        help_text=_('Sub-heading this row sits under, e.g. כנפיים – זיגוג עד 8 מ"מ'),
    )
    # The catalog occasionally lists one profile number twice within a single
    # group, same weight but a different application note (06260 under 4300
    # כנפיים is listed both "שניה לדלת עם חלון" and "שניה לדלת פתיחה פנימה...").
    # The description therefore belongs to the listing, not to the extrusion.
    listed_description = models.CharField(
        _('description as listed'), max_length=255, blank=True,
        help_text=_('Only set when it differs from the profile description.'),
    )
    # Klil prints conflicting weights for a few profiles across series pages
    # (04876 is 143 g/m on the 4300/4500 pages and 152 g/m on 7300/7500;
    # 06050 is 575 on p13 and 573 on p23). Verified against the source at
    # 500dpi — the catalog itself disagrees. Set only where it differs from
    # Profile.weight_g_per_m, so the printed value stays recoverable.
    listed_weight_g_per_m = models.PositiveIntegerField(
        _('weight as listed (g/m)'), null=True, blank=True,
        validators=[MinValueValidator(1)],
    )

    role = models.CharField(
        _('role'), max_length=20, choices=ProfileRole.choices, default=ProfileRole.OTHER
    )

    # Glazing capacity is printed as a range: "זיגוג 11÷16 מ״מ", or as a ceiling:
    # "זיגוג עד 8 מ״מ" (min stays null).
    glass_min_mm = models.DecimalField(
        _('glass min (mm)'), max_digits=5, decimal_places=1, null=True, blank=True
    )
    glass_max_mm = models.DecimalField(
        _('glass max (mm)'), max_digits=5, decimal_places=1, null=True, blank=True
    )
    track_count = models.PositiveSmallIntegerField(
        _('tracks'), null=True, blank=True,
        help_text=_('For rails: 1–5 נתיבים.'),
    )

    catalog_page = models.PositiveSmallIntegerField(_('catalog page'), null=True, blank=True)
    position = models.PositiveSmallIntegerField(_('position'), default=0)

    class Meta:
        verbose_name = _('series profile')
        verbose_name_plural = _('series profiles')
        ordering = ['series', 'position']
        # Deliberately not unique on (series, profile, group_header): the source
        # catalog contains genuine same-group repeats. Uniqueness includes the
        # listed description so those survive an idempotent re-import.
        constraints = [
            models.UniqueConstraint(
                fields=['series', 'profile', 'group_header', 'listed_description'],
                name='unique_profile_listing',
            )
        ]
        indexes = [
            models.Index(fields=['series', 'role']),
            models.Index(fields=['role', 'glass_max_mm']),
        ]

    def __str__(self):
        return f'{self.series.code} · {self.profile.number}'

    @property
    def display_description(self):
        return self.listed_description or self.profile.description

    @property
    def effective_weight_g_per_m(self):
        return self.listed_weight_g_per_m or self.profile.weight_g_per_m

    @property
    def price_per_m(self):
        """Metal price for one metre of this profile, from its series' price/kg.

        None when either the weight or the series price is unknown -- the
        catalog then shows no price rather than a misleading zero.
        """
        weight_g = self.effective_weight_g_per_m
        price_kg = self.series.price_per_kg
        if not weight_g or price_kg is None:
            return None
        return (Decimal(weight_g) / Decimal(1000)) * price_kg

    def fits_glass(self, thickness_mm):
        """Whether a given glass thickness is within this profile's range."""
        if self.glass_max_mm is None:
            return None
        if thickness_mm > self.glass_max_mm:
            return False
        if self.glass_min_mm is not None and thickness_mm < self.glass_min_mm:
            return False
        return True


# ---------------------------------------------------------------------------
# 3. Stock
# ---------------------------------------------------------------------------


class Shop(models.Model):
    """The owner's own business -- the shop the map is centred on.

    A single row: there is one business. It is kept as a model (not settings)
    so the office can edit its address and move its pin like any other place.
    """

    name = models.CharField(_('name'), max_length=200, default='AlomForce')
    legal_name = models.CharField(_('registered legal name'), max_length=200, blank=True)
    tax_id = models.CharField(
        _('tax ID'), max_length=32, blank=True,
        help_text=_('ח.פ. / ע.מ. — appears on order and delivery documents.'))
    # Company logo, printed at the top of order/delivery/payslip PDFs. Stored in
    # the configured storage (Cloudinary when set up, local /media otherwise).
    logo = models.ImageField(_('logo'), upload_to='shop/', blank=True, null=True)
    address = models.TextField(_('address'), blank=True)
    city = models.CharField(_('city'), max_length=100, blank=True)
    phone = models.CharField(_('phone'), max_length=32, blank=True)
    email = models.EmailField(_('email'), blank=True)
    latitude = models.DecimalField(
        _('latitude'), max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        _('longitude'), max_digits=9, decimal_places=6, null=True, blank=True
    )

    class Meta:
        verbose_name = _('shop')
        verbose_name_plural = _('shop')

    def __str__(self):
        return self.name

    @classmethod
    def get(cls):
        """The single shop row, created on first access."""
        return cls.objects.first() or cls.objects.create()


class AppConfig(models.Model):
    """App-wide configuration a manager edits from the desktop Settings page.

    Kept in the database (not only .env) so image storage can be switched to
    Cloudinary from the app -- fill in the three credentials and uploads go to
    Cloudinary without a redeploy. Empty credentials mean local disk storage.
    """

    cloudinary_cloud_name = models.CharField(
        _('Cloudinary cloud name'), max_length=100, blank=True)
    cloudinary_api_key = models.CharField(
        _('Cloudinary API key'), max_length=100, blank=True)
    cloudinary_api_secret = models.CharField(
        _('Cloudinary API secret'), max_length=200, blank=True)

    # OpenAI — invoice scanning (ChatGPT Vision) reads a photo into fields.
    openai_api_key = models.CharField(
        _('OpenAI API key'), max_length=200, blank=True)

    # SMTP — sending the accountant the zipped invoices.
    smtp_host = models.CharField(_('SMTP host'), max_length=200, blank=True)
    smtp_port = models.PositiveIntegerField(_('SMTP port'), null=True, blank=True)
    smtp_user = models.CharField(_('SMTP username'), max_length=200, blank=True)
    smtp_password = models.CharField(_('SMTP password'), max_length=200, blank=True)
    smtp_from = models.EmailField(_('SMTP from address'), blank=True)
    smtp_use_tls = models.BooleanField(_('SMTP use TLS'), default=True)

    # Accountant — where the books get sent.
    accountant_name = models.CharField(_('accountant name'), max_length=200, blank=True)
    accountant_email = models.EmailField(_('accountant email'), blank=True)
    accountant_phone = models.CharField(_('accountant phone'), max_length=32, blank=True)

    # Green Invoice — legal e-invoicing (stored now, wired later).
    greeninvoice_api_key = models.CharField(
        _('Green Invoice API key'), max_length=200, blank=True)
    greeninvoice_api_secret = models.CharField(
        _('Green Invoice API secret'), max_length=200, blank=True)

    class Meta:
        verbose_name = _('app configuration')
        verbose_name_plural = _('app configuration')

    def __str__(self):
        return 'App configuration'

    # Fields whose value may instead come from a Railway (environment) variable,
    # which is the safe place for production secrets. The environment always
    # wins over the database, so keys never have to live in the DB in prod.
    ENV_MAP = {
        'openai_api_key': 'OPENAI_API_KEY',
        'smtp_host': 'EMAIL_HOST',
        'smtp_port': 'EMAIL_PORT',
        'smtp_user': 'EMAIL_HOST_USER',
        'smtp_password': 'EMAIL_HOST_PASSWORD',
        'smtp_from': 'DEFAULT_FROM_EMAIL',
        'greeninvoice_api_key': 'GREENINVOICE_API_KEY',
        'greeninvoice_api_secret': 'GREENINVOICE_API_SECRET',
        'cloudinary_cloud_name': 'CLOUDINARY_CLOUD_NAME',
        'cloudinary_api_key': 'CLOUDINARY_API_KEY',
        'cloudinary_api_secret': 'CLOUDINARY_API_SECRET',
    }

    @classmethod
    def get(cls):
        return cls.objects.first() or cls.objects.create()

    def setting(self, field):
        """Resolve a config value: an environment variable wins, else the DB.

        Uses decouple's config(), so both a real env var (Railway) and a value
        in the project's .env (local dev) are picked up.
        """
        from decouple import config
        env = self.ENV_MAP.get(field)
        if env:
            val = config(env, default=None)
            if val:
                return str(val).strip()
        return getattr(self, field, '') or ''

    def from_env(self, field):
        """True when this value is supplied by an environment / .env variable."""
        from decouple import config
        env = self.ENV_MAP.get(field)
        return bool(env and config(env, default=None))

    @property
    def cloudinary_ready(self):
        return bool(self.setting('cloudinary_cloud_name')
                    and self.setting('cloudinary_api_key')
                    and self.setting('cloudinary_api_secret'))

    @property
    def smtp_ready(self):
        return bool(self.setting('smtp_host') and self.setting('smtp_from'))

    @property
    def openai_ready(self):
        return bool(self.setting('openai_api_key'))

    @property
    def greeninvoice_ready(self):
        return bool(self.setting('greeninvoice_api_key')
                    and self.setting('greeninvoice_api_secret'))


class Warehouse(models.Model):
    name = models.CharField(_('name'), max_length=150)
    address = models.TextField(_('address'), blank=True)
    city = models.CharField(_('city'), max_length=100, blank=True)
    latitude = models.DecimalField(
        _('latitude'), max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        _('longitude'), max_digits=9, decimal_places=6, null=True, blank=True
    )
    is_active = models.BooleanField(_('active'), default=True)

    class Meta:
        verbose_name = _('warehouse')
        verbose_name_plural = _('warehouses')

    def __str__(self):
        return self.name


class Location(models.Model):
    """A rack or bay inside a warehouse. Barcode-labelled for the worker app."""

    warehouse = models.ForeignKey(
        Warehouse, verbose_name=_('warehouse'), on_delete=models.CASCADE,
        related_name='locations',
    )
    code = models.CharField(_('code'), max_length=50)
    barcode = models.CharField(_('barcode'), max_length=64, blank=True, db_index=True)
    description = models.CharField(_('description'), max_length=255, blank=True)

    class Meta:
        verbose_name = _('location')
        verbose_name_plural = _('locations')
        ordering = ['warehouse', 'code']
        constraints = [
            models.UniqueConstraint(
                fields=['warehouse', 'code'], name='unique_location_per_warehouse'
            )
        ]

    def __str__(self):
        return f'{self.warehouse.name} / {self.code}'


class StockItem(models.Model):
    """A profile held at a location in a specific bar length and finish.

    Length and finish are part of the identity: 6.5 m anodised 03303 is not
    interchangeable with 6.0 m white 03303, even though both are profile 03303.
    """

    profile = models.ForeignKey(
        Profile, verbose_name=_('profile'), on_delete=models.PROTECT,
        related_name='stock_items',
    )
    location = models.ForeignKey(
        Location, verbose_name=_('location'), on_delete=models.PROTECT,
        related_name='stock_items',
    )
    length_mm = models.PositiveIntegerField(
        _('bar length (mm)'), validators=[MinValueValidator(1)]
    )
    finish = models.CharField(
        _('finish'), max_length=100, blank=True,
        help_text=_('Anodised, powder-coated RAL code, mill finish, etc.'),
    )

    minimum_quantity = models.PositiveIntegerField(
        _('reorder level'), default=0,
        help_text=_('Alert the office when quantity on hand drops below this.'),
    )

    class Meta:
        verbose_name = _('stock item')
        verbose_name_plural = _('stock items')
        constraints = [
            models.UniqueConstraint(
                fields=['profile', 'location', 'length_mm', 'finish'],
                name='unique_stock_item',
            )
        ]

    def __str__(self):
        return f'{self.profile.number} @ {self.location.code} ({self.length_mm}mm)'

    @property
    def quantity(self):
        """Bars on hand, summed from the movement ledger."""
        return self.movements.aggregate(n=Sum('quantity'))['n'] or 0

    @property
    def needs_reorder(self):
        return self.quantity < self.minimum_quantity


class MovementType(models.TextChoices):
    RECEIPT = 'receipt', _('Goods received')
    PICK = 'pick', _('Picked for order')
    RETURN = 'return', _('Returned')

    ADJUSTMENT = 'adjustment', _('Stock count adjustment')
    TRANSFER_IN = 'transfer_in', _('Transferred in')
    TRANSFER_OUT = 'transfer_out', _('Transferred out')
    SCRAP = 'scrap', _('Scrapped')


class StockMovement(models.Model):
    """One in/out event. Positive quantity adds bars, negative removes them.

    Stock is an append-only ledger rather than a mutable quantity field: two
    warehouse workers scanning the same rack at once would otherwise overwrite
    each other, and a wrong count with no history is impossible to audit back.
    """

    stock_item = models.ForeignKey(
        StockItem, verbose_name=_('stock item'), on_delete=models.PROTECT,
        related_name='movements',
    )
    movement_type = models.CharField(
        _('type'), max_length=20, choices=MovementType.choices
    )
    quantity = models.IntegerField(
        _('bars'), help_text=_('Negative for outgoing movements.')
    )

    order_line = models.ForeignKey(
        'OrderLine', verbose_name=_('order line'), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='movements',
    )
    performed_by = models.ForeignKey(
        User, verbose_name=_('performed by'), on_delete=models.PROTECT,
        related_name='stock_movements',
    )
    note = models.CharField(_('note'), max_length=255, blank=True)
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('stock movement')
        verbose_name_plural = _('stock movements')
        ordering = ['-created_at']
        indexes = [models.Index(fields=['stock_item', '-created_at'])]

    def __str__(self):
        return f'{self.get_movement_type_display()} {self.quantity:+d} — {self.stock_item}'


# ---------------------------------------------------------------------------
# 4. Sales
# ---------------------------------------------------------------------------


class PriceTier(models.Model):
    """Named discount level, e.g. contractor vs retail."""

    name = models.CharField(_('name'), max_length=100, unique=True)
    discount_percent = models.DecimalField(
        _('discount %'), max_digits=5, decimal_places=2, default=Decimal('0.00')
    )

    class Meta:
        verbose_name = _('price tier')
        verbose_name_plural = _('price tiers')

    def __str__(self):
        return f'{self.name} (-{self.discount_percent}%)'


class BusinessType(models.TextChoices):
    """Israeli business forms — these determine how the client is invoiced."""

    OSEK_PATUR = 'osek_patur', _('Osek Patur (עוסק פטור)')
    OSEK_MURSHE = 'osek_murshe', _('Osek Murshe (עוסק מורשה)')
    COMPANY = 'company', _('Company (חברה בע"מ)')
    PARTNERSHIP = 'partnership', _('Partnership (שותפות)')
    NONPROFIT = 'nonprofit', _('Non-profit (עמותה)')
    OTHER = 'other', _('Other')


class Client(models.Model):
    """A company that buys profiles.

    Created either by the office or by a client signing up in the client app,
    in which case the business details come from the signup form.
    """

    name = models.CharField(_('business name'), max_length=200)
    legal_name = models.CharField(
        _('registered legal name'), max_length=200, blank=True,
        help_text=_('If it differs from the trading name.'),
    )
    business_type = models.CharField(
        _('business type'), max_length=20, choices=BusinessType.choices,
        default=BusinessType.OSEK_MURSHE,
    )
    tax_id = models.CharField(
        _('tax ID'), max_length=32, blank=True, db_index=True,
        help_text=_('ח.פ. / ע.מ. — appears on every invoice.'),
    )
    business_number = models.CharField(
        _('company registration number'), max_length=32, blank=True
    )

    contact_name = models.CharField(_('contact name'), max_length=150, blank=True)
    phone = models.CharField(_('phone'), max_length=32, blank=True)
    email = models.EmailField(_('email'), blank=True)
    website = models.URLField(_('website'), blank=True)

    address = models.TextField(_('billing address'), blank=True)
    city = models.CharField(_('city'), max_length=100, blank=True)
    postal_code = models.CharField(_('postal code'), max_length=20, blank=True)
    delivery_address = models.TextField(
        _('delivery address'), blank=True,
        help_text=_('Only if deliveries go somewhere other than the billing address.'),
    )
    # Map coordinates. Filled by geocoding the address, then adjustable by
    # dragging the pin -- so a wrong or vague address can still be placed right.
    latitude = models.DecimalField(
        _('latitude'), max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        _('longitude'), max_digits=9, decimal_places=6, null=True, blank=True
    )
    notes = models.TextField(_('notes'), blank=True)

    price_tier = models.ForeignKey(
        PriceTier, verbose_name=_('price tier'), on_delete=models.PROTECT,
        null=True, blank=True, related_name='clients',
    )
    credit_limit = models.DecimalField(
        _('credit limit'), max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    is_active = models.BooleanField(_('active'), default=True)
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('client')
        verbose_name_plural = _('clients')
        ordering = ['name']

    def __str__(self):
        return self.name


class OrderStatus(models.TextChoices):
    DRAFT = 'draft', _('Draft')
    SUBMITTED = 'submitted', _('Submitted')
    CONFIRMED = 'confirmed', _('Confirmed')
    PICKING = 'picking', _('Picking')
    READY = 'ready', _('Ready for delivery')
    OUT_FOR_DELIVERY = 'out_for_delivery', _('Out for delivery')  # on the truck
    DELIVERED = 'delivered', _('Delivered')
    CANCELLED = 'cancelled', _('Cancelled')


class Order(models.Model):
    """A client order. Created in the desktop app or by the client on mobile."""

    number = models.CharField(_('order number'), max_length=32, unique=True)
    client = models.ForeignKey(
        Client, verbose_name=_('client'), on_delete=models.PROTECT, related_name='orders'
    )
    status = models.CharField(
        _('status'), max_length=20, choices=OrderStatus.choices, default=OrderStatus.DRAFT
    )
    ordered_at = models.DateTimeField(_('ordered at'), auto_now_add=True)
    required_by = models.DateField(_('required by'), null=True, blank=True)
    notes = models.TextField(_('notes'), blank=True)

    # Terms snapshot: taken from the client's price tier and the statutory VAT
    # rate when the order is created, so a later change to either does not
    # silently re-price an order that has already been sent.
    discount_percent = models.DecimalField(
        _('discount %'), max_digits=5, decimal_places=2, default=Decimal('0.00'))
    vat_percent = models.DecimalField(
        _('VAT %'), max_digits=5, decimal_places=2, default=Decimal('18.00'))

    created_by = models.ForeignKey(
        User, verbose_name=_('created by'), on_delete=models.PROTECT,
        related_name='orders_created',
    )

    # The generated documents are persisted here so they live in the configured
    # storage (Cloudinary when set up, local /media otherwise) rather than only
    # being streamed on the fly.
    order_note_pdf = models.FileField(
        _('order note PDF'), upload_to='orders/', blank=True, null=True)
    delivery_note_pdf = models.FileField(
        _('delivery note PDF'), upload_to='orders/', blank=True, null=True)
    # Set when the client signs the delivery note on the phone app. Null means
    # the delivery note is not yet signed.
    delivery_signed_at = models.DateTimeField(
        _('delivery signed at'), null=True, blank=True)
    # Unguessable token for the public, login-free delivery-note link the driver
    # sends the client over WhatsApp. Minted when the delivery is signed.
    public_token = models.UUIDField(
        _('public token'), null=True, blank=True, editable=False, db_index=True)

    class Meta:
        verbose_name = _('order')
        verbose_name_plural = _('orders')
        ordering = ['-ordered_at']

    def __str__(self):
        return f'{self.number} — {self.client.name}'

    # Aluminium is sold by weight, so the money follows the weight: each line is
    # weight x price/kg, then an order-wide discount and VAT.
    @property
    def subtotal(self):
        return sum((line.line_total for line in self.lines.all()), Decimal('0.00'))

    @property
    def discount_amount(self):
        return (self.subtotal * self.discount_percent / 100).quantize(Decimal('0.01'))

    @property
    def net(self):
        return self.subtotal - self.discount_amount

    @property
    def vat_amount(self):
        return (self.net * self.vat_percent / 100).quantize(Decimal('0.01'))

    @property
    def total(self):
        return self.net + self.vat_amount

    @property
    def total_weight_kg(self):
        return sum((line.effective_weight_kg or Decimal('0')
                    for line in self.lines.all()), Decimal('0'))

    # -- invoicing (an order may be invoiced in parts) -------------------

    @property
    def invoiced_total(self):
        """Sum of income invoices raised against this order."""
        return sum((inv.total for inv in self.invoices.all()
                    if inv.direction == 'income'), Decimal('0.00'))

    @property
    def remaining_to_invoice(self):
        """How much of the order total is not yet invoiced (never negative)."""
        remaining = self.total - self.invoiced_total
        return remaining if remaining > 0 else Decimal('0.00')

    @property
    def is_fully_invoiced(self):
        # A hair of tolerance so rounding never leaves an order 'unfinished'.
        return self.invoiced_total >= (self.total - Decimal('0.01'))


class OrderLine(models.Model):
    """One profile on an order, measured in metres and priced by weight.

    The quantity a customer asks for is a total length of profile. That length
    times the profile's weight-per-metre gives the aluminium weight, and weight
    times the series price-per-kilo gives the money -- which is how the metal is
    actually sold. A line may instead be entered as a bar count at a bar length
    (10 bars of 6 m); metres is then just bars x length, kept in `total_length_m`
    either way so everything downstream reads one field.
    """

    order = models.ForeignKey(
        Order, verbose_name=_('order'), on_delete=models.CASCADE, related_name='lines'
    )
    profile = models.ForeignKey(
        Profile, verbose_name=_('profile'), on_delete=models.PROTECT,
        related_name='order_lines',
    )
    series = models.ForeignKey(
        Series, verbose_name=_('series'), on_delete=models.PROTECT,
        null=True, blank=True,
        help_text=_('Which series this line was ordered for; a profile can serve several.'),
    )

    # Optional bar-count detail: present when the line was entered as "N bars of
    # L", null when entered as a plain total length. total_length_m is always set.
    length_mm = models.PositiveIntegerField(
        _('bar length (mm)'), null=True, blank=True,
        validators=[MinValueValidator(1)])
    quantity = models.PositiveIntegerField(
        _('bars'), null=True, blank=True, validators=[MinValueValidator(1)])
    total_length_m = models.DecimalField(
        _('total length (m)'), max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))])

    # Weight of aluminium for this line. Left null to derive from the profile's
    # weight-per-metre; set to override when the catalog weight is wrong or the
    # cut differs from the nominal section.
    weight_kg_override = models.DecimalField(
        _('weight override (kg)'), max_digits=10, decimal_places=2,
        null=True, blank=True)
    # Price per kilo charged on this line, snapshotted from the series so the
    # order keeps its price even if the series price/kg later changes.
    price_per_kg = models.DecimalField(
        _('price per kg'), max_digits=10, decimal_places=2, default=Decimal('0.00'))

    # Filled by the warehouse worker while preparing the order: the line's real
    # weighed weight (which also overrides the estimate via weight_kg_override),
    # a note for any shortage, and a done flag once it is picked.
    prepared = models.BooleanField(_('prepared'), default=False)
    shortage_note = models.CharField(_('shortage note'), max_length=255, blank=True)
    # How much of the line was actually loaded onto the truck (metres). Null
    # until the delivery is prepared; the delivery note shows it as 'supplied'.
    delivered_length_m = models.DecimalField(
        _('delivered length (m)'), max_digits=10, decimal_places=2,
        null=True, blank=True)

    class Meta:
        verbose_name = _('order line')
        verbose_name_plural = _('order lines')

    def __str__(self):
        return f'{self.profile.number} — {self.total_length_m} m'

    @property
    def computed_weight_kg(self):
        """Weight from the catalog: weight-per-metre x total metres."""
        per_m = self.profile.weight_kg_per_m
        if per_m is None:
            return None
        return (Decimal(str(per_m)) * self.total_length_m).quantize(Decimal('0.01'))

    @property
    def effective_weight_kg(self):
        """The override if set, else the computed weight."""
        if self.weight_kg_override is not None:
            return self.weight_kg_override
        return self.computed_weight_kg

    @property
    def line_total(self):
        weight = self.effective_weight_kg or Decimal('0')
        return (weight * self.price_per_kg).quantize(Decimal('0.01'))

    # The standard stock bar length when a line was entered as plain metres and
    # so carries no bar length of its own (matches the order editor's default).
    DEFAULT_BAR_MM = 6000

    @property
    def bars_needed(self):
        """Whole bars to pull: the bar count if given, else total length ÷ bar
        length rounded up (using the line's bar length, or the 6 m standard)."""
        import math
        if self.quantity:
            return self.quantity
        bar_mm = self.length_mm or self.DEFAULT_BAR_MM
        if not bar_mm:
            return None
        bar_m = Decimal(bar_mm) / 1000
        return math.ceil(self.total_length_m / bar_m)


class Document(models.Model):
    """Invoices and delivery notes. Both are PDFs stored on Cloudinary."""

    class Kind(models.TextChoices):
        INVOICE = 'invoice', _('Invoice')
        DELIVERY_NOTE = 'delivery_note', _('Delivery note')
        QUOTE = 'quote', _('Quote')
        CREDIT_NOTE = 'credit_note', _('Credit note')

    kind = models.CharField(_('kind'), max_length=20, choices=Kind.choices)
    number = models.CharField(_('document number'), max_length=32)
    client = models.ForeignKey(
        Client, verbose_name=_('client'), on_delete=models.PROTECT, related_name='documents'
    )
    order = models.ForeignKey(
        Order, verbose_name=_('order'), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='documents',
    )

    issued_at = models.DateField(_('issued at'))
    due_at = models.DateField(_('due at'), null=True, blank=True)
    total = models.DecimalField(_('total'), max_digits=12, decimal_places=2)
    vat = models.DecimalField(
        _('VAT'), max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    is_paid = models.BooleanField(_('paid'), default=False)

    pdf = models.FileField(_('PDF'), upload_to='documents/', blank=True, null=True)

    class Meta:
        verbose_name = _('document')
        verbose_name_plural = _('documents')
        ordering = ['-issued_at']
        constraints = [
            models.UniqueConstraint(fields=['kind', 'number'], name='unique_document_number')
        ]

    def __str__(self):
        return f'{self.get_kind_display()} {self.number}'


class Invoice(models.Model):
    """An income or expense invoice for the accountant's books.

    Income invoices are money coming in (sales to clients); expense invoices are
    money going out (purchases from suppliers). Either can be entered by hand,
    scanned from a photo (OpenAI Vision), or generated legally through Green
    Invoice later. The file (PDF or photo) is kept in the configured storage so
    the whole set can be zipped and sent to the accountant.
    """

    class Direction(models.TextChoices):
        INCOME = 'income', _('Income')
        EXPENSE = 'expense', _('Expense')

    class Source(models.TextChoices):
        MANUAL = 'manual', _('Manual')
        GENERATED = 'generated', _('Generated')
        SCANNED = 'scanned', _('Scanned')

    class Status(models.TextChoices):
        UNPAID = 'unpaid', _('Unpaid')
        PAID = 'paid', _('Paid')

    direction = models.CharField(
        _('direction'), max_length=10, choices=Direction.choices)
    number = models.CharField(_('invoice number'), max_length=64, blank=True)

    # Income invoices may link to a client; expenses name a supplier free-text.
    # party_name/party_tax_id snapshot whoever the invoice is with, either way.
    client = models.ForeignKey(
        Client, verbose_name=_('client'), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='invoices')
    # Income invoices can be raised against an order -- often for part of it, so
    # an order can have several invoices that together cover its total.
    order = models.ForeignKey(
        'Order', verbose_name=_('order'), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='invoices')
    party_name = models.CharField(_('party name'), max_length=200, blank=True)
    party_tax_id = models.CharField(_('party tax ID'), max_length=32, blank=True)

    issued_at = models.DateField(_('issued at'))
    category = models.CharField(_('category'), max_length=100, blank=True)

    subtotal = models.DecimalField(
        _('subtotal'), max_digits=12, decimal_places=2, default=Decimal('0.00'))
    vat = models.DecimalField(
        _('VAT'), max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total = models.DecimalField(
        _('total'), max_digits=12, decimal_places=2, default=Decimal('0.00'))
    # How much the client has actually paid against this invoice. Status is
    # derived from it (paid once amount_paid covers the total).
    amount_paid = models.DecimalField(
        _('amount paid'), max_digits=12, decimal_places=2, default=Decimal('0.00'))

    source = models.CharField(
        _('source'), max_length=12, choices=Source.choices, default=Source.MANUAL)
    status = models.CharField(
        _('status'), max_length=10, choices=Status.choices, default=Status.UNPAID)

    # Filled by Green Invoice later: the legal allocation (hakptsa) number and
    # the provider's document id.
    allocation_number = models.CharField(
        _('allocation number'), max_length=64, blank=True)
    external_id = models.CharField(_('external id'), max_length=64, blank=True)

    file = models.FileField(
        _('file'), upload_to='invoices/', blank=True, null=True)
    notes = models.TextField(_('notes'), blank=True)

    created_by = models.ForeignKey(
        User, verbose_name=_('created by'), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='invoices_created')
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('invoice')
        verbose_name_plural = _('invoices')
        ordering = ['-issued_at', '-id']

    def __str__(self):
        return f'{self.get_direction_display()} {self.number or self.id}'

    @property
    def balance_due(self):
        """How much of the invoice total is still unpaid (never negative)."""
        remaining = (self.total or Decimal('0')) - (self.amount_paid or Decimal('0'))
        return remaining if remaining > 0 else Decimal('0.00')

    def sync_status(self):
        """Set paid/unpaid from how much has been paid (a hair of tolerance)."""
        if (self.amount_paid or Decimal('0')) >= (self.total or Decimal('0')) - Decimal('0.01'):
            self.status = self.Status.PAID
        else:
            self.status = self.Status.UNPAID


class Delivery(models.Model):
    """A delivery run — what the driver sees in the worker app."""

    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        LOADED = 'loaded', _('Loaded')
        EN_ROUTE = 'en_route', _('En route')
        DELIVERED = 'delivered', _('Delivered')
        FAILED = 'failed', _('Failed')

    order = models.ForeignKey(
        Order, verbose_name=_('order'), on_delete=models.PROTECT, related_name='deliveries'
    )
    driver = models.ForeignKey(
        User, verbose_name=_('driver'), on_delete=models.PROTECT,
        null=True, blank=True, related_name='deliveries',
    )
    status = models.CharField(
        _('status'), max_length=20, choices=Status.choices, default=Status.PENDING
    )
    scheduled_for = models.DateField(_('scheduled for'), null=True, blank=True)
    delivered_at = models.DateTimeField(_('delivered at'), null=True, blank=True)

    address = models.TextField(_('delivery address'), blank=True)
    recipient_name = models.CharField(_('received by'), max_length=150, blank=True)
    signature = models.ImageField(
        _('signature'), upload_to='deliveries/signatures/', blank=True, null=True
    )
    photo = models.ImageField(
        _('proof photo'), upload_to='deliveries/photos/', blank=True, null=True
    )
    notes = models.TextField(_('notes'), blank=True)

    class Meta:
        verbose_name = _('delivery')
        verbose_name_plural = _('deliveries')
        ordering = ['-scheduled_for']

    def __str__(self):
        return f'{self.order.number} → {self.get_status_display()}'


# ---------------------------------------------------------------------------
# 6. Attendance (clock in / out)
# ---------------------------------------------------------------------------


class Shift(models.Model):
    """One work session: a clock-in, and a clock-out when the shift ends.

    A worker has at most one open shift (clock_out is null) at a time; the
    clock-in endpoint refuses a second. Hours worked come from the two
    timestamps, and feed the salary calculation later.
    """

    worker = models.ForeignKey(
        User, verbose_name=_('worker'), on_delete=models.CASCADE,
        related_name='shifts',
    )
    clock_in = models.DateTimeField(_('clock in'))
    clock_out = models.DateTimeField(_('clock out'), null=True, blank=True)
    note = models.CharField(_('note'), max_length=255, blank=True)
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('shift')
        verbose_name_plural = _('shifts')
        ordering = ['-clock_in']
        indexes = [models.Index(fields=['worker', '-clock_in'])]

    def __str__(self):
        return f'{self.worker.full_name} — {self.clock_in:%Y-%m-%d %H:%M}'

    @property
    def is_open(self):
        return self.clock_out is None

    @property
    def duration_minutes(self):
        """Minutes worked; counts up to now while the shift is still open."""
        end = self.clock_out or timezone.now()
        return max(0, int((end - self.clock_in).total_seconds() // 60))

    @property
    def hours(self):
        return round(self.duration_minutes / 60, 2)


class ShiftCorrectionRequest(models.Model):
    """A worker's request to fix a day's clock-in/out, for a manager to approve.

    Clocking is once-per-day and can't be edited by the worker, so a mistake
    (forgot to clock out, wrong time, or a whole day missed) is fixed by raising
    a request. `shift` is the shift to change, or null when the day has none and
    a new one should be created on approval.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        APPROVED = 'approved', _('Approved')
        REJECTED = 'rejected', _('Rejected')

    worker = models.ForeignKey(
        User, verbose_name=_('worker'), on_delete=models.CASCADE,
        related_name='correction_requests')
    shift = models.ForeignKey(
        Shift, verbose_name=_('shift'), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='correction_requests')

    work_date = models.DateField(_('work date'))
    requested_clock_in = models.DateTimeField(_('requested clock in'), null=True, blank=True)
    requested_clock_out = models.DateTimeField(_('requested clock out'), null=True, blank=True)
    reason = models.TextField(_('reason'), blank=True)

    status = models.CharField(
        _('status'), max_length=10, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        User, verbose_name=_('reviewed by'), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='correction_reviews')
    reviewed_at = models.DateTimeField(_('reviewed at'), null=True, blank=True)
    review_note = models.CharField(_('review note'), max_length=255, blank=True)

    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('shift correction request')
        verbose_name_plural = _('shift correction requests')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.worker.full_name} — {self.work_date} ({self.status})'


# ---------------------------------------------------------------------------
# 7. Payroll (payslips)
# ---------------------------------------------------------------------------


class Payslip(models.Model):
    """A worker's pay for one month.

    Either generated by the app from clocked shifts (base pay + Israeli
    overtime) or entered by hand from the accountant. Every figure is a snapshot
    the office can override while the slip is a draft; finalising locks it.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        FINAL = 'final', _('Finalised')

    class Source(models.TextChoices):
        GENERATED = 'generated', _('Generated')
        MANUAL = 'manual', _('Manual')

    worker = models.ForeignKey(
        User, verbose_name=_('worker'), on_delete=models.CASCADE,
        related_name='payslips')
    year = models.PositiveIntegerField(_('year'))
    month = models.PositiveSmallIntegerField(_('month'))

    status = models.CharField(
        _('status'), max_length=10, choices=Status.choices, default=Status.DRAFT)
    source = models.CharField(
        _('source'), max_length=10, choices=Source.choices, default=Source.GENERATED)

    # Snapshots so the slip is self-contained even if the worker's terms change.
    pay_basis = models.CharField(
        _('pay basis'), max_length=10, choices=PayBasis.choices, default=PayBasis.HOURLY)
    overtime_enabled = models.BooleanField(_('overtime paid'), default=True)

    # Loaded from shifts, then overridable.
    days_worked = models.PositiveSmallIntegerField(_('days worked'), default=0)
    regular_hours = models.DecimalField(
        _('regular hours'), max_digits=7, decimal_places=2, default=Decimal('0'))
    overtime_125_hours = models.DecimalField(
        _('overtime 125% hours'), max_digits=7, decimal_places=2, default=Decimal('0'))
    overtime_150_hours = models.DecimalField(
        _('overtime 150% hours'), max_digits=7, decimal_places=2, default=Decimal('0'))

    hourly_rate = models.DecimalField(
        _('hourly rate'), max_digits=10, decimal_places=2, default=Decimal('0'))
    base_pay = models.DecimalField(
        _('base pay'), max_digits=12, decimal_places=2, default=Decimal('0'))
    overtime_pay = models.DecimalField(
        _('overtime pay'), max_digits=12, decimal_places=2, default=Decimal('0'))

    note = models.CharField(_('note'), max_length=255, blank=True)
    pdf = models.FileField(_('PDF'), upload_to='payslips/', null=True, blank=True)
    created_by = models.ForeignKey(
        User, verbose_name=_('created by'), on_delete=models.SET_NULL,
        null=True, blank=True, related_name='payslips_created')
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)
    finalised_at = models.DateTimeField(_('finalised at'), null=True, blank=True)

    class Meta:
        verbose_name = _('payslip')
        verbose_name_plural = _('payslips')
        ordering = ['-year', '-month', 'worker__last_name']
        constraints = [
            models.UniqueConstraint(
                fields=['worker', 'year', 'month'], name='unique_payslip_period')
        ]

    def __str__(self):
        return f'{self.worker.full_name} — {self.year}-{self.month:02d}'

    @property
    def is_final(self):
        return self.status == self.Status.FINAL

    @property
    def adjustments_total(self):
        return sum((a.amount for a in self.adjustments.all()), Decimal('0.00'))

    @property
    def total_pay(self):
        return (self.base_pay + self.overtime_pay + self.adjustments_total
                ).quantize(Decimal('0.01'))

    @property
    def total_hours(self):
        return self.regular_hours + self.overtime_125_hours + self.overtime_150_hours


class PayslipAdjustment(models.Model):
    """A one-off line on a payslip: a positive bonus/allowance or a negative
    deduction/advance."""

    payslip = models.ForeignKey(
        Payslip, verbose_name=_('payslip'), on_delete=models.CASCADE,
        related_name='adjustments')
    label = models.CharField(_('label'), max_length=100)
    amount = models.DecimalField(
        _('amount'), max_digits=12, decimal_places=2,
        help_text=_('Positive to add (bonus, travel), negative to deduct (advance).'))

    class Meta:
        verbose_name = _('payslip adjustment')
        verbose_name_plural = _('payslip adjustments')
        ordering = ['id']

    def __str__(self):
        return f'{self.label}: {self.amount}'
