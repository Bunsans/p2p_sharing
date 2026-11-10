from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from payments.models import PaymentAttempt, ProviderCommand, ProviderObservation
from payments.services import prepare_settlement, start_payment
from payments.simulator import Simulator
from payments.worker import PaymentResult, claim_command, execute_one


class PaymentWorkerTests(TransactionTestCase):
    def setUp(self):
        start = timezone.now()
        settlement = prepare_settlement(
            booking_ref="worker-booking", renter_ref="renter", owner_ref="owner",
            terms_version=1, owner_kopecks=80_000, deposit_kopecks=300_000,
            starts_at=start, ends_at=start + timedelta(hours=4),
        )
        charge = settlement.charges.get(purpose="rent")
        self.attempt = start_payment(
            command_id=uuid4(), charge_id=charge.id, actor_ref="renter",
            expected_terms_version=1, handover_allowed=True,
        )
        self.command = self.attempt.command
        self.now = timezone.now()

    def refresh(self):
        self.attempt.refresh_from_db()
        self.command.refresh_from_db()

    def test_simulator_finishes_one_operation_without_network(self):
        self.assertTrue(execute_one(Simulator(), now=self.now))
        self.refresh()
        self.assertEqual(self.attempt.status, PaymentAttempt.Status.SUCCEEDED)
        self.assertEqual(self.attempt.external_id, f"sim_{self.command.idempotency_key.hex}")
        self.assertEqual(self.command.status, ProviderCommand.Status.DONE)
        self.assertEqual(self.command.first_sent_at, self.now)
        self.assertEqual(self.command.attempt_count, 1)
        self.assertFalse(execute_one(Simulator(), now=self.now))

    def test_pending_operation_is_checked_by_id_instead_of_posted_again(self):
        provider = Mock()
        provider.create_payment.return_value = PaymentResult("pending", "payment-1", 96_000)
        provider.get_payment.return_value = PaymentResult("succeeded", "payment-1", 96_000)
        self.assertTrue(execute_one(provider, now=self.now))
        self.refresh()
        self.assertEqual(self.attempt.status, PaymentAttempt.Status.PENDING)
        self.assertEqual(self.command.status, ProviderCommand.Status.QUEUED)
        self.assertFalse(execute_one(provider, now=self.now))
        self.assertTrue(execute_one(provider, now=self.command.next_attempt_at))
        self.refresh()
        provider.create_payment.assert_called_once()
        provider.get_payment.assert_called_once_with("payment-1")
        self.assertEqual(self.attempt.status, PaymentAttempt.Status.SUCCEEDED)

    def test_lost_response_retries_same_key_and_body_without_double_payment(self):
        provider = Mock()
        accepted = {}
        calls = []

        def create(payload, key):
            self.assertFalse(connection.in_atomic_block)
            calls.append((payload, key))
            if key not in accepted:
                accepted[key] = PaymentResult("succeeded", "payment-1", 96_000)
                raise TimeoutError("response lost after provider accepted payment")
            return accepted[key]

        provider.create_payment.side_effect = create
        execute_one(provider, now=self.now)
        self.refresh()
        self.assertEqual(self.attempt.status, PaymentAttempt.Status.UNKNOWN)
        self.assertEqual(self.command.status, ProviderCommand.Status.QUEUED)
        self.assertEqual(self.command.last_error_code, "transport_error")
        self.assertEqual(self.command.next_attempt_at, self.now + timedelta(seconds=30))
        execute_one(provider, now=self.command.next_attempt_at)
        self.refresh()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(calls, [(self.command.payload, self.command.idempotency_key)] * 2)
        self.assertEqual(self.command.first_sent_at, self.now)
        self.assertEqual(self.command.attempt_count, 2)
        self.assertEqual(self.attempt.status, PaymentAttempt.Status.SUCCEEDED)

    def test_unknown_operation_past_window_is_not_posted(self):
        claim_command(now=self.now)
        provider = Mock()
        execute_one(provider, now=self.now + timedelta(hours=23, minutes=59))
        self.refresh()
        provider.create_payment.assert_not_called()
        provider.get_payment.assert_not_called()
        self.assertEqual(self.attempt.status, PaymentAttempt.Status.UNKNOWN)
        self.assertEqual(self.command.status, ProviderCommand.Status.NEEDS_REVIEW)
        self.assertEqual(self.command.last_error_code, "idempotency_expired")

    def test_known_id_can_be_checked_after_idempotency_window(self):
        PaymentAttempt.objects.filter(pk=self.attempt.pk).update(external_id="payment-1")
        ProviderCommand.objects.filter(pk=self.command.pk).update(first_sent_at=self.now - timedelta(days=2))
        provider = Mock()
        provider.get_payment.return_value = PaymentResult("succeeded", "payment-1", 96_000)
        execute_one(provider, now=self.now)
        self.refresh()
        provider.create_payment.assert_not_called()
        provider.get_payment.assert_called_once_with("payment-1")
        self.assertEqual(self.attempt.status, PaymentAttempt.Status.SUCCEEDED)

    def test_provider_calls_run_outside_database_transaction(self):
        provider = Mock()

        def create(payload, key):
            self.assertFalse(connection.in_atomic_block)
            return PaymentResult("pending", "payment-1", 96_000)

        def get(external_id):
            self.assertFalse(connection.in_atomic_block)
            return PaymentResult("succeeded", external_id, 96_000)

        provider.create_payment.side_effect = create
        provider.get_payment.side_effect = get
        execute_one(provider, now=self.now)
        self.refresh()
        execute_one(provider, now=self.command.next_attempt_at)
        self.refresh()
        self.assertEqual(self.attempt.status, PaymentAttempt.Status.SUCCEEDED)

    def test_worker_rejects_execution_inside_callers_transaction(self):
        provider = Mock()
        with self.assertRaises(RuntimeError), transaction.atomic():
            execute_one(provider, now=self.now)
        provider.create_payment.assert_not_called()
        provider.get_payment.assert_not_called()
        self.refresh()
        self.assertEqual(self.command.status, ProviderCommand.Status.QUEUED)

    def test_unexecuted_claim_is_recovered_with_same_key_and_original_clock(self):
        first = claim_command(now=self.now)
        self.assertEqual(first.id, self.command.id)
        self.assertIsNotNone(first.lease_token)
        self.assertEqual(first.lease_until, self.now + timedelta(seconds=60))
        self.assertIsNone(claim_command(now=self.now + timedelta(seconds=59)))
        provider = Mock()
        provider.create_payment.return_value = PaymentResult("succeeded", "payment-1", 96_000)
        execute_one(provider, now=self.now + timedelta(seconds=61))
        self.refresh()
        self.assertEqual(self.command.first_sent_at, self.now)
        self.assertEqual(self.command.attempt_count, 2)
        provider.create_payment.assert_called_once_with(self.command.payload, self.command.idempotency_key)
        self.assertEqual(self.command.status, ProviderCommand.Status.DONE)

    def test_stale_reply_is_recorded_without_overwriting_newer_success(self):
        stale_provider = Mock()
        newer_provider = Mock()
        newer_provider.create_payment.return_value = PaymentResult("succeeded", "payment-1", 96_000)

        def delayed_create(payload, key):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(execute_one(newer_provider, now=self.now + timedelta(seconds=61)))
            return PaymentResult("pending", "payment-1", 96_000)

        stale_provider.create_payment.side_effect = delayed_create
        execute_one(stale_provider, now=self.now)
        self.refresh()
        self.assertEqual(self.attempt.status, PaymentAttempt.Status.SUCCEEDED)
        self.assertEqual(self.command.status, ProviderCommand.Status.DONE)
        self.assertTrue(ProviderObservation.objects.filter(
            command=self.command, status="pending", external_id="payment-1", applied=False,
            amount_kopecks=96_000, currency="RUB",
        ).exists())

    def test_confirmed_terminal_attempt_does_not_call_provider_again(self):
        PaymentAttempt.objects.filter(pk=self.attempt.pk).update(
            status=PaymentAttempt.Status.SUCCEEDED, external_id="payment-1",
        )
        provider = Mock()
        execute_one(provider, now=self.now)
        self.refresh()
        provider.create_payment.assert_not_called()
        provider.get_payment.assert_not_called()
        self.assertEqual(self.command.status, ProviderCommand.Status.DONE)

    def test_invalid_provider_result_stops_automatic_execution(self):
        for result in (
            PaymentResult("succeeded", "payment-1", 95_999),
            PaymentResult("succeeded", "payment-1", True),
            PaymentResult("succeeded", "payment-1", 96_000, "USD"),
            PaymentResult("unknown", "payment-1", 96_000),
            PaymentResult([], "payment-1", 96_000),
            PaymentResult("succeeded", "", 96_000),
            PaymentResult("succeeded", "x" * 129, 96_000),
            PaymentResult("succeeded", " " + "x" * 128 + " ", 96_000),
        ):
            with self.subTest(result=result):
                PaymentAttempt.objects.filter(pk=self.attempt.pk).update(status="pending", external_id="")
                ProviderCommand.objects.filter(pk=self.command.pk).update(
                    status="queued", lease_token=None, lease_until=None, next_attempt_at=self.now,
                )
                provider = Mock()
                provider.create_payment.return_value = result
                execute_one(provider, now=self.now)
                self.refresh()
                self.assertEqual(self.attempt.status, PaymentAttempt.Status.UNKNOWN)
                self.assertEqual(self.command.status, ProviderCommand.Status.NEEDS_REVIEW)
                self.assertEqual(self.command.last_error_code, "invalid_result")
                self.assertFalse(execute_one(provider, now=self.now + timedelta(days=1)))
                provider.create_payment.assert_called_once()

    def test_reconciliation_cannot_replace_known_external_id(self):
        PaymentAttempt.objects.filter(pk=self.attempt.pk).update(external_id="known-payment")
        provider = Mock()
        provider.get_payment.return_value = PaymentResult("succeeded", "different-payment", 96_000)
        execute_one(provider, now=self.now)
        self.refresh()
        self.assertEqual(self.attempt.external_id, "known-payment")
        self.assertEqual(self.command.status, ProviderCommand.Status.NEEDS_REVIEW)
        self.assertEqual(self.command.last_error_code, "invalid_result")

    def test_simulator_worker_does_not_claim_other_provider_accounts(self):
        for changes in ({"provider": "real-bank"}, {"environment": "production"},
                        {"account_ref": "other-account"}):
            with self.subTest(changes=changes):
                PaymentAttempt.objects.filter(pk=self.attempt.pk).update(
                    **({"provider": "simulator", "environment": "test", "account_ref": "local"} | changes),
                )
                self.assertIsNone(claim_command(now=self.now))
        self.refresh()
        self.assertEqual(self.command.status, ProviderCommand.Status.QUEUED)

    def test_concurrent_workers_claim_one_command_once(self):
        self.assertEqual(connection.vendor, "postgresql", "claim contention requires PostgreSQL")
        barrier = Barrier(4)

        def claim():
            try:
                barrier.wait(timeout=10)
                command = claim_command(now=self.now)
                return command.id if command else None
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(claim) for _ in range(4)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(results.count(self.command.id), 1)
        self.assertEqual(results.count(None), 3)
        self.refresh()
        self.assertEqual(self.command.attempt_count, 1)
