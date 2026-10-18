import asyncio
import csv
import io
import re
import sys
from typing import Optional

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

TOOLTIP_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def clean_year(text: str) -> str:
    """Extract a 4-digit release year (YYYY)."""
    match = re.search(r"\b(18\d\d|19\d\d|20\d\d)\b", text)
    return match.group(1) if match else ""


class FilmowScraper:
    def __init__(self, page_concurrency: int = 2, detail_concurrency: int = 15):
        self.page_bouncer = asyncio.Semaphore(page_concurrency)
        self.detail_bouncer = asyncio.Semaphore(detail_concurrency)

    async def fetch_page(
        self,
        url: str,
        client: AsyncSession,
        max_retries: int = 4,
        backoff_delay: float = 3.0,
    ) -> Optional[str]:
        """Fetch a single page with polite concurrency, automatic retries, and 429 backoff."""
        async with self.page_bouncer:
            for attempt in range(1, max_retries + 1):
                try:
                    response = await client.get(url, headers=DEFAULT_HEADERS)
                    if response.status_code == 200:
                        await asyncio.sleep(0.8)
                        return response.text
                    elif response.status_code == 404:
                        return None
                    elif response.status_code == 429:
                        wait_time = backoff_delay * attempt
                        print(f"Rate limited (429) fetching {url}. Waiting {wait_time:.1f}s (attempt {attempt}/{max_retries})...")
                        await asyncio.sleep(wait_time)
                    else:
                        print(f"HTTP error {response.status_code} fetching {url}")
                        return None
                except Exception as exc:
                    wait_time = backoff_delay * attempt
                    print(f"Network error fetching {url} ({exc}). Retrying in {wait_time:.1f}s...")
                    await asyncio.sleep(wait_time)

            return None

    def parse_total_pages(self, html: str) -> int:
        """Extract the total page count from Filmow pagination markup."""
        soup = BeautifulSoup(html, "lxml")

        last_page_icon = soup.select_one(".icon-double-angle-right")
        if last_page_icon:
            parent_link = last_page_icon.find_parent("a")
            if parent_link and parent_link.has_attr("href"):
                href = parent_link["href"]
                try:
                    return int(href.split("=")[-1])
                except (ValueError, IndexError):
                    pass

        next_page_icon = soup.select_one(".icon-angle-right")
        if next_page_icon:
            pagination_div = soup.select_one(".pagination-centered")
            if pagination_div:
                links = pagination_div.find_all("a")
                if len(links) >= 2:
                    try:
                        return int(links[-2].text.strip())
                    except ValueError:
                        pass

        return 1

    def parse_movies_from_page(self, html: str) -> list[dict]:
        """Parse movie items from a page, extracting PK, fallback title, and rating."""
        soup = BeautifulSoup(html, "lxml")
        items = soup.find_all("li", class_="movie_list_item")
        movies = []

        for item in items:
            pk = item.get("data-movie-pk")
            if not pk:
                continue

            fallback_title = "Unknown Title"
            title_tag = item.select_one("span.title")
            if title_tag and title_tag.text.strip():
                fallback_title = title_tag.text.strip()
            else:
                cover_link = item.select_one("a.cover")
                if cover_link and cover_link.get("title"):
                    fallback_title = cover_link["title"].strip()

            rating_val: Optional[float] = None
            rating_span = item.select_one("span.star-rating-small")
            if rating_span and rating_span.get("title"):
                parts = rating_span["title"].split()
                if len(parts) >= 2:
                    try:
                        rating_val = float(parts[1].replace(",", "."))
                    except ValueError:
                        rating_val = None

            movies.append({
                "pk": pk,
                "title": fallback_title,
                "rating": rating_val,
                "year": "",
            })

        return movies

    async def fetch_movie_details(
        self,
        client: AsyncSession,
        movie: dict,
        max_retries: int = 4,
        backoff_delay: float = 2.0,
    ) -> dict:
        """Fetch movie metadata (original title, release year) from tooltip endpoint."""
        pk = movie["pk"]
        url = f"https://filmow.com/async/tooltip/movie/?movie_pk={pk}"

        async with self.detail_bouncer:
            for attempt in range(1, max_retries + 1):
                try:
                    response = await client.get(url, headers=TOOLTIP_HEADERS)
                    if response.status_code == 429:
                        wait_time = backoff_delay * attempt
                        await asyncio.sleep(wait_time)
                        continue
                    elif response.status_code != 200:
                        break

                    data = response.json()

                    movie_meta = data.get("movie", {})
                    orig_title = movie_meta.get("title_orig")
                    display_title = movie_meta.get("title")

                    if orig_title and orig_title.strip():
                        movie["title"] = orig_title.strip()
                    elif display_title and display_title.strip():
                        movie["title"] = display_title.strip()

                    html_content = data.get("html", "")
                    if html_content:
                        soup = BeautifulSoup(html_content, "lxml")
                        for item in soup.select("div.item"):
                            text = item.text
                            if "Lançamento" in text:
                                movie["year"] = clean_year(text)

                    return movie

                except Exception:
                    wait_time = backoff_delay * attempt
                    await asyncio.sleep(wait_time)

        return movie

    async def scrape_list(
        self,
        username: str,
        category: str = "filmes",
        list_type: str = "ja-vi",
        client: Optional[AsyncSession] = None,
    ) -> tuple[list[dict], int]:
        """Scrape all movies for a given user, category, and list type. Returns (movies, total_pages)."""
        base_url = f"https://filmow.com/usuario/{username}/{category}/{list_type}/"
        own_client = False
        if client is None:
            client = AsyncSession(impersonate="chrome120", timeout=20.0)
            own_client = True

        try:
            first_page_html = await self.fetch_page(base_url, client)
            if not first_page_html:
                return [], 0

            total_pages = self.parse_total_pages(first_page_html)
            all_movies = self.parse_movies_from_page(first_page_html)

            if total_pages > 1:
                page_urls = [f"{base_url}?pagina={p}" for p in range(2, total_pages + 1)]
                page_tasks = [self.fetch_page(u, client) for u in page_urls]
                pages_html = await asyncio.gather(*page_tasks)

                for html in pages_html:
                    if html:
                        all_movies.extend(self.parse_movies_from_page(html))

            print(f"[{category}] Encontrada(s) {total_pages} página(s) com {len(all_movies)} filmes. Buscando detalhes...")

            detail_tasks = [
                self.fetch_movie_details(client, movie)
                for movie in all_movies
            ]
            detailed_movies = await asyncio.gather(*detail_tasks)
            return list(detailed_movies), total_pages

        finally:
            if own_client:
                await client.close()


def generate_letterboxd_csv(movies: list[dict]) -> io.StringIO:
    """Generate Letterboxd-compatible CSV in-memory with Title, Year, Rating (Unix LF line endings)."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)

    # Clean Letterboxd format: Title, Year, Rating
    writer.writerow(["Title", "Year", "Rating"])

    for m in movies:
        title = m.get("title", "").strip()
        year = m.get("year", "").strip()

        rating_str = ""
        raw_rating = m.get("rating")
        if isinstance(raw_rating, (int, float)):
            rating_val = float(raw_rating)
            rating_str = str(int(rating_val)) if rating_val.is_integer() else f"{rating_val:.1f}"

        writer.writerow([title, year, rating_str])

    output.seek(0)
    return output


# FastAPI application - Clean, minimal, no Swagger schemas clutter
app = FastAPI(
    title="FilmowBox",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

HTML_UI = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>FilmowBox — Exportar Filmow para Letterboxd</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body { background: #14181c; color: #9ab; display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; }
    .card { background: #1f252c; border: 1px solid #2c3440; border-radius: 12px; max-width: 460px; width: 100%; padding: 32px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
    h1 { color: #fff; font-size: 26px; font-weight: 700; margin-bottom: 6px; letter-spacing: -0.5px; }
    p.subtitle { color: #89a; font-size: 14px; margin-bottom: 24px; line-height: 1.4; }
    label { display: block; font-size: 12px; font-weight: 700; color: #9ab; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.8px; }
    input[type="text"] { width: 100%; padding: 13px 16px; background: #2c3440; border: 1px solid #445566; border-radius: 8px; color: #fff; font-size: 15px; outline: none; transition: border-color 0.2s; }
    input[type="text"]:focus { border-color: #00e054; }
    button { width: 100%; margin-top: 18px; padding: 14px; background: #00e054; color: #14181c; border: none; border-radius: 8px; font-size: 15px; font-weight: 700; cursor: pointer; transition: background 0.2s, opacity 0.2s; }
    button:hover { background: #00b343; }
    button:disabled { background: #34404d; color: #789; cursor: not-allowed; }
    .log-box { margin-top: 18px; padding: 14px 16px; background: #14181c; border-radius: 8px; font-size: 13px; border: 1px solid #2c3440; display: none; line-height: 1.5; }
    .log-box.active { display: block; }
    .log-status { color: #fff; font-weight: 600; display: flex; align-items: center; gap: 10px; }
    .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.2); border-radius: 50%; border-top-color: #00e054; animation: spin 0.8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .error-box { margin-top: 16px; padding: 12px 14px; background: rgba(255,75,75,0.12); border: 1px solid #ff4b4b; border-radius: 8px; color: #ff6b6b; font-size: 13px; display: none; }
    .error-box.active { display: block; }
  </style>
</head>
<body>
  <div class="card">
    <h1>FilmowBox</h1>
    <p class="subtitle">Exporte todos os seus filmes assistidos do Filmow diretamente para um CSV pronto para o Letterboxd.</p>
    <form id="exportForm">
      <label for="username">Usuário do Filmow</label>
      <input type="text" id="username" placeholder="Digite seu usuário" required autocomplete="off" />
      <button type="submit" id="btn">Baixar CSV para Letterboxd</button>
    </form>
    <div id="error" class="error-box"></div>
    <div id="log" class="log-box">
      <div class="log-status">
        <span id="spinner" class="spinner"></span>
        <span id="logText">Conectando ao Filmow...</span>
      </div>
      <div id="logDetails" style="color: #89a; margin-top: 4px; font-size: 12px;"></div>
    </div>
  </div>

  <script>
    const form = document.getElementById('exportForm');
    const input = document.getElementById('username');
    const btn = document.getElementById('btn');
    const log = document.getElementById('log');
    const logText = document.getElementById('logText');
    const logDetails = document.getElementById('logDetails');
    const spinner = document.getElementById('spinner');
    const errorBox = document.getElementById('error');

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = input.value.trim();
      if (!username) return;

      errorBox.classList.remove('active');
      errorBox.textContent = '';
      log.classList.add('active');
      spinner.style.display = 'inline-block';
      logText.textContent = 'Conectando ao Filmow e contando páginas...';
      logDetails.textContent = 'Isso pode levar alguns minutos em perfis com muitos filmes.';
      btn.disabled = true;

      try {
        const response = await fetch(`/api/export?username=${encodeURIComponent(username)}`);
        if (!response.ok) {
          const errData = await response.json().catch(() => ({ detail: 'Falha ao exportar dados do Filmow.' }));
          throw new Error(errData.detail || 'Ocorreu um erro ao exportar seus filmes.');
        }

        const totalPages = response.headers.get('X-Total-Pages') || '';
        const totalMovies = response.headers.get('X-Total-Movies') || '';

        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `letterboxd_${username}_watched.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);

        spinner.style.display = 'none';
        logText.textContent = 'Concluído! O arquivo foi baixado com sucesso.';
        if (totalMovies && totalPages) {
          logDetails.textContent = `Encontrados ${totalMovies} filmes em ${totalPages} página(s). Pronto para importar no Letterboxd.`;
        } else {
          logDetails.textContent = 'Pronto para importar no Letterboxd.';
        }
      } catch (err) {
        log.classList.remove('active');
        errorBox.textContent = err.message;
        errorBox.classList.add('active');
      } finally {
        btn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def home():
    """Página inicial simples e direta ao ponto."""
    return HTMLResponse(content=HTML_UI)


@app.get("/api/export")
async def export_letterboxd_csv(
    username: str = Query(..., description="Nome de usuário no Filmow"),
):
    clean_username = username.strip().lower().lstrip("@")
    if "filmow.com/usuario/" in clean_username:
        match = re.search(r"filmow\.com/usuario/([^/]+)", clean_username)
        if match:
            clean_username = match.group(1)

    scraper = FilmowScraper()
    categories_to_scrape = ["filmes", "curtas", "tv"]

    all_movies = []
    total_pages_count = 0

    async with AsyncSession(impersonate="chrome120", timeout=20.0) as client:
        for cat in categories_to_scrape:
            movies, pages = await scraper.scrape_list(
                username=clean_username,
                category=cat,
                list_type="ja-vi",
                client=client,
            )
            all_movies.extend(movies)
            total_pages_count += pages

    if not all_movies:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum filme assistido foi encontrado para o usuário '{clean_username}'. Verifique se o perfil existe e se é público.",
        )

    print(f"\n[RESUMO] Encontradas {total_pages_count} página(s) e {len(all_movies)} filmes para '{clean_username}'.")

    csv_buffer = generate_letterboxd_csv(all_movies)

    filename = f"letterboxd_{clean_username}_watched.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "text/csv; charset=utf-8",
        "X-Total-Pages": str(total_pages_count),
        "X-Total-Movies": str(len(all_movies)),
        "Access-Control-Expose-Headers": "X-Total-Pages, X-Total-Movies",
    }

    return StreamingResponse(
        iter([csv_buffer.getvalue()]),
        media_type="text/csv",
        headers=headers,
    )


async def cli_export(username: str):
    clean_username = username.strip().lower().lstrip("@")
    if "filmow.com/usuario/" in clean_username:
        match = re.search(r"filmow\.com/usuario/([^/]+)", clean_username)
        if match:
            clean_username = match.group(1)

    scraper = FilmowScraper()
    categories = ["filmes", "curtas", "tv"]
    all_movies = []
    total_pages_count = 0

    async with AsyncSession(impersonate="chrome120", timeout=20.0) as client:
        for cat in categories:
            movies, pages = await scraper.scrape_list(
                username=clean_username,
                category=cat,
                list_type="ja-vi",
                client=client,
            )
            all_movies.extend(movies)
            total_pages_count += pages

    if not all_movies:
        print(f"Nenhum filme assistido foi encontrado ou não foi possível acessar o perfil de '{clean_username}'.")
        return

    csv_data = generate_letterboxd_csv(all_movies)
    output_filename = f"letterboxd_{clean_username}_watched.csv"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write(csv_data.getvalue())

    print(f"\n[SUCESSO] Encontradas {total_pages_count} páginas com {len(all_movies)} filmes.")
    print(f"Arquivo exportado com sucesso: {output_filename}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        cli_username = sys.argv[1]
        asyncio.run(cli_export(cli_username))
    else:
        print("Iniciando servidor FilmowBox em http://127.0.0.1:8000 ...")
        uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)