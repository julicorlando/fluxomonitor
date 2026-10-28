# NetControl no FluxoMonitor

Módulo isolado de inventário e topologia. Cadastro pelo Django Admin, painel em /netcontrol/, API autenticada em /netcontrol/topology.json.

1. Configurar variável SECRET_KEY e banco de dados normalmente.
2. Executar `python manage.py migrate`.
3. Entrar no painel com usuário staff e cadastrar Unidade, Equipamentos e Links.
4. RouterOS v7: configurar serviço REST HTTPS com certificado válido, usuário de leitura, IP e nome da variável de ambiente da senha em cada ativo.
5. Definir a variável referenciada (ex.: NETCONTROL_ROUTER1_PASSWORD) no Coolify.
6. Instalar requests pelo requirements e executar `python manage.py poll_network` (ou `--asset ID`).
7. Agendar o comando via cron externo se desejar atualizações.

Não escreve em roteadores; apenas HTTP GET de interfaces. Não executa descoberta ativa da rede, e mapeamento de links depende de cadastro manual. O modelo prevê SNMP, porém coletor SNMP ainda não foi implementado nesta primeira integração. Status "error" significa falha de consulta, não necessariamente offline. Nunca exponha o endpoint REST à internet pública; mantenha acesso por rede de gerenciamento ou VPN. A conta precisa de permissões mínimas para consultar interfaces.

## Coolify (Docker Compose)

A branch `main` usa compose.yaml. O serviço `netcontrol_collector` consulta a API RouterOS v7 a cada 120 segundos; não executa alterações. Adicione opcionalmente `NETCONTROL_ROUTER1_PASSWORD` às variáveis do stack no Coolify. Se usar esse nome em `credential_env` do ativo, o valor é transmitido aos serviços `web` e `netcontrol_collector` via Compose. Para outros roteadores, declare também seus nomes de variáveis no `compose.yaml` e no Coolify antes de usar. Use VPN e certificado HTTPS válido para a API REST. O comando de migrações já consta em `entrypoint.sh`. Não é necessário alterar o banco existente manualmente.
