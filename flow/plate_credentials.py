import base64
import hashlib
from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


def cipher():
    # Domain separation from Django signing. Existing SECRET_KEY_FALLBACKS allow rotation.
    keys=[settings.SECRET_KEY]+list(getattr(settings,'SECRET_KEY_FALLBACKS',[]))
    return MultiFernet([Fernet(base64.urlsafe_b64encode(hashlib.sha256(('fluxo.nvr.credentials.v1:'+key).encode()).digest())) for key in keys])

def encrypt_password(value): return cipher().encrypt(value.encode()).decode() if value else ''
def decrypt_password(value): return cipher().decrypt(value.encode()).decode() if value else ''
