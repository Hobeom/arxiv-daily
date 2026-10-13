"""Enrich collected papers and grow a related-paper graph.

Runs after daily_arxiv.py and before build_explorer.py. Three steps, each
optional and safe to fail (state is saved after every step):

  1. Hugging Face paper API -> hf_cache/hf_papers.json
     upvotes, project page, GitHub repo + stars, organization.
  2. Semantic Scholar -> arxiv/graph.json
     references / citations (batch API) and recommendations per paper,
     kept as arXiv ids.
  3. Graph expansion: arXiv papers that at least `min_score` distinct
     keyword-collected ("seed") papers reference, are cited by, or are
     recommended alongside, are fetched from arXiv and added to every topic
     whose query phrases (and categories) they match. Papers added this way
     never act as seeds, so the graph grows one hop from the keyword
     collection and stops there; rejected candidates are remembered.
"""
import argparse
import collections
import datetime
import json
import logging
import os
import re
import time

import requests

from daily_arxiv import (HF_PAPERS_API, _SESSION, _save_alphaxiv_cache,
                         fetch_arxiv, json_to_md, load_config, load_meta,
                         load_topic_papers, make_paper_row, save_meta,
                         strip_version, update_json_file)

logging.basicConfig(
    format='[%(asctime)s %(levelname)s] %(message)s',
    datefmt='%m/%d/%Y %H:%M:%S',
    level=logging.INFO,
)

HF_FILE = 'hf_cache/hf_papers.json'
GRAPH_FILE = 'arxiv/graph.json'
S2_API = 'https://api.semanticscholar.org'
S2_BATCH_FIELDS = 'externalIds,references.externalIds,citations.externalIds'
S2_REC_FIELDS = 'externalIds,title,publicationDate'
ARXIV_ID_RE = re.compile(r'^\d{4}\.\d{4,5}$')


def now():
    return datetime.datetime.utcnow()


def iso(dt=None):
    return (dt or now()).strftime('%Y-%m-%dT%H:%M:%S')


def days_since(value):
    """Days since an ISO date/datetime string (inf if missing/invalid)."""
    try:
        return (now() - datetime.datetime.fromisoformat(value[:19])).days
    except (TypeError, ValueError):
        return float('inf')


def id_age_days(arxiv_id):
    """Approximate age from a new-style arXiv id (YYMM.nnnnn)."""
    try:
        yymm = arxiv_id.split('.')[0]
        month = datetime.datetime(2000 + int(yymm[:2]), int(yymm[2:]), 1)
        return (now() - month).days
    except (ValueError, IndexError):
        return float('inf')


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def dump_json(obj, path):
    """Write atomically so a killed job never leaves a truncated file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, sort_keys=True,
                  separators=(',', ':'))
    os.replace(tmp, path)


# ------------------------------------------------------------- 1. HF papers
def hf_due(entry, paper_date):
    if not entry:
        return True
    if days_since(paper_date) > 30:
        return False  # old papers: one lookup is enough
    # recent papers: refresh upvotes daily; HF may index a paper later
    return days_since(entry.get('at')) >= (1 if entry.get('ok') else 3)


def enrich_hf(papers, limit):
    cache = load_json(HF_FILE, {})
    todo = [
        pid for pid in sorted(papers, key=lambda i: papers[i]['date'],
                              reverse=True)
        if hf_due(cache.get(pid), papers[pid]['date'])
    ][:limit]
    logging.info('HF: %d cached, %d to look up', len(cache), len(todo))
    errors = 0
    for n, pid in enumerate(todo):
        try:
            r = _SESSION.get(HF_PAPERS_API + pid, timeout=20)
        except requests.RequestException as e:
            errors += 1
            logging.warning('HF network error on %s: %s', pid, e)
            if errors >= 5:
                break
            continue
        if r.status_code == 429:
            logging.warning('HF rate limited after %d lookups; stopping', n)
            break
        if r.status_code == 404:
            cache[pid] = {'ok': False, 'at': iso()}
        elif r.status_code == 200:
            j = r.json()
            cache[pid] = {
                'ok': True,
                'at': iso(),
                'upvotes': j.get('upvotes', 0),
                'github': j.get('githubRepo') or None,
                'stars': j.get('githubStars'),
                'project': j.get('projectPage') or None,
                'org': (j.get('organization') or {}).get('fullname'),
            }
        else:
            errors += 1
            if errors >= 5:
                break
        if n % 200 == 199:
            dump_json(cache, HF_FILE)
        time.sleep(0.3)
    dump_json(cache, HF_FILE)
    on_hf = sum(v.get('ok', False) for v in cache.values())
    logging.info('HF: %d of %d looked-up papers are on HF', on_hf,
                 len(cache))


# --------------------------------------------------------- 2. S2 graph data
class S2Unavailable(Exception):
    pass


class S2Client:

    WAIT_BUDGET = 20 * 60  # total back-off per run before giving up

    def __init__(self, key):
        self.key = key
        self.interval = 1.1 if key else 3.5
        self.last = 0.0
        self.waited = 0

    def request(self, method, url, **kw):
        headers = {'User-Agent': 'arxiv-daily/1.0'}
        if self.key:
            headers['x-api-key'] = self.key
        for delay in (30, 60, 120, None):
            wait = self.interval - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = requests.request(method, url, headers=headers,
                                     timeout=60, **kw)
            except requests.RequestException as e:
                logging.warning('S2 network error: %s', e)
                r = None
            if r is not None and r.status_code in (200, 404):
                return r
            if r is not None and r.status_code in (401, 403) and self.key:
                # A rejected key is worse than none: fall back to the
                # shared unauthenticated pool for the rest of the run.
                logging.warning('S2 rejected the API key (HTTP %d); '
                                'continuing without it', r.status_code)
                self.key, self.interval = None, 3.5
                headers.pop('x-api-key', None)
                continue
            if delay is None or self.waited + delay > self.WAIT_BUDGET:
                break
            self.waited += delay
            logging.warning('S2 %s — waiting %ds',
                            r.status_code if r is not None else 'error',
                            delay)
            time.sleep(delay)
        raise S2Unavailable(url)


def arxiv_ids(items):
    out = []
    for it in items or []:
        aid = ((it or {}).get('externalIds') or {}).get('ArXiv')
        if aid and ARXIV_ID_RE.match(aid):
            out.append(aid)
    return sorted(set(out))


def s2_batch_due(entry, paper_date):
    if not entry or 's2' not in entry:
        return True
    age, fetched = days_since(paper_date), days_since(entry['s2'])
    if not entry.get('found'):
        return age <= 60 and fetched >= 2  # S2 may index it a bit later
    return age <= 90 and fetched >= 7  # citations still growing


def enrich_s2(papers, graph, client, recs_limit):
    nodes = graph.setdefault('papers', {})
    titles = graph.setdefault('titles', {})
    order = sorted(papers, key=lambda i: papers[i]['date'], reverse=True)

    todo = [p for p in order if s2_batch_due(nodes.get(p),
                                             papers[p]['date'])]
    logging.info('S2: %d papers need references/citations', len(todo))
    for i in range(0, len(todo), 500):
        chunk = todo[i:i + 500]
        r = client.request('POST',
                           f'{S2_API}/graph/v1/paper/batch',
                           params={'fields': S2_BATCH_FIELDS},
                           json={'ids': [f'arXiv:{p}' for p in chunk]})
        if r.status_code != 200:
            continue
        for pid, item in zip(chunk, r.json()):
            node = nodes.setdefault(pid, {})
            node['s2'] = iso()
            node['found'] = bool(item)
            if item:
                node['refs'] = arxiv_ids(item.get('references'))
                node['cites'] = arxiv_ids(item.get('citations'))
        dump_json(graph, GRAPH_FILE)

    def recs_due(pid):
        at = nodes.get(pid, {}).get('recs_at')
        if not at:
            return True
        return days_since(papers[pid]['date']) <= 30 and days_since(at) >= 14

    todo = [p for p in order if recs_due(p)][:recs_limit]
    logging.info('S2: %d recommendation lookups this run', len(todo))
    for n, pid in enumerate(todo):
        r = client.request(
            'GET',
            f'{S2_API}/recommendations/v1/papers/forpaper/arXiv:{pid}',
            params={'limit': 10, 'fields': S2_REC_FIELDS})
        node = nodes.setdefault(pid, {})
        node['recs_at'] = iso()
        recs = r.json().get('recommendedPapers', []) if r.ok else []
        node['recs'] = arxiv_ids(recs)
        for rec in recs:
            aid = (rec.get('externalIds') or {}).get('ArXiv')
            if aid and ARXIV_ID_RE.match(aid) and rec.get('title'):
                titles[aid] = [rec['title'], rec.get('publicationDate') or '']
        if n % 25 == 24:
            dump_json(graph, GRAPH_FILE)
    dump_json(graph, GRAPH_FILE)


# ----------------------------------------------------- 3. graph expansion
def topic_matchers(config):
    out = {}
    for topic, info in config['keywords'].items():
        pats = [
            re.compile(r'(?<![\w-])' + re.escape(q.lower()) + r'(?![\w-])')
            for q in info['queries']
        ]
        out[topic] = (pats, set(info.get('categories') or []))
    return out


def match_topics(meta, matchers, allowed):
    """Topics a candidate belongs to, or a rejection reason."""
    cats = set(meta.get('categories') or [])
    if not cats & allowed:
        return [], 'category'
    text = (meta.get('title', '') + ' ' + meta.get('abstract', '')).lower()
    topics = [
        t for t, (pats, tcats) in matchers.items()
        if (not tcats or cats & tcats) and any(p.search(text) for p in pats)
    ]
    return topics, None if topics else 'no-topic'


def expand(config, papers, graph, opts):
    meta = load_meta()
    nodes = graph.get('papers', {})
    decided = graph.setdefault('candidates', {})
    allowed = set(opts['allowed_categories'])
    # Seeds: keyword-collected papers in the allowed categories (not e.g.
    # the astro-ph "VLA" papers stored before the category filter existed).
    seeds = [
        p for p in papers if meta.get(p, {}).get('source') != 'graph' and
        (not meta.get(p, {}).get('categories') or
         allowed & set(meta[p]['categories']))
    ]

    def undecided(t):
        d = decided.get(t)
        # arXiv lookups that came back empty are retried after 30 days
        return d is None or (d.get('reason') == 'not-found' and
                             days_since(d.get('at')) >= 30)

    via = collections.defaultdict(set)
    for s in seeds:
        node = nodes.get(s, {})
        for rel in ('refs', 'cites', 'recs'):
            for t in node.get(rel, []):
                if t not in papers:
                    via[t].add(s)
    pool = sorted(
        (t for t, src in via.items()
         if len(src) >= opts['min_score'] and undecided(t) and
         id_age_days(t) <= opts['max_age_days']),
        key=lambda t: (len(via[t]), t), reverse=True)
    logging.info('Expansion: %d candidates over threshold (%d seeds)',
                 len(pool), len(seeds))
    # How many undecided candidates each threshold would admit; useful when
    # tuning min_score.
    scores = [len(src) for t, src in via.items() if undecided(t)]
    logging.info('Expansion: undecided candidates by score: %s', {
        f'>={k}': sum(x >= k for x in scores) for k in (3, 5, 10, 20, 50)
    })

    matchers = topic_matchers(config)
    added = collections.defaultdict(dict)
    n_added = 0
    # at most 10 arXiv lookups (500 candidates) per run
    for i in range(0, min(len(pool), 500), 50):
        if n_added >= opts['max_new_per_run']:
            break
        chunk = pool[i:i + 50]
        results = fetch_arxiv('', max_results=len(chunk),
                              id_list=','.join(chunk))
        if results is None:
            logging.warning('Expansion: arXiv unavailable; stopping')
            break
        by_id = {strip_version(r.get_short_id()): r for r in results}
        for cand in chunk:
            if n_added >= opts['max_new_per_run']:
                break
            res = by_id.get(cand)
            if res is None:
                decided[cand] = {'status': 'rejected', 'reason': 'not-found',
                                 'score': len(via[cand]), 'at': iso()}
                continue
            topics, reason = match_topics(res.to_meta(), matchers, allowed)
            if not topics:
                decided[cand] = {'status': 'rejected', 'reason': reason,
                                 'score': len(via[cand]), 'at': iso()}
                continue
            try:
                key, row = make_paper_row(res)
            except Exception as e:
                logging.error('Expansion: row failed for %s: %s', cand, e)
                continue
            meta[key].update(source='graph', score=len(via[cand]),
                             via=sorted(via[cand])[:20])
            for t in topics:
                added[t][key] = row
            decided[cand] = {'status': 'added', 'topics': topics,
                             'score': len(via[cand]), 'at': iso()}
            n_added += 1
            logging.info('Expansion: + %s (score %d) -> %s | %s', key,
                         len(via[cand]), topics, res.title[:80])

    for topic, rows in added.items():
        info = config['keywords'][topic]
        update_json_file(info['json_readme_path'], [{topic: rows}])
        if config.get('publish_readme'):
            json_to_md(info['json_readme_path'], info['md_readme_path'],
                       task=f'Update Readme (graph) {topic}')
    save_meta()
    _save_alphaxiv_cache()
    dump_json(graph, GRAPH_FILE)
    logging.info('Expansion: added %d papers (%s)', n_added,
                 {t: len(r) for t, r in added.items()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path', default='config.yaml')
    parser.add_argument('--skip', nargs='*', default=[],
                        choices=['hf', 's2', 'expand'])
    args = parser.parse_args()
    config = load_config(args.config_path)
    opts = {
        'hf_per_run': 1500,
        's2_recs_per_run': 150,
        'graph_expansion': True,
        'min_score': 5,
        'max_age_days': 365,
        'max_new_per_run': 30,
        'allowed_categories': ['cs.RO', 'cs.CV', 'cs.AI', 'cs.LG'],
    }
    opts.update(config.get('enrich') or {})
    papers = load_topic_papers(config)
    graph = load_json(GRAPH_FILE, {})

    if 'hf' not in args.skip:
        try:
            enrich_hf(papers, opts['hf_per_run'])
        except Exception:
            logging.exception('HF enrichment failed')
    if 's2' not in args.skip:
        key = os.getenv('S2_API_KEY') or None
        logging.info('S2: %s API key', 'using' if key else 'no')
        try:
            enrich_s2(papers, graph, S2Client(key), opts['s2_recs_per_run'])
        except S2Unavailable:
            logging.warning('S2 unavailable; keeping what was fetched')
        except Exception:
            logging.exception('S2 enrichment failed')
        dump_json(graph, GRAPH_FILE)
    if opts['graph_expansion'] and 'expand' not in args.skip:
        try:
            expand(config, papers, graph, opts)
        except Exception:
            logging.exception('Graph expansion failed')


if __name__ == '__main__':
    main()
