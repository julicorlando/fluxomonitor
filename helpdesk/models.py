from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

COMPANIES = [(code, code) for code in ('PDU', 'CM', 'SC', 'EVPI', 'PRED', 'FRT', 'TSP')]


class Company(models.Model):
    code = models.CharField(max_length=4, choices=COMPANIES, unique=True)
    name = models.CharField(max_length=150)
    last_ticket = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['code']
        constraints = [models.CheckConstraint(condition=Q(code__in=[c[0] for c in COMPANIES]), name='support_official_company')]

    def __str__(self):
        return self.code


class Contact(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=14, unique=True)
    email = models.EmailField()
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name', 'id']

    def __str__(self):
        return f'{self.name} · {self.company}'


class AgentGrant(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    company = models.ForeignKey(Company, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'company'], name='support_agent_company')]


class BotConfig(models.Model):
    enabled = models.BooleanField(default=True)
    webhook_ready = models.BooleanField(default=False)
    webhook_checked_at = models.DateTimeField(null=True, blank=True)
    last_webhook_at = models.DateTimeField(null=True, blank=True)
    worker_at = models.DateTimeField(null=True, blank=True)
    detail = models.CharField(max_length=250, blank=True)


class Ticket(models.Model):
    STATUS = [('open', 'Aberto'), ('working', 'Em atendimento'), ('waiting', 'Aguardando solicitante'),
              ('resolved', 'Resolvido'), ('closed', 'Concluído'), ('cancelled', 'Cancelado')]
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    code = models.CharField(max_length=30, unique=True)
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT)
    requester_name = models.CharField(max_length=150)
    requester_phone = models.CharField(max_length=14)
    requester_email = models.EmailField()
    target = models.CharField(max_length=200)
    description = models.TextField(max_length=4000)
    status = models.CharField(max_length=12, choices=STATUS, default='open')
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=['company', 'number'], name='support_company_ticket')]

    def __str__(self):
        return self.code


class Conversation(models.Model):
    contact = models.OneToOneField(Contact, on_delete=models.CASCADE)
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    state = models.CharField(max_length=24, default='idle')
    draft = models.JSONField(default=dict)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class Inbound(models.Model):
    external_id = models.CharField(max_length=250, unique=True)
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT)
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    phone = models.CharField(max_length=14)
    text = models.TextField(max_length=4000, blank=True)
    sent_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=250, blank=True)

    class Meta:
        indexes = [models.Index(fields=['processed_at', 'id'], name='support_inbox_pending'),
                   models.Index(fields=['contact', 'received_at'], name='support_inbox_contact')]


class TicketUpdate(models.Model):
    ticket = models.ForeignKey(Ticket, related_name='updates', on_delete=models.CASCADE)
    author = models.CharField(max_length=150)
    text = models.TextField(max_length=4000)
    internal = models.BooleanField(default=False)
    old_status = models.CharField(max_length=12, blank=True)
    new_status = models.CharField(max_length=12, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class Outbound(models.Model):
    STATUS = [('pending', 'Na fila'), ('sending', 'Enviando'), ('accepted', 'Aceito pelo gateway'),
              ('delivered', 'Entregue'), ('read', 'Lido'), ('failed', 'Falhou'), ('unknown', 'Resultado incerto'),
              ('blocked', 'Bloqueado')]
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT)
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    phone = models.CharField(max_length=14)
    ticket = models.ForeignKey(Ticket, null=True, blank=True, related_name='messages', on_delete=models.PROTECT)
    text = models.TextField()
    key = models.CharField(max_length=150, unique=True)
    status = models.CharField(max_length=12, choices=STATUS, default='pending')
    gateway_id = models.CharField(max_length=250, blank=True, db_index=True)
    detail = models.CharField(max_length=250, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    attempted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'id'], name='support_outbox_pending')]


class DeliveryAck(models.Model):
    external_id = models.CharField(max_length=250, unique=True)
    phone = models.CharField(max_length=14)
    rank = models.SmallIntegerField(default=0)


class Satisfaction(models.Model):
    ticket = models.ForeignKey(Ticket, related_name='surveys', on_delete=models.CASCADE)
    cycle = models.PositiveIntegerField()
    rating = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    feedback = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    answered_at = models.DateTimeField(null=True, blank=True)
    cancelled = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['ticket', 'cycle'], name='support_survey_cycle'),
                       models.CheckConstraint(condition=Q(rating__isnull=True) | Q(rating__gte=1, rating__lte=5), name='support_rating_1_5')]
