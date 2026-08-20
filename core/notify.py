"""What the business notifies, and who it tells.

Every notification the system sends is defined here rather than scattered
through the views, so the set can be read in one sitting and a message can be
reworded without hunting for it.

Two rules run through all of it:

  Nobody is told what they just did. The person who tapped the button can see
  the result on their screen; a notification for it is noise, and noise is how
  people learn to ignore notifications.

  Nothing here can fail the work it describes. push.send_* swallows its own
  errors, and these functions are called after the record is saved -- a phone
  that is off must never roll back a delivery.
"""

# Lazy, not gettext: these strings are built inside the request of whoever
# tapped the button, but they are read by somebody else, possibly in another
# language. Translating eagerly here would render every notification in the
# sender's language. push renders each one per recipient instead.
from django.utils.functional import lazy
from django.utils.translation import gettext_lazy as _

from core import push
from core.models import OrderStatus, Role


def _fmt(template, **params):
    """Translate and fill in a message when it is read, not when it is sent.

    `gettext_lazy(...) % params` is not lazy: the % operator casts the proxy
    to str there and then, in the sender's language. This keeps both the
    translation and the interpolation pending until push renders the message
    for a particular recipient.
    """
    return lazy(lambda: str(template) % params, str)()


# Who watches the floor without standing on it.
BACK_OFFICE = [Role.OFFICE, Role.MANAGER]


def _client_of(order):
    return order.client.name if order.client else _('a client')


# -- attendance ---------------------------------------------------------

def worker_clocked_in(worker):
    """The office cannot see who walked in; the worker plainly can."""
    push.send_to_roles(
        BACK_OFFICE,
        _('Clocked in'),
        _fmt(_('%(worker)s started work.'), worker=worker.full_name),
        {'kind': 'clock_in', 'worker_id': worker.id},
        exclude=worker,
    )


def worker_clocked_out(worker, hours=None):
    body = (_fmt(_('%(worker)s finished work (%(hours)s h).'),
                 worker=worker.full_name, hours=hours)
            if hours is not None else
            _fmt(_('%(worker)s finished work.'), worker=worker.full_name))
    push.send_to_roles(
        BACK_OFFICE,
        _('Clocked out'),
        body,
        {'kind': 'clock_out', 'worker_id': worker.id},
        exclude=worker,
    )


# -- orders -------------------------------------------------------------

def order_created(order, by=None):
    """A new order is work for the warehouse, and news for the office."""
    push.send_to_roles(
        [Role.WAREHOUSE] + BACK_OFFICE,
        _('New order'),
        _fmt(_('%(number)s for %(client)s.'),
             number=order.number, client=_client_of(order)),
        {'kind': 'order_created', 'order_id': order.id},
        exclude=by,
    )


# Each step of an order's journey: who needs telling, and what it says.
# Keyed by the status being entered.
_ORDER_STEPS = {
    OrderStatus.PICKING: (
        BACK_OFFICE,
        lambda o: (_('Order being loaded'),
                   _fmt(_('%(number)s for %(client)s is being picked.'),
                        number=o.number, client=_client_of(o))),
    ),
    OrderStatus.READY: (
        [Role.DRIVER] + BACK_OFFICE,
        lambda o: (_('Order loaded'),
                   _fmt(_('%(number)s for %(client)s is ready to load.'),
                        number=o.number, client=_client_of(o))),
    ),
    OrderStatus.OUT_FOR_DELIVERY: (
        BACK_OFFICE,
        lambda o: (_('On the way'),
                   _fmt(_('%(number)s is on its way to %(client)s.'),
                        number=o.number, client=_client_of(o))),
    ),
    OrderStatus.DELIVERED: (
        BACK_OFFICE,
        lambda o: (_('Arrived'),
                   _fmt(_('%(number)s was delivered to %(client)s.'),
                        number=o.number, client=_client_of(o))),
    ),
    OrderStatus.CANCELLED: (
        [Role.WAREHOUSE, Role.DRIVER] + BACK_OFFICE,
        lambda o: (_('Order cancelled'),
                   _fmt(_('%(number)s for %(client)s was cancelled.'),
                        number=o.number, client=_client_of(o))),
    ),
}


def order_step(order, new_status, by=None):
    """Announce a step in an order's journey, if that step is worth announcing.

    Only on entering the status, never on a save that leaves it unchanged --
    the caller compares before and after. Draft, submitted and confirmed are
    absent on purpose: they are office paperwork, and nobody on the floor
    needs waking for them.
    """
    step = _ORDER_STEPS.get(new_status)
    if step is None:
        return
    roles, message = step
    title, body = message(order)
    push.send_to_roles(
        roles, title, body,
        {'kind': 'order_step', 'order_id': order.id, 'status': new_status},
        exclude=by,
    )


def delivery_signed(order, delivery, by=None):
    """Arrival, with the name of whoever actually took the goods.

    Separate from the DELIVERED step above because this one can say who
    signed, which is the part the office wants.
    """
    push.send_to_roles(
        BACK_OFFICE,
        _('Delivery signed'),
        _fmt(_('%(number)s was received by %(person)s.'),
             number=order.number,
             person=delivery.recipient_name or _('the client')),
        {'kind': 'delivery_signed', 'order_id': order.id},
        exclude=by,
    )
