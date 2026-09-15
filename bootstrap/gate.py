"""Business write gate. A ready file cannot skip binding or the lease."""
from contracts.ports import check_binding

from .binding import require_complete


def assert_business_allowed(binding, lease, locks, loan=None):
    require_complete(binding)
    locks.assert_held(lease)
    if loan is not None:
        check_binding(binding, loan)
