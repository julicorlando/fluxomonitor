import re
from django.core.exceptions import ValidationError


def normalize_phone(value):
    value = re.sub(r'[\s()+.\-]', '', str(value))
    if re.fullmatch(r'81(?:9[0-9]{8}|[2-5][0-9]{7})', value):
        value = '55' + value
    if not re.fullmatch(r'5581(?:9[0-9]{8}|[2-5][0-9]{7})', value):
        raise ValidationError('Informe um telefone brasileiro válido com DDD 81. Ex.: +55 81 99999-9999.')
    return '+' + value


def phone_from_chat(chat):
    if not isinstance(chat, str) or not chat.endswith(('@c.us', '@s.whatsapp.net')):
        return None
    try:
        return normalize_phone(chat.split('@')[0])
    except ValidationError:
        return None
