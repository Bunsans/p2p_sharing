"""Durable execution for the local simulator; provider HTTP integration is separate."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import OutgoingAttempt, PaymentAttempt, ProviderCommand, ProviderObservation, Settlement


LEASE = timedelta(seconds=60)
# Leave a margin before the candidate provider's 24-hour guarantee ends.
RETRY_WINDOW = timedelta(hours=24) - timedelta(minutes=1)
RETRY_DELAY = timedelta(seconds=30)
TERMINAL = {PaymentAttempt.Status.SUCCEEDED, PaymentAttempt.Status.CANCELED}


@dataclass(frozen=True)
class PaymentResult:
    status: str
    external_id: str
    amount_kopecks: int
    currency: str = "RUB"


@dataclass(frozen=True)
class OutgoingResult:
    status: str
    external_id: str
    amount_kopecks: int
    currency: str
    operation: str
    source_payment_id: str
    recipient_ref: str


def claim_command(*, now=None):
    """Take one due command; a recovered lease retains its body, key and first send."""
    now = now or timezone.now()
    due = Q(status="queued", next_attempt_at__lte=now) | Q(status="running", lease_until__lte=now)
    payments = Q(attempt__provider="simulator", attempt__environment="test", attempt__account_ref="local")
    outgoing = Q(
        outgoing_attempt__provider="simulator", outgoing_attempt__environment="test", outgoing_attempt__account_ref="local",
        outgoing_attempt__reservation__decision__payment__provider="simulator",
        outgoing_attempt__reservation__decision__payment__environment="test",
        outgoing_attempt__reservation__decision__payment__account_ref="local",
    ) & (
        Q(outgoing_attempt__reservation__previous_refund__isnull=True)
        | Q(outgoing_attempt__reservation__previous_refund__outgoing_attempts__status="succeeded")
    )
    with transaction.atomic():
        command = (
            ProviderCommand.objects.filter(due, payments | outgoing)
            .select_for_update(skip_locked=True, of=("self",))
            .order_by("next_attempt_at", "id")
            .first()
        )
        if command is None:
            return None
        command.status = ProviderCommand.Status.RUNNING
        command.lease_token = uuid4()
        command.lease_until = now + LEASE
        command.first_sent_at = command.first_sent_at or now
        command.attempt_count += 1
        command.save(update_fields=[
            "status", "lease_token", "lease_until", "first_sent_at", "attempt_count", "updated_at",
        ])
        return command


def _finish(claim, *, now, result=None, error_code="", review=False):
    if claim.attempt_id:
        settlement_id = claim.attempt.charge.settlement_id
        attempt_model, attempt_id = PaymentAttempt, claim.attempt_id
    else:
        settlement_id = claim.outgoing_attempt.reservation.decision.payment.charge.settlement_id
        attempt_model, attempt_id = OutgoingAttempt, claim.outgoing_attempt_id
    with transaction.atomic():
        Settlement.objects.select_for_update().get(pk=settlement_id)
        attempt = attempt_model.objects.select_for_update().get(pk=attempt_id)
        command = ProviderCommand.objects.select_for_update().get(pk=claim.pk)
        observation = None
        if result is not None:
            observation = ProviderObservation.objects.create(
                command=command, lease_token=claim.lease_token, status=result.status,
                external_id=result.external_id, amount_kopecks=result.amount_kopecks,
                currency=result.currency,
            )
        if command.lease_token != claim.lease_token or command.status != "running":
            return
        if attempt.status in TERMINAL:
            command.status = ProviderCommand.Status.DONE
            if result is not None and (attempt.status != result.status or attempt.external_id != result.external_id):
                command.status = ProviderCommand.Status.NEEDS_REVIEW
                error_code = "result_conflict"
        elif result is not None:
            if attempt.external_id and attempt.external_id != result.external_id:
                command.status = ProviderCommand.Status.NEEDS_REVIEW
                error_code = "result_conflict"
            else:
                # A duplicate external ID must not roll back its observation or authorize another POST.
                try:
                    with transaction.atomic():
                        attempt.status = result.status
                        attempt.external_id = result.external_id
                        attempt.save(update_fields=["status", "external_id", "updated_at"])
                except IntegrityError:
                    command.status = ProviderCommand.Status.NEEDS_REVIEW
                    error_code = "result_conflict"
                else:
                    observation.applied = True
                    observation.save(update_fields=["applied"])
                    command.status = "done" if result.status in TERMINAL else "queued"
        else:
            attempt.status = PaymentAttempt.Status.UNKNOWN
            attempt.save(update_fields=["status", "updated_at"])
            command.status = "needs_review" if review else "queued"
        command.last_error_code = error_code
        command.next_attempt_at = now + RETRY_DELAY
        command.lease_token = None
        command.lease_until = None
        command.save(update_fields=[
            "status", "last_error_code", "next_attempt_at", "lease_token", "lease_until", "updated_at",
        ])


def _valid_result(result, attempt):
    if isinstance(attempt, OutgoingAttempt):
        reservation = attempt.reservation
        payment = reservation.decision.payment
        settlement = payment.charge.settlement
        recipient = settlement.renter_ref if attempt.operation == "refund" else settlement.owner_ref
        if (not isinstance(result, OutgoingResult) or result.operation != attempt.operation
                or result.source_payment_id != payment.external_id or result.recipient_ref != recipient):
            return False
        amount = reservation.amount_kopecks
    else:
        if not isinstance(result, PaymentResult):
            return False
        settlement = attempt.charge.settlement
        amount = attempt.charge.amount_kopecks
    return (
        isinstance(result.status, str)
        and result.status in {"pending", "succeeded", "canceled"}
        and isinstance(result.external_id, str)
        and bool(result.external_id.strip())
        and len(result.external_id) <= 128
        and type(result.amount_kopecks) is int
        and result.amount_kopecks == amount
        and result.currency == settlement.currency
        and (not attempt.external_id or result.external_id == attempt.external_id)
    )


def execute_one(provider, *, now=None):
    """Run at most one command outside a transaction; only simulator jobs are claimed.

    An adapter must raise OSError for an uncertain transport outcome. Unexpected
    failures leave the lease to expire; they never cancel the payment attempt.
    """
    if connection.in_atomic_block:
        raise RuntimeError("payment execution must run outside a database transaction")
    claim = claim_command(now=now)
    if claim is None:
        return False
    current_time = now or timezone.now()
    if claim.attempt_id:
        attempt = PaymentAttempt.objects.select_related("charge__settlement").get(pk=claim.attempt_id)
    else:
        attempt = OutgoingAttempt.objects.select_related("reservation__decision__payment__charge__settlement").get(pk=claim.outgoing_attempt_id)
    if attempt.status in TERMINAL:
        _finish(claim, now=current_time)
        return True
    if not attempt.external_id and current_time >= claim.first_sent_at + RETRY_WINDOW:
        _finish(claim, now=current_time, error_code="idempotency_expired", review=True)
        return True
    try:
        if claim.outgoing_attempt_id:
            if attempt.external_id:
                result = provider.get_outgoing(attempt.operation, attempt.external_id)
            else:
                result = provider.create_outgoing(claim.payload, claim.idempotency_key)
        elif attempt.external_id:
            result = provider.get_payment(attempt.external_id)
        else:
            result = provider.create_payment(claim.payload, claim.idempotency_key)
    except OSError:
        _finish(claim, now=now or timezone.now(), error_code="transport_error")
    else:
        if _valid_result(result, attempt):
            _finish(claim, now=now or timezone.now(), result=result)
        else:
            _finish(claim, now=now or timezone.now(), error_code="invalid_result", review=True)
    return True
