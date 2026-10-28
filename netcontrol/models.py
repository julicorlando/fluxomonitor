from django.db import models

class Site(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.CharField(max_length=250, blank=True)
    def __str__(self): return self.name

class NetworkAsset(models.Model):
    KINDS = [('router','Roteador/MikroTik'),('switch','Switch'),('ap','Access Point'),('camera','Câmera IP'),('printer','Impressora'),('nvr','NVR'),('server','Servidor'),('other','Outro')]
    PROTOCOLS = [('none','Sem coleta'),('snmp','SNMP v2c'),('routeros','RouterOS REST v7')]
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, choices=KINDS, default='other')
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name='assets')
    ip = models.GenericIPAddressField(null=True, blank=True)
    mac = models.CharField(max_length=17, blank=True)
    manufacturer = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=120, blank=True)
    location = models.CharField(max_length=180, blank=True)
    patrimony = models.CharField(max_length=100, blank=True)
    protocol = models.CharField(max_length=10, choices=PROTOCOLS, default='none')
    # Secrets are environment variables, never database plaintext.
    credential_env = models.CharField(max_length=100, blank=True, help_text='Nome da variável de ambiente com community SNMP ou senha RouterOS')
    username = models.CharField(max_length=80, blank=True)
    verify_tls = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    last_status = models.CharField(max_length=20, default='unknown')
    last_checked = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=240, blank=True)
    def __str__(self): return self.name
    class Meta:
        ordering = ['site__name','name']

class Interface(models.Model):
    asset = models.ForeignKey(NetworkAsset, on_delete=models.CASCADE, related_name='interfaces')
    index = models.PositiveIntegerField()
    name = models.CharField(max_length=120)
    status = models.CharField(max_length=20, default='unknown')
    speed_bps = models.BigIntegerField(null=True, blank=True)
    mac = models.CharField(max_length=32, blank=True)
    vlan = models.CharField(max_length=120, blank=True)
    last_seen = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['asset','index'], name='netcontrol_unique_asset_interface')]
    def __str__(self): return f'{self.asset}: {self.name}'

class NetworkLink(models.Model):
    source = models.ForeignKey(NetworkAsset, on_delete=models.CASCADE, related_name='links_out')
    target = models.ForeignKey(NetworkAsset, on_delete=models.CASCADE, related_name='links_in')
    source_interface = models.ForeignKey(Interface, null=True, blank=True, on_delete=models.SET_NULL)
    evidence = models.CharField(max_length=20, choices=[('manual','Manual'),('lldp','LLDP'),('inferred','Inferida')], default='manual')
    notes = models.CharField(max_length=180, blank=True)
    def __str__(self): return f'{self.source} → {self.target}'

class PollHistory(models.Model):
    asset = models.ForeignKey(NetworkAsset, on_delete=models.CASCADE, related_name='checks')
    created_at = models.DateTimeField(auto_now_add=True)
    successful = models.BooleanField(default=False)
    detail = models.CharField(max_length=240, blank=True)
    class Meta:
        ordering = ['-created_at']
