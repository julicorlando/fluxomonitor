# Axion Fluxo — Django

Aplicação para três câmeras: 192.168.1.235, 192.168.1.236 e 192.168.1.237. Painel protegido por login, atualização de consulta a cada 5 segundos, entradas/saídas por câmera e consolidadas, saldo diário inicial 0, gráfico por 15 minutos, pico por hora e intervalo, CSV e administrador.

## Implantar no Coolify

1. Coloque este projeto em um repositório Git ou no servidor. No Coolify crie um recurso Docker Compose usando `compose.yaml`.
2. Configure `SECRET_KEY` e `POSTGRES_PASSWORD` com valores aleatórios diferentes. Não use os exemplos. Gere cada segredo com `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
3. Implante. PostgreSQL usa volume persistente. O serviço web aplica migrações e cadastra as três câmeras automaticamente. Verifique disponibilidade de 8086 antes de implantar; altere o lado esquerdo de `8086:8000` se ocupado.
4. No terminal do serviço **web** execute `python manage.py createsuperuser` e siga as perguntas. Nenhuma senha padrão é criada.
5. Na Cloudflare, no túnel existente, configure `fluxo.axionwebdigital.com.br` para o serviço HTTP `10.0.0.147:8086`, desde que o processo cloudflared consiga alcançar esse endereço.
6. Abra `https://fluxo.axionwebdigital.com.br`. Acesse `/admin/` para renomear câmeras, cadastrar seus seriais e inspecionar envios originais.
7. Faça backup regular do PostgreSQL pelo Coolify. Não remova o volume de dados ao redeploy.

O container web escuta internamente 8000; 8086 é a porta publicada no servidor. O túnel encerra HTTPS. Não configure redirecionamento HTTP→HTTPS interno sem avaliar o proxy. DEBUG permanece desligado.

## APIs de consulta

Crie token no terminal web: `python manage.py create_api_key consulta`.
O token aparece somente nessa execução; guarde-o. Todas as APIs exigem `Authorization: Bearer TOKEN`, ou sessão autenticada de navegador.

| Método | Caminho | Uso |
|---|---|---|
| GET | `/api/v1/live/?date=2026-10-13` | Totais, câmeras, intervalos, picos e alertas |
| GET | `/api/v1/cameras/` | Cadastro e último contato |
| GET | `/api/v1/history/?start=2026-10-01&end=2026-10-13` | Histórico recalculado, até 367 dias |
| GET | `/api/v1/export.csv?date=2026-10-13` | CSV do dia |
| GET | `/health/` | Verificação básica do processo, sem autenticação |
| POST | `/api/v1/ingest/` | Leitura normalizada |
| POST | `/api/camera/dataUpload` | Captura nativa ou leitura normalizada |
| POST | `/api/camera/heartBeat` | Sinal de funcionamento |

Exemplo: `curl -H "Authorization: Bearer $FLUXO_TOKEN" 'https://fluxo.axionwebdigital.com.br/api/v1/live/'`.

## Recebimento e integração real

Crie um token exclusivo por câmera:
`python manage.py create_api_key camera-1 --camera 1 --ingest --no-read`
Repita para IDs mostrados no administrador. Um token associado a câmera fixa a identidade do envio; um token geral de ingestão requer `camera_id`.

Formato normalizado aceito:

```json
{
  "event_id": "camera1-20261013-120000",
  "timestamp": "2026-10-13T12:00:00-03:00",
  "entries": 10,
  "exits": 4,
  "mode": "delta",
  "counter_epoch": "2026-10-13"
}
```

`delta`: contagens daquele intervalo, não acumuladas. `cumulative`: contadores acumulados; calcula diferenças em ordem cronológica. `event_id` deve ser estável em reenvios. Não gere novo ID em cada tentativa. `counter_epoch` identifica um ciclo de contador e deve mudar somente após reset confirmado.

A primeira leitura acumulada vira referência e não é somada; sem histórico do início do dia haverá cobertura parcial. Uma queda sem ciclo identificado é marcada e não somada. Leituras tardias são ordenadas pelo horário do equipamento e recalculam o fechamento. Diferenças acumuladas atravessando meia-noite são atribuídas ao horário da leitura com alerta; precisão diária exige intervalos datados ou leitura no limite do dia. Picos usam entradas + saídas e o horário da leitura; para picos precisos, o adaptador precisa preservar o horário de cada intervalo nativo.

**O protocolo JSON nativo ainda não foi validado.** O endpoint guarda objetos desconhecidos em RawDelivery e responde 202, sem inventar contagens. A captura exige token no cabeçalho. As telas da câmera não demonstram suporte a cabeçalho Bearer, domínio ou HTTPS. Portanto não configure a câmera diretamente antes de confirmar esses recursos. Se incompatível, use um gateway local na rede 192.168.1.x para receber o envio e encaminhar por HTTPS autenticado. Esse gateway e MQTT ainda não estão implementados neste pacote. O IP 10.0.0.147 não é acessível diretamente pelas câmeras segundo a informação fornecida.

A configuração mostrada nas capturas está em 5 minutos e janela 00:00–21:00; essa frequência não equivale a tempo real de 5 segundos. Só altere depois de validar as capacidades do firmware e receber um exemplo real. Nenhum reset ou ajuste remoto é aplicado às câmeras pelo projeto.

## Fechamento

O serviço `closer` verifica dias encerrados a cada 60 segundos. Ao virar meia-noite em America/Sao_Paulo, salva o dia completo, incluindo 23:59:59; o dia novo começa em zero sem transportar saldo. Essa abordagem substitui um fechamento antecipado às 23:59, que perderia o último minuto. Fechamentos são recalculáveis e atualizados para dados tardios. DailyReport é o registro salvo; as consultas recalculam do histórico bruto normalizado.

Saldo = entradas − saídas; passagens = entradas + saídas. Não equivale a visitantes únicos. Online significa contato recebido nos últimos 180 s; intervalos de envio maiores mostrarão ausência de sinal recente, mesmo sem falha de equipamento.

## Desenvolvimento e testes

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DEBUG=1
python manage.py migrate
python manage.py seed_cameras
python manage.py createsuperuser
python manage.py runserver
python manage.py test
```

SQLite é somente o padrão para desenvolvimento; Compose usa PostgreSQL. Antes de produção valide Docker, Cloudflare, comunicação com equipamento e uma amostra JSON real. Não há implantação remota realizada automaticamente.

## Dispositivos e API web

Após redeploy, `/devices/` permite gestores (is_staff) adicionar, editar e desativar dispositivos. Sem limite de três. Campos: nome, IP, serial, local, fabricante, modelo e URL da API do equipamento. A estrutura Camera no banco permanece para preservar IDs, tokens e histórico; o nome público passa a Dispositivos.

- GET `/api/v1/devices/`: cadastro de dispositivos.
- GET `/api/v1/devices/1/?date=2026-10-14`: último contador recebido e totais do dia do dispositivo 1.
- GET `/api/v1/devices/1/readings/?limit=100`: leituras com ID, horário, entradas, saídas, modo e ciclo. Paginação por `before=ID`.
- POST de ingestão aceita `device_id`, preservando `camera_id` para compatibilidade.
- Token associado a dispositivo só consulta seu dispositivo, inclusive totais e histórico. Tokens gerais consultam todos.

API web do sistema = consultas HTTPS acima. API web do equipamento = endpoint do fabricante, ainda não confirmado. A URL cadastrada NÃO aciona coleta automática. Como o servidor não alcança a rede 192.168.1.x, consulta ativa exige VPN/rota ou coletor local. Precisamos de URL, autenticação e resposta real para implementar esse adaptador. Não abra as câmeras diretamente na internet.

## Gestão e análise

- `/devices/ID/`: painel individual. Filtros por dispositivo e grupo no painel geral.
- `/groups/`: agrupamentos; defina o grupo em cada cadastro.
- `include_in_total`: escolha se o contador entra na soma consolidada. Consulta individual sempre contabiliza o dispositivo selecionado.
- `expected_interval`: segundos entre leituras esperadas; online usa tolerância de três intervalos, mínimo 180 s.
- `/diagnostics/`: último envio, status de processamento e JSON com chaves sensíveis ocultadas.
- `/tokens/`: somente administradores; criação com escopos, segredo mostrado uma vez e revogação.
- `/users/`: somente administradores; perfis consulta, operador e administrador. Consulta não altera dispositivos; operador administra dispositivos e vê diagnóstico; administrador gerencia tokens e usuários. Todos consultam todos os locais; isolamento por unidade não implementado.
- `/reports/`: até 93 dias, comparação com período anterior de mesma duração, picos e alertas; exportação CSV para Excel e impressão/salvar PDF pelo navegador (não XLSX nem geração de PDF no servidor).
- Tema claro/escuro salvo no navegador; botão TV usa tela cheia.
- Ausência de leituras, gaps acima da tolerância e resets são indicados; cobertura observada não é certificação de precisão. Fechamento diário mantém alertas.
- Sem dados de demonstração inseridos no banco de produção. Sem coleta ativa antes de protocolo confirmado.

Demonstração: caixa no painel usa `/api/v1/demo/` com números fictícios identificados e sem gravação; exportação do painel fica oculta durante demonstração. Relatórios continuam sempre usando dados reais.

## Operação

Calendário em `/calendar/` (gestores): dias da semana e exceções fechadas; sem configuração = 24h. Alertas usam expediente e tolerância max(180s, 3×intervalo). Horários atravessando meia-noite ainda não suportados. As leituras fora do expediente são mantidas.
`/alerts/`: falha e recuperação no painel, sem notificação externa. Avaliado pelo serviço closer a cada minuto. `/events/`: anotações por dia/grupo, mostradas nos relatórios. Ranking compara passagens de todos os dispositivos exibidos. `/audit/`: alterações de dispositivos, grupos, tokens, usuários, calendário e eventos com ator; senhas e digests excluídos. Alterações diretas por SQL não são auditadas. Alterações de grupos de usuários e permissões são registradas pelos IDs envolvidos; privilégios staff/superuser do usuário também entram.
`/summaries/`: diários e semanais salvos idempotentemente após fechamento, sem envio externo; recomputados para leituras tardias. `/api/docs/`: endpoints, autenticação, exemplos Python e orientação Power BI. Não é console interativo/OpenAPI.

## WhatsApp por QR

Adicionado serviço WAHA dedicado, interno (sem porta pública), sessões em volume persistente. Antes de redeploy configure **WAHA_API_KEY** com segredo novo, diferente de SECRET_KEY e POSTGRES_PASSWORD. WAHA_IMAGE permite fixar versão/digest validado; padrão devlikeapro/waha:latest. Não altera outro serviço WAHA existente no servidor.
Administrador: `/whatsapp/` → Conectar → escanear QR em Aparelhos conectados → atualizar página → selecionar número com país/DDD OU grupo carregado da conta → salvar e ativar. QR e configuração são restritos a superusuário; chave do gateway só fica no ambiente.
Padrão 22:10 America/Sao_Paulo, entradas de dispositivos incluídos. Métrica selecionável: entradas ou passagens. Texto exato `Boa noite, fluxo final {total}`. O total é do dia até a execução, não fechamento contábil da meia-noite. Verificação pelo closer a cada minuto, janela de recuperação até 22:20; sessão offline antes da tentativa pode ser recuperada dentro dessa janela.
Envio bloqueado quando falta leitura válida do dia ou contato recente de algum dispositivo incluído. Dados de demonstração não são enviados. Esse controle não garante cobertura integral do dia; alertas de gaps devem ser verificados. Sem captura real dos contadores, o envio ficará bloqueado.
Uma tentativa por dia, com trava e registro único. Resposta do gateway = accepted, não comprova entrega no destinatário. Timeout/erro após tentativa = unknown sem reenvio automático, para evitar mensagem duplicada. Logs no painel. Não faz envio retroativo depois de 22:20 nem envio manual de teste. Desconectar desativa o agendamento. Configuração pode ser auditada.
WAHA é uma integração com sessão WhatsApp Web, não a API oficial Meta. Instalação e pareamento reais precisam de teste no servidor; testes locais usam gateway simulado.
Referências: https://waha.devlike.pro/swagger/ ; https://waha.devlike.pro/docs/how-to/send-messages/ ; https://waha.devlike.pro/docs/how-to/groups/
