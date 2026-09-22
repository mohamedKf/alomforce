"""Legal invoicing through Green Invoice.

Two endpoints: one that says whether the button should show at all, and one
behind the button. Kept out of views.py so the accountant's-book invoices
(InvoiceViewSet) and the legally issued ones do not share a file that is
already long; the row they produce is the same Invoice model either way.

The URL patterns live at the bottom of this module so backend/urls.py needs a
single `include` line rather than an import block plus routes.
"""

import logging

from django.db import transaction
from django.urls import path
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core import greeninvoice
from core.models import AppConfig, Invoice, Order
from core.serializers import InvoiceSerializer
from core.views import BASE, IsOffice, store_pdf

logger = logging.getLogger(__name__)


class InvoicingStatusView(APIView):
    """GET /api/invoicing/status/ -> {ready, sandbox}.

    `ready` decides whether the apps show "Issue invoice"; `sandbox` lets them
    label it, because a sandbox invoice looks real and must not be handed to a
    customer.
    """

    permission_classes = BASE + [IsOffice]

    def get(self, request):
        return Response({
            'ready': AppConfig.get().greeninvoice_ready,
            'sandbox': greeninvoice.is_sandbox(),
        })


class IssueInvoiceView(APIView):
    """POST /api/orders/<id>/issue_invoice/ -- raise the order's legal invoice.

    Body: {kind: "invoice" | "invoice_receipt",
           payment: {type, date, cheque_number}   (invoice_receipt only),
           email_client: bool}

    Refuses anything that would produce a wrong legal document: an order with
    no lines, a line nobody has priced (it would invoice the aluminium at
    zero), or an order that already has a generated invoice (Green Invoice
    would happily number a second one, and cancelling it is a credit note and
    a phone call). The rest -- partial invoicing, manual invoices alongside --
    is the office's business and is not second-guessed here.
    """

    permission_classes = BASE + [IsOffice]

    @staticmethod
    def _refuse(message, code=status.HTTP_400_BAD_REQUEST):
        return Response({'detail': message}, status=code)

    def post(self, request, pk):
        try:
            order = Order.objects.select_related('client').get(pk=pk)
        except Order.DoesNotExist:
            return self._refuse(_('Order not found.'), status.HTTP_404_NOT_FOUND)

        kind = request.data.get('kind') or 'invoice'
        if kind not in greeninvoice.KINDS:
            return self._refuse(_('Choose "invoice" or "invoice_receipt".'))
        payment = request.data.get('payment') or None
        if payment is not None and not isinstance(payment, dict):
            return self._refuse(_('Payment must be an object.'))
        email_client = bool(request.data.get('email_client'))

        if not AppConfig.get().greeninvoice_ready:
            return self._refuse(
                _('Green Invoice is not set up: add the API key and secret in Settings.'))

        lines = list(order.lines.select_related('profile'))
        if not lines:
            return self._refuse(_('The order has no lines to invoice.'))
        unpriced = [line.profile.number for line in lines if line.needs_a_price]
        if unpriced:
            return self._refuse(
                _('Price every line before issuing an invoice (missing: %(lines)s).')
                % {'lines': ', '.join(unpriced)})
        existing = order.invoices.filter(direction=Invoice.Direction.INCOME,
                                         source=Invoice.Source.GENERATED).first()
        if existing:
            return self._refuse(
                _('Invoice %(number)s was already issued for this order.')
                % {'number': existing.number})

        try:
            client = greeninvoice.GreenInvoiceClient.from_config()
            issued = client.issue_tax_invoice(order, kind=kind, payment=payment,
                                              email_client=email_client)
        except greeninvoice.GreenInvoiceNotConfigured as exc:
            return self._refuse(str(exc))
        except greeninvoice.GreenInvoiceError as exc:
            return self._refuse(str(exc), status.HTTP_502_BAD_GATEWAY)

        # From here the legal document exists. Whatever goes wrong below must
        # be loud enough to reconcile by hand, never a silent second issue.
        try:
            invoice = self._record(order, kind, issued, request.user)
        except Exception:
            logger.exception('Green Invoice document %s (no. %s) issued for order %s '
                             'but not recorded', issued['external_id'],
                             issued['number'], order.number)
            raise

        return Response(InvoiceSerializer(invoice, context={'request': request}).data,
                        status=status.HTTP_201_CREATED)

    @staticmethod
    def _record(order, kind, issued, user):
        """Write the Invoice row and attach the PDF."""
        client = order.client
        paid = greeninvoice.KINDS[kind] == greeninvoice.TAX_INVOICE_RECEIPT
        with transaction.atomic():
            invoice = Invoice(
                direction=Invoice.Direction.INCOME,
                source=Invoice.Source.GENERATED,
                number=issued['number'],
                external_id=issued['external_id'],
                allocation_number=issued['allocation_number'],
                client=client,
                order=order,
                party_name=client.legal_name or client.name,
                party_tax_id=client.tax_id,
                subtotal=order.net,
                vat=order.vat_amount,
                total=order.total,
                amount_paid=order.total if paid else 0,
                issued_at=timezone.localdate(),
                notes=issued['url'],
                created_by=user,
            )
            invoice.sync_status()
            invoice.save()

        if issued['pdf_bytes']:
            # Storage is outside the transaction and best-effort: the number
            # and link are already saved, so the PDF can be fetched again.
            try:
                store_pdf(invoice, 'file', f'{invoice.number}_greeninvoice.pdf',
                          issued['pdf_bytes'])
            except Exception:                                 # noqa: BLE001
                logger.exception('Could not store the PDF for invoice %s',
                                 invoice.number)
        return invoice


# green invoice -- included from backend/urls.py
urlpatterns = [
    path('api/orders/<int:pk>/issue_invoice/', IssueInvoiceView.as_view(),
         name='order-issue-invoice'),
    path('api/invoicing/status/', InvoicingStatusView.as_view(),
         name='invoicing-status'),
]
