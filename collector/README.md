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
