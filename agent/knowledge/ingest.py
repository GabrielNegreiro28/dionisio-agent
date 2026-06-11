"""
Atualiza a base de conhecimento: baixa a documentação do Dionísio (GitBook)
consolidada (llms-full.txt — a doc inteira num arquivo markdown) e salva localmente.

Rode quando a documentação mudar:
    python -m knowledge.ingest
"""

import sys
from pathlib import Path

import httpx

URL = "https://dionisio.gitbook.io/documentacao-dionisio/llms-full.txt"
DEST = Path(__file__).parent / "dionisio_docs.md"


def main() -> None:
    try:
        resp = httpx.get(URL, timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        sys.exit(f"Falha ao baixar a documentação: {e}")
    text = resp.text
    DEST.write_text(text, encoding="utf-8")
    headings = sum(1 for line in text.splitlines() if line.startswith("#"))
    print(f"OK — {len(text)} chars, ~{headings} seções salvas em {DEST}")


if __name__ == "__main__":
    main()
