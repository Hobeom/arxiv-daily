"""Build docs/data.json for the paper explorer page (docs/index.html).

Steps (each one degrades gracefully when offline / without API keys):
  1. merge every topic's data.json into one paper list
  2. backfill missing abstracts from the arXiv API into arxiv/meta.json
  3. optional: structured summaries with Gemini (GEMINI_API_KEY),
     cached in arxiv/summaries.json
  4. TF-IDF + SVD embeddings -> 3D UMAP similarity map, k-means sub-topics
     and nearest-neighbour "similar papers"
"""
import argparse
import collections
import datetime
import json
import logging
import os
import re
import time

import numpy as np
import requests
from sklearn.cluster import KMeans
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import normalize

from daily_arxiv import (fetch_arxiv, load_config, load_meta, parse_paper_row,
                         save_meta)

logging.basicConfig(
    format='[%(asctime)s %(levelname)s] %(message)s',
    datefmt='%m/%d/%Y %H:%M:%S',
    level=logging.INFO,
)

SUMMARY_FILE = 'arxiv/summaries.json'
OUT_FILE = 'docs/data.json'
GEMINI_URL = ('https://generativelanguage.googleapis.com/v1beta/models/'
              '{model}:generateContent')

# Words too common in this corpus to describe a cluster.
DOMAIN_STOP = {
    'robot', 'robots', 'robotic', 'learning', 'model', 'models', 'method',
    'methods', 'approach', 'paper', 'propose', 'proposed', 'based', 'using',
    'task', 'tasks', 'results', 'performance', 'show', 'framework', 'new',
    'data', 'training', 'policy', 'policies', 'real', 'world', 'use', 'work',
    'existing', 'demonstrate', 'experiments', 'achieves', 'present', 'novel',
    'state', 'art', 'end', 'large', 'high', 'low', 'time', 'set', 'agents',
    'agent', 'ai', 'introduce', 'significantly', 'improves', 'extensive'
}


# Kept for similarity, but too broad to be useful in a cluster label.
LABEL_STOP = {
    'vla', 'vlas', 'vision-language-action', 'vision', 'language', 'action',
    'actions', 'embodied', 'robotics', 'manipulation'
}


def load_papers(config) -> dict:
    papers = {}
    for topic, info in config['keywords'].items():
        try:
            with open(info['json_readme_path'], 'r') as f:
                content = f.read()
            data = json.loads(content) if content else {}
        except FileNotFoundError:
            data = {}
        for pid, row in data.get(topic, {}).items():
            if pid in papers:
                papers[pid]['topics'].append(topic)
                continue
            paper = parse_paper_row(pid, str(row))
            if paper:
                paper['topics'] = [topic]
                papers[pid] = paper
    return papers


def backfill_meta(ids: list, limit: int, batch: int = 100):
    """Fetch abstracts for papers collected before meta.json existed."""
    meta = load_meta()
    missing = [i for i in ids if i not in meta][:limit]
    if not missing:
        return
    logging.info('Backfilling metadata for %d papers', len(missing))
    for i in range(0, len(missing), batch):
        chunk = missing[i:i + batch]
        results = fetch_arxiv('', max_results=len(chunk),
                              id_list=','.join(chunk))
        if not results:
            logging.warning('Backfill stopped: arXiv returned nothing')
            break
        for r in results:
            meta[re.sub(r'v\d+$', '', r.get_short_id())] = r.to_meta()
        save_meta()
        time.sleep(3)


# ---------------------------------------------------------------- summaries
SUMMARY_PROMPT = """You summarize robotics / physical-AI research papers.
Read the title and abstract and reply with JSON only, using these keys:
  one_liner: one sentence (<= 30 words) saying what they did and why it matters
  problem: 1-2 sentences
  approach: 1-2 sentences
  result: 1-2 sentences with concrete numbers if the abstract has them
  novelty: 1 sentence
  key_points: 3-5 short bullet strings
  methods: up to 5 short method / technique names
  datasets: up to 5 benchmarks, datasets or robot platforms (empty if none)
Do not invent facts that are not in the abstract.

Title: {title}
Abstract: {abstract}
"""


def gemini_summary(title, abstract, api_key, model):
    body = {
        'contents': [{
            'parts': [{
                'text': SUMMARY_PROMPT.format(title=title, abstract=abstract)
            }]
        }],
        'generationConfig': {
            'responseMimeType': 'application/json',
            'temperature': 0.2
        },
    }
    resp = requests.post(
        GEMINI_URL.format(model=model),
        params={'key': api_key},
        json=body,
        timeout=60)
    if resp.status_code == 429:
        raise RuntimeError('rate limited')
    resp.raise_for_status()
    text = resp.json()['candidates'][0]['content']['parts'][0]['text']
    out = json.loads(text)
    if isinstance(out, list):
        out = out[0]
    keep = ('one_liner', 'problem', 'approach', 'result', 'novelty',
            'key_points', 'methods', 'datasets')
    return {k: out[k] for k in keep if out.get(k)}


def update_summaries(papers: list, limit: int, model: str) -> dict:
    summaries = {}
    if os.path.exists(SUMMARY_FILE):
        with open(SUMMARY_FILE, 'r', encoding='utf-8') as f:
            summaries = json.load(f)
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key or limit <= 0:
        return summaries
    todo = [p for p in papers if p['id'] not in summaries and p['abstract']]
    logging.info('Summaries: %d cached, %d to go, doing %d', len(summaries),
                 len(todo), min(limit, len(todo)))
    for p in todo[:limit]:
        try:
            summaries[p['id']] = gemini_summary(p['title'], p['abstract'],
                                                api_key, model)
        except Exception as e:
            logging.warning('Summary failed for %s: %s', p['id'], e)
            if 'rate limited' in str(e):
                break
        time.sleep(4)
    os.makedirs(os.path.dirname(SUMMARY_FILE), exist_ok=True)
    with open(SUMMARY_FILE, 'w', encoding='utf-8') as f:
        json.dump(summaries, f, ensure_ascii=False, sort_keys=True)
    return summaries


# ---------------------------------------------------------------- embedding
def embed(papers: list):
    """Return (3D coords, cluster ids, cluster info, neighbour ids)."""
    # URLs (code links in abstracts) would otherwise form their own cluster.
    texts = [
        re.sub(r'https?://\S+|\S+\.(?:com|io|org)/\S*', ' ',
               (p['title'] + '. ') * 2 + p['abstract']) for p in papers
    ]
    tfidf = TfidfVectorizer(
        stop_words=list(ENGLISH_STOP_WORDS | DOMAIN_STOP),
        ngram_range=(1, 2),
        min_df=2,
        max_df=0.4,
        sublinear_tf=True,
        max_features=60000,
        token_pattern=r'(?u)\b[a-zA-Z][a-zA-Z0-9\-]+\b')
    X = tfidf.fit_transform(texts)
    dims = max(2, min(128, X.shape[1] - 1, len(papers) - 1))
    Z = normalize(TruncatedSVD(dims, random_state=0).fit_transform(X))

    try:
        import umap
        xyz = umap.UMAP(
            n_components=3,
            n_neighbors=15,
            min_dist=0.15,
            metric='cosine',
            random_state=42).fit_transform(Z)
    except Exception as e:  # e.g. umap not installed
        logging.warning('UMAP unavailable (%s); using SVD axes', e)
        xyz = Z[:, :3]
    xyz = (xyz - xyz.mean(0)) / (xyz.std(0) + 1e-9)
    xyz = np.clip(xyz, -3, 3)  # keep a few outliers from shrinking the map

    k = int(np.clip(round(np.sqrt(len(papers) / 3)), 4, 40))
    km = KMeans(k, n_init=4, random_state=0).fit(Z)
    vocab = np.array(tfidf.get_feature_names_out())
    clusters = []
    for c in range(k):
        rows = np.where(km.labels_ == c)[0]
        weights = np.asarray(X[rows].mean(0)).ravel()
        terms = []
        for t in vocab[np.argsort(-weights)]:
            # skip a unigram already covered by a chosen bigram and vice versa
            if (LABEL_STOP.intersection(t.split())
                    or any(t in s or s in t for s in terms)):
                continue
            terms.append(t)
            if len(terms) == 4:
                break
        clusters.append({
            'id': c,
            'label': ' · '.join(terms[:3]),
            'terms': terms,
            'n': int(len(rows)),
        })

    nn = NearestNeighbors(n_neighbors=min(7, len(papers)),
                          metric='cosine').fit(Z)
    _, idx = nn.kneighbors(Z)
    neighbours = [[papers[j]['id'] for j in row if j != i][:6]
                  for i, row in enumerate(idx)]
    return xyz, km.labels_, clusters, neighbours


def build(config, backfill_limit, summary_limit):
    papers = load_papers(config)
    order = sorted(papers, key=lambda i: papers[i]['date'], reverse=True)
    backfill_meta(order, backfill_limit)
    meta = load_meta()

    # Apply each topic's category filter to stored papers too, so papers
    # collected before the filter existed (e.g. astro-ph "VLA") drop out.
    topic_cats = {
        t: set(v['categories'])
        for t, v in config['keywords'].items() if v.get('categories')
    }
    out = []
    for pid in order:
        p, m = papers[pid], meta.get(pid, {})
        cats = set(m.get('categories', []))
        if cats:
            p['topics'] = [
                t for t in p['topics']
                if t not in topic_cats or cats & topic_cats[t]
            ]
        if not p['topics']:
            continue
        out.append({
            'id': pid,
            'title': p['title'],
            'date': p['date'],
            'authors': m.get('authors') or [p['author']],
            'abstract': m.get('abstract', ''),
            'cats': m.get('categories', []),
            'topics': p['topics'],
            'code': p['code'],
            'hf': p['hf'],
            'ax': p['ax'],
        })

    summaries = update_summaries(out, summary_limit,
                                 config.get('summary_model',
                                            'gemini-2.5-flash'))
    for p in out:
        if p['id'] in summaries:
            p['summary'] = summaries[p['id']]

    clusters = []
    if len(out) >= 10:
        xyz, labels, clusters, neighbours = embed(out)
        for p, c, (x, y, z), sim in zip(out, labels, xyz, neighbours):
            p.update(x=round(float(x), 3), y=round(float(y), 3),
                     z=round(float(z), 3), c=int(c), sim=sim)

    authors = collections.Counter(a for p in out for a in p['authors'])
    days = collections.Counter(p['date'] for p in out)
    stats = {
        'total': len(out),
        'topics': [{
            'name': t,
            'n': sum(t in p['topics'] for p in out)
        } for t in config['keywords']],
        'clusters': clusters,
        'authors': authors.most_common(40),
        'days': sorted(days.items()),
        'with_code': sum(bool(p['code']) for p in out),
        'with_summary': sum('summary' in p for p in out),
    }
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(
            {
                'title': config.get('site_title', 'arXiv Daily'),
                'repo': 'https://github.com/{}/{}'.format(
                    config.get('user_name', 'HoBeom'),
                    config.get('repo_name', 'arxiv-daily')),
                'updated': datetime.datetime.utcnow().strftime(
                    '%Y-%m-%d %H:%M UTC'),
                'papers': out,
                'stats': stats,
            },
            f,
            ensure_ascii=False,
            separators=(',', ':'))
    logging.info('Explorer data: %d papers, %d clusters -> %s', len(out),
                 len(clusters), OUT_FILE)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path', default='config.yaml')
    parser.add_argument(
        '--backfill',
        type=int,
        default=5000,
        help='max papers whose abstracts are fetched from arXiv per run')
    parser.add_argument(
        '--summaries',
        type=int,
        default=None,
        help='max Gemini summaries per run (default: config summary_per_run)')
    args = parser.parse_args()
    cfg = load_config(args.config_path)
    n_sum = args.summaries
    if n_sum is None:
        n_sum = cfg.get('summary_per_run', 0)
    build(cfg, args.backfill, n_sum)
