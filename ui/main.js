/**
 * @typedef {'downloading'|'preparing'|'pending'|'scheduled'|'finished'|'error'} DownloadStatus
 *
 * @typedef {object} Download
 * @property {string}         id            - queue key (url-derived)
 * @property {string}         title
 * @property {string}         url
 * @property {'video'|'audio'} download_type
 * @property {string}         codec         - auto | h264 | h265 | av1 | vp9
 * @property {string}         format        - mp4 | any | m4a | mp3 | …
 * @property {string}         quality       - best | 1080 | 720 | …
 * @property {DownloadStatus} status
 * @property {string|null}    msg
 * @property {number|null}    percent       - 0–100
 * @property {number|null}    speed         - bytes/s
 * @property {number|null}    eta           - seconds
 * @property {string|null}    filename
 * @property {number|null}    size          - bytes
 * @property {number|null}    timestamp     - nanoseconds since epoch
 * @property {string|null}    error
 * @property {Array<{filename:string,size:number}>} [subtitle_files]
 * @property {string[]}       [subtitle_langs]
 */

/**
 * @typedef {object} AppConfig
 * @property {string}  PUBLIC_HOST_URL
 */

/**
 * @typedef {'light'|'dark'|'auto'} Theme
 */

/** @returns {object} Alpine.js component */
function app() {
  return {
    // ── form ──────────────────────────────────────────────────────────────────
    url: '',
    downloadType: /** @type {'video'|'audio'} */ ('video'),
    codec: 'auto',
    format: 'mp4',
    quality: 'best',

    // ── queue state ───────────────────────────────────────────────────────────
    /** @type {Download[]} */
    queue: [],
    /** @type {Download[]} */
    done: [],

    // ── ui flags ──────────────────────────────────────────────────────────────
    adding: false,
    /** @type {string|null} */
    error: null,

    // ── config ────────────────────────────────────────────────────────────────
    /** @type {AppConfig} */
    config: { PUBLIC_HOST_URL: 'download/' },

    // ── cookies ───────────────────────────────────────────────────────────────
    hasCookies: false,
    cookieUploading: false,
    cookieText: '',
    cookieTab: /** @type {'file'|'paste'} */ ('file'),

    // ── theme ─────────────────────────────────────────────────────────────────
    theme: /** @type {Theme} */ (localStorage.getItem('metube_theme') || 'auto'),

    // ── subtitles ─────────────────────────────────────────────────────────────
    /** @type {string} comma-separated language codes entered by user */
    subtitleLangsInput: '',

    // ── logs ──────────────────────────────────────────────────────────────────
    /** @type {string|null} id of the download whose log panel is open */
    openLogsId: null,
    /** @type {Object.<string, string[]>} */
    logs: {},
    _logsTimer: null,

    // ── internals ─────────────────────────────────────────────────────────────
    _polling: false,
    _mq: null,

    // ══════════════════════════════════════════════════════════════════════════
    //  Lifecycle
    // ══════════════════════════════════════════════════════════════════════════

    /** Called by x-init */
    async init() {
      this._applyTheme();
      this._mq = window.matchMedia('(prefers-color-scheme: dark)');
      this._mq.addEventListener('change', () => this._applyTheme());

      await Promise.all([
        this._loadConfig(),
        this._refreshCookieStatus(),
        this.pollState(),
      ]);

      setInterval(() => this.pollState(), 2000);
    },

    // ══════════════════════════════════════════════════════════════════════════
    //  Polling
    // ══════════════════════════════════════════════════════════════════════════

    /** Fetch live queue state from server and update local arrays. */
    async pollState() {
      if (this._polling) return;
      this._polling = true;
      try {
        const resp = await fetch('queue');
        if (!resp.ok) return;
        const [q, d] = await resp.json();
        this.queue = q.map(([id, dl]) => ({ ...dl, id })).reverse();
        this.done  = d.map(([id, dl]) => ({ ...dl, id })).reverse();
      } catch { /* network blip – keep previous state */ } finally {
        this._polling = false;
      }
    },

    // ══════════════════════════════════════════════════════════════════════════
    //  Config
    // ══════════════════════════════════════════════════════════════════════════

    async _loadConfig() {
      try {
        const resp = await fetch('configuration');
        if (resp.ok) this.config = await resp.json();
      } catch { /* ignore */ }
    },

    // ══════════════════════════════════════════════════════════════════════════
    //  Downloads
    // ══════════════════════════════════════════════════════════════════════════

    /** POST /add with current form state. */
    async addDownload() {
      if (!this.url.trim()) return;
      const { langs, error: langsError } = this._parseSubtitleLangs();
      if (langsError) { this.error = langsError; return; }
      this.adding = true;
      this.error  = null;
      try {
        const resp = await fetch('add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url:                  this.url.trim(),
            download_type:        this.downloadType,
            codec:                this.downloadType === 'video' ? this.codec : 'auto',
            format:               this.format,
            quality:              this.quality,
            subtitle_langs:       langs,
          }),
        });
        const data = await resp.json();
        if (data.status === 'ok') {
          this.url = '';
          await this.pollState();
        } else {
          this.error = data.msg || 'Failed to add download';
        }
      } catch (e) {
        this.error = String(e);
      } finally {
        this.adding = false;
      }
    },

    /**
     * Delete a download entry.
     * @param {string} id
     * @param {'queue'|'done'} where
     */
    async deleteDownload(id, where) {
      await fetch('delete', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ ids: [id], where }),
      });
      await this.pollState();
    },

    /**
     * Re-add a failed download with its original parameters.
     * @param {Download} dl
     */
    async retryDownload(dl) {
      await fetch('add', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          url:                  dl.url,
          download_type:        dl.download_type,
          codec:                dl.codec,
          format:               dl.format,
          quality:              dl.quality,
          subtitle_langs:       dl.subtitle_langs || [],
        }),
      });
      await this.deleteDownload(dl.id, 'done');
    },

    /**
     * Return the browser-accessible download URL for a finished item.
     * @param {Download} dl
     * @returns {string}
     */
    downloadUrl(dl) {
      return this.config.PUBLIC_HOST_URL + encodeURIComponent(dl.filename || '');
    },

    // ══════════════════════════════════════════════════════════════════════════
    //  Cookies
    // ══════════════════════════════════════════════════════════════════════════

    async _refreshCookieStatus() {
      try {
        const resp = await fetch('cookies');
        if (resp.ok) {
          const data = await resp.json();
          this.hasCookies = !!data.has_cookies;
        }
      } catch { /* ignore */ }
    },

    /**
     * Upload cookies from a file input.
     * @param {Event} event
     */
    async uploadCookieFile(event) {
      const input = /** @type {HTMLInputElement} */ (event.target);
      const file = input.files?.[0];
      if (!file) return;
      this.cookieUploading = true;
      try {
        const fd = new FormData();
        fd.append('cookies', file);
        await fetch('cookies', { method: 'POST', body: fd });
        await this._refreshCookieStatus();
        input.value = '';
      } finally {
        this.cookieUploading = false;
      }
    },

    /** Upload cookies from the paste textarea. */
    async uploadCookieText() {
      if (!this.cookieText.trim()) return;
      this.cookieUploading = true;
      try {
        const blob = new Blob([this.cookieText], { type: 'text/plain' });
        const fd = new FormData();
        fd.append('cookies', blob, 'cookies.txt');
        await fetch('cookies', { method: 'POST', body: fd });
        await this._refreshCookieStatus();
        this.cookieText = '';
      } finally {
        this.cookieUploading = false;
      }
    },

    /** DELETE uploaded cookies. */
    async deleteCookies() {
      await fetch('cookies', { method: 'DELETE' });
      await this._refreshCookieStatus();
    },

    // ══════════════════════════════════════════════════════════════════════════
    //  Subtitles
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Parse and validate the subtitle language input.
     * @returns {{ langs: string[], error: string|null }}
     */
    _parseSubtitleLangs() {
      const raw = this.subtitleLangsInput.trim();
      if (!raw) return { langs: [], error: null };
      const langs = raw.split(',').map(s => s.trim()).filter(Boolean);
      const invalid = langs.filter(l => !/^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$/.test(l));
      if (invalid.length) return { langs: [], error: `Invalid subtitle language codes: ${invalid.join(', ')}` };
      return { langs, error: null };
    },

    // ══════════════════════════════════════════════════════════════════════════
    //  Logs
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Toggle the log panel for a download. Only one panel open at a time.
     * @param {string} id
     */
    toggleLogs(id) {
      if (this.openLogsId === id) {
        this.openLogsId = null;
        clearInterval(this._logsTimer);
        this._logsTimer = null;
      } else {
        this.openLogsId = id;
        this._fetchLogs(id);
        clearInterval(this._logsTimer);
        this._logsTimer = setInterval(() => {
          if (this.openLogsId) this._fetchLogs(this.openLogsId);
        }, 5000);
      }
    },

    /** @param {string} id */
    async _fetchLogs(id) {
      try {
        const resp = await fetch('logs?id=' + encodeURIComponent(id));
        if (resp.ok) this.logs = { ...this.logs, [id]: await resp.json() };
      } catch { /* ignore */ }
    },

    // ══════════════════════════════════════════════════════════════════════════
    //  Theme
    // ══════════════════════════════════════════════════════════════════════════

    /** @param {Theme} t */
    setTheme(t) {
      this.theme = t;
      localStorage.setItem('metube_theme', t);
      this._applyTheme();
    },

    _applyTheme() {
      document.documentElement.dataset.theme = this.resolvedTheme;
    },

    // ══════════════════════════════════════════════════════════════════════════
    //  Computed helpers
    // ══════════════════════════════════════════════════════════════════════════

    get activeCount()  { return this.queue.filter(d => d.status === 'downloading').length; },
    get queuedCount()  { return this.queue.filter(d => ['pending', 'preparing'].includes(d.status)).length; },
    get doneCount()    { return this.done.filter(d => d.status === 'finished').length; },
    get failedCount()  { return this.done.filter(d => d.status === 'error').length; },

    /** @returns {Theme} */
    get resolvedTheme() {
      if (this.theme !== 'auto') return this.theme;
      return (this._mq?.matches) ? 'dark' : 'light';
    },

    /** Format options depending on download type. */
    get formatOptions() {
      return this.downloadType === 'video'
        ? [{ v: 'mp4', l: 'MP4' }, { v: 'any', l: 'Any' }, { v: 'ios', l: 'iOS' }]
        : [{ v: 'm4a', l: 'M4A' }, { v: 'mp3', l: 'MP3' }, { v: 'opus', l: 'Opus' }, { v: 'wav', l: 'WAV' }, { v: 'flac', l: 'FLAC' }];
    },

    /** Quality options depending on download type. */
    get qualityOptions() {
      return this.downloadType === 'video'
        ? [{ v: 'best', l: 'Best' }, { v: '1080', l: '1080p' }, { v: '720', l: '720p' }, { v: '480', l: '480p' }, { v: '360', l: '360p' }, { v: '240', l: '240p' }]
        : [{ v: 'best', l: 'Best' }, { v: '320K', l: '320K' }, { v: '192K', l: '192K' }, { v: '128K', l: '128K' }];
    },

    /** Reset format and quality to sensible defaults when type changes. */
    onTypeChange() {
      this.format  = this.downloadType === 'video' ? 'mp4' : 'm4a';
      this.quality = 'best';
    },

    // ══════════════════════════════════════════════════════════════════════════
    //  Formatting utilities
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Format a byte count as a human-readable string.
     * @param {number|null} bytes
     * @returns {string}
     */
    formatBytes(bytes) {
      if (bytes == null) return '—';
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024 ** 2) return (bytes / 1024).toFixed(1) + ' KB';
      if (bytes < 1024 ** 3) return (bytes / 1024 ** 2).toFixed(1) + ' MB';
      return (bytes / 1024 ** 3).toFixed(2) + ' GB';
    },

    /**
     * Format an ETA in seconds as M:SS or H:MM:SS.
     * @param {number|null} secs
     * @returns {string}
     */
    formatEta(secs) {
      if (secs == null || secs < 0) return '—';
      const h = Math.floor(secs / 3600);
      const m = Math.floor((secs % 3600) / 60);
      const s = Math.floor(secs % 60);
      if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
      return `${m}:${String(s).padStart(2,'0')}`;
    },

    /**
     * Format a download speed in bytes/s as a human-readable string.
     * @param {number|null} bps
     * @returns {string}
     */
    formatSpeed(bps) {
      if (bps == null) return '';
      return this.formatBytes(bps) + '/s';
    },

    /**
     * Format a nanosecond timestamp as a locale time string.
     * @param {number|null} ns
     * @returns {string}
     */
    formatTime(ns) {
      if (!ns) return '';
      return new Date(ns / 1e6).toLocaleString();
    },

    /**
     * Badge label for a download's type + format + quality.
     * @param {Download} dl
     * @returns {string}
     */
    badge(dl) {
      const parts = [dl.download_type];
      if (dl.format && dl.format !== 'any') parts.push(dl.format);
      if (dl.quality && dl.quality !== 'best') parts.push(dl.quality);
      return parts.join(' · ');
    },
  };
}
