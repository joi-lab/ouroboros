/** Dependency-free JSDoc mirror of the Dashboard storage envelopes homed in `ouroboros.storage` and
 *  re-exported by `ouroboros.gateway.contracts` (`GET /api/storage`, `POST /api/storage/measure`,
 *  `POST /api/storage/measure/cancel`, `POST /api/storage/free/preview`, `POST /api/storage/free`,
 *  `POST /api/storage/free/cancel`). */

/**
 * One depth-1 entry of a place's roots; files directly in a root form the part `(files)`.
 * @typedef {Object} StoragePart
 * @property {string} name
 * @property {number} bytes
 * @property {number} files
 */

/**
 * The parts beyond the largest eight, summed; the engine's `projects` and `profiles` are listed whatever their rank.
 * @typedef {Object} StorageRest
 * @property {number} count
 * @property {number} bytes
 */

/**
 * One counted place. `bytes` is null (unknown, never zero) when none of its present roots could be listed;
 * `growth_bytes` is against the previous finished count, present only when both counts hold the place.
 * @typedef {Object} StoragePlace
 * @property {string} id  // model_calls | engine | task_results | archives | memory_skills | project_folders | deliverables | work_folders | working_copies | app_state | data_other | program
 * @property {string} group  // records | engine | work | temporary | other
 * @property {string[]} roots
 * @property {boolean} present
 * @property {boolean} same_volume  // on the data folder's disk
 * @property {number|null} bytes
 * @property {number} logical_bytes
 * @property {number} files
 * @property {number} unreadable  // entries or folders that could not be read; the walk went on
 * @property {StoragePart[]} parts
 * @property {StorageRest} rest
 * @property {(number|null)=} growth_bytes
 * @property {'unreadable'=} growth_flag  // either count had unreadable items here
 */

/**
 * Free and total space of the data folder's volume, never a machine total.
 * @typedef {Object} StorageVolume
 * @property {number|null} free_bytes
 * @property {number|null} total_bytes
 * @property {string} observed_at
 */

/**
 * The last finished count with growth against the one before it.
 * @typedef {Object} StorageCount
 * @property {string} started_at
 * @property {string} finished_at
 * @property {number} duration_seconds
 * @property {string} io_priority  // the disk priority the count child ran at
 * @property {'allocated'|'logical'} basis  // what `bytes` counts
 * @property {number} bytes
 * @property {number} logical_bytes
 * @property {number} files
 * @property {number} unreadable
 * @property {StorageVolume} volume  // as observed when the count finished
 * @property {StoragePlace[]} places
 * @property {(number|null)=} growth_bytes
 * @property {string=} growth_since
 * @property {'unreadable'=} growth_flag
 */

/**
 * A running count: where it is and the places already finished in this run.
 * @typedef {Object} StorageCounting
 * @property {string} started_at
 * @property {string} place
 * @property {string} current
 * @property {number} places_done
 * @property {number} places_total
 * @property {number} bytes
 * @property {number} files
 * @property {StoragePart[]} done  // `name` is the finished place's id
 */

/**
 * @typedef {Object} StorageError
 * @property {string} at
 * @property {string} message
 * @property {boolean} stopped  // the owner stopped it, as opposed to a failure
 */

/**
 * One of the largest items a free-space group offers.
 * @typedef {Object} StorageFreeItem
 * @property {string} id
 * @property {string} name  // the task's title when it has one, else the folder name
 * @property {number|null} bytes
 */

/**
 * One group the free action offers: finished tasks' work folders, working copies of finished work.
 * @typedef {Object} StorageFreeGroup
 * @property {string} id  // work_folders | working_copies
 * @property {number} count
 * @property {number} inside_wait  // finished less than retention_days ago
 * @property {number} in_use  // live, pending, unknown or non-terminal: offered nowhere, named here
 * @property {number} unreadable  // no readable task record or custody: shown, never freed
 * @property {number|null} bytes
 * @property {boolean} bytes_complete
 * @property {StorageFreeItem[]} items
 */

/**
 * @typedef {Object} StorageFreeKept
 * @property {string} reason  // results_pending | in_use | busy | unreadable_record | restart | other
 * @property {number} count
 */

/**
 * The result of one run as it stands now: a moved folder is removed once its trash entry is gone.
 * @typedef {Object} StorageFreeRun
 * @property {string[]} groups
 * @property {number} selected
 * @property {number} done
 * @property {number} removed
 * @property {number} removed_bytes
 * @property {number} deleting  // moved aside, the maintenance child has not deleted them yet
 * @property {number} deleting_bytes
 * @property {number} stuck  // moved aside and survived the child's removal; retried at the child's next run
 * @property {StorageFreeKept[]} kept
 * @property {number|null} free_before
 * @property {number|null} free_after
 * @property {boolean} stopped
 * @property {string|null} error
 */

/**
 * The free-space tray: a pure preview under a plan id, then a run of exactly the previewed items.
 * @typedef {Object} StorageFree
 * @property {'idle'|'listing'|'ready'|'running'|'done'|'failed'|'stopped'} state
 * @property {string|null} plan
 * @property {string|null} started_at
 * @property {string|null} finished_at
 * @property {number} done  // folders sized so far (listing) or attempted (running)
 * @property {number} total
 * @property {StorageFreeGroup[]} groups
 * @property {StorageFreeRun|null} run
 * @property {string|null} error
 */

/**
 * `POST /api/storage/free`: the plan id the preview answered with and the groups to free of it.
 * @typedef {Object} StorageFreeRequest
 * @property {string} plan
 * @property {string[]} groups  // work_folders | working_copies
 */

/**
 * The passive projection; the measure and free POSTs answer it too, with `started` (and the free run's
 * `reason`). `count` is omitted when `?count=<finished_at>` named the latest finished count.
 * @typedef {Object} StorageResponse
 * @property {(StorageCount|null)=} count
 * @property {StorageCounting|null} counting
 * @property {StorageError|null} error  // the last count's end; the saved count stays
 * @property {StorageVolume} volume  // fresh at this read
 * @property {number} retention_days
 * @property {StorageFree} free
 * @property {boolean=} started
 * @property {string=} reason  // why a free run did not start: preview_outdated | busy | unknown_group | nothing_selected
 */
