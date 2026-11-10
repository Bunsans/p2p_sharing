from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from payments.models import OutgoingAttempt, OutgoingRequest, PaymentAttempt, ProviderCommand
from payments.outgoing import start_outgoing
from payments.reservations import reserve_money
from payments.services import prepare_settlement, start_payment
from payments.simulator import Simulator


class AdminAndCommandTests(TransactionTestCase):
    def setUp(self):
        now = timezone.now()
        settlement = prepare_settlement(
            booking_ref="admin-booking", renter_ref="renter", owner_ref="owner",
            terms_version=1, owner_kopecks=80_000, deposit_kopecks=300_000,
            starts_at=now, ends_at=now + timedelta(hours=4),
        )
        self.attempts = [start_payment(
            command_id=uuid4(), charge_id=charge.id, actor_ref="renter",
            expected_terms_version=1, handover_allowed=True,
        ) for charge in settlement.charges.all()]

    def test_simulation_requires_flag_and_respects_batch_limit(self):
        with self.assertRaises(CommandError):
            call_command("run_payment_worker")
        with self.assertRaises(CommandError):
            call_command("run_payment_worker", simulate=True, limit=0)
        self.assertFalse(ProviderCommand.objects.exclude(status="queued").exists())
        output = StringIO()
        call_command("run_payment_worker", simulate=True, limit=1, stdout=output)
        self.assertIn("LOCAL SIMULATION: processed 1", output.getvalue())
        self.assertEqual(PaymentAttempt.objects.filter(status="succeeded").count(), 1)
        self.assertEqual(ProviderCommand.objects.filter(status="queued").count(), 1)

    def test_even_superuser_cannot_change_or_delete_financial_history(self):
        user = get_user_model().objects.create_superuser(username="operator", password="test-only")
        self.client.force_login(user)
        attempt = self.attempts[0]
        change_url = reverse("admin:payments_paymentattempt_change", args=[attempt.pk])
        self.assertEqual(self.client.get(change_url).status_code, 200)
        self.assertEqual(self.client.post(change_url, {"status": "succeeded"}).status_code, 403)
        delete_url = reverse("admin:payments_paymentattempt_delete", args=[attempt.pk])
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:payments_paymentattempt_add")).status_code, 403)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "pending")
        command = attempt.command
        response = self.client.get(reverse("admin:payments_providercommand_change", args=[command.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '"metadata"')

    def test_staff_without_view_permission_cannot_read_payment_history(self):
        user = get_user_model().objects.create_user(username="staff", is_staff=True)
        self.client.force_login(user)
        for model in ("paymentattempt", "outgoingattempt", "outgoingrequest"):
            with self.subTest(model=model):
                self.assertEqual(self.client.get(reverse(f"admin:payments_{model}_changelist")).status_code, 403)

    def test_decisions_and_reservations_are_view_only_even_for_superuser(self):
        call_command("run_payment_worker", simulate=True, limit=2, stdout=StringIO())
        payment = PaymentAttempt.objects.get(charge__purpose="rent")
        decision = reserve_money(
            command_id=uuid4(), settlement_id=payment.charge.settlement_id, payment_id=payment.id,
            kind="rent_refund", amount_kopecks=48_000, actor_ref="admin",
            expected_terms_version=1, basis_ref="admin-refund", basis_version=1, decision_allowed=True,
        )
        outgoing = start_outgoing(
            command_id=uuid4(), settlement_id=payment.charge.settlement_id,
            reservation_id=decision.reservations.get().id, actor_ref="admin",
            expected_terms_version=1, execution_allowed=True,
        )
        user = get_user_model().objects.create_superuser(username="operator", password="test-only")
        self.client.force_login(user)
        for obj in (decision, decision.reservations.get(), decision.requests.get(),
                    outgoing, OutgoingRequest.objects.get(attempt=outgoing)):
            with self.subTest(model=obj._meta.model_name):
                prefix = f"admin:payments_{obj._meta.model_name}"
                change_url = reverse(f"{prefix}_change", args=[obj.pk])
                self.assertEqual(self.client.get(change_url).status_code, 200)
                self.assertEqual(self.client.post(change_url, {"amount_kopecks": 1}).status_code, 403)
                self.assertEqual(self.client.post(reverse(f"{prefix}_delete", args=[obj.pk]), {"post": "yes"}).status_code, 403)
                self.assertEqual(self.client.get(reverse(f"{prefix}_add")).status_code, 403)
        decision.refresh_from_db()
        self.assertEqual(decision.amount_kopecks, 48_000)
        outgoing.refresh_from_db()
        self.assertEqual(outgoing.status, "pending")
        command = ProviderCommand.objects.get(outgoing_attempt=outgoing)
        response = self.client.get(reverse("admin:payments_providercommand_change", args=[command.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '"source_payment_id"')

    def test_command_simulates_deposit_refund_and_payout_in_bounded_batches(self):
        call_command("run_payment_worker", simulate=True, limit=2, stdout=StringIO())
        payment = PaymentAttempt.objects.get(charge__purpose="deposit")
        decision = reserve_money(
            command_id=uuid4(), settlement_id=payment.charge.settlement_id, payment_id=payment.id,
            kind="deposit_distribution", amount_kopecks=50_000, actor_ref="admin",
            expected_terms_version=1, basis_ref="deposit-distribution", basis_version=1, decision_allowed=True,
        )
        for reservation in decision.reservations.all():
            start_outgoing(
                command_id=uuid4(), settlement_id=payment.charge.settlement_id,
                reservation_id=reservation.id, actor_ref="admin",
                expected_terms_version=1, execution_allowed=True,
            )
        with self.assertRaises(CommandError):
            call_command("run_payment_worker")
        self.assertEqual(OutgoingAttempt.objects.filter(status="pending").count(), 2)
        output = StringIO()
        call_command("run_payment_worker", simulate=True, limit=1, stdout=output)
        self.assertIn("LOCAL SIMULATION: processed 1", output.getvalue())
        self.assertIn("payments/refunds/payouts", output.getvalue())
        self.assertEqual(OutgoingAttempt.objects.filter(status="succeeded").count(), 1)
        self.assertEqual(OutgoingAttempt.objects.filter(status="pending").count(), 1)
        call_command("run_payment_worker", simulate=True, limit=1, stdout=StringIO())
        self.assertEqual(OutgoingAttempt.objects.filter(status="succeeded").count(), 2)
        self.assertEqual(decision.reservations.count(), 2)
        simulator = Simulator()
        for attempt in OutgoingAttempt.objects.all():
            command = ProviderCommand.objects.get(outgoing_attempt=attempt)
            created = simulator.create_outgoing(command.payload, command.idempotency_key)
            self.assertEqual(created.external_id, attempt.external_id)
            self.assertEqual(simulator.create_outgoing(command.payload, command.idempotency_key), created)
            self.assertEqual(simulator.get_outgoing(attempt.operation, attempt.external_id), created)
            self.assertEqual(created.source_payment_id, payment.external_id)
            self.assertEqual(created.recipient_ref, "renter" if attempt.operation == "refund" else "owner")

    def test_simulator_rejects_wrong_outgoing_operation_and_account(self):
        call_command("run_payment_worker", simulate=True, limit=2, stdout=StringIO())
        payment = PaymentAttempt.objects.get(charge__purpose="rent")
        decision = reserve_money(
            command_id=uuid4(), settlement_id=payment.charge.settlement_id, payment_id=payment.id,
            kind="rent_refund", amount_kopecks=48_000, actor_ref="admin",
            expected_terms_version=1, basis_ref="simulator-refund", basis_version=1, decision_allowed=True,
        )
        attempt = start_outgoing(
            command_id=uuid4(), settlement_id=payment.charge.settlement_id,
            reservation_id=decision.reservations.get().id, actor_ref="admin",
            expected_terms_version=1, execution_allowed=True,
        )
        command = ProviderCommand.objects.get(outgoing_attempt=attempt)
        simulator = Simulator()
        result = simulator.create_outgoing(command.payload, command.idempotency_key)
        with self.assertRaises(ValueError):
            simulator.get_outgoing("payout", result.external_id)
        with self.assertRaises(ValueError):
            simulator.create_outgoing(command.payload | {"operation": "unknown"}, command.idempotency_key)
        OutgoingAttempt.objects.filter(pk=attempt.pk).update(account_ref="other")
        with self.assertRaises(ProviderCommand.DoesNotExist):
            simulator.get_outgoing("refund", result.external_id)
