import re
from collections import defaultdict
from django.db import migrations


def restore_sender_bindings(apps, schema_editor):
    alias = schema_editor.connection.alias
    Contact = apps.get_model('helpdesk', 'Contact')
    Legacy = apps.get_model('helpdesk', 'VerifiedChatIdentity')
    Binding = apps.get_model('helpdesk', 'SenderIdentity')
    Audit = apps.get_model('flow', 'AuditLog')
    Inbound = apps.get_model('helpdesk', 'Inbound')
    Pending = apps.get_model('helpdesk', 'UnresolvedInbound')
    contacts = {c.pk: c for c in Contact.objects.using(alias).all()}
    candidates = defaultdict(dict)
    for item in Legacy.objects.using(alias).all().iterator():
        contact = contacts.get(item.contact_id)
        if contact and item.phone == contact.phone and re.fullmatch(r'[0-9]+@lid', item.chat_id):
            candidates[item.chat_id][contact.pk] = {'phone': item.phone, 'source': 'legacy', 'confirmed_by': ''}
    # The old outbound cache may already have overwritten a manually confirmed LID.
    # Recover it only from an audit plus the original incoming phone snapshot.
    for log in Audit.objects.using(alias).filter(action='support_lid_verified').order_by('at', 'pk').iterator():
        data = log.changes if isinstance(log.changes, dict) else {}
        if data.get('verification') != 'master_confirmed_out_of_band':
            continue
        chat = data.get('lid')
        try:
            contact_id = int(data.get('contact_id') or log.object_id)
        except (ValueError, TypeError):
            continue
        contact = contacts.get(contact_id)
        if not contact or not isinstance(chat, str) or not re.fullmatch(r'[0-9]+@lid', chat):
            continue
        if data.get('phone'):
            proved = data['phone'] == contact.phone
        else:
            external = Pending.objects.using(alias).filter(sender=chat, status='linked').values_list('external_id', flat=True)
            proved = Inbound.objects.using(alias).filter(contact_id=contact.pk, phone=contact.phone, external_id__in=external).exists()
        if not proved:
            continue
        candidates[chat][contact.pk] = {'phone': contact.phone, 'source': 'master', 'confirmed_by': log.actor[:150]}
    restored = 0
    conflicts = 0
    for chat, owners in candidates.items():
        if len(owners) != 1:
            conflicts += 1
            continue
        contact_id, details = next(iter(owners.items()))
        _, created = Binding.objects.using(alias).get_or_create(chat_id=chat, defaults={'contact_id': contact_id, **details})
        restored += int(created)
    Audit.objects.using(alias).create(actor='sistema', action='support_id_upgrade', object_type='SenderIdentity', object_id='migration',
        changes={'restored': restored, 'conflicts_left_unassigned': conflicts})


class Migration(migrations.Migration):
    dependencies = [('helpdesk', '0007_senderidentity'), ('flow', '0029_final_replay_manual_destination')]
    operations = [migrations.RunPython(restore_sender_bindings, migrations.RunPython.noop)]
