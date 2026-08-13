"""API serializers.

Kept out of views.py only because DRF resolves serializers at import time and
circular imports get ugly once views reference each other. Everything else
stays in the single models.py / views.py pair.
"""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import (
    get_password_validators,
    validate_password as django_validate_password,
)
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from core import maplinks
from core.models import (
    AppConfig,
    Client,
    Family,
    Invoice,
    Location,
    MovementType,
    Payslip,
    PayslipAdjustment,
    Order,
    OrderLine,
    OrderStatus,
    Profile,
    Role,
    Series,
    SeriesProfile,
    Shift,
    ShiftCorrectionRequest,
    Shop,
    StockItem,
    StockMovement,
    Warehouse,
    validate_israeli_id,
)

User = get_user_model()


def as_drf_error(exc):
    """Convert a model-level ValidationError into DRF's shape.

    UserManager.create_user runs full_clean, and Django raises its own
    ValidationError. DRF only translates those when they come from a field
    validator, so one raised inside create() escapes as a 500 -- a duplicate ID
    number would give the desktop an HTML error page instead of a field error.
    """
    detail = getattr(exc, 'message_dict', None) or {'detail': exc.messages}

    # phone_normalised is an internal lookup column with no matching form
    # field, so its uniqueness error is reported against `phone` -- otherwise
    # the message cannot attach to any input the user can actually see.
    if 'phone_normalised' in detail:
        detail.pop('phone_normalised')
        detail['phone'] = [_('An account already exists for this phone number.')]

    return serializers.ValidationError(detail)


class PasswordRulesMixin:
    """Applies the password rules unless RELAXED_AUTH is on.

    Both the flag and the validator list are read at request time. Relying on
    AUTH_PASSWORD_VALIDATORS instead would silently pass everything whenever
    RELAXED_AUTH was on at import, even if the flag were later overridden —
    the two settings would drift apart and the strict path would stop being
    tested at all.
    """

    def validate_password(self, value):
        if getattr(settings, 'RELAXED_AUTH', False):
            return value

        django_validate_password(
            value,
            password_validators=get_password_validators(
                settings.STRICT_PASSWORD_VALIDATORS
            ),
        )
        return value


class UserSerializer(serializers.ModelSerializer):
    """A user as the apps see them. Read-only on identity fields."""

    full_name = serializers.CharField(read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'id_number', 'first_name', 'last_name', 'full_name',
            'email', 'phone', 'date_of_birth', 'address',
            'role', 'role_display', 'language',
            'hired_on', 'emergency_contact', 'emergency_phone',
            'pay_basis', 'overtime_enabled',
            'client', 'is_active', 'date_joined', 'must_change_password',
        ]
        read_only_fields = [
            'id', 'id_number', 'role', 'client', 'is_active', 'date_joined',
            'must_change_password', 'pay_basis', 'overtime_enabled',
        ]


class ClientSerializer(serializers.ModelSerializer):
    """A client company's own details, as shown in the client app."""

    business_type_display = serializers.CharField(
        source='get_business_type_display', read_only=True
    )

    class Meta:
        model = Client
        fields = [
            'id', 'name', 'legal_name', 'business_type', 'business_type_display',
            'tax_id', 'business_number',
            'contact_name', 'phone', 'email', 'website',
            'address', 'city', 'postal_code', 'delivery_address',
            'price_tier', 'credit_limit', 'is_active', 'created_at',
        ]
        read_only_fields = ['id', 'price_tier', 'credit_limit', 'is_active', 'created_at']


class LoginSerializer(TokenObtainPairSerializer):
    """JWT login for both staff and clients.

    Staff sign in with an ID number, clients with a phone number. Both arrive
    as `identifier`; UserManager.get_by_natural_key works out which it is, so
    one endpoint serves all three apps. `id_number` and `phone` are still
    accepted as aliases so each app can label its own field naturally.
    """

    username_field = User.USERNAME_FIELD

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[self.username_field].required = False
        self.fields['identifier'] = serializers.CharField(required=False)
        self.fields['phone'] = serializers.CharField(required=False)

    def validate(self, attrs):
        identifier = (
            attrs.pop('identifier', None)
            or attrs.pop('phone', None)
            or attrs.get(self.username_field)
        )
        if not identifier:
            raise serializers.ValidationError(
                {'identifier': _('Enter your ID number or phone number.')}
            )

        attrs.pop('phone', None)
        attrs[self.username_field] = str(identifier).strip()

        data = super().validate(attrs)
        data['user'] = UserSerializer(self.user).data
        if self.user.client_id:
            data['client'] = ClientSerializer(self.user.client).data
        return data

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Claims the mobile apps read without a round trip.
        token['role'] = user.role
        token['full_name'] = user.full_name
        token['language'] = user.language
        if user.client_id:
            token['client_id'] = user.client_id
        return token


class ChangePasswordSerializer(PasswordRulesMixin, serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError(_('Current password is incorrect.'))
        return value

    def save(self, **kwargs):
        user = self.context['request'].user
        user.set_password(self.validated_data['password'])
        # Clearing the flag here is what lifts the global block on the rest of
        # the API for a user created by a manager.
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])
        return user


class StaffAdminSerializer(serializers.ModelSerializer):
    """Manager-only view of a user: role and activation are writable here."""

    full_name = serializers.CharField(read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    client_name = serializers.CharField(source='client.name', read_only=True, default=None)

    class Meta:
        model = User
        fields = [
            'id', 'id_number', 'first_name', 'last_name', 'full_name',
            'role_display', 'client_name',
            'email', 'phone', 'date_of_birth', 'address',
            'role', 'language', 'hired_on',
            'emergency_contact', 'emergency_phone',
            'pay_basis', 'hourly_rate', 'daily_rate', 'monthly_salary',
            'overtime_enabled', 'daily_regular_hours',
            'client', 'is_active', 'date_joined', 'must_change_password',
        ]
        read_only_fields = ['id', 'id_number', 'date_joined', 'must_change_password']


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class FamilySerializer(serializers.ModelSerializer):
    series_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Family
        fields = ['id', 'name', 'name_en', 'slug', 'description', 'series_count']


class SeriesSerializer(serializers.ModelSerializer):
    family_name = serializers.CharField(source='family.name', read_only=True, default=None)
    profile_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Series
        fields = [
            'id', 'code', 'name', 'name_en', 'family', 'family_name',
            'manufacturer', 'catalog_page', 'price_per_kg', 'is_active',
            'profile_count',
        ]


class ProfileSerializer(serializers.ModelSerializer):
    """A physical extrusion, with the series it appears in."""

    series_codes = serializers.SerializerMethodField()
    weight_kg_per_m = serializers.FloatField(read_only=True)

    class Meta:
        model = Profile
        fields = [
            'id', 'number', 'description', 'description_en',
            'weight_g_per_m', 'weight_kg_per_m', 'section_image',
            'series_codes', 'is_active',
        ]

    def get_series_codes(self, obj):
        return sorted(s.code for s in obj.series.all())


class SeriesProfileSerializer(serializers.ModelSerializer):
    """A catalog row: this profile, as listed under this series.

    Flattened rather than nested, because this is what the catalog browser
    renders as a table and every extra nesting level is another loop in the UI.
    """

    number = serializers.CharField(source='profile.number', read_only=True)
    description = serializers.CharField(source='display_description', read_only=True)
    weight_g_per_m = serializers.IntegerField(
        source='effective_weight_g_per_m', read_only=True
    )
    section_image = serializers.ImageField(source='profile.section_image', read_only=True)
    series_code = serializers.CharField(source='series.code', read_only=True)
    series_name = serializers.CharField(source='series.name', read_only=True)
    family_name = serializers.CharField(
        source='series.family.name', read_only=True, default=None
    )
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    # Metal price for one metre, derived from the series' price/kg and this
    # row's weight. Null when either is unset; the browser shows a dash.
    price_per_m = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )
    price_per_kg = serializers.DecimalField(
        source='series.price_per_kg', max_digits=10, decimal_places=2,
        read_only=True,
    )

    class Meta:
        model = SeriesProfile
        fields = [
            'id', 'number', 'description', 'weight_g_per_m', 'section_image',
            'series', 'series_code', 'series_name', 'family_name',
            'group_header', 'role', 'role_display',
            'glass_min_mm', 'glass_max_mm', 'track_count', 'catalog_page',
            'price_per_kg', 'price_per_m',
        ]


# ---------------------------------------------------------------------------
# Manager-side user creation
# ---------------------------------------------------------------------------


class StaffCreateSerializer(PasswordRulesMixin, serializers.ModelSerializer):
    """A manager creates a worker (or another office user) from the desktop.

    The manager picks a starting password and tells the person directly, so no
    email or SMS delivery is needed. `must_change_password` is set, which locks
    the account out of everything except changing it — otherwise the manager
    would know every worker's password indefinitely.
    """

    id_number = serializers.CharField(required=True, validators=[validate_israeli_id])
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})
    role = serializers.ChoiceField(choices=[
        (value, label) for value, label in Role.choices if value != Role.CLIENT
    ])

    class Meta:
        model = User
        fields = [
            'id', 'id_number', 'first_name', 'last_name', 'email', 'phone',
            'date_of_birth', 'address', 'language', 'role',
            'hired_on', 'emergency_contact', 'emergency_phone',
            'pay_basis', 'hourly_rate', 'daily_rate', 'monthly_salary',
            'overtime_enabled', 'daily_regular_hours',
            'password',
        ]
        read_only_fields = ['id']

    def create(self, validated_data):
        password = validated_data.pop('password')
        request = self.context.get('request')
        try:
            return User.objects.create_user(
                password=password,
                must_change_password=True,
                created_by=request.user if request else None,
                **validated_data,
            )
        except DjangoValidationError as exc:
            raise as_drf_error(exc) from exc


class ClientContactCreateSerializer(PasswordRulesMixin, serializers.ModelSerializer):
    """A manager creates a client's contact person. Signs in by phone."""

    password = serializers.CharField(write_only=True, style={'input_type': 'password'})
    client = serializers.PrimaryKeyRelatedField(queryset=Client.objects.all())
    # Off by default, so a manually created contact still has to replace the
    # password. The client editor sets it False on purpose: a client's password
    # is their phone number and stays that way until they change it themselves.
    must_change_password = serializers.BooleanField(default=True, write_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'first_name', 'last_name', 'email', 'phone',
            'language', 'client', 'is_active', 'password', 'must_change_password',
        ]
        read_only_fields = ['id']

    def validate_phone(self, value):
        from core.models import normalise_phone

        if User.objects.filter(phone_normalised=normalise_phone(value)).exists():
            raise serializers.ValidationError(
                _('An account already exists for this phone number.')
            )
        return value

    def create(self, validated_data):
        password = validated_data.pop('password')
        phone = validated_data.pop('phone')
        must_change = validated_data.pop('must_change_password', True)
        request = self.context.get('request')
        try:
            return User.objects.create_client_user(
                phone=phone,
                password=password,
                must_change_password=must_change,
                created_by=request.user if request else None,
                **validated_data,
            )
        except DjangoValidationError as exc:
            raise as_drf_error(exc) from exc


class ClientAdminSerializer(serializers.ModelSerializer):
    """Full client record, editable by office and managers.

    A pasted map link sets the coordinates. The driver's Waze button navigates
    to the coordinate rather than the text address, so a link is the difference
    between arriving at the yard gate and arriving at the street.
    """

    business_type_display = serializers.CharField(
        source='get_business_type_display', read_only=True
    )
    contact_count = serializers.IntegerField(read_only=True)
    tier_discount_percent = serializers.DecimalField(
        source='price_tier.discount_percent', max_digits=5, decimal_places=2,
        read_only=True, default=None)

    class Meta:
        model = Client
        fields = [
            'id', 'name', 'legal_name', 'business_type', 'business_type_display',
            'tax_id', 'business_number',
            'contact_name', 'phone', 'email', 'website',
            'address', 'city', 'postal_code', 'delivery_address', 'notes',
            'location_url', 'latitude', 'longitude',
            'price_tier', 'tier_discount_percent', 'credit_limit',
            'is_active', 'created_at', 'contact_count',
        ]
        read_only_fields = ['id', 'created_at', 'contact_count']

    def validate(self, attrs):
        """Turn a pasted map link into coordinates, or say why it cannot.

        A link that parses to nothing is rejected rather than stored quietly:
        silently keeping it would leave the office believing the delivery point
        was set while the driver still gets sent to the street address.
        """
        if 'location_url' not in attrs:
            return attrs

        link = (attrs.get('location_url') or '').strip()
        if not link:
            # Clearing the link clears the point it set; a coordinate typed in
            # the same request still wins, so the pin stays draggable.
            attrs['location_url'] = ''
            if 'latitude' not in attrs:
                attrs['latitude'] = None
            if 'longitude' not in attrs:
                attrs['longitude'] = None
            return attrs

        point = maplinks.extract_coordinates(link)
        if point is None:
            raise serializers.ValidationError({'location_url': _(
                'That link does not contain a location. Open the place in '
                'Google Maps, Waze or Apple Maps, share it, and paste the '
                'link that gives you.'
            )})
        attrs['latitude'], attrs['longitude'] = point
        return attrs


class ShopSerializer(serializers.ModelSerializer):
    """The owner's own business location."""

    # Read-only URL of the logo. Uploading/clearing goes through the dedicated
    # POST/DELETE on ShopView, so a normal company-details save can never wipe it.
    logo = serializers.SerializerMethodField()

    class Meta:
        model = Shop
        fields = [
            'id', 'name', 'legal_name', 'tax_id', 'address', 'city',
            'phone', 'email', 'latitude', 'longitude', 'logo',
        ]
        read_only_fields = ['id', 'logo']

    def get_logo(self, obj):
        if not obj.logo:
            return None
        request = self.context.get('request')
        url = obj.logo.url
        return request.build_absolute_uri(url) if request else url


class SettingsSerializer(serializers.ModelSerializer):
    """App configuration (Cloudinary) for the Settings page.

    The secret is never sent back in full -- reads return whether it is set and
    a masked hint. Writes accept a new secret, or leave it untouched when the
    field is omitted (so re-saving the form doesn't wipe it).
    """

    # Secrets are write-only; reads report only whether each is set, never the
    # value. An omitted secret keeps the stored one (see update()).
    SECRET_FIELDS = ['cloudinary_api_secret', 'openai_api_key',
                     'smtp_password', 'greeninvoice_api_secret']

    cloudinary_api_secret = serializers.CharField(
        write_only=True, required=False, allow_blank=True)
    openai_api_key = serializers.CharField(
        write_only=True, required=False, allow_blank=True)
    smtp_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True)
    greeninvoice_api_secret = serializers.CharField(
        write_only=True, required=False, allow_blank=True)

    cloudinary_secret_set = serializers.SerializerMethodField()
    openai_key_set = serializers.SerializerMethodField()
    smtp_password_set = serializers.SerializerMethodField()
    greeninvoice_secret_set = serializers.SerializerMethodField()

    cloudinary_ready = serializers.BooleanField(read_only=True)
    smtp_ready = serializers.BooleanField(read_only=True)
    openai_ready = serializers.BooleanField(read_only=True)
    greeninvoice_ready = serializers.BooleanField(read_only=True)
    storage_backend = serializers.SerializerMethodField()

    # True when the value is supplied by a Railway env var (so the UI shows it
    # as managed outside the app and doesn't offer to overwrite it).
    openai_from_env = serializers.SerializerMethodField()
    smtp_from_env = serializers.SerializerMethodField()
    greeninvoice_from_env = serializers.SerializerMethodField()

    class Meta:
        model = AppConfig
        fields = [
            'id',
            'cloudinary_cloud_name', 'cloudinary_api_key',
            'cloudinary_api_secret', 'cloudinary_secret_set',
            'cloudinary_ready', 'storage_backend',
            'openai_api_key', 'openai_key_set', 'openai_ready', 'openai_from_env',
            'smtp_host', 'smtp_port', 'smtp_user', 'smtp_password',
            'smtp_password_set', 'smtp_from', 'smtp_use_tls', 'smtp_ready',
            'smtp_from_env',
            'accountant_name', 'accountant_email', 'accountant_phone',
            'greeninvoice_api_key', 'greeninvoice_api_secret',
            'greeninvoice_secret_set', 'greeninvoice_ready',
            'greeninvoice_from_env',
        ]
        read_only_fields = ['id']

    def get_cloudinary_secret_set(self, obj):
        return bool(obj.setting('cloudinary_api_secret'))

    def get_openai_key_set(self, obj):
        return bool(obj.setting('openai_api_key'))

    def get_smtp_password_set(self, obj):
        return bool(obj.setting('smtp_password'))

    def get_greeninvoice_secret_set(self, obj):
        return bool(obj.setting('greeninvoice_api_secret'))

    def get_openai_from_env(self, obj):
        return obj.from_env('openai_api_key')

    def get_smtp_from_env(self, obj):
        return obj.from_env('smtp_host')

    def get_greeninvoice_from_env(self, obj):
        return obj.from_env('greeninvoice_api_key')

    def get_storage_backend(self, obj):
        return 'cloudinary' if obj.cloudinary_ready else 'local'

    def update(self, instance, validated_data):
        # An omitted secret keeps the current one; a blank secret is a real
        # request to clear it, so only skip when the key is absent entirely.
        for field in self.SECRET_FIELDS:
            if field not in self.initial_data:
                validated_data.pop(field, None)
        return super().update(instance, validated_data)


class InvoiceSerializer(serializers.ModelSerializer):
    """An income or expense invoice. The file (PDF/photo) uploads via multipart."""

    client_name = serializers.CharField(source='client.name', read_only=True)
    order_number = serializers.CharField(source='order.number', read_only=True)
    direction_display = serializers.CharField(
        source='get_direction_display', read_only=True)
    source_display = serializers.CharField(source='get_source_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    balance_due = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'direction', 'direction_display', 'number',
            'client', 'client_name', 'order', 'order_number',
            'party_name', 'party_tax_id',
            'issued_at', 'category', 'subtotal', 'vat', 'total',
            'amount_paid', 'balance_due',
            'source', 'source_display', 'status', 'status_display',
            'allocation_number', 'external_id', 'file', 'file_url',
            'notes', 'created_at',
        ]
        read_only_fields = [
            'id', 'created_at', 'file_url', 'allocation_number', 'external_id',
            'balance_due',
            # Status is derived from amount_paid, so it's read-only here.
            'status', 'status_display',
        ]
        extra_kwargs = {'file': {'write_only': True, 'required': False}}

    def get_file_url(self, obj):
        if not obj.file:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url

    def create(self, validated_data):
        invoice = Invoice(**validated_data)
        invoice.sync_status()
        invoice.save()
        return invoice

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.sync_status()
        instance.save()
        return instance


class WarehouseSerializer(serializers.ModelSerializer):
    location_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Warehouse
        fields = [
            'id', 'name', 'address', 'city', 'latitude', 'longitude',
            'is_active', 'location_count',
        ]
        read_only_fields = ['id', 'location_count']


class LocationSerializer(serializers.ModelSerializer):
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)

    class Meta:
        model = Location
        fields = ['id', 'warehouse', 'warehouse_name', 'code', 'barcode', 'description']
        read_only_fields = ['id', 'warehouse_name']


class StockItemCreateSerializer(serializers.ModelSerializer):
    """Add a new holding, optionally with an opening quantity.

    The profile is given by its number (what the warehouse types), not its id.
    """

    profile = serializers.SlugRelatedField(
        slug_field='number', queryset=Profile.objects.all())
    initial_quantity = serializers.IntegerField(
        write_only=True, required=False, default=0, min_value=0)

    class Meta:
        model = StockItem
        fields = [
            'id', 'profile', 'location', 'length_mm', 'finish',
            'minimum_quantity', 'initial_quantity',
        ]
        read_only_fields = ['id']

    def create(self, validated_data):
        initial = validated_data.pop('initial_quantity', 0)
        item = StockItem.objects.create(**validated_data)
        if initial:
            request = self.context.get('request')
            StockMovement.objects.create(
                stock_item=item, movement_type=MovementType.RECEIPT,
                quantity=initial, note='Opening stock',
                performed_by=request.user if request else None)
        return item


class StockMovementCreateSerializer(serializers.Serializer):
    """One stock movement. The sign is derived from the type, so callers send a
    plain positive count for a receipt or a pick; only an adjustment is signed."""

    movement_type = serializers.ChoiceField(choices=MovementType.choices)
    quantity = serializers.IntegerField()
    note = serializers.CharField(required=False, allow_blank=True, default='')

    _NEGATIVE = {MovementType.PICK, MovementType.TRANSFER_OUT, MovementType.SCRAP}

    def validate(self, attrs):
        mtype, qty = attrs['movement_type'], attrs['quantity']
        if mtype == MovementType.ADJUSTMENT:
            attrs['signed'] = qty                     # adjustments may be + or -
        else:
            if qty <= 0:
                raise serializers.ValidationError(
                    {'quantity': _('Enter a quantity greater than zero.')})
            attrs['signed'] = -abs(qty) if mtype in self._NEGATIVE else abs(qty)
        return attrs


class StockItemSerializer(serializers.ModelSerializer):
    """A stock line: a profile, in a finish and length, at a location.

    Mirrors the catalog row (image, number, description, series) and adds the two
    things stock is about -- the finish (colour) and the amount on hand. The
    quantity comes from a `qty` annotation on the queryset, not the per-row
    property, so the whole page is one query.
    """

    number = serializers.CharField(source='profile.number', read_only=True)
    description = serializers.CharField(source='profile.description', read_only=True)
    section_image = serializers.ImageField(source='profile.section_image', read_only=True)
    weight_g_per_m = serializers.IntegerField(
        source='profile.weight_g_per_m', read_only=True)
    series_codes = serializers.SerializerMethodField()
    warehouse = serializers.CharField(source='location.warehouse.name', read_only=True)
    warehouse_id = serializers.IntegerField(source='location.warehouse_id', read_only=True)
    location_code = serializers.CharField(source='location.code', read_only=True)
    quantity = serializers.IntegerField(source='qty', read_only=True)
    needs_reorder = serializers.SerializerMethodField()
    types = serializers.SerializerMethodField()

    class Meta:
        model = StockItem
        fields = [
            'id', 'number', 'description', 'section_image', 'weight_g_per_m',
            'series_codes', 'types', 'finish', 'length_mm', 'quantity',
            'minimum_quantity', 'needs_reorder',
            'warehouse', 'warehouse_id', 'location_code',
        ]

    def get_series_codes(self, obj):
        return sorted(s.code for s in obj.profile.series.all())

    def get_types(self, obj):
        from core.models import ProfileRole
        labels = dict(ProfileRole.choices)
        values = sorted({sp.role for sp in obj.profile.series_profiles.all() if sp.role})
        return [{'value': v, 'label': str(labels.get(v, v))} for v in values]

    def get_needs_reorder(self, obj):
        return (getattr(obj, 'qty', 0) or 0) < obj.minimum_quantity


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class OrderLineSerializer(serializers.ModelSerializer):
    """A line as the app sends and reads it.

    Input carries the profile number and either a total length or a bar count;
    weight and price are worked out server-side (weight from the profile, price
    per kilo from the series) unless the caller overrides them. Output adds the
    read-only figures the table and PDF show.
    """

    profile = serializers.SlugRelatedField(
        slug_field='number', queryset=Profile.objects.all())
    series = serializers.SlugRelatedField(
        slug_field='code', queryset=Series.objects.all(), required=False, allow_null=True)

    number = serializers.CharField(source='profile.number', read_only=True)
    description = serializers.CharField(source='profile.description', read_only=True)
    weight_g_per_m = serializers.IntegerField(
        source='profile.weight_g_per_m', read_only=True)
    computed_weight_kg = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True)
    effective_weight_kg = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True)
    line_total = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    bars_needed = serializers.IntegerField(read_only=True)

    class Meta:
        model = OrderLine
        fields = [
            'id', 'profile', 'number', 'description', 'series',
            'weight_g_per_m', 'length_mm', 'quantity', 'total_length_m',
            'weight_kg_override', 'computed_weight_kg', 'effective_weight_kg',
            'price_per_kg', 'line_total', 'bars_needed', 'prepared',
            'shortage_note', 'delivered_length_m',
        ]
        read_only_fields = ['id']

    def validate(self, attrs):
        # Fill total_length_m from bars x length when only the bar detail is given.
        length_mm = attrs.get('length_mm')
        quantity = attrs.get('quantity')
        if not attrs.get('total_length_m'):
            if length_mm and quantity:
                attrs['total_length_m'] = (Decimal(length_mm) * quantity /
                                           Decimal(1000)).quantize(Decimal('0.01'))
            else:
                raise serializers.ValidationError(
                    {'total_length_m': _('Enter a total length, or bars and a bar length.')})
        # Default the price per kilo from the line's series (or the profile's
        # first priced series) when the caller did not set one.
        if not attrs.get('price_per_kg'):
            attrs['price_per_kg'] = self._series_price(attrs)
        return attrs

    @staticmethod
    def _series_price(attrs):
        series = attrs.get('series')
        if series and series.price_per_kg is not None:
            return series.price_per_kg
        profile = attrs.get('profile')
        if profile:
            priced = (profile.series.exclude(price_per_kg__isnull=True)
                      .order_by('code').first())
            if priced:
                return priced.price_per_kg
        return Decimal('0.00')


class OrderSerializer(serializers.ModelSerializer):
    """An order with its lines and computed money totals."""

    lines = OrderLineSerializer(many=True)
    client_name = serializers.CharField(source='client.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True)

    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    net = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    vat_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    total_weight_kg = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    invoiced_total = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    remaining_to_invoice = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    is_fully_invoiced = serializers.BooleanField(read_only=True)

    class Meta:
        model = Order
        fields = [
            'id', 'number', 'client', 'client_name', 'status', 'status_display',
            'ordered_at', 'required_by', 'notes',
            'discount_percent', 'vat_percent',
            'subtotal', 'discount_amount', 'net', 'vat_amount', 'total',
            'total_weight_kg', 'created_by_name', 'lines',
            'invoiced_total', 'remaining_to_invoice', 'is_fully_invoiced',
        ]
        read_only_fields = ['id', 'number', 'ordered_at', 'created_by_name']

    def create(self, validated_data):
        lines = validated_data.pop('lines', [])
        request = self.context.get('request')
        client = validated_data['client']
        # Snapshot the discount from the client's tier at creation time.
        if 'discount_percent' not in validated_data:
            tier = client.price_tier
            validated_data['discount_percent'] = (
                tier.discount_percent if tier else Decimal('0.00'))
        order = Order.objects.create(
            number=self._next_number(),
            created_by=request.user if request else None,
            **validated_data,
        )
        for line in lines:
            OrderLine.objects.create(order=order, **line)
        return order

    def update(self, instance, validated_data):
        lines = validated_data.pop('lines', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if lines is not None:
            # Lines are replaced wholesale: the editor sends the full set each
            # save, so reconciling individual rows would be needless complexity.
            instance.lines.all().delete()
            for line in lines:
                OrderLine.objects.create(order=instance, **line)
        return instance

    @staticmethod
    def _next_number():
        from django.utils import timezone
        year = timezone.now().year
        prefix = f'ORD-{year}-'
        last = (Order.objects.filter(number__startswith=prefix)
                .order_by('-number').first())
        seq = (int(last.number.rsplit('-', 1)[1]) + 1) if last else 1
        return f'{prefix}{seq:04d}'


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


class ShiftSerializer(serializers.ModelSerializer):
    """A work session with its computed duration."""

    worker_name = serializers.CharField(source='worker.full_name', read_only=True)
    is_open = serializers.BooleanField(read_only=True)
    duration_minutes = serializers.IntegerField(read_only=True)
    hours = serializers.FloatField(read_only=True)

    class Meta:
        model = Shift
        fields = [
            'id', 'worker', 'worker_name', 'clock_in', 'clock_out',
            'is_open', 'duration_minutes', 'hours', 'note',
        ]
        read_only_fields = ['id', 'worker', 'worker_name']


class ShiftCorrectionRequestSerializer(serializers.ModelSerializer):
    """A worker's clock-fix request; the review fields are read-only to workers."""

    worker_name = serializers.CharField(source='worker.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    reviewed_by_name = serializers.CharField(
        source='reviewed_by.full_name', read_only=True)

    class Meta:
        model = ShiftCorrectionRequest
        fields = [
            'id', 'worker', 'worker_name', 'shift', 'work_date',
            'requested_clock_in', 'requested_clock_out', 'reason',
            'status', 'status_display', 'reviewed_by', 'reviewed_by_name',
            'reviewed_at', 'review_note', 'created_at',
        ]
        read_only_fields = [
            'id', 'worker', 'worker_name', 'status', 'status_display',
            'reviewed_by', 'reviewed_by_name', 'reviewed_at', 'review_note',
            'created_at',
        ]

    def validate(self, attrs):
        if not attrs.get('requested_clock_in') and not attrs.get('requested_clock_out'):
            raise serializers.ValidationError(
                _('Give a new clock-in or clock-out time.'))
        return attrs


class PayslipAdjustmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayslipAdjustment
        fields = ['id', 'label', 'amount']
        read_only_fields = ['id']


class PayslipSerializer(serializers.ModelSerializer):
    """A payslip with its adjustment lines and computed totals."""

    adjustments = PayslipAdjustmentSerializer(many=True, required=False)
    worker_name = serializers.CharField(source='worker.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    source_display = serializers.CharField(source='get_source_display', read_only=True)
    adjustments_total = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    total_pay = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    total_hours = serializers.DecimalField(
        max_digits=8, decimal_places=2, read_only=True)
    is_final = serializers.BooleanField(read_only=True)

    class Meta:
        model = Payslip
        fields = [
            'id', 'worker', 'worker_name', 'year', 'month',
            'status', 'status_display', 'source', 'source_display',
            'pay_basis', 'overtime_enabled', 'days_worked',
            'regular_hours', 'overtime_125_hours', 'overtime_150_hours',
            'total_hours', 'hourly_rate', 'base_pay', 'overtime_pay',
            'adjustments', 'adjustments_total', 'total_pay',
            'note', 'is_final', 'created_at', 'finalised_at',
        ]
        read_only_fields = ['id', 'worker_name', 'created_at', 'finalised_at']

    def _write_adjustments(self, payslip, adjustments):
        payslip.adjustments.all().delete()
        for adj in adjustments:
            PayslipAdjustment.objects.create(payslip=payslip, **adj)

    def create(self, validated_data):
        adjustments = validated_data.pop('adjustments', [])
        request = self.context.get('request')
        payslip = Payslip.objects.create(
            created_by=request.user if request else None, **validated_data)
        self._write_adjustments(payslip, adjustments)
        return payslip

    def update(self, instance, validated_data):
        if instance.is_final:
            raise serializers.ValidationError(
                _('This payslip is finalised. Reopen it to edit.'))
        adjustments = validated_data.pop('adjustments', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if adjustments is not None:
            self._write_adjustments(instance, adjustments)
        return instance
