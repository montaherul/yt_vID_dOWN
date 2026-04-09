const form = document.getElementById("downloadForm");
const urlInput = document.getElementById("urlInput");
const downloadPathInput = document.getElementById("downloadPathInput");
const chooseFolderButton = document.getElementById("chooseFolderButton");
const qualitySelect = document.getElementById("qualitySelect");
const downloadButton = document.getElementById("downloadButton");
const buttonSpinner = document.getElementById("buttonSpinner");
const liveSpinner = document.getElementById("liveSpinner");
const previewBox = document.getElementById("previewBox");
const previewText = document.getElementById("previewText");
const statusCard = document.getElementById("statusCard");
const statusTitle = document.getElementById("statusTitle");
const statusMessage = document.getElementById("statusMessage");
const progressText = document.getElementById("progressText");
const progressExtra = document.getElementById("progressExtra");
const progressFill = document.getElementById("progressFill");
const saveLocation = document.getElementById("saveLocation");

const LOCAL_API_ORIGIN = "http://127.0.0.1:5000";
const DEPLOYED_API_ORIGIN = "https://youtube-video-download-fbdm.onrender.com";
let pollTimer = null;
let previewTimer = null;
let previewRequestToken = 0;
let isBusy = false;

function resolveApiBase() {
    const { protocol, hostname, port } = window.location;
    const isFlaskLocalHost =
        (hostname === "127.0.0.1" || hostname === "localhost") &&
        port === "5000";
    const isRenderHost = hostname === "youtube-video-download-fbdm.onrender.com";

    if (isRenderHost) {
        return "";
    }

    if (isFlaskLocalHost) {
        return "";
    }

    if (protocol === "file:") {
        return DEPLOYED_API_ORIGIN;
    }

    if (hostname === "127.0.0.1" || hostname === "localhost") {
        return LOCAL_API_ORIGIN;
    }

    return DEPLOYED_API_ORIGIN;
}

const API_BASE = resolveApiBase();
const API_LABEL = API_BASE || window.location.origin;
const SAVED_PATH_KEY = "yt-downloader-download-path";

function apiUrl(path) {
    return `${API_BASE}${path}`;
}

async function readJson(response) {
    const rawText = await response.text();

    try {
        return JSON.parse(rawText);
    } catch (error) {
        return {
            success: false,
            message: rawText || "The server returned an unexpected response.",
        };
    }
}

function setBusyState(busy) {
    isBusy = busy;
    downloadButton.disabled = busy;
    chooseFolderButton.disabled = busy;
    buttonSpinner.classList.toggle("hidden", !busy);
    liveSpinner.classList.toggle("hidden", !busy);
}

function showPreview(message) {
    previewBox.classList.remove("hidden");
    previewText.textContent = message;
}

function hidePreview() {
    previewBox.classList.add("hidden");
    previewText.textContent = "";
}

function showStatus() {
    statusCard.classList.remove("hidden");
}

function updateStatusCard({ title, message, percent, extra, location, state }) {
    showStatus();

    statusTitle.textContent = title || "Preparing download";
    statusMessage.textContent = message || "Working...";
    progressText.textContent = `${Math.max(0, Math.min(100, Number(percent || 0))).toFixed(1)}%`;
    progressExtra.textContent = extra || "Starting";
    progressFill.style.width = `${Math.max(0, Math.min(100, Number(percent || 0)))}%`;
    saveLocation.textContent = location || "";

    statusCard.classList.remove("status-success", "status-error");

    if (state === "completed") {
        statusCard.classList.add("status-success");
    }

    if (state === "failed") {
        statusCard.classList.add("status-error");
    }
}

function clearPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

function persistDownloadPath() {
    const value = downloadPathInput.value.trim();

    if (value) {
        localStorage.setItem(SAVED_PATH_KEY, value);
        return;
    }

    localStorage.removeItem(SAVED_PATH_KEY);
}

async function chooseFolder() {
    try {
        chooseFolderButton.disabled = true;
        chooseFolderButton.textContent = "Opening...";

        const response = await fetch(apiUrl("/select-folder"), {
            method: "POST",
        });
        const data = await readJson(response);

        if (!response.ok || !data.success) {
            updateStatusCard({
                title: "Folder selection failed",
                message: data.message || "Could not open the folder picker.",
                percent: 0,
                extra: "Folder",
                state: "failed",
            });
            return;
        }

        downloadPathInput.value = data.path || "";
        persistDownloadPath();

        updateStatusCard({
            title: "Folder selected",
            message: `Downloads will be saved to ${data.path}`,
            percent: 0,
            extra: "Ready",
            location: `Save folder: ${data.path}`,
            state: "queued",
        });
    } catch (error) {
        updateStatusCard({
            title: "Folder selection failed",
            message: `Could not connect to the backend at ${API_LABEL}.`,
            percent: 0,
            extra: "Offline",
            state: "failed",
        });
    } finally {
        chooseFolderButton.disabled = isBusy;
        chooseFolderButton.textContent = "Browse";
    }
}

async function fetchVideoInfo(url) {
    const trimmedUrl = url.trim();

    if (!trimmedUrl) {
        hidePreview();
        return;
    }

    const requestToken = ++previewRequestToken;
    showPreview("Checking video information...");

    try {
        const response = await fetch(apiUrl("/info"), {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ url: trimmedUrl }),
        });

        const data = await readJson(response);

        if (requestToken !== previewRequestToken) {
            return;
        }

        if (!response.ok || !data.success) {
            showPreview(data.message || "Could not read this YouTube link.");
            return;
        }

        if (data.is_playlist) {
            showPreview(`Playlist: ${data.title} (${data.entry_count} items)`);
            return;
        }

        showPreview(`Video: ${data.title}`);
    } catch (error) {
        if (requestToken !== previewRequestToken) {
            return;
        }

        showPreview(`Could not connect to the backend at ${API_LABEL}.`);
    }
}

async function pollStatus(jobId) {
    try {
        const response = await fetch(apiUrl(`/status/${jobId}`));
        const data = await readJson(response);

        if (!response.ok || !data.success) {
            updateStatusCard({
                title: "Download failed",
                message: data.message || "Could not retrieve the download status.",
                percent: 0,
                extra: "Error",
                state: "failed",
            });
            setBusyState(false);
            clearPolling();
            return;
        }

        const job = data.job;
        const extraParts = [];

        if (job.speed_text) {
            extraParts.push(job.speed_text);
        }

        if (job.eta_text) {
            extraParts.push(`ETA ${job.eta_text}`);
        }

        if (job.is_playlist && job.entry_count) {
            extraParts.unshift(`${job.completed_items || 0}/${job.entry_count} items`);
        }

        updateStatusCard({
            title: job.title || "Preparing download",
            message: job.message,
            percent: job.percent,
            extra: extraParts.join(" | ") || job.state,
            location: job.save_location || "",
            state: job.state,
        });

        if (job.state === "completed" || job.state === "failed") {
            setBusyState(false);
            clearPolling();
        }
    } catch (error) {
        updateStatusCard({
            title: "Connection problem",
            message: `Lost connection while reading the download status from ${API_LABEL}.`,
            percent: 0,
            extra: "Offline",
            state: "failed",
        });
        setBusyState(false);
        clearPolling();
    }
}

function startPolling(jobId) {
    clearPolling();
    pollStatus(jobId);
    pollTimer = setInterval(() => pollStatus(jobId), 1000);
}

urlInput.addEventListener("input", () => {
    clearTimeout(previewTimer);

    if (!urlInput.value.trim()) {
        previewRequestToken += 1;
        hidePreview();
        return;
    }

    previewTimer = setTimeout(() => {
        fetchVideoInfo(urlInput.value);
    }, 700);
});

downloadPathInput.addEventListener("change", persistDownloadPath);
downloadPathInput.addEventListener("blur", persistDownloadPath);
chooseFolderButton.addEventListener("click", chooseFolder);

form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const url = urlInput.value.trim();
    const quality = qualitySelect.value;
    const downloadPath = downloadPathInput.value.trim();

    if (!url || isBusy) {
        return;
    }

    persistDownloadPath();

    setBusyState(true);
    updateStatusCard({
        title: "Preparing download",
        message: "Checking the link and creating the download job...",
        percent: 0,
        extra: "Starting",
        state: "queued",
    });

    try {
        const response = await fetch(apiUrl("/download"), {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ url, quality, download_path: downloadPath }),
        });

        const data = await readJson(response);

        if (!response.ok || !data.success) {
            updateStatusCard({
                title: "Download failed",
                message: data.message || "The download could not be started.",
                percent: 0,
                extra: "Error",
                state: "failed",
            });
            setBusyState(false);
            return;
        }

        const preparingMessage = data.duplicate
            ? data.message
            : `Queued: ${data.title}`;

        updateStatusCard({
            title: data.title || "Preparing download",
            message: preparingMessage,
            percent: 0,
            extra: data.is_playlist ? `${data.entry_count} items` : "Queued",
            location: data.save_location || "",
            state: "queued",
        });

        startPolling(data.job_id);
    } catch (error) {
        updateStatusCard({
            title: "Server error",
            message: `Could not connect to the backend at ${API_LABEL}.`,
            percent: 0,
            extra: "Offline",
            state: "failed",
        });
        setBusyState(false);
    }
});

const savedPath = localStorage.getItem(SAVED_PATH_KEY);
if (savedPath) {
    downloadPathInput.value = savedPath;
}
