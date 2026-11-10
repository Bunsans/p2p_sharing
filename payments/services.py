"""Trusted backend entry points; no provider calls run in these transactions."""

from datetime import UTC, datetime
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from .calculations import rental_price
from .models import Charge, PaymentAttempt, PaymentRequest, ProviderCommand, Settlement


class CommandConflict(ValueError):
    """A command key or snapshot was reused with different data."""


def _reference(name, value):
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip() or len(value) > 128:
        raise ValueError(f"{name} must contain 1 to 128 characters")


def _integer(name, value, maximum, minimum=0):
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


@transaction.atomic
def prepare_settlement(*, booking_ref, renter_ref, owner_ref, terms_version,
                       owner_kopecks, deposit_kopecks, starts_at, ends_at, currency="RUB"):
    """Save trusted conditions once; changed conditions require an explicit new workflow."""
    for name, value in (("booking_ref", booking_ref), ("renter_ref", renter_ref),
                        ("owner_ref", owner_ref)):
        _reference(name, value)
    if renter_ref == owner_ref:
        raise ValueError("renter and owner must be different")
    _integer("terms_version", terms_version, 2**31 - 1, minimum=1)
    _integer("owner_kopecks", owner_kopecks, 2**63 - 1, minimum=1)
    _integer("deposit_kopecks", deposit_kopecks, 2**63 - 1)
    if currency != "RUB":
        raise ValueError("only RUB is supported")
    for name, value in (("starts_at", starts_at), ("ends_at", ends_at)):
        if not isinstance(value, datetime):
            raise TypeError(f"{name} must be a datetime")
        if timezone.is_naive(value):
            raise ValueError(f"{name} must include a timezone")
    starts_at = starts_at.astimezone(UTC)
    ends_at = ends_at.astimezone(UTC)
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")
    price = rental_price(owner_kopecks)
    _integer("rent_kopecks", price.total_kopecks, 2**63 - 1, minimum=1)
    snapshot = dict(
        renter_ref=renter_ref, owner_ref=owner_ref, terms_version=terms_version,
        owner_kopecks=owner_kopecks, commission_kopecks=price.commission_kopecks,
        rent_kopecks=price.total_kopecks, deposit_kopecks=deposit_kopecks,
        currency=currency, starts_at=starts_at, ends_at=ends_at,
    )
    settlement, created = Settlement.objects.get_or_create(booking_ref=booking_ref, defaults=snapshot)
    if not created:
        if any(getattr(settlement, name) != value for name, value in snapshot.items()):
            raise CommandConflict("booking already has different settlement conditions")
        return settlement
    Charge.objects.bulk_create([
        Charge(settlement=settlement, purpose=Charge.Purpose.RENT, amount_kopecks=price.total_kopecks),
        Charge(settlement=settlement, purpose=Charge.Purpose.DEPOSIT, amount_kopecks=deposit_kopecks),
    ])
    return settlement


def _repeat(request, charge_id, actor_ref, expected_terms_version):
    if (request.attempt.charge_id != charge_id or request.actor_ref != actor_ref
            or request.expected_terms_version != expected_terms_version):
        raise CommandConflict("command_id already has different payment parameters")
    return request.attempt


def start_payment(*, command_id, charge_id, actor_ref, expected_terms_version,
                  handover_allowed):
    """Atomically queue one attempt; retries preserve both business and provider keys.

    The trusted caller must obtain handover_allowed under its booking lock and
    keep the outer transaction open through this call (booking -> settlement).
    Invalid inputs raise TypeError/ValueError; permission failures raise PermissionDenied.
    """
    for name, value in (("command_id", command_id), ("charge_id", charge_id)):
        if not isinstance(value, UUID):
            raise TypeError(f"{name} must be a UUID")
    _reference("actor_ref", actor_ref)
    _integer("expected_terms_version", expected_terms_version, 2**31 - 1, minimum=1)
    if type(handover_allowed) is not bool:
        raise TypeError("handover_allowed must be a bool from the trusted backend")
    if not handover_allowed:
        raise PermissionDenied("handover is not allowed")
    try:
        with transaction.atomic():
            settlement_id = Charge.objects.values_list("settlement_id", flat=True).get(pk=charge_id)
            settlement = Settlement.objects.select_for_update().get(pk=settlement_id)
            charge = Charge.objects.select_for_update().get(pk=charge_id)
            if actor_ref != settlement.renter_ref:
                raise PermissionDenied("only the renter can start this payment")
            request = PaymentRequest.objects.select_related("attempt").filter(pk=command_id).first()
            if request:
                return _repeat(request, charge_id, actor_ref, expected_terms_version)
            if expected_terms_version != settlement.terms_version:
                raise CommandConflict("settlement terms version has changed")
            if charge.amount_kopecks == 0:
                raise ValueError("a zero amount does not require a payment")
            attempt = charge.attempts.exclude(status=PaymentAttempt.Status.CANCELED).first()
            if attempt is None:
                attempt = PaymentAttempt.objects.create(charge=charge)
                ProviderCommand.objects.create(attempt=attempt, payload={
                    "amount": {
                        "value": f"{charge.amount_kopecks // 100}.{charge.amount_kopecks % 100:02d}",
                        "currency": settlement.currency,
                    },
                    "metadata": {"attempt_id": str(attempt.id), "charge_id": str(charge.id)},
                })
            PaymentRequest.objects.create(
                id=command_id, attempt=attempt, actor_ref=actor_ref,
                expected_terms_version=expected_terms_version,
            )
            return attempt
    except IntegrityError:
        # A command key can race across different settlement locks; rollback first.
        request = PaymentRequest.objects.select_related("attempt").filter(pk=command_id).first()
        if request is None:
            raise
        return _repeat(request, charge_id, actor_ref, expected_terms_version)
