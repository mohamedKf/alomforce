"""Tests for users, authentication and manager-side account creation."""

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

import os
from unittest import mock

from core import maplinks
from core.models import Client, Role, User, normalise_phone, validate_israeli_id


def make_id(prefix8):
    """Build a valid 9-digit ID by computing the check digit for 8 given digits."""
    total = 0
    for index, char in enumerate(prefix8):
        digit = int(char) * (1 if index % 2 == 0 else 2)
        total += digit if digit < 10 else digit - 9
    return prefix8 + str((10 - total % 10) % 10)


VALID_ID = make_id('12345678')
OTHER_ID = make_id('87654321')
MANAGER_ID = make_id('11223344')
OFFICE_ID = make_id('55667788')


def make_user(id_number=VALID_ID, role=Role.WAREHOUSE, **extra):
    extra.setdefault('first_name', 'Dana')
    extra.setdefault('last_name', 'Levi')
    extra.setdefault('phone', f'050-{id_number[:7]}')
    return User.objects.create_user(
        id_number=id_number, password='str0ng-pass-9', role=role, **extra
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@override_settings(RELAXED_AUTH=False)
class IsraeliIdValidatorTests(TestCase):
    def test_accepts_valid_id(self):
        validate_israeli_id(VALID_ID)

    def test_rejects_bad_check_digit(self):
        bad = VALID_ID[:8] + str((int(VALID_ID[8]) + 1) % 10)
        with self.assertRaises(ValidationError):
            validate_israeli_id(bad)

    def test_rejects_non_digits(self):
        with self.assertRaises(ValidationError):
            validate_israeli_id('12345678a')

    def test_rejects_too_long(self):
        with self.assertRaises(ValidationError):
            validate_israeli_id('1234567890')


class PhoneNormalisationTests(TestCase):
    def test_strips_formatting(self):
        for typed in ['052-777-8899', '052 777 8899', '(052) 777-8899', '0527778899']:
            self.assertEqual(normalise_phone(typed), '0527778899')


@override_settings(RELAXED_AUTH=False)
class UserModelTests(TestCase):
    def test_create_user_normalises_id(self):
        short = make_id('00123456').lstrip('0')
        user = make_user(id_number=short)
        self.assertEqual(len(user.id_number), 9)

    def test_password_is_hashed(self):
        user = make_user()
        self.assertNotEqual(user.password, 'str0ng-pass-9')
        self.assertTrue(user.check_password('str0ng-pass-9'))

    def test_client_role_requires_client_link(self):
        user = User(id_number=VALID_ID, first_name='Dana', last_name='Levi',
                    phone='050-1234567', role=Role.CLIENT)
        with self.assertRaises(ValidationError):
            user.full_clean(exclude=['password'])

    def test_staff_require_an_id_number(self):
        user = User(first_name='Dana', last_name='Levi', phone='050-1234567',
                    role=Role.WAREHOUSE)
        with self.assertRaises(ValidationError):
            user.full_clean(exclude=['password'])

    def test_duplicate_id_rejected(self):
        make_user()
        with self.assertRaises(ValidationError):
            make_user(first_name='Other')


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class LoginTests(APITestCase):
    def setUp(self):
        self.user = make_user()

    def test_login_with_id_number(self):
        response = self.client.post(
            reverse('login'), {'identifier': VALID_ID, 'password': 'str0ng-pass-9'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertEqual(response.data['user']['role'], Role.WAREHOUSE)

    def test_login_accepts_unpadded_id(self):
        user = make_user(id_number=make_id('00123456'))
        response = self.client.post(
            reverse('login'),
            {'identifier': user.id_number.lstrip('0'), 'password': 'str0ng-pass-9'},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_wrong_password_rejected(self):
        response = self.client.post(
            reverse('login'), {'identifier': VALID_ID, 'password': 'wrong-pass-1'}
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.post(
            reverse('login'), {'identifier': VALID_ID, 'password': 'str0ng-pass-9'}
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_requires_auth(self):
        self.assertEqual(
            self.client.get(reverse('me')).status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_me_cannot_escalate_role(self):
        self.client.force_authenticate(user=self.user)
        self.client.patch(reverse('me'), {'role': Role.MANAGER})
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, Role.WAREHOUSE)


class PublicSignupRemovedTests(APITestCase):
    """Accounts are created by managers only; no public route may exist."""

    def test_no_public_signup_routes(self):
        for path in ('/api/auth/register/', '/api/auth/register-client/'):
            self.assertEqual(
                self.client.post(path, {}).status_code,
                status.HTTP_404_NOT_FOUND,
                f'{path} should not exist',
            )


# ---------------------------------------------------------------------------
# Manager-side account creation
# ---------------------------------------------------------------------------


@override_settings(RELAXED_AUTH=True)
class StaffCreationTests(APITestCase):
    def setUp(self):
        self.manager = make_user(id_number=MANAGER_ID, role=Role.MANAGER)
        self.client.force_authenticate(user=self.manager)

    def payload(self, **overrides):
        data = {
            'id_number': OTHER_ID,
            'first_name': 'Sami',
            'last_name': 'Haddad',
            'phone': '052-1112222',
            'role': Role.DRIVER,
            'password': '12345678',
        }
        data.update(overrides)
        return data

    def test_manager_creates_active_worker(self):
        response = self.client.post(reverse('staff-list'), self.payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(id_number=OTHER_ID)
        self.assertEqual(user.role, Role.DRIVER)
        self.assertTrue(user.is_active)
        self.assertEqual(user.created_by, self.manager)

    def test_new_worker_must_change_password(self):
        self.client.post(reverse('staff-list'), self.payload())
        self.assertTrue(User.objects.get(id_number=OTHER_ID).must_change_password)

    def test_cannot_create_client_role_as_staff(self):
        """Client contacts go through the contacts endpoint, which links a company."""
        response = self.client.post(reverse('staff-list'), self.payload(role=Role.CLIENT))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_manager_cannot_create_staff(self):
        worker = make_user(role=Role.WAREHOUSE)
        self.client.force_authenticate(user=worker)
        response = self.client.post(reverse('staff-list'), self.payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_delete_deactivates_rather_than_removes(self):
        self.client.post(reverse('staff-list'), self.payload())
        user = User.objects.get(id_number=OTHER_ID)
        response = self.client.delete(reverse('staff-detail', args=[user.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_reset_password_reinstates_the_flag(self):
        self.client.post(reverse('staff-list'), self.payload())
        user = User.objects.get(id_number=OTHER_ID)
        user.must_change_password = False
        user.save()

        response = self.client.post(
            reverse('staff-reset-password', args=[user.pk]), {'password': 'newstart99'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.must_change_password)
        self.assertTrue(user.check_password('newstart99'))


@override_settings(RELAXED_AUTH=True)
class ClientCompanyTests(APITestCase):
    def setUp(self):
        self.office = make_user(id_number=OFFICE_ID, role=Role.OFFICE)
        self.client.force_authenticate(user=self.office)

    def test_office_creates_client_company(self):
        response = self.client.post(reverse('client-list'), {
            'name': 'Mizrahi Aluminium Works',
            'business_type': 'company',
            'tax_id': '514213456',
            'city': 'Netanya',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Client.objects.filter(tax_id='514213456').exists())

    def test_warehouse_may_look_up_clients_but_not_change_them(self):
        """The counter case: a warehouse worker taking an order needs to find
        the client and can add a missing one, but must not be able to edit or
        delete an existing company. This replaces an older test that asserted
        warehouse workers were locked out of clients entirely -- the view was
        deliberately opened up for the counter, and the test was left behind
        asserting a policy the code no longer had."""
        company = Client.objects.create(name='Counter co', tax_id='514213456')
        worker = make_user(role=Role.WAREHOUSE)
        self.client.force_authenticate(user=worker)

        self.assertEqual(self.client.get(reverse('client-list')).status_code,
                         status.HTTP_200_OK)
        self.assertEqual(
            self.client.post(reverse('client-list'), {'name': 'New at counter'}).status_code,
            status.HTTP_201_CREATED)

        # ...but the company's own record stays office-only.
        self.assertEqual(
            self.client.patch(reverse('client-detail', args=[company.pk]),
                              {'name': 'Renamed'}).status_code,
            status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.delete(reverse('client-detail', args=[company.pk])).status_code,
            status.HTTP_403_FORBIDDEN)

    def test_delete_deactivates_client(self):
        company = Client.objects.create(name='Mizrahi', tax_id='514213456')
        self.client.delete(reverse('client-detail', args=[company.pk]))
        company.refresh_from_db()
        self.assertFalse(company.is_active)
        self.assertTrue(Client.objects.filter(pk=company.pk).exists())


@override_settings(RELAXED_AUTH=True)
class ContactCreationTests(APITestCase):
    def setUp(self):
        self.manager = make_user(id_number=MANAGER_ID, role=Role.MANAGER)
        self.company = Client.objects.create(name='Mizrahi', tax_id='514213456')
        self.client.force_authenticate(user=self.manager)

    def payload(self, **overrides):
        data = {
            'first_name': 'Yosef', 'last_name': 'Mizrahi',
            'phone': '052-777-8899', 'client': self.company.pk,
            'password': '12345678',
        }
        data.update(overrides)
        return data

    def test_manager_creates_client_contact(self):
        response = self.client.post(
            reverse('staff-contacts'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        user = User.objects.get(phone_normalised='0527778899')
        self.assertEqual(user.role, Role.CLIENT)
        self.assertEqual(user.client, self.company)
        self.assertIsNone(user.id_number)
        self.assertTrue(user.must_change_password)

    def test_contact_signs_in_by_phone(self):
        self.client.post(reverse('staff-contacts'), self.payload(), format='json')
        user = User.objects.get(phone_normalised='0527778899')
        user.must_change_password = False
        user.save()

        self.client.force_authenticate(user=None)
        response = self.client.post(
            reverse('login'), {'identifier': '0527778899', 'password': '12345678'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['client']['tax_id'], '514213456')

    def test_duplicate_phone_rejected(self):
        self.client.post(reverse('staff-contacts'), self.payload(), format='json')
        response = self.client.post(
            reverse('staff-contacts'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# The forced password change
# ---------------------------------------------------------------------------


@override_settings(RELAXED_AUTH=True)
class PasswordChangeGateTests(APITestCase):
    def setUp(self):
        self.user = make_user(role=Role.OFFICE)
        self.user.must_change_password = True
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def test_blocked_from_the_rest_of_the_api(self):
        response = self.client.get('/api/catalog/listings/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_can_still_read_own_profile(self):
        self.assertEqual(
            self.client.get(reverse('me')).status_code, status.HTTP_200_OK
        )

    def test_changing_password_lifts_the_block(self):
        response = self.client.post(reverse('change-password'), {
            'current_password': 'str0ng-pass-9', 'password': 'my-own-pass-42',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertFalse(self.user.must_change_password)
        self.assertTrue(self.user.check_password('my-own-pass-42'))
        self.assertEqual(
            self.client.get('/api/catalog/listings/').status_code, status.HTTP_200_OK
        )

    def test_wrong_current_password_rejected(self):
        response = self.client.post(reverse('change-password'), {
            'current_password': 'not-it', 'password': 'my-own-pass-42',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.must_change_password)

    def test_unflagged_user_is_not_blocked(self):
        self.user.must_change_password = False
        self.user.save()
        self.assertEqual(
            self.client.get('/api/catalog/listings/').status_code, status.HTTP_200_OK
        )


# ---------------------------------------------------------------------------
# Testing-phase switch
# ---------------------------------------------------------------------------


class RelaxedAuthTests(APITestCase):
    def _create_worker(self):
        manager = make_user(id_number=MANAGER_ID, role=Role.MANAGER)
        self.client.force_authenticate(user=manager)
        return self.client.post(reverse('staff-list'), {
            'id_number': '12345678', 'first_name': 'Test', 'last_name': 'Worker',
            'phone': '050-0000001', 'role': Role.WAREHOUSE, 'password': '12345678',
        })

    @override_settings(RELAXED_AUTH=True)
    def test_arbitrary_id_and_weak_password_accepted(self):
        self.assertEqual(self._create_worker().status_code, status.HTTP_201_CREATED)

    @override_settings(RELAXED_AUTH=False)
    def test_strict_mode_rejects_both(self):
        response = self._create_worker()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('id_number', response.data)

    def test_deploy_check_blocks_relaxed_auth_in_production(self):
        from core.checks import relaxed_auth_not_in_production

        with self.settings(RELAXED_AUTH=True, DEBUG=False):
            self.assertEqual(len(relaxed_auth_not_in_production(None)), 1)
        with self.settings(RELAXED_AUTH=True, DEBUG=True):
            self.assertEqual(relaxed_auth_not_in_production(None), [])
        with self.settings(RELAXED_AUTH=False, DEBUG=False):
            self.assertEqual(relaxed_auth_not_in_production(None), [])


@override_settings(RELAXED_AUTH=True)
class ApiValidationErrorTests(APITestCase):
    """Model-level failures must reach the client as field errors, not 500s."""

    def setUp(self):
        self.manager = make_user(id_number=MANAGER_ID, role=Role.MANAGER)
        self.client.force_authenticate(user=self.manager)
        self.payload = {
            'id_number': OTHER_ID, 'first_name': 'Sami', 'last_name': 'Haddad',
            'phone': '052-1112222', 'role': Role.DRIVER, 'password': '12345678',
        }

    def test_duplicate_id_returns_400_not_500(self):
        self.assertEqual(
            self.client.post(reverse('staff-list'), self.payload).status_code,
            status.HTTP_201_CREATED,
        )
        response = self.client.post(reverse('staff-list'), self.payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('id_number', response.data)

    def test_duplicate_phone_returns_400_not_500(self):
        self.client.post(reverse('staff-list'), self.payload)
        response = self.client.post(reverse('staff-list'), dict(
            self.payload, id_number=make_id('99887766')
        ))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_phone_error_names_a_real_field(self):
        """The error must attach to `phone`, not the internal lookup column."""
        self.client.post(reverse('staff-list'), self.payload)
        response = self.client.post(reverse('staff-list'), dict(
            self.payload, id_number=make_id('99887766')
        ))
        self.assertIn('phone', response.data)
        self.assertNotIn('phone_normalised', response.data)


@override_settings(RELAXED_AUTH=True)
class DashboardTests(APITestCase):
    def setUp(self):
        self.manager = make_user(id_number=MANAGER_ID, role=Role.MANAGER)
        make_user(id_number=OTHER_ID, role=Role.DRIVER)
        make_user(id_number=OFFICE_ID, role=Role.OFFICE, is_active=False)
        Client.objects.create(name='Galil', tax_id='515998877')
        self.client.force_authenticate(user=self.manager)

    def test_dashboard_reports_real_and_pending_areas(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        self.assertTrue(data['workers']['available'])
        self.assertEqual(data['workers']['total'], 3)      # manager + driver + inactive office
        self.assertEqual(data['workers']['active'], 2)     # inactive office excluded
        self.assertTrue(data['clients']['available'])
        self.assertEqual(data['clients']['total'], 1)
        self.assertTrue(data['catalog']['available'])

        # Stock now reports real figures (items and reorder alerts).
        self.assertTrue(data['stock']['available'])
        self.assertIn('alerts', data['stock'])
        # Orders not built yet — must say so rather than report a misleading zero.
        self.assertFalse(data['orders']['available'])

    def test_online_count_reflects_presence(self):
        from django.utils import timezone
        # Nobody has a last_seen yet.
        self.assertEqual(self.client.get(reverse('dashboard')).data['workers']['online'], 0)

        self.manager.last_seen = timezone.now()
        self.manager.save(update_fields=['last_seen'])
        self.assertEqual(self.client.get(reverse('dashboard')).data['workers']['online'], 1)

    def test_online_workers_list(self):
        from django.utils import timezone
        self.manager.last_seen = timezone.now()
        self.manager.save(update_fields=['last_seen'])
        response = self.client.get(reverse('dashboard-online'))
        names = [u['full_name'] for u in response.data]
        self.assertIn(self.manager.full_name, names)

    def test_clients_cannot_see_dashboard(self):
        company = Client.objects.create(name='X', tax_id='1')
        contact = User.objects.create_client_user(
            phone='052-000000', password='pw', first_name='C', last_name='C',
            client=company,
        )
        self.client.force_authenticate(user=contact)
        self.assertEqual(
            self.client.get(reverse('dashboard')).status_code, status.HTTP_403_FORBIDDEN
        )


@override_settings(RELAXED_AUTH=True)
class PresenceTrackingTests(APITestCase):
    def test_request_records_last_seen(self):
        user = make_user(role=Role.OFFICE)
        self.assertIsNone(user.last_seen)

        token = self.client.post(
            reverse('login'), {'identifier': VALID_ID, 'password': 'str0ng-pass-9'}
        ).data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.client.get(reverse('me'))

        user.refresh_from_db()
        self.assertIsNotNone(user.last_seen)
        self.assertTrue(user.is_online)


@override_settings(RELAXED_AUTH=True)
class AttendanceTests(APITestCase):
    def _auth(self, user):
        token = self.client.post(
            reverse('login'),
            {'identifier': user.id_number, 'password': 'str0ng-pass-9'},
        ).data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def test_clock_in_out_cycle(self):
        user = make_user(role=Role.WAREHOUSE)
        self._auth(user)

        # No open shift yet.
        self.assertIsNone(self.client.get('/api/attendance/current/').data)

        r = self.client.post('/api/attendance/clock_in/')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertTrue(r.data['is_open'])

        # A second clock-in is refused.
        self.assertEqual(
            self.client.post('/api/attendance/clock_in/').status_code,
            status.HTTP_400_BAD_REQUEST)

        # current now returns the open shift.
        self.assertTrue(self.client.get('/api/attendance/current/').data['is_open'])

        out = self.client.post('/api/attendance/clock_out/')
        self.assertEqual(out.status_code, status.HTTP_200_OK)
        self.assertFalse(out.data['is_open'])
        self.assertIsNotNone(out.data['clock_out'])

        # Clocking out again is refused.
        self.assertEqual(
            self.client.post('/api/attendance/clock_out/').status_code,
            status.HTTP_400_BAD_REQUEST)

    def test_worker_sees_only_own_shifts(self):
        me = make_user(id_number=make_id('11111118'), role=Role.WAREHOUSE)
        other = make_user(id_number=make_id('22222226'), role=Role.WAREHOUSE)
        from core.models import Shift
        from django.utils import timezone
        Shift.objects.create(worker=other, clock_in=timezone.now())
        self._auth(me)
        rows = self.client.get('/api/attendance/').data
        results = rows.get('results', rows) if isinstance(rows, dict) else rows
        self.assertEqual(len(results), 0)

    def test_clients_cannot_clock_in(self):
        from core.models import Client
        client_co = Client.objects.create(name='ACME')
        user = User.objects.create_client_user(
            phone='050-9998887', password='str0ng-pass-9', client=client_co,
            first_name='C', last_name='C')
        token = self.client.post(
            reverse('login'),
            {'identifier': '0509998887', 'password': 'str0ng-pass-9'}).data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(
            self.client.post('/api/attendance/clock_in/').status_code,
            status.HTTP_403_FORBIDDEN)


@override_settings(RELAXED_AUTH=True)
class PayrollTests(APITestCase):
    def _shift(self, worker, day, hours):
        from datetime import datetime, timedelta
        from django.utils import timezone
        from core.models import Shift
        start = timezone.make_aware(datetime(2026, 8, day, 6, 0))
        Shift.objects.create(worker=worker, clock_in=start,
                             clock_out=start + timedelta(hours=hours))

    def test_israeli_overtime_split(self):
        from decimal import Decimal
        from core.models import PayBasis, Shift
        from core.payroll import compute_payroll
        w = make_user(role=Role.WAREHOUSE)
        w.pay_basis = PayBasis.HOURLY
        w.hourly_rate = Decimal('50')
        w.overtime_enabled = True
        w.daily_regular_hours = Decimal('8')
        w.save()
        self._shift(w, 3, 11)   # 8 reg + 2@125 + 1@150
        self._shift(w, 4, 8)    # 8 reg
        d = compute_payroll(w, Shift.objects.filter(worker=w))
        self.assertEqual(d['regular_hours'], 16.0)
        self.assertEqual(d['overtime_125_hours'], 2.0)
        self.assertEqual(d['overtime_150_hours'], 1.0)
        self.assertEqual(d['total_pay'], 1000.0)   # 800 + 125 + 75

    def test_overtime_toggle_off_pays_flat(self):
        from decimal import Decimal
        from core.models import PayBasis, Shift
        from core.payroll import compute_payroll
        w = make_user(role=Role.WAREHOUSE)
        w.pay_basis = PayBasis.HOURLY
        w.hourly_rate = Decimal('50')
        w.overtime_enabled = False
        w.save()
        self._shift(w, 3, 11)
        d = compute_payroll(w, Shift.objects.filter(worker=w))
        self.assertEqual(d['overtime_125_hours'], 0.0)
        self.assertEqual(d['total_pay'], 550.0)    # 11h flat * 50

    def test_monthly_overtime_uses_182_divisor(self):
        from decimal import Decimal
        from core.models import PayBasis, Shift
        from core.payroll import compute_payroll
        w = make_user(role=Role.WAREHOUSE)
        w.pay_basis = PayBasis.MONTHLY
        w.monthly_salary = Decimal('9100')   # /182 = 50/hr
        w.overtime_enabled = True
        w.save()
        self._shift(w, 3, 11)   # 2@125 + 1@150 overtime
        d = compute_payroll(w, Shift.objects.filter(worker=w))
        self.assertEqual(d['hourly_rate'], 50.0)
        self.assertEqual(d['overtime_pay'], 200.0)
        self.assertEqual(d['total_pay'], 9300.0)


@override_settings(RELAXED_AUTH=True)
class PayslipTests(APITestCase):
    def _auth(self, user):
        token = self.client.post(
            reverse('login'),
            {'identifier': user.id_number, 'password': 'str0ng-pass-9'}).data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def _shift(self, worker, day, hours):
        from datetime import datetime, timedelta
        from django.utils import timezone
        from core.models import Shift
        start = timezone.make_aware(datetime(2026, 8, day, 6, 0))
        Shift.objects.create(worker=worker, clock_in=start,
                             clock_out=start + timedelta(hours=hours))

    def test_generate_edit_finalise_lock(self):
        from decimal import Decimal
        from core.models import PayBasis, Payslip
        mgr = make_user(id_number=make_id('11111118'), role=Role.MANAGER)
        w = make_user(id_number=make_id('22222226'), role=Role.WAREHOUSE)
        w.pay_basis = PayBasis.HOURLY
        w.hourly_rate = Decimal('55')
        w.overtime_enabled = True
        w.daily_regular_hours = Decimal('8')
        w.save()
        self._shift(w, 3, 9)   # 8 reg + 1 ot125
        self._shift(w, 4, 8)   # 8 reg
        self._auth(mgr)

        gen = self.client.post('/api/payslips/generate/',
                               {'worker': w.id, 'year': 2026, 'month': 8})
        self.assertEqual(gen.status_code, status.HTTP_201_CREATED)
        pid = gen.data['id']
        self.assertEqual(float(gen.data['regular_hours']), 16.0)
        self.assertEqual(float(gen.data['overtime_125_hours']), 1.0)
        self.assertEqual(float(gen.data['total_pay']), 16 * 55 + 1 * 55 * 1.25)

        # a second generate for the same period is refused
        self.assertEqual(
            self.client.post('/api/payslips/generate/',
                             {'worker': w.id, 'year': 2026, 'month': 8}).status_code,
            status.HTTP_400_BAD_REQUEST)

        # add a bonus adjustment
        edited = self.client.patch(
            f'/api/payslips/{pid}/',
            {'adjustments': [{'label': 'Bonus', 'amount': '100.00'}]}, format='json')
        self.assertEqual(float(edited.data['adjustments_total']), 100.0)

        # finalise locks editing
        self.assertEqual(
            self.client.post(f'/api/payslips/{pid}/finalise/').data['status'], 'final')
        self.assertEqual(
            self.client.patch(f'/api/payslips/{pid}/', {'note': 'x'}, format='json').status_code,
            status.HTTP_400_BAD_REQUEST)

    def test_worker_sees_only_own_finalised(self):
        from core.models import Payslip
        w1 = make_user(id_number=make_id('33333334'), role=Role.WAREHOUSE)
        w2 = make_user(id_number=make_id('44444442'), role=Role.WAREHOUSE)
        Payslip.objects.create(worker=w1, year=2026, month=7, status='final')
        Payslip.objects.create(worker=w1, year=2026, month=8, status='draft')
        Payslip.objects.create(worker=w2, year=2026, month=7, status='final')
        self._auth(w1)
        rows = self.client.get('/api/payslips/').data
        results = rows.get('results', rows) if isinstance(rows, dict) else rows
        self.assertEqual(len(results), 1)  # only w1's finalised slip


class MapLinkTests(TestCase):
    """Coordinates out of links people actually share.

    Every case is a real URL shape from Google Maps, Waze, Apple Maps or OSM.
    resolve=False throughout: these must parse without touching the network,
    and only genuinely short links should ever need a redirect.
    """

    def assertPoint(self, url, lat, lng):
        point = maplinks.extract_coordinates(url, resolve=False)
        self.assertIsNotNone(point, f'no coordinate found in {url}')
        self.assertAlmostEqual(float(point[0]), lat, places=4, msg=url)
        self.assertAlmostEqual(float(point[1]), lng, places=4, msg=url)

    def test_google_place_pin_beats_viewport(self):
        # !3d/!4d is the pin; @ is where the camera sat. They differ, and the
        # pin is the one a driver wants.
        self.assertPoint(
            'https://www.google.com/maps/place/X/@32.0800,34.7800,17z/'
            'data=!3m1!4b1!4m5!3m4!1s0x0:0x0!8m2!3d32.0853!4d34.7818',
            32.0853, 34.7818)

    def test_google_viewport_only(self):
        self.assertPoint('https://www.google.com/maps/@32.0853,34.7818,15z',
                         32.0853, 34.7818)

    def test_google_query_forms(self):
        self.assertPoint('https://maps.google.com/?q=32.0853,34.7818',
                         32.0853, 34.7818)
        self.assertPoint('https://www.google.com/maps?q=loc:32.0853,34.7818',
                         32.0853, 34.7818)

    def test_waze(self):
        self.assertPoint('https://waze.com/ul?ll=32.0853,34.7818&navigate=yes',
                         32.0853, 34.7818)
        self.assertPoint(
            'https://www.waze.com/live-map/directions?to=ll.32.0853%2C34.7818',
            32.0853, 34.7818)

    def test_apple(self):
        self.assertPoint('https://maps.apple.com/?ll=32.0853,34.7818',
                         32.0853, 34.7818)
        self.assertPoint('https://maps.apple.com/?daddr=32.0853,34.7818',
                         32.0853, 34.7818)

    def test_openstreetmap_and_geo_uri(self):
        self.assertPoint(
            'https://www.openstreetmap.org/?mlat=32.0853&mlon=34.7818#map=19/32/34',
            32.0853, 34.7818)
        self.assertPoint('geo:32.0853,34.7818', 32.0853, 34.7818)

    def test_negative_and_southern_coordinates(self):
        self.assertPoint('https://maps.google.com/?q=-33.8688,151.2093',
                         -33.8688, 151.2093)

    def test_rejects_links_without_a_location(self):
        for url in ('https://example.com/', 'not a url', '',
                    'https://www.google.com/maps/search/aluminium'):
            self.assertIsNone(maplinks.extract_coordinates(url, resolve=False),
                              f'should not have found a point in {url!r}')

    def test_rejects_null_island_and_out_of_range(self):
        # 0,0 means a parse went wrong, not a delivery in the Atlantic.
        self.assertIsNone(
            maplinks.extract_coordinates('https://maps.google.com/?q=0,0',
                                         resolve=False))
        self.assertIsNone(
            maplinks.extract_coordinates('https://maps.google.com/?q=99.5,200.1',
                                         resolve=False))

    def test_short_links_are_recognised_as_needing_expansion(self):
        self.assertTrue(maplinks._is_short('https://maps.app.goo.gl/abc123'))
        self.assertTrue(maplinks._is_short('https://waze.com/ul/hsv8v8xyz'))
        # Already carries the point, so no network call is needed.
        self.assertFalse(maplinks._is_short('https://waze.com/ul?ll=32.08,34.78'))


class ClientLocationLinkTests(APITestCase):
    """The link is what the office pastes; coordinates are what it produces."""

    def setUp(self):
        self.office = User.objects.create_user(
            id_number=make_id('11111111'), password='Str0ng!Passw0rd',
            first_name='O', last_name='F', role=Role.OFFICE, phone='050-1112233')
        self.client.force_authenticate(self.office)

    def test_pasting_a_link_sets_the_delivery_point(self):
        r = self.client.post('/api/clients/', {
            'name': 'Yard client',
            'location_url': 'https://maps.google.com/?q=32.0853,34.7818',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertAlmostEqual(float(r.data['latitude']), 32.0853, places=4)
        self.assertAlmostEqual(float(r.data['longitude']), 34.7818, places=4)

    def test_a_link_with_no_location_is_refused(self):
        r = self.client.post('/api/clients/', {
            'name': 'Bad link', 'location_url': 'https://example.com/hello',
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('location_url', r.data)

    def test_clearing_the_link_clears_the_point(self):
        created = self.client.post('/api/clients/', {
            'name': 'Clearable',
            'location_url': 'https://waze.com/ul?ll=32.0853,34.7818',
        }, format='json').data
        r = self.client.patch(f"/api/clients/{created['id']}/",
                              {'location_url': ''}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(r.data['latitude'])
        self.assertIsNone(r.data['longitude'])


class DeliverySignatureTests(APITestCase):
    """Who signed, and how the note reaches them afterwards."""

    def setUp(self):
        self.driver = User.objects.create_user(
            id_number=make_id('22222222'), password='Str0ng!Passw0rd',
            first_name='D', last_name='R', role=Role.DRIVER, phone='050-4445566')
        self.office = User.objects.create_user(
            id_number=make_id('33333333'), password='Str0ng!Passw0rd',
            first_name='O', last_name='F', role=Role.OFFICE, phone='050-7778899')
        self.customer = Client.objects.create(
            name='Gate client', contact_name='Owner Person', phone='04-9001122')
        from core.models import Order, OrderStatus
        self.order = Order.objects.create(
            number='ORD-TEST-0001', client=self.customer,
            status=OrderStatus.OUT_FOR_DELIVERY, created_by=self.office)

    def _sign(self, **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile
        # A 1x1 PNG is enough; the image itself is not what is under test.
        png = SimpleUploadedFile(
            'sig.png',
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00'
            b'\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc'
            b'\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82',
            content_type='image/png')
        self.client.force_authenticate(self.driver)
        payload = {'recipient_name': 'Gate Man', 'signature': png}
        payload.update(extra)
        return self.client.post(
            f'/api/orders/{self.order.id}/sign_delivery/',
            payload, format='multipart')

    def test_signing_stores_the_recipients_phone(self):
        r = self._sign(recipient_phone='052-1234567')
        self.assertEqual(r.status_code, 200, r.data)
        from core.models import Delivery
        delivery = Delivery.objects.get(order=self.order)
        self.assertEqual(delivery.recipient_name, 'Gate Man')
        self.assertEqual(delivery.recipient_phone, '052-1234567')

    def test_sign_response_carries_both_recipients(self):
        r = self._sign(recipient_phone='052-1234567')
        # The person who signed...
        self.assertEqual(r.data['recipient_phone'], '052-1234567')
        self.assertEqual(r.data['recipient_name'], 'Gate Man')
        # ...and the client's own contact, so the app can offer both sends.
        self.assertEqual(r.data['client_phone'], '04-9001122')
        self.assertEqual(r.data['client_contact_name'], 'Owner Person')
        self.assertTrue(r.data['public_url'].endswith('/'))

    def test_recipient_survives_for_a_later_resend(self):
        """The driver must be able to re-send days later, from a fresh app."""
        self._sign(recipient_phone='052-1234567')
        self.client.force_authenticate(self.driver)
        rows = self.client.get('/api/orders/deliveries/?done=true').data
        rows = rows.get('results', rows) if isinstance(rows, dict) else rows
        row = next(r for r in rows if r['id'] == self.order.id)
        self.assertEqual(row['recipient_phone'], '052-1234567')
        self.assertEqual(row['recipient_name'], 'Gate Man')

    def test_signing_without_a_phone_is_still_allowed(self):
        # A signature with no phone must not block the delivery being recorded.
        r = self._sign()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['recipient_phone'], '')


class MapboxTokenTests(APITestCase):
    """The token can come from Settings or the environment; env wins."""

    def setUp(self):
        from core.models import AppConfig
        self.cfg = AppConfig.get()
        self.manager = User.objects.create_user(
            id_number=make_id('44444444'), password='Str0ng!Passw0rd',
            first_name='M', last_name='G', role=Role.MANAGER, phone='050-1010101')
        self.client.force_authenticate(self.manager)

    def test_token_saved_in_settings_reaches_the_map(self):
        r = self.client.patch('/api/settings/',
                              {'mapbox_token': 'pk.from_settings'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        # Blank the environment: this developer's .env carries a real token,
        # and an env value legitimately outranks Settings, which would mask
        # whether the saved value works at all.
        with mock.patch.dict(os.environ, {'MAPBOX_TOKEN': ''}):
            # /api/config/ is what the apps read to draw the map.
            self.assertEqual(self.client.get('/api/config/').data['mapbox_token'],
                             'pk.from_settings')

    def test_environment_overrides_the_saved_value(self):
        self.cfg.mapbox_token = 'pk.from_settings'
        self.cfg.save(update_fields=['mapbox_token'])
        with mock.patch.dict(os.environ, {'MAPBOX_TOKEN': 'pk.from_railway'}):
            self.assertEqual(self.client.get('/api/config/').data['mapbox_token'],
                             'pk.from_railway')
            data = self.client.get('/api/settings/').data
            # The page must say the value is managed outside the app, and show
            # which token is actually in force.
            self.assertTrue(data['mapbox_from_env'])
            self.assertEqual(data['mapbox_effective'], 'pk.from_railway')

    def test_without_either_the_token_is_empty_not_an_error(self):
        with mock.patch.dict(os.environ, {'MAPBOX_TOKEN': ''}):
            self.assertEqual(
                self.client.get('/api/config/').data['mapbox_token'], '')


class ClientPinVersusLinkTests(APITestCase):
    """A pasted link and a dragged pin must not fight each other."""

    def setUp(self):
        self.office = User.objects.create_user(
            id_number=make_id('55555555'), password='Str0ng!Passw0rd',
            first_name='O', last_name='F', role=Role.OFFICE, phone='050-2223344')
        self.client.force_authenticate(self.office)
        self.created = self.client.post('/api/clients/', {
            'name': 'Pin client',
            'location_url': 'https://maps.google.com/?q=32.0853,34.7818',
        }, format='json').data

    def test_dragging_the_pin_survives_a_save_that_resends_the_same_link(self):
        # The desktop posts the whole form back, link included, after a drag.
        r = self.client.patch(f"/api/clients/{self.created['id']}/", {
            'location_url': self.created['location_url'],
            'latitude': '32.100000', 'longitude': '34.800000',
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertAlmostEqual(float(r.data['latitude']), 32.1, places=4)
        self.assertAlmostEqual(float(r.data['longitude']), 34.8, places=4)

    def test_a_new_link_moves_the_pin(self):
        r = self.client.patch(f"/api/clients/{self.created['id']}/", {
            'location_url': 'https://maps.google.com/?q=31.7683,35.2137',
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertAlmostEqual(float(r.data['latitude']), 31.7683, places=4)
        self.assertAlmostEqual(float(r.data['longitude']), 35.2137, places=4)


class TranslationTests(APITestCase):
    """API messages come back in the caller's language.

    The catalogue is deliberately partial -- the messages a user is shown are
    translated, field labels are not -- so this pins the part that matters and
    would catch the .mo files going missing from a deploy.
    """

    def setUp(self):
        self.worker = User.objects.create_user(
            id_number=make_id('66666666'), password='Str0ng!Passw0rd',
            first_name='W', last_name='H', role=Role.WAREHOUSE, phone='050-9998877')
        self.client.force_authenticate(self.worker)
        from core.models import Order, OrderStatus
        self.order = Order.objects.create(
            number='ORD-L10N-0001', client=Client.objects.create(name='L10n co'),
            status=OrderStatus.READY, created_by=self.worker)

    def _error_in(self, language):
        r = self.client.post(f'/api/orders/{self.order.id}/line_action/',
                             {'line_id': 999999}, format='json',
                             HTTP_ACCEPT_LANGUAGE=language)
        self.assertEqual(r.status_code, 400)
        return str(r.data['line_id'][0])

    def test_english_is_the_source_text(self):
        self.assertEqual(self._error_in('en'), 'Unknown line.')

    def test_hebrew_and_arabic_are_translated(self):
        hebrew = self._error_in('he')
        arabic = self._error_in('ar')
        self.assertEqual(hebrew, 'שורה לא מוכרת.')
        self.assertEqual(arabic, 'سطر غير معروف.')
        # And genuinely different from each other and from English.
        self.assertNotEqual(hebrew, arabic)
        self.assertNotIn('Unknown', hebrew)


class StockCannotGoNegativeTests(APITestCase):
    """A shelf cannot hold less than nothing.

    Found by testing the app: picking 999 from a shelf holding 6 was accepted
    and left the ledger at -993, which then fed the shortage flags on order
    picking.
    """

    def setUp(self):
        from core.models import Location, Profile, StockItem, Warehouse
        self.staff = User.objects.create_user(
            id_number=make_id('77777777'), password='Str0ng!Passw0rd',
            first_name='S', last_name='K', role=Role.WAREHOUSE, phone='050-3334455')
        self.client.force_authenticate(self.staff)
        warehouse = Warehouse.objects.create(name='Test WH')
        location = Location.objects.create(warehouse=warehouse, code='T-1')
        self.item = StockItem.objects.create(
            profile=Profile.objects.first(), location=location, length_mm=6000)

    def _move(self, mtype, qty):
        return self.client.post(f'/api/stock/{self.item.id}/move/',
                                {'movement_type': mtype, 'quantity': qty},
                                format='json')

    def test_picking_more_than_is_there_is_refused(self):
        self._move('receipt', 6)
        r = self._move('pick', 999)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('6', str(r.data['quantity'][0]))   # tells them what is there
        self.assertEqual(self.item.quantity, 6)          # and nothing moved

    def test_picking_exactly_what_is_there_is_allowed(self):
        self._move('receipt', 6)
        self.assertEqual(self._move('pick', 6).status_code, 200)
        self.assertEqual(self.item.quantity, 0)

    def test_an_adjustment_cannot_go_below_zero_either(self):
        self._move('receipt', 3)
        r = self._move('adjustment', -10)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.item.quantity, 3)

    def test_receiving_is_never_blocked(self):
        self.assertEqual(self._move('receipt', 500).status_code, 200)
        self.assertEqual(self.item.quantity, 500)
