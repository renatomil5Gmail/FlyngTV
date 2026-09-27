# PlayTV — cadastro e Pix

Aplicação web com cadastro por WhatsApp, consulta de clientes no Supabase e pagamentos Pix. Com `SUPABASE_URL` e `SUPABASE_SERVICE_ROLE_KEY` configurados, `public.clientes` é a fonte oficial dos cadastros; o SQLite local guarda o espelho mínimo necessário para correlacionar pagamentos do Mercado Pago.

## Rodar localmente

1. Instale as dependências listadas em `requirements.txt` no ambiente Python do projeto (`python -m pip install -r requirements.txt`).
2. Configure `MERCADOPAGO_TOKEN` no arquivo `.env` (use primeiro um token de teste do Mercado Pago). O arquivo `.env.example` mostra o formato.
3. Inicie a API FastAPI com Uvicorn na pasta do projeto (`python -m uvicorn main:app --reload`) e abra `http://127.0.0.1:8000`.

Sem o token, a página e as funções de cadastro/consulta funcionam, mas a geração e confirmação do Pix ficam desabilitadas. O token nunca deve ser colocado no HTML.

## Fluxo

- O telefone é normalizado para apenas dígitos e consultado em `public.clientes`; o arquivo `supabase_migration.sql` prepara colunas e índice necessários.
- No cadastro novo, o telefone é consultado antes de seguir. Se já existir, a pessoa escolhe atualizar os dados informados ou continuar com os dados existentes.
- O cadastro é inserido/atualizado antes da criação do Pix. A transação só é registrada quando o Mercado Pago retorna um Pix válido.
- Todos os prazos incluem o mesmo plano completo. O preço fixo é R$ 35 por mês de contrato: 1 mês = R$ 35, 3 meses = R$ 105 e 6 meses = R$ 210, sem multiplicar pelo número de telas. A quantidade de telas é mantida separadamente no cadastro e sincronizada na aprovação.
- O Pix recebe expiração explícita de 30 minutos por padrão (`PIX_EXPIRATION_MINUTES`, configurável entre 10 minutos e 24 horas); a tela usa a expiração devolvida pelo Mercado Pago para parar a espera e permitir gerar outro código.
- A confirmação é consultada pelo servidor e também recebida pelo webhook. Uma transação aprovada ativa a vigência do cliente pelo número de meses contratado; renovações somam ao fim da vigência atual, e reentregas de webhook/consultas repetidas não somam o mesmo pagamento outra vez.
- O endpoint `POST /webhook/mercadopago` valida a assinatura HMAC, consulta o pagamento no Mercado Pago e atualiza a transação. Configure `MERCADOPAGO_WEBHOOK_SECRET` no ambiente do servidor.
- Os links de suporte buscam o primeiro telefone em `public."Contato"` pelo PostgREST do Supabase. Configure `SUPABASE_URL` e `SUPABASE_SERVICE_ROLE_KEY` no Render; a chave `service_role` fica somente no backend. `SUPORTE_WHATSAPP` pode ser usado como fallback opcional.

**Configuração Supabase:** execute `supabase_migration.sql` no SQL Editor do projeto antes do deploy. No Environment do serviço do Render, configure `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` e `CUSTOMER_DB_BACKEND=supabase`. A resposta de `/api/saude` deve mostrar `clientes_backend: "supabase"` e `clientes_backend_configurado: true`; se mostrar `sqlite`, os clientes continuarão sendo buscados no banco errado. A `service_role` nunca deve ir para HTML, GitHub ou navegador.

**Integração pendente:** o formulário de teste grátis está pronto, mas o endpoint retorna indisponibilidade até receber documentação oficial e credenciais da API Live21. Nenhuma rota ou payload da Live21 foi presumida, e não se cria um cadastro local como se o teste tivesse sido liberado. O SQLite do serviço Free do Render ainda guarda as transações e não é persistente; use um armazenamento durável antes de depender dos pagamentos em produção.

**Arte em movimento:** a arte atual é um JPEG único e achatado, então seus objetos não podem se mover separadamente sem uma nova peça. Para animar apenas a arte sem mover os formulários, forneça um vídeo de fundo `WebM`/`MP4`, uma animação `Lottie`, ou os objetos em PNG/SVG transparente separados do fundo.

## Render grátis para testes

O arquivo `render.yaml` cria um Web Service Free para testar a página e receber webhooks. Para publicar, o projeto precisa estar em um repositório GitHub acessível pela sua conta Render. No painel do Render, use **New > Blueprint**, conecte esse repositório e informe os secrets solicitados (`MERCADOPAGO_TOKEN` e `MERCADOPAGO_WEBHOOK_SECRET`) diretamente no painel. Não envie credenciais pelo chat.

Após o deploy, use a URL `https://NOME-DO-SERVICO.onrender.com` como URL de teste no painel do Mercado Pago, com o caminho `/webhook/mercadopago`, habilitando o evento **Payments**. O diagnóstico fica em `/api/mercadopago/diagnostico`. O token exibido anteriormente foi recusado (HTTP 403); revogue-o e gere outro antes de testar.

**Limitações importantes:** serviço Free pode dormir após 15 minutos sem requisições e demorar cerca de um minuto para acordar. Seu disco é efêmero; SQLite e cadastros podem ser perdidos ao reiniciar, hibernar ou publicar uma nova versão. Portanto, use esta publicação apenas para validar a integração com dados de teste, nunca para cadastros/pagamentos reais. O PostgreSQL Free do Render também expira após 30 dias. Para produção é necessário escolher um banco gerenciado persistente e uma hospedagem adequada; essa versão ainda precisa migrar do SQLite para PostgreSQL.

Nunca suba `.env`; ele já está em `.gitignore`.
