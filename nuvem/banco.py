"""Acesso ao banco: Postgres em produção, SQLite para desenvolver e testar.

Com `DATABASE_URL` (ou `POSTGRES_URL`, que a Vercel cria ao ligar Neon ou
Supabase ao projeto) usa Postgres. Sem ela, usa um arquivo SQLite local.
O SQL do app é escrito uma vez, com `?` como marcador; aqui ele é traduzido
para o formato de cada banco.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

_TABELAS = """
CREATE TABLE IF NOT EXISTS usuarios (
    id {pk},
    email TEXT NOT NULL UNIQUE,
    nome TEXT NOT NULL,
    senha_hash TEXT NOT NULL,
    creditos INTEGER NOT NULL DEFAULT 0,
    plano TEXT NOT NULL DEFAULT 'gratis',
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessoes (
    token_hash TEXT PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    expira_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversas (
    id {pk},
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    titulo TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mensagens (
    id {pk},
    conversa_id BIGINT NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
    papel TEXT NOT NULL CHECK (papel IN ('user', 'assistant')),
    texto TEXT NOT NULL,
    conteudo_api TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extrato (
    id {pk},
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    delta INTEGER NOT NULL,
    motivo TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversas_usuario ON conversas(usuario_id, atualizado_em);
CREATE INDEX IF NOT EXISTS idx_mensagens_conversa ON mensagens(conversa_id, id);
CREATE INDEX IF NOT EXISTS idx_sessoes_usuario ON sessoes(usuario_id);

-- Parque: as máquinas de cada conta. O token do agente é guardado só como hash.
CREATE TABLE IF NOT EXISTS agentes (
    id {pk},
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    hostname TEXT NOT NULL,
    apelido TEXT,
    tags TEXT NOT NULL DEFAULT '',
    info TEXT,
    metricas TEXT,
    versao TEXT,
    job_atual BIGINT,
    token_hash TEXT NOT NULL UNIQUE,
    visto_em TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    UNIQUE (usuario_id, hostname)
);
CREATE TABLE IF NOT EXISTS pareamentos (
    codigo TEXT PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    expira_em TEXT NOT NULL
);
-- status: aguardando (precisa de aprovação) | pendente | executando
--         | concluido | falhou | cancelado
CREATE TABLE IF NOT EXISTS jobs (
    id {pk},
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    tipo TEXT NOT NULL,
    titulo TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{{}}',
    tags TEXT NOT NULL DEFAULT '',
    origem TEXT NOT NULL DEFAULT 'painel',
    status TEXT NOT NULL DEFAULT 'pendente',
    agente TEXT,
    progresso INTEGER NOT NULL DEFAULT 0,
    mensagem TEXT,
    resultado TEXT,
    log TEXT NOT NULL DEFAULT '',
    tentativas INTEGER NOT NULL DEFAULT 0,
    max_tentativas INTEGER NOT NULL DEFAULT 2,
    cancelar INTEGER NOT NULL DEFAULT 0,
    criado_em TEXT NOT NULL,
    iniciado_em TEXT,
    terminado_em TEXT,
    visto_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_usuario ON jobs(usuario_id, id);
CREATE INDEX IF NOT EXISTS idx_jobs_fila ON jobs(usuario_id, status, id);
-- Cérebro: notas em markdown, ligadas por [[links]].
CREATE TABLE IF NOT EXISTS notas (
    id {pk},
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    nome TEXT NOT NULL,
    conteudo TEXT NOT NULL DEFAULT '',
    origem TEXT NOT NULL DEFAULT 'painel',
    atualizado_em TEXT NOT NULL,
    UNIQUE (usuario_id, nome)
);
CREATE TABLE IF NOT EXISTS rotinas (
    id {pk},
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    nome TEXT NOT NULL,
    descricao TEXT NOT NULL DEFAULT '',
    tipo TEXT NOT NULL,
    payload TEXT NOT NULL,
    maquina TEXT NOT NULL,
    frequencia TEXT NOT NULL DEFAULT 'manual',
    ultima_execucao TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sugestoes (
    usuario_id BIGINT PRIMARY KEY REFERENCES usuarios(id) ON DELETE CASCADE,
    conteudo TEXT NOT NULL,
    gerado_em TEXT NOT NULL
);
-- Códigos de 6 dígitos mandados por e-mail (confirmar conta, entrar de um
-- dispositivo novo). Só o hash do código fica guardado.
CREATE TABLE IF NOT EXISTS codigos (
    id {pk},
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    finalidade TEXT NOT NULL,
    codigo_hash TEXT NOT NULL,
    expira_em TEXT NOT NULL,
    tentativas INTEGER NOT NULL DEFAULT 0,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codigos_usuario ON codigos(usuario_id, finalidade);
-- Navegadores em que a pessoa já confirmou o e-mail: não pedem código de novo.
CREATE TABLE IF NOT EXISTS dispositivos (
    token_hash TEXT PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    criado_em TEXT NOT NULL
);
-- Contador de tentativas (cadastro, login, códigos) por IP ou por conta.
-- Fica no banco porque na Vercel cada requisição pode cair numa instância.
CREATE TABLE IF NOT EXISTS limites (
    id {pk},
    chave TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_limites ON limites(chave, criado_em);
-- Pagamentos (Mercado Pago). `referencia` é o id no provedor, único: a mesma
-- notificação chegando duas vezes não soma créditos duas vezes.
CREATE TABLE IF NOT EXISTS pagamentos (
    id {pk},
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    provedor TEXT NOT NULL DEFAULT 'mp',
    referencia TEXT NOT NULL UNIQUE,
    tipo TEXT NOT NULL,
    plano TEXT NOT NULL,
    valor_brl REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pendente',
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pagamentos_usuario ON pagamentos(usuario_id, id);
-- Funil e incidentes: contadores sem cookie (ver nuvem/operacao.py).
CREATE TABLE IF NOT EXISTS eventos (
    id {pk},
    nome TEXT NOT NULL,
    usuario_id BIGINT,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eventos ON eventos(nome, criado_em);
CREATE INDEX IF NOT EXISTS idx_eventos_usuario ON eventos(nome, usuario_id);
-- Conversas publicadas como página pública (/c/TOKEN).
CREATE TABLE IF NOT EXISTS compartilhamentos (
    token TEXT PRIMARY KEY,
    conversa_id BIGINT NOT NULL UNIQUE REFERENCES conversas(id) ON DELETE CASCADE,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interesses (
    id {pk},
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    plano TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
"""

# Colunas novas em tabelas que já existem em produção. Rodam a cada partida;
# "coluna já existe" é ignorado.
_MIGRACOES = [
    "ALTER TABLE mensagens ADD COLUMN oculta INTEGER NOT NULL DEFAULT 0",
    # Contas que já existiam antes da confirmação por e-mail contam como confirmadas;
    # as novas são gravadas com 0 explicitamente.
    "ALTER TABLE usuarios ADD COLUMN email_verificado INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE usuarios ADD COLUMN email_canonico TEXT",
    # Máquina nova nasce no modo seguro: só roda ações prontas.
    "ALTER TABLE agentes ADD COLUMN modo TEXT NOT NULL DEFAULT 'seguro'",
    # Até quando o plano pago vale; vencido, o cron diário volta para o grátis.
    "ALTER TABLE usuarios ADD COLUMN plano_ate TEXT",
    # Assinatura recorrente no Mercado Pago (id do preapproval), se houver.
    "ALTER TABLE usuarios ADD COLUMN assinatura_id TEXT",
    "ALTER TABLE usuarios ADD COLUMN aceitou_termos_em TEXT",
    # Indicação: o código de cada conta e quem a indicou.
    "ALTER TABLE usuarios ADD COLUMN codigo_indicacao TEXT",
    "ALTER TABLE usuarios ADD COLUMN indicado_por BIGINT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_indicacao ON usuarios(codigo_indicacao)",
]


class ErroDuplicado(Exception):
    """Violação de UNIQUE (ex.: e-mail já cadastrado), igual nos dois bancos."""


def url_postgres() -> str | None:
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")


class Banco:
    """Uma conexão por thread. `execute` devolve um cursor com fetchone/fetchall
    cujas linhas aceitam acesso por nome (`row["id"]`) e `dict(row)`."""

    def __init__(self, caminho_sqlite: Path, url: str | None = None) -> None:
        self.url = url
        self.caminho = caminho_sqlite
        self.postgres = bool(url)
        self._local = threading.local()
        self._schema_ok = False
        self._trava = threading.Lock()

    # -- conexão ------------------------------------------------------------
    def _conectar(self) -> Any:
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row

            # prepare_threshold=None: compatível com poolers (PgBouncer/Neon pooled)
            con = psycopg.connect(self.url, autocommit=True, row_factory=dict_row, prepare_threshold=None)
        else:
            con = sqlite3.connect(self.caminho, check_same_thread=False, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA journal_mode = WAL")
        with self._trava:
            if not self._schema_ok:
                self._criar_schema(con)
                self._schema_ok = True
        return con

    def _criar_schema(self, con: Any) -> None:
        if self.postgres:
            con.execute(_TABELAS.format(pk="BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"))
        else:
            con.executescript(_TABELAS.format(pk="INTEGER PRIMARY KEY"))
        for sql in _MIGRACOES:
            try:
                con.execute(sql)
            except Exception as exc:  # noqa: BLE001
                if "duplicate column" not in str(exc) and "already exists" not in str(exc):
                    raise

    def _con(self) -> Any:
        con = getattr(self._local, "con", None)
        if con is not None and self.postgres and (con.closed or con.broken):
            con = None  # conexão caiu (comum em serverless ociosa): reabre
        if con is None:
            con = self._conectar()
            self._local.con = con
        return con

    # -- consultas ----------------------------------------------------------
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        try:
            return self._con().execute(self._sql(sql), tuple(params))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise ErroDuplicado(str(exc)) from exc
            raise
        except Exception as exc:
            if self.postgres and type(exc).__name__ == "UniqueViolation":
                raise ErroDuplicado(str(exc)) from exc
            raise

    def inserir(self, sql: str, params: Sequence[Any] = ()) -> int:
        """INSERT que devolve o id novo (RETURNING funciona nos dois bancos)."""
        return self.execute(sql + " RETURNING id", params).fetchone()["id"]

    @contextmanager
    def transacao(self) -> Iterator["Banco"]:
        con = self._con()
        if self.postgres:
            with con.transaction():
                yield self
        else:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield self
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
