// The settled task card's one seam: its Files row (result_files.js) and its
// Continue action (task_continue.js) follow the same settled task detail.

import { syncResultFilesItem } from './result_files.js';
import { syncContinueAction } from './task_continue.js';

/** @returns {boolean} whether the card's timeline items changed (the Files row) */
export function syncSettledItems(record, detail) {
    syncContinueAction(record, detail);
    return syncResultFilesItem(record, detail);
}
