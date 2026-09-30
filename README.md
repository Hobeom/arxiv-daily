<p align="center">
  <h1 align="center"><br><ins>ARXIV-DAILY</ins><br>Automatically Update Papers Daily using Github Actions</h1>

</p>

##

This repository hosts the source code for arxiv-daily, an useful sripts to fetch arxiv paper daily.

## Table of Keywords

Focus: **Physical AI**. Browse everything in the explorer (3D similarity map, list, stats, summaries): https://hobeom.github.io/arxiv-daily/

 * [Physical AI](arxiv/physical-ai/)
 * [Vision Language Action Model](arxiv/vla-model/)
 * [Robot & Agent](arxiv/robot-agent/)
 * [World Models](arxiv/world-models/)
 * [Robot Manipulation](arxiv/robot-manipulation/)
 * [Humanoid & Locomotion](arxiv/humanoid-locomotion/)
 * [Sim-to-Real](arxiv/sim-to-real/)
 * [Embodied Navigation & Driving](arxiv/embodied-navigation/)

## Overview

This codebase is composed of the following parts:

- `daily_arxiv.py`: main scripts to processing given configurations
- `config.yaml`: configuration file of papers' keywords etc. (optional `categories` per topic limits arXiv categories)
- `build_explorer.py`: builds `docs/data.json` for the explorer page `docs/index.html` — abstracts (`arxiv/meta.json`), optional Gemini summaries (`arxiv/summaries.json`), TF-IDF + UMAP 3D similarity map, auto sub-topic clusters and similar papers

## Release plan

 We are still in the process of fully releasing. Here is the release plan:

- [x] Configuration file
- [x] Update code link
- [x] Remove origin wechat
- [x] Remove origin badge
- [x] Remove origin gitpage code
- [ ] Automatically generate Questions and Answers ([auto-paper-analysis](https://github.com/deep-diver/auto-paper-analysis))
- [ ] Reformat citekey ?
- [ ] Questions and Answers link to gitpage
- [ ] Language translation ([`ChatGPT`](https://chat.openai.com/chat))
- [ ] Usefull comments
- [ ] ...
