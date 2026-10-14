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
