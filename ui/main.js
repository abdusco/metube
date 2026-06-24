/**
 * @typedef {'queued'|'running'|'finished'|'error'|'canceled'} DownloadStatus
 *
 * @typedef {object} Download
 * @property {string}         id
 * @property {string}         title
 * @property {string}         url
 * @property {'video'|'audio'} download_type
 * @property {string}         codec         - auto | h264 | h265 | av1 | vp9
 * @property {string}         format        - mp4 | any | m4a | mp3 | …
 * @property {string}         quality       - best | 1080 | 720 | …
 * @property {DownloadStatus} status
 * @property {string|null}    message
 * @property {number|null}    percent       - 0–100
 * @property {number|null}    speed         - bytes/s
 * @property {number|null}    eta           - seconds
 * @property {string|null}    filename
 * @property {number|null}    size          - bytes
 * @property {string}         created_at    - ISO 8601 datetime
 * @property {string}         updated_at    - ISO 8601 datetime
 * @property {string|null}    started_at    - ISO 8601 datetime
 * @property {string|null}    finished_at   - ISO 8601 datetime
 * @property {string|null}    error
 * @property {Array<{filename:string,size:number,download_url:string}>} [subtitle_files]
 * @property {string|null}    download_url
 * @property {string[]}       [subtitle_langs]
 */

/**
 * @typedef {'light'|'dark'|'auto'} Theme
 */

function app() {
  return {
    url: "",
    downloadType: /** @type {'video'|'audio'} */ ("video"),
    codec: "auto",
    format: "mp4",
    quality: "best",

    /** @type {Download[]} */
    queue: [],
    /** @type {Download[]} */
    done: [],

    adding: false,
    /** @type {string|null} */
    error: null,

    /** @type {string[]} */
    cookieDomains: [],
    cookieUploading: false,
    cookieText: "",
    cookieTab: /** @type {'file'|'paste'} */ ("file"),

    theme: /** @type {Theme} */ (
      localStorage.getItem("metube_theme") || "auto"
    ),

    subtitleLangsInput: "",

    /** @type {string|null} id of the download whose log panel is open */
    openLogsId: null,
    /** @type {Object<string, string[]>} */
    logs: {},
    _logsTimer: null,

    _polling: false,
    _mq: null,

    async init() {
      this._applyTheme();
      this._mq = window.matchMedia("(prefers-color-scheme: dark)");
      this._mq.addEventListener("change", () => this._applyTheme());

      await Promise.all([this._refreshCookieStatus(), this.pollState()]);

      setInterval(() => this.pollState(), 2000);
    },

    async pollState() {
      if (this._polling) return;
      this._polling = true;
      try {
        const resp = await fetch("jobs");
        if (!resp.ok) return;
        const data = await resp.json();
        this.queue = (data.queued || []).slice().reverse();
        this.done = (data.done || []).slice().reverse();
      } catch {
        /* network blip – keep previous state */
      } finally {
        this._polling = false;
      }
    },

    async addDownload() {
      if (!this.url.trim()) return;

      const { langs, error: langsError } = this._parseSubtitleLangs();
      if (langsError) {
        this.error = langsError;
        return;
      }

      this.adding = true;
      this.error = null;
      try {
        const resp = await fetch("jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url: this.url.trim(),
            download_type: this.downloadType,
            codec: this.downloadType === "video" ? this.codec : "auto",
            format: this.format,
            quality: this.quality,
            subtitle_langs: langs,
          }),
        });
        const data = await resp.json();
        if (data.status === "ok") {
          this.url = "";
          await this.pollState();
        } else {
          this.error = data.message || "Failed to add download";
        }
      } catch (e) {
        this.error = String(e);
      } finally {
        this.adding = false;
      }
    },

    /**
     * Cancel a queued/running download.
     * @param {string} id
     */
    async cancelDownload(id) {
      await fetch(`jobs/${id}`, { method: "DELETE" });
      await this.pollState();
    },

    /** Clear all completed downloads. */
    async clearCompletedJobs() {
      await fetch("jobs/clear", { method: "POST" });
      await this.pollState();
    },

    /**
     * Re-add a failed download with its original parameters.
     * @param {Download} dl
     */
    async retryDownload(dl) {
      await fetch(`jobs/${dl.id}/retry`, { method: "POST" });
      await this.pollState();
    },

    async _refreshCookieStatus() {
      try {
        const resp = await fetch("cookies");
        if (resp.ok) {
          const data = await resp.json();
          this.cookieDomains = data.domains || [];
        }
      } catch {
        /* ignore */
      }
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
        fd.append("cookies", file);
        await fetch("cookies", { method: "POST", body: fd });
        await this._refreshCookieStatus();
        input.value = "";
      } finally {
        this.cookieUploading = false;
      }
    },

    async uploadCookieText() {
      if (!this.cookieText.trim()) return;
      this.cookieUploading = true;
      try {
        const blob = new Blob([this.cookieText], { type: "text/plain" });
        const fd = new FormData();
        fd.append("cookies", blob, "cookies.txt");
        await fetch("cookies", { method: "POST", body: fd });
        await this._refreshCookieStatus();
        this.cookieText = "";
      } finally {
        this.cookieUploading = false;
      }
    },

    async deleteCookies() {
      await fetch("cookies", { method: "DELETE" });
      await this._refreshCookieStatus();
    },

    /** @param {string} domain */
    async deleteCookiesForDomain(domain) {
      await fetch(`cookies/${encodeURIComponent(domain)}`, {
        method: "DELETE",
      });
      await this._refreshCookieStatus();
    },

    /**
     * Parse and validate the subtitle language input.
     * @returns {{ langs: string[], error: string|null }}
     */
    _parseSubtitleLangs() {
      const raw = this.subtitleLangsInput.trim();
      if (!raw) return { langs: [], error: null };
      const langs = raw
        .split(/\s*,\s*/)
        .map((s) => s.trim())
        .filter(Boolean);
      const invalid = langs.filter(
        (l) => !/^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$/.test(l),
      );
      if (invalid.length)
        return {
          langs: [],
          error: `Invalid subtitle language codes: ${invalid.join(", ")}`,
        };
      return { langs, error: null };
    },

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
        const resp = await fetch("logs?id=" + encodeURIComponent(id));
        if (resp.ok) {
          const data = await resp.json();
          this.logs = { ...this.logs, [id]: data.lines };
        }
      } catch {}
    },

    /** @param {Theme} t */
    setTheme(t) {
      this.theme = t;
      localStorage.setItem("metube_theme", t);
      this._applyTheme();
    },

    _applyTheme() {
      document.documentElement.dataset.theme = this.resolvedTheme;
    },

    get stats() {
      return {
        active: this.queue.filter((d) => d.status === "running").length,
        queued: this.queue.filter((d) => d.status === "queued").length,
        done: this.done.filter((d) => d.status === "finished").length,
        failed: this.done.filter((d) => d.status === "error").length,
      };
    },

    /** @returns {Theme} */
    get resolvedTheme() {
      if (this.theme !== "auto") return this.theme;
      return this._mq?.matches ? "dark" : "light";
    },

    /** Format options depending on download type. */
    get formatOptions() {
      return this.downloadType === "video"
        ? [
            { v: "mp4", l: "MP4" },
            { v: "any", l: "Any" },
            { v: "ios", l: "iOS" },
          ]
        : [
            { v: "m4a", l: "M4A" },
            { v: "mp3", l: "MP3" },
            { v: "opus", l: "Opus" },
            { v: "wav", l: "WAV" },
            { v: "flac", l: "FLAC" },
          ];
    },

    /** Quality options depending on download type. */
    get qualityOptions() {
      return this.downloadType === "video"
        ? [
            { v: "best", l: "Best" },
            { v: "1080", l: "1080p" },
            { v: "720", l: "720p" },
            { v: "480", l: "480p" },
            { v: "360", l: "360p" },
            { v: "240", l: "240p" },
          ]
        : [
            { v: "best", l: "Best" },
            { v: "320K", l: "320K" },
            { v: "192K", l: "192K" },
            { v: "128K", l: "128K" },
          ];
    },

    /** Reset format and quality to sensible defaults when type changes. */
    onTypeChange() {
      this.format = this.downloadType === "video" ? "mp4" : "m4a";
      this.quality = "best";
    },

    /**
     * Format a byte count as a human-readable string.
     * @param {number|null} bytes
     * @returns {string}
     */
    formatBytes(bytes) {
      if (bytes == null) return "—";
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 ** 2) return (bytes / 1024).toFixed(1) + " KB";
      if (bytes < 1024 ** 3) return (bytes / 1024 ** 2).toFixed(1) + " MB";
      return (bytes / 1024 ** 3).toFixed(2) + " GB";
    },

    /**
     * Format an ETA in seconds as M:SS or H:MM:SS.
     * @param {number|null} secs
     * @returns {string}
     */
    formatEta(secs) {
      if (secs == null || secs < 0) return "—";
      const h = Math.floor(secs / 3600);
      const m = Math.floor((secs % 3600) / 60);
      const s = Math.floor(secs % 60);
      if (h > 0)
        return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
      return `${m}:${String(s).padStart(2, "0")}`;
    },

    /**
     * Format a download speed in bytes/s as a human-readable string.
     * @param {number|null} bps
     * @returns {string}
     */
    formatSpeed(bps) {
      if (bps == null) return "";
      return this.formatBytes(bps) + "/s";
    },

    /**
     * Format an ISO datetime string as a locale time string.
     * @param {string|null} iso
     * @returns {string}
     */
    formatTime(iso) {
      if (!iso) return "";
      const date = new Date(iso);
      if (Number.isNaN(date.getTime())) return "";
      return date.toLocaleString();
    },

    /**
     * Badge label for a download's type + format + quality.
     * @param {Download} dl
     * @returns {string}
     */
    badge(dl) {
      const parts = [dl.download_type];
      if (dl.format && dl.format !== "any") parts.push(dl.format);
      if (dl.quality && dl.quality !== "best") parts.push(dl.quality);
      return parts.join(" · ");
    },
  };
}
