from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, connections, transaction
from django.db.models import Sum
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase
from django.utils import timezone

from payments.models import (
    Charge, MoneyDecision, MoneyDecisionRequest, MoneyReservation, PaymentAttempt, ProviderCommand,
)
from payments.reservations import reserve_money
from payments.services import CommandConflict, prepare_settlement


class MoneyReservationTests(TransactionTestCase):
    def setUp(self):
        now = timezone.now()
        self.conditions = dict(
            booking_ref="reservation-booking", renter_ref="renter", owner_ref="owner",
            terms_version=1, owner_kopecks=80_000, deposit_kopecks=300_000,
            starts_at=now, ends_at=now + timedelta(hours=4),
        )
        self.settlement = prepare_settlement(**self.conditions)
        self.rent = self.payment(self.settlement, Charge.Purpose.RENT)
        self.deposit = self.payment(self.settlement, Charge.Purpose.DEPOSIT)

    def payment(self, settlement, purpose):
        return PaymentAttempt.objects.create(
            charge=settlement.charges.get(purpose=purpose), status=PaymentAttempt.Status.SUCCEEDED,
        )

    def parameters(self, **changes):
        return dict(
            command_id=uuid4(), settlement_id=self.settlement.id, payment_id=self.rent.id,
            kind=MoneyDecision.Kind.RENT_REFUND, amount_kopecks=48_000, actor_ref="support-1",
            expected_terms_version=1, basis_ref=str(uuid4()), basis_version=1, decision_allowed=True,
        ) | changes

    def reserve(self, **changes):
        return reserve_money(**self.parameters(**changes))

    def assert_no_decisions(self):
        self.assertFalse(MoneyDecision.objects.exists())
        self.assertFalse(MoneyReservation.objects.exists())
        self.assertFalse(MoneyDecisionRequest.objects.exists())

    def test_rent_refund_stores_source_audit_and_original_shares(self):
        parameters = self.parameters()
        decision = reserve_money(**parameters)
        reservation = decision.reservations.get()
        self.assertEqual(decision.payment_id, self.rent.id)
        self.assertEqual(decision.actor_ref, "support-1")
        self.assertEqual(decision.basis_ref, parameters["basis_ref"])
        self.assertEqual(decision.basis_version, 1)
        self.assertEqual(decision.expected_terms_version, 1)
        self.assertIsNotNone(decision.created_at)
        self.assertEqual(reservation.purpose, "rent_refund")
        self.assertEqual((reservation.amount_kopecks, reservation.rent_owner_kopecks,
                          reservation.rent_commission_kopecks), (48_000, 40_000, 8_000))
        self.assertEqual(MoneyDecisionRequest.objects.get(pk=parameters["command_id"]).decision_id, decision.id)
        self.rent.refresh_from_db()
        self.assertEqual(self.rent.status, "succeeded")
        self.assertFalse(ProviderCommand.objects.exists())

    def test_repeated_kopeck_refunds_exhaust_original_shares_exactly(self):
        settlement = prepare_settlement(**(self.conditions | {"booking_ref": "kopecks", "owner_kopecks": 3}))
        payment = self.payment(settlement, Charge.Purpose.RENT)
        shares = []
        for _ in range(4):
            decision = self.reserve(settlement_id=settlement.id, payment_id=payment.id, amount_kopecks=1)
            reservation = decision.reservations.get()
            shares.append((reservation.rent_owner_kopecks, reservation.rent_commission_kopecks))
        self.assertEqual(shares, [(1, 0), (1, 0), (0, 1), (1, 0)])
        with self.assertRaises(CommandConflict):
            self.reserve(settlement_id=settlement.id, payment_id=payment.id, amount_kopecks=1)
        self.assertEqual(MoneyReservation.objects.count(), 4)

    def test_payout_cannot_spend_commission_despite_available_total(self):
        with self.assertRaises(CommandConflict):
            self.reserve(kind="rent_payout", amount_kopecks=80_001)
        self.assert_no_decisions()
        decision = self.reserve(kind="rent_payout", amount_kopecks=80_000)
        reservation = decision.reservations.get()
        self.assertEqual((reservation.amount_kopecks, reservation.rent_owner_kopecks,
                          reservation.rent_commission_kopecks), (80_000, 80_000, 0))
        with self.assertRaises(CommandConflict):
            self.reserve(amount_kopecks=1)

    def test_refund_then_payout_can_use_only_remaining_owner_share(self):
        self.reserve()
        with self.assertRaises(CommandConflict):
            self.reserve(kind="rent_payout", amount_kopecks=40_001)
        self.reserve(kind="rent_payout", amount_kopecks=40_000)
        totals = MoneyReservation.objects.aggregate(amount=Sum("amount_kopecks"), owner=Sum("rent_owner_kopecks"))
        self.assertEqual(totals, {"amount": 88_000, "owner": 80_000})
        with self.assertRaises(CommandConflict):
            self.reserve(amount_kopecks=1)

    def test_payout_then_refund_uses_the_same_owner_limit(self):
        self.reserve(kind="rent_payout", amount_kopecks=40_000)
        self.reserve()
        with self.assertRaises(CommandConflict):
            self.reserve(amount_kopecks=1)
        self.assertEqual(MoneyDecision.objects.count(), 2)

    def test_deposit_distribution_reserves_both_parts_without_rental_shares(self):
        decision = self.reserve(payment_id=self.deposit.id, kind="deposit_distribution", amount_kopecks=50_000)
        self.assertEqual(dict(decision.reservations.values_list("purpose", "amount_kopecks")), {
            "deposit_refund": 250_000, "deposit_compensation": 50_000,
        })
        self.assertFalse(decision.reservations.exclude(rent_owner_kopecks=0, rent_commission_kopecks=0).exists())
        with self.assertRaises(CommandConflict):
            self.reserve(payment_id=self.deposit.id, kind="deposit_distribution", amount_kopecks=1)
        self.assertEqual(MoneyDecision.objects.count(), 1)
        self.assertEqual(MoneyReservation.objects.count(), 2)

    def test_deposit_zero_full_and_sub_ruble_parts_are_exact(self):
        for compensation, expected in (
            (0, {"deposit_refund": 100}),
            (100, {"deposit_compensation": 100}),
            (50, {"deposit_refund": 50, "deposit_compensation": 50}),
        ):
            with self.subTest(compensation=compensation):
                settlement = prepare_settlement(**(self.conditions | {
                    "booking_ref": f"small-deposit-{compensation}", "deposit_kopecks": 100,
                }))
                payment = self.payment(settlement, Charge.Purpose.DEPOSIT)
                decision = self.reserve(settlement_id=settlement.id, payment_id=payment.id,
                                        kind="deposit_distribution", amount_kopecks=compensation)
                self.assertEqual(dict(decision.reservations.values_list("purpose", "amount_kopecks")), expected)
        self.assertFalse(MoneyReservation.objects.filter(amount_kopecks=0).exists())

    def test_compensation_cannot_exceed_deposit(self):
        with self.assertRaises(ValueError):
            self.reserve(payment_id=self.deposit.id, kind="deposit_distribution", amount_kopecks=300_001)
        self.assert_no_decisions()

    def test_zero_source_cannot_create_an_empty_distribution(self):
        settlement = prepare_settlement(**(self.conditions | {"booking_ref": "zero", "deposit_kopecks": 0}))
        payment = self.payment(settlement, Charge.Purpose.DEPOSIT)
        with self.assertRaises(CommandConflict):
            self.reserve(settlement_id=settlement.id, payment_id=payment.id,
                         kind="deposit_distribution", amount_kopecks=0)
        self.assert_no_decisions()

    def test_refund_cannot_borrow_deposit_or_other_settlement_funds(self):
        other = prepare_settlement(**(self.conditions | {"booking_ref": "other"}))
        self.payment(other, Charge.Purpose.RENT)
        with self.assertRaises(CommandConflict):
            self.reserve(amount_kopecks=96_001)
        self.assert_no_decisions()
        self.reserve(amount_kopecks=96_000)
        self.reserve(payment_id=self.deposit.id, kind="deposit_distribution", amount_kopecks=0)
        self.assertEqual(MoneyDecision.objects.count(), 2)

    def test_only_successful_source_payment_can_fund_decision(self):
        for status in ("pending", "unknown", "canceled"):
            PaymentAttempt.objects.filter(pk=self.rent.pk).update(status=status)
            with self.subTest(status=status), self.assertRaises(CommandConflict):
                self.reserve()
        self.assert_no_decisions()

    def test_source_settlement_purpose_and_terms_are_checked(self):
        other = prepare_settlement(**(self.conditions | {"booking_ref": "other"}))
        for changes in (
            {"settlement_id": other.id},
            {"payment_id": self.deposit.id},
            {"payment_id": self.deposit.id, "kind": "rent_payout"},
            {"kind": "deposit_distribution"},
            {"expected_terms_version": 2},
        ):
            with self.subTest(changes=changes), self.assertRaises(CommandConflict):
                self.reserve(**changes)
        self.assert_no_decisions()

    def test_input_types_amounts_versions_and_permission_are_checked(self):
        for changes, error in (
            ({"command_id": str(uuid4())}, TypeError),
            ({"payment_id": str(self.rent.id)}, TypeError),
            ({"settlement_id": str(self.settlement.id)}, TypeError),
            ({"kind": []}, TypeError),
            ({"kind": "time_debt"}, ValueError),
            ({"amount_kopecks": True}, TypeError),
            ({"amount_kopecks": 1.0}, TypeError),
            ({"amount_kopecks": 0}, ValueError),
            ({"amount_kopecks": -1}, ValueError),
            ({"amount_kopecks": 2**63}, ValueError),
            ({"expected_terms_version": True}, TypeError),
            ({"expected_terms_version": 0}, ValueError),
            ({"basis_version": True}, TypeError),
            ({"basis_version": 0}, ValueError),
            ({"basis_ref": " "}, ValueError),
            ({"basis_ref": "x" * 129}, ValueError),
            ({"actor_ref": 42}, TypeError),
            ({"actor_ref": ""}, ValueError),
            ({"decision_allowed": 1}, TypeError),
            ({"decision_allowed": False}, PermissionDenied),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                self.reserve(**changes)
        self.assert_no_decisions()

    def test_repeated_command_and_basis_return_same_exhausted_reservation(self):
        parameters = self.parameters(amount_kopecks=96_000)
        original = reserve_money(**parameters)
        self.assertEqual(reserve_money(**parameters).id, original.id)
        alias = parameters | {"command_id": uuid4()}
        self.assertEqual(reserve_money(**alias).id, original.id)
        self.assertEqual(reserve_money(**alias).id, original.id)
        self.assertEqual(MoneyDecision.objects.count(), 1)
        self.assertEqual(MoneyReservation.objects.count(), 1)
        self.assertEqual(MoneyDecisionRequest.objects.count(), 2)
        with self.assertRaises(PermissionDenied):
            reserve_money(**(parameters | {"decision_allowed": False}))

    def test_existing_keys_cannot_change_any_decision_parameter(self):
        parameters = self.parameters()
        reserve_money(**parameters)
        changes = (
            {"amount_kopecks": 1}, {"kind": "rent_payout"}, {"actor_ref": "other-support"},
            {"payment_id": self.deposit.id}, {"basis_version": 2}, {"expected_terms_version": 2},
        )
        for change in changes:
            for command_id in (parameters["command_id"], uuid4()):
                with self.subTest(change=change, command_id=command_id), self.assertRaises(CommandConflict):
                    reserve_money(**(parameters | {"command_id": command_id} | change))
        with self.assertRaises(CommandConflict):
            reserve_money(**(parameters | {"basis_ref": "different-basis"}))
        self.assertEqual(MoneyDecisionRequest.objects.count(), 1)
        self.assertEqual(MoneyReservation.objects.count(), 1)

    def test_basis_is_global_and_cannot_be_reused_for_another_source(self):
        parameters = self.parameters()
        reserve_money(**parameters)
        other = prepare_settlement(**(self.conditions | {"booking_ref": "other"}))
        payment = self.payment(other, Charge.Purpose.RENT)
        with self.assertRaises(CommandConflict):
            reserve_money(**(parameters | {"command_id": uuid4(), "settlement_id": other.id, "payment_id": payment.id}))
        self.assertEqual(MoneyDecision.objects.count(), 1)
        self.assertEqual(MoneyDecisionRequest.objects.count(), 1)

    def test_failure_after_first_deposit_part_rolls_back_whole_decision(self):
        original_create = MoneyReservation.objects.bulk_create

        def fail_after_first_part(reservations):
            original_create(reservations[:1])
            raise RuntimeError("second reservation could not be stored")

        with patch.object(MoneyReservation.objects, "bulk_create", side_effect=fail_after_first_part):
            with self.assertRaises(RuntimeError):
                self.reserve(payment_id=self.deposit.id, kind="deposit_distribution", amount_kopecks=50_000)
        self.assert_no_decisions()

    def test_request_storage_failure_and_outer_rollback_remove_reservations(self):
        with patch.object(MoneyDecisionRequest.objects, "create", side_effect=RuntimeError("storage failure")):
            with self.assertRaises(RuntimeError):
                self.reserve()
        self.assert_no_decisions()
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.reserve()
            raise RuntimeError("booking transaction failed")
        self.assert_no_decisions()

    def test_database_constraints_and_protected_history(self):
        decision = self.reserve()
        reservation = decision.reservations.get()
        for changes in (
            {"amount_kopecks": 0}, {"amount_kopecks": 48_001}, {"rent_owner_kopecks": -1},
            {"purpose": "rent_payout"}, {"purpose": "deposit_refund"}, {"purpose": "unknown"},
        ):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                MoneyReservation.objects.filter(pk=reservation.pk).update(**changes)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MoneyReservation.objects.create(decision=decision, purpose="rent_refund", amount_kopecks=1,
                                            rent_owner_kopecks=1)
        for changes in ({"amount_kopecks": 0}, {"kind": "unknown"}, {"basis_version": 0}, {"expected_terms_version": 0}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                MoneyDecision.objects.filter(pk=decision.pk).update(**changes)
        for obj in (decision, self.rent, self.settlement):
            with self.subTest(model=type(obj).__name__), self.assertRaises(ProtectedError):
                obj.delete()

    def concurrent_calls(self, calls):
        self.assertEqual(connection.vendor, "postgresql", "concurrency checks require PostgreSQL")
        barrier = Barrier(len(calls))

        def run(changes):
            try:
                barrier.wait(timeout=10)
                try:
                    return self.reserve(**changes).id
                except CommandConflict as error:
                    return error
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(calls)) as executor:
            futures = [executor.submit(run, changes) for changes in calls]
            return [future.result(timeout=20) for future in futures]

    def assert_one_winner(self, results):
        self.assertEqual(sum(isinstance(result, CommandConflict) for result in results), 1)
        self.assertEqual(MoneyDecision.objects.count(), 1)
        self.assertEqual(MoneyDecisionRequest.objects.count(), 1)

    def test_competing_refunds_cannot_exceed_source(self):
        results = self.concurrent_calls([{"amount_kopecks": 60_000}, {"amount_kopecks": 60_000}])
        self.assert_one_winner(results)
        self.assertEqual(MoneyReservation.objects.get().amount_kopecks, 60_000)

    def test_concurrent_refunds_that_fit_preserve_full_original_shares(self):
        results = self.concurrent_calls([{}, {}])
        self.assertFalse(any(isinstance(result, Exception) for result in results))
        self.assertEqual(len(set(results)), 2)
        self.assertEqual(MoneyReservation.objects.aggregate(
            amount=Sum("amount_kopecks"), owner=Sum("rent_owner_kopecks"), commission=Sum("rent_commission_kopecks"),
        ), {"amount": 96_000, "owner": 80_000, "commission": 16_000})

    def test_competing_refund_and_payout_cannot_spend_same_owner_share(self):
        results = self.concurrent_calls([{"amount_kopecks": 48_000}, {"kind": "rent_payout", "amount_kopecks": 50_000}])
        self.assert_one_winner(results)
        self.assertEqual(MoneyReservation.objects.count(), 1)

    def test_competing_deposit_distributions_cannot_reserve_twice(self):
        params = dict(payment_id=self.deposit.id, kind="deposit_distribution")
        results = self.concurrent_calls([params | {"amount_kopecks": 50_000}, params | {"amount_kopecks": 100_000}])
        self.assert_one_winner(results)
        self.assertEqual(MoneyReservation.objects.aggregate(total=Sum("amount_kopecks"))["total"], 300_000)

    def test_simultaneous_same_command_and_basis_are_idempotent(self):
        params = {"command_id": uuid4(), "basis_ref": "same-approval"}
        results = self.concurrent_calls([params, params])
        self.assertEqual(results[0], results[1])
        aliases = self.concurrent_calls([{"basis_ref": "same-approval"}, {"basis_ref": "same-approval"}])
        self.assertEqual(aliases, [results[0], results[0]])
        self.assertEqual(MoneyDecision.objects.count(), 1)
        self.assertEqual(MoneyReservation.objects.count(), 1)
        self.assertEqual(MoneyDecisionRequest.objects.count(), 3)

    def test_concurrent_new_commands_for_same_basis_create_one_decision(self):
        results = self.concurrent_calls([{"basis_ref": "same-approval"}, {"basis_ref": "same-approval"}])
        self.assertEqual(results[0], results[1])
        self.assertEqual(MoneyDecision.objects.count(), 1)
        self.assertEqual(MoneyReservation.objects.count(), 1)
        self.assertEqual(MoneyDecisionRequest.objects.count(), 2)

    def test_racing_global_command_across_settlements_rolls_back_loser(self):
        other = prepare_settlement(**(self.conditions | {"booking_ref": "other"}))
        payment = self.payment(other, Charge.Purpose.RENT)
        command_id = uuid4()
        barrier = Barrier(2)
        original_create = MoneyDecisionRequest.objects.create

        def racing_create(**kwargs):
            barrier.wait(timeout=10)
            return original_create(**kwargs)

        with patch.object(MoneyDecisionRequest.objects, "create", side_effect=racing_create):
            results = self.concurrent_calls([
                {"command_id": command_id},
                {"command_id": command_id, "settlement_id": other.id, "payment_id": payment.id},
            ])
        self.assert_one_winner(results)
        self.assertEqual(MoneyReservation.objects.count(), 1)

    def test_racing_global_basis_across_settlements_rolls_back_loser(self):
        other = prepare_settlement(**(self.conditions | {"booking_ref": "other"}))
        payment = self.payment(other, Charge.Purpose.RENT)
        barrier = Barrier(2)
        original_create = MoneyDecision.objects.create

        def racing_create(**kwargs):
            barrier.wait(timeout=10)
            return original_create(**kwargs)

        with patch.object(MoneyDecision.objects, "create", side_effect=racing_create):
            results = self.concurrent_calls([
                {"basis_ref": "same-approval"},
                {"basis_ref": "same-approval", "settlement_id": other.id, "payment_id": payment.id},
            ])
        self.assert_one_winner(results)
        self.assertEqual(MoneyReservation.objects.count(), 1)
