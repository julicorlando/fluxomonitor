"""Presentation-only discount. Stored readings and verification stay original."""
from decimal import Decimal,ROUND_FLOOR
from .models import WhatsAppConfig

def adjusted_entries(entries,percent):
    percent=Decimal(str(percent))
    if not percent.is_finite() or not Decimal(0)<=percent<=Decimal(100):
        raise ValueError('Margem inválida: use de 0 a 100%.')
    if not isinstance(entries,int) or isinstance(entries,bool) or entries<0:
        raise ValueError('Total de entradas inválido.')
    adjusted=int((Decimal(entries)*(Decimal(100)-percent)/100).to_integral_value(rounding=ROUND_FLOOR))
    return {'original':entries,'adjusted':adjusted,'deducted':entries-adjusted,'percent':format(percent,'.2f')}

def admin_adjustment(request,entries):
    if not request.user.is_superuser:return None
    percent=WhatsAppConfig.objects.filter(pk=1).values_list('error_margin_percent',flat=True).first()
    return adjusted_entries(entries,percent if percent is not None else 0)
