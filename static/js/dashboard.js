/* Extracted from templates/dashboard.html in v1.2.1.
   These were four consecutive inline <script> blocks immediately
   before </body>. Concatenated IN ORDER and loaded from the same
   position, because later blocks call into earlier ones and some
   run getElementById at top level - they need the DOM already
   parsed, which is why this loads at the end of body and not in
   head. */

/* ---- block 1 of 4 (was inline in dashboard.html) ---- */
        // ---------- Navigation ----------
        document.querySelectorAll('.sidebar-nav a').forEach(link => {
            link.addEventListener('click', function(e) {
                e.preventDefault();
                const page = this.dataset.page;

                // Update active nav
                document.querySelectorAll('.sidebar-nav a').forEach(l => l.classList.remove('active'));
                this.classList.add('active');

                // Show/hide pages
                document.querySelectorAll('.page').forEach(p => p.style.display = 'none');
                const targetPage = document.getElementById('page-' + page);
                if (targetPage) targetPage.style.display = 'block';

                // Load page data
                if (page === 'channels') loadChannels();
                if (page === 'music-videos') {
                    document.getElementById('music-video-search-input').focus();
                }
                if (page === 'settings') {
                    loadConfig();
                    loadPlexBasePath();
                    loadMusicVideoPath();
                    resumeConversionPollingIfRunning();
                    loadSystemHealth();
                    loadMonitorStatus();
                    loadNotifyConfig();
                    loadRetentionConfig();
                }
                if (page === 'dashboard') {
                    loadDashboardStats();
                    // Refresh the sidebar version too. It used to be fetched
                    // once, at page load, and never again — so a tab left open
                    // across an upgrade reported the old version indefinitely,
                    // right next to stats that were refreshing correctly. It
                    // also meant the "update available" pill kept nagging after
                    // you had already installed the update.
                    loadVersionBadge();
                }
                if (page === 'swap-art') loadSwapArtArtists();
                if (page === 'artists') loadArtistsPage();
                if (page === 'downloads') {
                    loadDownloads();
                    // Start polling every 2 seconds while on downloads page
                    if (downloadPollInterval) clearInterval(downloadPollInterval);
                    downloadPollInterval = setInterval(loadDownloads, 2000);
                } else {
                    // Stop polling when leaving downloads page
                    if (downloadPollInterval) {
                        clearInterval(downloadPollInterval);
                        downloadPollInterval = null;
                    }
                }

                // Close the mobile sidebar after navigating
                closeSidebar();
            });
        });

        // ---------- Mobile Sidebar ----------
        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('open');
            document.getElementById('sidebar-backdrop').classList.toggle('active');
        }
        function closeSidebar() {
            document.getElementById('sidebar').classList.remove('open');
            document.getElementById('sidebar-backdrop').classList.remove('active');
        }

        // ---------- Modal Dismissal (Escape key + click-outside) ----------
        function dismissModal(overlay) {
            overlay.classList.remove('active');
            // folder-browser-modal has one extra piece of state to reset on close
            if (overlay.id === 'folder-browser-modal') _folderBrowserTarget = null;
        }
        document.querySelectorAll('.modal-overlay').forEach(overlay => {
            overlay.addEventListener('click', function(e) {
                if (e.target === overlay) dismissModal(overlay);
            });
        });
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                const active = document.querySelector('.modal-overlay.active');
                if (active) dismissModal(active);
            }
        });

        // ---------- HTML escaping ----------
        // Escapes a string for safe interpolation into innerHTML/attribute
        // values built via template literals elsewhere in this file.
        function escapeHtml(str) {
            // Must be safe for BOTH text-content and quoted-attribute-value
            // contexts (this file uses it as both, e.g. `alt="${escapeHtml(x)}"`
            // and `<div>${escapeHtml(x)}</div>`) - the DOM textContent/innerHTML
            // round-trip trick escapes &/</> but NOT quotes (quotes are only
            // special inside attribute values, not text nodes), so it isn't
            // enough on its own. Escape all five manually instead.
            return String(str ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        // Formats a raw byte count (e.g. a file size) as KB/MB/GB.
        // Takes bytes. The Disk Usage card used to inline its own copy of this
        // and got it wrong by exactly one unit — labelling raw bytes as KB, so
        // every reading was 1024x too large. It went unnoticed because the value
        // it formatted came from walking the near-empty staging directory, and
        // "0.0 KB" looks correct however you divide it.
        function formatBytes(b) {
            if (!b) return 'N/A';
            if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
            if (b < 1024 * 1024 * 1024) return (b / (1024 * 1024)).toFixed(1) + ' MB';
            // TB matters now that this measures the media library and not a
            // staging folder — a multi-terabyte NAS would otherwise read '3072.00 GB'.
            if (b < 1024 ** 4) return (b / (1024 ** 3)).toFixed(2) + ' GB';
            return (b / (1024 ** 4)).toFixed(2) + ' TB';
        }

        // ---------- Toast Notifications ----------
        function showToast(message, type) {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = 'toast ' + type;

            const text = document.createElement('span');
            text.textContent = message;
            toast.appendChild(text);

            const closeBtn = document.createElement('button');
            closeBtn.type = 'button';
            closeBtn.className = 'toast-close';
            closeBtn.setAttribute('aria-label', 'Dismiss notification');
            closeBtn.textContent = '✕';
            toast.appendChild(closeBtn);

            container.appendChild(toast);

            let dismissTimer;
            function dismiss() {
                clearTimeout(dismissTimer);
                toast.style.opacity = '0';
                toast.style.transition = 'opacity 0.3s ease';
                setTimeout(() => toast.remove(), 300);
            }
            function scheduleDismiss() {
                dismissTimer = setTimeout(dismiss, 3000);
            }

            toast.addEventListener('mouseenter', () => clearTimeout(dismissTimer));
            toast.addEventListener('mouseleave', scheduleDismiss);
            closeBtn.addEventListener('click', dismiss);

            scheduleDismiss();
        }

        // ---------- Navigation (shared) ----------
        function navigateTo(page) {
            const link = document.querySelector(`.sidebar-nav a[data-page="${page}"]`);
            if (link) link.click();
        }

        // ---------- Dashboard Stats ----------
        async function loadDashboardStats() {
            let channelCount = 0;
            let downloadsCount = 0;
            try {
                const [chResp, statsResp] = await Promise.all([
                    fetch('/api/channels'),
                    fetch('/api/stats')
                ]);
                const chData = await chResp.json();
                const statsData = await statsResp.json();
                if (chData.channels) {
                    channelCount = chData.channels.length;
                    document.getElementById('stat-channels').textContent = channelCount;
                }
                if (statsData.videos_count !== undefined) {
                    // null means no automatic check has run yet, which is not
                    // the same as "nothing new" — show a dash rather than 0.
                    const pending = statsData.new_available;
                    document.getElementById('stat-videos').textContent =
                        (pending === null || pending === undefined) ? '--' : pending;
                }
                if (statsData.downloads_count !== undefined) {
                    downloadsCount = statsData.downloads_count;
                    document.getElementById('stat-downloads').textContent = downloadsCount;
                }
                if (statsData.disk_usage !== undefined) {
                    document.getElementById('stat-disk').textContent =
                        formatBytes(statsData.disk_usage);
                }
            } catch (e) {
                console.error('Failed to load stats:', e);
            }
            loadGettingStarted(channelCount, downloadsCount);
        }

        // ---------- Getting Started checklist ----------
        const GETTING_STARTED_DISMISSED_KEY = 'vidshelf_getting_started_dismissed';

        async function loadGettingStarted(channelCount, downloadsCount) {
            const card = document.getElementById('getting-started-card');
            if (localStorage.getItem(GETTING_STARTED_DISMISSED_KEY) === 'true') {
                card.style.display = 'none';
                return;
            }

            let plexConnected = false;
            try {
                const plexResp = await fetch('/api/plex/config');
                const plexData = await plexResp.json();
                // token_set, not token: the server no longer sends the
                // credential itself, only whether one exists.
                plexConnected = !!plexData.token_set;
            } catch (e) { /* leave plexConnected false if this fails */ }

            const hasChannel = channelCount > 0;
            const hasDownload = downloadsCount > 0;

            // Once the two non-optional steps are done, there's nothing left
            // this card can usefully remind someone about - auto-hide rather
            // than making them dismiss it themselves. Plex stays optional by
            // design (see README) so it doesn't gate auto-hiding.
            if (hasChannel && hasDownload) {
                card.style.display = 'none';
                return;
            }

            card.style.display = 'block';
            setGettingStartedStep('gs-step-channel', hasChannel);
            setGettingStartedStep('gs-step-download', hasDownload);
            setGettingStartedStep('gs-step-plex', plexConnected);
        }

        function setGettingStartedStep(elId, done) {
            const el = document.getElementById(elId);
            const icon = el.querySelector('.gs-step-icon');
            icon.textContent = done ? '✅' : '⬜';
            el.style.opacity = done ? '0.55' : '1';
            el.style.textDecoration = done ? 'line-through' : 'none';
        }

        function dismissGettingStarted() {
            localStorage.setItem(GETTING_STARTED_DISMISSED_KEY, 'true');
            document.getElementById('getting-started-card').style.display = 'none';
        }

        // ---------- Channels ----------
        // A fetch that cannot hang forever.
        //
        // Several endpoints depend on things outside this app's control: a
        // YouTube lookup per channel, or a directory walk across a network
        // mount. Without a deadline a stall renders as a spinner that never
        // resolves, which is indistinguishable from a broken page — and that
        // was reported as a bug more than once before this existed.
        const FETCH_TIMEOUT_MS = 25000;

        async function fetchWithTimeout(url, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            try {
                return await fetch(url, Object.assign({}, options, { signal: controller.signal }));
            } finally {
                // Always clear it, or a fast response leaves a pending timer
                // that fires an abort on an already-settled request.
                clearTimeout(timer);
            }
        }

        // AbortError is what a timeout surfaces as; say so rather than showing
        // the user a bare "aborted".
        function describeFetchError(e, timeoutMs = FETCH_TIMEOUT_MS) {
            if (e && e.name === 'AbortError') {
                return `Timed out after ${Math.round(timeoutMs / 1000)}s`;
            }
            return (e && e.message) ? e.message : 'Request failed';
        }

        async function loadChannels() {
            const container = document.getElementById('channels-list');
            try {
                const resp = await fetchWithTimeout('/api/channels');
                const data = await resp.json();
                if (data.error) {
                    container.innerHTML = '<div class="empty-state"><p>Error loading channels: ' + escapeHtml(data.error) + '</p></div>';
                    return;
                }
                if (!data.channels || data.channels.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-icon">📺</div><p>No channels configured. Add channels in config.json.</p></div>';
                    document.getElementById('stat-channels').textContent = '0';
                    return;
                }
                document.getElementById('stat-channels').textContent = data.channels.length;
                let html = '';
                const modeLabels = {'manual': 'Manual', 'new': 'New Only', 'all': 'All Videos'};
                const modeColors = {'manual': '#606070', 'new': '#7ddf90', 'all': '#e94560'};
                data.channels.forEach((ch, i) => {
                    const mode = ch.download_mode || 'manual';
                    const modeLabel = modeLabels[mode] || 'Manual';
                    const modeColor = modeColors[mode] || '#606070';
                    const displayName = ch.display_name || 'Channel ' + (i + 1);
                    html += `
                        <div class="video-card" style="padding:16px;">
                            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px;">
                                <div style="font-weight:600;color:#fff;">📺 ${displayName}</div>
                                <button class="btn btn-sm" style="background:rgba(220,53,69,0.15);color:#f8a5b0;border:1px solid rgba(220,53,69,0.3);" onclick="confirmRemoveChannel('${encodeURIComponent(ch.url)}')" title="Remove channel">✕</button>
                            </div>
                            <div style="font-size:0.85em;color:#8888a0;word-break:break-all;margin-bottom:4px;">${escapeHtml(ch.url)}</div>
                            <div style="font-size:0.8em;color:#606070;">Download: ${escapeHtml(ch.download_path)}</div>
                            <div style="font-size:0.8em;color:#606070;">Plex: ${escapeHtml(ch.plex_media_path)}</div>
                            <div style="margin-top:8px;display:flex;align-items:center;gap:10px;">
                                <span style="font-size:0.78em;color:#9090a0;">Mode:</span>
                                <select onchange="changeChannelMode('${encodeURIComponent(ch.url)}', this.value)" style="padding:4px 8px;background:#0f0f1a;border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;font-size:0.8em;">
                                    <option value="manual" ${mode === 'manual' ? 'selected' : ''}>🖐 Manual</option>
                                    <option value="new" ${mode === 'new' ? 'selected' : ''}>🆕 New Only</option>
                                    <option value="all" ${mode === 'all' ? 'selected' : ''}>📥 All Videos</option>
                                </select>
                                <span style="font-size:0.72em;padding:2px 8px;border-radius:4px;background:${modeColor}22;color:${modeColor};border:1px solid ${modeColor}44;">${modeLabel}</span>
                                <span style="font-size:0.78em;color:#9090a0;margin-left:6px;">Max:</span>
                                <select onchange="changeChannelQuality('${encodeURIComponent(ch.url)}', this.value)" style="padding:4px 8px;background:#0f0f1a;border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;font-size:0.8em;" title="Cap the resolution downloaded from this channel">
                                    <option value="0" ${!ch.max_height ? 'selected' : ''}>Best available</option>
                                    <option value="2160" ${ch.max_height == 2160 ? 'selected' : ''}>4K (2160p)</option>
                                    <option value="1440" ${ch.max_height == 1440 ? 'selected' : ''}>1440p</option>
                                    <option value="1080" ${ch.max_height == 1080 ? 'selected' : ''}>1080p</option>
                                    <option value="720" ${ch.max_height == 720 ? 'selected' : ''}>720p</option>
                                </select>
                            </div>
                            <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">
                                <button class="btn btn-primary btn-sm" onclick="loadChannelVideos('${encodeURIComponent(ch.url)}')">📋 List Videos</button>
                                <button class="btn btn-sm" style="background:rgba(40,167,69,0.12);color:#7ddf90;border:1px solid rgba(40,167,69,0.25);" onclick="downloadAllChannelVideos('${encodeURIComponent(ch.url)}')">⬇ Download All</button>
                            </div>
                        </div>`;
                });
                container.innerHTML = html;
            } catch (e) {
                // Offer a way out rather than leaving a dead panel. Channel
                // names come from a per-channel YouTube lookup, so a slow or
                // rate-limited response is the common cause here.
                container.innerHTML =
                    '<div class="empty-state"><div class="empty-icon">⚠️</div>'
                    + '<p>Couldn\'t load channels — ' + escapeHtml(describeFetchError(e)) + '.</p>'
                    + '<button class="btn btn-secondary" onclick="loadChannels()">Retry</button></div>';
            }
        }

        async function changeChannelQuality(url, value) {
            // Encoded at the call site — see the comment there on why
            // escapeHtml is not the right tool inside an inline handler.
            try { url = decodeURIComponent(url); } catch (e) { /* leave as-is */ }
            try {
                const resp = await fetch('/api/channels/quality', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url, max_height: parseInt(value, 10) || 0 })
                });
                const data = await resp.json();
                if (data.error) { showToast('❌ ' + data.error, 'error'); return; }
                showToast('✅ ' + data.message, 'success');
            } catch (e) {
                showToast('❌ Could not save quality setting', 'error');
            }
        }

        async function loadChannelVideos(channelUrl) {
            // Encoded at the call site — see the comment there on why
            // escapeHtml is not the right tool inside an inline handler.
            try { channelUrl = decodeURIComponent(channelUrl); } catch (e) { /* leave as-is */ }
            const section = document.getElementById('videos-section');
            const grid = document.getElementById('videos-grid');
            section.style.display = 'block';
            grid.innerHTML = '<div class="loading"><div class="spinner"></div><p>Fetching videos from channel...</p></div>';

            if (!channelUrl) {
                try {
                    const resp = await fetch('/api/channels');
                    const data = await resp.json();
                    if (data.channels && data.channels.length > 0) {
                        channelUrl = data.channels[0].url;
                    } else {
                        grid.innerHTML = '<div class="empty-state"><p>No channels configured.</p></div>';
                        return;
                    }
                } catch (e) {
                    grid.innerHTML = '<div class="empty-state"><p>Error: ' + escapeHtml(e.message) + '</p></div>';
                    return;
                }
            }

            try {
                const resp = await fetch('/api/channel/videos?url=' + encodeURIComponent(channelUrl));
                const data = await resp.json();
                if (data.error) {
                    grid.innerHTML = '<div class="empty-state"><p>Error: ' + escapeHtml(data.error) + '</p></div>';
                    return;
                }
                if (!data.videos || data.videos.length === 0) {
                    grid.innerHTML = '<div class="empty-state"><div class="empty-icon">🎬</div><p>No videos found on this channel.</p></div>';
                    return;
                }
                document.getElementById('stat-videos').textContent = data.videos.length;
                let html = '';
                data.videos.forEach(v => {
                    const thumb = v.thumbnail || '';
                    const title = v.title || v.id;
                    const duration = v.duration ? Math.floor(v.duration / 60) + 'm ' + (v.duration % 60) + 's' : '--';
                    const views = v.view_count ? Number(v.view_count).toLocaleString() + ' views' : '--';
                    html += `
                        <div class="video-card">
                            ${thumb ? `<img class="video-thumb" src="${thumb}" alt="${escapeHtml(title)}" onerror="this.style.display='none'">` : '<div class="video-thumb" style="display:flex;align-items:center;justify-content:center;font-size:2em;">🎬</div>'}
                            <div class="video-info">
                                <div class="video-title">${escapeHtml(title)}</div>
                                <div class="video-meta">
                                    <span>⏱ ${duration}</span>
                                    <span>👁 ${views}</span>
                                </div>
                                <div class="video-actions">
                                    <button class="btn btn-primary btn-sm" data-video-id="${escapeHtml(v.id)}" data-channel-url="${escapeHtml(channelUrl)}" onclick="downloadVideo(this)">⬇ Download</button>
                                </div>
                            </div>
                        </div>`;
                });
                grid.innerHTML = html;
            } catch (e) {
                grid.innerHTML = '<div class="empty-state"><p>Failed to fetch videos: ' + escapeHtml(e.message) + '</p></div>';
            }
        }

        async function downloadVideo(btn) {
            const videoId = btn.dataset.videoId;
            const channelUrl = btn.dataset.channelUrl;
            btn.disabled = true;
            try {
                const resp = await fetch('/api/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ video_id: videoId, channel_url: channelUrl })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('✅ ' + data.message, 'success');
                    loadDashboardStats();
                    loadDownloads();
                } else {
                    showToast('❌ Error: ' + data.error, 'error');
                }
            } catch (e) {
                showToast('❌ Download failed: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
            }
        }

        // ---------- Downloads Progress ----------
        let downloadPollInterval = null;

        async function loadDownloads() {
            const container = document.getElementById('downloads-list');
            try {
                const resp = await fetch('/api/downloads/progress');
                const data = await resp.json();
                if (!data.downloads || data.downloads.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-icon">📥</div><p>No downloads yet. Go to Channels to start downloading videos.</p></div>';
                    return;
                }
                let html = '<div style="display:flex;flex-direction:column;gap:12px;">';
                data.downloads.forEach(d => {
                    const pct = d.progress || 0;
                    const statusBadge = getStatusBadge(d.status);
                    const speed = d.speed ? formatSpeed(d.speed) : '';
                    const eta = d.eta ? formatETA(d.eta) : '';
                    const errorMsg = d.error ? `<div style="color:#f8a5b0;font-size:0.8em;margin-top:4px;">❌ ${escapeHtml(d.error)}</div>` : '';
                    const isActive = d.status === 'downloading' || d.status === 'queued' || d.status === 'converting';
                    
                    // Final path status badge
                    let finalStatusHtml = '';
                    if (d.status === 'completed') {
                        if (d.moved_to_final === true && d.final_file_exists === true) {
                            finalStatusHtml = `<div style="font-size:0.75em;color:#7ddf90;margin-top:4px;">✅ Moved to final destination</div>`;
                        } else if (d.moved_to_final === true && d.final_file_exists === false) {
                            finalStatusHtml = `<div style="font-size:0.75em;color:#f8a5b0;margin-top:4px;">❌ Moved but file NOT found at destination!</div>`;
                        } else if (d.moved_to_final === false) {
                            finalStatusHtml = `<div style="font-size:0.75em;color:#e9c46a;margin-top:4px;">⚠️ Not moved to final destination</div>`;
                        } else {
                            finalStatusHtml = `<div style="font-size:0.75em;color:#8888a0;margin-top:4px;">⏳ Move status unknown</div>`;
                        }
                    }
                    
                    // Final path display
                    let finalPathHtml = '';
                    if (d.final_path && d.filename) {
                        const finalPath = d.final_path + '\\' + d.filename;
                        finalPathHtml = `<div style="font-size:0.72em;color:#505060;margin-top:2px;word-break:break-all;">📁 ${finalPath}</div>`;
                    }
                    
                    html += `
                        <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:8px;padding:14px 16px;">
                            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">
                                <div style="font-weight:500;color:#fff;font-size:0.9em;flex:1;word-break:break-word;margin-right:12px;">${escapeHtml(d.title || d.video_id)}</div>
                                ${statusBadge}
                            </div>
                            ${isActive ? `
                            <div style="margin-top:8px;">
                                <div style="display:flex;justify-content:space-between;font-size:0.75em;color:#8888a0;margin-bottom:4px;">
                                    <span>${pct.toFixed(1)}%</span>
                                    <span>${speed} ${eta}</span>
                                </div>
                                <div style="width:100%;height:6px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;">
                                    <div style="height:100%;width:${pct}%;background:${d.status === 'error' ? '#dc3545' : '#e94560'};border-radius:3px;transition:width 0.5s ease;"></div>
                                </div>
                            </div>
                            ` : ''}
                            ${d.status === 'error' ? errorMsg : ''}
                            ${d.filename ? `<div style="font-size:0.75em;color:#606070;margin-top:4px;">📄 ${escapeHtml(d.filename)}</div>` : ''}
                            ${finalStatusHtml}
                            ${finalPathHtml}
                            <div style="font-size:0.72em;color:#505060;margin-top:4px;">${escapeHtml(d.video_id)} • ${formatTime(d.started_at)}</div>
                            ${downloadActions(d)}
                        </div>`;
                });
                html += '</div>';
                container.innerHTML = html;
            } catch (e) {
                container.innerHTML = '<div class="empty-state"><p>Failed to load downloads: ' + escapeHtml(e.message) + '</p></div>';
            }
        }

        // Cancel while it's in flight, retry once it isn't. Deliberately no
        // action on a completed download — re-downloading something you already
        // have is what the channel pages are for.
        function downloadActions(d) {
            const btn = (label, fn, danger) =>
                `<button class="btn btn-sm" style="margin-top:8px;${danger
                    ? 'background:rgba(220,53,69,0.12);color:#f8a5b0;border:1px solid rgba(220,53,69,0.28);'
                    : 'background:rgba(255,255,255,0.06);color:#9090a0;border:1px solid rgba(255,255,255,0.12);'}"`
                + ` onclick="${fn}('${encodeURIComponent(d.download_id)}')">${label}</button>`;
            if (['queued', 'downloading', 'converting'].includes(d.status)) {
                return btn('⃠ Cancel', 'cancelDownload', true);
            }
            if (['error', 'cancelled'].includes(d.status)) {
                return btn('↻ Retry', 'retryDownload', false);
            }
            return '';
        }

        async function cancelDownload(id) {
            // Encoded at the call site (downloadActions) because it lands
            // inside an inline onclick JS string, where escapeHtml cannot help.
            try { id = decodeURIComponent(id); } catch (e) { /* leave as-is */ }
            try {
                const resp = await fetchWithTimeout(`/api/downloads/${encodeURIComponent(id)}/cancel`,
                                                    { method: 'POST' }, 15000);
                const data = await resp.json();
                showToast(data.success ? '⃠ ' + data.detail : '❌ ' + (data.detail || data.error),
                          data.success ? 'success' : 'error');
                loadDownloads();
            } catch (e) {
                showToast('❌ ' + describeFetchError(e), 'error');
            }
        }

        async function retryDownload(id) {
            // Encoded at the call site (downloadActions) because it lands
            // inside an inline onclick JS string, where escapeHtml cannot help.
            try { id = decodeURIComponent(id); } catch (e) { /* leave as-is */ }
            try {
                const resp = await fetchWithTimeout(`/api/downloads/${encodeURIComponent(id)}/retry`,
                                                    { method: 'POST' }, 15000);
                const data = await resp.json();
                showToast(data.success ? '↻ ' + data.message : '❌ ' + (data.error || 'Retry failed'),
                          data.success ? 'success' : 'error');
                loadDownloads();
            } catch (e) {
                showToast('❌ ' + describeFetchError(e), 'error');
            }
        }

        function getStatusBadge(status) {
            const badges = {
                'queued': '<span style="font-size:0.72em;padding:3px 10px;border-radius:4px;background:rgba(23,162,184,0.15);color:#8dd7e5;border:1px solid rgba(23,162,184,0.25);white-space:nowrap;">⏳ Queued</span>',
                'downloading': '<span style="font-size:0.72em;padding:3px 10px;border-radius:4px;background:rgba(233,69,96,0.15);color:#e94560;border:1px solid rgba(233,69,96,0.25);white-space:nowrap;">⬇ Downloading</span>',
                'converting': '<span style="font-size:0.72em;padding:3px 10px;border-radius:4px;background:rgba(233,196,106,0.15);color:#e9c46a;border:1px solid rgba(233,196,106,0.25);white-space:nowrap;">⚙️ Converting</span>',
                'completed': '<span style="font-size:0.72em;padding:3px 10px;border-radius:4px;background:rgba(40,167,69,0.15);color:#7ddf90;border:1px solid rgba(40,167,69,0.25);white-space:nowrap;">✅ Completed</span>',
                'error': '<span style="font-size:0.72em;padding:3px 10px;border-radius:4px;background:rgba(220,53,69,0.15);color:#f8a5b0;border:1px solid rgba(220,53,69,0.25);white-space:nowrap;">❌ Error</span>',
                'cancelled': '<span style="font-size:0.72em;padding:3px 10px;border-radius:4px;background:rgba(255,255,255,0.06);color:#9090a0;border:1px solid rgba(255,255,255,0.12);white-space:nowrap;">⃠ Cancelled</span>'
            };
            return badges[status] || `<span style="font-size:0.72em;padding:3px 10px;border-radius:4px;background:rgba(255,255,255,0.06);color:#9090a0;white-space:nowrap;">${status}</span>`;
        }

        function formatSpeed(bytesPerSec) {
            if (!bytesPerSec) return '';
            if (bytesPerSec < 1024) return bytesPerSec.toFixed(0) + ' B/s';
            if (bytesPerSec < 1024 * 1024) return (bytesPerSec / 1024).toFixed(1) + ' KB/s';
            return (bytesPerSec / (1024 * 1024)).toFixed(1) + ' MB/s';
        }

        function formatETA(seconds) {
            if (!seconds || seconds <= 0) return '';
            if (seconds < 60) return '• ' + Math.round(seconds) + 's remaining';
            if (seconds < 3600) return '• ' + Math.floor(seconds / 60) + 'm ' + Math.round(seconds % 60) + 's remaining';
            return '• ' + Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm remaining';
        }

        function formatTime(unixTs) {
            if (!unixTs) return '';
            const d = new Date(unixTs * 1000);
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }

        // ---------- Music Videos ----------
        let musicVideoSearchArtist = '';

        let musicVideoSearchPage = 1;

        function renderMusicVideoCard(v) {
            const thumb = v.thumbnail || '';
            const title = v.title || v.id;
            const duration = v.duration ? Math.floor(v.duration / 60) + 'm ' + (v.duration % 60) + 's' : '--';
            const views = v.view_count ? Number(v.view_count).toLocaleString() + ' views' : '--';
            const score = v.score || 0;
            const channel = v.channel || 'Unknown';

            let scoreColor = '#606070';
            if (score >= 70) scoreColor = '#7ddf90';
            else if (score >= 50) scoreColor = '#e9c46a';
            else if (score >= 30) scoreColor = '#e9a06a';

            const quality = v.best_quality || 'unknown';
            let qualityBadge = '';
            if (quality !== 'unknown') {
                const qualityColors = {'4K': '#e94560', '1440p': '#e97a45', '1080p': '#45aae9', '720p': '#45e9c4'};
                const qColor = qualityColors[quality] || '#8888a0';
                qualityBadge = `<span style="font-size:0.7em;padding:2px 8px;border-radius:4px;background:${qColor}22;color:${qColor};border:1px solid ${qColor}44;font-weight:600;">${quality}</span>`;
            }

            return `
                <div class="video-card">
                    ${thumb ? `<img class="video-thumb" src="${thumb}" alt="${escapeHtml(title)}" onerror="this.style.display='none'">` : '<div class="video-thumb" style="display:flex;align-items:center;justify-content:center;font-size:2em;">🎬</div>'}
                    <div class="video-info">
                        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:4px;">
                            <div class="video-title" style="flex:1;">${escapeHtml(title)}</div>
                            <div style="display:flex;flex-direction:column;align-items:center;gap:2px;flex-shrink:0;">
                                <span style="font-size:0.75em;font-weight:700;color:${scoreColor};">${score}</span>
                                <span style="font-size:0.6em;color:#606070;">score</span>
                            </div>
                        </div>
                        <div class="video-meta">
                            <span>📺 ${escapeHtml(channel)}</span>
                        </div>
                        <div class="video-meta">
                            <span>⏱ ${duration}</span>
                            <span>👁 ${views}</span>
                            ${qualityBadge ? '<span>' + qualityBadge + '</span>' : ''}
                        </div>
                        <div class="video-actions">
                            <button class="btn btn-primary btn-sm" data-video-id="${escapeHtml(v.id)}" data-title="${escapeHtml(title)}" onclick="downloadMusicVideo(this)">⬇ Download</button>
                        </div>
                    </div>
                </div>`;
        }

        function removeMusicVideoLoadMoreButton() {
            const existing = document.getElementById('music-video-load-more');
            if (existing) existing.remove();
        }

        function addMusicVideoLoadMoreButton(grid) {
            removeMusicVideoLoadMoreButton();
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-secondary';
            btn.id = 'music-video-load-more';
            btn.textContent = 'Load More Results';
            btn.style.gridColumn = '1 / -1';
            btn.style.justifySelf = 'center';
            btn.style.marginTop = '12px';
            btn.addEventListener('click', () => fetchMusicVideoPage(/*append=*/true));
            grid.appendChild(btn);
        }

        async function fetchMusicVideoPage(append) {
            const input = document.getElementById('music-video-search-input');
            const btn = document.getElementById('music-video-search-btn');
            const status = document.getElementById('music-video-search-status');
            const grid = document.getElementById('music-video-results');

            if (!append) {
                const artist = input.value.trim();
                if (!artist) {
                    showToast('❌ Please enter an artist name', 'error');
                    input.focus();
                    return;
                }
                musicVideoSearchArtist = artist;
                musicVideoSearchPage = 1;
                grid.innerHTML = '';
                status.innerHTML = '<div class="loading"><div class="spinner"></div><p>Searching for music videos by ' + escapeHtml(artist) + '...</p></div>';
            } else {
                musicVideoSearchPage += 1;
                removeMusicVideoLoadMoreButton();
                status.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading more results...</p></div>';
            }

            btn.disabled = true;
            btn.textContent = '⏳ Searching...';

            try {
                const resp = await fetch('/api/music-videos/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ artist: musicVideoSearchArtist, page: musicVideoSearchPage })
                });
                const data = await resp.json();

                if (data.error) {
                    status.innerHTML = '<div class="empty-state"><p>Error: ' + escapeHtml(data.error) + '</p></div>';
                    return;
                }

                if (!data.videos || data.videos.length === 0) {
                    if (!append) {
                        status.innerHTML = '<div class="empty-state"><div class="empty-icon">🎵</div><p>No music videos found for "' + escapeHtml(musicVideoSearchArtist) + '". Try a different search term.</p></div>';
                    }
                    return;
                }

                status.innerHTML = '<div style="color:#8888a0;font-size:0.85em;margin-bottom:16px;">Found <strong style="color:#e0e0e0;">' + data.total + '</strong> results, ranked by quality score</div>';

                data.videos.forEach(v => {
                    grid.insertAdjacentHTML('beforeend', renderMusicVideoCard(v));
                });

                if (data.has_more) addMusicVideoLoadMoreButton(grid);
            } catch (e) {
                status.innerHTML = '<div class="empty-state"><p>Failed to search: ' + escapeHtml(e.message) + '</p></div>';
            } finally {
                btn.disabled = false;
                btn.textContent = '🔍 Search';
            }
        }

        async function searchMusicVideos() {
            await fetchMusicVideoPage(/*append=*/false);
        }

        async function downloadMusicVideo(btn) {
            const videoId = btn.dataset.videoId;
            const title = btn.dataset.title;
            if (!musicVideoSearchArtist) {
                showToast('❌ Please search for an artist first', 'error');
                return;
            }
            btn.disabled = true;
            try {
                const resp = await fetch('/api/music-videos/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        video_id: videoId,
                        title: title,
                        artist: musicVideoSearchArtist
                    })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('✅ ' + data.message, 'success');
                    loadDashboardStats();
                } else {
                    showToast('❌ Error: ' + data.error, 'error');
                }
            } catch (e) {
                showToast('❌ Download failed: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
            }
        }

        // Enter key triggers search
        document.getElementById('music-video-search-input').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                searchMusicVideos();
            }
        });

        // ---------- Music Video Path ----------
        async function loadMusicVideoPath() {
            const display = document.getElementById('music-video-path-display');
            const input = document.getElementById('settings-music-video-path');
            try {
                const resp = await fetch('/api/music-video-path');
                const data = await resp.json();
                const path = data.music_video_plex_path || '/app/music_videos_final';
                input.value = path;
                display.textContent = path;
                renderMusicPathConflict(data.conflict);
            } catch (e) {
                display.textContent = 'Error loading';
            }
        }

        // v1.8.0 folded two settings for this directory into one. If the old
        // key held a real path that disagreed, say so rather than picking for
        // them — the old value may point at a second share, and silently
        // adopting or relocating a library is not a call to make for someone.
        function renderMusicPathConflict(conflict) {
            const host = document.getElementById('music-path-conflict');
            if (!host) return;
            if (!conflict) { host.style.display = 'none'; host.innerHTML = ''; return; }
            host.style.display = 'block';
            host.innerHTML =
                '<div style="margin-top:10px;padding:10px 12px;border-radius:6px;'
                + 'background:rgba(233,196,106,0.10);border:1px solid rgba(233,196,106,0.35);'
                + 'color:#e9c46a;font-size:0.85em;">'
                + '⚠ Two settings used to point at your music-video folder, and they '
                + 'disagreed. The one below is what Vidshelf has actually been using.'
                + '<div style="margin-top:6px;color:#c0c0d0;">Old, unused setting: <code>'
                + escapeHtml(conflict.was) + '</code><br>In use now: <code>'
                + escapeHtml(conflict.now) + '</code></div>'
                + '<div style="margin-top:6px;color:#9090a0;">Nothing was moved. Saving a '
                + 'path here dismisses this notice.</div></div>';
        }

        async function saveMusicVideoPath() {
            const input = document.getElementById('settings-music-video-path');
            const path = input.value.trim();
            if (!path) {
                showToast('❌ Music video Plex path cannot be empty', 'error');
                return;
            }
            try {
                const resp = await fetch('/api/music-video-path', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ music_video_plex_path: path })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('✅ ' + data.message, 'success');
                    document.getElementById('music-video-path-display').textContent = path;
                } else {
                    showToast('❌ ' + data.error, 'error');
                }
            } catch (e) {
                showToast('❌ Failed to save: ' + e.message, 'error');
            }
        }

        // ---------- Settings ----------
        async function loadConfig() {
            const container = document.getElementById('config-content');
            try {
                const resp = await fetch('/api/config');
                const data = await resp.json();
                container.innerHTML = '<pre style="background:#0f0f1a;padding:16px;border-radius:8px;overflow-x:auto;font-size:0.85em;color:#c0c0d0;">' +
                    JSON.stringify(data, null, 2) + '</pre>';
            } catch (e) {
                container.innerHTML = '<div class="empty-state"><p>Failed to load config: ' + escapeHtml(e.message) + '</p></div>';
            }
            loadSystemInfo();
        }

        async function loadSystemInfo() {
            const container = document.getElementById('system-info-content');
            try {
                const resp = await fetch('/api/system/info');
                const data = await resp.json();
                if (data.error) {
                    container.innerHTML = '<div class="empty-state"><p>Error: ' + escapeHtml(data.error) + '</p></div>';
                    return;
                }
                container.innerHTML = `
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;">
                        <div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);">
                            <div style="font-size:0.75em;color:#8888a0;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Vidshelf Version</div>
                            <div style="font-size:1.1em;font-weight:600;color:#fff;">${data.app_version}</div>
                        </div>
                        <div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);">
                            <div style="font-size:0.75em;color:#8888a0;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Python</div>
                            <div style="font-size:1.1em;font-weight:600;color:#fff;">${data.python_version}</div>
                        </div>
                        <div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);">
                            <div style="font-size:0.75em;color:#8888a0;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">yt-dlp</div>
                            <div style="font-size:1.1em;font-weight:600;color:#fff;">${data.yt_dlp_version}</div>
                        </div>
                        <div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);">
                            <div style="font-size:0.75em;color:#8888a0;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Channels</div>
                            <div style="font-size:1.1em;font-weight:600;color:#fff;">${data.channels_count}</div>
                        </div>
                        <div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);">
                            <div style="font-size:0.75em;color:#8888a0;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Total Downloads</div>
                            <div style="font-size:1.1em;font-weight:600;color:#fff;">${data.downloads_count}</div>
                        </div>
                        <div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);">
                            <div style="font-size:0.75em;color:#8888a0;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Disk Used</div>
                            <div style="font-size:1.1em;font-weight:600;color:#fff;">${formatBytes(data.disk_used)}</div>
                        </div>
                        <div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);">
                            <div style="font-size:0.75em;color:#8888a0;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Disk Free</div>
                            <div style="font-size:1.1em;font-weight:600;color:#fff;">${formatBytes(data.disk_free)}</div>
                        </div>
                        <div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);grid-column:1/-1;">
                            <div style="font-size:0.75em;color:#8888a0;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Platform</div>
                            <div style="font-size:0.9em;color:#c0c0d0;word-break:break-all;">${data.platform}</div>
                        </div>
                    </div>`;
            } catch (e) {
                container.innerHTML = '<div class="empty-state"><p>Failed to load system info: ' + escapeHtml(e.message) + '</p></div>';
            }
        }

        async function changePassword() {
            const currentPw = document.getElementById('settings-current-pw').value;
            const newPw = document.getElementById('settings-new-pw').value;
            const confirmPw = document.getElementById('settings-confirm-pw').value;

            if (!currentPw) {
                showToast('❌ Please enter your current password', 'error');
                return;
            }
            if (!newPw || newPw.length < 6) {
                showToast('❌ New password must be at least 6 characters', 'error');
                return;
            }
            if (newPw !== confirmPw) {
                showToast('❌ New passwords do not match', 'error');
                return;
            }

            try {
                const resp = await fetch('/api/password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ current_password: currentPw, new_password: newPw })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('✅ Password updated successfully', 'success');
                    document.getElementById('settings-current-pw').value = '';
                    document.getElementById('settings-new-pw').value = '';
                    document.getElementById('settings-confirm-pw').value = '';
                } else {
                    showToast('❌ ' + data.error, 'error');
                }
            } catch (e) {
                showToast('❌ Failed to update password: ' + e.message, 'error');
            }
        }

        function confirmClearDownloads() {
            showConfirmModal(
                'Clear all download history?\n\nThis will allow "New Only" mode to re-download previously skipped videos. Video files will NOT be deleted.',
                async function() {
                    try {
                        const resp = await fetch('/api/downloads/clear', { method: 'POST' });
                        const data = await resp.json();
                        if (data.success) {
                            showToast('✅ Download history and progress cleared', 'success');
                            loadDashboardStats();
                            loadDownloads();
                        } else {
                            showToast('❌ ' + data.error, 'error');
                        }
                    } catch (e) {
                        showToast('❌ Failed to clear history: ' + e.message, 'error');
                    }
                }
            );
        }

        // ---------- Plex Base Path ----------
        async function loadPlexBasePath() {
            const display = document.getElementById('plex-base-path-display');
            const input = document.getElementById('settings-plex-path');
            try {
                const resp = await fetch('/api/plex-base-path');
                const data = await resp.json();
                const path = data.plex_base_path || './downloads';
                input.value = path;
                display.textContent = path;
            } catch (e) {
                display.textContent = 'Error loading';
            }
        }

        async function savePlexBasePath() {
            const input = document.getElementById('settings-plex-path');
            const path = input.value.trim();
            if (!path) {
                showToast('❌ Plex base path cannot be empty', 'error');
                return;
            }
            try {
                const resp = await fetch('/api/plex-base-path', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ plex_base_path: path })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('✅ ' + data.message, 'success');
                    document.getElementById('plex-base-path-display').textContent = path;
                    loadSystemInfo();
                } else {
                    showToast('❌ ' + data.error, 'error');
                }
            } catch (e) {
                showToast('❌ Failed to save: ' + e.message, 'error');
            }
        }

        // ---------- Channel Management (Add/Remove) ----------
        let pendingRemoveUrl = null;

        function showAddChannelModal() {
            document.getElementById('channel-url').value = '';
            document.getElementById('channel-download-path').value = '';
            document.getElementById('channel-plex-path').value = '';

            (async function() {
                try {
                    const resp = await fetch('/api/plex-base-path');
                    const data = await resp.json();
                    const basePath = data.plex_base_path || '';
                    if (basePath) {
                        document.getElementById('channel-plex-path').value = basePath;
                    }
                } catch (_) {}
            })();

            document.getElementById('add-channel-modal').classList.add('active');
            document.getElementById('channel-url').focus();
        }

        function closeAddChannelModal() {
            document.getElementById('add-channel-modal').classList.remove('active');
        }

        async function addChannel() {
            const url = document.getElementById('channel-url').value.trim();
            const downloadPath = document.getElementById('channel-download-path').value.trim() || './downloads';
            const plexPath = document.getElementById('channel-plex-path').value.trim() || './downloads';

            if (!url) {
                showToast('❌ Please enter a channel URL', 'error');
                document.getElementById('channel-url').focus();
                return;
            }

            if (!url.match(/^https?:\/\/(www\.)?(youtube\.com|youtu\.be)\//) && !url.startsWith('@')) {
                showToast('❌ Please enter a valid YouTube URL or @handle', 'error');
                return;
            }

            let fullUrl = url;
            if (url.startsWith('@')) {
                fullUrl = 'https://www.youtube.com/' + url;
            }

            const downloadMode = document.getElementById('channel-download-mode').value;
            const btn = document.getElementById('add-channel-submit-btn');
            btn.disabled = true;
            showToast('⏳ Adding channel...', 'success');
            try {
                const resp = await fetch('/api/channels/add', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        url: fullUrl,
                        download_path: downloadPath,
                        plex_media_path: plexPath,
                        download_mode: downloadMode
                    })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('✅ ' + data.message, 'success');
                    closeAddChannelModal();
                    loadChannels();
                    loadDashboardStats();
                } else {
                    showToast('❌ ' + data.error, 'error');
                }
            } catch (e) {
                showToast('❌ Failed to add channel: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
            }
        }

        function showConfirmModal(message, onConfirm) {
            document.getElementById('confirm-message').textContent = message;
            const btn = document.getElementById('confirm-action-btn');
            const newBtn = btn.cloneNode(true);
            btn.parentNode.replaceChild(newBtn, btn);
            newBtn.addEventListener('click', function() {
                closeConfirmModal();
                if (onConfirm) onConfirm();
            });
            document.getElementById('confirm-modal-close').onclick = closeConfirmModal;
            document.getElementById('confirm-cancel-btn').onclick = closeConfirmModal;
            document.getElementById('confirm-modal').classList.add('active');
        }

        function closeConfirmModal() {
            document.getElementById('confirm-modal').classList.remove('active');
        }

        function confirmRemoveChannel(url) {
            // Encoded at the call site — see the comment there on why
            // escapeHtml is not the right tool inside an inline handler.
            try { url = decodeURIComponent(url); } catch (e) { /* leave as-is */ }
            showConfirmModal(
                'Are you sure you want to remove this channel?\n\n' + url,
                function() { removeChannel(url); }
            );
        }

        async function removeChannel(url) {
            showToast('⏳ Removing channel...', 'success');
            try {
                const resp = await fetch('/api/channels/remove', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('✅ ' + data.message, 'success');
                    loadChannels();
                    loadDashboardStats();
                    document.getElementById('videos-section').style.display = 'none';
                } else {
                    showToast('❌ ' + data.error, 'error');
                }
            } catch (e) {
                showToast('❌ Failed to remove channel: ' + e.message, 'error');
            }
        }

        async function changeChannelMode(channelUrl, newMode) {
            // Encoded at the call site — see the comment there on why
            // escapeHtml is not the right tool inside an inline handler.
            try { channelUrl = decodeURIComponent(channelUrl); } catch (e) { /* leave as-is */ }
            showToast('⏳ Updating download mode...', 'success');
            try {
                const resp = await fetch('/api/channels/mode', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: channelUrl, download_mode: newMode })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('✅ Download mode updated to ' + newMode, 'success');
                    loadChannels();
                } else {
                    showToast('❌ ' + data.error, 'error');
                }
            } catch (e) {
                showToast('❌ Failed to update mode: ' + e.message, 'error');
            }
        }

        async function downloadAllChannelVideos(channelUrl) {
            // Encoded at the call site — see the comment there on why
            // escapeHtml is not the right tool inside an inline handler.
            try { channelUrl = decodeURIComponent(channelUrl); } catch (e) { /* leave as-is */ }
            showConfirmModal(
                'Download all videos from this channel?\n\nThis may take a while for large channels.',
                async function() {
                    showToast('⏳ Starting batch download...', 'success');
                    try {
                        const resp = await fetch('/api/channels/download-all', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ url: channelUrl })
                        });
                        const data = await resp.json();
                        if (data.success) {
                            showToast('✅ ' + data.message, 'success');
                            loadChannels();
                            loadDownloads();
                        } else {
                            showToast('❌ ' + data.error, 'error');
                        }
                    } catch (e) {
                        showToast('❌ Batch download failed: ' + e.message, 'error');
                    }
                }
            );
        }

        // ---------- Folder Browser ----------
        let _folderBrowserTarget = null;

        async function openFolderBrowser(inputId) {
            _folderBrowserTarget = inputId;
            document.getElementById('folder-browser-modal').classList.add('active');
            await loadFolderBrowser('');
        }

        function closeFolderBrowser() {
            document.getElementById('folder-browser-modal').classList.remove('active');
            _folderBrowserTarget = null;
        }

        async function loadFolderBrowser(path) {
            const listEl = document.getElementById('folder-browser-list');
            const pathEl = document.getElementById('folder-browser-path');
            pathEl.textContent = path || '(Drives)';
            listEl.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading...</p></div>';
            try {
                const resp = await fetch('/api/browse-folder', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: path })
                });
                const data = await resp.json();
                if (data.error) {
                    listEl.innerHTML = `<div class="empty-state"><p>Error: ${escapeHtml(data.error)}</p></div>`;
                    return;
                }
                if (data.entries.length === 0) {
                    listEl.innerHTML = '<div class="empty-state"><p>This folder is empty.</p></div>';
                    return;
                }
                let html = '';
                if (data.parent_path) {
                    const parentEscaped = data.parent_path.replace(/\\/g, '\\\\');
                    html += `<div class="folder-item" data-path="${parentEscaped}" style="padding:10px 14px;cursor:pointer;border-radius:6px;transition:all 0.15s;display:flex;align-items:center;gap:10px;border-bottom:1px solid rgba(255,255,255,0.04);">
                        <span style="font-size:1.1em;">📂</span>
                        <span style="color:#c0c0d0;">.. (parent)</span>
                    </div>`;
                }
                data.entries.forEach(entry => {
                    const entryEscaped = entry.path.replace(/\\/g, '\\\\');
                    html += `<div class="folder-item" data-path="${entryEscaped}" style="padding:10px 14px;cursor:pointer;border-radius:6px;transition:all 0.15s;display:flex;align-items:center;gap:10px;border-bottom:1px solid rgba(255,255,255,0.04);">
                        <span style="font-size:1.1em;">📁</span>
                        <span style="flex:1;color:#e0e0e0;">${escapeHtml(entry.name)}</span>
                        <span style="font-size:0.72em;color:#606070;font-family:Consolas,monospace;">${escapeHtml(entry.path)}</span>
                    </div>`;
                });
                listEl.innerHTML = html;
                listEl.querySelectorAll('.folder-item').forEach(el => {
                    el.addEventListener('mouseenter', () => el.style.background = 'rgba(255,255,255,0.06)');
                    el.addEventListener('mouseleave', () => el.style.background = 'transparent');
                    el.addEventListener('click', function() {
                        const p = this.dataset.path;
                        loadFolderBrowser(p);
                    });
                });
            } catch (e) {
                listEl.innerHTML = `<div class="empty-state"><p>Failed: ${escapeHtml(e.message)}</p></div>`;
            }
        }

        function selectFolderBrowser() {
            const pathEl = document.getElementById('folder-browser-path');
            const currentPath = pathEl.textContent;
            if (!currentPath || currentPath === '(Drives)') {
                showToast('❌ Please navigate to a folder first', 'error');
                return;
            }
            if (_folderBrowserTarget) {
                document.getElementById(_folderBrowserTarget).value = currentPath;
                closeFolderBrowser();
                showToast('✅ Path selected: ' + currentPath, 'success');
            }
        }

        function goToFolderBrowserPath() {
            const input = document.getElementById('folder-browser-input');
            const path = input.value.trim();
            if (!path) {
                showToast('❌ Please enter a path', 'error');
                return;
            }
            loadFolderBrowser(path);
        }

        document.getElementById('folder-browser-input').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                goToFolderBrowserPath();
            }
        });

        document.getElementById('add-channel-modal').addEventListener('click', function(e) {
            if (e.target === this) closeAddChannelModal();
        });
        document.getElementById('confirm-modal').addEventListener('click', function(e) {
            if (e.target === this) closeConfirmModal();
        });
        document.getElementById('folder-browser-modal').addEventListener('click', function(e) {
            if (e.target === this) closeFolderBrowser();
        });

        document.getElementById('channel-url').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') addChannel();
        });

        // ---------- Plex OAuth Integration ----------
        let plexOAuthPollInterval = null;

        async function checkPlexConnection() {
            const statusEl = document.getElementById('plex-connection-status');
            const oauthSection = document.getElementById('plex-oauth-section');
            const connectedSection = document.getElementById('plex-connected-section');
            try {
                const resp = await fetch('/api/plex/config');
                const data = await resp.json();
                const hasToken = !!data.token_set;
                const hasServer = data.server_url && data.server_url.length > 0;
                const hasLibrary = data.music_video_library_key && data.music_video_library_key.length > 0;

                if (hasToken) {
                    // Connected - show connected section
                    statusEl.style.display = 'none';
                    oauthSection.style.display = 'none';
                    connectedSection.style.display = 'block';

                    // Fetch account info and servers
                    try {
                        const srvResp = await fetch('/api/plex/oauth/servers', { method: 'POST' });
                        const srvData = await srvResp.json();
                        if (srvData.account) {
                            document.getElementById('plex-account-name').textContent = srvData.account.username || srvData.account.email || 'Unknown';
                        }
                        if (srvData.servers && srvData.servers.length > 0) {
                            document.getElementById('plex-connected-server').textContent = srvData.servers[0].name || 'Unknown';
                        }
                    } catch (_) {}
                    document.getElementById('plex-library-key').textContent = data.music_video_library_key || 'Not set';
                } else {
                    // Not connected - show OAuth section
                    statusEl.style.display = 'none';
                    oauthSection.style.display = 'block';
                    connectedSection.style.display = 'none';
                }
            } catch (e) {
                statusEl.innerHTML = '<div class="empty-state"><p>Failed to check Plex connection: ' + escapeHtml(e.message) + '</p></div>';
            }
        }

        async function startPlexOAuth() {
            const btn = document.getElementById('plex-oauth-btn');
            const statusEl = document.getElementById('plex-oauth-status');
            btn.disabled = true;
            btn.textContent = '⏳ Connecting...';
            statusEl.innerHTML = '<div style="color:#e9c46a;">Initiating Plex OAuth...</div>';

            try {
                const resp = await fetch('/api/plex/oauth/start', { method: 'POST' });
                const data = await resp.json();

                if (data.success && data.auth_url) {
                    statusEl.innerHTML = `
                        <div style="margin-top:8px;padding:12px;background:rgba(23,162,184,0.1);border:1px solid rgba(23,162,184,0.2);border-radius:6px;">
                            <div style="color:#8dd7e5;font-weight:500;margin-bottom:8px;">🔑 Plex Authorization Required</div>
                            <p style="font-size:0.9em;color:#c0c0d0;margin-bottom:12px;">
                                Click the button below to authorize Vidshelf with your Plex account.
                                A new tab will open with Plex's secure login page.
                            </p>
                            <a href="${escapeHtml(data.auth_url)}" target="_blank" class="btn btn-primary" style="display:inline-block;padding:12px 24px;text-decoration:none;font-size:1em;">
                                🔗 Authorize with Plex
                            </a>
                            <div style="margin-top:12px;font-size:0.85em;color:#8888a0;">
                                After authorizing, click "Check Authorization" below.
                            </div>
                            <button class="btn btn-secondary" onclick="checkPlexOAuth()" style="margin-top:8px;padding:10px 20px;">
                                ✅ Check Authorization
                            </button>
                            <div id="plex-oauth-check-status" style="margin-top:8px;font-size:0.85em;color:#8888a0;"></div>
                        </div>
                    `;
                    // Open the auth URL in a new tab
                    window.open(data.auth_url, '_blank');
                } else {
                    statusEl.innerHTML = '<div style="color:#f8a5b0;">Failed to initiate Plex OAuth. Please try again.</div>';
                    btn.disabled = false;
                    btn.textContent = '🔗 Connect to Plex';
                }
            } catch (e) {
                statusEl.innerHTML = '<div style="color:#f8a5b0;">Error: ' + escapeHtml(e.message) + '</div>';
                btn.disabled = false;
                btn.textContent = '🔗 Connect to Plex';
            }
        }

        async function checkPlexOAuth() {
            const statusEl = document.getElementById('plex-oauth-check-status');
            statusEl.innerHTML = '<div style="color:#e9c46a;">⏳ Checking authorization...</div>';

            try {
                const resp = await fetch('/api/plex/oauth/check', { method: 'POST' });
                const data = await resp.json();

                if (data.status === 'success') {
                    statusEl.innerHTML = '<div style="color:#7ddf90;">✅ Plex authorized successfully!</div>';
                    // Show server selection
                    if (data.servers && data.servers.length > 0) {
                        showPlexServerSelection(data.servers, data.account);
                    } else {
                        document.getElementById('plex-server-section').style.display = 'block';
                        document.getElementById('plex-server-list').innerHTML = '<div style="color:#e9c46a;">No Plex servers found on your account.</div>';
                    }
                    // Refresh connection status
                    setTimeout(checkPlexConnection, 1000);
                } else if (data.status === 'pending') {
                    statusEl.innerHTML = '<div style="color:#e9c46a;">⏳ Not yet authorized. Please complete authorization in the Plex tab, then click "Check Authorization" again.</div>';
                } else if (data.status === 'failed') {
                    statusEl.innerHTML = '<div style="color:#f8a5b0;">❌ Authorization failed or expired. Please click "Connect to Plex" to try again.</div>';
                    document.getElementById('plex-oauth-btn').disabled = false;
                    document.getElementById('plex-oauth-btn').textContent = '🔗 Connect to Plex';
                }
            } catch (e) {
                statusEl.innerHTML = '<div style="color:#f8a5b0;">Error: ' + escapeHtml(e.message) + '</div>';
            }
        }

        function showPlexServerSelection(servers, account) {
            const serverSection = document.getElementById('plex-server-section');
            const serverList = document.getElementById('plex-server-list');
            serverSection.style.display = 'block';

            if (account) {
                document.getElementById('plex-account-name').textContent = account.username || account.email || 'Unknown';
            }

            if (!servers || servers.length === 0) {
                serverList.innerHTML = '<div style="color:#e9c46a;">No Plex servers found on your account.</div>';
                return;
            }

            let html = '<div style="display:flex;flex-direction:column;gap:8px;">';
            servers.forEach(srv => {
                const lastSeen = srv.lastSeenAt ? new Date(srv.lastSeenAt * 1000).toLocaleDateString() : 'Unknown';
                html += `
                    <div style="padding:12px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:6px;cursor:pointer;transition:all 0.15s;"
                         onmouseenter="this.style.background='rgba(255,255,255,0.08)'"
                         onmouseleave="this.style.background='rgba(255,255,255,0.03)'"
                         onclick="selectPlexServer('${encodeURIComponent(srv.uri || '')}', '${encodeURIComponent(srv.name || '')}')">
                        <div style="display:flex;align-items:center;gap:10px;">
                            <span style="font-size:1.2em;">🖥</span>
                            <div>
                                <div style="font-weight:600;color:#fff;">${escapeHtml(srv.name)}</div>
                                <div style="font-size:0.8em;color:#8888a0;">${escapeHtml(srv.uri || 'No URI')} • Last seen: ${lastSeen}</div>
                            </div>
                        </div>
                    </div>`;
            });
            html += '</div>';
            serverList.innerHTML = html;
        }

        async function selectPlexServer(uri, name) {
            // Encoded at the call site because both land inside an inline
            // onclick JS string, where escapeHtml cannot help.
            try { uri = decodeURIComponent(uri); } catch (e) { /* leave as-is */ }
            try { name = decodeURIComponent(name); } catch (e) { /* leave as-is */ }
            if (!uri) {
                showToast('❌ This server has no accessible URI', 'error');
                return;
            }

            // Save the server URL
            try {
                const resp = await fetch('/api/plex/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ server_url: uri })
                });
                const data = await resp.json();
                if (data.success) {
                    document.getElementById('plex-server-selected').style.display = 'block';
                    document.getElementById('plex-server-name').textContent = name;
                    document.getElementById('plex-server-url').textContent = uri;
                    showToast('✅ Connected to ' + name, 'success');
                    checkPlexConnection();
                } else {
                    showToast('❌ Failed to save server: ' + data.error, 'error');
                }
            } catch (e) {
                showToast('❌ Error: ' + e.message, 'error');
            }
        }

async function findPlexLibraries() {
            const btn = document.getElementById('plex-discover-btn');
            const statusEl = document.getElementById('plex-library-status');
            const picker = document.getElementById('plex-library-picker');
            btn.disabled = true;
            btn.textContent = '⏳ Searching...';
            statusEl.innerHTML = '<div style="color:#e9c46a;">⏳ Listing Plex libraries...</div>';
            picker.style.display = 'none';

            try {
                const resp = await fetch('/api/plex/libraries');
                const data = await resp.json();
                const libraries = data.libraries || [];

                if (libraries.length === 0) {
                    statusEl.innerHTML = '<div style="color:#f8a5b0;">❌ No libraries found. Check server_url and token.</div>';
                    return;
                }

                const select = document.getElementById('plex-library-select');
                select.innerHTML = '';
                let autoTitle = null;
                libraries.forEach(lib => {
                    const opt = document.createElement('option');
                    opt.value = lib.key;
                    opt.textContent = `${lib.title} (${lib.type}, key=${lib.key})${lib.is_auto_discovered ? ' — suggested' : ''}`;
                    if (lib.is_auto_discovered) {
                        opt.selected = true;
                        autoTitle = lib.title;
                    }
                    select.appendChild(opt);
                });

                document.getElementById('plex-library-auto-note').textContent = autoTitle
                    ? `Auto-detected "${autoTitle}" from its name — double-check this is actually your music-video library before saving.`
                    : 'Could not auto-detect which library is for music videos — pick the right one manually.';

                picker.style.display = 'block';
                statusEl.innerHTML = `<div style="color:#8888a0;">Found ${libraries.length} librar${libraries.length === 1 ? 'y' : 'ies'} — confirm the right one below, then save.</div>`;
            } catch (e) {
                statusEl.innerHTML = `<div style="color:#f8a5b0;">Error: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
                btn.textContent = '🔍 Find Libraries';
            }
        }

        async function savePlexLibrary() {
            const btn = document.getElementById('plex-save-library-btn');
            const statusEl = document.getElementById('plex-library-status');
            const select = document.getElementById('plex-library-select');
            const libraryKey = select.value;
            const libraryTitle = select.options[select.selectedIndex].textContent;
            if (!libraryKey) return;

            btn.disabled = true;
            btn.textContent = '⏳ Saving...';
            try {
                const resp = await fetch('/api/plex/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ music_video_library_key: libraryKey })
                });
                const data = await resp.json();
                if (data.success) {
                    statusEl.innerHTML = `<div style="color:#7ddf90;">✅ Saved: ${escapeHtml(libraryTitle)}</div>`;
                    document.getElementById('plex-library-key').textContent = libraryKey;
                    showToast('✅ Library saved', 'success');
                    checkPlexConnection();
                } else {
                    statusEl.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(data.error)}</div>`;
                }
            } catch (e) {
                statusEl.innerHTML = `<div style="color:#f8a5b0;">Error: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
                btn.textContent = '💾 Save Selected Library';
            }
        }

        async function syncPlexCollections() {
            const btn = document.getElementById('plex-sync-btn');
            const resultEl = document.getElementById('plex-collections-result');
            btn.disabled = true;
            btn.textContent = '⏳ Syncing...';
            resultEl.innerHTML = '<div style="color:#e9c46a;">⏳ Syncing Plex collections...</div>';

            try {
                const resp = await fetch('/api/plex/collections/sync', { method: 'POST' });
                const data = await resp.json();

                if (data.results) {
                    let html = '<div style="margin-top:12px;">';
                    data.results.forEach(r => {
                        const status = r.collection_created ? '✅' : '❌';
                        const errors = r.errors && r.errors.length > 0 ? ` (${escapeHtml(r.errors.join(', '))})` : '';
                        html += `<div style="font-size:0.85em;color:#c0c0d0;margin-bottom:4px;">${status} ${escapeHtml(r.artist)}: ${r.videos_found} videos${errors}</div>`;
                    });
                    html += '</div>';
                    resultEl.innerHTML = html;
                    showToast(`✅ Synced ${data.results.length} collections`, 'success');
                } else if (data.result) {
                    const r = data.result;
                    const status = r.collection_created ? '✅' : '❌';
                    const errors = r.errors && r.errors.length > 0 ? ` (${escapeHtml(r.errors.join(', '))})` : '';
                    resultEl.innerHTML = `<div style="font-size:0.85em;color:#c0c0d0;">${status} ${escapeHtml(r.artist)}: ${r.videos_found} videos${errors}</div>`;
                    showToast(`✅ Synced collection for ${r.artist}`, 'success');
                } else {
                    resultEl.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(data.error || 'Sync failed')}</div>`;
                }
            } catch (e) {
                resultEl.innerHTML = `<div style="color:#f8a5b0;">Error: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
                btn.textContent = '🔄 Sync Collections';
            }
        }

        async function checkPlexCollectionsStatus() {
            const resultEl = document.getElementById('plex-collections-result');
            resultEl.innerHTML = '<div style="color:#e9c46a;">⏳ Checking collection status...</div>';

            try {
                const resp = await fetch('/api/plex/collections/status');
                const data = await resp.json();

                if (data.error) {
                    resultEl.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(data.error)}</div>`;
                    return;
                }

                let html = `<div style="margin-top:12px;padding:12px;background:rgba(255,255,255,0.03);border-radius:8px;">
                    <div style="font-size:0.85em;color:#8888a0;margin-bottom:8px;">
                        Library Key: <span style="color:#c0c0d0;">${data.library_key}</span> |
                        Collections: <span style="color:#c0c0d0;">${data.total_collections}</span> |
                        Artists with collection: <span style="color:#7ddf90;">${data.with_collection}</span> |
                        Without: <span style="color:#e9c46a;">${data.without_collection}</span>
                    </div>`;

                if (data.folders) {
                    data.folders.forEach(f => {
                        const icon = f.has_collection ? '✅' : '⬜';
                        html += `<div style="font-size:0.85em;color:#c0c0d0;margin-bottom:2px;">${icon} ${escapeHtml(f.artist)}</div>`;
                    });
                }
                html += '</div>';
                resultEl.innerHTML = html;
            } catch (e) {
                resultEl.innerHTML = `<div style="color:#f8a5b0;">Error: ${escapeHtml(e.message)}</div>`;
            }
        }

        async function cleanUpPlexTitles() {
            const btn = document.getElementById('plex-clean-titles-btn');
            const resultEl = document.getElementById('plex-collections-result');
            btn.disabled = true;
            btn.textContent = '⏳ Cleaning...';
            resultEl.innerHTML = '<div style="color:#e9c46a;">⏳ Cleaning up video titles...</div>';

            try {
                const resp = await fetch('/api/plex/titles/clean', { method: 'POST' });
                const data = await resp.json();

                if (data.errors && data.errors.length > 0 && data.scanned === 0) {
                    resultEl.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(data.errors.join(', '))}</div>`;
                    return;
                }

                let html = `<div style="margin-top:12px;padding:12px;background:rgba(255,255,255,0.03);border-radius:8px;">
                    <div style="font-size:0.85em;color:#8888a0;margin-bottom:8px;">
                        Scanned: <span style="color:#c0c0d0;">${data.scanned}</span> |
                        Cleaned: <span style="color:#7ddf90;">${data.cleaned}</span>
                    </div>`;
                if (data.examples && data.examples.length > 0) {
                    data.examples.forEach(ex => {
                        html += `<div style="font-size:0.8em;color:#c0c0d0;margin-bottom:6px;">
                            <div style="color:#8888a0;text-decoration:line-through;">${ex.before}</div>
                            <div style="color:#7ddf90;">→ ${ex.after}</div>
                        </div>`;
                    });
                }
                html += '</div>';
                resultEl.innerHTML = html;
                showToast(`✅ Cleaned ${data.cleaned} of ${data.scanned} titles`, 'success');
            } catch (e) {
                resultEl.innerHTML = `<div style="color:#f8a5b0;">Error: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
                btn.textContent = '🧹 Clean Up Titles';
            }
        }

        async function generatePlexTitleCards() {
            const btn = document.getElementById('plex-title-cards-btn');
            const resultEl = document.getElementById('plex-collections-result');
            btn.disabled = true;
            btn.textContent = '⏳ Generating...';
            resultEl.innerHTML = '<div style="color:#e9c46a;">⏳ Generating title-card posters (this can take a bit for a large library)...</div>';

            try {
                const resp = await fetch('/api/plex/title-cards/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({})
                });
                const data = await resp.json();

                if (data.error) {
                    resultEl.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(data.error)}</div>`;
                    return;
                }

                let html = `<div style="margin-top:12px;padding:12px;background:rgba(255,255,255,0.03);border-radius:8px;">
                    <div style="font-size:0.85em;color:#8888a0;margin-bottom:8px;">
                        Artists scanned: <span style="color:#c0c0d0;">${data.total_artists}</span> |
                        Title cards generated: <span style="color:#7ddf90;">${data.total_generated}</span>
                    </div>`;
                if (data.errors && data.errors.length > 0) {
                    html += `<div style="font-size:0.8em;color:#f8a5b0;">${escapeHtml(data.errors.slice(0, 5).join(', '))}</div>`;
                }
                html += '</div>';
                resultEl.innerHTML = html;
                showToast(`✅ Generated ${data.total_generated} title card(s)`, 'success');
            } catch (e) {
                resultEl.innerHTML = `<div style="color:#f8a5b0;">Error: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
                btn.textContent = '🖼️ Generate Title Cards';
            }
        }

        async function dedupePlexCollections() {
            const btn = document.getElementById('plex-dedupe-btn');
            const resultEl = document.getElementById('plex-collections-result');
            btn.disabled = true;
            btn.textContent = '⏳ Checking...';
            resultEl.innerHTML = '<div style="color:#e9c46a;">⏳ Looking for duplicate collections...</div>';

            try {
                const resp = await fetch('/api/plex/collections/dedupe', { method: 'POST' });
                const data = await resp.json();

                if (data.error) {
                    resultEl.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(data.error)}</div>`;
                    return;
                }

                let html = `<div style="margin-top:12px;padding:12px;background:rgba(255,255,255,0.03);border-radius:8px;">
                    <div style="font-size:0.85em;color:#8888a0;margin-bottom:8px;">
                        Duplicate groups found: <span style="color:#c0c0d0;">${data.groups_found}</span> |
                        Removed: <span style="color:#7ddf90;">${data.deleted.length}</span>
                    </div>`;
                if (data.deleted.length > 0) {
                    data.deleted.forEach(d => {
                        html += `<div style="font-size:0.8em;color:#c0c0d0;margin-bottom:2px;">🗑️ ${escapeHtml(d.title)}</div>`;
                    });
                }
                if (data.errors && data.errors.length > 0) {
                    html += `<div style="font-size:0.8em;color:#f8a5b0;margin-top:6px;">${escapeHtml(data.errors.join(', '))}</div>`;
                }
                html += '</div>';
                resultEl.innerHTML = html;
                showToast(data.groups_found === 0
                    ? '✅ No duplicate collections found'
                    : `✅ Removed ${data.deleted.length} duplicate collection(s)`, 'success');
            } catch (e) {
                resultEl.innerHTML = `<div style="color:#f8a5b0;">Error: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
                btn.textContent = '🧬 Remove Duplicate Collections';
            }
        }

        // ---------- Version badge / update check ----------
        // Populates the always-visible version line in the sidebar footer.
        // Deliberately fire-and-forget: the server answers from cache and
        // refreshes in the background, so this never delays anything, and a
        // failed check just leaves the badge showing the current version.
        async function loadVersionBadge() {
            const label = document.getElementById('version-current');
            const pill = document.getElementById('version-update-pill');
            if (!label || !pill) return;
            try {
                const resp = await fetch('/api/system/version');
                const data = await resp.json();
                if (data.error) return;

                label.textContent = 'Vidshelf v' + (data.current || 'unknown');

                if (data.update_available && data.latest) {
                    pill.textContent = 'v' + String(data.latest).replace(/^v/, '') + ' available';
                    pill.href = data.url || '#';
                    pill.style.display = 'inline-block';
                    pill.title = 'A newer release is available. Click for the release notes.';
                } else {
                    pill.style.display = 'none';
                }

                const toggle = document.getElementById('settings-update-check');
                if (toggle) toggle.checked = !!data.enabled;
            } catch (e) {
                // Offline or blocked — leave the badge as-is rather than
                // showing an error for something entirely cosmetic.
            }
        }

        async function setUpdateCheckEnabled(enabled) {
            try {
                await fetch('/api/system/update-check', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: enabled })
                });
                loadVersionBadge();
            } catch (e) {
                showToast('Could not save the update-check setting', 'error');
            }
        }

        // ---------- Automatic channel monitoring ----------
        function fmtWhen(ts) {
            if (!ts) return 'never';
            const d = new Date(ts * 1000);
            return d.toLocaleString();
        }

        async function loadMonitorStatus() {
            const box = document.getElementById('monitor-status');
            if (!box) return;
            try {
                const resp = await fetchWithTimeout('/api/monitor/status');
                const s = await resp.json();
                if (s.error) { box.textContent = ''; return; }

                const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
                const en = document.getElementById('monitor-enabled');
                if (en) en.checked = !!s.enabled;
                setVal('monitor-interval', s.interval_minutes);
                setVal('monitor-max', s.max_per_channel);
                setVal('monitor-listing', s.max_listing);
                setVal('monitor-queue', s.max_queue_depth);
                setVal('monitor-freegb', s.min_free_gb);

                const parts = [];
                parts.push(s.enabled
                    ? `<strong>On</strong> — every ${s.interval_minutes} min`
                    : '<strong>Off</strong> — nothing is checked automatically');
                parts.push(`last check: ${escapeHtml(fmtWhen(s.last_run))}`);
                if (s.last_run) {
                    parts.push(`${s.checked_channels} channel(s) checked, ${s.started_downloads} queued`);
                }
                // A skipped tick (full queue, low disk) is otherwise invisible —
                // the user would see "last check: <time>, 0 queued" and assume
                // there was nothing new.
                if (s.last_skip_reason) {
                    parts.push(`<span style="color:#f0c674;">skipped: ${escapeHtml(s.last_skip_reason)}</span>`);
                }
                if (s.last_error) {
                    parts.push(`<span style="color:#f8a5b0;">last error: ${escapeHtml(s.last_error)}</span>`);
                }
                let html = parts.join(' · ');

                // Per-channel detail from the most recent pass, so a channel that
                // errors is visible here rather than only in the logs.
                const results = s.last_results || [];
                const problems = results.filter(r => r.error);
                if (problems.length) {
                    html += '<div style="margin-top:8px;">' + problems.map(r =>
                        `<div style="color:#f8a5b0;">${escapeHtml(r.channel)}: ${escapeHtml(r.error)}</div>`
                    ).join('') + '</div>';
                }
                box.innerHTML = html;
            } catch (e) {
                box.textContent = 'Could not load monitor status.';
            }
        }

        async function saveMonitorConfig() {
            const body = {
                enabled: document.getElementById('monitor-enabled').checked,
                interval_minutes: parseInt(document.getElementById('monitor-interval').value, 10) || 60,
                max_per_channel: parseInt(document.getElementById('monitor-max').value, 10) || 5,
                max_listing: parseInt(document.getElementById('monitor-listing').value, 10) || 50,
                max_queue_depth: parseInt(document.getElementById('monitor-queue').value, 10) || 20,
                // 0 is meaningful here (it disables the free-space check), so a
                // bare || would wrongly replace it with the default.
                min_free_gb: Math.max(0, parseInt(document.getElementById('monitor-freegb').value, 10) || 0),
            };
            try {
                const resp = await fetch('/api/monitor/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const data = await resp.json();
                if (data.error) { showToast('❌ ' + data.error, 'error'); return; }
                showToast('✅ Monitoring settings saved', 'success');
                loadMonitorStatus();
            } catch (e) {
                showToast('❌ Could not save monitoring settings', 'error');
            }
        }

        async function runMonitorNow() {
            const btn = document.getElementById('monitor-run-btn');
            const original = btn.textContent;
            btn.disabled = true;
            btn.textContent = '⏳ Checking…';
            try {
                // Generous timeout: this fetches every monitored channel's video
                // list from YouTube, which is slow and outside our control.
                const resp = await fetchWithTimeout('/api/monitor/run', { method: 'POST' }, 120000);
                const data = await resp.json();
                if (data.error) { showToast('❌ ' + data.error, 'error'); return; }
                const started = (data.results || []).reduce((n, r) => n + (r.started || 0), 0);
                showToast(started
                    ? `✅ Queued ${started} new video(s)`
                    : '✅ Checked — nothing new', 'success');
                loadMonitorStatus();
            } catch (e) {
                showToast('❌ Check failed: ' + describeFetchError(e), 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = original;
            }
        }

        // ---------- Notifications ----------
        async function loadNotifyConfig() {
            try {
                const resp = await fetchWithTimeout('/api/config');
                const cfg = await resp.json();
                const n = cfg.notifications || {};
                const en = document.getElementById('notify-enabled');
                if (en) en.checked = !!n.enabled;
                // The URL usually embeds a secret, so it is never rendered back.
                // An empty field with a hint means "already set, leave blank to keep".
                const hint = document.getElementById('notify-url-hint');
                if (hint) {
                    hint.textContent = n.url
                        ? 'A URL is saved. Leave blank to keep it, or paste a new one to replace it.'
                        : 'No URL saved yet.';
                }
                const selected = n.events || ['download_failed', 'scheduler_summary', 'retention'];
                document.querySelectorAll('.notify-event').forEach(cb => {
                    cb.checked = selected.includes(cb.value);
                });
            } catch (e) { /* settings page still usable without this */ }
        }

        async function saveNotifyConfig() {
            const urlEl = document.getElementById('notify-url');
            const body = {
                enabled: document.getElementById('notify-enabled').checked,
                events: Array.from(document.querySelectorAll('.notify-event'))
                    .filter(cb => cb.checked).map(cb => cb.value),
            };
            // Only send the URL when the user actually typed one, so saving other
            // settings can't blank out a stored webhook.
            if (urlEl && urlEl.value.trim()) body.url = urlEl.value.trim();
            try {
                const resp = await fetch('/api/notifications/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const data = await resp.json();
                if (data.error) { showToast('❌ ' + data.error, 'error'); return; }
                if (urlEl) urlEl.value = '';
                const n = data.notifications || {};
                const hint = document.getElementById('notify-url-hint');
                if (hint) {
                    hint.textContent = n.url_set
                        ? `Saved${n.detected_kind ? ' — detected as ' + n.detected_kind : ''}. Leave blank to keep it.`
                        : 'No URL saved yet.';
                }
                showToast('✅ Notification settings saved', 'success');
            } catch (e) {
                showToast('❌ Could not save notification settings', 'error');
            }
        }

        async function testNotify() {
            const btn = document.getElementById('notify-test-btn');
            const out = document.getElementById('notify-test-result');
            btn.disabled = true;
            out.textContent = 'Sending…';
            try {
                const resp = await fetchWithTimeout('/api/notifications/test', { method: 'POST' }, 20000);
                const data = await resp.json();
                out.innerHTML = data.success
                    ? `<span style="color:#7ddf90;">✅ ${escapeHtml(data.detail)}</span>`
                    : `<span style="color:#f8a5b0;">❌ ${escapeHtml(data.detail || data.error || 'failed')}</span>`;
            } catch (e) {
                out.innerHTML = `<span style="color:#f8a5b0;">❌ ${escapeHtml(describeFetchError(e))}</span>`;
            } finally {
                btn.disabled = false;
            }
        }

        // ---------- Retention ----------
        async function loadRetentionConfig() {
            try {
                const resp = await fetchWithTimeout('/api/config');
                const cfg = await resp.json();
                const r = cfg.retention || {};
                const en = document.getElementById('retention-enabled');
                if (en) en.checked = !!r.enabled;
                const keep = document.getElementById('retention-keep');
                if (keep) keep.value = r.keep_last_per_artist || 10;
                const auto = document.getElementById('retention-auto');
                if (auto) auto.checked = !!r.auto_sweep;
            } catch (e) { /* non-fatal */ }
        }

        async function saveRetentionConfig() {
            const body = {
                enabled: document.getElementById('retention-enabled').checked,
                keep_last_per_artist: parseInt(document.getElementById('retention-keep').value, 10) || 10,
                auto_sweep: document.getElementById('retention-auto').checked,
            };
            try {
                const resp = await fetch('/api/retention/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const data = await resp.json();
                if (data.error) { showToast('❌ ' + data.error, 'error'); return; }
                showToast('✅ Retention settings saved', 'success');
            } catch (e) {
                showToast('❌ Could not save retention settings', 'error');
            }
        }

        async function previewRetention() {
            const btn = document.getElementById('retention-preview-btn');
            const out = document.getElementById('retention-result');
            btn.disabled = true;
            out.innerHTML = '<div class="loading" style="padding:12px;"><div class="spinner" style="width:20px;height:20px;"></div></div>';
            try {
                const resp = await fetchWithTimeout('/api/retention/plan', {}, 60000);
                const p = await resp.json();
                if (p.error) {
                    out.innerHTML = `<div style="color:#f8a5b0;">⚠️ ${escapeHtml(p.error)}</div>`;
                    return;
                }
                // Per-root problems must be surfaced: a root skipped because it
                // looks unmounted is important, and would otherwise be silent.
                const rootErrors = (p.roots || []).filter(r => r.error);
                const warn = rootErrors.length
                    ? '<div style="margin-bottom:10px;">' + rootErrors.map(r =>
                        `<div style="color:#f0c674;font-size:0.88em;">⚠️ ${escapeHtml(r.root)}: ${escapeHtml(r.error)}</div>`
                      ).join('') + '</div>'
                    : '';
                if (!p.candidate_count) {
                    out.innerHTML = warn + `<div style="color:#7ddf90;">✅ Nothing to delete — `
                        + `${p.artists_scanned} artist(s), ${p.total_files} video(s) across `
                        + `${(p.roots || []).length} root(s), keeping newest ${p.keep_last_per_artist} each.</div>`;
                    return;
                }
                const gb = (p.total_bytes / (1024 ** 3)).toFixed(2);
                let html = warn + `<div style="margin-bottom:10px;">`
                    + `Would delete <strong>${p.candidate_count}</strong> video(s), freeing <strong>${gb} GB</strong>`
                    + ` — keeping the newest ${p.keep_last_per_artist} per artist.</div>`;
                html += '<div style="max-height:220px;overflow:auto;border:1px solid rgba(255,255,255,0.06);border-radius:8px;padding:8px;">';
                html += p.sample.map(c =>
                    `<div style="font-size:0.85em;color:#9090a0;">${escapeHtml(c.artist)} — ${escapeHtml(c.name)}`
                    + ` <span style="color:#606070;">(${formatBytes(c.size_bytes)}, ${new Date(c.modified_at * 1000).toLocaleDateString()})</span></div>`
                ).join('');
                if (p.sample_truncated) {
                    html += `<div style="font-size:0.85em;color:#606070;margin-top:6px;">…and ${p.candidate_count - p.sample.length} more</div>`;
                }
                html += '</div>';
                html += `<button class="btn" style="margin-top:12px;background:rgba(220,53,69,0.15);color:#f8a5b0;border:1px solid rgba(220,53,69,0.35);"`
                    + ` onclick="applyRetention(${p.candidate_count})">🗑 Delete these ${p.candidate_count} file(s)</button>`;
                if (!p.enabled) {
                    html += '<div class="help-text" style="margin-top:8px;">Retention is disabled — enable it above before deleting.</div>';
                }
                out.innerHTML = html;
            } catch (e) {
                out.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(describeFetchError(e))}</div>`;
            } finally {
                btn.disabled = false;
            }
        }

        async function applyRetention(count) {
            // Deliberate double-confirm: this is the only irreversible action in
            // the app, and a mis-click deletes media.
            if (!confirm(`Permanently delete ${count} video file(s)?\n\nThis cannot be undone. `
                + `Pruned videos will NOT be re-downloaded automatically.`)) return;
            const out = document.getElementById('retention-result');
            out.innerHTML = '<div class="loading" style="padding:12px;"><div class="spinner" style="width:20px;height:20px;"></div></div>';
            try {
                const resp = await fetchWithTimeout('/api/retention/apply', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ confirm: 'DELETE' })
                }, 120000);
                const data = await resp.json();
                if (!data.success) {
                    out.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(data.error || data.plan_error || 'failed')}</div>`;
                    return;
                }
                const gb = (data.freed_bytes / (1024 ** 3)).toFixed(2);
                let html = `<div style="color:#7ddf90;">✅ Deleted ${data.deleted_count} file(s), freed ${gb} GB.</div>`;
                if ((data.failed || []).length) {
                    html += `<div style="color:#f8a5b0;margin-top:6px;">${data.failed.length} deletion(s) failed.</div>`;
                }
                out.innerHTML = html;
                showToast(`✅ Freed ${gb} GB`, 'success');
            } catch (e) {
                out.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(describeFetchError(e))}</div>`;
            }
        }

        // ---------- System Health ----------
        async function loadSystemHealth() {
            const list = document.getElementById('system-health-list');
            try {
                const resp = await fetch('/api/system/health');
                const data = await resp.json();
                if (data.error) {
                    list.innerHTML = `<div style="color:#f8a5b0;">${escapeHtml(data.error)}</div>`;
                    return;
                }

                const rows = [
                    { key: 'ffmpeg', label: 'ffmpeg', why: 'downloading and merging video/audio streams' },
                    { key: 'ffprobe', label: 'ffprobe', why: 'checking video/audio codecs for format conversion' },
                    { key: 'pillow', label: 'Pillow', why: 'generating per-video title-card posters' },
                    { key: 'fonts', label: 'DejaVu fonts', why: 'legible text on title-card posters (falls back to a tiny bitmap font without it)' },
                    // Optional, so a missing cookies file is informational
                    // rather than a failure — but it must be visible, because
                    // its absence only shows up as unexplained download errors.
                    { key: 'cookies', label: 'YouTube cookies', why: 'age-restricted and members-only videos', optional: true },
                ];

                let html = '';
                rows.forEach(r => {
                    const info = data[r.key] || {};
                    // 'found' for dependency probes, 'available' for optional
                    // extras like the cookies file.
                    const ok = !!(info.found || info.available);
                    // A missing optional item is information, not a fault —
                    // painting it red would imply something is broken.
                    const icon = ok ? '✅' : (r.optional ? 'ℹ️' : '❌');
                    const color = ok ? '#7ddf90' : (r.optional ? '#9090a0' : '#f8a5b0');
                    let detail = ok ? 'found' : 'not found';
                    if (ok && info.version) detail = `v${escapeHtml(info.version)}`;
                    if (info.detail) detail = escapeHtml(info.detail);
                    if (ok && info.path && !info.detail) detail += ` <span style="color:#606070;">(${escapeHtml(info.path)})</span>`;
                    html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:rgba(255,255,255,0.03);border-radius:6px;">
                        <div>
                            <span style="color:${color};">${icon}</span>
                            <strong style="margin-left:4px;">${escapeHtml(r.label)}</strong>
                            <span style="color:#707080;font-size:0.85em;"> — ${escapeHtml(r.why)}</span>
                        </div>
                        <div style="font-size:0.85em;color:#8888a0;">${detail}</div>
                    </div>`;
                });
                list.innerHTML = html;
            } catch (e) {
                list.innerHTML = `<div style="color:#f8a5b0;">Failed to check system health: ${escapeHtml(e.message)}</div>`;
            }
        }

        // ---------- Video Format Compatibility ----------
        let conversionPollInterval = null;

        async function scanConversionCandidates() {
            const btn = document.getElementById('conversion-scan-btn');
            const resultEl = document.getElementById('conversion-result');
            btn.disabled = true;
            btn.textContent = '⏳ Scanning...';
            resultEl.innerHTML = '<div style="color:#e9c46a;">⏳ Scanning your library for non-compatible videos...</div>';

            try {
                const resp = await fetch('/api/conversion/scan', { method: 'POST' });
                const data = await resp.json();
                if (data.error) {
                    resultEl.innerHTML = `<div style="color:#f8a5b0;">❌ ${escapeHtml(data.error)}</div>`;
                    return;
                }
                let html = `<div style="padding:12px;background:rgba(255,255,255,0.03);border-radius:8px;">
                    <div style="font-size:0.9em;color:#c0c0d0;">
                        ${data.needs_conversion === 0
                            ? '✅ Everything is already Plex-compatible.'
                            : `⚠️ <strong style="color:#e9c46a;">${data.needs_conversion}</strong> video(s) need conversion.`}
                    </div>`;
                if (data.files && data.files.length > 0) {
                    html += '<div style="margin-top:8px;font-size:0.78em;color:#8888a0;max-height:200px;overflow-y:auto;">';
                    data.files.forEach(f => { html += `<div style="margin-bottom:2px;">${escapeHtml(f)}</div>`; });
                    if (data.truncated) html += '<div style="color:#707080;">...and more</div>';
                    html += '</div>';
                }
                html += '</div>';
                resultEl.innerHTML = html;
            } catch (e) {
                resultEl.innerHTML = `<div style="color:#f8a5b0;">Error: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
                btn.textContent = '🔍 Scan Library';
            }
        }

        async function startConversion() {
            const btn = document.getElementById('conversion-start-btn');
            const resultEl = document.getElementById('conversion-result');
            btn.disabled = true;
            try {
                const resp = await fetch('/api/conversion/start', { method: 'POST' });
                const data = await resp.json();
                if (data.error) {
                    showToast('❌ ' + data.error, 'error');
                    btn.disabled = false;
                    return;
                }
                resultEl.innerHTML = '';
                showToast('⚙️ Conversion job started — this can take a while for large videos', 'success');
                document.getElementById('conversion-progress').style.display = 'block';
                if (conversionPollInterval) clearInterval(conversionPollInterval);
                conversionPollInterval = setInterval(pollConversionStatus, 2000);
                pollConversionStatus();
            } catch (e) {
                showToast('❌ Error: ' + e.message, 'error');
                btn.disabled = false;
            }
        }

        async function resumeConversionPollingIfRunning() {
            try {
                const resp = await fetch('/api/conversion/status');
                const data = await resp.json();
                if (data.running) {
                    document.getElementById('conversion-start-btn').disabled = true;
                    document.getElementById('conversion-progress').style.display = 'block';
                    if (conversionPollInterval === null) {
                        conversionPollInterval = setInterval(pollConversionStatus, 2000);
                    }
                    pollConversionStatus();
                }
            } catch (e) { /* ignore — status will just show nothing until a job starts */ }
        }

        async function pollConversionStatus() {
            try {
                const resp = await fetch('/api/conversion/status');
                const data = await resp.json();

                // 'running' flips true the instant the background thread
                // starts, before the (potentially slow) initial library
                // scan finishes — check 'running', not total_files > 0,
                // or a poll landing during that scan misreads "still
                // scanning" as "job already finished".
                if (data.phase === 'scanning') {
                    document.getElementById('conversion-progress-label').textContent = 'Scanning library...';
                    document.getElementById('conversion-progress-count').textContent = '';
                    document.getElementById('conversion-progress-bar').style.width = '0%';
                    document.getElementById('conversion-current-file').textContent = '';
                    return;
                }

                const total = data.total_files || 0;
                const scanned = data.scanned || 0;
                const pct = total > 0 ? Math.round((scanned / total) * 100) : 0;

                document.getElementById('conversion-progress-count').textContent = `${scanned} / ${total}`;
                document.getElementById('conversion-progress-bar').style.width = pct + '%';
                document.getElementById('conversion-current-file').textContent = data.current_file
                    ? 'Converting: ' + data.current_file : '';

                if (!data.running) {
                    document.getElementById('conversion-progress-label').textContent = 'Done';
                    if (conversionPollInterval) {
                        clearInterval(conversionPollInterval);
                        conversionPollInterval = null;
                    }
                    document.getElementById('conversion-start-btn').disabled = false;
                    if (total > 0) {
                        const resultEl = document.getElementById('conversion-result');
                        let html = `<div style="padding:12px;background:rgba(255,255,255,0.03);border-radius:8px;">
                            <div style="font-size:0.9em;color:#c0c0d0;">
                                ✅ Converted: <span style="color:#7ddf90;">${data.converted}</span> |
                                ❌ Failed: <span style="color:#f8a5b0;">${data.failed}</span>
                            </div>`;
                        if (data.errors && data.errors.length > 0) {
                            html += '<div style="margin-top:8px;font-size:0.78em;color:#f8a5b0;max-height:150px;overflow-y:auto;">';
                            data.errors.forEach(err => { html += `<div style="margin-bottom:2px;">${escapeHtml(err)}</div>`; });
                            html += '</div>';
                        }
                        html += '</div>';
                        resultEl.innerHTML = html;
                        showToast(`✅ Conversion finished: ${data.converted} converted, ${data.failed} failed`, data.failed > 0 ? 'error' : 'success');
                    }
                } else {
                    document.getElementById('conversion-progress-label').textContent = 'Converting...';
                }
            } catch (e) {
                // transient poll failure — try again on the next tick
            }
        }

        async function disconnectPlex() {
            showConfirmModal(
                'Disconnect Plex?\n\nThis will remove the Plex token and server configuration. You will need to re-authenticate.',
                async function() {
                    try {
                        const resp = await fetch('/api/plex/config', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ token: '', server_url: '', music_video_library_key: '' })
                        });
                        const data = await resp.json();
                        if (data.success) {
                            showToast('✅ Plex disconnected', 'success');
                            checkPlexConnection();
                        } else {
                            showToast('❌ ' + data.error, 'error');
                        }
                    } catch (e) {
                        showToast('❌ Error: ' + e.message, 'error');
                    }
                }
            );
        }

        // Check Plex connection when settings page loads
        const origSettingsLoad = loadConfig;
        loadConfig = function() {
            origSettingsLoad();
            checkPlexConnection();
        };

        // ---------- Initial Load ----------
        loadDashboardStats();
        loadVersionBadge();

/* ---- block 2 of 4 (was inline in dashboard.html) ---- */
document.getElementById('create-collection-link').addEventListener('click', function(e) {
    e.preventDefault();
    openCollectionModal();
});

function openCollectionModal() {
    const modal = document.getElementById('collection-modal');
    const artistSelect = document.getElementById('artist-select');
    modal.classList.add('active');
    // Load artists
    fetch('/api/artists')
        .then(r => r.json())
        .then(data => {
            artistSelect.innerHTML = '<option value="">Select an artist</option>';
            (data.artists || []).forEach(artist => {
                const opt = document.createElement('option');
                opt.value = artist;
                opt.textContent = artist;
                artistSelect.appendChild(opt);
            });
        })
        .catch(() => {
            showToast('Failed to load artists', 'error');
        });
}

document.getElementById('collection-modal-close').addEventListener('click', function() {
    document.getElementById('collection-modal').classList.remove('active');
});
document.getElementById('collection-modal-cancel').addEventListener('click', function() {
    document.getElementById('collection-modal').classList.remove('active');
});
document.getElementById('collection-modal-create').addEventListener('click', function() {
    const artist = document.getElementById('artist-select').value;
    if (!artist) {
        showToast('Please select an artist', 'error');
        return;
    }
    fetch('/api/plex/collections/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artist })
    })
    .then(r => r.json())
    .then(data => {
        if (data.result && data.result.success !== false) {
            showToast('Collection created successfully', 'success');
            document.getElementById('collection-modal').classList.remove('active');
        } else {
            const err = data.error || (data.result && data.result.error) || 'Failed to create collection';
            showToast(err, 'error');
        }
    })
    .catch(() => {
        showToast('Error creating collection', 'error');
    });
});

/* ---- block 3 of 4 (was inline in dashboard.html) ---- */
// ---------- Swap Artwork Page ----------
let swapArtSearchArtist = null;
let swapArtSearchPage = 1;
let swapArtSelectedUrl = null;

function loadSwapArtArtists() {
    const select = document.getElementById('swap-art-artist-select');
    fetch('/api/artists')
        .then(r => r.json())
        .then(data => {
            select.innerHTML = '<option value="">Select an artist</option>';
            (data.artists || []).forEach(artist => {
                const opt = document.createElement('option');
                opt.value = artist;
                opt.textContent = artist;
                select.appendChild(opt);
            });
        })
        .catch(() => {
            select.innerHTML = '<option value="">Failed to load artists</option>';
            showToast('Failed to load artists', 'error');
        });
}

document.getElementById('swap-art-artist-select').addEventListener('change', function() {
    updateSwapArtCurrentPreview(this.value);
    document.getElementById('swap-art-results').innerHTML = '';
    swapArtSearchArtist = null;
});

function updateSwapArtCurrentPreview(artist) {
    const img = document.getElementById('swap-art-current-preview');
    const empty = document.getElementById('swap-art-current-empty');
    if (!artist) {
        img.style.display = 'none';
        empty.style.display = 'block';
        empty.textContent = 'Select an artist to preview its current artwork.';
        return;
    }
    // Cache-bust so a just-swapped image doesn't show the old cached one
    img.src = `/api/artwork/current_image?artist=${encodeURIComponent(artist)}&_=${img.dataset.bust || 0}`;
    img.onload = () => { img.style.display = 'block'; empty.style.display = 'none'; };
    img.onerror = () => {
        img.style.display = 'none';
        empty.style.display = 'block';
        empty.textContent = 'No current artwork found for this artist.';
    };
}

function appendSwapArtImages(images, resultsDiv) {
    images.forEach(url => {
        const img = document.createElement('img');
        img.src = url;
        img.title = 'Click to select this image';
        img.addEventListener('click', () => {
            document.getElementById('swap-art-image-url').value = url;
            swapArtSelectedUrl = url;
            resultsDiv.querySelectorAll('img').forEach(i => i.classList.remove('selected'));
            img.classList.add('selected');
        });
        resultsDiv.insertBefore(img, document.getElementById('swap-art-load-more'));
    });
}

function removeSwapArtLoadMoreButton() {
    const existing = document.getElementById('swap-art-load-more');
    if (existing) existing.remove();
}

function addSwapArtLoadMoreButton(resultsDiv) {
    removeSwapArtLoadMoreButton();
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-secondary btn-sm';
    btn.id = 'swap-art-load-more';
    btn.textContent = 'Load 5 More';
    btn.style.alignSelf = 'center';
    btn.addEventListener('click', () => fetchSwapArtImagePage(resultsDiv, /*append=*/true));
    resultsDiv.appendChild(btn);
}

function fetchSwapArtImagePage(resultsDiv, append) {
    const statusEl = document.getElementById('swap-art-search-status');
    if (!append) {
        swapArtSearchArtist = document.getElementById('swap-art-artist-select').value;
        swapArtSearchPage = 1;
        resultsDiv.innerHTML = '';
    } else {
        swapArtSearchPage += 1;
    }
    if (!swapArtSearchArtist) {
        showToast('Select an artist first', 'error');
        return;
    }
    statusEl.innerHTML = '<div class="loading"><div class="spinner"></div><p>Searching for artwork...</p></div>';
    fetch(`/api/artwork/search?artist=${encodeURIComponent(swapArtSearchArtist)}&page=${swapArtSearchPage}`)
        .then(r => r.json())
        .then(data => {
            statusEl.innerHTML = '';
            const images = data.images || [];
            if (images.length === 0 && !append) {
                resultsDiv.innerHTML = '';
                statusEl.innerHTML = '<div class="empty-state"><p>No images found.</p></div>';
                return;
            }
            removeSwapArtLoadMoreButton();
            appendSwapArtImages(images, resultsDiv);
            if (data.has_more) addSwapArtLoadMoreButton(resultsDiv);
        })
        .catch(e => {
            statusEl.innerHTML = `<div class="empty-state"><p>Error searching images: ${escapeHtml(e.message)}</p></div>`;
        });
}

function searchArtworkImages() {
    fetchSwapArtImagePage(document.getElementById('swap-art-results'), /*append=*/false);
}

function swapArtwork() {
    const artist = document.getElementById('swap-art-artist-select').value;
    const imageUrl = document.getElementById('swap-art-image-url').value.trim();
    if (!artist) {
        showToast('Please select an artist', 'error');
        return;
    }
    if (!imageUrl) {
        showToast('Please enter or select an image URL', 'error');
        return;
    }
    const btn = document.getElementById('swap-art-swap-btn');
    btn.disabled = true;
    btn.textContent = '⏳ Swapping...';
    fetch('/api/artwork/swap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artist_name: artist, new_image_url: imageUrl })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showToast('✅ ' + (data.message || 'Artwork swapped successfully'), 'success');
                const img = document.getElementById('swap-art-current-preview');
                img.dataset.bust = Date.now();
                updateSwapArtCurrentPreview(artist);
            } else {
                showToast('❌ ' + (data.error || 'Failed to swap artwork'), 'error');
            }
        })
        .catch(e => {
            showToast('❌ Error: ' + e.message, 'error');
        })
        .finally(() => {
            btn.disabled = false;
            btn.textContent = '🔁 Swap Artwork';
        });
}

/* ---- block 4 of 4 (was inline in dashboard.html) ---- */
// ---------- Artists Page ----------
const _artistVideosCache = {};

// The full artist list as fetched, kept so search/filter/sort work locally.
// /api/artists/summary walks every artist directory and counts video files in
// each, which is real I/O against what is usually a network mount — so it is
// fetched once per page visit and filtered in memory, rather than re-querying
// the server on every keystroke.
let _artistsAll = [];
// Which rows the user had expanded, so a re-render doesn't collapse them.
let _expandedArtists = new Set();

async function loadArtistsPage() {
    const container = document.getElementById('artists-list');
    container.className = 'loading';
    container.innerHTML = '<div class="spinner"></div><p>Loading artists...</p>';
    try {
        // Timed out rather than plain fetch: /api/artists/summary walks every
        // artist directory and counts files in each, which is I/O against what
        // is usually a network mount. A stalled mount used to leave this
        // spinner up indefinitely with no way to tell slow from broken.
        const resp = await fetchWithTimeout('/api/artists/summary');
        const data = await resp.json();
        if (data.error) {
            container.className = 'empty-state';
            container.innerHTML = `<div class="empty-icon">⚠️</div><p>Error loading artists: ${escapeHtml(data.error)}</p>`
                + '<button class="btn btn-secondary" onclick="loadArtistsPage()">Retry</button>';
            _artistsAll = [];
            updateArtistFilterSummary(0, 0);
            return;
        }
        _artistsAll = data.artists || [];
        renderArtistList();
    } catch (e) {
        container.className = 'empty-state';
        container.innerHTML = `<div class="empty-icon">⚠️</div><p>Couldn't load artists — ${escapeHtml(describeFetchError(e))}.</p>`
            + '<button class="btn btn-secondary" onclick="loadArtistsPage()">Retry</button>';
        _artistsAll = [];
        updateArtistFilterSummary(0, 0);
    }
}

function getArtistFilters() {
    const val = (id, fallback) => {
        const el = document.getElementById(id);
        return el ? el.value : fallback;
    };
    return {
        query: val('artist-search', '').trim().toLowerCase(),
        artwork: val('artist-filter-artwork', 'all'),
        videos: val('artist-filter-videos', 'all'),
        sort: val('artist-sort', 'name-asc'),
    };
}

function filterAndSortArtists(artists, f) {
    let out = artists.filter(a => {
        if (f.artwork === 'has' && !a.has_artwork) return false;
        if (f.artwork === 'missing' && a.has_artwork) return false;
        if (f.videos === 'has' && !(a.video_count > 0)) return false;
        if (f.videos === 'empty' && a.video_count > 0) return false;
        if (f.query) {
            // Match the folder name too: it can differ from the display name
            // (folder_to_artist() rewrites it), and someone looking at the
            // filesystem will search for what they saw there.
            const name = (a.artist || '').toLowerCase();
            const folder = (a.folder || '').toLowerCase();
            if (!name.includes(f.query) && !folder.includes(f.query)) return false;
        }
        return true;
    });

    // localeCompare so accented names sort where a reader expects, rather than
    // after Z by code point — this library is full of them. Leading articles
    // are ignored so "The Beatles" files under B, matching how Plex sorts the
    // same library; sorting it under T puts every "The ..." band in one useless
    // clump. The \s+ in the pattern is what keeps "a-ha" intact.
    const sortKey = a => (a.artist || '').replace(/^(the|an|a)\s+/i, '');
    const byName = (x, y) => sortKey(x).localeCompare(sortKey(y), undefined, { sensitivity: 'base' });
    if (f.sort === 'name-desc') out.sort((x, y) => byName(y, x));
    else if (f.sort === 'videos-desc') out.sort((x, y) => (y.video_count - x.video_count) || byName(x, y));
    else if (f.sort === 'videos-asc') out.sort((x, y) => (x.video_count - y.video_count) || byName(x, y));
    else out.sort(byName);

    return out;
}

function updateArtistFilterSummary(shown, total) {
    const el = document.getElementById('artist-filter-summary');
    if (!el) return;
    if (total === 0) { el.textContent = ''; return; }
    el.innerHTML = shown === total
        ? `<strong>${total}</strong> artist${total === 1 ? '' : 's'}`
        : `<strong>${shown}</strong> of <strong>${total}</strong> artists`;
}

function renderArtistList() {
    const container = document.getElementById('artists-list');
    if (!container) return;

    const total = _artistsAll.length;
    const clearBtn = document.getElementById('artist-search-clear');
    const f = getArtistFilters();
    if (clearBtn) clearBtn.style.display = f.query ? 'block' : 'none';

    if (total === 0) {
        container.className = 'empty-state';
        container.innerHTML = '<div class="empty-icon">🎤</div><p>No artists yet. Download some music videos to see them here.</p>';
        updateArtistFilterSummary(0, 0);
        return;
    }

    const visible = filterAndSortArtists(_artistsAll, f);
    updateArtistFilterSummary(visible.length, total);

    if (visible.length === 0) {
        // Distinct from the "no artists at all" state above: here the library
        // isn't empty, the filters just exclude everything, so offer the way out.
        container.className = 'empty-state';
        container.innerHTML = '<div class="empty-icon">🔍</div><p>No artists match these filters.</p>'
            + '<button class="btn btn-secondary" onclick="resetArtistFilters()">Reset filters</button>';
        return;
    }

    container.className = '';
    container.innerHTML = visible.map(a => `
        <div class="artist-row" data-artist="${escapeHtml(a.artist)}">
            <div class="artist-row-header" onclick="toggleArtistRow(this)">
                <img class="artist-thumb" src="/api/artwork/current_image?artist=${encodeURIComponent(a.artist)}" onerror="this.style.visibility='hidden'">
                <div class="artist-row-name">${escapeHtml(a.artist)}${a.has_artwork ? '' : ' <span class=\"artist-row-count\">(no artwork)</span>'}</div>
                <div class="artist-row-count">${a.video_count} video${a.video_count === 1 ? '' : 's'}</div>
                <span class="artist-row-chevron">▶</span>
            </div>
            <div class="artist-row-videos"></div>
        </div>
    `).join('');

    // Re-apply expansion after the re-render. Only for rows whose video list is
    // already cached — re-fetching every previously-open row on each keystroke
    // would fire a burst of requests at the same slow directory walk this
    // design exists to avoid.
    container.querySelectorAll('.artist-row').forEach(row => {
        const artist = row.dataset.artist;
        if (_expandedArtists.has(artist) && _artistVideosCache[artist]) {
            row.classList.add('expanded');
            row.querySelector('.artist-row-videos').innerHTML = _artistVideosCache[artist];
        }
    });
}

// Debounced so typing doesn't re-render the whole list per character. Filtering
// is in-memory and fast, but the DOM rebuild is not free with a large library.
let _artistFilterTimer = null;
function onArtistFilterInput() {
    clearTimeout(_artistFilterTimer);
    _artistFilterTimer = setTimeout(renderArtistList, 120);
}

function clearArtistSearch() {
    const input = document.getElementById('artist-search');
    if (input) { input.value = ''; input.focus(); }
    renderArtistList();
}

function resetArtistFilters() {
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    set('artist-search', '');
    set('artist-filter-artwork', 'all');
    set('artist-filter-videos', 'all');
    set('artist-sort', 'name-asc');
    renderArtistList();
}

async function toggleArtistRow(headerEl) {
    const row = headerEl.closest('.artist-row');
    const artist = row.dataset.artist;
    const videosEl = row.querySelector('.artist-row-videos');
    const wasExpanded = row.classList.contains('expanded');

    if (wasExpanded) {
        row.classList.remove('expanded');
        _expandedArtists.delete(artist);
        return;
    }
    row.classList.add('expanded');
    // Tracked so filtering/sorting can restore this row's open state afterwards.
    _expandedArtists.add(artist);

    if (_artistVideosCache[artist]) {
        videosEl.innerHTML = _artistVideosCache[artist];
        return;
    }

    videosEl.innerHTML = '<div class="loading" style="padding:16px;"><div class="spinner" style="width:20px;height:20px;"></div></div>';
    try {
        const resp = await fetch(`/api/artists/videos?artist=${encodeURIComponent(artist)}`);
        const data = await resp.json();
        if (data.error) {
            videosEl.innerHTML = `<p class="artist-video-meta">Error: ${escapeHtml(data.error)}</p>`;
            return;
        }
        const videos = data.videos || [];
        if (videos.length === 0) {
            videosEl.innerHTML = '<p class="artist-video-meta">No video files found in this folder.</p>';
            return;
        }
        const html = videos.map(v => `
            <div class="artist-video-item">
                <span class="artist-video-title">${escapeHtml(v.title)}</span>
                <span class="artist-video-meta">${formatBytes(v.size_bytes)}</span>
                <span class="artist-video-meta">${new Date(v.modified_at * 1000).toLocaleDateString()}</span>
            </div>
        `).join('');
        _artistVideosCache[artist] = html;
        videosEl.innerHTML = html;
    } catch (e) {
        videosEl.innerHTML = `<p class="artist-video-meta">Failed to load videos: ${escapeHtml(e.message)}</p>`;
    }
}
