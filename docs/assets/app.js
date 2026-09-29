(function () {
  'use strict';

  var PALETTE = ['#2f6fed', '#19a57f', '#e5484d', '#f5a524', '#8e4ec6',
    '#0ea5c6', '#d6409f', '#6e7f16', '#b4541a', '#5b5bd6'];
  var PAGE = 60;
  var GRAPH_MAX = 500;

  var state = {
    data: null, topics: [], color: {},
    view: 'cards', q: '', topic: null, range: 30, sort: 'date',
    codeOnly: false, shown: PAGE
  };

  var $ = function (id) { return document.getElementById(id); };

  function store(key, val) {
    try {
      if (val === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, val);
    } catch (e) { return null; }
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function votes(p) { return (p.hf || 0) + (p.ax || 0); }

  function isoDaysAgo(n) {
    var d = new Date();
    d.setDate(d.getDate() - n);
    return d.toISOString().slice(0, 10);
  }

  // ---------- filtering ----------
  function filtered() {
    var q = state.q.trim().toLowerCase();
    var since = state.range ? isoDaysAgo(state.range) : '';
    var out = state.data.papers.filter(function (p) {
      if (since && p.date < since) return false;
      if (state.topic && p.topics.indexOf(state.topic) < 0) return false;
      if (state.codeOnly && !p.code) return false;
      if (q) {
        var hay = (p.title + ' ' + p.author + ' ' + p.id).toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    });
    if (state.sort === 'votes') {
      out.sort(function (a, b) { return votes(b) - votes(a) || (a.date < b.date ? 1 : -1); });
    }
    return out;
  }

  // ---------- rendering helpers ----------
  function linksHtml(p) {
    var h = '<a class="pill" href="' + esc(p.url) + '" target="_blank" rel="noopener">arXiv</a>';
    h += '<a class="pill" href="https://arxiv.org/pdf/' + esc(p.id) + '" target="_blank" rel="noopener">PDF</a>';
    if (p.code) h += '<a class="pill code" href="' + esc(p.code) + '" target="_blank" rel="noopener">Code</a>';
    if (p.hf != null) h += '<a class="pill" href="https://huggingface.co/papers/' + esc(p.id) + '" target="_blank" rel="noopener">🤗 ' + p.hf + '</a>';
    if (p.ax != null) h += '<a class="pill" href="https://alphaxiv.org/abs/' + esc(p.id) + '" target="_blank" rel="noopener">αX ↑' + p.ax + '</a>';
    return h;
  }

  function tagsHtml(p) {
    return p.topics.map(function (t) {
      return '<button class="tag" data-topic="' + esc(t) + '"><span style="color:' + state.color[t] + '">●</span> ' + esc(t) + '</button>';
    }).join('');
  }

  function cardHtml(p) {
    return '<article class="card">' +
      '<h3><a href="' + esc(p.url) + '" target="_blank" rel="noopener">' + esc(p.title) + '</a></h3>' +
      '<div class="meta">' + esc(p.author) + ' et al. · ' + esc(p.date) + ' · ' + esc(p.id) + '</div>' +
      '<div class="tags">' + tagsHtml(p) + '</div>' +
      '<div class="links">' + linksHtml(p) + '</div>' +
      '</article>';
  }

  function fmtDay(iso) {
    var d = new Date(iso + 'T00:00:00');
    if (isNaN(d)) return iso;
    return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
  }

  function renderCards(list) {
    var el = $('view-cards');
    var items = list.slice(0, state.shown);
    if (!items.length) { el.innerHTML = '<p class="empty">No papers match these filters.</p>'; return; }
    if (state.sort !== 'date') {
      el.innerHTML = '<div class="grid">' + items.map(cardHtml).join('') + '</div>';
      return;
    }
    var html = '', day = null, buf = [];
    items.forEach(function (p) {
      if (p.date !== day) {
        if (buf.length) html += '<div class="grid">' + buf.join('') + '</div>';
        buf = [];
        day = p.date;
        html += '<h2 class="day">' + esc(fmtDay(day)) + '</h2>';
      }
      buf.push(cardHtml(p));
    });
    if (buf.length) html += '<div class="grid">' + buf.join('') + '</div>';
    el.innerHTML = html;
  }

  function renderList(list) {
    var el = $('view-list');
    var items = list.slice(0, state.shown);
    if (!items.length) { el.innerHTML = '<p class="empty">No papers match these filters.</p>'; return; }
    el.innerHTML = '<div class="table-wrap"><table><thead><tr>' +
      '<th>Date</th><th>Title</th><th>Authors</th><th>Links</th></tr></thead><tbody>' +
      items.map(function (p) {
        return '<tr><td class="nowrap">' + esc(p.date) + '</td>' +
          '<td><a class="title" href="' + esc(p.url) + '" target="_blank" rel="noopener">' + esc(p.title) + '</a>' +
          '<div class="tags" style="margin-top:6px">' + tagsHtml(p) + '</div></td>' +
          '<td>' + esc(p.author) + ' et al.</td>' +
          '<td><div class="links">' + linksHtml(p) + '</div></td></tr>';
      }).join('') + '</tbody></table></div>';
  }

  function renderStats() {
    var papers = state.data.papers;
    var week = isoDaysAgo(7);
    var recent = papers.filter(function (p) { return p.date >= week; }).length;
    var withCode = papers.filter(function (p) { return p.code; }).length;
    $('stats').innerHTML = [
      [papers.length.toLocaleString(), 'papers tracked'],
      [recent.toLocaleString(), 'in the last 7 days'],
      [withCode.toLocaleString(), 'with code'],
      [state.topics.length, 'topics']
    ].map(function (s) { return '<div class="stat"><b>' + s[0] + '</b><span>' + s[1] + '</span></div>'; }).join('');
  }

  function renderChips() {
    var counts = {};
    var since = state.range ? isoDaysAgo(state.range) : '';
    state.data.papers.forEach(function (p) {
      if (since && p.date < since) return;
      p.topics.forEach(function (t) { counts[t] = (counts[t] || 0) + 1; });
    });
    var total = state.data.papers.filter(function (p) { return !since || p.date >= since; }).length;
    var html = '<button class="chip' + (state.topic ? '' : ' active') + '" data-topic="">All <span class="n">' + total + '</span></button>';
    state.topics.forEach(function (t) {
      html += '<button class="chip' + (state.topic === t ? ' active' : '') + '" data-topic="' + esc(t) + '">' +
        '<span class="dot" style="background:' + state.color[t] + '"></span>' + esc(t) +
        ' <span class="n">' + (counts[t] || 0) + '</span></button>';
    });
    $('topics').innerHTML = html;
  }

  function render() {
    var list = filtered();
    renderChips();
    $('count').textContent = list.length.toLocaleString() + ' paper' + (list.length === 1 ? '' : 's');
    ['cards', 'list', 'graph'].forEach(function (v) { $('view-' + v).hidden = state.view !== v; });
    if (state.view === 'cards') renderCards(list);
    else if (state.view === 'list') renderList(list);
    else renderGraph(list);
    $('more').hidden = state.view === 'graph' || list.length <= state.shown;
  }

  // ---------- graph view ----------
  var sim = null;

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function renderGraph(list) {
    var svgEl = $('graph');
    if (typeof d3 === 'undefined') {
      svgEl.outerHTML = '<p class="empty">Graph library failed to load.</p>';
      return;
    }
    if (sim) sim.stop();
    var papers = list.slice(0, GRAPH_MAX);
    var w = svgEl.clientWidth || 900, h = svgEl.clientHeight || 600;
    var topicsUsed = {};
    papers.forEach(function (p) { p.topics.forEach(function (t) { topicsUsed[t] = true; }); });

    var nodes = [], links = [];
    var nTopics = Object.keys(topicsUsed).length;
    state.topics.forEach(function (t, i) {
      if (!topicsUsed[t]) return;
      var a = (2 * Math.PI * nodes.length) / Math.max(nTopics, 1);
      nodes.push({ id: 'T:' + t, kind: 'topic', name: t, x: w / 2 + Math.cos(a) * w / 4, y: h / 2 + Math.sin(a) * h / 4 });
    });
    papers.forEach(function (p) {
      nodes.push({ id: p.id, kind: 'paper', p: p });
      p.topics.forEach(function (t) { links.push({ source: p.id, target: 'T:' + t }); });
    });

    var svg = d3.select(svgEl);
    svg.selectAll('*').remove();
    svg.attr('viewBox', [0, 0, w, h]);
    var g = svg.append('g');
    var zoom = d3.zoom().scaleExtent([0.1, 6]).on('zoom', function (ev) { g.attr('transform', ev.transform); });
    svg.call(zoom);

    var rScale = d3.scaleSqrt().domain([0, d3.max(papers, votes) || 1]).range([3.5, 14]);
    var border = cssVar('--border'), text = cssVar('--text'), surface = cssVar('--surface');

    var link = g.append('g').attr('stroke', border).attr('stroke-opacity', 0.8)
      .selectAll('line').data(links).join('line').attr('stroke-width', 0.8);

    var node = g.append('g').selectAll('g').data(nodes).join('g')
      .style('cursor', 'pointer')
      .call(d3.drag()
        .on('start', function (ev, d) { if (!ev.active) sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y; })
        .on('drag', function (ev, d) { d.fx = ev.x; d.fy = ev.y; })
        .on('end', function (ev, d) { if (!ev.active) sim.alphaTarget(0); if (d.kind === 'paper') { d.fx = null; d.fy = null; } }));

    node.append('circle')
      .attr('r', function (d) { return d.kind === 'topic' ? 18 : rScale(votes(d.p)); })
      .attr('fill', function (d) { return d.kind === 'topic' ? state.color[d.name] : state.color[d.p.topics[0]]; })
      .attr('fill-opacity', function (d) { return d.kind === 'topic' ? 1 : (d.p.topics.length > 1 ? 0.95 : 0.7); })
      .attr('stroke', function (d) { return d.kind === 'paper' && d.p.topics.length > 1 ? text : surface; })
      .attr('stroke-width', function (d) { return d.kind === 'topic' ? 3 : 1; });

    node.filter(function (d) { return d.kind === 'topic'; }).append('text')
      .text(function (d) { return d.name; })
      .attr('y', 32).attr('text-anchor', 'middle')
      .attr('fill', text).attr('font-size', 13).attr('font-weight', 600)
      .attr('paint-order', 'stroke').attr('stroke', surface).attr('stroke-width', 4);

    node.filter(function (d) { return d.kind === 'topic'; }).raise();

    var tip = $('tooltip');
    node.on('mousemove', function (ev, d) {
      tip.hidden = false;
      tip.textContent = d.kind === 'topic' ? d.name : d.p.title;
      tip.style.left = (ev.clientX + 12) + 'px';
      tip.style.top = (ev.clientY + 12) + 'px';
    }).on('mouseleave', function () { tip.hidden = true; })
      .on('click', function (ev, d) {
        ev.stopPropagation();
        if (d.kind === 'topic') { state.topic = state.topic === d.name ? null : d.name; state.shown = PAGE; render(); return; }
        showPanel(d.p);
        var ids = {};
        ids[d.id] = true;
        d.p.topics.forEach(function (t) { ids['T:' + t] = true; });
        node.attr('opacity', function (n) { return ids[n.id] ? 1 : 0.25; });
        link.attr('stroke-opacity', function (l) { return l.source.id === d.id ? 1 : 0.15; })
          .attr('stroke', function (l) { return l.source.id === d.id ? text : border; });
      });
    svg.on('click', function () {
      $('graph-panel').hidden = true;
      node.attr('opacity', 1);
      link.attr('stroke-opacity', 0.8).attr('stroke', border);
    });

    var fitted = false;
    sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(function (d) { return d.id; }).distance(function (l) { return l.source.p && l.source.p.topics.length > 1 ? 120 : 70; }).strength(0.35))
      .force('charge', d3.forceManyBody().strength(function (d) { return d.kind === 'topic' ? -600 : -18; }))
      .force('collide', d3.forceCollide().radius(function (d) { return d.kind === 'topic' ? 30 : rScale(votes(d.p)) + 1.5; }))
      .force('center', d3.forceCenter(w / 2, h / 2))
      .on('end', function () {
        // Fit the settled layout into the viewport once.
        if (fitted) return;
        fitted = true;
        var x0 = d3.min(nodes, function (d) { return d.x; }) - 40, x1 = d3.max(nodes, function (d) { return d.x; }) + 40;
        var y0 = d3.min(nodes, function (d) { return d.y; }) - 40, y1 = d3.max(nodes, function (d) { return d.y; }) + 50;
        var k = Math.min(1.5, 0.95 / Math.max((x1 - x0) / w, (y1 - y0) / h));
        svg.transition().duration(500).call(zoom.transform,
          d3.zoomIdentity.translate(w / 2, h / 2).scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2));
      })
      .on('tick', function () {
        link.attr('x1', function (d) { return d.source.x; }).attr('y1', function (d) { return d.source.y; })
          .attr('x2', function (d) { return d.target.x; }).attr('y2', function (d) { return d.target.y; });
        node.attr('transform', function (d) { return 'translate(' + d.x + ',' + d.y + ')'; });
      });
  }

  function showPanel(p) {
    var el = $('graph-panel');
    el.hidden = false;
    el.innerHTML = '<button class="close" aria-label="Close">×</button>' +
      '<h3><a href="' + esc(p.url) + '" target="_blank" rel="noopener">' + esc(p.title) + '</a></h3>' +
      '<div class="meta">' + esc(p.author) + ' et al. · ' + esc(p.date) + '</div>' +
      '<div class="tags" style="margin-top:8px">' + tagsHtml(p) + '</div>' +
      '<div class="links">' + linksHtml(p) + '</div>';
    el.querySelector('.close').onclick = function (ev) { ev.stopPropagation(); el.hidden = true; };
  }

  // ---------- events ----------
  function bind() {
    var timer;
    $('q').addEventListener('input', function (e) {
      clearTimeout(timer);
      timer = setTimeout(function () { state.q = e.target.value; state.shown = PAGE; render(); }, 150);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === '/' && document.activeElement !== $('q')) { e.preventDefault(); $('q').focus(); }
    });
    $('range').addEventListener('change', function (e) { state.range = +e.target.value; state.shown = PAGE; render(); });
    $('sort').addEventListener('change', function (e) { state.sort = e.target.value; state.shown = PAGE; render(); });
    $('code-only').addEventListener('change', function (e) { state.codeOnly = e.target.checked; state.shown = PAGE; render(); });
    $('more').addEventListener('click', function () { state.shown += PAGE; render(); });

    document.querySelectorAll('.tab').forEach(function (b) {
      b.addEventListener('click', function () {
        document.querySelectorAll('.tab').forEach(function (x) { x.classList.toggle('active', x === b); });
        state.view = b.dataset.view;
        store('view', state.view);
        render();
      });
    });

    // Topic chips and tags share the same data-topic handler.
    document.addEventListener('click', function (e) {
      var t = e.target.closest('[data-topic]');
      if (!t) return;
      var topic = t.dataset.topic || null;
      state.topic = (t.classList.contains('tag') || state.topic !== topic) ? topic : null;
      state.shown = PAGE;
      render();
      if (t.classList.contains('tag')) window.scrollTo({ top: $('topics').offsetTop - 80, behavior: 'smooth' });
    });

    $('theme-btn').addEventListener('click', function () {
      var root = document.documentElement;
      var cur = root.getAttribute('data-theme') ||
        (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      var next = cur === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      store('theme', next);
      if (state.view === 'graph') render();
    });

    var resizeTimer;
    window.addEventListener('resize', function () {
      if (state.view !== 'graph') return;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(render, 200);
    });
  }

  function init(data) {
    state.data = data;
    state.topics = data.topics.map(function (t) { return t.name; });
    state.topics.forEach(function (t, i) { state.color[t] = PALETTE[i % PALETTE.length]; });

    document.title = data.title;
    $('site-title').textContent = data.title;
    $('updated').textContent = data.updated;
    ['repo-link', 'repo-footer'].forEach(function (id) { $(id).href = data.repo; });

    var params = new URLSearchParams(location.search);
    if (params.get('topic') && state.topics.indexOf(params.get('topic')) >= 0) state.topic = params.get('topic');
    var savedView = params.get('view') || store('view');
    if (savedView === 'list' || savedView === 'graph') {
      state.view = savedView;
      document.querySelectorAll('.tab').forEach(function (x) { x.classList.toggle('active', x.dataset.view === savedView); });
    }

    renderStats();
    bind();
    render();
  }

  fetch('data/papers.json', { cache: 'no-cache' })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(init)
    .catch(function (e) {
      $('view-cards').innerHTML = '<p class="empty">Could not load papers (' + esc(e.message) + ').</p>';
    });
})();
