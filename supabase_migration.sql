-- Execute uma vez no SQL Editor do Supabase antes de publicar a integração.
-- `telefone` precisa ser único para impedir cadastros duplicados e permitir upsert.
ALTER TABLE public.clientes
    ADD COLUMN IF NOT EXISTS email TEXT,
    ADD COLUMN IF NOT EXISTS vigencia_ate TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS telas INTEGER NOT NULL DEFAULT 1;

CREATE UNIQUE INDEX IF NOT EXISTS clientes_telefone_unique
    ON public.clientes (telefone);
