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


class SlaPolicy(models.Model):
    PRIORITIES = [('critical', 'Crítica'), ('high', 'Alta'), ('normal', 'Normal'), ('low', 'Baixa')]
    priority = models.CharField(max_length=12, choices=PRIORITIES, unique=True)
    response_minutes = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(525600)])
    resolution_minutes = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(525600)])

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(response_minutes__gte=1) &
                       Q(resolution_minutes__gte=models.F('response_minutes')), name='support_sla_valid_targets')]

    def __str__(self):
        return self.get_priority_display()


class Ticket(models.Model):
    CATEGORIES = [('general', 'Geral / triagem'), ('hardware', 'Computadores e equipamentos'),
                  ('network', 'Rede e internet'), ('software', 'Sistemas e aplicativos'),
                  ('access', 'Acessos e permissões'), ('email', 'E-mail'),
                  ('printing', 'Impressoras'), ('security', 'Segurança da informação')]
    priority = models.CharField(max_length=12, choices=SlaPolicy.PRIORITIES, default='normal')
    category = models.CharField(max_length=16, choices=CATEGORIES, default='general')
    sla_started_at = models.DateTimeField(null=True, blank=True)
    response_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    resolution_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    first_responded_at = models.DateTimeField(null=True, blank=True)
    sla_calendar = models.JSONField(default=dict, blank=True)
    sla_pauses = models.JSONField(default=list, blank=True)
    sla_paused_at = models.DateTimeField(null=True, blank=True)
    sla_cycle = models.PositiveIntegerField(default=1)
    asset = models.ForeignKey('Asset', null=True, blank=True, on_delete=models.PROTECT, related_name='tickets')
    problem = models.ForeignKey('Problem', null=True, blank=True, on_delete=models.PROTECT, related_name='tickets')
    reopen_count = models.PositiveIntegerField(default=0)

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


class VerifiedChatIdentity(models.Model):
    """WAHA-confirmed phone-to-chat identity, never derived from message body."""
    contact = models.OneToOneField(Contact, on_delete=models.CASCADE, related_name='verified_chat')
    phone = models.CharField(max_length=14)
    chat_id = models.CharField(max_length=80, db_index=True)
    checked_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['chat_id', 'phone'], name='support_chat_identity')]


class UnresolvedInbound(models.Model):
    """Quarantined WhatsApp message; never enters the bot without verified identity."""
    external_id = models.CharField(max_length=250, unique=True)
    sender = models.CharField(max_length=100, db_index=True)
    text = models.TextField(max_length=4000, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    attempts = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=24, default='unresolved')


class SenderIdentity(models.Model):
    """Durable inbound identity, separate from WAHA's mutable outbound destination cache."""
    chat_id = models.CharField(max_length=80, unique=True)
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='sender_identities')
    phone = models.CharField(max_length=14)
    source = models.CharField(max_length=12, choices=[('master', 'Confirmado pelo master'), ('gateway', 'Confirmado pelo WAHA'), ('legacy', 'Vínculo anterior validado')])
    confirmed_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


def default_schedule():
    return {str(day): [['08:00','18:00']] for day in range(5)}


class CompanySlaConfig(models.Model):
    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name='sla_config')
    mode = models.CharField(max_length=12, choices=[('business','Expediente'),('continuous','24×7')], default='business')
    schedule = models.JSONField(default=default_schedule)
    holidays = models.JSONField(default=list, blank=True)
    pause_waiting = models.BooleanField(default=True)
    alerts_enabled = models.BooleanField(default=True)
    warning_minutes = models.PositiveIntegerField(default=30, validators=[MinValueValidator(1),MaxValueValidator(1440)])
    escalation_minutes = models.PositiveIntegerField(default=60, validators=[MinValueValidator(0),MaxValueValidator(10080)])
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    weekly_enabled = models.BooleanField(default=False)
    weekly_day = models.PositiveSmallIntegerField(default=0, choices=[(0,'Segunda'),(1,'Terça'),(2,'Quarta'),(3,'Quinta'),(4,'Sexta'),(5,'Sábado'),(6,'Domingo')])
    weekly_time = models.TimeField(default='08:00')


class SlaAgentProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT)


class Asset(models.Model):
    KINDS = [('computer','Computador'),('printer','Impressora'),('server','Servidor'),('network','Rede'),('other','Outro')]
    STATES = [('active','Em uso'),('stock','Em estoque'),('maintenance','Em manutenção'),('retired','Desativado')]
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    tag = models.CharField(max_length=60)
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=12, choices=KINDS, default='computer')
    state = models.CharField(max_length=12, choices=STATES, default='active')
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT)
    serial = models.CharField(max_length=120, blank=True)
    location = models.CharField(max_length=200, blank=True)
    notes = models.TextField(max_length=4000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['company__code','tag']
        constraints = [models.UniqueConstraint(fields=['company','tag'], name='support_asset_tag')]
    def __str__(self):
        return f'{self.company.code} · {self.tag} · {self.name}'


class Maintenance(models.Model):
    asset = models.ForeignKey(Asset, related_name='maintenance', on_delete=models.PROTECT)
    date = models.DateField()
    description = models.TextField(max_length=4000)
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    next_date = models.DateField(null=True, blank=True)
    author = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['-date','-id']


class KnowledgeArticle(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    title = models.CharField(max_length=180)
    keywords = models.CharField(max_length=400, help_text='Separe por vírgula. Ex.: impressora, impressão, não imprime')
    category = models.CharField(max_length=16, choices=Ticket.CATEGORIES, default='general')
    procedure = models.TextField(max_length=4000)
    published = models.BooleanField(default=False)
    bot_enabled = models.BooleanField(default=False)
    author = models.CharField(max_length=150)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ['title','id']


class KnowledgeSuggestion(models.Model):
    article = models.ForeignKey(KnowledgeArticle, on_delete=models.PROTECT)
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT)
    helpful = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Problem(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    title = models.CharField(max_length=180)
    category = models.CharField(max_length=16, choices=Ticket.CATEGORIES, default='general')
    status = models.CharField(max_length=16, choices=[('investigating','Em investigação'),('known','Causa identificada'),('resolved','Resolvido')], default='investigating')
    root_cause = models.TextField(max_length=4000, blank=True)
    workaround = models.TextField(max_length=4000, blank=True)
    author = models.CharField(max_length=150)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ['-updated_at','-id']


class SlaAlert(models.Model):
    STATUS = [('pending','Na fila'),('sending','Enviando'),('accepted','Aceito pelo gateway'),('blocked','Configuração pendente'),('failed','Recusado'),('unknown','Resultado incerto'),('cancelled','Cancelado')]
    ticket = models.ForeignKey(Ticket, related_name='sla_alerts', null=True, blank=True, on_delete=models.PROTECT)
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    cycle = models.PositiveIntegerField(default=1)
    phase = models.CharField(max_length=12, choices=[('response','Primeira resposta'),('resolution','Resolução'),('weekly','Resumo semanal')])
    kind = models.CharField(max_length=12, choices=[('warning','Prazo próximo'),('overdue','Prazo vencido'),('escalation','Escalonamento'),('weekly','Resumo semanal')])
    key = models.CharField(max_length=150, unique=True)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT)
    phone = models.CharField(max_length=14, blank=True)
    text = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=STATUS, default='pending')
    detail = models.CharField(max_length=250, blank=True)
    gateway_id = models.CharField(max_length=250, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    attempted_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    class Meta:
        indexes = [models.Index(fields=['status','id'], name='support_sla_alert_queue')]
