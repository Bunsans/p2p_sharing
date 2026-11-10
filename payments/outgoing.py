"""Explicit execution of approved reservations, currently only in local simulation."""

from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from .models import MoneyReservation, OutgoingAttempt, OutgoingRequest, PaymentAttempt, ProviderCommand, Settlement
from .services import CommandConflict, _integer, _reference


def _repeat(request, settlement_id, reservation_id, actor_ref, expected_terms_version):
    attempt = request.attempt
    if (attempt.reservation_id != reservation_id
            or attempt.reservation.decision.payment.charge.settlement_id != settlement_id
            or request.actor_ref != actor_ref or request.expected_terms_version != expected_terms_version):
        raise CommandConflict("command_id already has different outgoing parameters")
    return attempt


def start_outgoing(*, command_id, settlement_id, reservation_id, actor_ref,
                   expected_terms_version, execution_allowed):
    """Queue one reserved transfer; only a confirmed canceled attempt permits a new one.

    The caller authorizes execution/retry and keeps its booking transaction open.
    Sums, recipients and provider scope come from the approved reservation/source.
    Queuing a later refund is allowed; the worker waits for its predecessor.
    """
    for name, value in (("command_id", command_id), ("settlement_id", settlement_id), ("reservation_id", reservation_id)):
        if not isinstance(value, UUID):
            raise TypeError(f"{name} must be a UUID")
    _reference("actor_ref", actor_ref)
    _integer("expected_terms_version", expected_terms_version, 2**31 - 1, minimum=1)
    if type(execution_allowed) is not bool:
        raise TypeError("execution_allowed must be a bool from the trusted backend")
    if not execution_allowed:
        raise PermissionDenied("outgoing execution is not authorized")
    try:
        with transaction.atomic():
            settlement = Settlement.objects.select_for_update().get(pk=settlement_id)
            reservation = MoneyReservation.objects.select_related("decision__payment__charge").get(pk=reservation_id)
            payment = reservation.decision.payment
            if payment.charge.settlement_id != settlement.id:
                raise CommandConflict("reservation belongs to a different settlement")
            request = OutgoingRequest.objects.select_related("attempt").filter(pk=command_id).first()
            if request:
                return _repeat(request, settlement_id, reservation_id, actor_ref, expected_terms_version)
            if expected_terms_version != settlement.terms_version:
                raise CommandConflict("settlement terms version has changed")
            if payment.status != PaymentAttempt.Status.SUCCEEDED or not payment.external_id:
                raise CommandConflict("outgoing execution requires a confirmed source payment ID")
            if (payment.provider, payment.environment, payment.account_ref) != ("simulator", "test", "local"):
                raise CommandConflict("only local simulated sources can be executed")
            attempt = reservation.outgoing_attempts.exclude(status=OutgoingAttempt.Status.CANCELED).first()
            if attempt is None:
                is_refund = reservation.purpose in (MoneyReservation.Purpose.RENT_REFUND, MoneyReservation.Purpose.DEPOSIT_REFUND)
                operation = OutgoingAttempt.Operation.REFUND if is_refund else OutgoingAttempt.Operation.PAYOUT
                attempt = OutgoingAttempt.objects.create(
                    reservation=reservation, operation=operation, provider=payment.provider,
                    environment=payment.environment, account_ref=payment.account_ref,
                )
                amount = reservation.amount_kopecks
                ProviderCommand.objects.create(outgoing_attempt=attempt, payload={
                    "operation": operation,
                    "source_payment_id": payment.external_id,
                    "recipient_ref": settlement.renter_ref if is_refund else settlement.owner_ref,
                    "amount": {"value": f"{amount // 100}.{amount % 100:02d}", "currency": settlement.currency},
                    "metadata": {"attempt_id": str(attempt.id), "reservation_id": str(reservation.id)},
                })
            OutgoingRequest.objects.create(
                id=command_id, attempt=attempt, actor_ref=actor_ref, expected_terms_version=expected_terms_version,
            )
            return attempt
    except IntegrityError:
        # Global command keys can race across settlement locks; discard the losing attempt first.
        request = OutgoingRequest.objects.select_related("attempt").filter(pk=command_id).first()
        if request is None:
            raise
        return _repeat(request, settlement_id, reservation_id, actor_ref, expected_terms_version)
