"""Trusted local approvals and permanent reservations; no outgoing provider calls."""

from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum

from .calculations import split_deposit, split_rent_refund
from .models import (
    Charge, MoneyDecision, MoneyDecisionRequest, MoneyReservation, PaymentAttempt, Settlement,
)
from .services import CommandConflict, _integer, _reference


def _repeat(decision, settlement_id, parameters):
    if (decision.payment.charge.settlement_id != settlement_id
            or any(getattr(decision, name) != value for name, value in parameters.items())):
        raise CommandConflict("command_id or basis_ref already has different decision parameters")
    return decision


def _reservations(payment, settlement, kind, amount):
    purpose = MoneyReservation.Purpose
    totals = MoneyReservation.objects.filter(decision__payment=payment).aggregate(
        amount=Sum("amount_kopecks", default=0),
        owner=Sum("rent_owner_kopecks", default=0),
        commission=Sum("rent_commission_kopecks", default=0),
        refunded=Sum("amount_kopecks", filter=Q(purpose=purpose.RENT_REFUND), default=0),
    )
    if kind == MoneyDecision.Kind.DEPOSIT_DISTRIBUTION:
        split = split_deposit(payment.charge.amount_kopecks, amount)
        reservations = [
            MoneyReservation(purpose=purpose.DEPOSIT_REFUND, amount_kopecks=split.refund_kopecks,
                             rent_owner_kopecks=0, rent_commission_kopecks=0),
            MoneyReservation(purpose=purpose.DEPOSIT_COMPENSATION, amount_kopecks=split.compensation_kopecks,
                             rent_owner_kopecks=0, rent_commission_kopecks=0),
        ]
    elif kind == MoneyDecision.Kind.RENT_REFUND:
        if totals["refunded"] + amount > payment.charge.amount_kopecks:
            raise CommandConflict("refund exceeds the original payment")
        split = split_rent_refund(settlement.owner_kopecks, settlement.commission_kopecks,
                                 totals["refunded"], amount)
        previous = None
        if totals["refunded"]:
            previous = MoneyReservation.objects.get(
                decision__payment=payment, purpose=purpose.RENT_REFUND, next_refund__isnull=True,
            )
        reservations = [MoneyReservation(
            purpose=purpose.RENT_REFUND, amount_kopecks=amount,
            rent_owner_kopecks=split.owner_kopecks, rent_commission_kopecks=split.commission_kopecks,
            previous_refund=previous,
        )]
    else:
        reservations = [MoneyReservation(
            purpose=purpose.RENT_PAYOUT, amount_kopecks=amount,
            rent_owner_kopecks=amount, rent_commission_kopecks=0,
        )]
    if totals["amount"] + sum(r.amount_kopecks for r in reservations) > payment.charge.amount_kopecks:
        raise CommandConflict("decision exceeds the unreserved original payment")
    if payment.charge.purpose == Charge.Purpose.RENT and (
        totals["owner"] + sum(r.rent_owner_kopecks for r in reservations) > settlement.owner_kopecks
        or totals["commission"] + sum(r.rent_commission_kopecks for r in reservations) > settlement.commission_kopecks
    ):
        raise CommandConflict("decision exceeds the unreserved original rental shares")
    return [reservation for reservation in reservations if reservation.amount_kopecks]


def reserve_money(*, command_id, settlement_id, payment_id, kind, amount_kopecks,
                  actor_ref, expected_terms_version, basis_ref, basis_version, decision_allowed):
    """Record an approved refund, rental payout or full deposit distribution.

    amount_kopecks is the approved refund/payout, or compensation for a deposit
    distribution (the remainder is reserved for the renter in the same transaction).
    basis_ref identifies one immutable monetary approval globally, across retries.
    The caller verifies authority and basis under its booking lock, retaining its
    outer transaction through this call. Recipients come from the settlement.
    """
    for name, value in (("command_id", command_id), ("settlement_id", settlement_id), ("payment_id", payment_id)):
        if not isinstance(value, UUID):
            raise TypeError(f"{name} must be a UUID")
    for name, value in (("actor_ref", actor_ref), ("basis_ref", basis_ref)):
        _reference(name, value)
    if not isinstance(kind, str):
        raise TypeError("kind must be a string")
    if kind not in MoneyDecision.Kind.values:
        raise ValueError("unsupported decision kind")
    _integer("amount_kopecks", amount_kopecks, 2**63 - 1,
             minimum=0 if kind == MoneyDecision.Kind.DEPOSIT_DISTRIBUTION else 1)
    _integer("expected_terms_version", expected_terms_version, 2**31 - 1, minimum=1)
    _integer("basis_version", basis_version, 2**31 - 1, minimum=1)
    if type(decision_allowed) is not bool:
        raise TypeError("decision_allowed must be a bool from the trusted backend")
    if not decision_allowed:
        raise PermissionDenied("money decision is not authorized")
    parameters = dict(payment_id=payment_id, kind=kind, amount_kopecks=amount_kopecks,
                      actor_ref=actor_ref, expected_terms_version=expected_terms_version,
                      basis_ref=basis_ref, basis_version=basis_version)
    try:
        with transaction.atomic():
            settlement = Settlement.objects.select_for_update().get(pk=settlement_id)
            payment = PaymentAttempt.objects.select_for_update(of=("self",)).select_related("charge").get(pk=payment_id)
            if payment.charge.settlement_id != settlement.id:
                raise CommandConflict("payment belongs to a different settlement")
            request = MoneyDecisionRequest.objects.select_related("decision__payment__charge").filter(pk=command_id).first()
            if request:
                return _repeat(request.decision, settlement_id, parameters)
            decision = MoneyDecision.objects.select_related("payment__charge").filter(basis_ref=basis_ref).first()
            if decision:
                _repeat(decision, settlement_id, parameters)
            else:
                if expected_terms_version != settlement.terms_version:
                    raise CommandConflict("settlement terms version has changed")
                if payment.status != PaymentAttempt.Status.SUCCEEDED:
                    raise CommandConflict("only a confirmed successful payment can fund a decision")
                if payment.charge.amount_kopecks == 0:
                    raise CommandConflict("a zero charge cannot fund a money decision")
                expected_purpose = (Charge.Purpose.DEPOSIT if kind == MoneyDecision.Kind.DEPOSIT_DISTRIBUTION
                                    else Charge.Purpose.RENT)
                if payment.charge.purpose != expected_purpose:
                    raise CommandConflict("payment purpose does not match the decision")
                reservations = _reservations(payment, settlement, kind, amount_kopecks)
                decision = MoneyDecision.objects.create(**parameters)
                for reservation in reservations:
                    reservation.decision = decision
                # shortcut: reserves cannot be released; cancellation needs an explicit allocation correction.
                MoneyReservation.objects.bulk_create(reservations)
            MoneyDecisionRequest.objects.create(id=command_id, decision=decision)
            return decision
    except IntegrityError:
        # Global command/basis keys may race under different settlement locks.
        request = MoneyDecisionRequest.objects.select_related("decision__payment__charge").filter(pk=command_id).first()
        if request:
            return _repeat(request.decision, settlement_id, parameters)
        decision = MoneyDecision.objects.select_related("payment__charge").filter(basis_ref=basis_ref).first()
        if decision is None:
            raise
        _repeat(decision, settlement_id, parameters)
        request, _ = MoneyDecisionRequest.objects.get_or_create(id=command_id, defaults={"decision": decision})
        return _repeat(request.decision, settlement_id, parameters)
