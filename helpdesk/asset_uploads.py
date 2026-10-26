"""Private inventory attachments: validate bytes, compress photos and stream only to scoped staff."""
import io
from pathlib import Path
from uuid import uuid4
from PIL import Image, ImageOps, UnidentifiedImageError
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.utils.text import get_valid_filename

IMAGE_MAX_BYTES = 12 * 1024 * 1024
PDF_MAX_BYTES = 15 * 1024 * 1024


def prepare_attachment(upload, kind):
    name = get_valid_filename(Path(upload.name).name)[:180] or 'anexo'
    if kind == 'photo':
        if upload.size > IMAGE_MAX_BYTES:
            raise ValidationError('A imagem deve ter até 12 MB antes da compressão.')
        try:
            img = Image.open(upload)
            img.verify()
            upload.seek(0)
            img = Image.open(upload)
            if getattr(img, 'n_frames', 1) != 1:
                raise ValidationError('Envie uma fotografia estática.')
            img = ImageOps.exif_transpose(img).convert('RGB')
            img.thumbnail((1500, 1500), Image.Resampling.LANCZOS)
            for side, quality in [(1500, 74), (1200, 64), (1000, 55), (800, 45)]:
                img.thumbnail((side, side), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, 'WEBP', quality=quality, method=5)
                if buf.tell() <= 300 * 1024:
                    break
            if buf.tell() > 500 * 1024:
                raise ValidationError('Imagem excessivamente complexa; reduza sua resolução.')
            return ContentFile(buf.getvalue(), name=uuid4().hex + '.webp'), name
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ValidationError('Imagem inválida ou corrompida.') from exc
    if kind not in ('signed_term', 'document'):
        raise ValidationError('Tipo de anexo inválido.')
    if upload.size > PDF_MAX_BYTES:
        raise ValidationError('O PDF deve ter até 15 MB.')
    header = upload.read(5)
    upload.seek(0)
    if header != b'%PDF-':
        raise ValidationError('O documento precisa ser um arquivo PDF válido.')
    return ContentFile(upload.read(), name=uuid4().hex + '.pdf'), name
