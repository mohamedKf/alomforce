"""URL routing for the AlomForce API."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from core.views import (
    AttendanceViewSet,
    CatalogListingViewSet,
    ConfigView,
    DashboardView,
    DriversStatusView,
    OnlineWorkersView,
    ChangePasswordView,
    ClientViewSet,
    FamilyViewSet,
    InvoiceViewSet,
    LocationViewSet,
    LoginView,
    MapView,
    MeView,
    OrderViewSet,
    PayslipViewSet,
    ProfileViewSet,
    SeriesViewSet,
    SettingsView,
    ShiftCorrectionRequestViewSet,
    ShopView,
    public_delivery,
    public_delivery_pdf,
    StaffViewSet,
    StockItemViewSet,
    WarehouseViewSet,
)

router = DefaultRouter()
router.register('staff', StaffViewSet, basename='staff')
router.register('clients', ClientViewSet, basename='client')
router.register('stock', StockItemViewSet, basename='stock')
router.register('warehouses', WarehouseViewSet, basename='warehouse')
router.register('locations', LocationViewSet, basename='location')
router.register('orders', OrderViewSet, basename='order')
router.register('invoices', InvoiceViewSet, basename='invoice')
router.register('attendance', AttendanceViewSet, basename='attendance')
router.register('corrections', ShiftCorrectionRequestViewSet, basename='correction')
router.register('payslips', PayslipViewSet, basename='payslip')

catalog_router = DefaultRouter()
catalog_router.register('families', FamilyViewSet, basename='family')
catalog_router.register('series', SeriesViewSet, basename='series')
catalog_router.register('profiles', ProfileViewSet, basename='profile')
catalog_router.register('listings', CatalogListingViewSet, basename='listing')

auth_patterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('verify/', TokenVerifyView.as_view(), name='token-verify'),
    path('me/', MeView.as_view(), name='me'),
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),
]

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/dashboard/', DashboardView.as_view(), name='dashboard'),
    path('api/dashboard/online/', OnlineWorkersView.as_view(), name='dashboard-online'),
    path('api/dashboard/drivers/', DriversStatusView.as_view(), name='dashboard-drivers'),
    path('api/config/', ConfigView.as_view(), name='config'),
    path('api/shop/', ShopView.as_view(), name='shop'),
    path('api/settings/', SettingsView.as_view(), name='settings'),
    path('api/map/', MapView.as_view(), name='map'),
    path('api/auth/', include(auth_patterns)),
    path('api/catalog/', include(catalog_router.urls)),
    path('api/', include(router.urls)),
    # Public, login-free signed delivery note (the WhatsApp link target).
    path('d/<uuid:token>/', public_delivery, name='public-delivery'),
    path('d/<uuid:token>/pdf/', public_delivery_pdf, name='public-delivery-pdf'),
]

# In DEBUG the dev server serves uploaded media (profile section images) off
# the local disk. In production Cloudinary serves them directly, so this branch
# is skipped -- storage is chosen in settings.STORAGES, not here.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
