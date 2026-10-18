# 🎬 FilmowBox — Exportador Filmow para Letterboxd

Exporte sua lista completa de filmes assistidos do Filmow diretamente para um arquivo CSV pronto e formatado para importação no Letterboxd.

## 🚀 Como Funciona

1. Digite seu nome de usuário público do **Filmow**.
2. O FilmowBox busca todas as páginas das suas listas de filmes, curtas e produções para TV.
3. Gera um arquivo CSV no padrão oficial do Letterboxd:
   - `Title`: Título original do filme
   - `Year`: Ano de lançamento (YYYY)
   - `Rating`: Nota convertida na escala 0.5 a 5.0 estrelas
4. O arquivo é baixado com quebras de linha padrão Unix (`LF`).

## 🛠️ Tecnologias Utilizadas

- **Python 3.9+**
- **FastAPI**
- **curl-cffi** (simulação TLS do Chrome para requisições assíncronas resilientes)
- **BeautifulSoup4** (extração e parsing de HTML)
- **Uvicorn**
