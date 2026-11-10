from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase
from django.utils import timezone

from payments.models import Charge, PaymentAttempt, PaymentRequest, ProviderCommand, Settlement
from payments.services import CommandConflict, prepare_settlement, start_payment


class PaymentServicesTests(TransactionTestCase):
    def setUp(self):
        start = timezone.now()
        self.conditions = dict(
            booking_ref="booking-1", renter_ref="renter-1", owner_ref="owner-1",
            terms_version=1, owner_kopecks=80_000, deposit_kopecks=300_000,
            starts_at=start, ends_at=start + timedelta(hours=4),
        )
        self.settlement = prepare_settlement(**self.conditions)
        self.charge = self.settlement.charges.get(purpose=Charge.Purpose.RENT)

    def start(self, **changes):
        params = dict(command_id=uuid4(), charge_id=self.charge.id, actor_ref="renter-1",
                      expected_terms_version=1, handover_allowed=True)
        return start_payment(**(params | changes))

    def test_snapshot_and_charges_are_created_once(self):
        self.assertEqual(prepare_settlement(**self.conditions).id, self.settlement.id)
        self.assertEqual(Settlement.objects.count(), 1)
        self.assertEqual(self.settlement.commission_kopecks, 16_000)
        self.assertEqual(self.settlement.rent_kopecks, 96_000)
        self.assertEqual(dict(self.settlement.charges.values_list("purpose", "amount_kopecks")),
                         {"rent": 96_000, "deposit": 300_000})
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(ProviderCommand.objects.exists())

    def test_snapshot_cannot_be_replaced(self):
        for change in ({"owner_kopecks": 90_000}, {"terms_version": 2},
                       {"owner_ref": "different-owner"}, {"deposit_kopecks": 0},
                       {"ends_at": self.conditions["ends_at"] + timedelta(hours=1)}):
            with self.subTest(change=change), self.assertRaises(CommandConflict):
                prepare_settlement(**(self.conditions | change))
        self.settlement.refresh_from_db()
        self.assertEqual(self.settlement.owner_kopecks, 80_000)
        self.assertEqual(Charge.objects.count(), 2)

    def test_snapshot_normalizes_dst_fold_before_comparing_and_repeating(self):
        london = ZoneInfo("Europe/London")
        conditions = self.conditions | {
            "booking_ref": "clock-change",
            "starts_at": datetime(2026, 10, 25, 1, 45, tzinfo=london, fold=0),
            "ends_at": datetime(2026, 10, 25, 1, 15, tzinfo=london, fold=1),
        }
        settlement = prepare_settlement(**conditions)
        settlement.refresh_from_db()
        self.assertEqual(settlement.ends_at - settlement.starts_at, timedelta(minutes=30))
        self.assertEqual(prepare_settlement(**conditions).pk, settlement.pk)
        utc_conditions = conditions | {
            name: conditions[name].astimezone(UTC) for name in ("starts_at", "ends_at")
        }
        self.assertEqual(prepare_settlement(**utc_conditions).pk, settlement.pk)

    def test_invalid_conditions_are_rejected_before_storage(self):
        for changes, error in (
            ({"owner_kopecks": True}, TypeError),
            ({"deposit_kopecks": 1.0}, TypeError),
            ({"owner_kopecks": 0}, ValueError),
            ({"owner_kopecks": 2**63 - 1}, ValueError),
            ({"deposit_kopecks": -1}, ValueError),
            ({"terms_version": 0}, ValueError),
            ({"terms_version": False}, TypeError),
            ({"currency": "USD"}, ValueError),
            ({"owner_ref": "renter-1"}, ValueError),
            ({"renter_ref": " "}, ValueError),
            ({"booking_ref": "b" * 129}, ValueError),
            ({"starts_at": self.conditions["starts_at"].replace(tzinfo=None)}, ValueError),
            ({"ends_at": self.conditions["starts_at"]}, ValueError),
            ({"ends_at": "tomorrow"}, TypeError),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                prepare_settlement(**(self.conditions | {"booking_ref": "invalid"} | changes))
        self.assertEqual(Settlement.objects.count(), 1)

    def test_snapshot_rolls_back_when_charge_creation_fails(self):
        with patch.object(Charge.objects, "bulk_create", side_effect=RuntimeError("storage failure")):
            with self.assertRaises(RuntimeError):
                prepare_settlement(**(self.conditions | {"booking_ref": "new-booking"}))
        self.assertEqual(Settlement.objects.count(), 1)
        self.assertEqual(Charge.objects.count(), 2)

    def test_payment_request_and_provider_command_are_durable(self):
        command_id = uuid4()
        attempt = self.start(command_id=command_id)
        command = ProviderCommand.objects.get(attempt=attempt)
        self.assertEqual(attempt.status, PaymentAttempt.Status.PENDING)
        self.assertEqual(command.status, ProviderCommand.Status.QUEUED)
        self.assertEqual(command.payload, {
            "amount": {"value": "960.00", "currency": "RUB"},
            "metadata": {"attempt_id": str(attempt.id), "charge_id": str(self.charge.id)},
        })
        self.assertEqual(PaymentRequest.objects.get(pk=command_id).attempt_id, attempt.id)
        self.assertIsNone(command.first_sent_at)
        self.assertEqual(command.attempt_count, 0)

    def test_repeated_and_alias_commands_keep_same_operation_and_provider_key(self):
        command_id = uuid4()
        original = self.start(command_id=command_id)
        provider_key = original.command.idempotency_key
        self.assertEqual(self.start(command_id=command_id).id, original.id)
        self.assertEqual(self.start().id, original.id)
        self.assertEqual(PaymentRequest.objects.count(), 2)
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        self.assertEqual(ProviderCommand.objects.get().idempotency_key, provider_key)

    def test_unknown_and_success_do_not_authorize_a_new_attempt(self):
        original = self.start()
        for status in (PaymentAttempt.Status.UNKNOWN, PaymentAttempt.Status.SUCCEEDED):
            PaymentAttempt.objects.filter(pk=original.pk).update(status=status)
            self.assertEqual(self.start().id, original.id)
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        self.assertEqual(ProviderCommand.objects.count(), 1)

    def test_new_key_after_confirmed_cancel_starts_new_attempt_but_old_key_stays_old(self):
        command_id = uuid4()
        original = self.start(command_id=command_id)
        PaymentAttempt.objects.filter(pk=original.pk).update(status=PaymentAttempt.Status.CANCELED)
        ProviderCommand.objects.filter(attempt=original).update(status=ProviderCommand.Status.DONE)
        repeated = self.start(command_id=command_id)
        self.assertEqual(repeated.id, original.id)
        self.assertEqual(repeated.status, PaymentAttempt.Status.CANCELED)
        new_attempt = self.start()
        self.assertNotEqual(new_attempt.id, original.id)
        self.assertNotEqual(new_attempt.command.idempotency_key, original.command.idempotency_key)
        self.assertEqual(PaymentAttempt.objects.count(), 2)

    def test_command_id_conflicts_with_changed_charge_or_version(self):
        command_id = uuid4()
        self.start(command_id=command_id)
        deposit = self.settlement.charges.get(purpose=Charge.Purpose.DEPOSIT)
        for change in ({"charge_id": deposit.id}, {"expected_terms_version": 2}):
            with self.subTest(change=change), self.assertRaises(CommandConflict):
                self.start(command_id=command_id, **change)
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_permissions_versions_and_types_are_checked(self):
        for changes, error in (
            ({"actor_ref": "owner-1"}, PermissionDenied),
            ({"handover_allowed": False}, PermissionDenied),
            ({"handover_allowed": 1}, TypeError),
            ({"expected_terms_version": 2}, CommandConflict),
            ({"expected_terms_version": True}, TypeError),
            ({"expected_terms_version": 0}, ValueError),
            ({"command_id": str(uuid4())}, TypeError),
            ({"charge_id": str(self.charge.id)}, TypeError),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                self.start(**changes)
        self.assertFalse(PaymentRequest.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_zero_deposit_does_not_create_a_payment(self):
        settlement = prepare_settlement(**(self.conditions | {"booking_ref": "free-deposit", "deposit_kopecks": 0}))
        charge = settlement.charges.get(purpose=Charge.Purpose.DEPOSIT)
        with self.assertRaises(ValueError):
            self.start(charge_id=charge.id)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_payment_rolls_back_when_command_storage_fails(self):
        with patch.object(ProviderCommand.objects, "create", side_effect=RuntimeError("storage failure")):
            with self.assertRaises(RuntimeError):
                self.start()
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(ProviderCommand.objects.exists())
        self.assertFalse(PaymentRequest.objects.exists())

    def test_outer_transaction_rollback_removes_payment_intent(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.start()
                raise RuntimeError("booking transaction failed")
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(ProviderCommand.objects.exists())

    def test_database_protects_active_attempt_and_financial_history(self):
        self.start()
        with self.assertRaises(IntegrityError), transaction.atomic():
            PaymentAttempt.objects.create(charge=self.charge)
        with self.assertRaises(ProtectedError):
            self.settlement.delete()
        with self.assertRaises(ProtectedError):
            self.charge.delete()

    def test_external_id_is_unique_within_provider_account(self):
        first = self.start()
        deposit = self.settlement.charges.get(purpose=Charge.Purpose.DEPOSIT)
        second = self.start(charge_id=deposit.id)
        PaymentAttempt.objects.filter(pk=first.pk).update(external_id="external-1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            PaymentAttempt.objects.filter(pk=second.pk).update(external_id="external-1")
        PaymentAttempt.objects.filter(pk=second.pk).update(account_ref="other-account", external_id="external-1")

    def concurrent_calls(self, calls):
        self.assertEqual(connection.vendor, "postgresql", "concurrency checks require PostgreSQL")
        barrier = Barrier(len(calls))

        def run(params):
            try:
                barrier.wait(timeout=10)
                return self.start(**params).id
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(calls)) as executor:
            futures = [executor.submit(run, params) for params in calls]
            return [future.result(timeout=20) for future in futures]

    def test_simultaneous_commands_create_one_operation(self):
        ids = self.concurrent_calls([{}, {}, {}, {}])
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        self.assertEqual(ProviderCommand.objects.count(), 1)
        self.assertEqual(PaymentRequest.objects.count(), 4)

    def test_simultaneous_repeated_key_creates_one_request(self):
        command_id = uuid4()
        ids = self.concurrent_calls([{"command_id": command_id}, {"command_id": command_id}])
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(PaymentRequest.objects.count(), 1)

    def test_racing_same_key_across_settlements_rolls_back_loser(self):
        other = prepare_settlement(**(self.conditions | {"booking_ref": "other"}))
        other_charge = other.charges.get(purpose=Charge.Purpose.RENT)
        command_id = uuid4()
        insert_barrier = Barrier(2)
        original_create = PaymentRequest.objects.create

        def racing_create(**kwargs):
            insert_barrier.wait(timeout=10)
            return original_create(**kwargs)

        with patch.object(PaymentRequest.objects, "create", side_effect=racing_create):
            with self.assertRaises(CommandConflict):
                self.concurrent_calls([
                    {"command_id": command_id},
                    {"command_id": command_id, "charge_id": other_charge.id},
                ])
        self.assertEqual(PaymentRequest.objects.count(), 1)
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        self.assertEqual(ProviderCommand.objects.count(), 1)
