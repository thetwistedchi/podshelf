(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const STATUS = { done: 'Downloaded', queued: 'Queued', downloading: 'Downloading', failed: 'Failed',
                   deleted: 'Removed', skipped: 'Not downloaded', new: 'New' };

  function toast(msg, bad) {
    const t = $('#toast');
    t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
    clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 3500);
  }

  function fmtBytes(n) {
    if (!n) return '0 MB';
    return n > 1e9 ? (n / 1073741824).toFixed(2) + ' GB' : (n / 1048576).toFixed(1) + ' MB';
  }

  async function api(url, opts = {}) {
    const r = await fetch(url, { credentials: 'same-origin', ...opts });
    if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }

  // ---------------------------------------------------------- dialogs + tabs
  function open(id) { const d = document.getElementById(id); d && d.showModal(); }
  document.addEventListener('click', (e) => {
    if (e.target.tagName === 'DIALOG') e.target.close();         // click on backdrop
    const tab = e.target.closest('.tab[data-tab]');
    if (tab) {
      const box = tab.closest('dialog, section, body');
      $$('.tab[data-tab]', box).forEach((t) => t.classList.toggle('active', t === tab));
      $$('.tab-panel', box).forEach((p) => (p.hidden = p.id !== tab.dataset.tab));
    }
  });
  setTimeout(() => $$('.flash.ok').forEach((f) => f.remove()), 6000);

  // ---------------------------------------------------------- search
  const sf = $('#search-form');
  if (sf) {
    sf.addEventListener('submit', async (e) => {
      e.preventDefault();
      const q = $('#search-q').value.trim();
      const box = $('#search-results');
      if (!q) return;
      box.innerHTML = '<p class="muted">Searching…</p>';
      try {
        const res = await api('/api/search?q=' + encodeURIComponent(q));
        if (!res.length) { box.innerHTML = '<p class="muted">No podcasts found.</p>'; return; }
        box.innerHTML = '';
        res.forEach((r) => {
          const el = document.createElement('div');
          el.className = 'result';
          el.innerHTML = `<img alt="" loading="lazy"><div><strong></strong><span class="small muted"></span></div>`;
          $('img', el).src = r.image || '/static/placeholder.svg';
          $('strong', el).textContent = r.title;
          $('span', el).textContent = [r.author, r.genre, r.count ? r.count + ' episodes' : ''].filter(Boolean).join(' · ');
          const b = document.createElement('button');
          b.className = 'btn' + (r.subscribed ? '' : ' primary');
          b.textContent = r.subscribed ? 'Subscribed' : 'Choose';
          b.disabled = r.subscribed;
          b.type = 'button';
          b.onclick = () => {
            $('#feed-url').value = r.feed_url;
            $('.tab[data-tab="tab-url"]').click();
            $('#feed-url').focus();
          };
          el.appendChild(b);
          box.appendChild(el);
        });
      } catch (err) { box.innerHTML = ''; toast('Search failed: ' + err.message, true); }
    });
  }
  const addForm = $('#add-form');
  if (addForm) addForm.addEventListener('submit', () => {
    const b = $('button', addForm); b.disabled = true; b.textContent = 'Reading feed…';
  });

  // ---------------------------------------------------------- episodes
  async function act(id, action, confirmMsg, reload) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    try {
      const ep = await api(`/api/episodes/${id}/${action}`, { method: 'POST' });
      if (reload) return location.reload();
      updateRow(ep);
      toast({ download: 'Added to download queue', delete: 'File deleted', cancel: 'Removed from queue',
              pin: ep.pinned ? 'Episode will be kept' : 'Episode follows retention again', skip: 'Skipped',
              played: ep.played ? 'Marked as played' : 'Marked as unplayed' }[action] || 'Done');
    } catch (err) { toast(err.message, true); }
  }

  function actionsHtml(ep) {
    const b = (cls, title, onclick, icon) => `<button class="icon ${cls}" title="${title}" onclick="${onclick}">${icon}</button>`;
    let h = b('', 'Play', `Podshelf.play(${ep.id})`, '▶') +
      b('check' + (ep.played ? ' on' : ''), ep.played ? 'Mark as unplayed' : 'Mark as played', `Podshelf.act(${ep.id}, 'played')`, '✓') +
      b('', 'Edit title and number', `Podshelf.edit(${ep.id})`, '✎');
    if (['new', 'skipped', 'deleted', 'failed'].includes(ep.status)) h += b('', 'Download', `Podshelf.act(${ep.id}, 'download')`, '⬇');
    else if (['queued', 'downloading'].includes(ep.status)) h += b('', 'Cancel', `Podshelf.act(${ep.id}, 'cancel')`, '✕');
    else if (ep.status === 'done')
      h += b('pin' + (ep.pinned ? ' on' : ''), ep.pinned ? 'Unpin (allow cleanup)' : 'Keep forever (ignore retention)', `Podshelf.act(${ep.id}, 'pin')`, '📌') +
           b('', 'Delete file', `Podshelf.act(${ep.id}, 'delete', 'Delete this file?')`, '🗑');
    return h;
  }

  function updateRow(ep) {
    const li = document.getElementById('ep-' + ep.id);
    if (!li) return;
    if (ep.played !== undefined) {
      li.classList.toggle('played', !!ep.played);
      const dot = $('.dot', li);
      if (dot && (ep.played || ep.status !== 'done')) dot.remove();
      if (!dot && !ep.played && ep.status === 'done') $('.ep-title', li).insertAdjacentHTML('afterbegin', '<span class="dot" title="Unplayed"></span>');
    }
    li.dataset.status = ep.status;
    const chip = $('.chip', li);
    chip.className = 'chip status-' + ep.status;
    chip.textContent = STATUS[ep.status] || ep.status;
    if (ep.played !== undefined) $('.ep-actions', li).innerHTML = actionsHtml(ep);
    if (ep.status !== 'downloading') $('.progress', li).hidden = true;
  }

  // ---------------------------------------------------------- edit
  function edit(id) {
    const li = document.getElementById('ep-' + id);
    const f = $('#edit-form');
    f.id.value = id;
    f.title.value = li.dataset.title;
    f.number.value = li.dataset.number;
    $('#edit-orig').textContent = 'Feed title: ' + li.dataset.origTitle;
    open('edit-dialog');
    f.title.focus();
  }
  function resetEdit() {
    const f = $('#edit-form');
    const li = document.getElementById('ep-' + f.id.value);
    f.title.value = li.dataset.origTitle;
    f.number.value = '';
  }
  const ef = $('#edit-form');
  if (ef) ef.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('button.primary', ef); btn.disabled = true;
    try {
      await api(`/api/episodes/${ef.id.value}/edit`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: ef.title.value, number: ef.number.value }),
      });
      location.reload();
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });

  function sanitize(html) {
    const doc = new DOMParser().parseFromString(html || '', 'text/html');
    const ok = new Set(['A', 'P', 'BR', 'B', 'STRONG', 'I', 'EM', 'UL', 'OL', 'LI']);
    const walk = (node) => {
      [...node.children].forEach((c) => {
        walk(c);
        if (!ok.has(c.tagName)) c.replaceWith(...c.childNodes);
        else [...c.attributes].forEach((a) => { if (!(c.tagName === 'A' && a.name === 'href')) c.removeAttribute(a.name); });
        if (c.tagName === 'A') {
          if (!/^https?:/i.test(c.getAttribute('href') || '')) c.removeAttribute('href');
          c.target = '_blank'; c.rel = 'noopener noreferrer';
        }
      });
    };
    $$('script,style,iframe,object,embed', doc).forEach((n) => n.remove());
    walk(doc.body);
    return doc.body.innerHTML.trim() || '<span class="muted">No description.</span>';
  }

  async function toggleDesc(id) {
    const li = document.getElementById('ep-' + id);
    const d = $('.desc', li);
    if (!d.hidden) { d.hidden = true; return; }
    if (!d.dataset.loaded) {
      try {
        const ep = await api('/api/episodes/' + id);
        let html = '';
        if (ep.chapters && ep.chapters.length) {
          html += '<div class="chapters"><strong class="small">Chapters</strong><ol>' + ep.chapters.map((c) => {
            const t = fmtTime(c.start);
            const title = document.createElement('span'); title.textContent = c.title || '';
            return `<li><button class="linklike chap" onclick="Podshelf.play(${id}, ${c.start})">${t}</button> ${title.innerHTML}</li>`;
          }).join('') + '</ol></div>';
        }
        d.innerHTML = html + sanitize(ep.description);
        d.dataset.loaded = 1;
      } catch (err) { toast(err.message, true); return; }
    }
    d.hidden = false;
  }

  // ---------------------------------------------------------- player
  function fmtTime(s) {
    s = Math.floor(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(sec).padStart(2, '0');
  }

  function play(id, at) {
    const li = document.getElementById('ep-' + id);
    const p = $('#player'), a = $('#player-audio');
    $('#player-title').textContent = li ? li.dataset.title : '';
    $('#player-podcast').textContent = li ? li.dataset.podcast : '';
    $('#player-cover').src = li ? li.dataset.cover : '/static/placeholder.svg';
    const src = `/episodes/${id}/audio`;
    if (!a.src.endsWith(src)) a.src = src;
    if (at !== undefined) {
      const seek = () => { a.currentTime = at; };
      if (a.readyState >= 1) seek(); else a.addEventListener('loadedmetadata', seek, { once: true });
    }
    p.hidden = false;
    document.body.classList.add('has-player');
    a.play().catch(() => {});
  }
  function closePlayer() {
    const a = $('#player-audio'); a.pause(); a.removeAttribute('src'); a.load();
    $('#player').hidden = true; document.body.classList.remove('has-player');
  }

  // ---------------------------------------------------------- live status
  let wasRefreshing = new Set($$('[data-podcast]').filter((el) => el.querySelector('.refreshing')).map((el) => +el.dataset.podcast));
  let lastActive = null;
  async function poll() {
    try {
      const s = await api('/api/status');
      const q = s.counts.queued + s.counts.downloading;
      const nq = $('#nav-queue'), nf = $('#nav-failed');
      nq.textContent = q; nq.hidden = !q;
      nf.textContent = s.counts.failed; nf.hidden = !s.counts.failed;

      s.active.forEach((a) => {
        $$(`#ep-${a.id} .progress, #active-list li[data-id="${a.id}"] .progress`).forEach((pr) => {
          pr.hidden = false;
          const pct = a.total ? Math.min(100, (a.done / a.total) * 100) : 0;
          $('.bar', pr).style.width = (a.total ? pct : 100) + '%';
          pr.classList.toggle('indeterminate', !a.total);
          $('.ptext', pr).textContent = a.total
            ? `${fmtBytes(a.done)} of ${fmtBytes(a.total)} · ${fmtBytes(a.speed)}/s`
            : `${fmtBytes(a.done)} · ${fmtBytes(a.speed)}/s`;
        });
        const li = document.getElementById('ep-' + a.id);
        if (li && li.dataset.status !== 'downloading') updateRow({ id: a.id, status: 'downloading' });
      });
      // rows that finished downloading since the last poll
      $$('.episode[data-status="downloading"], .episode[data-status="queued"]').forEach(async (li) => {
        if (!s.active.find((a) => a.id === +li.dataset.id) && li.dataset.status === 'downloading') {
          updateRow(await api('/api/episodes/' + li.dataset.id));
        }
      });

      const nowRefreshing = new Set(s.refreshing);
      const hero = $('.hero[data-podcast]');
      if (hero) {
        const pid = +hero.dataset.podcast;
        if (wasRefreshing.has(pid) && !nowRefreshing.has(pid)) location.reload();
      } else if ($('.grid') && [...wasRefreshing].some((id) => !nowRefreshing.has(id))) {
        location.reload();
      }
      wasRefreshing = nowRefreshing;

      if (window._queuePage) {
        const ids = s.active.map((a) => a.id).sort().join(',');
        if (lastActive !== null && ids !== lastActive) location.reload();
        lastActive = ids;
      }
    } catch (e) { /* network hiccup, try again next round */ }
  }
  setInterval(poll, 3000);
  poll();

  // ---------------------------------------------------------- settings preview
  function namingPreview() {
    const pf = $('#podcast_format'), ef = $('#episode_format'), out = $('#naming-preview');
    let t;
    const upd = () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        try {
          const r = await api(`/api/naming-preview?podcast_format=${encodeURIComponent(pf.value)}&episode_format=${encodeURIComponent(ef.value)}`);
          out.textContent = r.sample;
        } catch (e) {}
      }, 250);
    };
    pf.addEventListener('input', upd); ef.addEventListener('input', upd);
  }

  window.Podshelf = { open, act, edit, resetEdit, toggleDesc, play, closePlayer, namingPreview, toast,
                      watchQueuePage: () => (window._queuePage = true) };
})();
