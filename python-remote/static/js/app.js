let isSeeking    = false;
let seekTarget   = null;
let dlInterval   = null;
let currentVideo = null;
let pendingLoad  = null;

const DISPLAY = { 'gallery-view': 'flex', 'download-view': 'block', 'remote-view': 'flex' };

function showOnly(id) {
    ['gallery-view', 'download-view', 'remote-view'].forEach(v => {
        document.getElementById(v).style.display = 'none';
    });
    document.getElementById(id).style.display = DISPLAY[id];
}

function openRemote(title, video, poster) {
    if (video === currentVideo) {
        // Same movie already in the player
        showOnly('remote-view');
        return;
    }
    if (currentVideo !== null) {
        // Different movie while one is playing ask for confirmation
        pendingLoad = { title, video, poster };
        document.getElementById('switch-modal').classList.add('active');
        return;
    }
    _doLoad(title, video, poster);
}

function _doLoad(title, video, poster) {
    currentVideo = video;
    document.getElementById('active-title').innerText = title;
    document.getElementById('remote-img').src = poster;
    fetch('/api/load?file=' + encodeURIComponent(video));
    showOnly('remote-view');
}

function confirmSwitch() {
    document.getElementById('switch-modal').classList.remove('active');
    if (!pendingLoad) return;
    const { title, video, poster } = pendingLoad;
    pendingLoad = null;
    // Save timestamp of current movie, then load the new one
    fetch('/api/stop').then(() => {
        currentVideo = null;
        _doLoad(title, video, poster);
    });
}

function cancelSwitch() {
    document.getElementById('switch-modal').classList.remove('active');
    pendingLoad = null;
}

function closeRemote() {
    showOnly('gallery-view');
}

function showDownloadView() {
    showOnly('download-view');
    refreshDownloads();
}

function closeDownloadView() {
    showOnly('gallery-view');
    // Silently refresh grid only if movies list has changed
    fetch('/api/movies')
        .then(r => r.json())
        .then(movies => {
            const grid = document.getElementById('movie-grid');
            if (movies.length !== grid.querySelectorAll('.movie-card').length) {
                rebuildGrid(movies);
            }
        })
        .catch(() => {});
}

function rebuildGrid(movies) {
    const grid = document.getElementById('movie-grid');
    grid.innerHTML = movies.map(m => `
        <div class="movie-card"
             data-title="${escHtml(m.title)}"
             data-video="${escHtml(m.video)}"
             data-poster="${escHtml(m.poster)}"
             data-watched="${m.watched ? 'true' : 'false'}">
            <div class="card-inner">
                <div class="card-front">
                    ${m.poster
                      ? `<img src="${escHtml(m.poster)}" alt="${escHtml(m.title)}" loading="lazy">`
                      : `<div class="no-poster">${escHtml(m.title)}</div>`}
                </div>
                <div class="card-back">
                    <div class="card-info"><div class="card-loading">Loading…</div></div>
                    <button class="card-play-btn">&#9654; Play</button>
                </div>
            </div>
        </div>
    `).join('');
    attachCardListeners();
}

function attachCardListeners() {
    document.querySelectorAll('.movie-card').forEach(card => {
        card.addEventListener('click', (e) => {
            // Play button on back face
            if (e.target.closest('.card-play-btn')) {
                card.classList.remove('flipped');
                openRemote(card.dataset.title, card.dataset.video, card.dataset.poster);
                return;
            }
            // Already flipped → flip back
            if (card.classList.contains('flipped')) {
                card.classList.remove('flipped');
                return;
            }
            // Flip and fetch YTS info (cached after first load)
            card.classList.add('flipped');
            if (!card.dataset.infoLoaded) {
                fetch('/api/movieinfo?title=' + encodeURIComponent(card.dataset.title))
                    .then(r => r.json())
                    .then(data => {
                        card.dataset.infoLoaded = 'true';
                        const infoEl = card.querySelector('.card-info');
                        if (!data.found) {
                            infoEl.innerHTML = '<div class="card-loading">No info found</div>';
                            return;
                        }
                        const genres = (data.genre || []).slice(0, 2).join(' · ');
                        const yearGenre = [String(data.year || ''), genres].filter(Boolean).join(' · ');
                        infoEl.innerHTML =
                            `<div class="card-rating">★ ${escHtml(String(data.rating))}</div>` +
                            (yearGenre ? `<div class="card-year-genre">${escHtml(yearGenre)}</div>` : '') +
                            `<div class="card-synopsis">${escHtml(data.synopsis || 'No synopsis available.')}</div>`;
                    })
                    .catch(() => {
                        card.querySelector('.card-info').innerHTML =
                            '<div class="card-loading">Could not load info</div>';
                    });
            }
        });
    });
}

attachCardListeners();

function filterMovies() {
    const q = document.getElementById('movie-search').value.toLowerCase();
    document.querySelectorAll('.movie-card').forEach(card => {
        const match = card.dataset.title.toLowerCase().includes(q);
        card.style.display = match ? '' : 'none';
    });
}

function fileCmd(action) {
    fetch('/files/' + action);
}

function stopMovie() {
    fetch('/api/stop');
    currentVideo = null;
    closeRemote();
}

function quitPlayer() {
    fetch('/api/quit').then(() => {
        currentVideo = null;
        showOnly('gallery-view');
    });
}

function updateProgress(percent, timestamp) {
    document.getElementById('progress-fill').style.width = percent + '%';
    document.getElementById('time-display').innerText = timestamp;
}

setInterval(() => {
    const remoteVisible = document.getElementById('remote-view').style.display === 'flex';
    if (remoteVisible) {
        fetch('/api/status')
            .then(r => r.json())
            .then(data => {
                if (data.percent === undefined) return;
                // Release seek lock early once MPV confirms position
                if (isSeeking && seekTarget !== null && Math.abs(data.percent - seekTarget) < 3) {
                    isSeeking  = false;
                    seekTarget = null;
                }
                if (!isSeeking) {
                    updateProgress(data.percent, data.time);
                }
            })
            .catch(() => {});
    }
}, 1000);

document.getElementById('progress-bar-el').addEventListener('click', function (e) {
    const rect = this.getBoundingClientRect();
    const pct  = Math.round(((e.clientX - rect.left) / rect.width) * 100);
    isSeeking  = true;
    seekTarget = pct;
    document.getElementById('progress-fill').style.width = pct + '%';
    fetch('/api/command?q=' + encodeURIComponent('seek ' + pct + ' absolute-percent'))
        .then(() => { setTimeout(() => { isSeeking = false; seekTarget = null; }, 3000); });
});

function toggleSubs() {
    const p = document.getElementById('sub-popup');
    p.style.display = p.style.display === 'block' ? 'none' : 'block';
}

function setSub(id) {
    fetch('/api/command?q=' + encodeURIComponent('set sid ' + id));
    toggleSubs();
}

function torrentSearch() {
    const q = document.getElementById('torrent-search-input').value.trim();
    if (!q) return;
    const container = document.getElementById('torrent-results');
    container.innerHTML = '<p style="text-align:center;opacity:0.5;padding:20px 0;">Searching…</p>';

    fetch('/api/torrent/search?q=' + encodeURIComponent(q))
        .then(r => r.json())
        .then(data => {
            if (data.error) { container.innerHTML = `<p>${escHtml(data.error)}</p>`; return; }
            const items = data.data || [];
            if (!items.length) { container.innerHTML = '<p style="opacity:0.5;">No results found</p>'; return; }

            container.innerHTML = items.map(m => {
                const buttons = (m.torrents || []).map(t =>
                    `<button class="torrent-quality-btn"
                        onclick='startDl(${JSON.stringify(t.magnet)},${JSON.stringify(m.name)},${JSON.stringify(m.poster || "")})'>
                        ${escHtml(t.quality || '')} ${escHtml(t.type || '')} (${escHtml(t.size || '')})
                     </button>`
                ).join('');
                return `<div class="torrent-item">
                    <div style="display:flex;gap:10px;align-items:flex-start;">
                        ${m.poster ? `<img src="${escHtml(m.poster)}" style="width:56px;border-radius:8px;" onerror="this.style.display='none'">` : ''}
                        <div>
                            <b style="font-size:14px;">${escHtml(m.name || 'Unknown')}</b>
                            <span style="opacity:0.5;font-size:13px;margin-left:6px;">(${escHtml(String(m.year || ''))})</span>
                            <div style="font-size:12px;opacity:0.55;margin:3px 0;">${escHtml((m.genre || []).join(', '))}</div>
                            <div style="margin-top:4px;">${buttons}</div>
                        </div>
                    </div>
                </div>`;
            }).join('');
        })
        .catch(e => { container.innerHTML = `<p>Error: ${escHtml(String(e))}</p>`; });
}

function startDl(magnet, title, posterUrl) {
    fetch('/api/torrent/download', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ magnet, title, poster_url: posterUrl })
    }).then(() => refreshDownloads());
}

function refreshDownloads() {
    fetch('/api/torrent/status')
        .then(r => r.json())
        .then(data => {
            const list = document.getElementById('downloads-list');
            if (!Array.isArray(data) || !data.length) {
                list.innerHTML = '<p style="opacity:0.5;font-size:13px;">No active downloads</p>';
                if (dlInterval) { clearInterval(dlInterval); dlInterval = null; }
                return;
            }
            list.innerHTML = data.map(d => {
                const rate = (d.download_rate / 1024).toFixed(0);
                return `<div class="dl-item">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <b style="font-size:13px;">${escHtml(d.title)}</b>
                        <span style="font-size:12px;opacity:0.55;">${escHtml(d.state)}</span>
                    </div>
                    <div class="dl-progress-track">
                        <div class="dl-progress-fill" style="width:${d.progress}%;"></div>
                    </div>
                    <div style="display:flex;justify-content:space-between;font-size:11px;opacity:0.55;">
                        <span>${d.progress}% · ${rate} KB/s · ${d.num_peers} peers</span>
                        <span onclick="cancelDl(${d.id})"
                              style="cursor:pointer;color:#ff6b6b;padding-left:8px;">Cancel</span>
                    </div>
                </div>`;
            }).join('');
            if (!dlInterval) dlInterval = setInterval(refreshDownloads, 2000);
        });
}

function cancelDl(id) {
    fetch('/api/torrent/cancel', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ id })
    }).then(() => refreshDownloads());
}

function escHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
