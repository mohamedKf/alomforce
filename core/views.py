
"""AlomForce — all API views.

Single views module shared by every client. Role decides what the API returns,
not which app the request came from.

Sections:
    1. Permissions
    2. Authentication and users
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext_lazy as _
from rest_framework import generics, permissions, renderers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from core.permissions import PasswordChangeRequired
from core.models import (
    Client,
    Family,
    Invoice,
    Location,
    MovementType,
    Order,
    Payslip,
    Profile,
    ProfileRole,
    Role,
    Series,
    SeriesProfile,
    Shift,
    ShiftCorrectionRequest,
    Shop,
    StockItem,
    StockMovement,
    Warehouse,
)
from core.serializers import (
    ChangePasswordSerializer,
    ClientAdminSerializer,
    ClientContactCreateSerializer,
    FamilySerializer,
    InvoiceSerializer,
    LoginSerializer,
    ProfileSerializer,
    LocationSerializer,
    OrderSerializer,
    PayslipSerializer,
    SeriesProfileSerializer,
    SeriesSerializer,
    SettingsSerializer,
    ShiftCorrectionRequestSerializer,
    ShiftSerializer,
    ShopSerializer,
    StaffAdminSerializer,
    StaffCreateSerializer,
    StockItemCreateSerializer,
    StockItemSerializer,
    StockMovementCreateSerializer,
    UserSerializer,
    WarehouseSerializer,
)

User = get_user_model()


class BinaryFileRenderer(renderers.BaseRenderer):
    """Lets PDF endpoints accept any Accept header (incl. application/pdf).

    The desktop downloads PDFs with `Accept: application/pdf`; without a renderer
    that advertises that type, DRF's content negotiation rejects the request with
    406 before the view runs. media_type '*/*' accepts everything. The PDF views
    return a raw HttpResponse, so render() below is never actually invoked.
    """

    media_type = '*/*'
    format = 'bin'
    charset = None
    render_style = 'binary'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


def store_pdf(instance, field_name, filename, data):
    """Save PDF bytes onto a model FileField, replacing any earlier copy.

    Writes through Django's default storage -- Cloudinary when the app is
    configured for it, the local media folder otherwise. Best-effort: a storage
    failure must not stop the PDF being served, so callers wrap this in try.
    """
    from django.core.files.base import ContentFile

    field = getattr(instance, field_name)
    if field:
        field.delete(save=False)          # don't pile up old copies in storage
    field.save(filename, ContentFile(data), save=False)
    instance.save(update_fields=[field_name])


# DRF's permission_classes REPLACES DEFAULT_PERMISSION_CLASSES rather than
# adding to it, so any view declaring its own permissions would silently skip
# the password gate. Every view builds its list from this one instead.
BASE = [permissions.IsAuthenticated, PasswordChangeRequired]


# ---------------------------------------------------------------------------
# 1. Permissions
# ---------------------------------------------------------------------------


class IsManager(permissions.BasePermission):
    """Only managers may assign roles or activate accounts."""

    message = 'Only managers can perform this action.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and request.user.role == Role.MANAGER)


class IsStaff(permissions.BasePermission):
    """Any employee — excludes client contacts."""

    message = 'Staff access only.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and request.user.is_staff_role)


class IsOffice(permissions.BasePermission):
    """Office and managers — the people who deal with clients and paperwork."""

    message = 'Office access only.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and request.user.role in {Role.OFFICE, Role.MANAGER})


class IsStockStaff(permissions.BasePermission):
    """The people who move stock and prepare orders — warehouse workers,
    drivers (who are also stock workers), office and managers."""

    message = 'Stock access only.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and request.user.role in
                    {Role.WAREHOUSE, Role.DRIVER, Role.OFFICE, Role.MANAGER})


class IsDeliveryStaff(permissions.BasePermission):
    """Drivers, office and managers — the people who run and track deliveries."""

    message = 'Delivery access only.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and request.user.role in
                    {Role.DRIVER, Role.OFFICE, Role.MANAGER})


# ---------------------------------------------------------------------------
# 2. Authentication and users
# ---------------------------------------------------------------------------


class LoginView(TokenObtainPairView):
    """POST /api/auth/login/ — {id_number, password} → access + refresh + user."""

    serializer_class = LoginSerializer
    permission_classes = [permissions.AllowAny]


class MeView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /api/auth/me/ — the signed-in user's own profile."""

    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]
    allows_pending_password = True

    def get_object(self):
        return self.request.user


class ChangePasswordView(APIView):
    """POST /api/auth/change-password/"""

    permission_classes = [permissions.IsAuthenticated]
    allows_pending_password = True

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'detail': 'Password updated.'}, status=status.HTTP_200_OK)


class StaffViewSet(viewsets.ModelViewSet):
    """Manager-only user administration.

    POST creates a worker; POST to /staff/contacts/ creates a client contact.
    Both start with a manager-set password the user must replace on first use.
    """

    permission_classes = BASE + [IsManager]
    queryset = User.objects.all()

    def get_serializer_class(self):
        if self.action == 'create':
            return StaffCreateSerializer
        if self.action == 'contacts':
            return ClientContactCreateSerializer
        return StaffAdminSerializer

    def get_queryset(self):
        qs = super().get_queryset().select_related('client')
        params = self.request.query_params
        if role := params.get('role'):
            qs = qs.filter(role=role)
        if params.get('pending') == 'true':
            qs = qs.filter(is_active=False)
        if client := params.get('client'):
            qs = qs.filter(client_id=client)
        if search := params.get('search'):
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(id_number__icontains=search)
                | Q(phone__icontains=search)
            )
        return qs.order_by('last_name', 'first_name')

    def perform_destroy(self, instance):
        """Deactivate rather than delete.

        A worker is referenced by every stock movement they ever made and every
        order they created; deleting the row would either fail on the protected
        foreign keys or take the history with it.
        """
        instance.is_active = False
        instance.save(update_fields=['is_active'])

    @action(detail=False, methods=['post'])
    def contacts(self, request):
        """POST /api/staff/contacts/ — create a client's contact person."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            StaffAdminSerializer(user).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['post'])
    def reset_password(self, request, pk=None):
        """POST /api/staff/<id>/reset-password/ — manager sets a new starting password."""
        user = self.get_object()
        password = request.data.get('password')
        if not password:
            return Response({'password': ['This field is required.']},
                            status=status.HTTP_400_BAD_REQUEST)
        user.set_password(password)
        user.must_change_password = True
        user.save(update_fields=['password', 'must_change_password'])
        return Response({'detail': 'Password reset. The user must change it on next sign-in.'})


class ClientViewSet(viewsets.ModelViewSet):
    """Client companies. Office and managers only — never client users."""

    serializer_class = ClientAdminSerializer
    queryset = Client.objects.all()

    def get_permissions(self):
        # Warehouse workers and drivers may look up and add clients (so they can
        # create an order at the counter); editing and deleting stay office-only.
        if self.action in ('list', 'retrieve', 'create'):
            return [p() for p in BASE + [IsStockStaff]]
        return [p() for p in BASE + [IsOffice]]

    def get_queryset(self):
        qs = super().get_queryset().annotate(contact_count=Count('users'))
        params = self.request.query_params
        if search := params.get('search'):
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(tax_id__icontains=search)
                | Q(contact_name__icontains=search)
                | Q(phone__icontains=search)
            )
        if params.get('active') == 'true':
            qs = qs.filter(is_active=True)
        return qs.order_by('name')

    def perform_destroy(self, instance):
        """Deactivate: clients are referenced by orders and invoices."""
        instance.is_active = False
        instance.save(update_fields=['is_active'])

    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        """GET /api/clients/<id>/statement/ — the client's documents + totals.

        One row per document (an order note and a delivery note per order;
        invoices come later). Filter with ?type=order_note|delivery_note,
        ?month=1-12 and ?year=YYYY. The summary always reports the selected
        period's purchases and the all-time total, whatever the type filter.
        """
        client = self.get_object()
        params = request.query_params
        doc_type = params.get('type') or 'all'

        base = Order.objects.filter(client=client).prefetch_related('lines')
        # All-time totals (order.total is computed from the lines).
        grand = list(base)
        grand_total = sum((o.total for o in grand), Decimal('0.00'))

        period = base
        if year := params.get('year'):
            period = period.filter(ordered_at__year=int(year))
        if month := params.get('month'):
            period = period.filter(ordered_at__month=int(month))
        period = list(period.order_by('-ordered_at'))
        period_total = sum((o.total for o in period), Decimal('0.00'))

        # What the client still owes: their unpaid income invoices, all-time.
        outstanding = (
            Invoice.objects.filter(client=client,
                                   direction=Invoice.Direction.INCOME)
            .exclude(status=Invoice.Status.PAID)
            .aggregate(t=Coalesce(Sum('total'), Decimal('0.00')))['t'])

        documents = []
        for o in period:
            row = {
                'order_id': o.id, 'number': o.number,
                'date': o.ordered_at.date().isoformat(),
                'status': o.status, 'status_display': o.get_status_display(),
                'weight': str(o.total_weight_kg),
            }
            if doc_type in ('all', 'order_note'):
                documents.append({**row, 'type': 'order_note',
                                  'type_display': _('Order note'),
                                  'amount': str(o.total), 'signed': None})
            if doc_type in ('all', 'delivery_note'):
                documents.append({**row, 'type': 'delivery_note',
                                  'type_display': _('Delivery note'),
                                  'amount': None,
                                  'signed': bool(o.delivery_signed_at),
                                  'signed_at': (o.delivery_signed_at.isoformat()
                                                if o.delivery_signed_at else None)})

        return Response({
            'client': {'id': client.id, 'name': client.name},
            'period': {'year': int(year) if (year := params.get('year')) else None,
                       'month': int(month) if (month := params.get('month')) else None},
            'summary': {
                'period_total': str(period_total),
                'grand_total': str(grand_total),
                'period_orders': len(period),
                'grand_orders': len(grand),
                'outstanding': str(outstanding),
            },
            'documents': documents,
        })


# ---------------------------------------------------------------------------
# 3. Catalog
# ---------------------------------------------------------------------------


class FamilyViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/catalog/families/"""

    serializer_class = FamilySerializer
    permission_classes = BASE
    pagination_class = None
    queryset = Family.objects.annotate(series_count=Count('series')).order_by('name')


class SeriesViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/catalog/series/  — optional ?family=<id>&search=<text>"""

    serializer_class = SeriesSerializer
    permission_classes = BASE
    pagination_class = None
    lookup_field = 'code'

    def get_queryset(self):
        qs = (
            Series.objects.select_related('family')
            .annotate(profile_count=Count('series_profiles', distinct=True))
            .order_by('code')
        )
        params = self.request.query_params
        if family := params.get('family'):
            qs = qs.filter(family_id=family)
        if search := params.get('search'):
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        return qs

    @action(detail=True, methods=['patch'], permission_classes=BASE + [IsManager])
    def price(self, request, code=None):
        """PATCH /api/catalog/series/<code>/price/ — {price_per_kg: <num|null>}

        Only the metal price is writable; the rest of a series is imported, not
        edited here. Managers only, since it moves every price in the catalog.
        """
        series = self.get_object()
        raw = request.data.get('price_per_kg')
        if raw in (None, ''):
            series.price_per_kg = None
        else:
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                return Response(
                    {'price_per_kg': [_('Enter a valid price.')]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if value < 0:
                return Response(
                    {'price_per_kg': [_('Price cannot be negative.')]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            series.price_per_kg = value
        series.save(update_fields=['price_per_kg'])
        return Response(SeriesSerializer(series).data)


class ProfileViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/catalog/profiles/  — unique extrusions, not catalog rows.

    Use this when the question is "what is profile 04935" (one answer).
    Use /api/catalog/listings/ when the question is "what does series 7000
    contain" — a profile appears once per series it belongs to.
    """

    serializer_class = ProfileSerializer
    permission_classes = BASE
    lookup_field = 'number'

    def get_queryset(self):
        qs = Profile.objects.prefetch_related('series').order_by('number')
        params = self.request.query_params
        if search := params.get('search'):
            qs = qs.filter(
                Q(number__icontains=search) | Q(description__icontains=search)
            )
        if series := params.get('series'):
            qs = qs.filter(series__code=series)
        if params.get('active') != 'all':
            qs = qs.filter(is_active=True)
        return qs.distinct()

    @action(detail=True, methods=['post', 'delete'],
            permission_classes=BASE + [IsManager],
            parser_classes=[MultiPartParser, FormParser])
    def section_image(self, request, number=None):
        """POST/DELETE /api/catalog/profiles/<number>/section_image/

        POST with a multipart `image` sets the cross-section drawing; DELETE
        clears it. Managers only. The file lands wherever STORAGES points --
        local disk now, Cloudinary once its credentials are set -- so no code
        changes when the catalog moves to the cloud.
        """
        profile = self.get_object()

        if request.method == 'DELETE':
            profile.section_image.delete(save=False)
            profile.section_image = None
            profile.save(update_fields=['section_image'])
            return Response(status=status.HTTP_204_NO_CONTENT)

        image = request.FILES.get('image')
        if not image:
            return Response(
                {'image': [_('No image file was provided.')]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        profile.section_image = image
        profile.save(update_fields=['section_image'])
        return Response(
            ProfileSerializer(profile, context={'request': request}).data
        )


class CatalogListingViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/catalog/listings/ — the catalog rows the browser renders.

    Filters:
        ?series=7000        profiles listed under that series
        ?family=<id>        everything in a product line
        ?role=sash          frame / sash / track / mullion / glazing_bead / ...
        ?tracks=3           rails by track count
        ?glass=16           profiles that accept 16mm glass
        ?search=<text>      profile number or description
    """

    serializer_class = SeriesProfileSerializer
    permission_classes = BASE

    def get_queryset(self):
        qs = SeriesProfile.objects.select_related(
            'profile', 'series', 'series__family'
        ).order_by('series__code', 'position')

        params = self.request.query_params
        if series := params.get('series'):
            qs = qs.filter(series__code=series)
        if family := params.get('family'):
            qs = qs.filter(series__family_id=family)
        if role := params.get('role'):
            qs = qs.filter(role=role)
        if tracks := params.get('tracks'):
            qs = qs.filter(track_count=tracks)
        if search := params.get('search'):
            qs = qs.filter(
                Q(profile__number__icontains=search)
                | Q(profile__description__icontains=search)
                | Q(listed_description__icontains=search)
            )
        if glass := params.get('glass'):
            # A profile fits the glass if it has a stated maximum at or above
            # the thickness, and either no minimum or one at or below it.
            # Rows with no stated range are excluded rather than assumed to fit.
            qs = qs.filter(
                Q(glass_max_mm__gte=glass)
                & (Q(glass_min_mm__isnull=True) | Q(glass_min_mm__lte=glass))
            )
        return qs

    @action(detail=False, methods=['get'])
    def roles(self, request):
        """GET /api/catalog/listings/roles/ — role list with counts, for filter UI.

        With ?series=<code> the counts are scoped to that series, so the filter
        can offer only the roles that series actually contains -- picking a role
        the series has none of (a frame in a series with no frames) is then not
        an option rather than a query that silently returns nothing.
        """
        rows = SeriesProfile.objects.all()
        if series := request.query_params.get('series'):
            rows = rows.filter(series__code=series)
        counts = {
            row['role']: row['n']
            for row in rows.values('role').annotate(n=Count('id'))
        }
        return Response([
            {'value': value, 'label': label, 'count': counts.get(value, 0)}
            for value, label in ProfileRole.choices
        ])

    # Cap so an unfiltered "print everything" can't spin up 1500 QR images.
    QR_LABELS_MAX = 300

    @action(detail=False, methods=['get'])
    def qr_labels(self, request):
        """GET /api/catalog/listings/qr_labels/ — a printable QR-label sheet.

        Honours the same filters as the listing, so the office narrows to a
        series (or search) and prints just those labels. Each QR encodes the
        profile number; the phone app scans it to look the profile up.
        """
        from django.http import HttpResponse
        from core.qr_labels import render_qr_labels

        # Unique profiles in the current filter, in catalog order.
        seen, rows = set(), []
        for sp in self.get_queryset():
            number = sp.profile.number
            if number in seen:
                continue
            seen.add(number)
            rows.append((number, sp.display_description))
            if len(rows) >= self.QR_LABELS_MAX:
                break
        if not rows:
            return Response({'detail': _('No profiles match these filters.')},
                            status=status.HTTP_400_BAD_REQUEST)
        pdf = render_qr_labels(rows)
        response = HttpResponse(pdf, content_type='application/pdf')
        response['Content-Disposition'] = 'inline; filename="qr_labels.pdf"'
        return response


# ---------------------------------------------------------------------------
# 4. Dashboard
# ---------------------------------------------------------------------------


class DashboardView(APIView):
    """GET /api/dashboard/ — headline numbers for the office landing screen.

    Every tile reports what it can today; areas without a backend yet report
    `available: false` so the desktop can show "not set up" rather than a
    misleading zero. Staff only.
    """

    permission_classes = BASE + [IsStaff]

    def get(self, request):
        return Response({
            'workers': self._workers(),
            'clients': self._clients(),
            'catalog': self._catalog(),
            'stock': self._stock(),
            'orders': {'available': False},
        })

    def _stock(self):
        items = StockItem.objects.annotate(
            qty=Coalesce(Sum('movements__quantity'), 0))
        out = items.filter(qty__lte=0).count()
        low = items.filter(qty__gt=0, qty__lt=F('minimum_quantity')).count()
        return {
            'available': True,
            'items': items.count(),
            'low': low,
            'out': out,
            'alerts': low + out,
            'warehouses': Warehouse.objects.filter(is_active=True).count(),
        }

    def _workers(self):
        from core.models import ONLINE_WINDOW

        staff = User.objects.exclude(role=Role.CLIENT)
        online_since = timezone.now() - ONLINE_WINDOW
        by_role = {
            row['role']: row['n']
            for row in staff.filter(is_active=True).values('role').annotate(n=Count('id'))
        }
        return {
            'available': True,
            'total': staff.count(),
            'active': staff.filter(is_active=True).count(),
            'online': staff.filter(is_active=True, last_seen__gte=online_since).count(),
            'pending_password': staff.filter(is_active=True, must_change_password=True).count(),
            'by_role': [
                {'role': value, 'label': str(label), 'count': by_role.get(value, 0)}
                for value, label in Role.choices if value != Role.CLIENT
            ],
        }

    def _clients(self):
        clients = Client.objects.all()
        return {
            'available': True,
            'total': clients.count(),
            'active': clients.filter(is_active=True).count(),
        }

    def _catalog(self):
        return {
            'available': True,
            'profiles': Profile.objects.filter(is_active=True).count(),
            'series': Series.objects.filter(is_active=True).count(),
            'families': Family.objects.count(),
        }


class OnlineWorkersView(APIView):
    """GET /api/dashboard/online/ — who is online right now, for the CRM panel."""

    permission_classes = BASE + [IsStaff]

    def get(self, request):
        from core.models import ONLINE_WINDOW

        online_since = timezone.now() - ONLINE_WINDOW
        users = (
            User.objects.exclude(role=Role.CLIENT)
            .filter(is_active=True, last_seen__gte=online_since)
            .order_by('-last_seen')
        )
        return Response([
            {
                'id': u.id,
                'full_name': u.full_name,
                'role': u.role,
                'role_display': u.get_role_display(),
                'last_seen': u.last_seen,
            }
            for u in users
        ])


# ---------------------------------------------------------------------------
# 5. Shop and map
# ---------------------------------------------------------------------------


class ConfigView(APIView):
    """GET /api/config/ — client configuration the app needs at startup.

    Just the Mapbox token today. A public token, so any signed-in user may read
    it; keeping it server-side means it is set in one place, not baked into the
    app build.
    """

    permission_classes = [permissions.IsAuthenticated]
    allows_pending_password = True

    def get(self, request):
        from django.conf import settings
        return Response({'mapbox_token': settings.MAPBOX_TOKEN})


class ShopView(APIView):
    """GET/PATCH /api/shop/ — the owner's own business, a single row.

    Everyone signed in can read it (the map centres on it); office and managers
    can edit its address and pin.
    """

    permission_classes = BASE + [IsStaff]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get(self, request):
        return Response(ShopSerializer(Shop.get(), context={'request': request}).data)

    def patch(self, request):
        if request.user.role not in {Role.OFFICE, Role.MANAGER}:
            return Response({'detail': _('Office access only.')},
                            status=status.HTTP_403_FORBIDDEN)
        serializer = ShopSerializer(Shop.get(), data=request.data, partial=True,
                                    context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def post(self, request):
        """Upload the company logo (multipart, field 'logo')."""
        if request.user.role not in {Role.OFFICE, Role.MANAGER}:
            return Response({'detail': _('Office access only.')},
                            status=status.HTTP_403_FORBIDDEN)
        upload = request.FILES.get('logo')
        if not upload:
            return Response({'logo': [_('No image was uploaded.')]},
                            status=status.HTTP_400_BAD_REQUEST)
        shop = Shop.get()
        if shop.logo:
            shop.logo.delete(save=False)
        shop.logo.save(upload.name, upload, save=True)
        return Response(ShopSerializer(shop, context={'request': request}).data)

    def delete(self, request):
        """Remove the company logo."""
        if request.user.role not in {Role.OFFICE, Role.MANAGER}:
            return Response({'detail': _('Office access only.')},
                            status=status.HTTP_403_FORBIDDEN)
        shop = Shop.get()
        if shop.logo:
            shop.logo.delete(save=True)
        return Response(ShopSerializer(shop, context={'request': request}).data)


class SettingsView(APIView):
    """GET/PATCH /api/settings/ — app configuration (Cloudinary). Managers only.

    Saving new Cloudinary credentials re-applies them live, so image storage
    switches to (or from) Cloudinary without a redeploy.
    """

    permission_classes = BASE + [IsManager]

    def get(self, request):
        from core.models import AppConfig
        return Response(SettingsSerializer(AppConfig.get()).data)

    def patch(self, request):
        from core.models import AppConfig
        from core.storage_config import apply_cloudinary_config

        serializer = SettingsSerializer(
            AppConfig.get(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        backend = apply_cloudinary_config()
        data = serializer.data
        data['storage_backend'] = 'cloudinary' if backend and 'Cloudinary' in backend else 'local'
        return Response(data)


class MapView(APIView):
    """GET /api/map/ — everything the map plots: the shop, warehouses, clients.

    Only places that have coordinates are returned for warehouses and clients;
    the shop is always sent so the map has something to centre on even before any
    client has been placed.
    """

    permission_classes = BASE + [IsOffice]

    def get(self, request):
        clients = (
            Client.objects.filter(is_active=True)
            .exclude(latitude__isnull=True).exclude(longitude__isnull=True)
        )
        warehouses = (
            Warehouse.objects.filter(is_active=True)
            .exclude(latitude__isnull=True).exclude(longitude__isnull=True)
        )
        return Response({
            'shop': ShopSerializer(Shop.get()).data,
            'warehouses': WarehouseSerializer(warehouses, many=True).data,
            'clients': [
                {
                    'id': c.id, 'name': c.name, 'city': c.city,
                    'address': c.address, 'phone': c.phone,
                    'latitude': c.latitude, 'longitude': c.longitude,
                }
                for c in clients
            ],
        })


# ---------------------------------------------------------------------------
# 6. Stock
# ---------------------------------------------------------------------------


class WarehouseViewSet(viewsets.ModelViewSet):
    """GET/POST/PATCH /api/warehouses/ — warehouses and their map pins."""

    serializer_class = WarehouseSerializer
    permission_classes = BASE + [IsOffice]

    def get_queryset(self):
        qs = Warehouse.objects.annotate(location_count=Count('locations'))
        if self.request.query_params.get('active') == 'true':
            qs = qs.filter(is_active=True)
        return qs.order_by('name')


class LocationViewSet(viewsets.ModelViewSet):
    """GET/POST/PATCH /api/locations/ — racks/bays; filter with ?warehouse=<id>."""

    serializer_class = LocationSerializer
    permission_classes = BASE + [IsStockStaff]

    def get_queryset(self):
        qs = Location.objects.select_related('warehouse')
        if warehouse := self.request.query_params.get('warehouse'):
            qs = qs.filter(warehouse_id=warehouse)
        return qs.order_by('warehouse__name', 'code')


class StockItemViewSet(viewsets.ModelViewSet):
    """GET /api/stock/ — the stock rows the Stock page renders.

    Like the catalog, but each row is a physical holding: a profile in a finish
    and length at a location, with the amount on hand. POST adds a holding;
    POST /api/stock/<id>/move/ records an in/out movement.

    Filters:
        ?series=7000        stock of profiles listed under that series
        ?role=sash          by the profile's role
        ?finish=Anodised    by finish (colour)
        ?warehouse=<id>     by warehouse
        ?availability=in|low|out
        ?search=<text>      profile number or description
    """

    serializer_class = StockItemSerializer

    def get_permissions(self):
        # Everyone on staff can look; only stock staff can change anything.
        if self.action in ('list', 'retrieve', 'options'):
            return [p() for p in BASE + [IsStaff]]
        return [p() for p in BASE + [IsStockStaff]]

    def get_serializer_class(self):
        if self.action == 'create':
            return StockItemCreateSerializer
        return StockItemSerializer

    def get_queryset(self):
        qs = (
            StockItem.objects
            .select_related('profile', 'location', 'location__warehouse')
            .prefetch_related('profile__series', 'profile__series_profiles')
            .annotate(qty=Coalesce(Sum('movements__quantity'), 0))
            .order_by('profile__number', 'finish', 'length_mm')
        )
        params = self.request.query_params
        if series := params.get('series'):
            qs = qs.filter(profile__series__code=series)
        if role := params.get('role'):
            qs = qs.filter(profile__series_profiles__role=role)
        if finish := params.get('finish'):
            qs = qs.filter(finish=finish)
        if warehouse := params.get('warehouse'):
            qs = qs.filter(location__warehouse_id=warehouse)
        if search := params.get('search'):
            qs = qs.filter(
                Q(profile__number__icontains=search)
                | Q(profile__description__icontains=search)
            )
        availability = params.get('availability')
        if availability == 'in':
            qs = qs.filter(qty__gt=0)
        elif availability == 'out':
            qs = qs.filter(qty__lte=0)
        elif availability == 'low':
            qs = qs.filter(qty__lt=F('minimum_quantity'))
        return qs.distinct()

    @action(detail=False, methods=['get'])
    def options(self, request):
        """GET /api/stock/options/ — the filter choices: finishes, warehouses,
        series and profile types, limited to what actually appears in stock."""
        from core.models import ProfileRole, Series

        finishes = sorted(
            f for f in StockItem.objects.values_list('finish', flat=True).distinct()
            if f
        )
        warehouses = [
            {'id': w.id, 'name': w.name}
            for w in Warehouse.objects.filter(is_active=True).order_by('name')
        ]
        # Series present in stock, labelled with their family.
        codes = {c for c in StockItem.objects.values_list(
            'profile__series__code', flat=True).distinct() if c}
        series = [
            {'code': s.code,
             'name': f'{s.code} · {s.family.name}' if s.family_id else s.code}
            for s in Series.objects.filter(code__in=codes)
            .select_related('family').order_by('code')
        ]
        # Profile types (roles) present in stock.
        role_values = {v for v in StockItem.objects.values_list(
            'profile__series_profiles__role', flat=True).distinct() if v}
        roles = [
            {'value': v, 'label': str(label)}
            for v, label in ProfileRole.choices if v in role_values
        ]
        return Response({'finishes': finishes, 'warehouses': warehouses,
                         'series': series, 'roles': roles})

    @action(detail=True, methods=['post'])
    def move(self, request, pk=None):
        """POST /api/stock/<id>/move/ — {movement_type, quantity, note}.

        Records one entry in the append-only ledger; the sign comes from the
        movement type. Returns the row with its refreshed amount.
        """
        item = self.get_object()
        serializer = StockMovementCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        StockMovement.objects.create(
            stock_item=item,
            movement_type=serializer.validated_data['movement_type'],
            quantity=serializer.validated_data['signed'],
            note=serializer.validated_data.get('note', ''),
            performed_by=request.user,
        )
        fresh = self.get_queryset().get(pk=item.pk)
        return Response(StockItemSerializer(fresh, context={'request': request}).data)


# ---------------------------------------------------------------------------
# 7. Orders
# ---------------------------------------------------------------------------


class OrderViewSet(viewsets.ModelViewSet):
    """GET/POST/PATCH /api/orders/ — client orders, priced by weight.

    Office and managers create and edit orders; the money (weight x price/kg,
    then discount and VAT) is computed and returned read-only. Warehouse workers
    may read orders and prepare them -- weigh lines, note shortages, advance the
    status -- through the detail actions below (but not edit prices or lines).
    """

    serializer_class = OrderSerializer

    def get_permissions(self):
        if self.action in ('deliveries', 'sign_delivery', 'start_delivery'):
            return [p() for p in BASE + [IsDeliveryStaff]]
        # Warehouse workers and drivers may also create orders (for a client at
        # the counter) and prepare them, not just read.
        if self.action in ('list', 'retrieve', 'create', 'order_note',
                           'delivery_note', 'line_action', 'set_status'):
            return [p() for p in BASE + [IsStockStaff]]
        return [p() for p in BASE + [IsOffice]]

    def get_queryset(self):
        qs = (Order.objects.select_related('client', 'created_by')
              .prefetch_related('lines__profile', 'lines__series'))
        params = self.request.query_params
        if client := params.get('client'):
            qs = qs.filter(client_id=client)
        if status_f := params.get('status'):
            qs = qs.filter(status=status_f)
        if search := params.get('search'):
            qs = qs.filter(
                Q(number__icontains=search) | Q(client__name__icontains=search))
        return qs.order_by('-ordered_at')

    def _pdf_response(self, order, kind):
        from django.http import HttpResponse
        from core import order_pdf

        company = order_pdf.company_from_shop()
        if kind == 'order_note':
            data = order_pdf.render_order_note(order, company)
            filename = f'{order.number}_order.pdf'
            field_name = 'order_note_pdf'
        else:
            data = order_pdf.render_delivery_note(order, company)
            filename = f'{order.number}_delivery.pdf'
            field_name = 'delivery_note_pdf'
        # Persist a copy to storage (Cloudinary/local); an order can still change,
        # so refresh the stored copy each time it's generated.
        try:
            store_pdf(order, field_name, filename, data)
        except Exception:                                 # noqa: BLE001
            pass
        response = HttpResponse(data, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response

    @action(detail=True, methods=['get'], renderer_classes=[BinaryFileRenderer])
    def order_note(self, request, pk=None):
        """GET /api/orders/<id>/order_note/ — the priced order note PDF."""
        return self._pdf_response(self.get_object(), 'order_note')

    @action(detail=True, methods=['get'], renderer_classes=[BinaryFileRenderer])
    def delivery_note(self, request, pk=None):
        """GET /api/orders/<id>/delivery_note/ — the delivery note PDF."""
        return self._pdf_response(self.get_object(), 'delivery_note')

    @action(detail=False, methods=['get'])
    def deliveries(self, request):
        """GET /api/orders/deliveries/ — the driver's delivery run.

        Orders marked 'ready' are still to deliver; ?done=true lists the ones
        already delivered (and signed).
        """
        from core.models import OrderStatus
        done = request.query_params.get('done') == 'true'
        statuses = ([OrderStatus.DELIVERED] if done
                    else [OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY])
        orders = (Order.objects.filter(status__in=statuses)
                  .select_related('client').prefetch_related('lines')
                  .order_by('required_by', '-ordered_at'))
        data = [self._delivery_row(o, request) for o in orders]
        return Response(data)

    def _delivery_row(self, o, request):
        c = o.client
        return {
            'id': o.id, 'number': o.number,
            'client_name': c.name if c else '',
            'client_phone': (c.phone if c else '') or '',
            'address': ((c.delivery_address or c.address) if c else '') or '',
            'city': (c.city if c else '') or '',
            'latitude': float(c.latitude) if c and c.latitude is not None else None,
            'longitude': float(c.longitude) if c and c.longitude is not None else None,
            'total_weight_kg': str(o.total_weight_kg),
            'line_count': o.lines.count(),
            'status': o.status, 'status_display': o.get_status_display(),
            'signed': bool(o.delivery_signed_at),
            'signed_at': (o.delivery_signed_at.isoformat()
                          if o.delivery_signed_at else None),
            'public_url': (request.build_absolute_uri(f'/d/{o.public_token}/')
                           if o.public_token else None),
            'required_by': o.required_by.isoformat() if o.required_by else None,
        }

    @action(detail=True, methods=['post'])
    def start_delivery(self, request, pk=None):
        """POST /api/orders/<id>/start_delivery/ — loaded on the truck."""
        from core.models import OrderStatus
        order = self.get_object()
        order.status = OrderStatus.OUT_FOR_DELIVERY
        order.save(update_fields=['status'])
        return Response(self._delivery_row(order, request))

    @action(detail=True, methods=['post'],
            parser_classes=[MultiPartParser, FormParser])
    def sign_delivery(self, request, pk=None):
        """POST /api/orders/<id>/sign_delivery/ — the client signs on delivery.

        Multipart: `signature` (image), `recipient_name`, optional `notes` and
        `photo`. Records the Delivery, stamps delivery_signed_at, marks the order
        delivered, and refreshes the stored (now signed) delivery-note PDF.
        """
        from core.models import Delivery, OrderStatus
        order = self.get_object()
        recipient = (request.data.get('recipient_name') or '').strip()
        signature = request.FILES.get('signature')
        photo = request.FILES.get('photo')
        delivery = Delivery.objects.create(
            order=order, driver=request.user,
            status=Delivery.Status.DELIVERED, delivered_at=timezone.now(),
            recipient_name=recipient, notes=request.data.get('notes', ''),
            address=((order.client.delivery_address or order.client.address)
                     if order.client else ''))
        if signature:
            delivery.signature.save(f'{order.number}_sig.png', signature, save=True)
        if photo:
            delivery.photo.save(f'{order.number}_photo.jpg', photo, save=True)
        import uuid
        order.delivery_signed_at = timezone.now()
        order.status = OrderStatus.DELIVERED
        if not order.public_token:
            order.public_token = uuid.uuid4()
        order.save(update_fields=['delivery_signed_at', 'status', 'public_token'])
        # Refresh the stored delivery note now that it is signed.
        try:
            from core import order_pdf
            data = order_pdf.render_delivery_note(
                order, order_pdf.company_from_shop())
            store_pdf(order, 'delivery_note_pdf',
                      f'{order.number}_delivery.pdf', data)
        except Exception:                                 # noqa: BLE001
            pass
        result = OrderSerializer(order, context={'request': request}).data
        result['public_url'] = request.build_absolute_uri(f'/d/{order.public_token}/')
        return Response(result)

    @action(detail=True, methods=['post'])
    def line_action(self, request, pk=None):
        """POST /api/orders/<id>/line_action/ — prepare one line.

        Body: {line_id, weight_kg?, shortage_note?, prepared?}. The warehouse
        worker weighs a line (which overrides the estimate), notes a shortage,
        or marks it picked. Only these three fields move — prices and quantities
        stay as the office set them.
        """
        order = self.get_object()
        line_id = request.data.get('line_id')
        line = order.lines.filter(pk=line_id).first()
        if line is None:
            return Response({'line_id': [_('Unknown line.')]},
                            status=status.HTTP_400_BAD_REQUEST)
        fields = []
        if 'weight_kg' in request.data:
            raw = request.data.get('weight_kg')
            if raw in (None, ''):
                line.weight_kg_override = None
            else:
                try:
                    line.weight_kg_override = Decimal(str(raw))
                except (InvalidOperation, ValueError, TypeError):
                    return Response({'weight_kg': [_('Enter a valid weight.')]},
                                    status=status.HTTP_400_BAD_REQUEST)
            fields.append('weight_kg_override')
        if 'shortage_note' in request.data:
            line.shortage_note = str(request.data.get('shortage_note') or '')[:255]
            fields.append('shortage_note')
        if 'delivered_length_m' in request.data:
            raw = request.data.get('delivered_length_m')
            if raw in (None, ''):
                line.delivered_length_m = None
            else:
                try:
                    line.delivered_length_m = Decimal(str(raw))
                except (InvalidOperation, ValueError, TypeError):
                    return Response({'delivered_length_m': [_('Enter a valid length.')]},
                                    status=status.HTTP_400_BAD_REQUEST)
            fields.append('delivered_length_m')
        if 'prepared' in request.data:
            line.prepared = bool(request.data.get('prepared'))
            fields.append('prepared')
        if fields:
            line.save(update_fields=fields)
        order.refresh_from_db()
        return Response(OrderSerializer(order, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def set_status(self, request, pk=None):
        """POST /api/orders/<id>/set_status/ — advance the order's status."""
        order = self.get_object()
        from core.models import OrderStatus

        new_status = request.data.get('status')
        valid = {c[0] for c in OrderStatus.choices}
        if new_status not in valid:
            return Response({'status': [_('Unknown status.')]},
                            status=status.HTTP_400_BAD_REQUEST)
        order.status = new_status
        order.save(update_fields=['status'])
        return Response(OrderSerializer(order, context={'request': request}).data)


class InvoiceViewSet(viewsets.ModelViewSet):
    """Income and expense invoices for the accountant's books.

    Filter with ?direction=income|expense, ?year=YYYY, ?month=1-12,
    ?tax_id=<partial> and ?search=<text>.
    """

    serializer_class = InvoiceSerializer
    permission_classes = BASE + [IsOffice]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        qs = Invoice.objects.select_related('client')
        p = self.request.query_params
        if direction := p.get('direction'):
            qs = qs.filter(direction=direction)
        if year := p.get('year'):
            qs = qs.filter(issued_at__year=int(year))
        if month := p.get('month'):
            qs = qs.filter(issued_at__month=int(month))
        if tax := p.get('tax_id'):
            qs = qs.filter(Q(party_tax_id__icontains=tax)
                           | Q(client__tax_id__icontains=tax))
        if search := p.get('search'):
            qs = qs.filter(Q(number__icontains=search)
                           | Q(party_name__icontains=search)
                           | Q(client__name__icontains=search))
        return qs.order_by('-issued_at', '-id')

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=['get'])
    def next_number(self, request):
        """GET /api/invoices/next_number/ — the next income invoice number.

        Income invoices are ours, so they auto-increment (INV-YYYY-NNNN);
        expense numbers come from the supplier and aren't generated here.
        """
        year = timezone.localtime().year
        prefix = f'INV-{year}-'
        last = (Invoice.objects.filter(direction=Invoice.Direction.INCOME,
                                       number__startswith=prefix)
                .order_by('-number').first())
        try:
            seq = int(last.number.rsplit('-', 1)[1]) + 1 if last else 1
        except (ValueError, IndexError):
            seq = 1
        return Response({'number': f'{prefix}{seq:04d}'})

    @action(detail=True, methods=['post'])
    def send_email(self, request, pk=None):
        """POST /api/invoices/<id>/send_email/ — email the invoice + its file.

        SMTP comes from Settings (a Railway env var wins over the stored value).
        WhatsApp is handled on the desktop (it can only carry text), so there is
        no send_whatsapp endpoint.
        """
        from django.core.mail import EmailMessage, get_connection
        from core.models import AppConfig

        invoice = self.get_object()
        to = (request.data.get('to')
              or (invoice.client.email if invoice.client else '') or '').strip()
        if not to:
            return Response({'detail': _('No recipient email address.')},
                            status=status.HTTP_400_BAD_REQUEST)
        cfg = AppConfig.get()
        if not cfg.smtp_ready:
            return Response(
                {'detail': _('Email is not configured in Settings.')},
                status=status.HTTP_400_BAD_REQUEST)

        connection = get_connection(
            host=cfg.setting('smtp_host'),
            port=int(cfg.setting('smtp_port') or 587),
            username=cfg.setting('smtp_user') or None,
            password=cfg.setting('smtp_password') or None,
            use_tls=bool(cfg.smtp_use_tls),
        )
        label = f'{invoice.get_direction_display()} {invoice.number or invoice.id}'
        body = request.data.get('message') or _(
            'Please find the attached invoice %(label)s.') % {'label': label}
        message = EmailMessage(
            subject=label, body=body, from_email=cfg.setting('smtp_from'),
            to=[to], connection=connection)
        if invoice.file:
            invoice.file.open('rb')
            data = invoice.file.read()
            invoice.file.close()
            message.attach(f'{invoice.number or invoice.id}.pdf', data,
                           'application/pdf')
        try:
            message.send()
        except Exception as exc:                          # noqa: BLE001
            return Response({'detail': str(exc)},
                            status=status.HTTP_502_BAD_GATEWAY)
        return Response({'sent': True, 'to': to})

    @action(detail=False, methods=['get'],
            renderer_classes=[BinaryFileRenderer])
    def accountant_zip(self, request):
        """GET /api/invoices/accountant_zip/?year=&month= — download the package
        (salary xlsx + combined income/expense PDFs + the invoice files)."""
        from django.http import HttpResponse
        from core.accountant_bundle import build_accountant_zip, zip_filename

        year, month = self._period(request)
        if year is None:
            return Response({'month': [_('Use ?year=&month=.')]},
                            status=status.HTTP_400_BAD_REQUEST)
        data, _summary = build_accountant_zip(year, month)
        resp = HttpResponse(data, content_type='application/zip')
        resp['Content-Disposition'] = (
            f'attachment; filename="{zip_filename(year, month)}"')
        return resp

    @action(detail=False, methods=['post'])
    def email_accountant(self, request):
        """POST /api/invoices/email_accountant/ {year, month} — email the package
        to the accountant (address from Settings; SMTP from env/Settings)."""
        from django.core.mail import EmailMessage, get_connection
        from core.accountant_bundle import build_accountant_zip, zip_filename
        from core.models import AppConfig

        year, month = self._period(request)
        if year is None:
            return Response({'month': [_('Give a year and month.')]},
                            status=status.HTTP_400_BAD_REQUEST)
        cfg = AppConfig.get()
        to = (request.data.get('to') or cfg.accountant_email or '').strip()
        if not to:
            return Response({'detail': _('No accountant email in Settings.')},
                            status=status.HTTP_400_BAD_REQUEST)
        if not cfg.smtp_ready:
            return Response({'detail': _('Email is not configured in Settings.')},
                            status=status.HTTP_400_BAD_REQUEST)

        data, summary = build_accountant_zip(year, month)
        connection = get_connection(
            host=cfg.setting('smtp_host'), port=int(cfg.setting('smtp_port') or 587),
            username=cfg.setting('smtp_user') or None,
            password=cfg.setting('smtp_password') or None,
            use_tls=bool(cfg.smtp_use_tls))
        subject = _('Invoices & salary %(m)s') % {'m': summary['month']}
        body = _('Attached: %(inc)d income and %(exp)d expense invoices plus the '
                 'salary sheet for %(m)s.') % {
            'inc': summary['income_count'], 'exp': summary['expense_count'],
            'm': summary['month']}
        msg = EmailMessage(subject=subject, body=body,
                           from_email=cfg.setting('smtp_from'), to=[to],
                           connection=connection)
        msg.attach(zip_filename(year, month), data, 'application/zip')
        try:
            msg.send()
        except Exception as exc:                          # noqa: BLE001
            return Response({'detail': str(exc)},
                            status=status.HTTP_502_BAD_GATEWAY)
        return Response({'sent': True, 'to': to, **summary})

    def _period(self, request):
        src = request.data if request.method == 'POST' else request.query_params
        try:
            return int(src.get('year')), int(src.get('month'))
        except (TypeError, ValueError):
            return None, None

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Income vs expense totals for the selected month/year (both sides)."""
        base = Invoice.objects.all()
        p = request.query_params
        if year := p.get('year'):
            base = base.filter(issued_at__year=int(year))
        if month := p.get('month'):
            base = base.filter(issued_at__month=int(month))
        rows = base.values('direction').annotate(
            total=Coalesce(Sum('total'), Decimal('0.00')), count=Count('id'))
        income = expense = Decimal('0.00')
        income_count = expense_count = 0
        for row in rows:
            if row['direction'] == Invoice.Direction.INCOME:
                income, income_count = row['total'], row['count']
            elif row['direction'] == Invoice.Direction.EXPENSE:
                expense, expense_count = row['total'], row['count']
        return Response({
            'income_total': str(income), 'expense_total': str(expense),
            'net': str(income - expense),
            'income_count': income_count, 'expense_count': expense_count,
        })


# ---------------------------------------------------------------------------
# 8. Attendance (clock in / out)
# ---------------------------------------------------------------------------


class AttendanceViewSet(viewsets.ReadOnlyModelViewSet):
    """Clock in / out and shift history.

    A worker sees and manages only their own shifts; managers and office may
    also read another worker's history with ?worker=<id> (for payroll later).
    """

    serializer_class = ShiftSerializer
    permission_classes = BASE + [IsStaff]

    def get_queryset(self):
        user = self.request.user
        qs = Shift.objects.select_related('worker')
        worker = self.request.query_params.get('worker')
        if worker and user.role in {Role.MANAGER, Role.OFFICE}:
            qs = qs.filter(worker_id=worker)
        else:
            qs = qs.filter(worker=user)
        # 'Recent shifts' resets weekly: pass ?since=YYYY-MM-DD (the phone sends
        # the current Sunday) to show only this week's shifts.
        since = self.request.query_params.get('since')
        if since:
            parsed = parse_date(since)
            if parsed:
                qs = qs.filter(clock_in__date__gte=parsed)
        return qs.order_by('-clock_in')

    def _open_shift(self, user):
        return Shift.objects.filter(worker=user, clock_out__isnull=True).first()

    def _shift_today(self, user):
        """The shift already started today, if any (one clock-in per day)."""
        start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        return (Shift.objects.filter(worker=user, clock_in__gte=start,
                                     clock_in__lt=start + timedelta(days=1))
                .order_by('-clock_in').first())

    @action(detail=False, methods=['get'])
    def current(self, request):
        """GET /api/attendance/current/ — the open shift, or null."""
        shift = self._open_shift(request.user)
        return Response(ShiftSerializer(shift).data if shift else None)

    @action(detail=False, methods=['post'])
    def clock_in(self, request):
        """POST /api/attendance/clock_in/ — start today's shift (once per day)."""
        if self._open_shift(request.user):
            return Response(
                {'detail': _('You are already clocked in.')},
                status=status.HTTP_400_BAD_REQUEST)
        # One shift per day: if today already has a shift (even a closed one),
        # a correction request is the way to change it, not a second clock-in.
        if self._shift_today(request.user):
            return Response(
                {'detail': _('You have already clocked in today.')},
                status=status.HTTP_400_BAD_REQUEST)
        shift = Shift.objects.create(
            worker=request.user, clock_in=timezone.now(),
            note=str(request.data.get('note') or '')[:255])
        return Response(ShiftSerializer(shift).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'])
    def clock_out(self, request):
        """POST /api/attendance/clock_out/ — end the open shift."""
        shift = self._open_shift(request.user)
        if not shift:
            return Response(
                {'detail': _('You are not clocked in.')},
                status=status.HTTP_400_BAD_REQUEST)
        shift.clock_out = timezone.now()
        if request.data.get('note'):
            shift.note = str(request.data['note'])[:255]
        shift.save(update_fields=['clock_out', 'note'])
        return Response(ShiftSerializer(shift).data)

    @action(detail=False, methods=['get'])
    def payroll(self, request):
        """GET /api/attendance/payroll/?month=YYYY-MM&worker=<id>

        Pay for a worker over a calendar month, computed from their shifts under
        Israeli overtime law. A worker sees their own; office/managers may pass
        ?worker=<id> to see anyone's.
        """
        from core.payroll import compute_payroll

        user = request.user
        worker = user
        worker_id = request.query_params.get('worker')
        if worker_id and user.role in {Role.MANAGER, Role.OFFICE}:
            worker = User.objects.filter(pk=worker_id).first()
            if worker is None:
                return Response({'worker': [_('Unknown worker.')]},
                                status=status.HTTP_400_BAD_REQUEST)

        # Default to the current month; accept ?month=YYYY-MM.
        now = timezone.localtime()
        month = request.query_params.get('month')
        try:
            year, mon = (int(x) for x in month.split('-')) if month else (now.year, now.month)
        except (ValueError, AttributeError):
            return Response({'month': [_('Use the format YYYY-MM.')]},
                            status=status.HTTP_400_BAD_REQUEST)
        start = now.replace(year=year, month=mon, day=1, hour=0, minute=0,
                            second=0, microsecond=0)
        end = start.replace(year=year + (mon == 12),
                            month=1 if mon == 12 else mon + 1)
        shifts = Shift.objects.filter(
            worker=worker, clock_in__gte=start, clock_in__lt=end,
            clock_out__isnull=False)

        data = compute_payroll(worker, shifts)
        data.update({
            'worker': worker.id,
            'worker_name': worker.full_name,
            'month': f'{year:04d}-{mon:02d}',
        })
        return Response(data)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """GET /api/attendance/summary/ — today's and this week's minutes.

        The week runs Sunday–Saturday (the Israeli work week), so the totals
        reset every Sunday.
        """
        now = timezone.localtime()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_week = start_of_day - timedelta(days=(start_of_day.weekday() + 1) % 7)
        shifts = Shift.objects.filter(worker=request.user, clock_in__gte=start_of_week)
        today = week = 0
        for s in shifts:
            m = s.duration_minutes
            week += m
            if s.clock_in >= start_of_day:
                today += m
        open_shift = self._open_shift(request.user)
        return Response({
            'today_minutes': today,
            'week_minutes': week,
            'week_start': start_of_week.date().isoformat(),
            'is_clocked_in': open_shift is not None,
            'open_since': open_shift.clock_in if open_shift else None,
        })

    @action(detail=False, methods=['get'])
    def month(self, request):
        """GET /api/attendance/month/?month=YYYY-MM&worker= — per-day work totals.

        Feeds the worker's monthly calendar: one entry per day that has a shift,
        with the clock-in/out and minutes worked.
        """
        user = request.user
        worker = user
        worker_id = request.query_params.get('worker')
        if worker_id and user.role in {Role.MANAGER, Role.OFFICE}:
            worker = User.objects.filter(pk=worker_id).first()
            if worker is None:
                return Response({'worker': [_('Unknown worker.')]},
                                status=status.HTTP_400_BAD_REQUEST)

        now = timezone.localtime()
        month = request.query_params.get('month')
        try:
            year, mon = ((int(x) for x in month.split('-')) if month
                         else (now.year, now.month))
            year, mon = int(year), int(mon)
        except (ValueError, AttributeError):
            return Response({'month': [_('Use the format YYYY-MM.')]},
                            status=status.HTTP_400_BAD_REQUEST)
        start = now.replace(year=year, month=mon, day=1, hour=0, minute=0,
                            second=0, microsecond=0)
        end = start.replace(year=year + (mon == 12),
                            month=1 if mon == 12 else mon + 1)
        shifts = Shift.objects.filter(
            worker=worker, clock_in__gte=start, clock_in__lt=end).order_by('clock_in')

        days = {}
        total_minutes = 0
        for s in shifts:
            local_in = timezone.localtime(s.clock_in)
            key = local_in.date().isoformat()
            minutes = s.duration_minutes
            total_minutes += minutes
            row = days.setdefault(key, {
                'date': key, 'clock_in': None, 'clock_out': None,
                'minutes': 0, 'open': False})
            # Emit local (Asia/Jerusalem) ISO strings so the clock hour is never
            # ambiguous, matching what the ShiftSerializer returns.
            if row['clock_in'] is None:
                row['clock_in'] = local_in.isoformat()
            row['clock_out'] = (timezone.localtime(s.clock_out).isoformat()
                                if s.clock_out else None)
            row['minutes'] += minutes
            if s.clock_out is None:
                row['open'] = True
        return Response({
            'month': f'{year:04d}-{mon:02d}',
            'worker': worker.id,
            'worker_name': worker.full_name,
            'days': list(days.values()),
            'days_worked': len(days),
            'total_minutes': total_minutes,
        })


class ShiftCorrectionRequestViewSet(viewsets.ModelViewSet):
    """Workers raise clock-fix requests; managers/office approve or reject them.

    A worker creates and sees only their own; office/managers see everyone's and
    act on them via approve/reject. Approving applies the requested times to the
    shift (creating one if the day had none).
    """

    serializer_class = ShiftCorrectionRequestSerializer

    def get_permissions(self):
        if self.action in ('approve', 'reject'):
            return [p() for p in BASE + [IsOffice]]
        return [p() for p in BASE + [IsStaff]]

    def get_queryset(self):
        user = self.request.user
        qs = ShiftCorrectionRequest.objects.select_related(
            'worker', 'reviewed_by', 'shift')
        if user.role in {Role.MANAGER, Role.OFFICE}:
            if worker := self.request.query_params.get('worker'):
                qs = qs.filter(worker_id=worker)
        else:
            qs = qs.filter(worker=user)
        if status_f := self.request.query_params.get('status'):
            qs = qs.filter(status=status_f)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(worker=self.request.user)

    def _finish(self, req, new_status, note):
        req.status = new_status
        req.reviewed_by = self.request.user
        req.reviewed_at = timezone.now()
        req.review_note = note[:255]
        req.save(update_fields=['status', 'reviewed_by', 'reviewed_at',
                                'review_note'])

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """POST /api/corrections/<id>/approve/ — apply the requested times."""
        req = self.get_object()
        if req.status != ShiftCorrectionRequest.Status.PENDING:
            return Response({'detail': _('Already reviewed.')},
                            status=status.HTTP_400_BAD_REQUEST)
        shift = req.shift
        if shift is None:
            shift = Shift(worker=req.worker,
                          clock_in=req.requested_clock_in or timezone.now())
        if req.requested_clock_in:
            shift.clock_in = req.requested_clock_in
        # An explicit clock-out is applied; note it may reopen or close the shift.
        shift.clock_out = req.requested_clock_out
        shift.save()
        req.shift = shift
        req.save(update_fields=['shift'])
        self._finish(req, ShiftCorrectionRequest.Status.APPROVED,
                     request.data.get('review_note', ''))
        return Response(ShiftCorrectionRequestSerializer(req).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """POST /api/corrections/<id>/reject/ — decline the request."""
        req = self.get_object()
        if req.status != ShiftCorrectionRequest.Status.PENDING:
            return Response({'detail': _('Already reviewed.')},
                            status=status.HTTP_400_BAD_REQUEST)
        self._finish(req, ShiftCorrectionRequest.Status.REJECTED,
                     request.data.get('review_note', ''))
        return Response(ShiftCorrectionRequestSerializer(req).data)


# ---------------------------------------------------------------------------
# 9. Payslips
# ---------------------------------------------------------------------------


class PayslipViewSet(viewsets.ModelViewSet):
    """Monthly payslips. Office and managers create, edit and finalise them.

    A slip is generated from a worker's clocked shifts (base pay + Israeli
    overtime), then hours, rates and adjustments can be overridden while it is a
    draft. Finalising locks it; a manager can reopen. A worker may read their
    own finalised slips.
    """

    serializer_class = PayslipSerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'pdf'):
            return [p() for p in BASE + [IsStaff]]
        return [p() for p in BASE + [IsOffice]]

    def get_queryset(self):
        user = self.request.user
        qs = Payslip.objects.select_related('worker').prefetch_related('adjustments')
        params = self.request.query_params
        if user.role not in {Role.MANAGER, Role.OFFICE}:
            # Workers see only their own finalised slips.
            qs = qs.filter(worker=user, status=Payslip.Status.FINAL)
        else:
            if worker := params.get('worker'):
                qs = qs.filter(worker_id=worker)
            if year := params.get('year'):
                qs = qs.filter(year=year)
            if month := params.get('month'):
                qs = qs.filter(month=month)
        return qs.order_by('-year', '-month')

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """POST /api/payslips/generate/ — {worker, year, month}.

        Load the worker's shifts for the month and build a draft payslip. Fails
        if a slip for that period already exists (edit it instead).
        """
        from core.payroll import compute_payroll

        worker_id = request.data.get('worker')
        try:
            year = int(request.data.get('year'))
            month = int(request.data.get('month'))
        except (TypeError, ValueError):
            return Response({'detail': _('year and month are required.')},
                            status=status.HTTP_400_BAD_REQUEST)
        worker = User.objects.filter(pk=worker_id).first()
        if worker is None:
            return Response({'worker': [_('Unknown worker.')]},
                            status=status.HTTP_400_BAD_REQUEST)
        if Payslip.objects.filter(worker=worker, year=year, month=month).exists():
            return Response(
                {'detail': _('A payslip for that month already exists.')},
                status=status.HTTP_400_BAD_REQUEST)

        start = timezone.localtime().replace(
            year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=year + (month == 12),
                            month=1 if month == 12 else month + 1)
        shifts = Shift.objects.filter(
            worker=worker, clock_in__gte=start, clock_in__lt=end,
            clock_out__isnull=False)
        calc = compute_payroll(worker, shifts)

        payslip = Payslip.objects.create(
            worker=worker, year=year, month=month,
            source=Payslip.Source.GENERATED,
            pay_basis=worker.pay_basis, overtime_enabled=worker.overtime_enabled,
            days_worked=calc['days_worked'],
            regular_hours=Decimal(str(calc['regular_hours'])),
            overtime_125_hours=Decimal(str(calc['overtime_125_hours'])),
            overtime_150_hours=Decimal(str(calc['overtime_150_hours'])),
            hourly_rate=Decimal(str(calc['hourly_rate'])),
            base_pay=Decimal(str(calc['base_pay'])),
            overtime_pay=Decimal(str(calc['overtime_pay'])),
            created_by=request.user,
        )
        return Response(PayslipSerializer(payslip, context={'request': request}).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def finalise(self, request, pk=None):
        """POST /api/payslips/<id>/finalise/ — lock the payslip."""
        payslip = self.get_object()
        payslip.status = Payslip.Status.FINAL
        payslip.finalised_at = timezone.now()
        payslip.save(update_fields=['status', 'finalised_at'])
        return Response(PayslipSerializer(payslip, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def reopen(self, request, pk=None):
        """POST /api/payslips/<id>/reopen/ — unlock (managers only)."""
        if request.user.role != Role.MANAGER:
            return Response({'detail': _('Only managers can reopen a payslip.')},
                            status=status.HTTP_403_FORBIDDEN)
        payslip = self.get_object()
        payslip.status = Payslip.Status.DRAFT
        payslip.finalised_at = None
        payslip.save(update_fields=['status', 'finalised_at'])
        return Response(PayslipSerializer(payslip, context={'request': request}).data)

    @action(detail=True, methods=['get'], renderer_classes=[BinaryFileRenderer])
    def pdf(self, request, pk=None):
        """GET /api/payslips/<id>/pdf/ — the printable payslip."""
        from django.http import HttpResponse
        from core import order_pdf, payslip_pdf

        payslip = self.get_object()
        data = payslip_pdf.render_payslip(payslip, order_pdf.company_from_shop())
        filename = f'payslip_{payslip.year}_{payslip.month:02d}.pdf'
        # A finalised payslip is immutable, so store it once; a draft can still
        # change, so refresh the stored copy on each render.
        try:
            if payslip.status != Payslip.Status.FINAL or not payslip.pdf:
                store_pdf(payslip, 'pdf', filename, data)
        except Exception:                                 # noqa: BLE001
            pass
        response = HttpResponse(data, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response


# ---------------------------------------------------------------------------
# Public, login-free signed delivery-note page (the WhatsApp link target)
# ---------------------------------------------------------------------------

def _public_order(token):
    from django.http import Http404
    order = (Order.objects.filter(public_token=token)
             .select_related('client').prefetch_related('lines__profile').first())
    if order is None:
        raise Http404('Unknown or expired link.')
    return order


def public_delivery(request, token):
    """GET /d/<token>/ — a client-facing page for a signed delivery note."""
    from django.http import HttpResponse
    from django.utils.html import escape
    from core.order_pdf import company_from_shop

    order = _public_order(token)
    company = company_from_shop()
    signed = (timezone.localtime(order.delivery_signed_at).strftime('%Y-%m-%d %H:%M')
              if order.delivery_signed_at else '—')
    rows = ''.join(
        f'<tr><td>{escape(l.profile.number)}</td>'
        f'<td>{escape(l.profile.description or "")}</td>'
        f'<td style="text-align:left">{l.bars_needed or "—"}</td>'
        f'<td style="text-align:left">{l.total_length_m} m</td></tr>'
        for l in order.lines.all())
    client = order.client
    addr = escape(((client.delivery_address or client.address) if client else '') or '')
    html = f"""<!doctype html><html lang="he" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(order.number)} — תעודת משלוח</title>
<style>
:root{{--navy:#14284b;--blue:#2f6fb0;--line:#e2e8f0;--muted:#6b7785}}
*{{box-sizing:border-box}}body{{margin:0;background:#f4f7fb;color:#14213a;
font-family:-apple-system,Segoe UI,Rubik,Arial,sans-serif}}
.wrap{{max-width:640px;margin:0 auto;padding:16px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:16px}}
.head{{background:var(--navy);color:#fff;border-radius:16px;padding:22px 20px;margin-bottom:16px}}
.head h1{{margin:0 0 4px;font-size:22px}}.head .sub{{opacity:.85;font-size:14px}}
.badge{{display:inline-block;background:#2e7d32;color:#fff;border-radius:20px;
padding:4px 12px;font-size:13px;font-weight:600}}
table{{width:100%;border-collapse:collapse;margin-top:8px}}
th,td{{text-align:right;padding:8px 6px;border-bottom:1px solid var(--line);font-size:14px}}
th{{color:var(--muted);font-weight:600}}
.kv{{display:flex;justify-content:space-between;padding:6px 0;font-size:14px}}
.kv .k{{color:var(--muted)}}
.btn{{display:block;text-align:center;background:var(--blue);color:#fff;text-decoration:none;
padding:14px;border-radius:12px;font-weight:700;font-size:16px;margin-top:8px}}
.btn.ghost{{background:#fff;color:var(--blue);border:1px solid var(--blue)}}
</style></head><body><div class="wrap">
<div class="head"><h1>{escape(company.get('name') or 'AlomForce')}</h1>
<div class="sub">תעודת משלוח {escape(order.number)}</div></div>
<div class="card">
<div class="kv"><span class="k">סטטוס</span><span class="badge">נמסר ונחתם</span></div>
<div class="kv"><span class="k">לקוח</span><b>{escape(client.name if client else '')}</b></div>
<div class="kv"><span class="k">כתובת</span><span>{addr or '—'}</span></div>
<div class="kv"><span class="k">נחתם בתאריך</span><span>{signed}</span></div>
<div class="kv"><span class="k">משקל כולל</span><b>{order.total_weight_kg} ק"ג</b></div>
</div>
<div class="card"><b>פריטים</b>
<table><thead><tr><th>מק"ט</th><th>תיאור</th><th style="text-align:left">מוטות</th>
<th style="text-align:left">אורך</th></tr></thead><tbody>{rows}</tbody></table></div>
<a class="btn" href="/d/{token}/pdf/">הורדת תעודת המשלוח (PDF)</a>
<a class="btn ghost" href="/d/{token}/pdf/" target="_blank">צפייה במסמך</a>
</div></body></html>"""
    return HttpResponse(html)


def public_delivery_pdf(request, token):
    """GET /d/<token>/pdf/ — the signed delivery note PDF, no login."""
    from django.http import HttpResponse
    from core import order_pdf

    order = _public_order(token)
    data = order_pdf.render_delivery_note(order, order_pdf.company_from_shop())
    response = HttpResponse(data, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{order.number}_delivery.pdf"'
    return response
