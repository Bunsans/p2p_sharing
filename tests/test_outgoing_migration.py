from datetime import timedelta
from uuid import UUID, uuid4

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from django.utils import timezone


class OutgoingMigrationTests(TransactionTestCase):
    migrate_from = ("payments", "0002_money_reservations")
    migrate_to = ("payments", "0003_outgoing_execution")

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql", "migration rollback requires PostgreSQL")
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        self.apps = executor.loader.project_state([self.migrate_from]).apps
        self.now = timezone.now()
        self.addCleanup(self.restore_current_schema)

    def restore_current_schema(self):
        if self.migrate_to not in MigrationRecorder(connection).applied_migrations():
            # A deliberately invalid legacy fixture must not block the next test's schema.
            self.apps.get_model("payments", "MoneyReservation").objects.all().delete()
            MigrationExecutor(connection).migrate([self.migrate_to])

    def source(self, booking_ref, purpose="rent"):
        Settlement = self.apps.get_model("payments", "Settlement")
        Charge = self.apps.get_model("payments", "Charge")
        PaymentAttempt = self.apps.get_model("payments", "PaymentAttempt")
        settlement = Settlement.objects.create(
            booking_ref=booking_ref, renter_ref="renter", owner_ref="owner", terms_version=1,
            owner_kopecks=3, commission_kopecks=1, rent_kopecks=4, deposit_kopecks=100,
            starts_at=self.now, ends_at=self.now + timedelta(hours=1),
        )
        charge = Charge.objects.create(
            settlement=settlement, purpose=purpose, amount_kopecks=4 if purpose == "rent" else 100,
        )
        return PaymentAttempt.objects.create(charge=charge, status="succeeded")

    def refund(self, payment, index, owner, commission):
        Decision = self.apps.get_model("payments", "MoneyDecision")
        Reservation = self.apps.get_model("payments", "MoneyReservation")
        decision = Decision.objects.create(
            id=UUID(int=100 - index), payment=payment, kind="rent_refund", amount_kopecks=1,
            actor_ref="support", basis_ref=str(uuid4()), basis_version=1, expected_terms_version=1,
        )
        Decision.objects.filter(pk=decision.pk).update(created_at=self.now + timedelta(seconds=index))
        return Reservation.objects.create(
            decision=decision, purpose="rent_refund", amount_kopecks=1,
            rent_owner_kopecks=owner, rent_commission_kopecks=commission,
        )

    def monetary_rows(self, apps):
        return list(apps.get_model("payments", "MoneyReservation").objects.order_by("id").values_list(
            "id", "decision_id", "purpose", "amount_kopecks", "rent_owner_kopecks", "rent_commission_kopecks",
        ))

    def migrate(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        return executor.loader.project_state([self.migrate_to]).apps

    def test_legacy_kopeck_refunds_keep_money_and_link_in_approval_order(self):
        payment = self.source("kopeck-refunds")
        refunds = [self.refund(payment, index, *shares) for index, shares in enumerate(
            [(1, 0), (1, 0), (0, 1), (1, 0)],
        )]
        before = self.monetary_rows(self.apps)

        apps = self.migrate()

        Reservation = apps.get_model("payments", "MoneyReservation")
        self.assertEqual(self.monetary_rows(apps), before)
        self.assertEqual(
            [Reservation.objects.get(pk=refund.pk).previous_refund_id for refund in refunds],
            [None, refunds[0].pk, refunds[1].pk, refunds[2].pk],
        )
        self.assertIn(self.migrate_to, MigrationRecorder(connection).applied_migrations())

    def test_different_sources_have_separate_chains_and_deposit_is_unlinked(self):
        first_payment, second_payment = self.source("first"), self.source("second")
        first = [self.refund(first_payment, index, 1, 0) for index in (0, 2)]
        second = [self.refund(second_payment, index, 1, 0) for index in (1, 3)]
        deposit_payment = self.source("deposit", purpose="deposit")
        Decision = self.apps.get_model("payments", "MoneyDecision")
        Reservation = self.apps.get_model("payments", "MoneyReservation")
        decision = Decision.objects.create(
            payment=deposit_payment, kind="deposit_distribution", amount_kopecks=50,
            actor_ref="support", basis_ref=str(uuid4()), basis_version=1, expected_terms_version=1,
        )
        deposit_parts = [Reservation.objects.create(decision=decision, purpose=purpose, amount_kopecks=50)
                         for purpose in ("deposit_refund", "deposit_compensation")]
        before = self.monetary_rows(self.apps)

        apps = self.migrate()

        Reservation = apps.get_model("payments", "MoneyReservation")
        self.assertEqual(self.monetary_rows(apps), before)
        for chain in (first, second):
            self.assertIsNone(Reservation.objects.get(pk=chain[0].pk).previous_refund_id)
            self.assertEqual(Reservation.objects.get(pk=chain[1].pk).previous_refund_id, chain[0].pk)
        for part in deposit_parts:
            self.assertIsNone(Reservation.objects.get(pk=part.pk).previous_refund_id)

    def test_invalid_legacy_shares_roll_back_links_schema_and_migration_record(self):
        payment = self.source("invalid-shares")
        for index in range(3):
            self.refund(payment, index, 1, 0)
        before = self.monetary_rows(self.apps)

        with self.assertRaisesRegex(RuntimeError, "Cannot reconstruct refund order"):
            self.migrate()

        self.assertNotIn(self.migrate_to, MigrationRecorder(connection).applied_migrations())
        self.assertEqual(self.monetary_rows(self.apps), before)
        with connection.cursor() as cursor:
            columns = {column.name for column in connection.introspection.get_table_description(
                cursor, "payments_moneyreservation",
            )}
        self.assertNotIn("previous_refund_id", columns)
        self.assertNotIn("payments_outgoingattempt", connection.introspection.table_names())
        self.assertNotIn("payments_outgoingrequest", connection.introspection.table_names())
