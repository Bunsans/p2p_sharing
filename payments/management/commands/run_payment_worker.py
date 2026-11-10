from django.core.management.base import BaseCommand, CommandError

from payments.simulator import Simulator
from payments.worker import execute_one


class Command(BaseCommand):
    help = "Execute one bounded batch of LOCAL SIMULATED payments (no bank API)."

    def add_arguments(self, parser):
        parser.add_argument("--simulate", action="store_true", help="Required: acknowledge local simulation")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not options["simulate"]:
            raise CommandError("Only local simulation is implemented; pass --simulate explicitly.")
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("--limit must be between 1 and 1000")
        processed = 0
        for _ in range(options["limit"]):
            if not execute_one(Simulator()):
                break
            processed += 1
        self.stdout.write(f"LOCAL SIMULATION: processed {processed} command(s); no bank calls.")
