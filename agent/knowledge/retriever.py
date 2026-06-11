"""
Camada de conhecimento — retriever TF-IDF puro (sem deps, sem embeddings) sobre
a documentação do Dionísio (GitBook), consolidada em dionisio_docs.md por ingest.py.

Usado para FUNDAMENTAR o caminho conversacional: quando o operador faz uma pergunta
de regra/negócio ("como funciona o no-show?", "o que é fila de espera?"), buscamos
os trechos relevantes da doc real e passamos como contexto pro LLM — em vez de ele
responder com conhecimento genérico.

Determinístico e testável: a busca é matemática (cosseno TF-IDF), sem chamada de rede
nem LLM. O LLM só entra depois, para redigir a resposta a partir dos trechos.

LIMITAÇÃO CONHECIDA (trade consciente): TF-IDF é léxico — casa por palavra, não por
significado. É sensível à formulação da pergunta (sinônimos/paráfrases podem trazer a
seção errada) e não entende semântica. Mitigações já no lugar: normalização de acento,
título contando em dobro, e limiar de relevância. A resposta continua segura mesmo quando
o trecho não é o ideal, porque o prompt manda NÃO inventar regra fora dos trechos e dizer
quando a doc não cobre. Para recall melhor, o próximo passo seria embeddings (ex: Voyage/
OpenAI) com busca vetorial — exige um provider de embeddings, por isso ficou fora deste MVP.
"""

import math
import re
from collections import Counter
from pathlib import Path

_DOC_PATH = Path(__file__).parent / "dionisio_docs.md"

# Stopwords PT-BR comuns (curtas/funcionais) — reduzem ruído no índice.
_STOP = {
    "a", "o", "e", "de", "da", "do", "das", "dos", "para", "por", "com", "que",
    "em", "no", "na", "nos", "nas", "um", "uma", "uns", "umas", "os", "as", "ao",
    "aos", "se", "sua", "seu", "suas", "seus", "ou", "como", "qual", "quais",
    "este", "esta", "isso", "ser", "são", "está", "estão", "pode", "podem", "tem",
    "ter", "mais", "menos", "sobre", "entre", "pelo", "pela", "foi", "ja", "já",
    "voce", "você", "voc", "the",
}

# Limiar de relevância: abaixo disso, não injetamos contexto (evita ruído em
# "obrigado", "previsão do tempo", etc.). Calibrado: perguntas de doc pontuam
# ~0.22+ no topo; off-topic fica abaixo de ~0.15.
_MIN_SCORE = 0.18


def _normalize(text: str) -> str:
    """Lowercase + remove acentos (casa 'no-show' com 'no show', 'fidelidade')."""
    text = text.lower()
    repl = (("á", "a"), ("à", "a"), ("ã", "a"), ("â", "a"), ("é", "e"), ("ê", "e"),
            ("í", "i"), ("ó", "o"), ("õ", "o"), ("ô", "o"), ("ú", "u"), ("ç", "c"))
    for a, b in repl:
        text = text.replace(a, b)
    return text


def _tokenize(text: str) -> list:
    toks = re.findall(r"[a-z0-9]+", _normalize(text))
    return [t for t in toks if len(t) > 2 and t not in _STOP]


def _chunk(md: str) -> list:
    """Quebra o markdown por headings (#/##/###), mantendo a seção como unidade
    e o título para citação. Retorna [(titulo, texto)]."""
    chunks, cur_title, cur = [], "Início", []
    for line in md.splitlines():
        m = re.match(r"^#{1,3}\s+(.*)", line)
        if m:
            body = "\n".join(cur).strip()
            if len(body) > 30:
                chunks.append((cur_title, body))
            cur_title = m.group(1).strip()
            cur = [line]
        else:
            cur.append(line)
    body = "\n".join(cur).strip()
    if len(body) > 30:
        chunks.append((cur_title, body))
    return chunks


class KnowledgeBase:
    def __init__(self, path: Path = _DOC_PATH):
        self.chunks = []          # [(titulo, texto)]
        self.idf = {}
        self.vecs = []            # [{termo: peso_normalizado}]
        self._load(path)

    @property
    def ready(self) -> bool:
        return bool(self.vecs)

    def _load(self, path: Path) -> None:
        if not Path(path).exists():
            return
        md = Path(path).read_text(encoding="utf-8")
        self.chunks = _chunk(md)
        token_lists = [_tokenize(f"{t} {t} {c}") for t, c in self.chunks]  # título conta dobrado
        n = len(token_lists)
        df = Counter()
        for toks in token_lists:
            df.update(set(toks))
        self.idf = {term: math.log((n + 1) / (d + 1)) + 1 for term, d in df.items()}
        for toks in token_lists:
            self.vecs.append(self._vectorize(toks))

    def _vectorize(self, toks: list) -> dict:
        if not toks:
            return {}
        tf = Counter(toks)
        total = len(toks)
        vec = {t: (c / total) * self.idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def search(self, query: str, k: int = 3) -> list:
        """Top-k trechos por similaridade de cosseno. Retorna [(titulo, texto, score)]."""
        qvec = self._vectorize(_tokenize(query))
        if not qvec or not self.vecs:
            return []
        scored = []
        for i, dv in enumerate(self.vecs):
            s = sum(w * dv.get(t, 0.0) for t, w in qvec.items())
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        out = []
        for s, i in scored[:k]:
            title, text = self.chunks[i]
            out.append((title, text, round(s, 3)))
        return out


_KB = None


def get_kb() -> KnowledgeBase:
    global _KB
    if _KB is None:
        _KB = KnowledgeBase()
    return _KB


def knowledge_context(query: str, k: int = 3, max_chars: int = 1500) -> str:
    """
    String pronta para injetar no prompt conversacional: os trechos mais
    relevantes da doc, com título. Vazio se nada for relevante (abaixo do limiar)
    ou se a base não foi ingerida — assim "obrigado"/"previsão do tempo" não puxam ruído.
    """
    kb = get_kb()
    if not kb.ready:
        return ""
    hits = kb.search(query, k=k)
    hits = [h for h in hits if h[2] >= _MIN_SCORE]
    if not hits:
        return ""
    parts, used = [], 0
    for title, text, _score in hits:
        snippet = text if len(text) <= max_chars else text[:max_chars] + "…"
        block = f"### {title}\n{snippet}"
        if used + len(block) > max_chars * len(hits):
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)
