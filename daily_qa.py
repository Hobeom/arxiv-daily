import argparse
import json
import logging
import os

import yaml

from daily_arxiv import fetch_arxiv
from qa.app import generate_qa

api_key = os.getenv('GEMINI_API_KEY')

logging.basicConfig(
    format='[%(asctime)s %(levelname)s] %(message)s',
    datefmt='%m/%d/%Y %H:%M:%S',
    level=logging.INFO,
)


def load_json(json_path):
    try:
        with open(json_path, 'r') as f:
            content = f.read()
            if not content:
                m = {}
            else:
                m = json.loads(content)
        json_data = m.copy()
    except Exception as e:
        logging.error(f'Failed to read json file: {json_path}')
        logging.error(e)
        json_data = {}
    return json_data


def get_arxiv_ids_from_daily_papers(config):
    arxiv_ids = dict()
    for topic, info in config['keywords'].items():
        json_path = info['json_readme_path']
        ids = load_json(json_path).get(topic, {}).keys()
        arxiv_ids[topic] = sorted(ids)
    return arxiv_ids


def load_qa_json(config):
    qa_json = dict()
    for topic, info in config['keywords'].items():
        json_path = info['qa_json_path']
        qa_json[topic] = load_json(json_path)
    return qa_json


def get_papers_from_arxiv_ids(arxiv_ids):
    query = ' OR '.join([f'id:{arxiv_id}' for arxiv_id in arxiv_ids])
    return fetch_arxiv(query=query, max_results=len(arxiv_ids))


def main(config):
    max_qa_num = config['max_qa_num']

    daily_arxiv_ids = get_arxiv_ids_from_daily_papers(config)
    qa_arxiv_data = load_qa_json(config)

    filterd_arxiv_ids = dict()
    for topic, arxiv_ids in daily_arxiv_ids.items():
        filterd_arxiv_ids[topic] = [
            arxiv_id for arxiv_id in arxiv_ids
            if arxiv_id not in qa_arxiv_data[topic].keys()
        ]
        logging.info(f'{len(filterd_arxiv_ids[topic])=}')

    for topic, info in config['keywords'].items():
        arxiv_ids = filterd_arxiv_ids[topic][:max_qa_num]
        logging.info(f'{arxiv_ids=}')
        if not arxiv_ids:
            continue

        logging.info(f'1. Get metadata for the papers [{arxiv_ids}]')
        papers = get_papers_from_arxiv_ids(arxiv_ids)
        logging.info('...DONE')

        contents = generate_qa(papers=papers, gemini_api_key=api_key)
        qa_arxiv_data[topic].update(contents)

        qa_json_path = info['qa_json_path']
        with open(qa_json_path, 'w') as f:
            json.dump(qa_arxiv_data[topic], f, indent=4)

    # contents = process_arxiv_ids(arxiv_ids=arxiv_ids, gemini_api_key=api_key)
    # save_contents(contents)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--config_path',
        type=str,
        default='config.yaml',
        help='configuration file path')
    args = parser.parse_args()
    config_path = args.config_path
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
        logging.info(f'{config=}')
    main(config)
