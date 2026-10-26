from django.contrib import admin
from .models import Company, Contact, AgentGrant, Ticket, TicketUpdate, Satisfaction, Inbound, Outbound, SenderIdentity, SlaPolicy, CompanySlaConfig, SlaAgentProfile, SlaAlert, Asset, Maintenance, KnowledgeArticle, KnowledgeSuggestion, Problem


# Read-only here: writes go through the scoped UI and transactional services.
class ReadOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in (Company, Contact, AgentGrant, Ticket, TicketUpdate, Satisfaction, Inbound, Outbound, SenderIdentity, SlaPolicy, CompanySlaConfig, SlaAgentProfile, SlaAlert, Asset, Maintenance, KnowledgeArticle, KnowledgeSuggestion, Problem):
    admin.site.register(model, ReadOnlyAdmin)
