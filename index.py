"""Ponto de entrada da Vercel (preset FastAPI): expõe o app do MiniJarvis Nuvem.

A Vercel procura uma instância FastAPI chamada `app` em index.py na raiz.
"""
from nuvem.app import app  # noqa: F401
