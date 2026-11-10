from django.contrib import admin

from .models import (
    Charge, PaymentAttempt, PaymentRequest, ProviderCommand, ProviderObservation, Settlement,
)


class HistoryAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(Settlement)
class SettlementAdmin(HistoryAdmin):
    list_display = ("id", "booking_ref", "terms_version", "rent_kopecks", "deposit_kopecks")
    search_fields = ("booking_ref", "=id")


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(HistoryAdmin):
    list_display = ("id", "charge", "status", "provider", "environment", "updated_at")
    list_filter = ("status", "provider", "environment")
    search_fields = ("=id", "external_id")


@admin.register(ProviderCommand)
class ProviderCommandAdmin(HistoryAdmin):
    list_display = ("id", "status", "last_error_code", "attempt_count", "first_sent_at", "next_attempt_at")
    list_filter = ("status", "last_error_code")
    exclude = ("payload",)

    def get_readonly_fields(self, request, obj=None):
        return [name for name in super().get_readonly_fields(request, obj) if name != "payload"]


admin.site.register([Charge, PaymentRequest, ProviderObservation], HistoryAdmin)
