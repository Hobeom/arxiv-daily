import argparse
import datetime
import json
import logging
import os
import re
import time

import feedparser
import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(
    format='[%(asctime)s %(levelname)s] %(message)s',
    datefmt='%m/%d/%Y %H:%M:%S',
    level=logging.INFO,
)
logging.info(f'Using script: {os.path.abspath(__file__)}')

HF_PAPERS_API = 'https://huggingface.co/api/papers/'
HF_CACHE_FILE = 'hf_cache/daily_papers.json'
ALPHAXIV_API = 'https://api.alphaxiv.org/v2/papers/'
ALPHAXIV_CACHE_FILE = 'hf_cache/alphaxiv_cache.json'
ALPHAXIV_REFRESH_DAYS = 30

ARXIV_API_URL = 'https://export.arxiv.org/api/query'
DELAY_SECONDS = 5
NUM_RETRIES = 6


def _requests_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=['GET'],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    s.headers.update({
        'User-Agent':
        'arxiv-daily/1.0 '
        '(+github.com/HoBeom/arxiv-daily; '
        'mailto:jhb1365@gmail.com)',
        'Accept':
        'application/atom+xml, '
        'application/xml;q=0.9, */*;q=0.8',
    })
    return s


_SESSION = _requests_session()


def _get_json_safe(url: str, retries: int = 4, backoff: float = 60.0):
    for attempt in range(retries):
        try:
            resp = _SESSION.get(url, timeout=10)
            if resp.status_code == 429:
                wait = backoff * (2**attempt)  # 60, 120, 240, 480
                logging.warning(
                    'HTTP 429 on %s — waiting %.0fs (attempt %d/%d)', url,
                    wait, attempt + 1, retries)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error('requests error on %s: %s', url, e)
            return None
    logging.error('Max retries exceeded for %s', url)
    return None


class ArxivAuthor:

    def __init__(self, name: str):
        self.name = name

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name


class ArxivPaper:

    def __init__(self, entry: dict):
        self.entry_id = entry.get('id', '')
        self.title = re.sub(r'\s+', ' ', entry.get('title', '').strip())
        self.summary = entry.get('summary', '').strip()
        self.authors = [
            ArxivAuthor(a.get('name', '')) for a in entry.get('authors', [])
        ]
        self.primary_category = entry.get('arxiv_primary_category',
                                          {}).get('term', '')
        self.categories = [t.get('term', '') for t in entry.get('tags', [])]
        self.comment = entry.get('arxiv_comment', None)
        self.updated = self._parse_date(entry.get('updated', ''))
        self.published = self._parse_date(entry.get('published', ''))
        self.code_url = self._extract_code_url(entry)

    @staticmethod
    def _extract_code_url(entry: dict):
        """Extract code URL from arxiv comment or links."""
        comment = entry.get('arxiv_comment', '') or ''
        # Authors often put "Code: https://github.com/..." in comment
        m = re.search(
            r'https?://github\.com/[^\s<>"\')\],;]+',
            comment,
        )
        if m:
            return m.group(0).rstrip('.')
        return None

    @staticmethod
    def _parse_date(date_str: str) -> datetime.datetime:
        if not date_str:
            return datetime.datetime.now()
        for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S%z'):
            try:
                return datetime.datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return datetime.datetime.now()

    def get_short_id(self) -> str:
        match = re.search(r'abs/(.+)$', self.entry_id)
        if match:
            return match.group(1)
        return self.entry_id.split('/')[-1]

    def to_meta(self) -> dict:
        return {
            'title': self.title,
            'abstract': re.sub(r'\s+', ' ', self.summary),
            'authors': [a.name for a in self.authors],
            'categories': self.categories or [self.primary_category],
            'published': self.published.strftime('%Y-%m-%d'),
            'comment': self.comment,
        }


def fetch_arxiv(query: str,
                start: int = 0,
                max_results: int = 10,
                id_list: str = '') -> list:
    params = {
        'search_query': query,
        'id_list': id_list,
        'sortBy': 'submittedDate',
        'sortOrder': 'descending',
        'start': start,
        'max_results': max_results,
    }
    backoff = DELAY_SECONDS
    for attempt in range(NUM_RETRIES + 1):
        logging.info(
            'arXiv request (try %d): query=%s start=%d max=%d',
            attempt,
            query,
            start,
            max_results,
        )
        try:
            resp = _SESSION.get(ARXIV_API_URL, params=params, timeout=30)
        except requests.RequestException as e:
            logging.warning('arXiv network error (%s) — waiting %ds', e,
                            backoff)
        else:
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                return [ArxivPaper(e) for e in feed.entries]
            logging.warning(
                'arXiv HTTP %d — waiting %ds before retry',
                resp.status_code,
                backoff,
            )
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)
    logging.error(
        'arXiv request failed after %d retries for query=%s',
        NUM_RETRIES,
        query,
    )
    return []


def build_subqueries(queries: list,
                     categories: list = None,
                     max_terms: int = 2) -> list:
    """Build arXiv search queries, chunking terms to keep requests small.

    e.g. ['world model', 'VLA'], ['cs.RO'] ->
    ['(all:"world model" OR all:"VLA") AND (cat:cs.RO)']
    """
    cat_filter = ''
    if categories:
        cat_filter = ' AND (' + ' OR '.join(
            f'cat:{c}' for c in categories) + ')'
    subqs = []
    for i in range(0, len(queries), max_terms):
        chunk = queries[i:i + max_terms]
        terms = ' OR '.join(f'all:"{t}"' for t in chunk)
        subqs.append(f'({terms}){cat_filter}')
    return subqs


def load_config(config_file: str) -> dict:
    """
    config_file: input config file path
    return: a dict of configuration
    """
    with open(config_file, 'r', encoding='utf-8') as f:
        config_data = yaml.load(f, Loader=yaml.FullLoader)
    config_data['kv'] = {
        k: build_subqueries(v['queries'], v.get('categories'))
        for k, v in config_data['keywords'].items()
    }
    logging.info(f'{config_data=}')
    return config_data


def get_authors(authors, first_author=False):
    output = str()
    if not first_author:
        output = ', '.join(str(author) for author in authors)
    else:
        output = authors[0]
    return output


def sort_papers(papers):
    output = dict()
    keys = list(papers.keys())
    keys.sort(reverse=True)
    for key in keys:
        output[key] = papers[key]
    return output


_hf_cache = None


def _load_hf_cache() -> dict:
    """Load HF daily papers cache (paper_id -> info)."""
    global _hf_cache
    if _hf_cache is not None:
        return _hf_cache
    _hf_cache = {}
    if os.path.exists(HF_CACHE_FILE):
        try:
            with open(HF_CACHE_FILE, 'r') as f:
                raw = json.load(f)
            for date_data in raw.values():
                for pid, info in date_data.get('papers', {}).items():
                    _hf_cache[pid] = info
            logging.info('HF cache loaded: %d papers', len(_hf_cache))
        except Exception as e:
            logging.warning('Failed to load HF cache: %s', e)
    return _hf_cache


_alphaxiv_cache = None


def _load_alphaxiv_cache() -> dict:
    """Load AlphaXiv cache {paper_id: {upvotes, code_url, fetched_at}}."""
    global _alphaxiv_cache
    if _alphaxiv_cache is not None:
        return _alphaxiv_cache
    _alphaxiv_cache = {}
    if os.path.exists(ALPHAXIV_CACHE_FILE):
        try:
            with open(ALPHAXIV_CACHE_FILE, 'r') as f:
                _alphaxiv_cache = json.load(f)
            logging.info('AlphaXiv cache: %d papers', len(_alphaxiv_cache))
        except Exception as e:
            logging.warning('Failed to load AlphaXiv cache: %s', e)
    return _alphaxiv_cache


def _save_alphaxiv_cache():
    global _alphaxiv_cache
    if _alphaxiv_cache is None:
        return
    os.makedirs(os.path.dirname(ALPHAXIV_CACHE_FILE), exist_ok=True)
    with open(ALPHAXIV_CACHE_FILE, 'w') as f:
        json.dump(_alphaxiv_cache, f, ensure_ascii=False)


def get_alphaxiv_info(paper_id: str) -> dict:
    """Fetch paper metadata from AlphaXiv API.

    Returns {upvotes, code_url} or None on failure.
    """
    url = ALPHAXIV_API + paper_id + '/metadata'
    r = _get_json_safe(url)
    if not r:
        return None
    try:
        pg = r['data']['paper_group']
        metrics = pg.get('metrics', {})
        gh = pg.get('resources', {}).get('github')
        return {
            'upvotes': metrics.get('public_total_votes', 0),
            'code_url': gh['url'] if gh and gh.get('url') else None,
            'fetched_at': datetime.datetime.now().isoformat(),
        }
    except (KeyError, TypeError):
        return None


def get_alphaxiv_cached(paper_id: str, paper_date: str = '') -> dict:
    """Get AlphaXiv info with caching.

    Fetches from API only if:
    - Not cached yet, or
    - Paper is within ALPHAXIV_REFRESH_DAYS and cache is stale.
    """
    cache = _load_alphaxiv_cache()
    cached = cache.get(paper_id)
    today = datetime.datetime.now()

    needs_refresh = False
    if not cached:
        needs_refresh = True
    elif paper_date:
        try:
            pd = datetime.datetime.strptime(paper_date, '%Y-%m-%d')
            age = (today - pd).days
            if age <= ALPHAXIV_REFRESH_DAYS:
                # Refresh if fetched more than 1 day ago
                fetched = datetime.datetime.fromisoformat(
                    cached.get('fetched_at', '2000-01-01'))
                if (today - fetched).days >= 1:
                    needs_refresh = True
        except ValueError:
            pass

    if needs_refresh:
        time.sleep(0.5)
        info = get_alphaxiv_info(paper_id)
        if info:
            cache[paper_id] = info
            return info
        elif cached:
            return cached
        return None

    return cached


def get_hf_paper_info(paper_id: str) -> dict:
    """Get paper info from cache first, then HF API as fallback.

    Returns dict with keys: exists, repo_url, upvotes.
    """
    cache = _load_hf_cache()
    if paper_id in cache:
        c = cache[paper_id]
        return {
            'exists': True,
            'repo_url': c.get('github_repo'),
            'upvotes': c.get('upvotes', 0),
        }

    url = HF_PAPERS_API + paper_id
    r = _get_json_safe(url)
    if r is None:
        return {'exists': False, 'repo_url': None, 'upvotes': 0}
    return {
        'exists': True,
        'repo_url': r.get('githubRepo') or None,
        'upvotes': r.get('upvotes', 0),
    }


META_FILE = 'arxiv/meta.json'
_meta = None


def load_meta() -> dict:
    """Paper metadata (abstract, full author list, ...) keyed by arXiv id."""
    global _meta
    if _meta is None:
        _meta = {}
        if os.path.exists(META_FILE):
            try:
                with open(META_FILE, 'r', encoding='utf-8') as f:
                    _meta = json.load(f)
            except Exception as e:
                logging.warning('Failed to load %s: %s', META_FILE, e)
    return _meta


def save_meta():
    if _meta is None:
        return
    os.makedirs(os.path.dirname(META_FILE), exist_ok=True)
    with open(META_FILE, 'w', encoding='utf-8') as f:
        json.dump(_meta, f, ensure_ascii=False, sort_keys=True)


def strip_version(paper_id: str) -> str:
    return re.sub(r'v\d+$', '', paper_id)


def get_daily_papers(subqueries, max_results=2):
    logging.info('[arxiv] delay=%ss, retries=%s', DELAY_SECONDS, NUM_RETRIES)
    content = dict()
    logging.info(f'subqueries={subqueries}')
    for idx, subq in enumerate(subqueries):
        results = fetch_arxiv(query=subq, max_results=max_results)
        for result in results:
            paper_id = result.get_short_id()
            # '|' would break the markdown table row
            paper_title = result.title.replace('|', '\\|')
            paper_url = result.entry_id
            paper_first_author = get_authors(result.authors, first_author=True)
            update_time = result.updated.date()

            logging.info(
                'Time=%s title=%s author=%s',
                update_time,
                paper_title,
                paper_first_author,
            )

            ver_pos = paper_id.find('v')
            if ver_pos == -1:
                paper_key = paper_id
            else:
                paper_key = paper_id[0:ver_pos]

            if paper_key in content:
                continue
            load_meta()[paper_key] = result.to_meta()
            try:
                cache = _load_hf_cache()
                cached = cache.get(paper_key)
                # code: arxiv comment -> HF cache -> alphaxiv
                code_url = result.code_url
                if not code_url and cached:
                    code_url = cached.get('github_repo')
                # alphaxiv fallback for code
                ax = get_alphaxiv_cached(paper_key, str(update_time))
                if not code_url and ax and ax.get('code_url'):
                    code_url = ax['code_url']
                code_cell = (f'[link]({code_url})' if code_url else 'null')
                # Stars: HF + AlphaXiv
                stars_cell = _build_stars_cell(paper_key, cached,
                                               str(update_time))
                content[paper_key] = (
                    '|**{}**|**{}**|{} et.al.|[{}]({})|{}|{}|\n'.format(
                        update_time,
                        paper_title,
                        paper_first_author,
                        paper_id,
                        paper_url,
                        code_cell,
                        stars_cell,
                    ))
            except Exception as e:
                logging.error(f'exception: {e} with id: {paper_key}')
        if idx != len(subqueries) - 1:
            time.sleep(5)
    _save_alphaxiv_cache()
    return content


def _build_stars_cell(paper_id, hf_cached, paper_date=''):
    """Build the Stars column with HF + AlphaXiv links."""
    parts = []
    # HF upvotes
    if hf_cached:
        hf_url = f'https://huggingface.co/papers/{paper_id}'
        ups = hf_cached.get('upvotes', 0)
        parts.append(f'[\U0001F917\U0001F44D{ups}]({hf_url})')
    # AlphaXiv upvotes
    ax = get_alphaxiv_cached(paper_id, paper_date)
    if ax:
        ax_url = f'https://alphaxiv.org/abs/{paper_id}'
        ax_ups = ax.get('upvotes', 0)
        parts.append(f'[\u03B1X\u2191{ax_ups}]({ax_url})')
    return ' '.join(parts) if parts else 'null'


def _extract_date(contents: str) -> str:
    """Extract date string from paper entry for sorting."""
    m = re.search(r'\*\*(\d{4}-\d{2}-\d{2})\*\*', contents)
    return m.group(1) if m else '0000-00-00'


def _update_entry(paper_id,
                  contents,
                  needs_code,
                  needs_stars,
                  hf_cache,
                  paper_date=''):
    """Update a paper entry: code + stars (HF + AlphaXiv).

    Entry format: |date|title|authors|pdf|code|stars|\\n
    """
    # Parse into columns
    line = contents.rstrip('\n')
    cols = line.split('|')
    # cols: ['', date, title, authors, pdf, code, stars, '']
    if len(cols) < 8:
        return contents

    # --- code link (col 5) ---
    if needs_code:
        code_url = None
        hf_cached = hf_cache.get(paper_id)
        if hf_cached:
            code_url = hf_cached.get('github_repo')
        ax = get_alphaxiv_cached(paper_id, paper_date)
        if not code_url and ax and ax.get('code_url'):
            code_url = ax['code_url']
        if code_url:
            cols[5] = f'[link]({code_url})'

    # --- stars (col 6) ---
    if needs_stars:
        hf_cached = hf_cache.get(paper_id)
        cols[6] = _build_stars_cell(paper_id, hf_cached, paper_date)

    return '|'.join(cols) + '\n'


def update_paper_links_all(json_file_path: dict):
    """Update paper links across ALL topic files.

    Code links: HF cache -> AlphaXiv API.
    Stars: HF upvotes + AlphaXiv upvotes (recent 30d refreshed).
    """
    hf_cache = _load_hf_cache()

    # Load all files
    file_data = {}
    for topic, filepath in json_file_path.items():
        try:
            with open(filepath, 'r') as f:
                content = f.read()
            file_data[filepath] = json.loads(content) if content else {}
        except FileNotFoundError:
            file_data[filepath] = {}

    # Collect pending papers across all files/topics
    pending = []
    for filepath, json_data in file_data.items():
        for keywords, v in json_data.items():
            for paper_id, contents in v.items():
                contents = str(contents)
                needs_code = '|null|' in contents
                needs_stars = 'alphaxiv.org' not in contents
                if not needs_code and not needs_stars:
                    continue
                date = _extract_date(contents)
                pending.append((date, filepath, keywords, paper_id, contents,
                                needs_code, needs_stars))

    # Sort newest first
    pending.sort(key=lambda x: x[0], reverse=True)
    logging.info('update_paper_links: %d papers to process', len(pending))

    for date, filepath, kw, pid, contents, nc, nh in pending:
        try:
            new_cont = _update_entry(pid, contents, nc, nh, hf_cache, date)
            file_data[filepath][kw][pid] = new_cont
        except Exception as e:
            logging.error(f'exception: {e} with id: {pid}')

    # Save AlphaXiv cache
    _save_alphaxiv_cache()

    # Write back all files
    for filepath, json_data in file_data.items():
        with open(filepath, 'w') as f:
            json.dump(json_data, f)


def update_json_file(filename, data_dict):
    """
    daily update json file using data_dict
    """
    try:
        with open(filename, 'r') as f:
            content = f.read()
            if not content:
                m = {}
            else:
                m = json.loads(content)
        json_data = m.copy()
    except FileNotFoundError:
        logging.info(f'Creating new data file: {filename}')
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        json_data = {}
    except Exception as e:
        logging.error(f'exception: {e} with filename: {filename}')
        json_data = {}

    for data in data_dict:
        for keyword in data.keys():
            papers = data[keyword]

            if keyword in json_data.keys():
                json_data[keyword].update(papers)
            else:
                json_data[keyword] = papers

    with open(filename, 'w') as f:
        json.dump(json_data, f)


def json_to_md(filename, md_filename, task='', to_web=False, use_title=True):
    """
    @param filename: str
    @param md_filename: str
    @return None
    """

    def pretty_math(s: str) -> str:
        ret = ''
        match = re.search(r'\$.*\$', s)
        if match is None:
            return s
        math_start, math_end = match.span()
        space_trail = space_leading = ''
        before, after = s[:math_start], s[math_end:]
        if before and before[-1] not in ' *':
            space_trail = ' '
        if after and after[0] not in ' *':
            space_leading = ' '
        ret += s[:math_start]
        ret += f'{space_trail}${match.group()[1:-1].strip()}${space_leading}'
        ret += s[math_end:]
        return ret

    DateNow = datetime.date.today()
    DateNow = str(DateNow)
    DateNow = DateNow.replace('-', '.')

    with open(filename, 'r') as f:
        content = f.read()
        if not content:
            data = {}
        else:
            data = json.loads(content)

    with open(md_filename, 'w+') as f:
        pass

    with open(md_filename, 'a+') as f:

        if use_title and to_web:
            f.write('---\n' + 'layout: default\n' + '---\n\n')

        for keyword in data.keys():
            day_content = data[keyword]
            if not day_content:
                continue
            f.write(f'## {keyword}\n\n')
            f.write('### Updated on ' + DateNow + '\n\n')

            if use_title:
                if to_web:
                    f.write('|Date|Title|Authors|PDF|Code|Stars|\n')
                    f.write('|:---------|:-----------------------|:---------|'
                            ':------|:------|:------|\n')
                else:
                    f.write('|Date|Title|Authors|PDF|Code|Stars|\n')
                    f.write('|---|---|---|---|---|---|\n')

            day_content = sort_papers(day_content)

            for _, v in day_content.items():
                if v is not None:
                    f.write(pretty_math(v))

            f.write('\n')

            f.write('<p align=right>(<a href="#">back to top</a>)'
                    '</p>\n\n')

    logging.info(f'{task} finished')


def format_keyword_name(keyword):
    """Format the keyword name to lowercase with spaces replaced by dashes."""
    return keyword.lower().replace(' & ', '-and-').replace(' ', '-')


_ROW_ID_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
_LINK_RE = re.compile(r'\((https?://[^)\s]+)\)')
_HF_UP_RE = re.compile('\U0001F917\U0001F44D(\\d+)')
_AX_UP_RE = re.compile('\u03B1X\u2191(\\d+)')


def parse_paper_row(paper_id: str, row: str):
    """Parse a stored markdown row into a dict for the web page.

    Row format: |**date**|**title**|author et.al.|[id](url)|code|stars|
    """
    cols = row.rstrip('\n').split('|')
    if len(cols) < 8:
        return None
    # Titles may contain '|': the last 4 columns are fixed, so re-join.
    date, title = cols[1], '|'.join(cols[2:-5])
    author, pdf, code, stars = cols[-5:-1]
    pdf_m = _ROW_ID_RE.search(pdf)
    code_m = _LINK_RE.search(code)
    hf_m = _HF_UP_RE.search(stars)
    ax_m = _AX_UP_RE.search(stars)
    return {
        'id': paper_id,
        'date': date.strip('*'),
        'title': title.strip().strip('*').strip().replace('\\|', '|'),
        'author': re.sub(r'\s*et\.al\.$', '', author.strip()),
        'url': pdf_m.group(2) if pdf_m else f'https://arxiv.org/abs/{paper_id}',
        'code': code_m.group(1) if code_m else None,
        'hf': int(hf_m.group(1)) if hf_m else None,
        'ax': int(ax_m.group(1)) if ax_m else None,
    }


def demo(**config):
    subqueries = config['kv']
    max_results = config['max_results']
    json_file_path = {
        k: v['json_readme_path']
        for k, v in config['keywords'].items()
    }
    md_readme_path = {
        k: v['md_readme_path']
        for k, v in config['keywords'].items()
    }

    # Update paper links mode: process all files at once, newest first
    if config['update_paper_links']:
        update_paper_links_all(json_file_path)

    failed = []
    for topic, queries in subqueries.items():
        logging.info(f'{topic=} {queries=}')
        json_file = json_file_path[topic]
        md_file = md_readme_path[topic]
        os.makedirs(os.path.dirname(json_file), exist_ok=True)
        os.makedirs(os.path.dirname(md_file), exist_ok=True)

        # One failing topic must not discard the others' results.
        try:
            if not config['update_paper_links']:
                content = get_daily_papers(queries, max_results=max_results)
                update_json_file(json_file, [{topic: content}])
            if config['publish_readme']:
                json_to_md(json_file, md_file, task='Update Readme')
        except Exception as e:
            logging.exception(f'topic {topic} failed: {e}')
            failed.append(topic)

    save_meta()

    if failed:
        logging.warning('Topics with errors: %s', failed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--config_path',
        type=str,
        default='config.yaml',
        help='configuration file path')
    parser.add_argument(
        '--update_paper_links',
        default=False,
        action='store_true',
        help='whether to update paper links etc.',
    )
    args = parser.parse_args()
    config = load_config(args.config_path)
    config = {**config, 'update_paper_links': args.update_paper_links}
    demo(**config)
