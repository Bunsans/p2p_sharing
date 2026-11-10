from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, connections, transaction
from django.db.models import Sum
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase
from django.utils import timezone

from payments.models import (
    MoneyDecision, MoneyReservation, OutgoingAttempt, OutgoingRequest, PaymentAttempt,
    ProviderCommand, ProviderObservation,
)
from payments.outgoing import start_outgoing
from payments.reservations import reserve_money
from payments.services import CommandConflict, prepare_settlement
from payments.simulator import Simulator
from payments.worker import OutgoingResult, claim_command, execute_one


class OutgoingTests(TransactionTestCase):
    def setUp(self):
        now = timezone.now()
        self.conditions = dict(
            booking_ref="outgoing-booking", renter_ref="renter", owner_ref="owner",
            terms_version=1, owner_kopecks=80_000, deposit_kopecks=300_000,
            starts_at=now, ends_at=now + timedelta(hours=4),
        )
        self.settlement = prepare_settlement(**self.conditions)
        self.source = self.payment(self.settlement, "rent")
        self.reservation = self.reserve().reservations.get()

    def payment(self, settlement, purpose):
        return PaymentAttempt.objects.create(
            charge=settlement.charges.get(purpose=purpose), status="succeeded",
            external_id=f"sim_source_{uuid4().hex}",
        )

    def reserve(self, **changes):
        return reserve_money(**(dict(
            command_id=uuid4(), settlement_id=self.settlement.id, payment_id=self.source.id,
            kind="rent_refund", amount_kopecks=48_000, actor_ref="support",
            expected_terms_version=1, basis_ref=str(uuid4()), basis_version=1, decision_allowed=True,
        ) | changes))

    def start(self, **changes):
        return start_outgoing(**(dict(
            command_id=uuid4(), settlement_id=self.settlement.id,
            reservation_id=self.reservation.id, actor_ref="operator",
            expected_terms_version=1, execution_allowed=True,
        ) | changes))

    def result(self, attempt, **changes):
        payload = attempt.command.payload
        rubles, kopecks = payload["amount"]["value"].split(".")
        result = OutgoingResult(
            status="succeeded", external_id=f"outgoing_{attempt.id.hex}",
            amount_kopecks=int(rubles) * 100 + int(kopecks), currency=payload["amount"]["currency"],
            operation=payload["operation"], source_payment_id=payload["source_payment_id"],
            recipient_ref=payload["recipient_ref"],
        )
        return replace(result, **changes)

    def test_queue_uses_reserved_money_source_and_snapshot_recipient(self):
        rent_payout = self.reserve(kind="rent_payout", amount_kopecks=40_000).reservations.get()
        deposit = self.payment(self.settlement, "deposit")
        distribution = self.reserve(payment_id=deposit.id, kind="deposit_distribution", amount_kopecks=50_000)
        cases = [
            (self.reservation, self.source, "refund", "renter", "480.00"),
            (rent_payout, self.source, "payout", "owner", "400.00"),
            (distribution.reservations.get(purpose="deposit_refund"), deposit, "refund", "renter", "2500.00"),
            (distribution.reservations.get(purpose="deposit_compensation"), deposit, "payout", "owner", "500.00"),
        ]
        for reservation, source, operation, recipient, value in cases:
            with self.subTest(purpose=reservation.purpose):
                command_id = uuid4()
                attempt = self.start(command_id=command_id, reservation_id=reservation.id)
                command = attempt.command
                self.assertEqual(attempt.status, "pending")
                self.assertEqual(attempt.operation, operation)
                self.assertEqual((attempt.provider, attempt.environment, attempt.account_ref),
                                 (source.provider, source.environment, source.account_ref))
                self.assertEqual(command.outgoing_attempt_id, attempt.id)
                self.assertIsNone(command.attempt_id)
                self.assertEqual(command.payload, {
                    "operation": operation, "source_payment_id": source.external_id,
                    "recipient_ref": recipient, "amount": {"value": value, "currency": "RUB"},
                    "metadata": {"attempt_id": str(attempt.id), "reservation_id": str(reservation.id)},
                })
                self.assertIsNone(command.first_sent_at)
                request = OutgoingRequest.objects.get(pk=command_id)
                self.assertEqual(request.attempt_id, attempt.id)
                self.assertEqual(request.actor_ref, "operator")
                self.assertEqual(request.expected_terms_version, 1)

    def test_retries_and_aliases_preserve_non_canceled_attempt(self):
        command_id = uuid4()
        attempt = self.start(command_id=command_id)
        key = attempt.command.idempotency_key
        for status in ("pending", "unknown", "succeeded"):
            OutgoingAttempt.objects.filter(pk=attempt.pk).update(status=status)
            self.assertEqual(self.start(command_id=command_id).id, attempt.id)
            self.assertEqual(self.start().id, attempt.id)
        self.assertEqual(OutgoingAttempt.objects.count(), 1)
        self.assertEqual(ProviderCommand.objects.get().idempotency_key, key)
        self.assertEqual(OutgoingRequest.objects.count(), 4)
        self.assertEqual(MoneyReservation.objects.count(), 1)

    def test_confirmed_cancel_allows_new_attempt_but_old_command_keeps_old_attempt(self):
        command_id = uuid4()
        attempt = self.start(command_id=command_id)
        provider = Mock()
        provider.create_outgoing.return_value = self.result(attempt, status="canceled")
        execute_one(provider)
        repeated = self.start(command_id=command_id)
        self.assertEqual((repeated.id, repeated.status), (attempt.id, "canceled"))
        retry = self.start()
        self.assertNotEqual(retry.id, attempt.id)
        self.assertNotEqual(retry.command.idempotency_key, attempt.command.idempotency_key)
        self.assertEqual(MoneyReservation.objects.count(), 1)
        self.assertEqual(OutgoingAttempt.objects.count(), 2)

    def test_command_key_conflicts_with_changed_reservation_actor_or_version(self):
        command_id = uuid4()
        self.start(command_id=command_id)
        other_reservation = self.reserve().reservations.get()
        for changes in (
            {"reservation_id": other_reservation.id}, {"actor_ref": "different-operator"},
            {"expected_terms_version": 2},
        ):
            with self.subTest(changes=changes), self.assertRaises(CommandConflict):
                self.start(command_id=command_id, **changes)
        self.assertEqual(OutgoingAttempt.objects.count(), 1)
        self.assertEqual(OutgoingRequest.objects.count(), 1)

    def test_invalid_input_permission_and_version_do_not_queue(self):
        for changes, error in (
            ({"command_id": str(uuid4())}, TypeError),
            ({"settlement_id": str(self.settlement.id)}, TypeError),
            ({"reservation_id": str(self.reservation.id)}, TypeError),
            ({"actor_ref": " "}, ValueError), ({"actor_ref": 42}, TypeError),
            ({"actor_ref": "x" * 129}, ValueError),
            ({"expected_terms_version": True}, TypeError),
            ({"expected_terms_version": 0}, ValueError),
            ({"expected_terms_version": 2}, CommandConflict),
            ({"execution_allowed": 1}, TypeError), ({"execution_allowed": False}, PermissionDenied),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                self.start(**changes)
        self.assertFalse(OutgoingAttempt.objects.exists())
        self.assertFalse(OutgoingRequest.objects.exists())
        self.assertFalse(ProviderCommand.objects.exists())

    def test_source_must_remain_successful_identified_and_in_simulator_scope(self):
        original = {name: getattr(self.source, name) for name in
                    ("status", "external_id", "provider", "environment", "account_ref")}
        for changes in (
            {"status": "pending"}, {"status": "unknown"}, {"status": "canceled"},
            {"external_id": ""}, {"provider": "bank"}, {"environment": "production"},
            {"account_ref": "another-account"},
        ):
            PaymentAttempt.objects.filter(pk=self.source.pk).update(**(original | changes))
            with self.subTest(changes=changes), self.assertRaises(CommandConflict):
                self.start()
        self.assertFalse(OutgoingAttempt.objects.exists())

    def test_reservation_from_another_settlement_is_rejected(self):
        other = prepare_settlement(**(self.conditions | {"booking_ref": "other"}))
        with self.assertRaises(CommandConflict):
            self.start(settlement_id=other.id)
        self.assertFalse(OutgoingAttempt.objects.exists())

    def test_command_storage_failure_and_outer_rollback_keep_reserve_without_attempt(self):
        with patch.object(ProviderCommand.objects, "create", side_effect=RuntimeError("storage failure")):
            with self.assertRaises(RuntimeError):
                self.start()
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.start()
            raise RuntimeError("booking transaction failed")
        with self.assertRaises(RuntimeError), transaction.atomic():
            additional = self.reserve().reservations.get()
            self.start(reservation_id=additional.id)
            raise RuntimeError("decision and execution transaction failed")
        self.assertFalse(OutgoingAttempt.objects.exists())
        self.assertFalse(OutgoingRequest.objects.exists())
        self.assertFalse(ProviderCommand.objects.exists())
        self.assertEqual(MoneyReservation.objects.count(), 1)
        self.assertEqual(MoneyDecision.objects.count(), 1)

    def test_database_protects_attempt_history_and_exactly_one_command_target(self):
        attempt = self.start()
        with self.assertRaises(IntegrityError), transaction.atomic():
            OutgoingAttempt.objects.create(reservation=self.reservation, operation="refund")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderCommand.objects.create(payload={})
        payout = self.reserve(kind="rent_payout", amount_kopecks=40_000).reservations.get()
        other = OutgoingAttempt.objects.create(reservation=payout, operation="payout")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderCommand.objects.create(attempt=self.source, outgoing_attempt=other, payload={})
        with self.assertRaises(IntegrityError), transaction.atomic():
            MoneyReservation.objects.filter(pk=payout.pk).update(previous_refund=self.reservation)
        for obj in (attempt, self.reservation):
            with self.subTest(model=type(obj).__name__), self.assertRaises(ProtectedError):
                obj.delete()

    def test_external_ids_are_unique_by_operation_type_and_provider_scope(self):
        refund = self.start()
        payout_reservation = self.reserve(kind="rent_payout", amount_kopecks=40_000).reservations.get()
        payout = self.start(reservation_id=payout_reservation.id)
        OutgoingAttempt.objects.filter(pk=refund.pk).update(external_id="shared-id")
        OutgoingAttempt.objects.filter(pk=payout.pk).update(external_id="shared-id")
        deposit = self.payment(self.settlement, "deposit")
        reservation = self.reserve(payment_id=deposit.id, kind="deposit_distribution", amount_kopecks=0).reservations.get()
        another_refund = self.start(reservation_id=reservation.id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OutgoingAttempt.objects.filter(pk=another_refund.pk).update(external_id="shared-id")
        OutgoingAttempt.objects.filter(pk=another_refund.pk).update(account_ref="other-account", external_id="shared-id")

    def test_simulated_success_keeps_reserve_consumed_exactly_once(self):
        attempt = self.start()
        self.assertTrue(execute_one(Simulator()))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "succeeded")
        self.assertFalse(execute_one(Simulator()))
        self.assertEqual(MoneyReservation.objects.get(pk=self.reservation.pk).amount_kopecks, 48_000)
        self.reserve()
        self.assertEqual(MoneyReservation.objects.aggregate(total=Sum("amount_kopecks"))["total"], 96_000)
        with self.assertRaises(CommandConflict):
            self.reserve(amount_kopecks=1)

    def test_lost_reply_recovers_same_body_key_and_first_send_without_second_transfer(self):
        attempt = self.start()
        command = attempt.command
        now = timezone.now()
        accepted, calls = {}, []
        provider = Mock()

        def create(payload, key):
            self.assertFalse(connection.in_atomic_block)
            calls.append((payload, key))
            if key not in accepted:
                accepted[key] = self.result(attempt)
                raise TimeoutError("reply lost after transfer")
            return accepted[key]

        provider.create_outgoing.side_effect = create
        execute_one(provider, now=now)
        attempt.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(attempt.status, "unknown")
        self.assertEqual(self.start().id, attempt.id)
        execute_one(provider, now=command.next_attempt_at)
        attempt.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(attempt.status, "succeeded")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(calls, [(command.payload, command.idempotency_key)] * 2)
        self.assertEqual(command.first_sent_at, now)
        self.assertEqual(command.attempt_count, 2)
        self.assertEqual(MoneyReservation.objects.count(), 1)

    def test_expired_lease_reuses_command_and_original_send_clock(self):
        attempt = self.start()
        now = timezone.now()
        claimed = claim_command(now=now)
        self.assertEqual(claimed.outgoing_attempt_id, attempt.id)
        self.assertIsNone(claim_command(now=now + timedelta(seconds=59)))
        provider = Mock()
        provider.create_outgoing.return_value = self.result(attempt)
        execute_one(provider, now=now + timedelta(seconds=61))
        attempt.refresh_from_db()
        command = attempt.command
        self.assertEqual(command.first_sent_at, now)
        self.assertEqual(command.attempt_count, 2)
        provider.create_outgoing.assert_called_once_with(claimed.payload, claimed.idempotency_key)

    def test_unknown_post_stops_at_safe_window_without_releasing_reserve(self):
        attempt = self.start()
        now = timezone.now()
        claim_command(now=now)
        provider = Mock()
        execute_one(provider, now=now + timedelta(hours=23, minutes=59))
        attempt.refresh_from_db()
        command = attempt.command
        self.assertEqual(attempt.status, "unknown")
        self.assertEqual(command.status, "needs_review")
        self.assertEqual(command.last_error_code, "idempotency_expired")
        provider.create_outgoing.assert_not_called()
        provider.get_outgoing.assert_not_called()
        self.assertEqual(self.start().id, attempt.id)
        self.assertEqual(MoneyReservation.objects.count(), 1)

    def test_known_id_is_reconciled_after_window_outside_transaction(self):
        attempt = self.start()
        now = timezone.now()
        OutgoingAttempt.objects.filter(pk=attempt.pk).update(external_id="known-refund")
        ProviderCommand.objects.filter(outgoing_attempt=attempt).update(first_sent_at=now - timedelta(days=2))
        provider = Mock()

        def get(operation, external_id):
            self.assertFalse(connection.in_atomic_block)
            return self.result(attempt, external_id=external_id)

        provider.get_outgoing.side_effect = get
        execute_one(provider, now=now)
        provider.get_outgoing.assert_called_once_with("refund", "known-refund")
        provider.create_outgoing.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "succeeded")

    def test_pending_reply_uses_get_without_posting_again(self):
        attempt = self.start()
        provider = Mock()
        provider.create_outgoing.return_value = self.result(attempt, status="pending")
        provider.get_outgoing.return_value = self.result(attempt)
        execute_one(provider)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "pending")
        self.assertFalse(execute_one(provider))
        execute_one(provider, now=attempt.command.next_attempt_at)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "succeeded")
        provider.create_outgoing.assert_called_once()
        provider.get_outgoing.assert_called_once_with("refund", self.result(attempt).external_id)

    def test_stale_success_is_recorded_without_replacing_newer_canceled_result(self):
        attempt = self.start()
        now = timezone.now()
        stale, current = Mock(), Mock()
        current.create_outgoing.return_value = self.result(attempt, status="canceled")

        def delayed(payload, key):
            self.assertTrue(execute_one(current, now=now + timedelta(seconds=61)))
            return self.result(attempt)

        stale.create_outgoing.side_effect = delayed
        execute_one(stale, now=now)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "canceled")
        self.assertTrue(ProviderObservation.objects.filter(
            command=attempt.command, status="succeeded", applied=False,
        ).exists())
        self.assertEqual(MoneyReservation.objects.count(), 1)

    def test_invalid_result_amount_identity_or_type_stops_automatic_execution(self):
        attempt = self.start()
        now = timezone.now()
        for changes in (
            {"status": "unknown"}, {"status": []}, {"external_id": ""},
            {"external_id": "x" * 129}, {"external_id": " "},
            {"amount_kopecks": 47_999}, {"amount_kopecks": True}, {"currency": "USD"},
            {"operation": "payout"}, {"source_payment_id": "different-payment"},
            {"recipient_ref": "different-renter"},
        ):
            with self.subTest(changes=changes):
                OutgoingAttempt.objects.filter(pk=attempt.pk).update(status="pending", external_id="")
                ProviderCommand.objects.filter(outgoing_attempt=attempt).update(
                    status="queued", next_attempt_at=now, lease_token=None, lease_until=None,
                )
                provider = Mock()
                provider.create_outgoing.return_value = self.result(attempt, **changes)
                execute_one(provider, now=now)
                attempt.refresh_from_db()
                self.assertEqual(attempt.status, "unknown")
                self.assertEqual(attempt.command.status, "needs_review")
                self.assertEqual(attempt.command.last_error_code, "invalid_result")
                self.assertFalse(execute_one(provider, now=now + timedelta(days=1)))
        self.assertEqual(MoneyReservation.objects.count(), 1)

    def test_reconciliation_cannot_replace_known_external_id(self):
        attempt = self.start()
        OutgoingAttempt.objects.filter(pk=attempt.pk).update(external_id="known-refund")
        provider = Mock()
        provider.get_outgoing.return_value = self.result(attempt, external_id="other-refund")
        execute_one(provider)
        attempt.refresh_from_db()
        self.assertEqual(attempt.external_id, "known-refund")
        self.assertEqual(attempt.command.last_error_code, "invalid_result")

    def test_worker_skips_other_outgoing_provider_profiles(self):
        attempt = self.start()
        profile = {"provider": "simulator", "environment": "test", "account_ref": "local"}
        for obj in (attempt, self.source):
            for changes in ({"provider": "bank"}, {"environment": "production"}, {"account_ref": "other"}):
                type(obj).objects.filter(pk=obj.pk).update(**(profile | changes))
                with self.subTest(model=type(obj).__name__, changes=changes):
                    self.assertIsNone(claim_command())
            type(obj).objects.filter(pk=obj.pk).update(**profile)
        self.assertEqual(attempt.command.status, "queued")

    def test_worker_cannot_run_inside_callers_transaction(self):
        self.start()
        provider = Mock()
        with self.assertRaises(RuntimeError), transaction.atomic():
            execute_one(provider)
        provider.create_outgoing.assert_not_called()
        self.assertEqual(ProviderCommand.objects.get().status, "queued")

    def concurrent_calls(self, function, arguments):
        self.assertEqual(connection.vendor, "postgresql", "concurrency checks require PostgreSQL")
        barrier = Barrier(len(arguments))

        def run(kwargs):
            try:
                barrier.wait(timeout=10)
                try:
                    result = function(**kwargs)
                    return result.id if result else None
                except CommandConflict as error:
                    return error
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(arguments)) as executor:
            futures = [executor.submit(run, kwargs) for kwargs in arguments]
            return [future.result(timeout=20) for future in futures]

    def test_concurrent_commands_for_one_reserve_create_one_attempt(self):
        results = self.concurrent_calls(self.start, [{}, {}, {}, {}])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(OutgoingAttempt.objects.count(), 1)
        self.assertEqual(ProviderCommand.objects.count(), 1)
        self.assertEqual(OutgoingRequest.objects.count(), 4)

    def test_concurrent_repeated_business_key_creates_one_request(self):
        command_id = uuid4()
        results = self.concurrent_calls(self.start, [{"command_id": command_id}, {"command_id": command_id}])
        self.assertEqual(results[0], results[1])
        self.assertEqual(OutgoingRequest.objects.count(), 1)

    def test_global_key_race_between_settlements_rolls_back_losing_attempt_and_command(self):
        other = prepare_settlement(**(self.conditions | {"booking_ref": "other"}))
        payment = self.payment(other, "rent")
        reservation = self.reserve(settlement_id=other.id, payment_id=payment.id).reservations.get()
        command_id = uuid4()
        barrier = Barrier(2)
        original_create = OutgoingRequest.objects.create

        def racing_create(**kwargs):
            barrier.wait(timeout=10)
            return original_create(**kwargs)

        with patch.object(OutgoingRequest.objects, "create", side_effect=racing_create):
            results = self.concurrent_calls(self.start, [
                {"command_id": command_id},
                {"command_id": command_id, "settlement_id": other.id, "reservation_id": reservation.id},
            ])
        self.assertEqual(sum(isinstance(result, CommandConflict) for result in results), 1)
        self.assertEqual(OutgoingAttempt.objects.count(), 1)
        self.assertEqual(OutgoingRequest.objects.count(), 1)
        self.assertEqual(ProviderCommand.objects.count(), 1)
        self.assertEqual(MoneyReservation.objects.count(), 2)

    def test_concurrent_workers_claim_one_outgoing_command_once(self):
        attempt = self.start()
        results = self.concurrent_calls(claim_command, [{}, {}, {}, {}])
        attempt.refresh_from_db()
        self.assertEqual(results.count(attempt.command.id), 1)
        self.assertEqual(results.count(None), 3)
        self.assertEqual(attempt.command.attempt_count, 1)

    def test_kopeck_refunds_follow_reservation_order_even_when_queued_in_reverse(self):
        settlement = prepare_settlement(**(self.conditions | {"booking_ref": "kopecks", "owner_kopecks": 3}))
        payment = self.payment(settlement, "rent")
        reservations = [self.reserve(settlement_id=settlement.id, payment_id=payment.id,
                                     amount_kopecks=1).reservations.get() for _ in range(4)]
        self.assertEqual([r.previous_refund_id for r in reservations], [None] + [r.id for r in reservations[:-1]])
        self.assertEqual([(r.rent_owner_kopecks, r.rent_commission_kopecks) for r in reservations],
                         [(1, 0), (1, 0), (0, 1), (1, 0)])
        now = timezone.now()
        for index, reservation in enumerate(reservations):
            MoneyDecision.objects.filter(pk=reservation.decision_id).update(created_at=now - timedelta(days=index))
        attempts = {r.id: self.start(settlement_id=settlement.id, reservation_id=r.id)
                    for r in reversed(reservations)}
        for reservation in reservations:
            self.assertTrue(execute_one(Simulator()))
            attempt = attempts[reservation.id]
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, "succeeded")
        self.assertFalse(execute_one(Simulator()))

    def test_missing_or_failed_predecessor_blocks_later_refund_until_successful_retry(self):
        later = self.reserve().reservations.get()
        later_attempt = self.start(reservation_id=later.id)
        provider = Mock()
        self.assertFalse(execute_one(provider))
        self.assertIsNone(later_attempt.command.first_sent_at)
        first = self.start()
        provider.create_outgoing.return_value = self.result(first, status="canceled")
        self.assertTrue(execute_one(provider))
        self.assertFalse(execute_one(provider))
        self.assertEqual(later_attempt.command.status, "queued")
        retry = self.start()
        provider.create_outgoing.return_value = self.result(retry)
        self.assertTrue(execute_one(provider))
        provider.create_outgoing.return_value = self.result(later_attempt)
        self.assertTrue(execute_one(provider))
        later_attempt.refresh_from_db()
        self.assertEqual(later_attempt.status, "succeeded")
        self.assertEqual(OutgoingAttempt.objects.count(), 3)
        self.assertEqual(MoneyReservation.objects.count(), 2)

    def test_deposit_refund_success_survives_failed_or_unknown_compensation(self):
        for outcome in ("canceled", "unknown"):
            with self.subTest(outcome=outcome):
                settlement = prepare_settlement(**(self.conditions | {"booking_ref": f"deposit-{outcome}"}))
                payment = self.payment(settlement, "deposit")
                decision = self.reserve(settlement_id=settlement.id, payment_id=payment.id,
                                        kind="deposit_distribution", amount_kopecks=50_000)
                refund = decision.reservations.get(purpose="deposit_refund")
                compensation = decision.reservations.get(purpose="deposit_compensation")
                refund_attempt = self.start(settlement_id=settlement.id, reservation_id=refund.id)
                self.assertTrue(execute_one(Simulator()))
                compensation_attempt = self.start(settlement_id=settlement.id, reservation_id=compensation.id)
                provider = Mock()
                if outcome == "unknown":
                    provider.create_outgoing.side_effect = TimeoutError("unknown payout outcome")
                else:
                    provider.create_outgoing.return_value = self.result(compensation_attempt, status="canceled")
                self.assertTrue(execute_one(provider))
                refund_attempt.refresh_from_db()
                compensation_attempt.refresh_from_db()
                self.assertEqual(refund_attempt.status, "succeeded")
                self.assertEqual(compensation_attempt.status, outcome)
                self.assertEqual(dict(decision.reservations.values_list("purpose", "amount_kopecks")),
                                 {"deposit_refund": 250_000, "deposit_compensation": 50_000})
                with self.assertRaises(CommandConflict):
                    self.reserve(settlement_id=settlement.id, payment_id=payment.id,
                                 kind="deposit_distribution", amount_kopecks=0)
                if outcome == "unknown":
                    self.assertEqual(self.start(settlement_id=settlement.id, reservation_id=compensation.id).id,
                                     compensation_attempt.id)
