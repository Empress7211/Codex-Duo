'use strict';

// Loaded only by the Orbit desktop copy. The official home is a read-only
// source; local edits are committed as before/after deltas to the owned helper.
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const installedWindows = new Set();

const MAPPING = 'app-server-project-id-by-legacy-project-id-by-host';
const KEYS = new Set([
  'local-projects', 'project-order', 'project-appearances',
  'thread-project-assignments', 'projectless-thread-ids',
  'thread-project-membership-host-ids', 'sidebar-project-thread-orders',
  'sidebar-thread-metadata', 'thread-workspace-root-hints',
  'thread-projectless-output-directories', 'pinned-thread-ids',
  'pinned-project-ids', MAPPING,
]);

const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);

function membershipChanges(beforeAssignments, beforeLoose, afterAssignments, afterLoose) {
  const before = new Set(beforeLoose || []);
  const after = new Set(afterLoose || []);
  const changed = {};
  for (const id of new Set([
    ...Object.keys(beforeAssignments || {}), ...Object.keys(afterAssignments || {}),
    ...before, ...after,
  ])) {
    const oldAssignment = beforeAssignments?.[id] ?? null;
    const assignment = afterAssignments?.[id] ?? null;
    if (!equal(oldAssignment, assignment) || before.has(id) !== after.has(id)) {
      changed[id] = { assignment, projectless: after.has(id) };
    }
  }
  return changed;
}

function helperRequest(options, request) {
  return new Promise((resolve, reject) => {
    const child = spawn(options.python, [options.helper, '--root', options.root,
      '--source-home', options.sourceHome, '--profile-home', options.profileHome],
    { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    let output = '';
    let error = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => { output += chunk; });
    child.stderr.on('data', chunk => { error += chunk; });
    child.on('error', reject);
    child.on('close', code => {
      if (code !== 0) {
        reject(new Error(error.trim() || `Sidebar helper exited ${code}`));
        return;
      }
      try { resolve(JSON.parse(output)); } catch (cause) { reject(cause); }
    });
    child.stdin.on('error', reject);
    child.stdin.end(JSON.stringify(request));
  });
}

function notifyWriterRelease(threadId) {
  for (const manager of installedWindows) {
    manager.sendMessageToAllWindows({ type: 'orbit-writer-locks-changed', threadId, has_writer: false });
  }
}

function install(globalState, windowManager, services = {}, options = {}) {
  const env = options.env || process.env;
  const root = options.root || env.ORBIT_ROOT;
  const sourceHome = options.sourceHome || env.ORBIT_SOURCE_HOME;
  const profileHome = options.profileHome || env.CODEX_HOME;
  const python = options.python || env.ORBIT_PYTHON;
  const helper = options.helper || env.ORBIT_SYNC_HELPER;
  if (!root || !sourceHome || !profileHome || !python || !helper) return () => {};
  if (path.resolve(sourceHome) === path.resolve(profileHome)) {
    throw new Error('Orbit sync requires an independent managed home');
  }
  const request = options.request || (message => helperRequest(
    { root, sourceHome, profileHome, python, helper }, message));
  const profileHost = 'local:' + path.resolve(profileHome);
  const statusPath = path.join(profileHome, '.orbit-sidebar-sync-status.json');
  const originals = {};
  const pending = new Map();
  const watchers = [];
  const watchedStats = new Map();
  const runtimeSignatures = new Map();
  let disposed = false;
  let applying = false;
  let inFlight = false;
  let scheduled = null;
  let queryScheduled = null;
  let catalogInvalidated = false;
  let snapshotRequested = true;
  let lastStatus = null;
  let revision = null;
  installedWindows.add(windowManager);

  function status(value) {
    const text = JSON.stringify(value);
    if (text === lastStatus) return;
    lastStatus = text;
    try {
      const temporary = statusPath + '.' + process.pid + '.tmp';
      fs.writeFileSync(temporary, text, { mode: 0o600 });
      fs.renameSync(temporary, statusPath);
    } catch (error) {
      console.error('Orbit sidebar sync status could not be saved:', error.message);
    }
  }

  function projectValue(key, value) {
    // Only the local project mapping belongs to shared state. SSH/remote host
    // mappings remain per instance and must survive canonical replay.
    if (key === MAPPING) {
      value = value && Object.hasOwn(value, profileHost)
        ? { [profileHost]: value[profileHost] } : undefined;
    }
    return { present: value !== undefined, value: clone(value) };
  }

  function read(key) { return projectValue(key, globalState.getStored(key)); }

  function record(key, before, after) {
    if (applying || disposed || (before.present === after.present && equal(before.value, after.value))) return;
    const existing = pending.get(key);
    pending.set(key, {
      before: existing ? existing.before : before.value,
      before_present: existing ? existing.before_present : before.present,
      after: after.value, after_present: after.present,
    });
    schedule();
  }

  for (const method of ['set', 'delete', 'update', 'updateAndPersist']) {
    if (typeof globalState[method] !== 'function') continue;
    const original = globalState[method];
    originals[method] = original;
    globalState[method] = function (key, ...args) {
      if (applying || !KEYS.has(key)) return original.call(this, key, ...args);
      let before = read(key);
      let after;
      if (method === 'update' || method === 'updateAndPersist') {
        const updater = args[0];
        args[0] = value => {
          // updateAndPersist runs from the desktop's existing persistence
          // queue. Capture its true input when invoked, after any replay that
          // happened while it waited, rather than the earlier call-site state.
          before = read(key);
          const updated = updater(value);
          after = projectValue(key, updated);
          return updated;
        };
      }
      const result = original.call(this, key, ...args);
      if (result && typeof result.then === 'function') {
        return result.then(value => { record(key, before, after || read(key)); return value; });
      }
      record(key, before, after || read(key));
      return result;
    };
  }

  function replay(response) {
    const oldAssignments = clone(globalState.getStored('thread-project-assignments'));
    const oldLoose = clone(globalState.getStored('projectless-thread-ids'));
    const changedKeys = [];
    applying = true;
    try {
      for (const key of response.keys) {
        if (!KEYS.has(key)) continue;
        let next = response.state[key];
        let present = Object.hasOwn(response.state, key);
        if (key === MAPPING) {
          const current = globalState.getStored(key) || {};
          next = { ...current };
          if (response.state[key] && Object.hasOwn(response.state[key], profileHost)) {
            next[profileHost] = response.state[key][profileHost];
          } else {
            delete next[profileHost];
          }
          present = Object.keys(next).length > 0;
        }
        const old = globalState.getStored(key);
        if (present ? !equal(old, next) : old !== undefined) {
          if (present) originals.set.call(globalState, key, clone(next));
          else originals.delete.call(globalState, key);
          changedKeys.push(key);
        }
      }
    } finally {
      applying = false;
    }
    revision = response.revision;
    if (changedKeys.length) {
      windowManager.sendMessageToAllWindows({ type: 'global-state-updated', keys: changedKeys });
      const memberships = membershipChanges(oldAssignments, oldLoose,
        globalState.getStored('thread-project-assignments'), globalState.getStored('projectless-thread-ids'));
      if (Object.keys(memberships).length) {
        windowManager.sendMessageToAllWindows({ type: 'thread-project-memberships-updated', memberships });
      }
      if (changedKeys.includes('local-projects')) {
        windowManager.sendMessageToAllWindows({ type: 'workspace-root-options-updated' });
      }
      invalidate();
    }
    status({ status: 'ok', revision });
  }

  function invalidate(catalog = false) {
    catalogInvalidated ||= catalog;
    if (queryScheduled || disposed) return;
    // Fixed-delay coalescing still refreshes during an ongoing stream of WAL
    // writes; resetting a debounce on every chunk would starve the sidebar.
    queryScheduled = setTimeout(() => {
      queryScheduled = null;
      if (catalogInvalidated) {
        catalogInvalidated = false;
        // This desktop uses the host's catalog for sidebar history. A query
        // invalidation only rereads its cached entries; routine history refresh
        // is disabled in catalog mode. The native source invalidation performs
        // a coalesced incremental scan and publishes new catalog entries.
        services.invalidateCatalog?.();
      }
      services.invalidateQuery?.(['recent-conversations']);
      services.invalidateQuery?.(['recent-conversations-meta']);
    }, options.queryDelay ?? 250);
    queryScheduled.unref?.();
  }

  function schedule() {
    if (disposed || inFlight || scheduled) return;
    scheduled = setTimeout(() => { scheduled = null; run(); }, options.delay ?? 100);
    scheduled.unref?.();
  }

  async function run() {
    if (disposed || inFlight) return;
    inFlight = true;
    const batch = new Map(pending);
    pending.clear();
    snapshotRequested = false;
    try {
      const response = await request(batch.size
        ? { action: 'commit', changes: Object.fromEntries(batch) } : { action: 'snapshot' });
      if (disposed) return;
      // A local edit made while Python held the coordinator lock must retain
      // its before/after pair. Applying a snapshot now would overwrite it.
      if (pending.size) snapshotRequested = true;
      else replay(response);
    } catch (error) {
      for (const [key, delta] of batch) {
        const newer = pending.get(key);
        pending.set(key, newer ? { ...newer, before: delta.before, before_present: delta.before_present } : delta);
      }
      status({ status: 'error', revision, error: error.message });
      console.error('Orbit sidebar sync failed:', error.message);
      // Keep unsent deltas and expose the failure. A later real file change or
      // user edit retries; do not spin on a deterministic helper failure.
      snapshotRequested = false;
      inFlight = false;
      return;
    }
    inFlight = false;
    if (pending.size || snapshotRequested) schedule();
  }

  function changedSnapshot() { snapshotRequested = true; schedule(); }

  function refreshRuntime() {
    if (disposed) return;
    const latest = new Map();
    const directory = path.join(root, 'runtime-state');
    try {
      for (const filename of fs.readdirSync(directory)) {
        if (!/^\d+\.json$/.test(filename)) continue;
        const pid = Number(filename.slice(0, -5));
        try { process.kill(pid, 0); } catch (error) {
          if (error.code === 'ESRCH') continue;
          if (error.code !== 'EPERM') throw error;
        }
        let snapshot;
        try { snapshot = JSON.parse(fs.readFileSync(path.join(directory, filename), 'utf8')); }
        catch (error) { if (error.code === 'ENOENT') continue; throw error; }
        for (const [threadId, record] of Object.entries(snapshot.threads || {})) {
          if (!latest.has(threadId) || latest.get(threadId).at < snapshot.updated_at) {
            latest.set(threadId, { at: snapshot.updated_at, record });
          }
        }
      }
      for (const [threadId, { record }] of latest) {
        // These snapshots contain actual flock evidence, not merely a completed
        // turn or idle notification. Unknown ownership never enables retry.
        const free = record.busy === false && record.status !== 'active';
        const previous = runtimeSignatures.get(threadId) || {};
        const signature = JSON.stringify({ free, status: record.status,
          source: record.source, owner_profile: record.owner_profile });
        if (previous.signature !== signature) {
          windowManager.sendMessageToAllWindows({ type: 'orbit-writer-locks-changed',
            threadId, has_writer: !free, status: record.status,
            source: record.source, owner_profile: record.owner_profile });
        }
        if (record.handoff_available === true && previous.handoff_available !== true) {
          // The managed owner reports its actual serialized idle gate. This
          // offers a real resume attempt; the UI stays locked until that attempt
          // succeeds after the owner releases its backend writer.
          windowManager.sendMessageToAllWindows({ type: 'orbit-writer-handoff-available',
            threadId, has_writer: true, handoff_available: true });
        }
        runtimeSignatures.set(threadId, { signature,
          handoff_available: record.handoff_available === true });
      }
      invalidate();
    } catch (error) {
      status({ status: 'error', revision, error: error.message });
      console.error('Orbit runtime snapshot refresh failed:', error.message);
    }
  }
  function watch(directory, names, onChange) {
    if (!directory || !fs.existsSync(directory)) return;
    try {
      const watcher = fs.watch(directory, (_, filename) => {
        if (filename == null || names.has(String(filename))) onChange();
      });
      watcher.unref?.();
      watcher.on('error', error => status({ status: 'error', revision, error: error.message }));
      watchers.push(watcher);
    } catch (error) {
      status({ status: 'error', revision, error: error.message });
    }
    for (const name of names) {
      const filename = path.join(directory, name);
      watchedStats.set(filename, { onChange, signature: null });
    }
  }

  watch(sourceHome, new Set(['.codex-global-state.json']), changedSnapshot);
  watch(path.join(root, 'shared'), new Set(['sidebar-state.json']), changedSnapshot);
  const sqliteHome = options.sqliteHome || env.ORBIT_SQLITE_HOME;
  watch(sqliteHome, new Set(['state_5.sqlite', 'state_5.sqlite-wal']), () => invalidate(true));
  const runtimeDirectory = path.join(root, 'runtime-state');
  fs.mkdirSync(runtimeDirectory, { recursive: true, mode: 0o700 });
  const runtimeWatcher = fs.watch(runtimeDirectory, (_, filename) => {
    if (filename == null || /^\d+\.json$/.test(String(filename))) refreshRuntime();
  });
  runtimeWatcher.unref?.();
  runtimeWatcher.on('error', error => status({ status: 'error', revision, error: error.message }));
  watchers.push(runtimeWatcher);
  watchedStats.set(runtimeDirectory, { onChange: refreshRuntime, signature: null });
  // fs.watch handles atomic renames. Stat just these known files as a light
  // fallback, including WAL commits that a filesystem watcher can coalesce.
  const poll = setInterval(() => {
    for (const [filename, record] of watchedStats) {
      let signature;
      try { const st = fs.statSync(filename); signature = `${st.ino}:${st.size}:${st.mtimeMs}`; }
      catch (error) { if (error.code !== 'ENOENT') status({ status: 'error', revision, error: error.message }); signature = 'missing'; }
      if (record.signature !== signature) { record.signature = signature; record.onChange(); }
    }
  }, options.pollInterval ?? 1000);
  poll.unref?.();
  schedule();

  return function dispose() {
    disposed = true;
    clearTimeout(scheduled);
    clearTimeout(queryScheduled);
    clearInterval(poll);
    for (const watcher of watchers) watcher.close();
    for (const [method, original] of Object.entries(originals)) globalState[method] = original;
    installedWindows.delete(windowManager);
  };
}

module.exports = { install, helperRequest, membershipChanges, notifyWriterRelease, KEYS };
