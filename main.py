import asyncio, httpx;
from bs4 import BeautifulSoup

async def get_page(url : str, bouncer : asyncio.Semaphore, client : httpx.AsyncClient) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }
    async with bouncer:
        while True:
            try:
                # async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(url, headers=headers)

                response.raise_for_status()

                await asyncio.sleep(1.5) # increased delay to be polite

                print(f"Successfully fetched {url}")                   
                return response.text
            except httpx.HTTPStatusError as exc:
                print(f"HTTP Error: {exc.response.status_code} when trying to fetch {url}")
                if exc.response.status_code == 429:
                    print("Rate limited! Waiting 5 seconds before retrying...")
                    await asyncio.sleep(5)
                else:
                    return "" # Return empty if it's a 404 or something else

            except httpx.RequestError as exc:
                print(f"Network Error: Could not connect to {url} - {exc}")
                print("Waiting 5 seconds before retrying...")
                await asyncio.sleep(5)

def get_total_pages(html : str) -> int:
    soup = BeautifulSoup(html, "lxml")

    last_page_icon = soup.select_one('.icon-double-angle-right')

    if last_page_icon:
        parent_link = last_page_icon.find_parent('a')

        if parent_link and parent_link.has_attr('href'):
            href = parent_link['href']
            page_number = href.split('=')[-1]
            return int(page_number)

    next_page_icon = soup.select_one('.icon-angle-right')

    if next_page_icon:
        pagination_div = soup.select_one('.pagination-centered')

        if pagination_div:
            links = pagination_div.find_all('a')
            if len(links) >= 2:
                page_number = links[-2].text.strip()
                return int(page_number)

    return 1

async def get_total_movies(base_url : str, total_pages : int) -> list:
    urls = []
    for page in range(1, total_pages + 1):
        urls.append(f"{base_url}?pagina={page}")
            
    return urls

async def get_movie_details(client: httpx.AsyncClient, movie: dict, bouncer: asyncio.Semaphore) -> dict:
    
    movie["title"] = "Unknown Title"
    movie["director"] = ""
    movie["year"] = ""
    headers = { ... }

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest"
    }
    async with bouncer:
        try:
            pk = movie['pk']
            url = f"https://filmow.com/async/tooltip/movie/?movie_pk={pk}"
            # Implementation for fetching movie details
            response = await client.get(url, headers=headers)

            response.raise_for_status()

            data = response.json()

            title = data['movie']['title_orig']

            soup = BeautifulSoup(data['html'], 'lxml')

            director = None
            year = None

            for item in soup.select('div.item'):
                # Check if the text "Diretor" is inside this div
                if "Diretor:" in item.text:
                    # The director's name is inside the <a> tag!
                    director = item.find('a').text

                # Check if the text "Lançamento" is inside this div
                elif "Lançamento" in item.text:
                    # The text looks like "Lançamento Mundial: 2025"
                    # Split it by the colon and grab the last part!
                    year = item.text.split(":")[-1].strip()

            movie["title"] = title
            movie["director"] = director
            movie["year"] = year
            
        except httpx.HTTPStatusError as exc:
            print(f"HTTP Error: {exc.response.status_code} when trying to fetch details for movie ID {movie['pk']}")
        
        return movie

async def main():
    url = "https://filmow.com/usuario/leonardohuffoliveira/filmes/ja-vi/"
    bouncer = asyncio.Semaphore(1)
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        lista = await get_page(url, bouncer, client)

        if lista:
            total_pages = get_total_pages(lista)
            print(f"Total pages: {total_pages}")
            movies_urls = await get_total_movies(url, total_pages)

            tasks = []
            for url in movies_urls:
                tasks.append(get_page(url, bouncer, client))

            all_movies = []

            results = await asyncio.gather(*tasks)

            for movie in results:
                soup = BeautifulSoup(movie, "lxml")
                movie_titles = soup.find_all('li', class_='movie_list_item')

                for item in movie_titles:
                    title = item.get('data-movie-pk')

                    rating_span = None
                    rating_span = item.select_one('span.star-rating-small')
            
                    if rating_span:
                        rating_span = rating_span.get('title', '')

                        try:
                            rating_span = float(rating_span.split(" ")[1].replace(',', '.'))
                        except ValueError:
                            rating_span = "Invalid rating"
                        
                    print(f"Movie ID: {title} - Rating: {rating_span}")
                    all_movies.append({ "pk": title, "rating": rating_span })

            tooltip_bouncer = asyncio.Semaphore(20)
            
            detail_tasks = []
            
            for movie in all_movies:
                detail_tasks.append(get_movie_details(client, movie, tooltip_bouncer))
                print(f"Fetching details for movie ID: {movie['pk']}")
            
            final_results = await asyncio.gather(*detail_tasks)
            print(final_results[0])  # Print the first movie's details as an example

        else:
            print("Failed to retrieve the page content.")

        
if __name__ == "__main__":
    asyncio.run(main());