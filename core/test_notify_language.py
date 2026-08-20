# -*- coding: utf-8 -*-
"""Each reader is notified in their own language, not the sender's.

The strings are built inside the request of whoever caused the event, so
without per-recipient rendering a manager working in English would send
English notifications to a warehouse worker who reads Hebrew.
"""

from django.test import TestCase
from django.utils import translation

from core import notify
from core.models import Client, Notification, Order, Role
from core.tests import make_id, make_user


class NotificationLanguageTests(TestCase):
    def setUp(self):
        self.hebrew_reader = make_user(make_id('11111111'), role=Role.OFFICE,
                                       language='he')
        self.arabic_reader = make_user(make_id('22222222'), role=Role.OFFICE,
                                       language='ar')
        self.buyer = Client.objects.create(name='ויטרינות פלוס')
        self.order = Order.objects.create(number='ORD-2026-0050',
                                          client=self.buyer,
                                          created_by=self.hebrew_reader)

    def test_each_reader_gets_their_own_language(self):
        # An English request, as when a manager working in English orders.
        with translation.override('en'):
            notify.order_created(self.order)

        hebrew = Notification.objects.get(user=self.hebrew_reader)
        arabic = Notification.objects.get(user=self.arabic_reader)
        self.assertEqual(hebrew.title, 'הזמנה חדשה')
        self.assertEqual(arabic.title, 'طلب جديد')
        # The order number survives translation in both.
        self.assertIn('ORD-2026-0050', hebrew.body)
        self.assertIn('ORD-2026-0050', arabic.body)
        self.assertIn('עבור', hebrew.body)

    def test_sender_language_does_not_leak(self):
        with translation.override('ar'):
            notify.order_created(self.order)
        self.assertEqual(
            Notification.objects.get(user=self.hebrew_reader).title,
            'הזמנה חדשה')
