"""Deterministic local success, explicitly unrelated to bank sandbox acceptance."""

from uuid import UUID

from .models import ProviderCommand
from .worker import PaymentResult


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
