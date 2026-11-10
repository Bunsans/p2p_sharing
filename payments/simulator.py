"""Deterministic local success, explicitly unrelated to bank sandbox acceptance."""

from uuid import UUID

from .models import ProviderCommand
from .worker import OutgoingResult, PaymentResult


class Simulator:
    def create_payment(self, payload, idempotency_key):
        rubles, kopecks = payload["amount"]["value"].split(".")
        return PaymentResult(
            "succeeded", f"sim_{idempotency_key.hex}",
            int(rubles) * 100 + int(kopecks), payload["amount"]["currency"],
        )

    def get_payment(self, external_id):
        if not external_id.startswith("sim_"):
            raise ValueError("not a simulated payment identifier")
        key = UUID(hex=external_id.removeprefix("sim_"))
        command = ProviderCommand.objects.get(
            idempotency_key=key, attempt__provider="simulator",
            attempt__environment="test", attempt__account_ref="local",
        )
        return self.create_payment(command.payload, key)

    def create_outgoing(self, payload, idempotency_key):
        operation = payload["operation"]
        if operation not in ("refund", "payout"):
            raise ValueError("unsupported simulated outgoing operation")
        rubles, kopecks = payload["amount"]["value"].split(".")
        return OutgoingResult(
            status="succeeded", external_id=f"sim_{operation}_{idempotency_key.hex}",
            amount_kopecks=int(rubles) * 100 + int(kopecks), currency=payload["amount"]["currency"],
            operation=operation, source_payment_id=payload["source_payment_id"],
            recipient_ref=payload["recipient_ref"],
        )

    def get_outgoing(self, operation, external_id):
        if operation not in ("refund", "payout") or not external_id.startswith(f"sim_{operation}_"):
            raise ValueError("not a simulated outgoing identifier for this operation")
        key = UUID(hex=external_id.removeprefix(f"sim_{operation}_"))
        command = ProviderCommand.objects.get(
            idempotency_key=key, outgoing_attempt__operation=operation,
            outgoing_attempt__provider="simulator", outgoing_attempt__environment="test",
            outgoing_attempt__account_ref="local",
        )
        return self.create_outgoing(command.payload, key)
