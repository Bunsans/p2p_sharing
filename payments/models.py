"""Payment history; writes go through services and the command worker."""

from uuid import uuid4

from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Settlement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    booking_ref = models.CharField(max_length=128, unique=True)
    renter_ref = models.CharField(max_length=128)
    owner_ref = models.CharField(max_length=128)
    terms_version = models.PositiveIntegerField()
    owner_kopecks = models.BigIntegerField()
    commission_kopecks = models.BigIntegerField()
    rent_kopecks = models.BigIntegerField()
    deposit_kopecks = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="RUB")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(owner_kopecks__gt=0, commission_kopecks__gte=0,
                            deposit_kopecks__gte=0),
                name="settlement_nonnegative_money",
            ),
            models.CheckConstraint(
                condition=Q(rent_kopecks=F("owner_kopecks") + F("commission_kopecks")),
                name="settlement_rent_sum",
            ),
            models.CheckConstraint(
                condition=Q(terms_version__gt=0, currency="RUB"),
                name="settlement_version_currency",
            ),
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")),
                name="settlement_positive_duration",
            ),
            models.CheckConstraint(
                condition=~Q(renter_ref=F("owner_ref")),
                name="settlement_distinct_parties",
            ),
        ]


class Charge(models.Model):
    class Purpose(models.TextChoices):
        RENT = "rent"
        DEPOSIT = "deposit"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    settlement = models.ForeignKey(Settlement, on_delete=models.PROTECT, related_name="charges")
    purpose = models.CharField(max_length=16, choices=Purpose)
    amount_kopecks = models.BigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["settlement", "purpose"], name="charge_unique_purpose"),
            models.CheckConstraint(
                condition=Q(amount_kopecks__gte=0, purpose__in=["rent", "deposit"]),
                name="charge_valid_amount_purpose",
            ),
        ]


class PaymentAttempt(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        UNKNOWN = "unknown"
        SUCCEEDED = "succeeded"
        CANCELED = "canceled"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    charge = models.ForeignKey(Charge, on_delete=models.PROTECT, related_name="attempts")
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    external_id = models.CharField(max_length=128, blank=True)
    provider = models.CharField(max_length=32, default="simulator")
    environment = models.CharField(max_length=16, default="test")
    account_ref = models.CharField(max_length=128, default="local")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["charge"], condition=~Q(status="canceled"),
                name="attempt_one_not_canceled",
            ),
            models.UniqueConstraint(
                fields=["provider", "environment", "account_ref", "external_id"],
                condition=~Q(external_id=""), name="attempt_scoped_external_id",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["pending", "unknown", "succeeded", "canceled"]),
                name="attempt_valid_status",
            ),
        ]


class PaymentRequest(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    attempt = models.ForeignKey(PaymentAttempt, on_delete=models.PROTECT, related_name="requests")
    actor_ref = models.CharField(max_length=128)
    expected_terms_version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(expected_terms_version__gt=0), name="request_positive_version",
            ),
        ]


class ProviderCommand(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        DONE = "done"
        NEEDS_REVIEW = "needs_review"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    attempt = models.OneToOneField(PaymentAttempt, on_delete=models.PROTECT, related_name="command")
    idempotency_key = models.UUIDField(default=uuid4, unique=True, editable=False)
    payload = models.JSONField()
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    first_sent_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="command_due_idx"),
            models.Index(fields=["status", "lease_until"], name="command_lease_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["queued", "running", "done", "needs_review"]),
                name="command_valid_status",
            ),
        ]


class ProviderObservation(models.Model):
    """Keep valid replies, including a late reply from an expired worker lease."""

    command = models.ForeignKey(ProviderCommand, on_delete=models.PROTECT, related_name="observations")
    lease_token = models.UUIDField()
    status = models.CharField(max_length=16, choices=PaymentAttempt.Status)
    external_id = models.CharField(max_length=128)
    amount_kopecks = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    applied = models.BooleanField(default=False)
    received_at = models.DateTimeField(auto_now_add=True)
