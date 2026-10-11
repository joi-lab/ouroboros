// V12 (TZ-1): a task card's one "Files" row. The host's recorded result rows are
// projected into root files and ONE folder record per nested tree (name, file count,
// size, member paths as its tooltip), with real download / directory-ZIP links built
// only from host-written fields (`artifacts`, `artifact_archives`); no backend is faked.
import { taskArtifactArchiveUrl, taskArtifactDownloadUrl } from './api_client.js';
import { isTerminalTaskDetail } from './log_events.js';
import { escapeHtmlAttr, escapeHtmlText as escapeHtml } from './utils.js';

const RESULT_FILE_ROWS = 8;
const FOLDER_TOOLTIP_PATHS = 20;

function artifactBytes(value) {
    const bytes = Number(value);
    if (value === null || value === undefined || value === '' || !Number.isFinite(bytes) || bytes < 0) return '';
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let size = bytes / 1024;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

// The host's word on one folder's archive (`artifact_archives[name]`): null when it says
// nothing, `{available: false}` when it refuses or no address can be built, else the address
// with the HOST's member count/size and the rows it leaves out.
function folderArchive(taskId, name, archives) {
    const fact = archives && typeof archives === 'object' && Object.hasOwn(archives, name) ? archives[name] : null;
    if (!fact || typeof fact !== 'object') return null;
    const fileCount = Number(fact.files);
    const url = fact.available === true && Number.isInteger(fileCount) && fileCount > 0
        ? taskArtifactArchiveUrl(taskId, name) : '';
    if (!url) return { available: false };
    const size = Number(fact.size);
    const excluded = Number(fact.excluded);
    return {
        available: true, url, fileCount,
        name: typeof fact.name === 'string' && fact.name ? fact.name : `${name}.zip`,
        bytes: fact.size !== null && fact.size !== '' && Number.isFinite(size) && size >= 0 ? size : null,
        excluded: Number.isInteger(excluded) && excluded > 0 ? excluded : 0,
    };
}

/**
 * V12: a task's result records as its card lists them. A root file stays one row; every
 * nested tree is ONE folder record (name, file count, size, member paths in its tooltip),
 * so a cloned repo or venv never floods the card and a bare name never stands for a
 * nested file. A root file offers its download when its capture still serves bytes
 * (status ready or unstated, no errors); a stat-only listing says `unverified`. A folder
 * offers its `.zip` only where the host's `artifact_archives` calls it available, with the
 * host's member count/size and `excluded` rows; a folder the host refuses says `no
 * archive`. Reads only host-written fields. null when the list names nothing.
 */
export function projectResultArtifacts(taskId, records, archives = null) {
    const files = [];
    const folders = new Map();
    for (const row of Array.isArray(records) ? records : []) {
        if (!row || typeof row !== 'object') continue;
        const parts = String(row.relpath || '').split('/').filter((part) => part && part !== '.');
        const size = Number(row.size);
        const bytes = row.size !== null && row.size !== '' && Number.isFinite(size) && size >= 0 ? size : null;
        const status = String(row.status || '').trim().toLowerCase();
        const serving = (status === '' || status === 'ready') && !(Array.isArray(row.errors) && row.errors.length)
            && !row.copy_status;
        if (parts.length > 1) {
            const folder = folders.get(parts[0])
                || { name: parts[0], fileCount: 0, bytes: 0, unavailableCount: 0, relpaths: [], archive: null };
            folder.fileCount += 1;
            folder.bytes = folder.bytes === null || bytes === null ? null : folder.bytes + bytes;
            if (!serving) folder.unavailableCount += 1;
            folder.relpaths.push(parts.join('/'));
            folders.set(parts[0], folder);
            continue;
        }
        const name = String(row.name || parts[0] || '').trim();
        if (!name) continue;
        files.push({
            name, bytes,
            note: serving ? (row.measured === false ? 'unverified' : '') : (status || 'unavailable'),
            url: serving ? taskArtifactDownloadUrl(taskId, name) : '',
        });
    }
    const dirs = [...folders.values()].sort((a, b) => a.name.localeCompare(b.name));
    for (const folder of dirs) folder.archive = folderArchive(taskId, folder.name, archives);
    const fileCount = files.length + dirs.reduce((sum, folder) => sum + folder.fileCount, 0);
    return fileCount ? { files, folders: dirs, fileCount } : null;
}

/**
 * Keep a card's one Files row in step with a SETTLED detail's record list: a detail without
 * the list leaves the row as it was, an empty list removes it. true when the items changed.
 */
export function syncResultFilesItem(record, detail) {
    if (!record || !isTerminalTaskDetail(detail) || !Array.isArray(detail?.artifacts)) return false;
    const view = projectResultArtifacts(record.groupId, detail.artifacts, detail.artifact_archives);
    const key = `files|${record.groupId}`;
    const index = record.items.findIndex((item) => item.dedupeKey === key);
    const current = record.items[index];
    if (JSON.stringify(current?.resultArtifacts ?? null) === JSON.stringify(view)) return false;
    if (!view) record.items.splice(index, 1);
    else if (current) current.resultArtifacts = view;
    else {
        record.items.push({
            phase: 'result', headline: 'Files', fullHeadline: 'Files', body: '', fullBody: '', fullRef: '',
            truncated: false, receipt: false, ts: '', sourceTs: String(detail.ts || ''), count: 1,
            dedupeKey: key, lineKey: `files-${String(record.groupId).replace(/[^A-Za-z0-9_-]/g, '-')}`,
            resultArtifacts: view,
        });
    }
    return true;
}

const fileCountText = (count) => `${count.toLocaleString('en-US')} ${count === 1 ? 'file' : 'files'}`;
const metaTail = (parts) => parts.filter(Boolean).map((part) => ` · ${escapeHtml(part)}`).join('');

function folderTooltip(folder) {
    const shown = folder.relpaths.slice(0, FOLDER_TOOLTIP_PATHS);
    const more = folder.relpaths.length - shown.length;
    return shown.join('\n') + (more > 0 ? `\n… ${more.toLocaleString('en-US')} more` : '');
}

// Rows use the markdown list markup (`md-li`, `md-link`) this body already styles.
function resultArtifactsHtml(view) {
    const rows = [
        ...view.files.map((file) => ({
            files: 1,
            html: (file.url
                ? `<a class="md-link" href="${escapeHtmlAttr(file.url)}" download="${escapeHtmlAttr(file.name)}">${escapeHtml(file.name)}</a>`
                : escapeHtml(file.name))
                + metaTail([artifactBytes(file.bytes), file.note]),
        })),
        ...view.folders.map((folder) => ({
            files: folder.fileCount,
            html: `<span title="${escapeHtmlAttr(folderTooltip(folder))}">`
                + (folder.archive?.available
                    ? `<a class="md-link" href="${escapeHtmlAttr(folder.archive.url)}" download="${escapeHtmlAttr(folder.archive.name)}">${escapeHtml(folder.name)}/</a>`
                    : `${escapeHtml(folder.name)}/`)
                + metaTail(folder.archive?.available
                    ? ['folder', fileCountText(folder.archive.fileCount), artifactBytes(folder.archive.bytes),
                        folder.archive.excluded ? `${folder.archive.excluded.toLocaleString('en-US')} not archived` : '']
                    : ['folder', fileCountText(folder.fileCount), artifactBytes(folder.bytes),
                        folder.unavailableCount ? `${folder.unavailableCount.toLocaleString('en-US')} unavailable` : '',
                        folder.archive ? 'no archive' : ''])
                + '</span>',
        })),
    ];
    const hidden = rows.slice(RESULT_FILE_ROWS).reduce((sum, row) => sum + row.files, 0);
    return rows.slice(0, RESULT_FILE_ROWS).map((row) => `<span class="md-li">• ${row.html}</span>`).join('')
        + (hidden ? `<span class="md-li">+${fileCountText(hidden).replace(/ (files?)$/, ' more $1')}</span>` : '');
}

/** The timeline markup of a Files item (`item.resultArtifacts`, see `syncResultFilesItem`). */
export function resultFilesItemHtml(item) {
    return `
        <div class="chat-live-line ${item.phase || 'result'}" data-live-line-key="${escapeHtmlAttr(item.lineKey || '')}" data-result-files data-expanded="0">
            <div class="chat-live-line-head">
                <span class="chat-live-line-title">Files</span>
                <span class="chat-live-line-time">${fileCountText(item.resultArtifacts.fileCount)}</span>
            </div>
            <div class="chat-live-line-body">${resultArtifactsHtml(item.resultArtifacts)}</div>
        </div>
    `;
}
