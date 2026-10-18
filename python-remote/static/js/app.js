let isSeeking          = false;
let seekTarget         = null;
let dlInterval         = null;
let currentVideo       = null;
let pendingLoad        = null;
let streamVideo        = null;
let streamCompatMode   = false;
let browserSubOffset   = 0.0;
let activeFilter       = 'all';
let currentDetailMovie = null;
let currentSubDelay    = 0.0;
let lastTrackSig       = '';

const DISPLAY = {
    'gallery-view':  'flex',
    'detail-view':   'flex',
    'download-view': 'block',
    'remote-view':   'flex',
    'stream-view':   'flex'
};
const ALL_VIEWS = ['gallery-view', 'detail-view', 'download-view', 'remote-view', 'stream-view'];

function showOnly(id) {
    ALL_VIEWS.forEach(v => {
        const el = document.getElementById(v);
        if (el) el.style.display = 'none';
    });
    const target = document.getElementById(id);
    if (target) target.style.display = DISPLAY[id];
}

/* ─── Library Filtering & Counts ───────────────────────────── */

function updateFilterChipCounts() {
    const cards = Array.from(document.querySelectorAll('.movie-card'));
    const countAll   = cards.length;
    const countHrLoc = cards.filter(c => c.dataset.hrLocal === 'true').length;
    const countHrAny = cards.filter(c => c.dataset.hrLocal === 'true' || c.dataset.hrOnline === 'true').length;
    const count4k    = cards.filter(c => c.dataset.is4k === 'true').length;
    const countHdr   = cards.filter(c => c.dataset.isHdr === 'true').length;
    const countRemux = cards.filter(c => c.dataset.isRemux === 'true').length;

    const setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = String(v); };
    setTxt('count-all', countAll);
    setTxt('count-hr-local', countHrLoc);
    setTxt('count-hr-any', countHrAny);
    setTxt('count-4k', count4k);
    setTxt('count-hdr', countHdr);
    setTxt('count-remux', countRemux);
}

function setLibraryFilter(filterKey, btnEl) {
    activeFilter = filterKey;
    document.querySelectorAll('.filter-chip').forEach(b => b.classList.remove('active'));
    if (btnEl) btnEl.classList.add('active');

    // If user selects HR + Online, ask backend to probe online HR availability if not yet checked
    if (filterKey === 'hr_any') {
        fetch('/api/library?filter=hr_any&check_online_hr=1')
            .then(r => r.json())
            .then(d => {
                if (d && Array.isArray(d.movies)) {
                    const onlineIds = new Set(d.movies.map(m => m.id));
                    document.querySelectorAll('.movie-card').forEach(card => {
                        if (onlineIds.has(card.dataset.id) && card.dataset.hrLocal !== 'true') {
                            card.dataset.hrOnline = 'true';
                        }
                    });
                    updateFilterChipCounts();
                    filterMovies();
                }
            })
            .catch(() => {});
    }
    filterMovies();
}

function filterMovies() {
    const q = (document.getElementById('movie-search').value || '').toLowerCase().trim();
    document.querySelectorAll('.movie-card').forEach(card => {
        const titleMatch = card.dataset.title.toLowerCase().includes(q) ||
                           card.dataset.video.toLowerCase().includes(q);
        let filterMatch = true;
        if (activeFilter === 'hr_local')    filterMatch = card.dataset.hrLocal === 'true';
        else if (activeFilter === 'hr_any') filterMatch = (card.dataset.hrLocal === 'true' || card.dataset.hrOnline === 'true');
        else if (activeFilter === 'en_local') filterMatch = card.dataset.enLocal === 'true';
        else if (activeFilter === '4k')     filterMatch = card.dataset.is4k === 'true';
        else if (activeFilter === 'hdr')    filterMatch = card.dataset.isHdr === 'true';
        else if (activeFilter === 'remux')  filterMatch = card.dataset.isRemux === 'true';
        else if (activeFilter === 'multi_audio') filterMatch = card.dataset.multiAudio === 'true';

        card.style.display = (titleMatch && filterMatch) ? '' : 'none';
    });
}

/* ─── Movie Detail View & Subtitle Discovery ───────────────── */

function openDetailView(movieId, video, title, poster) {
    document.getElementById('detail-nav-title').innerText = title || 'Movie Details';
    document.getElementById('detail-title').innerText = title || '';
    const posterEl = document.getElementById('detail-poster');
    if (poster) {
        posterEl.src = poster;
        posterEl.style.display = '';
    } else {
        posterEl.style.display = 'none';
    }
    document.getElementById('detail-indicators').innerHTML = '<div class="sub-empty-hint">Inspecting media streams &amp; subtitles…</div>';
    document.getElementById('detail-local-subs').innerHTML = '<div class="sub-empty-hint">Loading subtitles…</div>';
    document.getElementById('detail-online-subs').innerHTML =
        '<div class="sub-empty-hint">Tap "Search Croatian (HR)" to query Podnapisi, OpenSubtitles &amp; SubDL with release sync scoring.</div>';
    showOnly('detail-view');

    const targetId = movieId || (video ? video.split('/')[0] : title);
    fetch('/api/movies/' + encodeURIComponent(targetId))
        .then(r => r.json())
        .then(m => {
            if (m.error) return;
            currentDetailMovie = m;
            renderDetailMovie(m);
        })
        .catch(() => {});
}

function renderDetailMovie(m) {
    const vTracks = m.video_tracks || [];
    const v = vTracks[0] || {};
    const r = m.release || {};
    const audioTracks = m.audio_tracks || [];

    document.getElementById('detail-title').innerText = m.title || m.folder;
    const genres = m.genres || m.genre || [];
    const durMin = m.runtime_minutes ? `${m.runtime_minutes} min` : '';
    const subParts = [
        m.year ? String(m.year) : '',
        genres.slice(0, 3).join(' · '),
        durMin
    ].filter(Boolean);
    document.getElementById('detail-subline').innerText = subParts.join('  •  ');
    document.getElementById('detail-rating').innerText = m.rating ? `★ ${m.rating} / 10` : '';
    document.getElementById('detail-synopsis').innerText = m.overview || m.synopsis || '';

    const specParts = [
        m.resolution || '1080p',
        (v.codec || r.video_codec || 'H.264').toUpperCase(),
        v.width && v.height ? `${v.width}×${v.height}` : '',
        v.fps ? `${v.fps} fps` : '',
        m.bitrate_mbps ? `${m.bitrate_mbps} Mbps` : '',
        m.size_human || '',
        r.release_group ? `Group: ${r.release_group}` : ''
    ].filter(Boolean);
    document.getElementById('detail-specs').innerText = specParts.join(' · ');

    // 5 Required Capability Indicators
    const hrLocal = !!m.has_hr_subs_local;
    const hrDl    = !!m.has_hr_subs_downloadable;
    const hrStatus = hrLocal
        ? '✓ Local / Embedded Ready'
        : (hrDl ? '⬇ Downloadable Online' : 'Search Online Below');
    const hrClass = hrLocal ? 'ind-hr-on' : (hrDl ? 'ind-hr-dl' : '');

    const audioCodecs = Array.from(new Set(audioTracks.map(a => (a.codec || '').toUpperCase()).filter(Boolean)));

    const indicators = [
        {
            name: '4K UHD',
            status: m.is_4k ? `✓ ${v.width || 3840}×${v.height || 2160} UHD` : `Standard (${m.resolution || '1080p'})`,
            cls: m.is_4k ? 'ind-on' : ''
        },
        {
            name: 'HDR10',
            status: m.is_hdr ? `✓ ${m.hdr_type || 'HDR10'}` : 'SDR BT.709',
            cls: m.is_hdr ? 'ind-on' : ''
        },
        {
            name: '🇭🇷 HR Subtitles',
            status: hrStatus,
            cls: hrClass
        },
        {
            name: '🇬🇧 EN Subtitles',
            status: m.has_en_subs_local ? '✓ Local / Embedded' : 'Available Online',
            cls: m.has_en_subs_local ? 'ind-on' : ''
        },
        {
            name: 'Multiple Audio',
            status: m.multi_audio
                ? `✓ ${m.audio_track_count || audioTracks.length} Tracks (${audioCodecs.join(', ')})`
                : `1 Track (${audioCodecs[0] || 'Stereo'})`,
            cls: m.multi_audio ? 'ind-on' : ''
        }
    ];

    document.getElementById('detail-indicators').innerHTML = indicators.map(ind => `
        <div class="ind-card ${ind.cls}">
            <span class="ind-name">${escHtml(ind.name)}</span>
            <span class="ind-status">${escHtml(ind.status)}</span>
        </div>
    `).join('');

    // Warnings & Browser AAC Remux button
    const warnings = m.playback_warnings || [];
    document.getElementById('detail-warnings').innerHTML = warnings.map(w =>
        `<div class="warn-banner">&#9888; ${escHtml(w)}</div>`
    ).join('');

    const compatBtn = document.getElementById('detail-btn-compat');
    if (compatBtn) {
        compatBtn.style.display = m.needs_browser_audio_remux ? '' : 'none';
    }

    // Local & Embedded Subtitles with Provenance & Sync Confidence
    const localSubs = Array.isArray(m.subtitles)
        ? m.subtitles
        : ((m.subtitles && m.subtitles.local) ? m.subtitles.local : []);
    const localEl = document.getElementById('detail-local-subs');
    if (!localSubs.length) {
        localEl.innerHTML = '<div class="sub-empty-hint">No local or embedded subtitles yet. Download Croatian or English subtitles below.</div>';
    } else {
        localEl.innerHTML = localSubs.map(s => {
            const syncCode = s.sync_confidence || s.sync_status || 'unverified';
            const syncLabel = s.sync_reason || s.sync_label || (syncCode === 'embedded' ? 'Embedded Track' : 'Local Subtitle');
            const srcInfo = [
                `Lang: ${(s.lang || 'und').toUpperCase()}`,
                `Source: ${s.source || 'local'}`,
                s.encoding ? `Encoding: ${s.encoding}` : '',
                s.release_name ? `Release: ${s.release_name}` : ''
            ].filter(Boolean).join(' · ');
            return `
                <div class="sub-item-row">
                    <div class="sub-item-main">
                        <div class="sub-item-title">
                            <span>${escHtml(s.label || s.filename || 'Subtitle')}</span>
                            <span class="sync-badge sync-${escHtml(syncCode)}">${escHtml(syncLabel)}</span>
                        </div>
                        <div class="sub-item-meta">${escHtml(srcInfo)}</div>
                    </div>
                </div>
            `;
        }).join('');
    }

    // Audio Tracks Breakdown
    const audioEl = document.getElementById('detail-audio-tracks');
    if (!audioTracks.length) {
        audioEl.innerHTML = '<div class="sub-empty-hint">Standard audio stream</div>';
    } else {
        const browserCodecs = new Set(['aac', 'mp3', 'opus', 'vorbis', 'flac']);
        audioEl.innerHTML = audioTracks.map((a, idx) => {
            const codecLower = (a.codec || '').toLowerCase();
            const browserOk = browserCodecs.has(codecLower);
            return `
            <div class="sub-item-row">
                <div class="sub-item-main">
                    <div class="sub-item-title">
                        <span>Track #${idx + 1}: ${escHtml((a.codec || '').toUpperCase())} (${escHtml(a.channel_layout || ((a.channels || 2) + 'ch'))})</span>
                        ${a.is_lossless ? '<span class="sync-badge sync-likely">Lossless HD Audio</span>' : ''}
                    </div>
                    <div class="sub-item-meta">Language: ${escHtml((a.language || a.lang || 'und').toUpperCase())} ${a.title ? '· ' + escHtml(a.title) : ''} · Browser Direct: ${browserOk ? 'Yes' : 'Requires AAC Remux'}</div>
                </div>
            </div>
            `;
        }).join('');
    }
}

function closeDetailView() {
    showOnly('gallery-view');
}

function playFromDetail() {
    if (!currentDetailMovie) return;
    openRemote(currentDetailMovie.title, currentDetailMovie.video, currentDetailMovie.poster);
}

function streamFromDetail(useCompat) {
    if (!currentDetailMovie) return;
    openStream(currentDetailMovie.title, currentDetailMovie.video, useCompat);
}

function openDetailFromRemote() {
    document.getElementById('sub-popup').style.display = 'none';
    if (!currentVideo) return;
    const folder = currentVideo.split('/')[0];
    const title  = document.getElementById('active-title').innerText || folder;
    const poster = document.getElementById('remote-img').src || '';
    openDetailView(folder, currentVideo, title, poster);
}

function searchOnlineSubtitles(lang) {
    if (!currentDetailMovie) return;
    document.getElementById('sub-search-hr').classList.toggle('active', lang === 'hr');
    document.getElementById('sub-search-en').classList.toggle('active', lang === 'en');

    const container = document.getElementById('detail-online-subs');
    container.innerHTML = `<div class="sub-empty-hint">Searching Podnapisi.net, OpenSubtitles &amp; SubDL for ${lang.toUpperCase()} subtitles…</div>`;

    fetch(`/api/subtitles/search?file=${encodeURIComponent(currentDetailMovie.video)}&langs=${encodeURIComponent(lang)}`)
        .then(r => r.json())
        .then(data => {
            const results = data.results || [];
            if (!results.length) {
                container.innerHTML = `<div class="sub-empty-hint">No online ${lang.toUpperCase()} subtitles returned. You can also upload a .srt or .zip file above.</div>`;
                return;
            }
            container.innerHTML = results.map((item) => {
                const syncCode = item.sync_confidence || item.sync_status || 'unverified';
                const syncLabel = item.sync_reason || item.sync_label || 'Title Match';
                const relTitle = item.release_name || item.release || item.filename || currentDetailMovie.title;
                const metaLine = [
                    `Provider: ${item.provider}`,
                    item.fps ? `${item.fps} fps` : '',
                    item.downloads ? `${item.downloads} dl` : ''
                ].filter(Boolean).join(' · ');
                const payload = encodeURIComponent(JSON.stringify(item));
                return `
                    <div class="sub-item-row">
                        <div class="sub-item-main">
                            <div class="sub-item-title">
                                <span>[${escHtml((item.lang || lang).toUpperCase())}] ${escHtml(relTitle)}</span>
                                <span class="sync-badge sync-${escHtml(syncCode)}">${escHtml(syncLabel)}</span>
                            </div>
                            <div class="sub-item-meta">${escHtml(metaLine)}</div>
                        </div>
                        <button class="sub-dl-btn" onclick="downloadOnlineSub('${payload}', this)">Download</button>
                    </div>
                `;
            }).join('');
        })
        .catch(e => {
            container.innerHTML = `<div class="sub-empty-hint">Search error: ${escHtml(String(e))}</div>`;
        });
}

function downloadOnlineSub(encodedItem, btnEl) {
    if (!currentDetailMovie) return;
    const item = JSON.parse(decodeURIComponent(encodedItem));
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.innerText = 'Saving…';
    }
    fetch('/api/subtitles/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            file: currentDetailMovie.video,
            rel_video: currentDetailMovie.video,
            candidate: item
        })
    })
        .then(r => r.json())
        .then(res => {
            if (res.ok || res.status === 'downloaded') {
                if (btnEl) btnEl.innerText = '✓ Installed';
                openDetailView(currentDetailMovie.id, currentDetailMovie.video, currentDetailMovie.title, currentDetailMovie.poster);
                refreshLibraryCards();
            } else {
                if (btnEl) {
                    btnEl.disabled = false;
                    btnEl.innerText = 'Retry';
                }
            }
        })
        .catch(() => {
            if (btnEl) {
                btnEl.disabled = false;
                btnEl.innerText = 'Retry';
            }
        });
}

function uploadSubtitleFile(inputEl) {
    if (!currentDetailMovie || !inputEl.files || !inputEl.files.length) return;
    const fileObj = inputEl.files[0];
    const form = new FormData();
    form.append('file', fileObj);
    form.append('rel_video', currentDetailMovie.video);
    form.append('lang', 'hr');

    fetch('/api/subtitles/upload', {
        method: 'POST',
        body: form
    })
        .then(r => r.json())
        .then(res => {
            inputEl.value = '';
            if (res.ok || res.status === 'uploaded') {
                openDetailView(currentDetailMovie.id, currentDetailMovie.video, currentDetailMovie.title, currentDetailMovie.poster);
                refreshLibraryCards();
            }
        })
        .catch(() => {});
}

function refreshLibraryCards() {
    fetch('/api/movies')
        .then(r => r.json())
        .then(movies => {
            rebuildGrid(movies);
            updateFilterChipCounts();
            filterMovies();
        })
        .catch(() => {});
}

/* ─── Remote & Player Views ────────────────────────────────── */

function openRemote(title, video, poster) {
    if (video === currentVideo) {
        showOnly('remote-view');
        return;
    }
    if (currentVideo !== null) {
        pendingLoad = { title, video, poster };
        document.getElementById('switch-modal').classList.add('active');
        return;
    }
    _doLoad(title, video, poster);
}

function _doLoad(title, video, poster) {
    currentVideo = video;
    lastTrackSig = '';
    document.getElementById('active-title').innerText = title;
    document.getElementById('remote-img').src = poster || '';
    fetch('/api/load?file=' + encodeURIComponent(video));
    showOnly('remote-view');
}

function confirmSwitch() {
    document.getElementById('switch-modal').classList.remove('active');
    if (!pendingLoad) return;
    const { title, video, poster } = pendingLoad;
    pendingLoad = null;
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
    document.getElementById('sub-popup').style.display = 'none';
    showOnly('gallery-view');
}

function showDownloadView() {
    showOnly('download-view');
    refreshDownloads();
    fetch('/api/acquisition/storage')
        .then(r => r.json())
        .then(s => {
            const el = document.getElementById('storage-status-bar');
            if (el && s.free_gb !== undefined) {
                el.innerText = `Storage: ${s.free_gb} GB free of ${s.total_gb} GB (${s.path || ''})`;
            }
        })
        .catch(() => {});
}

function closeDownloadView() {
    showOnly('gallery-view');
    fetch('/api/movies')
        .then(r => r.json())
        .then(movies => {
            const grid = document.getElementById('movie-grid');
            if (movies.length !== grid.querySelectorAll('.movie-card').length) {
                rebuildGrid(movies);
                updateFilterChipCounts();
            }
        })
        .catch(() => {});
}

/* ─── Browser Stream View ──────────────────────────────────── */

function openStream(title, video, forceCompat) {
    const videoEl = document.getElementById('stream-player');
    document.getElementById('stream-title').innerText = title;
    streamVideo = video;
    streamCompatMode = !!forceCompat;
    browserSubOffset = 0.0;
    document.getElementById('stream-sub-offset').innerText = '0.0s';
    document.getElementById('stream-compat-toggle').classList.toggle('active', streamCompatMode);

    _loadStreamSource(0);

    fetch('/api/resume_get?file=' + encodeURIComponent(video))
        .then(r => r.json())
        .then(d => {
            if (typeof d.sub_delay === 'number') {
                browserSubOffset = d.sub_delay;
                document.getElementById('stream-sub-offset').innerText =
                    (browserSubOffset >= 0 ? '+' : '') + browserSubOffset.toFixed(1) + 's';
            }
            if (d.time > 5) {
                if (videoEl.readyState >= 1) {
                    videoEl.currentTime = d.time;
                } else {
                    videoEl.addEventListener('loadedmetadata', () => { videoEl.currentTime = d.time; }, { once: true });
                }
            }
            _reloadStreamSubtitles();
        })
        .catch(() => {
            _reloadStreamSubtitles();
        });
    showOnly('stream-view');
}

function _loadStreamSource(startSec) {
    const videoEl = document.getElementById('stream-player');
    if (!streamVideo) return;
    if (streamCompatMode) {
        videoEl.src = `/stream-compat/${streamVideo}?start=${Math.floor(startSec || 0)}`;
    } else {
        videoEl.src = `/stream/${streamVideo}`;
    }
}

function _reloadStreamSubtitles() {
    const videoEl = document.getElementById('stream-player');
    if (!streamVideo) return;
    videoEl.querySelectorAll('track').forEach(t => t.remove());

    fetch(`/api/subtitles?file=${encodeURIComponent(streamVideo)}&offset=${encodeURIComponent(browserSubOffset)}`)
        .then(r => r.json())
        .then(data => {
            const subs = Array.isArray(data) ? data : (data.subtitles || []);
            let defaultAssigned = false;
            subs.forEach((sub, i) => {
                const track = document.createElement('track');
                track.kind = 'subtitles';
                track.label = sub.label || sub.filename || `Subtitle #${i + 1}`;
                track.srclang = sub.lang || 'en';
                track.src = sub.vtt_url || sub.url;
                if (!defaultAssigned && (sub.lang === 'hr' || i === 0)) {
                    track.default = true;
                    defaultAssigned = true;
                }
                videoEl.appendChild(track);
            });
        })
        .catch(() => {});
}

function adjustBrowserSubOffset(delta) {
    browserSubOffset = Math.round((browserSubOffset + delta) * 10) / 10;
    document.getElementById('stream-sub-offset').innerText =
        (browserSubOffset >= 0 ? '+' : '') + browserSubOffset.toFixed(1) + 's';
    _reloadStreamSubtitles();
}

function toggleStreamCompatMode() {
    const videoEl = document.getElementById('stream-player');
    const curTime = videoEl.currentTime || 0;
    streamCompatMode = !streamCompatMode;
    document.getElementById('stream-compat-toggle').classList.toggle('active', streamCompatMode);
    _loadStreamSource(curTime);
    _reloadStreamSubtitles();
    videoEl.play().catch(() => {});
}

function closeStream() {
    const videoEl = document.getElementById('stream-player');
    if (streamVideo && videoEl.currentTime > 5) {
        fetch('/api/resume_save', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ file: streamVideo, time: videoEl.currentTime, sub_delay: browserSubOffset })
        });
    }
    videoEl.pause();
    videoEl.src = '';
    streamVideo = null;
    showOnly('gallery-view');
}

/* ─── Grid Construction & Card Flip Listeners ──────────────── */

function rebuildGrid(movies) {
    const grid = document.getElementById('movie-grid');
    grid.innerHTML = movies.map(m => {
        const topBadges = [
            m.is_4k ? '<span class="cbadge cbadge-4k">4K</span>' : '',
            m.is_hdr ? `<span class="cbadge cbadge-hdr">${escHtml(m.hdr_type || 'HDR10')}</span>` : '',
            m.is_remux ? '<span class="cbadge cbadge-remux">REMUX</span>' : ''
        ].join('');
        const botBadges = [
            m.has_hr_subs_local
                ? '<span class="cbadge cbadge-hr" title="Local Croatian subtitles available">HR ✓</span>'
                : (m.has_hr_subs_downloadable ? '<span class="cbadge cbadge-hr-dl" title="Croatian subtitles downloadable">HR ⬇</span>' : ''),
            m.has_en_subs_local ? '<span class="cbadge cbadge-en">EN</span>' : '',
            m.multi_audio ? `<span class="cbadge cbadge-audio">${m.audio_track_count || 2} AUD</span>` : ''
        ].join('');

        return `
        <div class="movie-card"
             data-id="${escHtml(m.id || m.folder || m.title)}"
             data-title="${escHtml(m.title)}"
             data-video="${escHtml(m.video)}"
             data-poster="${escHtml(m.poster || '')}"
             data-watched="${m.watched ? 'true' : 'false'}"
             data-is-4k="${m.is_4k ? 'true' : 'false'}"
             data-is-hdr="${m.is_hdr ? 'true' : 'false'}"
             data-is-remux="${m.is_remux ? 'true' : 'false'}"
             data-hr-local="${m.has_hr_subs_local ? 'true' : 'false'}"
             data-hr-online="${m.has_hr_subs_downloadable ? 'true' : 'false'}"
             data-en-local="${m.has_en_subs_local ? 'true' : 'false'}"
             data-multi-audio="${m.multi_audio ? 'true' : 'false'}">
            <div class="card-inner">
                <div class="card-front">
                    ${m.poster
                      ? `<img src="${escHtml(m.poster)}" alt="${escHtml(m.title)}" loading="lazy">`
                      : `<div class="no-poster">${escHtml(m.title)}</div>`}
                    <div class="card-badges-top">${topBadges}</div>
                    <div class="card-badges-bottom">${botBadges}</div>
                </div>
                <div class="card-back">
                    <div class="card-info"><div class="card-loading">Loading…</div></div>
                    <button class="card-detail-btn">&#9432; Details &amp; HR Subs</button>
                    <button class="card-play-btn">&#9654; Play on TV</button>
                    <button class="card-stream-btn">&#9654; Stream Here</button>
                </div>
            </div>
        </div>
        `;
    }).join('');
    attachCardListeners();
}

function attachCardListeners() {
    document.querySelectorAll('.movie-card').forEach(card => {
        card.addEventListener('click', (e) => {
            if (e.target.closest('.card-detail-btn')) {
                card.classList.remove('flipped');
                openDetailView(card.dataset.id, card.dataset.video, card.dataset.title, card.dataset.poster);
                return;
            }
            if (e.target.closest('.card-play-btn')) {
                card.classList.remove('flipped');
                openRemote(card.dataset.title, card.dataset.video, card.dataset.poster);
                return;
            }
            if (e.target.closest('.card-stream-btn')) {
                card.classList.remove('flipped');
                openStream(card.dataset.title, card.dataset.video, false);
                return;
            }
            if (card.classList.contains('flipped')) {
                card.classList.remove('flipped');
                return;
            }
            card.classList.add('flipped');
            if (!card.dataset.infoLoaded) {
                fetch('/api/movieinfo?title=' + encodeURIComponent(card.dataset.title))
                    .then(r => r.json())
                    .then(data => {
                        card.dataset.infoLoaded = 'true';
                        const infoEl = card.querySelector('.card-info');
                        if (!data.found) {
                            infoEl.innerHTML = '<div class="card-loading">Tap Details &amp; HR Subs below</div>';
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
                            '<div class="card-loading">Tap Details &amp; HR Subs</div>';
                    });
            }
        });
    });
}

attachCardListeners();
updateFilterChipCounts();

/* ─── Remote Player Controls & Status Polling ──────────────── */

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

function renderRemoteTracks(data) {
    if (typeof data.sub_delay === 'number') {
        currentSubDelay = data.sub_delay;
        const delayEl = document.getElementById('sub-delay-val');
        if (delayEl) {
            delayEl.innerText = (currentSubDelay >= 0 ? '+' : '') + currentSubDelay.toFixed(1) + 's';
        }
    }

    // Telemetry line (codec, resolution, HDR, bitrate)
    const vInfo = data.video_info || {};
    const telEl = document.getElementById('remote-telemetry');
    if (telEl) {
        const parts = [
            vInfo.codec ? vInfo.codec.toUpperCase() : '',
            vInfo.width && vInfo.height ? `${vInfo.width}×${vInfo.height}` : '',
            vInfo.hdr ? 'HDR10' : '',
            vInfo.hwdec && vInfo.hwdec !== 'no' ? `HW:${vInfo.hwdec}` : '',
            vInfo.bitrate_mbps ? `${vInfo.bitrate_mbps} Mbps` : ''
        ].filter(Boolean);
        telEl.innerText = parts.join(' · ');
    }

    const tracks = data.tracks || {};
    const subs = tracks.sub || [];
    const audios = tracks.audio || [];
    const sig = JSON.stringify({ s: subs, a: audios });
    if (sig === lastTrackSig) return;
    lastTrackSig = sig;

    const subContainer = document.getElementById('sub-tracks-container');
    if (subContainer) {
        const anySelected = subs.some(s => s.selected);
        let html = `<div class="sub-option ${!anySelected ? 'selected' : ''}" onclick="setSub('no')">
            <span>None (Off)</span>${!anySelected ? '<span>✓</span>' : ''}
        </div>`;
        subs.forEach(s => {
            const langTag = (s.lang || 'und').toUpperCase();
            const label = s.title || s.lang_label || `Subtitle #${s.id}`;
            html += `<div class="sub-option ${s.selected ? 'selected' : ''}" onclick="setSub(${s.id})">
                <span>[${escHtml(langTag)}] ${escHtml(label)} ${s.external ? '(Ext)' : ''}</span>
                ${s.selected ? '<span>✓</span>' : ''}
            </div>`;
        });
        subContainer.innerHTML = html;
    }

    const audContainer = document.getElementById('audio-tracks-container');
    if (audContainer && audios.length) {
        audContainer.innerHTML = audios.map(a => {
            const langTag = (a.lang || 'und').toUpperCase();
            const codecTag = (a.codec || '').toUpperCase();
            const ch = a.channels ? `${a.channels}ch` : '';
            return `<div class="sub-option ${a.selected ? 'selected' : ''}" onclick="setAudio(${a.id})">
                <span>[${escHtml(langTag)}] ${escHtml(a.title || codecTag)} (${escHtml(ch)})</span>
                ${a.selected ? '<span>✓</span>' : ''}
            </div>`;
        }).join('');
    }
}

setInterval(() => {
    const remoteVisible = document.getElementById('remote-view').style.display === 'flex';
    if (remoteVisible) {
        fetch('/api/status')
            .then(r => r.json())
            .then(data => {
                if (data.percent === undefined) return;
                if (isSeeking && seekTarget !== null && Math.abs(data.percent - seekTarget) < 3) {
                    isSeeking  = false;
                    seekTarget = null;
                }
                if (!isSeeking) {
                    updateProgress(data.percent, data.time);
                }
                renderRemoteTracks(data);
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
    const sid = (id === 0 || id === 'no' || id === 'off') ? 'no' : id;
    fetch('/api/player/subtitle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sid })
    }).then(() => { lastTrackSig = ''; });
    toggleSubs();
}

function setAudio(aid) {
    fetch('/api/player/audio', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ aid })
    }).then(() => { lastTrackSig = ''; });
}

function setAudioMode(mode) {
    fetch('/api/player/audio', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode })
    });
}

function adjustSubDelay(delta) {
    fetch('/api/player/sub-delay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delta, file: currentVideo })
    })
        .then(r => r.json())
        .then(d => {
            if (typeof d.sub_delay === 'number') {
                currentSubDelay = d.sub_delay;
                document.getElementById('sub-delay-val').innerText =
                    (currentSubDelay >= 0 ? '+' : '') + currentSubDelay.toFixed(1) + 's';
            }
        });
}

function resetSubDelay() {
    fetch('/api/player/sub-delay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delay: 0.0, file: currentVideo })
    })
        .then(r => r.json())
        .then(() => {
            currentSubDelay = 0.0;
            document.getElementById('sub-delay-val').innerText = '0.0s';
        });
}

/* ─── Torrent & Acquisition View (with anime.pi Min/Max Size & Quality Filters) ─ */

let dlFilterSort = 'seeds';
let dlFilterQuality = '';
let dlFilterMinGb = 0;
let dlFilterMaxGb = 0;
let lastTorrentSearchData = null;

function setDlFilter(group, val) {
    if (group === 'sort') {
        dlFilterSort = val;
        document.querySelectorAll('#dl-sort-pills .dl-pill').forEach(b => {
            b.classList.toggle('active', b.dataset.val === val);
        });
    } else if (group === 'quality') {
        dlFilterQuality = val;
        document.querySelectorAll('#dl-quality-pills .dl-pill').forEach(b => {
            b.classList.toggle('active', b.dataset.val === val);
        });
    }
    if (lastTorrentSearchData) {
        renderTorrentResults(lastTorrentSearchData);
    }
}

function setDlSizePill(bound, gbVal) {
    const num = parseFloat(gbVal) || 0;
    if (bound === 'min') {
        dlFilterMinGb = num;
        const inp = document.getElementById('dl-min-gb-input');
        if (inp) inp.value = num > 0 ? String(num) : '';
        document.querySelectorAll('#dl-minsize-pills .dl-pill').forEach(b => {
            b.classList.toggle('active', parseFloat(b.dataset.val) === num);
        });
    } else {
        dlFilterMaxGb = num;
        const inp = document.getElementById('dl-max-gb-input');
        if (inp) inp.value = num > 0 ? String(num) : '';
        document.querySelectorAll('#dl-maxsize-pills .dl-pill').forEach(b => {
            b.classList.toggle('active', parseFloat(b.dataset.val) === num);
        });
    }
    if (lastTorrentSearchData) {
        renderTorrentResults(lastTorrentSearchData);
    }
}

function onDlCustomSizeInput(bound, rawVal) {
    const num = Math.max(0, parseFloat(rawVal) || 0);
    if (bound === 'min') {
        dlFilterMinGb = num;
        document.querySelectorAll('#dl-minsize-pills .dl-pill').forEach(b => {
            b.classList.toggle('active', parseFloat(b.dataset.val) === num);
        });
    } else {
        dlFilterMaxGb = num;
        document.querySelectorAll('#dl-maxsize-pills .dl-pill').forEach(b => {
            b.classList.toggle('active', parseFloat(b.dataset.val) === num);
        });
    }
    if (lastTorrentSearchData) {
        renderTorrentResults(lastTorrentSearchData);
    }
}

function _parseSizeGbJs(t) {
    if (typeof t.size_gb === 'number' && t.size_gb > 0) return t.size_gb;
    if (typeof t.size_bytes === 'number' && t.size_bytes > 0) return t.size_bytes / (1024 ** 3);
    const m = String(t.size || '').match(/(\d+(?:\.\d+)?)\s*(TB|GB|MB)/i);
    if (!m) return 0;
    const v = parseFloat(m[1]);
    const u = m[2].toUpperCase();
    if (u === 'TB') return v * 1024;
    if (u === 'GB') return v;
    if (u === 'MB') return v / 1024;
    return 0;
}

function _filterAndSortTorrentsJs(list) {
    const filtered = (list || []).filter(t => {
        const gb = _parseSizeGbJs(t);
        if (dlFilterMinGb > 0 && gb < dlFilterMinGb) return false;
        if (dlFilterMaxGb > 0 && gb > dlFilterMaxGb) return false;
        if (dlFilterQuality) {
            const q = String(t.quality || '').toLowerCase();
            if (dlFilterQuality === 'remux' && !t.is_remux) return false;
            if (dlFilterQuality === '2160p' && !t.is_4k && !q.includes('2160p')) return false;
            if (dlFilterQuality === '1080p' && (!q.includes('1080p') || t.is_4k)) return false;
            if (dlFilterQuality === '720p' && !q.includes('720p')) return false;
        }
        return true;
    });

    filtered.sort((a, b) => {
        const seedsA = a.seeds || 0, seedsB = b.seeds || 0;
        const sizeA = _parseSizeGbJs(a), sizeB = _parseSizeGbJs(b);
        if (dlFilterSort === 'largest') {
            return (sizeB - sizeA) || (seedsB - seedsA);
        }
        if (dlFilterSort === 'smallest') {
            const sa = sizeA > 0 ? sizeA : 999999;
            const sb = sizeB > 0 ? sizeB : 999999;
            return (sa - sb) || (seedsB - seedsA);
        }
        if (dlFilterSort === 'remux') {
            const remA = a.is_remux ? 1 : 0, remB = b.is_remux ? 1 : 0;
            if (remB !== remA) return remB - remA;
            const kA = a.is_4k ? 1 : 0, kB = b.is_4k ? 1 : 0;
            if (kB !== kA) return kB - kA;
            return (seedsB - seedsA) || (sizeB - sizeA);
        }
        return (seedsB - seedsA) || (sizeB - sizeA);
    });
    return filtered;
}

function torrentSearch() {
    const q = document.getElementById('torrent-search-input').value.trim();
    if (!q) return;
    const container = document.getElementById('torrent-results');
    container.innerHTML = '<p style="text-align:center;opacity:0.5;padding:20px 0;">Searching RARBG, 1337x, TorrentGalaxy, YTS &amp; 4K UHD Remux indexers…</p>';

    fetch('/api/torrent/search?q=' + encodeURIComponent(q))
        .then(r => r.json())
        .then(data => {
            if (data.error) { container.innerHTML = `<p>${escHtml(data.error)}</p>`; return; }
            lastTorrentSearchData = data;
            renderTorrentResults(data);
        })
        .catch(e => { container.innerHTML = `<p>Error: ${escHtml(String(e))}</p>`; });
}

function renderTorrentResults(data) {
    const container = document.getElementById('torrent-results');
    const rawItems = data.data || data.movies || [];
    const rawReleases = data.releases || [];

    const items = rawItems
        .map(m => Object.assign({}, m, { torrents: _filterAndSortTorrentsJs(m.torrents || []) }))
        .filter(m => m.torrents.length > 0);
    const releases = _filterAndSortTorrentsJs(rawReleases);

    if (!items.length && !releases.length) {
        container.innerHTML = '<p style="opacity:0.6;padding:12px 0;">No releases match the active size/quality filters. Try widening Min/Max GB or Quality.</p>';
        return;
    }

    let html = items.map(m => {
        const title = m.name || m.title || 'Unknown';
        const poster = m.poster || m.cover || '';
        const genres = m.genre || m.genres || [];
        const allT = m.torrents || [];
        // When showing "Any" quality in default "Most seeders" mode, ensure the top 4K Remux & 4K UHD options
        // are always surfaced alongside the top-seeded encodes on the movie card.
        let cardTorrents = allT.slice(0, 12);
        if (!dlFilterQuality && dlFilterSort === 'seeds') {
            const topRemux = allT.filter(t => t.is_remux).slice(0, 3);
            const top4k = allT.filter(t => t.is_4k && !t.is_remux).slice(0, 3);
            const merged = [];
            const seen = new Set();
            [...topRemux, ...top4k, ...allT].forEach(t => {
                const k = t.hash || t.magnet;
                if (k && !seen.has(k) && merged.length < 14) {
                    seen.add(k);
                    merged.push(t);
                }
            });
            cardTorrents = merged;
        } else {
            cardTorrents = allT.slice(0, 16);
        }

        const buttons = cardTorrents.map(t => {
            const qStr = t.is_4k ? '4K' : (t.quality || '1080p');
            const tStr = t.is_remux ? 'REMUX' : ((t.type && !qStr.toLowerCase().includes(t.type.toLowerCase())) ? t.type.toUpperCase() : '');
            const hdrStr = (t.hdr && t.hdr !== 'SDR') ? t.hdr : '';
            const provStr = t.provider ? ` · ${t.provider}` : '';
            const seedsStr = typeof t.seeds === 'number' ? ` · ▲${t.seeds}` : '';
            const label = [qStr, tStr, hdrStr].filter(Boolean).join(' ');
            const btnCls = t.is_remux ? 'torrent-quality-btn remux-btn' : (t.is_4k ? 'torrent-quality-btn uhd-btn' : 'torrent-quality-btn');
            return `<button class="${btnCls}" title="${escHtml(t.release_name || label)}"
                onclick='startDl(${JSON.stringify(t.magnet)},${JSON.stringify(title)},${JSON.stringify(poster)})'>
                ${escHtml(label)} (${escHtml(t.size || '')}${escHtml(seedsStr)}${escHtml(provStr)})
             </button>`;
        }).join('');
        return `<div class="torrent-item">
            <div style="display:flex;gap:10px;align-items:flex-start;">
                ${poster ? `<img src="${escHtml(poster)}" style="width:56px;border-radius:8px;" onerror="this.style.display='none'">` : ''}
                <div style="flex:1;min-width:0;">
                    <b style="font-size:14px;">${escHtml(title)}</b>
                    ${m.year ? `<span style="opacity:0.5;font-size:13px;margin-left:6px;">(${escHtml(String(m.year))})</span>` : ''}
                    ${m.rating && m.rating !== 'N/A' ? `<span style="color:var(--accent);font-size:12px;font-weight:700;margin-left:6px;">★ ${escHtml(String(m.rating))}</span>` : ''}
                    <div style="font-size:12px;opacity:0.55;margin:3px 0;">${escHtml(genres.join(', '))}</div>
                    <div style="margin-top:4px;">${buttons}</div>
                </div>
            </div>
        </div>`;
    }).join('');

    if (releases.length) {
        html += `<b class="section-label" style="margin-top:14px;">Matching Releases (${releases.length}) — RARBG · 1337x · TorrentGalaxy · TPB · YTS</b>`;
        html += releases.slice(0, 30).map(r => {
            const rTitle = r.title || r.release_name || 'Movie';
            const rPoster = r.poster_url || '';
            const badges = [
                r.is_4k ? '4K UHD' : (r.quality || ''),
                r.is_remux ? 'REMUX' : '',
                r.hdr && r.hdr !== 'SDR' ? r.hdr : '',
                r.video_codec || '',
                r.audio_codec || '',
                r.provider || ''
            ].filter(Boolean).join(' · ');
            const btnCls = r.is_remux ? 'torrent-quality-btn remux-btn' : (r.is_4k ? 'torrent-quality-btn uhd-btn' : 'torrent-quality-btn');
            return `<div class="torrent-item">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                    <div style="min-width:0;flex:1;">
                        <div style="font-size:12px;font-weight:700;word-break:break-word;">${escHtml(r.release_name || rTitle)}</div>
                        <div style="font-size:11px;opacity:0.72;margin-top:2px;">${escHtml(badges)} · <b>${escHtml(r.size || '')}</b> · ▲${r.seeds || 0} seeds</div>
                    </div>
                    <button class="${btnCls}"
                        onclick='startDl(${JSON.stringify(r.magnet)},${JSON.stringify(rTitle)},${JSON.stringify(rPoster)})'>
                        Download
                    </button>
                </div>
            </div>`;
        }).join('');
    }

    container.innerHTML = html;
}

function addDirectMagnet() {
    const inp = document.getElementById('magnet-direct-input');
    const magnet = (inp.value || '').trim();
    if (!magnet.startsWith('magnet:?')) return;
    inp.value = '';
    startDl(magnet, '', '');
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
                        <span onclick="cancelDl('${escHtml(String(d.id))}')"
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
