"""Read-only SNMP v2c inventory queries for NetControl devices."""

import asyncio
import ipaddress

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
    walk_cmd,
)


class SnmpPollError(Exception):
    """A sanitized SNMP collection failure."""


def parse_interface_table(var_binds):
    interfaces = {}
    table_prefix = (1, 3, 6, 1, 2, 1, 2, 2, 1)
    for oid_value, value in var_binds:
        parts = tuple(int(part) for part in str(oid_value).strip('.').split('.'))
        if len(parts) != len(table_prefix) + 2 or parts[:len(table_prefix)] != table_prefix:
            continue
        column, index = parts[-2], parts[-1]
        if index < 1:
            continue
        entry = interfaces.setdefault(index, {'index': index})
        if column == 2:  # ifDescr
            entry['name'] = value.prettyPrint()[:120]
        elif column == 5:  # ifSpeed (bits/second)
            entry['speed_bps'] = int(value)
        elif column == 6:  # ifPhysAddress
            try:
                raw_mac = value.asOctets()
            except AttributeError:
                raw_mac = b''
            entry['mac'] = ':'.join(f'{byte:02X}' for byte in raw_mac) if raw_mac else ''
        elif column == 8:  # ifOperStatus
            oper = int(value)
            entry['status'] = 'online' if oper == 1 else 'offline' if oper in (2, 6, 7) else 'unknown'
    return [item for item in interfaces.values() if item.get('name')]


async def _collect(ip, community):
    engine = SnmpEngine()
    target = await UdpTransportTarget.create((str(ip), 161), timeout=1.5, retries=1)
    auth = CommunityData(community, mpModel=1)
    context = ContextData()
    try:
        error, status, _index, values = await get_cmd(
            engine, auth, target, context,
            ObjectType(ObjectIdentity('1.3.6.1.2.1.1.1.0')),
            lookupMib=False,
        )
        if error or status:
            raise SnmpPollError('SNMP sem resposta válida (verifique IP, comunidade e ACL).')
        system_description = values[0][1].prettyPrint()[:180] if values else ''

        # IF-MIB::ifTable, limited to 512 rows to bound response size and work.
        rows = []
        async for error, status, _index, var_binds in walk_cmd(
            engine, auth, target, context,
            ObjectType(ObjectIdentity('1.3.6.1.2.1.2.2.1')),
            lexicographicMode=False,
            maxRows=512,
            lookupMib=False,
        ):
            if error or status:
                raise SnmpPollError('Falha ao consultar a tabela de interfaces SNMP.')
            rows.extend(var_binds)
        return {'system_description': system_description, 'interfaces': parse_interface_table(rows)}
    finally:
        engine.close_dispatcher()


def collect_snmp(ip, community):
    address = ipaddress.ip_address(ip)
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_multicast:
        raise SnmpPollError('Coleta SNMP permitida somente para endereços privados da rede.')
    try:
        return asyncio.run(_collect(address, community))
    except SnmpPollError:
        raise
    except Exception as exc:
        # Do not include request/auth objects or the community string in errors/logs.
        raise SnmpPollError('Falha ao consultar o equipamento via SNMP.') from exc
