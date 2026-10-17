/** Dependency-free JSDoc mirror of the Dashboard storage envelopes homed in `ouroboros.storage` and
 *  re-exported by `ouroboros.gateway.contracts` (`GET /api/storage`, `POST /api/storage/measure`). */

/**
 * One measured row. `bytes` counts logical file sizes; it is a lower bound when `coverage`
 * is `partial` and null (unknown, never zero) when none of the row's folders could be listed.
 * @typedef {Object} StorageCategory
 * @property {string} id  // observability | task_drives | results | archives | memory | skills | other
 * @property {number|null} bytes
 * @property {number} files
 * @property {'complete'|'partial'|'unreadable'} coverage
 * @property {''|'time_limit'|'entry_limit'} stopped
 * @property {number} errors  // entries or folders that could not be read
 * @property {number} skipped  // folders past the depth bound and linked files of unknown identity, not counted
 */

/**
 * One finished walk of the data folder, held in the server process only.
 * @typedef {Object} StorageMeasurement
 * @property {string} finished_at
 * @property {number} duration_seconds
 * @property {number} measured_bytes  // counted bytes only; a lower bound unless `complete`
 * @property {boolean} complete
 * @property {number|null} free_bytes  // free space on the data folder's volume, not a machine total
 * @property {StorageCategory[]} categories
 * @property {string[]} not_measured  // engine | worktrees | project_folders: excluded wherever configured
 */

/**
 * @typedef {Object} StorageError
 * @property {string} at
 * @property {string} message
 */

/**
 * The passive projection; the measure POST answers it too, with `started`.
 * @typedef {Object} StorageResponse
 * @property {boolean} measuring
 * @property {StorageMeasurement|null} measurement
 * @property {StorageError|null} error  // the last failed refresh; the previous measurement stays
 * @property {boolean=} started
 */
