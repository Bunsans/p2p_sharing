"""Pure calculations; these functions neither authorize nor transfer money."""

from datetime import timedelta
from typing import NamedTuple


class DepositSplit(NamedTuple):
    compensation_kopecks: int
    refund_kopecks: int


class RentalPrice(NamedTuple):
    owner_kopecks: int
    commission_kopecks: int
    total_kopecks: int


class RefundSplit(NamedTuple):
    owner_kopecks: int
    commission_kopecks: int


def _validate_kopecks(name: str, value: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer number of kopecks")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _round_half_up(numerator: int, denominator: int) -> int:
    whole, remainder = divmod(numerator, denominator)
    return whole + (1 if remainder * 2 >= denominator else 0)


def rental_price(owner_kopecks: int) -> RentalPrice:
    """Add the fixed 20% markup, rounding once to kopecks with HALF_UP."""
    _validate_kopecks("owner_kopecks", owner_kopecks)
    commission = _round_half_up(owner_kopecks, 5)
    return RentalPrice(owner_kopecks, commission, owner_kopecks + commission)


def split_rent_refund(
    owner_kopecks: int,
    commission_kopecks: int,
    refunded_before_kopecks: int,
    refund_kopecks: int,
) -> RefundSplit:
    """Allocate a new refund from the original shares, cumulatively.

    The cumulative owner share is rounded HALF_UP; the commission receives
    the remainder. All preceding refunds must follow this same rule.
    """
    for name, value in (
        ("owner_kopecks", owner_kopecks),
        ("commission_kopecks", commission_kopecks),
        ("refunded_before_kopecks", refunded_before_kopecks),
        ("refund_kopecks", refund_kopecks),
    ):
        _validate_kopecks(name, value)
    total = owner_kopecks + commission_kopecks
    if total == 0:
        raise ValueError("original rental price must be positive")
    refunded_after = refunded_before_kopecks + refund_kopecks
    if refunded_after > total:
        raise ValueError("cumulative refunds must not exceed the rental price")
    owner_before = _round_half_up(refunded_before_kopecks * owner_kopecks, total)
    owner_after = _round_half_up(refunded_after * owner_kopecks, total)
    owner_refund = owner_after - owner_before
    return RefundSplit(owner_refund, refund_kopecks - owner_refund)


def split_deposit(deposit_kopecks: int, compensation_kopecks: int) -> DepositSplit:
    """Split a deposit using an already approved compensation (ADR-0003)."""
    for name, value in (
        ("deposit_kopecks", deposit_kopecks),
        ("compensation_kopecks", compensation_kopecks),
    ):
        _validate_kopecks(name, value)
    if compensation_kopecks > deposit_kopecks:
        raise ValueError("compensation must not exceed the deposit")
    return DepositSplit(compensation_kopecks, deposit_kopecks - compensation_kopecks)


def additional_half_hours(total_additional_time: timedelta) -> int:
    """Round cumulative extension and overdue time up once (ADR-0002).

    The caller supplies confirmed billable time, with any support-approved
    exclusions already removed. This counts periods, not new money owed.
    """
    if not isinstance(total_additional_time, timedelta):
        raise TypeError("total_additional_time must be a timedelta")
    if total_additional_time < timedelta(0):
        raise ValueError("total_additional_time must not be negative")
    periods, remainder = divmod(total_additional_time, timedelta(minutes=30))
    return periods + (1 if remainder else 0)
