"use strict";

const $ = (selector) => document.querySelector(selector);
const messages = JSON.parse($("#ui-text").dataset.messages);
const t = (key) => messages[key] || key;
const errorText = (code) => messages[`error.${code}`] || t("error.operation_failed");
const panels = [...document.querySelectorAll(".module-panel")];
const links = [...document.querySelectorAll("[data-module-target]")];
const nextButtons = [...document.querySelectorAll("[data-next-from]")];
const languageMenu = $(".language-menu");
const languageMenuToggle = $("#language-menu-toggle");
const languageMenuOptions = $("#language-menu-options");
const writeButtons = ["#analyze-account", "#generate-inventory", "#recover-inventory", "#diagnose-inventory", "#diagnose-downloads", "#check-connection", "#connect-chatgpt",
    "#save-cookie", "#change-storage-root", "#download-resources", "#clear-local-data"].map($);
const scopeDependentButtons = ["#generate-inventory", "#download-resources"].map($);
let active = false;
let submitting = false;
let seenJobs = new Map();
const analysisScope = {
    project_ids: [],
    include_unassigned: false,
};
const workflowState = {
    connected: false,
    sessionChecking: false,
    analysis: null,
    inventory: null,
    recoverableInventory: null,
    download: null,
    downloadScope: null,
    jobs: [],
};
const navigationStateClasses = [
    "state-locked", "state-ready", "state-running", "state-complete", "state-warning", "state-error",
];

const downloadSettings = {
    mode: "optimal", workers: 1, resource_delay: 0.5, safe_mode: true, auto_safe_mode: true,
};

function updateDownloadSettings() {
    const safeMode = $("#download-safe-mode").checked;
    const autoSafeMode = $("#download-auto-safe-mode");
    const advancedControls = $("#download-advanced-controls");
    const manual = $('input[name="download-mode"]:checked').value === "manual";
    const optimalSettings = $("#optimal-download-settings");
    const scopeCount = new Set(analysisScope.project_ids).size + Number(analysisScope.include_unassigned);
    const workers = Math.min(3, Math.max(1, scopeCount));
    const spanish = document.documentElement.lang === "es";
    const workerLabel = workers === 1 ? "worker" : "workers";
    const delayLabel = spanish ? "0,5 s entre recursos" : "0.5 s between resources";
    if (safeMode) {
        advancedControls.hidden = true;
        $("#download-settings").hidden = true;
        optimalSettings.hidden = false;
        document.querySelectorAll('input[name="download-mode"]').forEach((input) => {
            input.disabled = true;
        });
        $("#download-workers").disabled = true;
        $("#download-delay").disabled = true;
        autoSafeMode.disabled = true;
        downloadSettings.mode = manual ? "manual" : "optimal";
        downloadSettings.workers = 1;
        downloadSettings.resource_delay = 5.0;
        downloadSettings.safe_mode = true;
        downloadSettings.auto_safe_mode = autoSafeMode.checked;
        optimalSettings.textContent = t("download.safe_mode_settings");
        return;
    }
    advancedControls.hidden = false;
    document.querySelectorAll('input[name="download-mode"]').forEach((input) => {
        input.disabled = false;
    });
    autoSafeMode.disabled = false;
    $("#download-settings").hidden = !manual;
    optimalSettings.hidden = manual;
    $("#download-workers").disabled = !manual;
    $("#download-delay").disabled = !manual;
    downloadSettings.mode = manual ? "manual" : "optimal";
    downloadSettings.workers = manual ? $("#download-workers").valueAsNumber : 1;
    downloadSettings.resource_delay = manual ? $("#download-delay").valueAsNumber : 0.5;
    downloadSettings.safe_mode = false;
    downloadSettings.auto_safe_mode = autoSafeMode.checked;
    optimalSettings.textContent = `${workers} ${workerLabel} \u00b7 ${delayLabel}`;
}

$("#download-mode").addEventListener("change", updateDownloadSettings);
$("#download-safe-mode").addEventListener("change", updateDownloadSettings);
$("#download-auto-safe-mode").addEventListener("change", updateDownloadSettings);
$("#download-workers").addEventListener("input", updateDownloadSettings);
$("#download-delay").addEventListener("input", updateDownloadSettings);
updateDownloadSettings();

function notice(message = "") {
    $("#app-notice").textContent = message;
    $("#app-notice").hidden = !message;
}

const liveHelpToggle = $("#live-help-toggle");
const liveHelpBubble = document.createElement("div");
liveHelpBubble.id = "live-help-bubble";
liveHelpBubble.className = "live-help-bubble";
liveHelpBubble.setAttribute("role", "tooltip");
liveHelpBubble.hidden = true;
document.body.append(liveHelpBubble);
let liveHelpEnabled = false;
let liveHelpTarget = null;

function hideLiveHelp(target = liveHelpTarget) {
    if (target) target.removeAttribute("aria-describedby");
    liveHelpTarget = null;
    liveHelpBubble.classList.remove("is-visible");
    liveHelpBubble.hidden = true;
}

function showLiveHelp(target) {
    const message = target?.dataset.help;
    if (!liveHelpEnabled || !message || target === liveHelpTarget) return;
    hideLiveHelp();
    liveHelpTarget = target;
    liveHelpBubble.textContent = message;
    liveHelpBubble.hidden = false;
    target.setAttribute("aria-describedby", liveHelpBubble.id);
    requestAnimationFrame(() => {
        if (liveHelpTarget !== target) return;
        const rect = target.getBoundingClientRect();
        const bubble = liveHelpBubble.getBoundingClientRect();
        let top = rect.top - bubble.height - 10;
        let placement = "top";
        if (top < 8) {
            top = rect.bottom + 10;
            placement = "bottom";
        }
        const left = Math.min(
            Math.max(8, rect.left + (rect.width - bubble.width) / 2),
            window.innerWidth - bubble.width - 8,
        );
        liveHelpBubble.dataset.placement = placement;
        liveHelpBubble.style.left = `${left}px`;
        liveHelpBubble.style.top = `${top}px`;
        liveHelpBubble.classList.add("is-visible");
    });
}

function setLiveHelp(enabled) {
    liveHelpEnabled = enabled;
    liveHelpToggle.setAttribute("aria-pressed", String(enabled));
    liveHelpToggle.classList.toggle("is-active", enabled);
    if (!enabled) hideLiveHelp();
}

liveHelpToggle.addEventListener("click", () => setLiveHelp(!liveHelpEnabled));
document.addEventListener("pointerover", (event) => showLiveHelp(event.target.closest("[data-help]")));
document.addEventListener("pointerout", (event) => {
    const target = event.target.closest("[data-help]");
    if (target && !target.contains(event.relatedTarget)) hideLiveHelp(target);
});
document.addEventListener("focusin", (event) => showLiveHelp(event.target.closest("[data-help]")));
document.addEventListener("focusout", (event) => {
    const target = event.target.closest("[data-help]");
    if (target && !target.contains(event.relatedTarget)) hideLiveHelp(target);
});
window.addEventListener("resize", () => hideLiveHelp());

const cleanupHeldKeys = new Set();
let cleanupHoldStartedAt = null;
let cleanupHoldFrame = null;
let cleanupExecuting = false;

function resetCleanupHold() {
    cleanupHoldStartedAt = null;
    if (cleanupHoldFrame !== null) cancelAnimationFrame(cleanupHoldFrame);
    cleanupHoldFrame = null;
    $("#cleanup-confirm-progress").value = 0;
}

function closeCleanupModal() {
    resetCleanupHold();
    cleanupHeldKeys.clear();
    $("#cleanup-modal").hidden = true;
}

function resetLocalUi() {
    document.querySelectorAll('input[name="analysis-project"], input[name="analysis-root"]')
        .forEach((checkbox) => { checkbox.checked = false; });
    analysisScope.project_ids = [];
    analysisScope.include_unassigned = false;
    workflowState.analysis = null;
    workflowState.inventory = null;
    workflowState.recoverableInventory = null;
    workflowState.download = null;
    workflowState.downloadScope = null;
    $("#backup-results").hidden = true;
    $("#inventory-results").hidden = true;
    $("#inventory-diagnostics").hidden = true;
    $("#download-diagnostics").hidden = true;
    $("#diagnose-inventory").hidden = true;
    $("#diagnose-downloads").hidden = true;
    $("#recover-inventory").hidden = true;
    $("#download-results").hidden = true;
    $("#stop-download").hidden = true;
    $("#download-current-status").hidden = false;
    $("#download-status").hidden = false;
    $("#download-status").dataset.state = "pending";
    $("#download-progress-word").textContent = t("common.pending");
    $("#download-project").textContent = "-";
    $("#download-resource").textContent = "-";
    $("#download-progress").textContent = "0 / 0";
    $("#download-total-progress").hidden = true;
    renderProcessProgress("download", 0, 0);
    for (const prefix of ["backup", "inventory"]) renderProcessProgress(prefix, 0, 0);
    $("#download-worker-list").replaceChildren();
    $("#download-worker-list").hidden = true;
    seenJobs.clear();
    updateAnalysisScopeUI();
}

function runCleanupConfirmation(timestamp) {
    if (!cleanupHeldKeys.has("a") || !cleanupHeldKeys.has("l")) {
        resetCleanupHold();
        return;
    }
    if (cleanupHoldStartedAt === null) cleanupHoldStartedAt = timestamp;
    const elapsed = Math.min(4000, timestamp - cleanupHoldStartedAt);
    $("#cleanup-confirm-progress").value = elapsed;
    if (elapsed < 4000) {
        cleanupHoldFrame = requestAnimationFrame(runCleanupConfirmation);
        return;
    }
    cleanupHoldFrame = null;
    cleanupExecuting = true;
    submit(async () => {
        await api("/api/system/cleanup", {
            include_downloaded: $("#cleanup-include-downloads").checked,
        });
        closeCleanupModal();
        resetLocalUi();
        const spanish = document.documentElement.lang === "es";
        notice(spanish ? "Datos locales eliminados." : "Local data cleared.");
    }).finally(() => { cleanupExecuting = false; });
}

function startCleanupHold() {
    if (cleanupExecuting || cleanupHoldStartedAt !== null) return;
    cleanupHoldFrame = requestAnimationFrame(runCleanupConfirmation);
}

function lockButtons() {
    writeButtons.forEach((button) => {
        button.disabled = active || submitting ||
            (scopeDependentButtons.includes(button) && !hasValidAnalysisScope());
    });
    updateNavigationStates();
}

function showModule() {
    const requested = location.hash.slice(1);
    let id = panels.some((panel) => panel.id === requested) ? requested : "sistema";
    if (!isStageAccessible(id)) id = "sistema";
    const activePanel = panels.find((panel) => panel.id === id);
    panels.forEach((panel) => {
        panel.hidden = panel !== activePanel;
        panel.classList.remove("is-entering");
    });
    void activePanel.offsetWidth;
    activePanel.classList.add("is-entering");
    activePanel.addEventListener("animationend", () => activePanel.classList.remove("is-entering"), {once: true});
    links.forEach((link) => {
        if (link.dataset.moduleTarget === id) {
            link.setAttribute("aria-current", "page");
        }
        else link.removeAttribute("aria-current");
    });
    document.querySelectorAll("#language-menu-options a").forEach((option) => {
        option.href = `${option.href.split("#")[0]}#${id}`;
    });
}
window.addEventListener("hashchange", showModule);
links.forEach((link) => link.addEventListener("click", (event) => {
    if (link.dataset.navigationState !== "locked") return;
    event.preventDefault();
}));
nextButtons.forEach((button) => button.addEventListener("click", () => {
    if (button.disabled) return;
    location.hash = `#${button.dataset.nextTarget}`;
}));
languageMenuToggle.addEventListener("click", () => {
    const expanded = languageMenuToggle.getAttribute("aria-expanded") === "true";
    languageMenuToggle.setAttribute("aria-expanded", String(!expanded));
    languageMenuOptions.hidden = expanded;
});
document.addEventListener("click", (event) => {
    if (!languageMenu.contains(event.target)) {
        languageMenuToggle.setAttribute("aria-expanded", "false");
        languageMenuOptions.hidden = true;
    }
});
window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || languageMenuOptions.hidden) return;
    languageMenuToggle.setAttribute("aria-expanded", "false");
    languageMenuOptions.hidden = true;
    languageMenuToggle.focus();
});
const aboutModal = $("#about-modal");

function closeAboutModal() {
    aboutModal.hidden = true;
}

$("#about-logo").addEventListener("click", () => {
    aboutModal.hidden = false;
    $("#about-close").focus();
});
$("#about-close").addEventListener("click", closeAboutModal);
document.querySelector("[data-about-close]").addEventListener("click", closeAboutModal);
window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !aboutModal.hidden) closeAboutModal();
});
showModule();

async function api(path, body) {
    let response;
    try {
        response = await fetch(path, body === undefined ? {cache: "no-store"} : {
            method: "POST", headers: {"Content-Type": "application/json", "X-OAI-UI": "1"},
            body: JSON.stringify(body),
        });
    } catch { throw new Error(t("error.network")); }
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(errorText(data.error));
    }
    return response.json();
}

const checkUpdatesButton = $("#check-updates");
if (checkUpdatesButton) {
    checkUpdatesButton.addEventListener("click", async () => {
        const result = $("#update-check-result");
        const link = $("#view-update");
        checkUpdatesButton.disabled = true;
        result.textContent = t("about.checking_updates");
        result.hidden = false;
        link.hidden = true;
        try {
            const update = await api("/api/about/updates", {});
            if (update.status === "available") {
                result.textContent = t("about.update_available").replace("{version}", update.version);
                link.href = update.release_url;
                link.hidden = false;
            } else {
                result.textContent = t("about.up_to_date");
            }
        } catch {
            result.textContent = t("about.update_check_failed");
        } finally {
            checkUpdatesButton.disabled = false;
        }
    });
}

function renderSession(data) {
    const connected = Boolean(data.authenticated);
    workflowState.sessionChecking = false;
    workflowState.connected = connected;
    const status = $("#system-status");
    status.textContent = connected ? t("system.connected") :
        (data.cookie_found ? t("system.invalid_session") : t("system.no_credentials"));
    status.dataset.state = connected ? "complete" : "pending";
    for (const [field, id] of [["name", "user"], ["email", "email"]]) {
        const value = data.user?.[field] || "";
        $(`#system-${id}`).textContent = value;
        $(`#system-${id}-row`).hidden = !value;
    }
    $("#connection-action").hidden = false;
    if (connected) $("#manual-cookie").hidden = true;
    updateNavigationStates();
}

async function loadSession() {
    workflowState.sessionChecking = true;
    $("#system-status").textContent = t("system.checking");
    $("#system-status").dataset.state = "working";
    updateNavigationStates();
    try {
        renderSession(await api("/api/system/status"));
    } catch (error) {
        workflowState.sessionChecking = false;
        workflowState.connected = false;
        $("#system-status").dataset.state = "error";
        $("#connection-action").hidden = false;
        updateNavigationStates();
        throw error;
    }
}

function renderStorage(data) {
    $("#storage-root").textContent = data.storage_root;
    if (Number.isFinite(Number(data.analysis_request_delay))) {
        $("#analysis-request-delay").value = data.analysis_request_delay;
    }
}

function syncAnalysisScope() {
    const checkboxes = [...document.querySelectorAll('input[name="analysis-project"]')];
    const root = $('input[name="analysis-root"]');
    analysisScope.project_ids = checkboxes.filter((checkbox) => checkbox.checked)
        .map((checkbox) => checkbox.value);
    analysisScope.include_unassigned = Boolean(root?.checked);
    $("#analysis-select-all").checked = Boolean(root?.checked) &&
        checkboxes.every((checkbox) => checkbox.checked);
    updateAnalysisScopeUI();
}

function hasValidAnalysisScope() {
    return analysisScope.include_unassigned || analysisScope.project_ids.length > 0;
}

function scopeSnapshot() {
    return {
        project_ids: [...analysisScope.project_ids].sort(),
        include_unassigned: analysisScope.include_unassigned,
    };
}

function scopesMatch(left, right) {
    if (!left || !right || Boolean(left.include_unassigned) !== Boolean(right.include_unassigned)) {
        return false;
    }
    const leftIds = [...(left.project_ids || [])].map(String).sort();
    const rightIds = [...(right.project_ids || [])].map(String).sort();
    return leftIds.length === rightIds.length && leftIds.every((id, index) => id === rightIds[index]);
}

function hasSelectableAnalysis() {
    const analysis = workflowState.analysis;
    if (!analysis) return false;
    const rootCount = Number(analysis.root_conversation_count ?? analysis.standalone_conversations ?? 0);
    return rootCount > 0 || (analysis.projects || []).some(
        (project) => Number(project.conversation_count || 0) > 0
    );
}

function hasCurrentInventory() {
    return hasValidAnalysisScope() && scopesMatch(workflowState.inventory?.scope, scopeSnapshot());
}

function hasUsableCurrentInventory() {
    return hasCurrentInventory() && workflowState.inventory.usable === true;
}

function setRecoverableInventory(result) {
    workflowState.recoverableInventory = result?.usable === true ? result : null;
    $("#recover-inventory").hidden = !workflowState.recoverableInventory;
}

function runningJob(kind) {
    return workflowState.jobs.find((job) => job.kind === kind && job.status === "running");
}

function updateInventoryActions(inventoryRunning = Boolean(runningJob("inventory"))) {
    const inventory = workflowState.inventory;
    const inventoryErrors = Array.isArray(inventory?.errors) ? inventory.errors.length : 0;
    $("#generate-inventory").hidden = inventoryRunning;
    $("#recover-inventory").hidden = inventoryRunning || !workflowState.recoverableInventory;
    $("#diagnose-inventory").hidden = inventoryRunning || !hasCurrentInventory() || inventoryErrors === 0;
    $("#diagnose-downloads").hidden = inventoryRunning || !hasUsableCurrentInventory() ||
        Number(inventory?.diagnostic_failures || 0) === 0;
    nextButtons.filter((button) => button.dataset.nextFrom === "inventario")
        .forEach((button) => { button.hidden = inventoryRunning; });
}

function navigationStateFor(id) {
    const backupJob = runningJob("backup");
    const inventoryJob = runningJob("inventory");
    const downloadJob = runningJob("download");

    if (id === "sistema") {
        return workflowState.sessionChecking || runningJob("connection") ? "running" :
            workflowState.connected ? "complete" : "ready";
    }
    if (id === "backup") {
        if (!workflowState.connected) return "locked";
        if (backupJob) return "running";
        if (!hasSelectableAnalysis()) return "ready";
        return workflowState.analysis.partial ? "warning" : "complete";
    }
    if (id === "inventario") {
        if (!workflowState.connected ||
                (!hasSelectableAnalysis() && !workflowState.recoverableInventory)) return "locked";
        if (inventoryJob) return "running";
        if (!hasCurrentInventory()) return "ready";
        if (!hasUsableCurrentInventory()) return "error";
        return workflowState.inventory.partial || (workflowState.inventory.errors || []).length ?
            "warning" : "complete";
    }
    if (id === "descarga") {
        if (!workflowState.connected || !hasUsableCurrentInventory()) return "locked";
        if (downloadJob) return "running";
        if (!workflowState.download || !scopesMatch(workflowState.downloadScope, scopeSnapshot())) {
            return "ready";
        }
        return "complete";
    }
    return "ready";
}

function isStageAccessible(id) {
    return navigationStateFor(id) !== "locked";
}

function canAdvanceFrom(section) {
    if (section === "sistema") return workflowState.connected;
    if (section === "backup") {
        return workflowState.connected && hasSelectableAnalysis() && hasValidAnalysisScope() &&
            !workflowState.analysis.partial && !runningJob("backup");
    }
    if (section === "inventario") {
        return hasUsableCurrentInventory() && !runningJob("inventory");
    }
    return false;
}

function updateNextButtons() {
    nextButtons.forEach((button) => {
        button.disabled = !canAdvanceFrom(button.dataset.nextFrom);
    });
}

function updateNavigationStates() {
    links.forEach((link) => {
        const state = navigationStateFor(link.dataset.moduleTarget);
        link.classList.remove(...navigationStateClasses);
        link.classList.add(`state-${state}`);
        link.dataset.navigationState = state;
        link.setAttribute("aria-disabled", String(state === "locked"));
        link.tabIndex = state === "locked" ? -1 : 0;
    });
    updateNextButtons();
    updateInventoryActions();
}

function updateAnalysisScopeUI() {
    const status = $("#analysis-scope-status");
    const root = $('input[name="analysis-root"]');
    const selectedProjects = analysisScope.project_ids.length;
    const selectedConversations = [...document.querySelectorAll('input[name="analysis-project"]:checked')]
        .reduce((total, checkbox) => total + Number(checkbox.dataset.conversationCount || 0), 0);
    const rootConversations = analysisScope.include_unassigned ?
        Number(root?.dataset.conversationCount || 0) : 0;
    const summary = analysisScope.include_unassigned ?
        (selectedProjects ? `${selectedProjects} ${status.dataset.projectsRootLabel}` : status.dataset.rootLabel) :
        (selectedProjects ? `${selectedProjects} ${status.dataset.projectsLabel}` : status.dataset.emptyLabel);

    $("#analysis-scope-summary").textContent = summary;
    $("#analysis-scope-conversations").textContent = selectedConversations + rootConversations;
    $("#analysis-scope-conversations-label").textContent = status.dataset.conversationsLabel;
    if (!hasCurrentInventory()) {
        $("#diagnose-inventory").hidden = true;
        $("#diagnose-downloads").hidden = true;
        $("#inventory-diagnostics").hidden = true;
        $("#download-diagnostics").hidden = true;
    }
    updateDownloadSettings();
    lockButtons();
}

$("#backup-results").addEventListener("change", (event) => {
    const target = event.target;
    if (target.id === "analysis-select-all") {
        const root = $('input[name="analysis-root"]');
        root.checked = target.checked;
        document.querySelectorAll('input[name="analysis-project"]').forEach((checkbox) => {
            checkbox.checked = target.checked;
        });
    } else if (target.name !== "analysis-root" && target.name !== "analysis-project") return;
    syncAnalysisScope();
});

function renderBackup(result) {
    if (!result) return;
    workflowState.analysis = result;
    const totalScopes = result.total === undefined ?
        Math.max(0, Number(result.total_projects) || 0) + 1 :
        Math.max(0, Number(result.total) || 0);
    const completedScopes = result.current === undefined ?
        totalScopes : Math.max(0, Number(result.current) || 0);
    renderProcessProgress("backup", completedScopes, totalScopes);
    $("#backup-results").hidden = false;
    $("#backup-total-projects").textContent = result.total_projects;
    $("#backup-total-conversations").textContent = result.total_conversations;
    $("#backup-project-list").replaceChildren();
    const rootRow = document.createElement("div");
    rootRow.className = "root-scope";
    const rootName = document.createElement("dt");
    const rootCount = document.createElement("dd");
    const rootLabel = document.createElement("label");
    const rootCheckbox = document.createElement("input");
    rootCheckbox.type = "checkbox";
    rootCheckbox.name = "analysis-root";
    rootLabel.dataset.help = t("help.scope_projects");
    rootCheckbox.checked = analysisScope.include_unassigned;
    rootCheckbox.dataset.conversationCount = result.root_conversation_count || 0;
    rootLabel.append(rootCheckbox, document.createTextNode(` ${$("#backup-project-list").dataset.rootLabel}`));
    rootName.append(rootLabel);
    rootCount.textContent = `${rootCheckbox.dataset.conversationCount} ${t("backup.conversations")}`;
    rootRow.append(rootName, rootCount);
    $("#backup-project-list").append(rootRow);
    for (const project of result.projects || []) {
        const row = document.createElement("div");
        const name = document.createElement("dt");
        const count = document.createElement("dd");
        const label = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.name = "analysis-project";
        label.dataset.help = t("help.scope_projects");
        checkbox.value = project.id;
        checkbox.checked = analysisScope.project_ids.includes(checkbox.value);
        checkbox.dataset.conversationCount = project.conversation_count;
        label.append(checkbox, document.createTextNode(` ${project.name}`));
        name.append(label);
        count.textContent = `${project.conversation_count} ${t("backup.conversations")}`;
        row.append(name, count);
        $("#backup-project-list").append(row);
    }
    syncAnalysisScope();
}

function renderInventory(result) {
    if (!result || result.conversation_files_scanned === undefined) return;
    workflowState.inventory = result;
    setRecoverableInventory(result);
    const inventoryCurrent = result.current ?? result.conversation_files_scanned;
    const inventoryTotal = result.total ?? result.conversation_files_scanned;
    renderProcessProgress("inventory", inventoryCurrent, inventoryTotal);
    $("#download-diagnostics").hidden = true;
    $("#inventory-diagnostics").hidden = true;
    $("#inventory-results").hidden = false;
    $("#inventory-conversations").textContent = result.conversation_files_scanned;
    $("#inventory-unique-resources").textContent = result.resource_count;
    $("#inventory-total-references").textContent = result.total_references;
    const inventoryErrors = Array.isArray(result.errors) ? result.errors.length : 0;
    const downloadErrors = result.diagnostic_failures || 0;
    const comparison = result.database_comparison || {};
    const downloadStatus = result.download_status || {};
    $("#inventory-indexed-conversations").textContent = comparison.known_conversations ?? 0;
    $("#inventory-indexed-resources").textContent = comparison.known_resources ?? 0;
    $("#inventory-new-conversations").textContent = comparison.new_conversations ?? result.conversation_files_scanned;
    $("#inventory-new-resources").textContent = comparison.new_resources ?? result.resource_count;
    $("#inventory-pending-downloads").textContent = downloadStatus.pending ?? 0;
    $("#inventory-failed-downloads").textContent = downloadStatus.failed ?? 0;
    $("#diagnose-inventory").hidden = inventoryErrors === 0;
    $("#diagnose-downloads").hidden = downloadErrors === 0;
    updateNavigationStates();
}

function renderDownloadWorker(row, worker) {
    row.dataset.help = t("help.download_progress");
    const total = Number(worker.total);
    const validTotal = Number.isFinite(total) && total > 0;
    const completed = worker.status === "completed";
    const current = Math.max(0, Number(worker.current) || 0);
    const unavailable = Math.max(0, Number(worker.unavailable) || 0);
    const value = completed && validTotal ? total : Math.min(current, total);
    const status = completed && unavailable > 0 ?
        $("#download-worker-list").dataset.completedWithIncidents :
        completed ? t("common.completed") :
        worker.status === "error" ? "Error" : t(`phase.${worker.phase}`);
    const title = document.createElement("h3");
    title.textContent = `${t("download.thread")} ${worker.worker_id}`;
    row.append(title);

    const details = [
        [t("download.project"), worker.current_project || t("download.root")],
        [t("download.resource"), worker.current_resource || "-"],
        [t("download.progress"), `${current} / ${validTotal ? total : "-"}`],
        [t("common.status"), status],
    ];
    if (unavailable > 0) {
        details.push([$("#download-worker-list").dataset.unavailableLabel, unavailable]);
    }
    for (const [label, detail] of details) {
        const field = document.createElement("p");
        if (label === $("#download-worker-list").dataset.unavailableLabel) {
            field.className = "download-unavailable";
        }
        const name = document.createElement("strong");
        name.textContent = `${label}: `;
        field.append(name, document.createTextNode(detail));
        row.append(field);
    }

    const progress = document.createElement("progress");
    progress.className = "download-worker-progress";
    progress.setAttribute("aria-label", `${t("download.thread")} ${worker.worker_id}`);
    if (validTotal) {
        progress.max = total;
        progress.value = value;
    } else {
        progress.classList.add("is-indeterminate");
    }
    row.append(progress);
}

function renderProcessProgress(prefix, current, total) {
    const totalValue = Math.max(0, Number(total) || 0);
    const currentValue = Math.max(0, Number(current) || 0);
    const bar = $(`#${prefix}-total-bar`);

    const count = $(`#${prefix}-total-count`);
    if (count) count.textContent = `${currentValue} / ${totalValue}`;
    if (totalValue > 0) {
        bar.max = totalValue;
        bar.value = Math.min(currentValue, totalValue);
    } else {
        bar.max = 1;
        bar.value = 0;
    }
}

function renderJob(job) {
    if (job.kind === "download") {
        const running = job.status === "running";
        $("#stop-download").hidden = !running;
        $("#stop-download").disabled = Boolean(job.stop_requested);
        $("#download-status").dataset.state = running ? "working" :
            (["error", "partial"].includes(job.status) ? "error" : "complete");
        $("#download-progress-word").textContent = running ? t(`phase.${job.phase}`) :
            job.status === "error" ? errorText(job.error) :
            job.result?.stopped_by_user ? "Detenida por el usuario" :
            job.result?.aborted ? t("download.aborted") :
            job.status === "partial" ? t("common.partial") : t("common.completed");
        $("#download-project").textContent = job.current_project === null ?
            t("download.root") : job.current_project || "—";
        $("#download-resource").textContent = job.current_resource || "—";
    $("#download-progress").textContent = `${job.current || 0} / ${job.total || 0}`;
    $("#download-total-progress").hidden = false;
    renderProcessProgress("download", job.current, job.total);
        const activeWorkers = job.worker_status || [];
        const hasWorkerStatus = activeWorkers.length > 0;
        $("#download-current-status").hidden = hasWorkerStatus;
        $("#download-status").hidden = hasWorkerStatus;
        $("#download-worker-list").replaceChildren();
        for (const worker of activeWorkers) {
            const row = document.createElement("li");
            row.className = "download-worker-status";
            renderDownloadWorker(row, worker);
            $("#download-worker-list").append(row);
        }
        for (const error of job.result?.errors || []) {
            const row = document.createElement("li");
            row.textContent = `${error.project_name || t("download.root")}: ${errorText(error.error)}`;
            $("#download-worker-list").append(row);
        }
        $("#download-worker-list").hidden = !hasWorkerStatus &&
            !(job.result?.errors || []).length;
        $("#download-results").hidden = running;
        for (const key of ["downloaded", "skipped", "unavailable", "failed"]) {
            $(`#download-${key}`).textContent = job.result?.[key] ?? job[key] ?? 0;
        }
        if (job.result) workflowState.download = job.result;
        updateNavigationStates();
        return;
    }
    if (job.kind === "connection") {
        if (job.status === "running") {
            $("#system-status").textContent = t("system.waiting_browser_login");
            $("#system-status").dataset.state = "working";
        } else if (job.result) renderSession(job.result);
        else if (job.error) {
            $("#system-status").textContent = errorText(job.error);
            $("#system-status").dataset.state = "error";
            $("#manual-cookie").hidden = false;
        }
        updateNavigationStates();
        return;
    }
    const prefix = job.kind === "backup" ? "backup" : "inventory";
    const status = $(`#${prefix}-status`);
    const word = $(`#${prefix}-progress-word`);
    const detail = $(`#${prefix}-progress-detail`);
    if (job.kind === "inventory") updateInventoryActions(job.status === "running");
    if (job.kind === "backup" && job.error === "invalid_session") {
        renderSession({authenticated: false, cookie_found: true, user: null});
        location.hash = "#sistema";
    }
    status.dataset.state = job.status === "running" ? "working" :
        (["error", "partial"].includes(job.status) ? "error" : "complete");
    if (job.status === "running") {
        word.textContent = t(`phase.${job.phase}`);
        detail.textContent = [job.current_project, job.total ? `${job.current} / ${job.total}` : ""]
            .filter(Boolean).join(" · ");
        if (Number(job.total) > 0) {
            renderProcessProgress(prefix, job.current, job.total);
        }
    } else {
        word.textContent = job.status === "error" ? errorText(job.error) :
            job.status === "partial" ? t("common.partial") : t("common.completed");
        detail.textContent = "";
    }
    if (job.result) (prefix === "backup" ? renderBackup : renderInventory)(job.result);
    updateNavigationStates();
}

function applyJobs(jobs) {
    workflowState.jobs = jobs;
    active = jobs.some((job) => job.status === "running");
    lockButtons();
    for (const job of jobs) {
        const signature = JSON.stringify(job);
        if (job.status === "running" || seenJobs.get(job.kind) !== signature) {
            renderJob(job);
            seenJobs.set(job.kind, signature);
        }
    }
    updateNavigationStates();
}

async function poll() {
    try { applyJobs(await api("/api/jobs")); }
    catch (error) { notice(error.message); }
    finally { window.setTimeout(poll, 1500); }
}

async function submit(operation) {
    if (active || submitting) return;
    submitting = true;
    lockButtons();
    notice();
    try { await operation(); }
    catch (error) { notice(error.message); }
    finally { submitting = false; lockButtons(); }
}

async function startJob(path, body = {}) {
    const job = await api(path, body);
    active = true;
    lockButtons();
    renderJob(job);
    applyJobs(await api("/api/jobs"));
}

$("#download-resources").addEventListener("click", () => submit(async () => {
    if (!hasValidAnalysisScope()) {
        throw new Error(errorText("download_empty_scope"));
    }
    updateDownloadSettings();
    if (!downloadSettings.safe_mode && downloadSettings.mode === "manual" &&
        (!$("#download-workers").reportValidity() || !$("#download-delay").reportValidity())) return;
    const body = {
        project_ids: [...analysisScope.project_ids],
        include_unassigned: analysisScope.include_unassigned,
        mode: downloadSettings.mode,
        safe_mode: downloadSettings.safe_mode,
        auto_safe_mode: downloadSettings.auto_safe_mode,
    };
    if (!downloadSettings.safe_mode && downloadSettings.mode === "manual") {
        body.resource_delay = downloadSettings.resource_delay;
        body.workers = downloadSettings.workers;
    }
    $("#download-results").hidden = true;
    workflowState.download = null;
    workflowState.downloadScope = scopeSnapshot();
    updateNavigationStates();
    $("#download-total-progress").hidden = false;
    renderProcessProgress("download", 0, 0);
    await startJob("/api/download", body);
}));

$("#stop-download").addEventListener("click", async () => {
    if ($("#stop-download").disabled) return;
    $("#stop-download").disabled = true;
    try { renderJob(await api("/api/download/stop", {})); }
    catch (error) { notice(error.message); }
});

$("#analyze-account").addEventListener("click", () => submit(async () => {
    const requestDelay = $("#analysis-request-delay");
    if (!requestDelay.reportValidity() || !Number.isFinite(requestDelay.valueAsNumber)) return;
    $("#backup-results").hidden = true;
    renderProcessProgress("backup", 0, 0);
    await startJob("/api/backup/analyze", {request_delay: requestDelay.valueAsNumber});
}));
$("#generate-inventory").addEventListener("click", () => submit(async () => {
    if (!hasValidAnalysisScope()) {
        throw new Error(errorText("empty_scope"));
    }
    $("#inventory-results").hidden = true;
    $("#download-diagnostics").hidden = true;
    $("#inventory-diagnostics").hidden = true;
    updateInventoryActions(true);
    renderProcessProgress("inventory", 0, 0);
    await startJob("/api/inventory", {
        project_ids: [...analysisScope.project_ids],
        include_unassigned: analysisScope.include_unassigned,
    });
}));
$("#recover-inventory").addEventListener("click", () => submit(async () => {
    const result = await api("/api/inventory/recover", {});
    analysisScope.project_ids = [...result.scope.project_ids];
    analysisScope.include_unassigned = Boolean(result.scope.include_unassigned);
    const availableProjectIds = new Set(
        (workflowState.analysis?.projects || []).map((project) => String(project.id))
    );
    const analysis = result.scope.project_ids.every((id) => availableProjectIds.has(String(id))) ?
        workflowState.analysis : result.analysis;
    if (analysis) renderBackup(analysis);
    renderInventory(result);
    const partial = Boolean(result.partial) || (result.errors || []).length > 0;
    $("#inventory-status").dataset.state = partial ? "error" : "complete";
    $("#inventory-progress-word").textContent = partial ?
        t("common.partial") : t("common.completed");
    $("#inventory-progress-detail").textContent = "";
    updateNavigationStates();
}));
$("#diagnose-inventory").addEventListener("click", () => submit(async () => {
    $("#inventory-diagnostics").hidden = true;
    const result = await api("/api/inventory/diagnostics");
    for (const cause of ["unreadable", "not_object", "invalid_mapping"]) {
        $(`#diagnosis-${cause}`).textContent = result.counts[cause];
    }
    $("#diagnosis-total").textContent = result.total_errors;
    $("#diagnosis-files").replaceChildren();
    for (const item of result.files) {
        const row = document.createElement("li");
        row.textContent = `${item.file}: ${t(`inventory.diagnosis.${item.cause}`)}`;
        $("#diagnosis-files").append(row);
    }
    $("#inventory-diagnostics").hidden = false;
}));

function appendDownloadDiagnosisField(row, label, value, className = "") {
    const field = document.createElement("p");
    field.className = `download-diagnosis-field ${className}`.trim();
    const name = document.createElement("strong");
    name.textContent = `${label}: `;
    field.append(name, document.createTextNode(value));
    row.append(field);
}

function renderDownloadDiagnosisFailure(row, failure, rootLabel) {
    const project = failure.project_name || rootLabel;
    const categoryValue = $(`#download-diagnosis-${failure.category}`);
    const category = categoryValue.closest("p").firstChild.textContent.trim().replace(/:$/, "");
    const technicalMessage = (failure.error || `HTTP ${failure.http_status}`)
        .replace(/\s+/g, " ").trim();
    const summary = technicalMessage.length > 220 ? `${technicalMessage.slice(0, 217)}...` : technicalMessage;

    row.className = "download-diagnosis-item";
    appendDownloadDiagnosisField(row, "Proyecto", project);
    appendDownloadDiagnosisField(
        row,
        "Archivo",
        failure.conversation_id ? `${failure.conversation_id}.json` : "-"
    );
    appendDownloadDiagnosisField(row, "Conversaci\u00f3n", failure.title || "-");
    appendDownloadDiagnosisField(row, "Error", category);
    appendDownloadDiagnosisField(row, "Mensaje", summary, "download-diagnosis-message");
}

$("#diagnose-downloads").addEventListener("click", () => submit(async () => {
    const panel = $("#download-diagnostics");
    panel.hidden = true;
    const result = await api("/api/conversations/diagnostics", {
        project_ids: [...analysisScope.project_ids],
        include_unassigned: analysisScope.include_unassigned,
    });
    for (const category of ["401", "403", "404", "429", "network", "other"]) {
        const count = result.counts[category] || 0;
        const value = $(`#download-diagnosis-${category}`);
        value.textContent = count;
        value.closest("p").hidden = count === 0;
    }
    $("#download-diagnosis-total").closest("p").hidden = true;
    $("#download-diagnosis-files").replaceChildren();
    for (const failure of result.failures) {
        const row = document.createElement("li");
        renderDownloadDiagnosisFailure(row, failure, panel.dataset.rootLabel);
        $("#download-diagnosis-files").append(row);
    }
    panel.hidden = false;
}));
$("#clear-local-data").addEventListener("click", () => {
    if (active || submitting) return;
    $("#cleanup-include-downloads").checked = false;
    $("#cleanup-modal").hidden = false;
    $("#cleanup-cancel").focus();
});
$("#cleanup-cancel").addEventListener("click", closeCleanupModal);
document.querySelector("[data-cleanup-close]").addEventListener("click", closeCleanupModal);
window.addEventListener("keydown", (event) => {
    if ($("#cleanup-modal").hidden || cleanupExecuting) return;
    const key = event.key.toLowerCase();
    if (key !== "a" && key !== "l") return;
    event.preventDefault();
    cleanupHeldKeys.add(key);
    if (cleanupHeldKeys.has("a") && cleanupHeldKeys.has("l")) startCleanupHold();
});
window.addEventListener("keyup", (event) => {
    const key = event.key.toLowerCase();
    if (key !== "a" && key !== "l") return;
    cleanupHeldKeys.delete(key);
    if (!cleanupExecuting) resetCleanupHold();
});
window.addEventListener("blur", () => {
    if (!cleanupExecuting) resetCleanupHold();
});
$("#connect-chatgpt").addEventListener("click", () => submit(() => startJob("/api/system/connect")));
$("#check-connection").addEventListener("click", () => submit(loadSession));
$("#manual-connection").addEventListener("click", (event) => {
    event.preventDefault();
    $("#manual-cookie").hidden = !$("#manual-cookie").hidden;
    if (!$("#manual-cookie").hidden) $("#cookie-input").focus();
});
$("#save-cookie").addEventListener("click", () => submit(async () => {
    const value = $("#cookie-input").value.trim();
    if (!value) throw new Error(t("system.cookie_empty"));
    try {
        const result = await api("/api/system/cookie", {cookie: value});
        renderSession(result);
        notice(t("system.connection_success"));
    } finally { $("#cookie-input").value = ""; }
}));
$("#change-storage-root").addEventListener("click", () => {
    submit(async () => {
        const result = await api("/api/system/storage/browse", {});
        if (result.cancelled) return;
        renderStorage(result);
        $("#download-diagnostics").hidden = true;
        $("#inventory-diagnostics").hidden = true;
        seenJobs.clear();
        $("#download-results").hidden = true;
        $("#download-progress-word").textContent = t("common.pending");
        $("#download-status").dataset.state = "pending";
        $("#download-status").hidden = false;
        $("#download-project").textContent = "—";
        $("#download-resource").textContent = "—";
    $("#download-progress").textContent = "0 / 0";
    $("#download-total-progress").hidden = true;
    renderProcessProgress("download", 0, 0);
        $("#download-worker-list").replaceChildren();
        $("#download-worker-list").hidden = true;
        for (const prefix of ["backup", "inventory"]) {
            $(`#${prefix}-results`).hidden = true;
            renderProcessProgress(prefix, 0, 0);
            $(`#${prefix}-progress-word`).textContent = t("common.pending");
            $(`#${prefix}-progress-detail`).textContent = "";
            $(`#${prefix}-status`).dataset.state = "pending";
        }
        await loadSummaries();
        await loadSession();
    });
});

async function loadSummaries() {
    const [backup, inventory] = await Promise.all([
        api("/api/backup/overview"), api("/api/inventory/overview"),
    ]);
    analysisScope.project_ids = [];
    analysisScope.include_unassigned = false;
    workflowState.analysis = null;
    workflowState.inventory = null;
    $("#backup-project-list").replaceChildren();
    renderBackup(backup);
    setRecoverableInventory(inventory);
    updateAnalysisScopeUI();
    updateNavigationStates();
}

syncAnalysisScope();
Promise.all([api("/api/system/storage").then(renderStorage), loadSummaries()])
    .catch((error) => notice(error.message));
loadSession().catch((error) => notice(error.message));
poll();
