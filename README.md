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

## Integração PanPanTech PFC (documentação EN v1.0 recebida)

Suporte implementado para push JSON de janelas. Cadastre serial exato de cada dispositivo. Crie token de recebimento exclusivo por dispositivo (painel Tokens ou create_api_key --camera ID --ingest --no-read). Na câmera: Server 10.0.0.147, Port 8086, Data Path /api/camera/dataUpload/TOKEN, HB Path /api/camera/heartBeat/TOKEN. O programa permite Data/HB Path; a aceitação de caminho com token deve ser confirmada em campo. Não compartilhe essas URLs. Alternativa com Bearer: /api/native/dataUpload e /api/native/heartBeat. Endpoints antigos normalizados continuam compatíveis.
Formato real: sn, in, out, passby, turnback, avgStayTime, inAdult/outAdult/passbyAdult/turnbackAdult, inChild/outChild/passbyChild/turnbackChild, total, startTime/endTime/time. Timestamp das janelas em segundos. eventList opcional é preservado, mas não interpretado em contagens; o exemplo do manual tem valor incompatível com a unidade declarada e precisa de confirmação antes de uso.
Heartbeat responde code=0, msg=success, data.sn e data.time com relógio do servidor (Unix segundos). Câmera usa essa resposta para sincronizar; configure NTP do servidor corretamente. Falhas retornam code não zero e não são reconhecidas como sucesso.
Intervalo 0 = tempo real confirmado no manual. Cada mesma janela sn + endTime é substituída apenas quando time aumenta; duplicata idêntica não soma; versão antiga é reconhecida sem substituir. Mesma versão com conteúdo divergente, início alterado e janelas distintas sobrepostas são rejeitados para não inventar reconciliação. Recebimento serializado por dispositivo. Atualizações retroativas recalculam fechamento. Picos por horário ainda usam fim da janela; precisão limitada quando janela é longa ou atravessa meia-noite.
Token próprio autentica o receptor. A documentação menciona assinatura no push, mas não define seus cabeçalhos/algoritmo; assinatura nativa de push ainda não é validada. Para pull, sign_parameters implementa a fórmula de referência ASCII + parâmetros nome=valor separados por & + &ServiceSecret + MD5 uppercase. **Não usa a chave de exemplo do documento.** Falta ServiceSecret real e exemplo compatível com firmware: manual avisa que separador varia. Consulta ativa na porta 8020 ainda não está habilitada.
Não combine push e pull sem deduplicação cruzada. O SN é a identidade lógica; banco preserva IDs internos já utilizados. Testar uma câmera com tcpdump, verificar resposta code 0 e comparar janelas com programa de PC antes de ativar as três ou o WhatsApp.

Geração automática por dispositivo: administrador usa botão “Gerar token de recebimento” em /devices/. Serial obrigatório. Sistema gera segredo, vincula exclusivamente ao dispositivo, limita a recebimento e mostra Data/HB Path prontos. Segredo aparece uma vez, somente hash permanece no banco. Regenerar revoga somente tokens anteriores criados por esse botão; demais chaves são preservadas. POST com CSRF obrigatório.

## Broker MQTT no Coolify

Serviço mqtt (Eclipse Mosquitto 2), com volume fluxo_mqtt, reinício automático, autenticação obrigatória, limite de mensagem 1 MiB e healthcheck autenticado. Aceita somente tópicos fluxomonitor/#. Credencial compartilhada inicialmente; ainda não há isolamento por câmera.
Antes do redeploy, adicione MQTT_USERNAME=fluxo, MQTT_PASSWORD com senha exclusiva e MQTT_BIND_IP=10.0.0.147. O bind exige que esse IP exista no host Docker; altere se migrar o servidor. Não altere POSTGRES_PASSWORD, SECRET_KEY ou WAHA_API_KEY existentes. A senha MQTT é reprocessada em hash a cada inicialização; alterar requer recriar o serviço.
Câmera: Host 10.0.0.147, Port 1883, usuário/senha acima, Data TOPIC fluxomonitor/235/data, HeartBeat TOPIC fluxomonitor/235/heartbeat. Use 236 e 237 para as demais. Campos Authentication/SSL exigem confirmação do fabricante. Este listener é TCP sem TLS, destinado à rede local; mantenha acesso restrito à rede dos equipamentos. Não usa o domínio HTTP/Cloudflare Tunnel.
Após deploy, use o terminal do serviço mqtt para observar uma mensagem real (o shell expande variáveis sem precisar colar senha):
```sh
mosquitto_sub -h 127.0.0.1 -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" -t 'fluxomonitor/+/data' -t 'fluxomonitor/+/heartbeat' -v
```
O broker recebe e distribui mensagens, mas **ainda não grava contagens no Django**. Falta capturar o payload real e confirmar regras MQTT do fabricante, inclusive eventuais respostas de aplicação, antes de implementar o coletor e deduplicação. O healthcheck publica somente fluxomonitor/health, sem dados de contagem. QoS/armazenamento do broker não garantem recuperação de todas as contagens sem suporte do dispositivo e coletor.
Referência: https://mosquitto.org/man/mosquitto-conf-5.html

## Coleta ativa PFC pela porta 8020

Assinatura comprovada em 14/10/2026 para .235: parâmetros em ordem ASCII, nomes e valores concatenados SEM separadores, depois ServiceSecret, MD5 uppercase. A chave encontrada no cliente do fabricante foi aceita tanto em httpPathGet quanto PassengerFlowDataGet. Configure-a em PFC_SERVICE_SECRET; não é o token de recebimento Django. A fórmula de referência com & continua separada e não é usada pelo coletor.

No Coolify configure PFC_SERVICE_SECRET com a chave validada, PFC_PULL_IPS=192.168.1.235 e PFC_POLL_SECONDS=5. Redeploy main cria serviço collector e executa migração 0007 automaticamente. Banco e senhas existentes permanecem. O collector consulta IP cadastrado no painel; api_url é apenas referência e não controla o destino. Se o broker MQTT estiver no Compose, mantenha suas variáveis configuradas mesmo que a câmera não suporte MQTT.

Desabilite Address 1/2 de upload HTTP na câmera para usar uma única fonte. O servidor bloqueia ingestão de contagens para IPs em PFC_PULL_IPS; heartbeat continua permitido. O coletor também recusa dispositivo com leituras de outra fonte. Não apague históricos para contornar o bloqueio: revise a origem dos dados antes de migrar uma câmera já utilizada.

Primeira execução verifica startIndex como offset usando posições 0 e 1, encontra o primeiro evento de hoje em America/Sao_Paulo com pesquisa exponencial/binária, sem importar meses históricos. Essa semântica ainda exige validação no equipamento; se um índice fora do intervalo gerar erro em vez de lista vazia, o coletor bloqueia e o diagnóstico mostra o motivo, para adaptar com resposta real. Não supõe que startIndex seja idIndex. Importa até 100 eventos por câmera/ciclo, recuperando backlog antes de alcançar o presente. Consultas HTTP usam timeout de 8 segundos; 5s é intervalo mínimo, não garantia de atualização em 5s sob falha/backlog.

Eventos 0/1 viram entradas/saídas delta, timestamp Unix segundos, identidade pfc-event:idIndex por dispositivo. Eventos 2/3/4 e outros são preservados no JSON bruto e não somam entradas/saídas. Mesmo ID com conteúdo diferente bloqueia a coleta. Verifica o último evento da posição anterior para detectar deslocamento do buffer e relocaliza hoje com deduplicação. Após deslocamento, registros anteriores a hoje não são recuperados automaticamente. Retomada normal com cursor preservado recupera registros pendentes, incluindo o dia anterior, enquanto ainda existirem no equipamento. Horários >5min no futuro bloqueiam avanço. Não altera relógio nem zera equipamento.

Painel Diagnóstico mostra última consulta e erro; logs collector mostram IP e quantidade consultada sem segredo. last_seen avança após resposta válida mesmo sem eventos. Contagem zero com câmera online pode ser legítima. Confirme os horários/eventos no cliente de PC; contadores acumulados históricos do PC não devem ser comparados diretamente ao total de hoje. Primeiro valide .235; depois altere PFC_PULL_IPS para 192.168.1.235,192.168.1.236,192.168.1.237 e redeploy. As três devem estar cadastradas e habilitadas, e a chave deve funcionar em cada uma (configuração atual usa chave compartilhada). Registre serial exato quando disponível, mas pull vincula a leitura ao IP cadastrado.

50 testes Django passam localmente, incluindo coletor simulado. Não há teste de rede real neste ambiente nem acesso ao servidor privado. É necessária confirmação de paginação na primeira execução em hardware.

Atualização de paginação: após diagnóstico real indicar que startIndex não é offset, o coletor também testa startIndex=primeiro idIndex+1 e compara o evento seguinte. Se confirmado, usa cursor absoluto último idIndex+1 (inclusive com gaps), verifica âncora pelo próprio ID e localiza hoje por IDs. Modo é persistido na migração 0008. Não converte cursores anteriores de offset silenciosamente. Se nenhum modo for comprovado, mantém bloqueio com mensagem de paginação desconhecida. 51 testes passam; confirmação final do modo absoluto depende da primeira consulta real após redeploy.

Respostas de API recusadas por formato são agora preservadas em Diagnóstico com request.startIndex/getQuantity e response original, após rollback da importação. Erros idênticos consecutivos não geram novos registros. Nenhuma assinatura/chave é incluída. 52 testes passam.

Firmware real confirmou resposta sem PassengerFlowData quando rptQuantity=0 e result indica sucesso. O coletor aceita exclusivamente essa ausência como página vazia; quantidade positiva sem lista continua bloqueada. Busca inicial por IDs pode ultrapassar o último registro e usa página vazia para localizar os eventos de hoje. 54 testes passam.

IP 192.168.1.237 permite exatamente até dois cadastros com nomes diferentes; todos os outros IPs continuam únicos. Migração 0009 impõe limites no banco por slots 0/1. Coletor consulta um cadastro habilitado de .237 e copia suas leituras confirmadas para o segundo, sem nova consulta nem recontagem por replay. Cada cadastro tem total próprio; ambos incluídos duplicam intencionalmente a contribuição à soma. Cadastre a réplica deixando serial vazio (serial físico continua único), marque ativo e incluir no total conforme desejado. PFC_PULL_IPS contém .237 apenas uma vez. Seed preserva duplicata no redeploy.

Regra WhatsApp atualizada: sempre total de entradas (não passagens), mesmo com dispositivos sem leitura/contato recente; sem entradas envia 0. Valor reflete dados disponíveis e réplicas incluídas. Falhas/sessão WhatsApp offline continuam exigindo recuperação. Registro skipped pode ser retomado dentro da janela configurada; accepted/sending/unknown continuam bloqueando repetição. Não há reenvio fora da janela nem envio manual automático. Esta regra substitui o bloqueio por cobertura e seleção de métrica anteriores.

Botão Teste de envio em /whatsapp/: somente administrador, POST protegido por CSRF, usa destinatário salvo e total atual de entradas, não cria WhatsAppDelivery diário nem altera horário. Requer sessão WORKING. Resultado accepted/unknown registrado na auditoria, sem repetição automática. 58 testes passam.

WhatsApp permite configurar quantidade (1–24) e horários distintos HH:MM separados por vírgula ou linha. Todos os dias, Brasília, destino salvo compartilhado; cada envio usa entradas disponíveis naquele momento. Histórico e deduplicação por data+horário (migração 0010 preserva horário configurado atual e associa os históricos antigos a ele). Janela de recuperação 10 minutos por horário dentro do próprio dia; horários 23:5x não recuperam após meia-noite. Alterar horários para outro já passado há mais de 10min não dispara retroativo. Botão teste continua separado. 60 testes passam.

Mensagens por horário: Bom dia fluxo parcial {entradas} 06:00–11:59; Boa tarde 12:00–17:59; Boa noite a partir de 18:00 até o final. Mensagem final exata: fluxo final {entradas}, às 22:10 em dias normais e às 21:00 domingos/feriados/dias especiais. Final incluído automaticamente; horários configurados anteriores são parciais, posteriores ao final não disparam. Calendário permite marcar Feriado/dia especial independentemente de Fechado; não há importação automática de feriados nacionais/municipais. Cadastro da mesma data atualiza sua exceção.
Destinatários: múltiplos números separados por linha/vírgula e múltiplos grupos selecionáveis, podem coexistir, limite 50. Destino antigo preservado por fallback. Identidade de envio data+horário+destinatário, falha em um não impede os demais. Teste dispara para todos os salvos sem consumir horários. 63 testes passam.

## Importar histórico salvo no dispositivo

Redeploy main cria serviço history, usando as mesmas variáveis PFC do collector. Migração 0013 automática. Gestores acessam /imports/ (menu Importar histórico), escolhem dispositivo e intervalo dentro dos últimos 90 dias e iniciam. Uma tarefa ativa por dispositivo; câmera precisa estar habilitada e seu IP listado em PFC_PULL_IPS. O serviço processa uma tarefa por vez em lotes de até 100 eventos, com pausa de 2s. Progresso persistido no banco; retoma após reinício. Atualize a página para ver progresso. Parar cancela os próximos lotes e preserva dados já importados.
Percorre desde o primeiro ID disponível até uma página vazia, validando paginação por ID/offset sem busca binária por data. Ajustes no relógio podem fazer timestamps retrocederem: a data de cada evento é filtrada individualmente, sem encerrar ao cruzar a data final. Não altera cursor da coleta atual. Usa mesmos IDs pfc-event:idIndex para impedir duplicidade entre importação, coleta atual e nova importação. Eventos 0/1 viram entradas/saídas; demais são preservados no JSON sem somar. Datas e relatórios em Brasília; dias passados com leituras novas são recalculados. Inicialmente mostra a data do primeiro evento por ID (não necessariamente a menor data): 90 dias é capacidade anunciada, não promessa de disponibilidade. Concluído com zero pode significar que o período não existe mais na câmera, não fluxo zero certificado.
Selecionar réplica da .237 encaminha a tarefa ao cadastro habilitado principal para o mesmo IP; collector copia novas leituras para a réplica, mantendo o fator dois na soma quando ambos incluídos. Não envia WhatsApp retroativo; teste e próximos agendamentos passam a considerar entradas importadas quando pertencem ao dia atual. Erros de assinatura/formato param a tarefa e aparecem na página/Diagnóstico; o botão Reiniciar com segurança em tarefas falhas/canceladas reexamina desde o início, zera apenas indicadores da tentativa e preserva leituras/deduplicação e cursor ao vivo. Carga histórica depende da quantidade de eventos e pode levar tempo. Testes locais simulados: 72 passam, incluindo relógio retrocedendo, datas intercaladas, IDs ambíguos, deduplicação, cursor ao vivo preservado, reinício e acesso.

Interface GesTi: nome e login atualizados, navegação compartilhada em Acompanhamento, Operação, Integrações e Administração conforme perfil. Menu lateral no desktop, grupos recolhidos no celular, tema claro/escuro, foco de teclado e modo TV com navegação oculta. Controles do painel agrupados separados dos menus. Mensagens automáticas e teste começam com *MENSAGEM AUTOMATICA* seguido de linha em branco; um asterisco em cada lado é o formato de negrito do WhatsApp. Regras de horários, destinatários e soma preservadas. 67 testes passam após atualização dos textos.
