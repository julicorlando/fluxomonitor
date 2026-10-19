# Monitor externo do GesTi

O workflow roda nos runners padrão do GitHub, fora do servidor Coolify, com agenda nominal de cinco minutos. O GitHub pode atrasar execuções; isto não constitui SLA de alerta instantâneo. O repositório atual é público. Runners maiores ou mudança para repositório privado podem alterar custos/limites.

O endpoint público `/health/monitor/` retorna apenas saúde resumida e instante da confirmação; não publica contagens, IPs, destinatários nem credenciais. Falta de sessão WhatsApp ou de atividade dos serviços deixa o endpoint degradado. São feitas três tentativas HTTPS antes de registrar uma ocorrência.

Uma única issue do bot é atribuída ao proprietário do repositório quando o monitor falha. A issue é fechada após normalização. Notificações por email/push dependem das preferências da conta GitHub e da assinatura do repositório/issue; este recurso não entrega SMS nem WhatsApp durante uma queda completa do servidor. Consulte as configurações de notificações do GitHub. Não são necessários tokens estáticos: o workflow usa o `GITHUB_TOKEN` temporário limitado a este repositório.

Instale o workflow somente depois de o novo endpoint estar publicado e saudável, para não registrar a primeira implantação como indisponibilidade. É possível executar manualmente pela aba Actions.
