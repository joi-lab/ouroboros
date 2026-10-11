import { renderPageHeader } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';
import { escapeHtmlAttr, escapeHtmlText as escapeHtml } from './utils.js';
import { fetchJson, jsonPost } from './api_client.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { downloadViaHostBridge, setInlineStatus } from './ui_helpers.js';
import { bindMenu } from './ui_interactions.js';

function formatFileSize(size) {
    const num = Number(size);
    if (!Number.isFinite(num) || num < 0) return '';
    if (num < 1024) return `${num} B`;
    if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KB`;
    return `${(num / (1024 * 1024)).toFixed(1)} MB`;
}

function iconForEntry(entry) {
    return entry.type === 'dir' ? '▸' : '•';
}

function defaultDirectoryMeta() {
    return 'Browse folders, preview/edit text files, upload, download, copy, and move files here. This is a file manager, not a chat attachment picker.';
}

function defaultDirectoryContent() {
    return 'Open a folder or file from the left panel to browse, preview, or edit its contents.';
}

export function initFiles({ state: appState, setBeforePageLeave } = {}) {
    const page = document.createElement('div');
    page.id = 'page-files';
    page.className = 'page app-page-glass';
    page.innerHTML = `
        ${renderPageHeader({
            title: 'Files',
            icon: PAGE_ICONS.files,
            actionsHtml: '<button class="btn btn-default" id="files-refresh">Refresh</button>',
        })}
        <div class="files-layout">
            <section class="files-sidebar">
                <div class="files-toolbar">
                    <input id="files-search" class="ui-control" name="files-filter" type="text" aria-label="Filter current folder" placeholder="Filter current folder...">
                </div>
                <div class="files-browser-header">
                    <div id="files-breadcrumb" class="files-breadcrumb"></div>
                    <div class="files-browser-actions">
                        <button class="btn btn-default" id="files-paste" title="Paste copied or moved item" hidden>Paste</button>
                        <button class="btn btn-default" id="files-new-file" title="Create file">+ File</button>
                        <button class="btn btn-default" id="files-new-dir" title="Create directory">+ Dir</button>
                    </div>
                </div>
                <div id="files-list" class="files-list scroll-fade-y" tabindex="0" aria-label="Files in current folder"></div>
            </section>
            <section class="files-preview">
                <div class="files-preview-header">
                    <div>
                        <div id="files-preview-path" class="files-preview-path">Files</div>
                        <div id="files-preview-meta" class="files-preview-meta">${defaultDirectoryMeta()}</div>
                    </div>
                    <div class="files-preview-actions">
                        <button class="btn btn-default" id="files-download" hidden>Download</button>
                        <button class="btn btn-default" id="files-open-external" hidden>Open externally</button>
                        <button class="btn btn-primary" id="files-save" hidden disabled>Save</button>
                    </div>
                </div>
                <div id="files-preview-status" class="ui-status files-preview-status" role="status" aria-live="polite" hidden></div>
                <div id="files-preview-content" class="files-preview-content scroll-fade-y">${defaultDirectoryContent()}</div>
            </section>
            <div class="files-drop-overlay" aria-hidden="true">
                <div class="files-drop-card">Drop files to upload into the current folder</div>
            </div>
            <div id="files-context-menu" class="files-context-menu ui-popup" role="menu" aria-label="File actions" hidden>
                <button type="button" role="menuitem" class="files-context-item" data-action="download">Download</button>
                <button type="button" role="menuitem" class="files-context-item" data-action="copy">Copy</button>
                <button type="button" role="menuitem" class="files-context-item" data-action="move">Move</button>
                <button type="button" role="menuitem" class="files-context-item" data-action="paste">Paste Here</button>
                <button type="button" role="menuitem" class="files-context-item files-context-item-danger" data-action="delete">Delete</button>
            </div>
        </div>
    `;
    document.getElementById('content').appendChild(page);

    const layoutEl = page.querySelector('.files-layout');
    const listEl = page.querySelector('#files-list');
    const breadcrumbEl = page.querySelector('#files-breadcrumb');
    const previewPathEl = page.querySelector('#files-preview-path');
    const previewMetaEl = page.querySelector('#files-preview-meta');
    const previewContentEl = page.querySelector('#files-preview-content');
    const previewStatusEl = page.querySelector('#files-preview-status');
    const contextMenuEl = page.querySelector('#files-context-menu');
    let contextMenuBinding = null;
    let contextAnchor = null;
    const saveBtn = page.querySelector('#files-save');
    const downloadBtn = page.querySelector('#files-download');
    const openExternalBtn = page.querySelector('#files-open-external');
    const pasteBtn = page.querySelector('#files-paste');
    const newFileBtn = page.querySelector('#files-new-file');
    const newDirBtn = page.querySelector('#files-new-dir');
    const searchEl = page.querySelector('#files-search');
    const refreshBtn = page.querySelector('#files-refresh');

    const state = {
        path: '.',
        parentPath: '.',
        entries: [],
        selectedPath: '',
        selectedType: '',
        filter: '',
        rootPath: '',
        dragDepth: 0,
        contextPath: '',
        editorPath: '',
        editorOriginalFilename: '',
        editorOriginal: '',
        editorValue: '',
        editorDirty: false,
        editorWritable: false,
        editorIsNew: false,
        editorFilename: '',
        editorSaving: false,
        clipboard: null,
        contextEntryType: '',
        contextDestinationPath: '.',
    };
    let viewRequest = 0;
    let draftRevision = 0;

    function updateEditorActions() {
        const visible = state.editorWritable && state.selectedType === 'file';
        const canSave = visible && (
            state.editorIsNew
                ? Boolean(state.editorFilename.trim())
                : state.selectedPath === state.editorPath
        ) && (state.editorDirty || state.editorIsNew);
        saveBtn.hidden = !visible;
        saveBtn.disabled = !canSave || state.editorSaving;
        saveBtn.textContent = state.editorSaving ? 'Saving…' : 'Save';
        const fileSelected = state.selectedType === 'file' && Boolean(state.selectedPath);
        downloadBtn.hidden = !fileSelected;
        openExternalBtn.hidden = !fileSelected;
    }

    function resetEditorState() {
        state.editorPath = '';
        state.editorOriginalFilename = '';
        state.editorOriginal = '';
        state.editorValue = '';
        state.editorDirty = false;
        state.editorWritable = false;
        state.editorIsNew = false;
        state.editorFilename = '';
        updateEditorActions();
    }

    function updateClipboardActions() {
        pasteBtn.hidden = !state.clipboard;
        pasteBtn.disabled = !state.clipboard;
        pasteBtn.textContent = state.clipboard
            ? `Paste ${state.clipboard.mode === 'move' ? 'Move' : 'Copy'}`
            : 'Paste';
    }

    function setPreview({ path, meta, content, html, node }) {
        showStatus('');
        previewPathEl.textContent = path || 'Select a file';
        previewMetaEl.textContent = meta || '';
        if (node) {
            previewContentEl.replaceChildren(node);
            return;
        }
        if (typeof html === 'string') {
            previewContentEl.innerHTML = html;
            return;
        }
        previewContentEl.textContent = content || '';
    }

    function renderEditor(content, options = {}) {
        const wrapper = document.createElement('div');
        wrapper.className = 'files-editor-shell';

        if (options.isNew) {
            const nameInput = document.createElement('input');
            nameInput.className = 'files-editor-name ui-control';
            nameInput.type = 'text';
            nameInput.name = 'filename';
            nameInput.setAttribute('aria-label', 'File name');
            nameInput.placeholder = 'new-file.txt';
            nameInput.value = state.editorFilename || '';
            nameInput.autocomplete = 'off';
            nameInput.spellcheck = false;
            nameInput.addEventListener('input', () => {
                draftRevision += 1;
                state.editorFilename = nameInput.value;
                state.editorDirty = state.editorValue !== state.editorOriginal || state.editorFilename !== state.editorOriginalFilename;
                showStatus('');
                updateEditorActions();
            });
            wrapper.appendChild(nameInput);
        }

        const textarea = document.createElement('textarea');
        textarea.className = 'files-editor ui-control ui-control-code';
        textarea.name = 'file-content';
        textarea.setAttribute('aria-label', 'File contents');
        textarea.setAttribute('aria-describedby', 'files-preview-path files-preview-meta');
        textarea.value = content || '';
        textarea.spellcheck = false;
        textarea.placeholder = options.isNew ? 'Start typing file contents...' : '';
        textarea.addEventListener('input', () => {
            draftRevision += 1;
            state.editorValue = textarea.value;
            state.editorDirty = state.editorValue !== state.editorOriginal || state.editorFilename !== state.editorOriginalFilename;
            showStatus('');
            updateEditorActions();
        });
        wrapper.appendChild(textarea);
        return wrapper;
    }

    async function showModal({ title, message, input = false, initialValue = '', confirmLabel = 'OK', cancelLabel = 'Cancel' }) {
        const result = await openConfirmDialog({
            title,
            body: message,
            input,
            initialValue,
            confirmLabel,
            cancelLabel,
            danger: /delete|discard/i.test(confirmLabel),
        });
        return typeof result === 'boolean' ? { confirmed: result, value: '' } : result;
    }

    async function canLeaveEditor() {
        if (!state.editorDirty) return true;
        const result = await showModal({
            title: 'Discard Changes?',
            message: 'You have unsaved edits in the current file. Leave without saving?',
            confirmLabel: 'Discard',
            cancelLabel: 'Stay',
        });
        return Boolean(result?.confirmed);
    }

    function showContextMenu(x, y, path, type, destinationPath = '.', anchor = listEl) {
        hideContextMenu();
        state.contextPath = path || '';
        state.contextEntryType = type || '';
        state.contextDestinationPath = destinationPath || '.';
        const downloadItem = contextMenuEl.querySelector('[data-action="download"]');
        const pasteItem = contextMenuEl.querySelector('[data-action="paste"]');
        const deleteItem = contextMenuEl.querySelector('[data-action="delete"]');
        if (downloadItem) {
            downloadItem.hidden = type !== 'file';
        }
        if (pasteItem) {
            pasteItem.hidden = !state.clipboard || (type === 'file');
        }
        if (deleteItem) {
            deleteItem.hidden = !path;
        }
        contextMenuEl.querySelectorAll('[data-action="copy"], [data-action="move"]').forEach((item) => {
            item.hidden = !path;
        });
        contextAnchor = anchor;
        contextAnchor.setAttribute('aria-expanded', 'true');
        contextAnchor.setAttribute('aria-haspopup', 'menu');
        document.body.appendChild(contextMenuEl);
        contextMenuEl.hidden = false;
        contextMenuBinding = bindMenu(contextMenuEl, {
            anchor,
            point: Number.isFinite(x) && Number.isFinite(y) ? { x, y } : undefined,
            onClose: resetContextMenu,
        });
    }

    function resetContextMenu() {
        state.contextPath = '';
        state.contextEntryType = '';
        state.contextDestinationPath = '.';
        contextAnchor?.setAttribute('aria-expanded', 'false');
        contextAnchor = null;
        contextMenuBinding = null;
        contextMenuEl.hidden = true;
        layoutEl.appendChild(contextMenuEl);
    }

    function hideContextMenu(options) {
        contextMenuBinding?.close(options);
    }

    function filteredEntries() {
        const needle = state.filter.trim().toLowerCase();
        if (!needle) return state.entries;
        return state.entries.filter((entry) => entry.name.toLowerCase().includes(needle));
    }

    function renderBreadcrumb(items) {
        breadcrumbEl.innerHTML = '';
        items.forEach((item, idx) => {
            const btn = document.createElement('button');
            btn.className = 'files-crumb';
            btn.textContent = item.name;
            btn.addEventListener('click', () => {
                loadDirectory(item.path).catch(showError);
            });
            breadcrumbEl.appendChild(btn);
            if (idx < items.length - 1) {
                const sep = document.createElement('span');
                sep.className = 'files-crumb-sep';
                sep.textContent = '/';
                breadcrumbEl.appendChild(sep);
            }
        });
    }

    function renderList() {
        listEl.innerHTML = '';
        const entries = filteredEntries();
        const listEntries = [];
        if (state.parentPath && state.path !== '.') {
            listEntries.push({
                name: '..',
                path: state.parentPath,
                type: 'dir',
                isParentLink: true,
            });
        }
        listEntries.push(...entries);

        if (!listEntries.length) {
            const empty = document.createElement('div');
            empty.className = 'files-empty';
            empty.textContent = state.filter ? 'No matches in this folder.' : 'Folder is empty.';
            listEl.appendChild(empty);
            return;
        }

        listEntries.forEach((entry) => {
            const button = document.createElement('button');
            const selected = state.selectedPath === entry.path;
            button.type = 'button';
            if (!entry.isParentLink) {
                button.setAttribute('aria-haspopup', 'menu');
                button.setAttribute('aria-expanded', 'false');
            }
            button.className = `files-entry ${entry.isParentLink ? 'parent-link' : ''} ${selected ? 'selected' : ''}`;
            button.innerHTML = `
                <span class="files-entry-icon">${iconForEntry(entry)}</span>
                <span class="files-entry-name">${escapeHtml(entry.name)}</span>
                <span class="files-entry-meta">${entry.isParentLink ? 'up' : (entry.type === 'file' ? formatFileSize(entry.size) : 'open')}</span>
            `;
            button.addEventListener('contextmenu', (event) => {
                if (entry.isParentLink) return;
                event.preventDefault();
                showContextMenu(
                    event.clientX,
                    event.clientY,
                    entry.path,
                    entry.type,
                    entry.type === 'dir' ? entry.path : state.path || '.',
                    button,
                );
            });
            button.addEventListener('keydown', (event) => {
                if (entry.isParentLink || (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10'))) return;
                event.preventDefault();
                showContextMenu(undefined, undefined, entry.path, entry.type, entry.type === 'dir' ? entry.path : state.path, button);
            });
            button.addEventListener('click', () => {
                hideContextMenu();
                if (entry.type === 'dir') {
                    loadDirectory(entry.path).catch(showError);
                } else {
                    loadFile(entry.path).catch(showError);
                }
            });
            listEl.appendChild(button);
        });
    }

    function showStatus(text, tone = 'muted') {
        setInlineStatus(previewStatusEl, text, tone);
        previewStatusEl.hidden = !text;
    }

    function showError(err) {
        showStatus(`Request failed: ${err instanceof Error ? err.message : String(err)}`, 'error');
    }

    async function loadDirectory(path = '.', options = {}) {
        const request = ++viewRequest;
        const preserveEditor = options.skipEditorReset || (!options.useBackendDefault && path === state.path);
        if (!preserveEditor && !options.skipLeaveCheck && !(await canLeaveEditor())) return;
        if (request !== viewRequest) return;
        const revision = draftRevision;
        hideContextMenu();
        const params = new URLSearchParams();
        if (options.useBackendDefault !== true) {
            params.set('path', path);
        }
        const query = params.toString();
        const data = await fetchJson(`/api/files/list${query ? `?${query}` : ''}`);
        if (request !== viewRequest) return;
        if (!preserveEditor && revision !== draftRevision) {
            showStatus('New edits kept. Choose the folder again when ready to leave.');
            return;
        }

        if (!options.keepStatus) showStatus('');
        if (!preserveEditor) {
            state.selectedPath = '';
            state.selectedType = 'dir';
            resetEditorState();
        }
        state.rootPath = data.root_path || state.rootPath;
        state.path = data.path || '.';
        state.parentPath = data.parent_path || '.';
        state.entries = Array.isArray(data.entries) ? data.entries : [];
        renderBreadcrumb(Array.isArray(data.breadcrumb) ? data.breadcrumb : []);
        renderList();

        if (!preserveEditor || (!state.selectedPath && !state.editorWritable)) {
            setPreview({
                path: data.display_path || state.rootPath || 'Files',
                meta: data.truncated ? 'Directory listing truncated.' : defaultDirectoryMeta(),
                content: defaultDirectoryContent(),
            });
        }
    }

    async function loadFile(path) {
        if (state.selectedPath === path && state.editorPath === path) return;
        const request = ++viewRequest;
        if (!(await canLeaveEditor()) || request !== viewRequest) return;
        const revision = draftRevision;
        hideContextMenu();
        const params = new URLSearchParams({ path });
        const data = await fetchJson(`/api/files/read?${params.toString()}`);
        if (request !== viewRequest) return;
        if (revision !== draftRevision) {
            showStatus('New edits kept. Choose the file again when ready to leave.');
            return;
        }

        state.selectedPath = data.path || path;
        state.selectedType = 'file';
        renderList();
        if (data.is_image && data.content_url) {
            resetEditorState();
            setPreview({
                path: data.display_path || state.rootPath || 'Files',
                meta: `${formatFileSize(data.size)} • ${data.media_type || 'image'}`,
                html: `<img class="files-preview-image" src="${escapeHtmlAttr(data.content_url)}" alt="${escapeHtmlAttr(data.name || data.path || 'image')}">`,
            });
            return;
        }

        if (data.is_pdf && data.content_url) {
            resetEditorState();
            const safeUrl = escapeHtmlAttr(data.content_url);
            setPreview({
                path: data.display_path || state.rootPath || 'Files',
                meta: `${formatFileSize(data.size)} • PDF preview`,
                html: `<iframe class="files-preview-frame" sandbox="allow-same-origin" src="${safeUrl}" title="${escapeHtmlAttr(data.name || 'PDF preview')}"></iframe>`,
            });
            return;
        }

        if (!data.is_text) {
            resetEditorState();
            setPreview({
                path: data.display_path || state.rootPath || 'Files',
                meta: `${formatFileSize(data.size)} • binary or unsupported preview`,
                content: 'Binary or non-text file preview is not available in the UI yet.',
            });
            return;
        }

        const editable = !data.truncated;
        state.editorPath = state.selectedPath;
        state.editorOriginalFilename = data.name || '';
        state.editorOriginal = data.content || '';
        state.editorValue = data.content || '';
        state.editorDirty = false;
        state.editorWritable = editable;
        state.editorIsNew = false;
        state.editorFilename = data.name || '';
        updateEditorActions();
        setPreview({
            path: data.display_path || state.rootPath || 'Files',
            meta: editable
                ? `${formatFileSize(data.size)} • editable`
                : `${formatFileSize(data.size)} • preview truncated • read-only`,
            node: editable ? renderEditor(data.content || '', { isNew: false }) : document.createTextNode(data.content || ''),
        });
    }

    function filenameFromPath(path) {
        return String(path || '').split('/').filter(Boolean).pop() || 'download';
    }

    async function downloadFile(path, { openExternal = false } = {}) {
        if (!path) return;
        const params = new URLSearchParams({ path });
        const url = `/api/files/download?${params.toString()}`;
        const filename = filenameFromPath(path);
        const result = await downloadViaHostBridge(url, filename, { openExternal });
        if (result.native) {
            showStatus(openExternal ? `Opened ${filename} externally.` : `${filename} saved to ${result.path || 'Downloads'}.`, 'success');
            return;
        }
    }

    async function createDirectory() {
        const result = await showModal({
            title: 'Create Directory',
            message: 'Enter a name for the new directory in the current folder.',
            input: true,
            confirmLabel: 'Create',
            cancelLabel: 'Cancel',
        });
        const name = (result?.value || '').trim();
        if (!result?.confirmed || !name) return;
        await jsonPost('/api/files/mkdir', { path: state.path || '.', name });
        await loadDirectory(state.path || '.');
        showStatus(`Directory ${name} created.`, 'success');
    }

    async function finishFileOperation(view, { directory, path = '', type = 'dir', message }) {
        const currentView = view.request === viewRequest;
        const replacePreview = currentView && view.revision === draftRevision;
        if (replacePreview) {
            resetEditorState();
            state.selectedPath = path;
            state.selectedType = type;
            updateEditorActions();
            setPreview({ path: path || state.rootPath, meta: message, content: '' });
        }
        showStatus(message, 'success');
        // A completed write remains real, but it cannot take over newer work.
        // Refreshing the original folder must not cancel a newer navigation.
        if (!currentView || (!replacePreview && directory !== state.path)) return;
        try {
            await loadDirectory(directory, { skipLeaveCheck: true, skipEditorReset: true, keepStatus: true });
            showStatus(message, 'success');
        } catch (err) {
            showStatus(`${message} Folder refresh failed: ${err instanceof Error ? err.message : String(err)}`, 'error');
        }
    }

    async function pasteClipboard(destinationPath = state.path || '.') {
        const clipboard = state.clipboard;
        if (!clipboard) return;
        if (!(await canLeaveEditor())) return;
        const view = { request: viewRequest, revision: draftRevision };

        const data = await jsonPost('/api/files/transfer', {
            source_path: clipboard.path,
            destination_dir: destinationPath || '.',
            mode: clipboard.mode,
        });

        if (state.clipboard === clipboard) state.clipboard = null;
        updateClipboardActions();
        await finishFileOperation(view, {
            directory: destinationPath || '.', path: data.path || '', type: data.type || '',
            message: `${clipboard.mode === 'move' ? 'Moved' : 'Copied'} ${data.display_path || data.path || clipboard.name}.`,
        });
    }

    async function deleteSelectedEntry(path = state.selectedPath) {
        if (!path) return;
        const entry = state.entries.find((item) => item.path === path);
        if (!entry) return;
        if (!(await canLeaveEditor())) return;

        const result = await showModal({
            title: `Delete ${entry.type === 'dir' ? 'Directory' : 'File'}?`,
            message: entry.type === 'dir'
                ? `Delete "${entry.name}" and all its contents? This cannot be undone.`
                : `Delete "${entry.name}"? This cannot be undone.`,
            confirmLabel: 'Delete',
            cancelLabel: 'Cancel',
        });
        if (!result?.confirmed) return;
        const view = { request: viewRequest, revision: draftRevision };
        const directory = state.path || '.';

        await jsonPost('/api/files/delete', { path });

        await finishFileOperation(view, {
            directory, message: `${entry.type === 'dir' ? 'Directory' : 'File'} deleted: ${entry.name}.`,
        });
    }

    async function uploadFiles(fileList) {
        const files = Array.from(fileList || []);
        if (!files.length) return;
        if (!(await canLeaveEditor())) return;
        const uploadPath = state.path || '.';
        const view = { request: viewRequest, revision: draftRevision };
        let uploaded;

        for (const file of files) {
            const form = new FormData();
            form.set('path', uploadPath);
            form.set('file', file);

            showStatus(`Uploading ${file.name} into ${uploadPath}…`);

            uploaded = await fetchJson('/api/files/upload', {
                method: 'POST',
                body: form,
            });
        }

        await finishFileOperation(view, {
            directory: uploadPath, path: uploaded.path || '', type: 'file',
            message: `Upload complete: ${files.map(file => file.name).join(', ')} in ${uploadPath}.`,
        });
    }

    async function saveCurrentFile() {
        if (!state.editorWritable || state.editorSaving || (!state.editorDirty && !state.editorIsNew)) return;
        const relName = state.editorFilename.trim();
        if (state.editorIsNew && !relName) return;
        const savePath = state.editorIsNew
            ? (state.path && state.path !== '.' ? `${state.path}/${relName}` : relName)
            : state.editorPath;
        if (!savePath) return;
        const editor = previewContentEl.querySelector('.files-editor');
        const nameInput = previewContentEl.querySelector('.files-editor-name');
        const content = state.editorValue;
        state.editorSaving = true;
        if (nameInput) nameInput.disabled = true;
        updateEditorActions();
        showStatus('Saving…');
        try {
            const data = await jsonPost('/api/files/write', {
                path: savePath,
                content,
                create: state.editorIsNew,
            });
            if (editor !== previewContentEl.querySelector('.files-editor')) return;
            state.selectedPath = data.path || savePath;
            state.selectedType = 'file';
            state.editorPath = state.selectedPath;
            state.editorFilename = data.name || relName;
            state.editorOriginalFilename = state.editorFilename;
            state.editorIsNew = false;
            state.editorOriginal = content;
            state.editorDirty = state.editorValue !== content;
            if (nameInput === document.activeElement) editor?.focus();
            nameInput?.remove();
            previewPathEl.textContent = data.display_path || state.editorPath;
            previewMetaEl.textContent = `${formatFileSize(data.size)} • editable`;
            showStatus(state.editorDirty ? 'Saved. Newer edits are still unsaved.' : 'Saved.', 'success');
        } finally {
            state.editorSaving = false;
            if (nameInput) nameInput.disabled = false;
            updateEditorActions();
        }
        try {
            await loadDirectory(state.path || '.', { skipEditorReset: true, keepStatus: true });
        } catch (err) {
            if (editor === previewContentEl.querySelector('.files-editor')) {
                showStatus(`Saved. Folder refresh failed: ${err instanceof Error ? err.message : String(err)}`, 'error');
            }
        }
    }

    function createNewFile(options = {}) {
        if (state.editorDirty && !options.force) return;
        hideContextMenu();
        viewRequest += 1;
        state.selectedPath = '';
        state.selectedType = 'file';
        state.editorPath = '';
        state.editorOriginalFilename = '';
        state.editorOriginal = '';
        state.editorValue = '';
        state.editorDirty = true;
        state.editorWritable = true;
        state.editorIsNew = true;
        state.editorFilename = '';
        renderList();
        updateEditorActions();
        setPreview({
            path: state.path && state.path !== '.'
                ? `${state.rootPath || 'Files'}/${state.path}`
                : (state.rootPath || 'Files'),
            meta: 'New file • editable',
            node: renderEditor('', { isNew: true }),
        });
    }

    searchEl.addEventListener('input', () => {
        state.filter = searchEl.value || '';
        renderList();
    });

    newFileBtn.addEventListener('click', async () => {
        if (!(await canLeaveEditor())) return;
        createNewFile({ force: true });
    });

    newDirBtn.addEventListener('click', () => {
        createDirectory().catch(showError);
    });

    pasteBtn.addEventListener('click', () => {
        pasteClipboard().catch(showError);
    });

    saveBtn.addEventListener('click', () => {
        saveCurrentFile().catch(showError);
    });
    downloadBtn.addEventListener('click', () => {
        downloadFile(state.selectedPath).catch(showError);
    });
    openExternalBtn.addEventListener('click', () => {
        downloadFile(state.selectedPath, { openExternal: true }).catch(showError);
    });

    layoutEl.addEventListener('dragenter', (event) => {
        event.preventDefault();
        state.dragDepth += 1;
        layoutEl.classList.add('drag-active');
    });

    layoutEl.addEventListener('dragover', (event) => {
        event.preventDefault();
        if (event.dataTransfer) {
            event.dataTransfer.dropEffect = 'copy';
        }
    });

    layoutEl.addEventListener('dragleave', (event) => {
        event.preventDefault();
        state.dragDepth = Math.max(0, state.dragDepth - 1);
        if (state.dragDepth === 0) {
            layoutEl.classList.remove('drag-active');
        }
    });

    layoutEl.addEventListener('drop', async (event) => {
        event.preventDefault();
        state.dragDepth = 0;
        layoutEl.classList.remove('drag-active');
        hideContextMenu();
        try {
            await uploadFiles(event.dataTransfer && event.dataTransfer.files);
        } catch (err) {
            showError(err);
        }
    });

    contextMenuEl.addEventListener('click', (event) => {
        const action = event.target.closest('[data-action]')?.dataset.action;
        if (!action) return;
        const path = state.contextPath;
        const destinationPath = state.contextDestinationPath;
        hideContextMenu({ restoreFocus: true });
        if (action === 'download') {
            downloadFile(path).catch(showError);
        } else if (action === 'copy' || action === 'move') {
            const entry = state.entries.find((item) => item.path === path);
            if (entry) {
                state.clipboard = {
                    mode: action,
                    path: entry.path,
                    name: entry.name,
                    type: entry.type,
                };
                updateClipboardActions();
                showStatus(`${action === 'move' ? 'Move' : 'Copy'} ready: ${entry.name} will be ${action === 'move' ? 'moved' : 'copied'} into the next folder where you press Paste.`);
            }
        } else if (action === 'paste') {
            pasteClipboard(destinationPath).catch(showError);
        } else if (action === 'delete') {
            deleteSelectedEntry(path).catch(showError);
        }
    });

    listEl.addEventListener('contextmenu', (event) => {
        if (event.target === listEl && state.clipboard) {
            event.preventDefault();
            showContextMenu(event.clientX, event.clientY, '', 'dir', state.path || '.');
        }
    });

    listEl.addEventListener('keydown', (event) => {
        if (event.target !== listEl || !state.clipboard || (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10'))) return;
        event.preventDefault();
        showContextMenu(undefined, undefined, '', 'dir', state.path || '.');
    });

    refreshBtn.addEventListener('click', () => {
        loadDirectory(state.path || '.').catch(showError);
    });

    window.addEventListener('beforeunload', (event) => {
        if (!state.editorDirty) return;
        event.preventDefault();
        event.returnValue = '';
    });

    document.addEventListener('keydown', (event) => {
        const active = document.activeElement;
        const inEditor = active && (
            active.classList?.contains('files-editor') ||
            active.classList?.contains('files-editor-name')
        );
        const dialogOpen = Boolean(document.querySelector('[aria-modal="true"]'));
        if (!page.classList.contains('active')) return;
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
            if (!inEditor || dialogOpen) return;
            event.preventDefault();
            if (event.repeat) return;
            saveCurrentFile().catch(showError);
            return;
        }
        if (event.key === 'Delete') {
            if (inEditor || active === searchEl || dialogOpen || contextMenuBinding) return;
            event.preventDefault();
            deleteSelectedEntry().catch(showError);
        }
    });

    if (typeof setBeforePageLeave === 'function') {
        setBeforePageLeave(async ({ from }) => {
            if (from !== 'files') return true;
            hideContextMenu();
            return canLeaveEditor();
        });
    }
    if (appState) {
        appState.filesState = state;
    }

    updateClipboardActions();
    loadDirectory('.', { useBackendDefault: true }).catch(showError);
}
