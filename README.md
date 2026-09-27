# PlayTV — cadastro e Pix

Protótipo web com cadastro por WhatsApp, busca de clientes, atualização pelo mesmo telefone e registro de pagamentos. O banco local é SQLite e fica no arquivo `clientes.db`, criado automaticamente na primeira execução.

## Rodar localmente

1. Instale as dependências listadas em `requirements.txt` no ambiente Python do projeto (`python -m pip install -r requirements.txt`).
2. Configure `MERCADOPAGO_TOKEN` no arquivo `.env` (use primeiro um token de teste do Mercado Pago). O arquivo `.env.example` mostra o formato.
3. Inicie a API FastAPI com Uvicorn na pasta do projeto (`python -m uvicorn main:app --reload`) e abra `http://127.0.0.1:8000`.

Sem o token, a página e as funções de cadastro/consulta funcionam, mas a geração e confirmação do Pix ficam desabilitadas. O token nunca deve ser colocado no HTML.

## Fluxo

- O telefone é normalizado para apenas dígitos e usado como identificador único.
- No cadastro novo, o telefone é consultado antes de seguir. Se já existir, a pessoa escolhe atualizar os dados informados ou continuar com os dados existentes.
- O cadastro é inserido/atualizado antes da criação do Pix. A transação só é registrada quando o Mercado Pago retorna um Pix válido.
- O Pix recebe expiração explícita de 30 minutos por padrão (`PIX_EXPIRATION_MINUTES`, configurável entre 10 minutos e 24 horas); a tela usa a expiração devolvida pelo Mercado Pago para parar a espera e permitir gerar outro código.
- A confirmação é consultada pelo servidor e também recebida pelo webhook. Uma transação aprovada ativa a vigência do cliente pelo número de meses contratado; renovações somam ao fim da vigência atual, e reentregas de webhook/consultas repetidas não somam o mesmo pagamento outra vez.
- O endpoint `POST /webhook/mercadopago` valida a assinatura HMAC, consulta o pagamento no Mercado Pago e atualiza a transação. Configure `MERCADOPAGO_WEBHOOK_SECRET` no ambiente do servidor.
- Os links de suporte buscam o primeiro telefone em `public."Contato"` pelo PostgREST do Supabase. Configure `SUPABASE_URL` e `SUPABASE_SERVICE_ROLE_KEY` no Render; a chave `service_role` fica somente no backend. `SUPORTE_WHATSAPP` pode ser usado como fallback opcional.

**Integração pendente:** o formulário de teste grátis está pronto, mas o endpoint retorna indisponibilidade até receber documentação oficial e credenciais da API Live21. Nenhuma rota ou payload da Live21 foi presumida, e não se cria um cadastro local como se o teste tivesse sido liberado. O SQLite do serviço Free do Render não é armazenamento persistente; antes de depender dos cadastros e vencimentos em produção, migre para um banco persistente e configure `DATABASE_PATH`/conexão de produção conforme o novo backend.

**Arte em movimento:** a arte atual é um único JPEG achatado. A página aplica movimento suave ao fundo e animações decorativas. Para mover objetos específicos da própria arte (estrela, bola, pipoca, controle etc.), forneça os objetos separados em PNG transparente/SVG e um fundo sem esses objetos, ou o arquivo-fonte em camadas (PSD/AI).

## Render grátis para testes

O arquivo `render.yaml` cria um Web Service Free para testar a página e receber webhooks. Para publicar, o projeto precisa estar em um repositório GitHub acessível pela sua conta Render. No painel do Render, use **New > Blueprint**, conecte esse repositório e informe os secrets solicitados (`MERCADOPAGO_TOKEN` e `MERCADOPAGO_WEBHOOK_SECRET`) diretamente no painel. Não envie credenciais pelo chat.

Após o deploy, use a URL `https://NOME-DO-SERVICO.onrender.com` como URL de teste no painel do Mercado Pago, com o caminho `/webhook/mercadopago`, habilitando o evento **Payments**. O diagnóstico fica em `/api/mercadopago/diagnostico`. O token exibido anteriormente foi recusado (HTTP 403); revogue-o e gere outro antes de testar.

**Limitações importantes:** serviço Free pode dormir após 15 minutos sem requisições e demorar cerca de um minuto para acordar. Seu disco é efêmero; SQLite e cadastros podem ser perdidos ao reiniciar, hibernar ou publicar uma nova versão. Portanto, use esta publicação apenas para validar a integração com dados de teste, nunca para cadastros/pagamentos reais. O PostgreSQL Free do Render também expira após 30 dias. Para produção é necessário escolher um banco gerenciado persistente e uma hospedagem adequada; essa versão ainda precisa migrar do SQLite para PostgreSQL.

O deploy não pode ser concluído daqui sem o repositório GitHub vinculado à sua conta Render. Neste computador, o comando `git` não está disponível e não há remote Git configurado. Nunca suba `.env`; ele já está em `.gitignore`.
