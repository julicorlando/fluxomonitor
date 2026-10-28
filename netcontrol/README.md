# NetControl no FluxoMonitor

Módulo isolado de inventário e topologia. Cadastro pelo Django Admin, painel em /netcontrol/, API autenticada em /netcontrol/topology.json.

1. Configurar variável SECRET_KEY e banco de dados normalmente.
2. Executar `python manage.py migrate`.
3. Entrar no painel com usuário staff e cadastrar Unidade, Equipamentos e Links.
4. RouterOS v7: configurar serviço REST HTTPS com certificado válido, usuário de leitura, IP e nome da variável de ambiente da senha em cada ativo.
5. Switch SNMP v2c: usar comunidade de leitura somente, limitar a origem à rede do coletor e configurar IP, protocolo `snmp` e referência da variável de comunidade.
6. Definir a variável referenciada no Coolify e mapeá-la no serviço `netcontrol_collector` do Compose.
7. O serviço `netcontrol_collector` executa `python manage.py poll_network` a cada 120 segundos.

Não altera dispositivos: RouterOS usa apenas HTTP GET; SNMP usa somente GET/WALK e aceita apenas IP privado. A coleta SNMP grava nome, estado, velocidade e MAC das interfaces IF-MIB; não coleta ainda VLAN, PoE, tráfego ou LLDP. O mapa de links continua exigindo cadastro ou evidência externa. Status "error" significa falha de consulta, não necessariamente offline. Nunca exponha o endpoint REST à internet pública; mantenha acesso por rede de gerenciamento ou VPN. A conta precisa de permissões mínimas para consultar interfaces.

## Coolify (Docker Compose)

A branch `main` usa compose.yaml. O serviço `netcontrol_collector` consulta RouterOS v7 e SNMP v2c a cada 120 segundos. `CRED_SG2` é encaminhada somente ao coletor para o switch atualmente cadastrado. Para cada novo segredo, mapeie sua variável somente no serviço do coletor. Use uma comunidade SNMP somente leitura e restrinja a ACL do switch ao IP/rede do coletor. O comando de migrações já consta em `entrypoint.sh`; esta implementação não altera o schema do banco.
