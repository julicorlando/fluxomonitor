# Piloto de leitura de placas — Fluxo

O painel está em `/plates/`, integrado às páginas Django atuais. O projeto ainda não tem frontend React. Fotos exigem login; consulta é liberada aos usuários existentes; revisão é restrita a staff; câmeras e tokens ao superadmin. Links públicos de BI não expõem placas.

## Publicação do painel

Instale `requirements.txt` e execute `python manage.py migrate` antes de iniciar a aplicação (o entrypoint atual já executa migrate). A migração 0016 adiciona tabelas independentes, sem modificar as contagens existentes. O painel guarda JPEGs limitados e reduzidos no próprio banco; considere esse volume nos backups e defina uma política de retenção antes de expandir. Faça backup antes do deploy.

Em Placas > Configurar câmeras, crie uma cancela e copie o token exibido uma única vez. Substituir o token invalida imediatamente o anterior. Desativar a câmera impede recebimentos novos; preserva o histórico. Último envio indica captura recebida, não heartbeat nem disponibilidade contínua do vídeo.

## Coletor local (Ubuntu)

Uma instância por câmera. O host precisa alcançar o RTSP e o endereço HTTPS do painel. Não exponha a câmera na internet.

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr python3-venv
python3 -m venv .venv-plates
. .venv-plates/bin/activate
pip install -r collector/requirements.txt
# Carregue as variáveis por arquivo de ambiente privado (chmod 600):
# PLATES_BASE_URL=https://fluxo.axionwebdigital.com.br
# PLATES_RTSP_URL=<endereço fornecido pela câmera/NVR>
# PLATES_TOKEN=<token gerado no painel>
# PLATES_ROI=[0.5583,0.6311,0.0949,0.0467]
# PLATES_QUEUE=/var/lib/fluxo-plates/entrada1.sqlite3
python collector/plates.py
```

A ROI acima é somente ponto de partida aproximado da captura enviada: `[x,y,largura,altura]`, valores de 0 a 1 em relação ao vídeo. Ajuste à resolução original para abranger os caracteres da placa nas diferentes posições de parada. Uma ROI estreita pode perder veículos que parem fora da posição calibrada. Essa área também será a foto salva. Credenciais RTSP ficam somente no host local; não são enviadas ao painel. O coletor exige variáveis realmente exportadas para seu processo (por exemplo EnvironmentFile no systemd).

O OCR usa OpenCV e Tesseract, aceita formato antigo e Mercosul, exige pelo menos três leituras iguais e maioria de 70% em uma janela de 12 amostras. O índice OCR não representa probabilidade calibrada de acerto. Após oito segundos sem leitura válida, inicia nova sessão: isso pode gerar nova captura se um carro ficar parado e a placa desaparecer temporariamente. Dois veículos seguidos sem intervalo de leitura também podem ser consolidados indevidamente. Para produção, validar presença/saída e integrar sensor de laço/ticket ou rastreamento de veículos. Não comanda cancelas.

A fila SQLite persiste imagens durante falhas da central e reenvia o mesmo UUID, evitando duplicação por retry. Máximo de 10.000 itens; ao atingir o limite o processo para explicitamente, para evitar perda silenciosa. Monitore disco e processo. O token é exclusivo de cada câmera; não use os tokens de contagem de pessoas. HTTP 401 indica token desativado, trocado ou incorreto; HTTP 400 indica payload rejeitado. A fila não é apagada nesses casos.

## Homologação obrigatória

Esta é uma implementação piloto, sem taxa de acerto medida. Teste o vídeo original com carros de tamanhos diferentes, placas antigas/Mercosul, sol, chuva e noite. Meça acerto completo por veículo, leituras perdidas e duplicações. OCR de região fixa pode falhar com fundos poluídos e placas em posições diferentes; nesse caso, adicione detector de placas treinado e licenciado adequadamente. Não substitua o controle de tickets sem homologação.

## API

`POST /api/v1/plates/ingest/` multipart com `Authorization: Bearer <token>`: `event_id` UUID persistente por passagem, `plate`, `captured_at` ISO 8601 com fuso, `confidence` entre 0 e 1, `image` JPEG/PNG até 500 KB e 8 milhões de pixels. Respostas: 201 novo, 200 reenvio, 400 inválido, 401 sem autorização. O token determina a câmera; o cliente não escolhe outra câmera no payload.

## Intelbras NVD 3116 — seleção pelo painel

1. Acesse **Placas > NVRs e canais** como superadmin. Cadastre nome, IP do NVR e porta RTSP (normalmente 554).
2. Copie o token exclusivo do NVR para o servidor local. Para substituir um token perdido, abra **Editar conexão > Substituir token do coletor**; o anterior será invalidado.
3. Abra **Selecionar canais**, marque os canais de 1 a 16, identifique cada cancela e ajuste a ROI individual. A tela lista posições de canal para configuração manual: não consulta automaticamente o inventário do gravador nem afirma que o canal está conectado. O vídeo principal é o padrão.
4. No servidor que alcança o NVR, instale os requisitos do coletor e Tesseract conforme acima e configure, em arquivo de ambiente privado, as variáveis:

```text
PLATES_BASE_URL=https://fluxo.axionwebdigital.com.br
PLATES_NVR_TOKEN=<token do NVR>
PLATES_NVR_USERNAME=<usuário do gravador com permissão de visualização>
PLATES_NVR_PASSWORD=<senha do gravador>
PLATES_QUEUE_DIR=/var/lib/fluxo-plates/queues
```

Execute `python collector/nvr.py` com as variáveis exportadas. Uma instância deste supervisor atende um NVR; execute outra instância com configuração própria para cada gravador adicional. O supervisor consulta a seleção a cada 15 segundos e inicia um processo por canal marcado. Alterações de IP, porta, vídeo ou ROI reiniciam somente os processos afetados. Desmarcar um canal encerra seu processo; desativar o NVR encerra todos. O servidor rejeita imediatamente ingestão para NVR/canal desativado. Após 90 segundos sem obter configuração atualizada, o supervisor pausa os processos. Filas pendentes são preservadas e só retomam quando o canal volta a ser selecionado.

O endereço é montado no coletor: `rtsp://USUARIO:SENHA@IP:PORTA/cam/realmonitor?channel=N&subtype=0`. Caracteres especiais em usuário/senha são codificados. Não é necessário obter o IP individual das câmeras conectadas ao NVR. A integração faz leitura de vídeo ao vivo e não altera as gravações. IP e canal corretos, permissões RTSP e codec decodificável devem ser testados no equipamento real. Comece com um canal para medir consumo de CPU, carga no NVR e taxa de acerto. A tela de último contato mostra consulta de configuração pelo supervisor; não comprova abertura do vídeo. Última captura mostra ingestão concluída.

A API `GET /api/v1/plates/collector-config/` aceita somente o token daquele NVR e retorna seus canais selecionados, sem usuário ou senha do equipamento. No envio multipart de capturas com token de NVR, acrescente `channel` (1–16). Tokens individuais de câmeras existentes continuam funcionando, respeitando a desativação do gravador associado. O cadastro de contagem de pessoas é independente.
