/* Flow Pipeline dashboard — vanilla JS, no build step. */

const state = {
  page: 'projects',
  projectId: null,
  tab: 'monitor',
  project: null,
  logCursor: 0,
  poll: null,
  accounts: [],
  settings: null,
  templates: [],
  caps: null,
  health: null,
  polling: false,          // a poll request is in flight
  renderedSignature: null, // what the project page currently shows
  logPinned: true,         // log pane follows new lines unless you scroll up
  assembleInputs: {},      // uploaded music/voiceover per project, kept across re-renders
};

/* ------------------------------------------------------------------ utils -- */
const app = document.getElementById('app');

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const opts = { method: options.method || 'GET', headers: {} };
  if (options.body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(options.body);
  }
  if (options.form) opts.body = options.form;
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || text || res.statusText;
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    error.payload = data;
    throw error;
  }
  return data;
}

function toast(message, isError = false) {
  const node = document.getElementById('toast');
  node.textContent = message;
  node.className = 'toast show' + (isError ? ' error' : '');
  clearTimeout(node._timer);
  const linger = String(message).length > 120 || String(message).includes('\n');
  node._timer = setTimeout(() => { node.className = 'toast'; }, linger ? 15000 : 3600);
}

function fmtTime(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
  catch { return iso; }
}

function statusPill(status) {
  const map = {
    done: 'ok', complete: 'ok', active: 'ok', manifest_ready: 'ok', ready_to_assemble: 'info',
    running: 'running', assembling: 'running', queued: 'info', draft: 'info',
    needs_review: 'err', failed: 'err', exhausted: 'err', needs_auth: 'err', needs_human: 'err',
    waiting_account: 'warn', waiting_approval: 'warn', waiting_human: 'warn', paused: 'warn',
    low: 'warn', awaiting_manifest: 'warn', skipped: 'warn', disabled: 'warn',
  };
  const hints = {
    needs_human: 'a human action was recorded — sign in with flow_login.bat, then Refresh credits (a good balance reading clears it) or Mark usable',
    needs_auth: 'the session is not authenticated — run flow_login.bat, then Refresh credits',
    exhausted: 'no credits left on this account',
  };
  return h('span', { class: `pill ${map[status] || ''}`, title: hints[status] || '' },
    (status || '').replace(/_/g, ' '));
}

function btn(label, onClick, cls = 'btn tiny') {
  return h('button', { class: cls, onclick: onClick }, label);
}

function badge(text, cls = 'pill') { return h('span', { class: cls }, text); }

/* ------------------------------------------------------------------- boot -- */
async function boot() {
  document.querySelectorAll('.nav-btn').forEach((node) => {
    node.addEventListener('click', () => {
      state.page = node.dataset.nav;
      state.projectId = null;
      document.querySelectorAll('.nav-btn').forEach((n) => n.classList.toggle('active', n === node));
      render();
    });
  });
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event && event.reason;
    const message = (reason && (reason.message || reason)) || 'unexpected error';
    toast(String(message), true);
  });
  try {
    const [health, settings] = await Promise.all([api('/api/health'), api('/api/settings')]);
    state.health = health;
    state.caps = health.capabilities;
    state.settings = settings;
  } catch (error) { toast(error.message, true); }
  renderStatusPills();
  render();
}

function renderStatusPills() {
  const host = document.getElementById('statusPills');
  host.innerHTML = '';
  const driver = state.health?.driver || '?';
  const fromEnv = state.health?.driver_from_env;
  host.appendChild(h('span', {
    class: `pill ${driver === 'mock' ? 'warn' : 'ok'}`,
    title: (driver === 'mock' ? 'Offline mock driver — no real Flow calls' : 'gflow-cli driver')
      + (fromEnv ? ' (forced by FLOWPIPE_DRIVER in the environment, which beats Settings)' : ''),
  }, `driver: ${driver}${fromEnv ? ' (env)' : ''}`));
  const stageA = state.health?.stage_a || {};
  host.appendChild(h('span', {
    class: `pill ${stageA.key_present ? 'ok' : 'warn'}`,
    title: stageA.key_present
      ? `Stage A runs headless on ${stageA.provider} (${stageA.model})`
      : `No API key for ${stageA.provider || 'Stage A'} — Stage A runs in manual mode`,
  }, stageA.key_present
    ? `stage A: ${stageA.provider} · key set`
    : `stage A: manual mode`));
}

function stopPolling() {
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
}

function startPolling() {
  stopPolling();
  state.poll = setInterval(async () => {
    if (state.page !== 'project' || !state.projectId) return;
    if (!['monitor', 'gallery', 'final'].includes(state.tab)) return;
    if (state.polling) return;  // a slow request must not stack up requests
    state.polling = true;
    try { await loadProject(state.projectId, { silent: true }); }
    catch { /* keep polling */ }
    finally { state.polling = false; }
  }, 2500);
}

/* --------------------------------------------------------------- projects -- */
async function renderProjects() {
  stopPolling();
  app.innerHTML = '';
  const [projects] = await Promise.all([api('/api/projects')]);

  const header = h('div', { class: 'row', style: 'margin-bottom:16px' },
    h('div', {}, h('h1', {}, 'Projects'), h('div', { class: 'muted small' }, 'Each project is one run: manifest, clips, final cut.')),
    h('div', { class: 'spacer' }),
    h('button', { class: 'btn primary', onclick: () => toggleNewProject() }, '+ New project'),
  );
  app.appendChild(header);

  const formHost = h('div', { id: 'newProjectHost', class: 'hide' });
  app.appendChild(formHost);
  formHost.appendChild(await newProjectForm());

  if (!projects.length) {
    app.appendChild(h('div', { class: 'panel' },
      h('h2', {}, 'No projects yet'),
      h('p', { class: 'muted' }, 'Create one: give it a topic, pick a master prompt template, and Stage A will produce a manifest you can edit before anything is sent to Flow.'),
    ));
    return;
  }

  const grid = h('div', { class: 'grid cols-3' });
  for (const project of projects) {
    const stats = project.stats?.by_kind || {};
    const total = Object.values(stats).reduce((sum, bucket) => sum + Object.values(bucket).reduce((a, b) => a + b, 0), 0);
    const done = Object.values(stats).reduce((sum, bucket) => sum + (bucket.done || 0), 0);
    grid.appendChild(h('div', {
      class: 'card',
      onclick: () => openProject(project.id),
    },
      h('div', { class: 'row' }, h('div', { class: 'title' }, project.topic), h('div', { class: 'spacer' }), statusPill(project.status)),
      h('div', { class: 'meta' },
        h('span', {}, `${project.scene_count} scenes`),
        h('span', {}, `${project.character_count} characters`),
        h('span', {}, `${done}/${total} jobs`)),
      h('div', { class: 'meta' }, h('span', { class: 'mono' }, project.id)),
      project.manifest_error
        ? h('div', { class: 'small', style: 'color:var(--err)' }, `project.json problem: ${project.manifest_error.slice(0, 120)}`)
        : null,
      project.error ? h('div', { class: 'small', style: 'color:var(--err)' }, project.error.slice(0, 140)) : null,
    ));
  }
  app.appendChild(grid);
}

function toggleNewProject() {
  document.getElementById('newProjectHost').classList.toggle('hide');
}

async function newProjectForm() {
  if (!state.templates.length) state.templates = await api('/api/templates');
  const topic = h('input', { placeholder: 'e.g. A lighthouse keeper finds a message in a bottle from 100 years ago' });
  const template = h('select', {}, state.templates.map((t) => h('option', { value: t.id }, t.name)));
  const scenes = h('input', { type: 'number', value: 5, min: 1, max: 20 });
  const duration = h('input', { type: 'number', value: 30, min: 4, max: 600 });
  const style = h('input', { value: 'cinematic, photoreal, shallow depth of field, film grain' });
  const tone = h('input', { value: 'warm, wistful' });
  const extra = h('textarea', { placeholder: 'Optional extra direction for Claude (characters you want, things to avoid, aspect ratio notes…)' });
  const mode = h('select', {}, [
    h('option', { value: 'api' }, 'Claude API (headless)'),
    h('option', { value: 'manual' }, 'Claude Desktop (manual copy/paste)'),
  ]);
  const submit = h('button', { class: 'btn primary', onclick: async () => {
    submit.disabled = true;
    try {
      const created = await api('/api/projects', {
        method: 'POST',
        body: {
          topic: topic.value.trim(),
          template_id: template.value,
          scene_count: Number(scenes.value),
          target_duration: Number(duration.value),
          style: style.value,
          tone: tone.value,
          extra: extra.value,
          mode: mode.value,
        },
      });
      if (created.manual_request) {
        toast('Stage A prompt written — run it in Claude Desktop, then import the JSON');
        await openProject(created.id);
        state.tab = 'manifest';
        renderProjectPage();
        return;
      }
      toast('Manifest generated');
      await openProject(created.id);
    } catch (error) {
      toast(error.message, true);
      if (error.payload?.id) await openProject(error.payload.id);
    } finally {
      submit.disabled = false;
    }
  } }, 'Create project & run Stage A');

  return h('div', { class: 'panel', style: 'margin-bottom:18px' },
    h('h2', {}, 'New project'),
    h('div', { class: 'grid cols-2' },
      h('label', { class: 'field' }, 'Topic', topic),
      h('label', { class: 'field' }, 'Master prompt template', template),
      h('label', { class: 'field' }, 'Scenes', scenes),
      h('label', { class: 'field' }, 'Target duration (s)', duration),
      h('label', { class: 'field' }, 'Visual style', style),
      h('label', { class: 'field' }, 'Tone', tone),
      h('label', { class: 'field' }, 'Stage A path', mode),
      h('label', { class: 'field' }, 'Extra direction', extra),
    ),
    h('div', { class: 'row', style: 'margin-top:12px' }, submit),
  );
}

async function openProject(projectId) {
  state.projectId = projectId;
  state.page = 'project';
  state.tab = state.tab || 'monitor';
  state.logCursor = 0;
  state.renderedSignature = null;
  document.querySelectorAll('.nav-btn').forEach((n) => n.classList.toggle('active', false));
  await loadProject(projectId);
  startPolling();
}

/* What the project page actually renders. While a background poll returns the
   same values, re-rendering only throws away video playback, typed text and
   scroll position, so it is skipped. */
function projectSignature(data) {
  if (!data) return '';
  return JSON.stringify([
    state.tab,
    data.project && [data.project.status, data.project.error, data.project.output_path,
                     data.project.updated_at, data.project.active_account],
    data.output_url, data.subtitle_url, data.download_path,
    data.manifest_error,
    data.narration && [data.narration.exists, data.narration.state, data.narration.error,
                       data.narration.scenes, data.narration.generated_at, data.narration.message],
    (data.jobs || []).map((job) => [job.id, job.status, job.attempts, job.error, job.account_id]),
    data.stats && data.stats.total,
    data.assets && [data.assets.characters.length, data.assets.scenes.length],
    data.pending_switch && data.pending_switch.id,
  ]);
}

async function loadProject(projectId, { silent = false } = {}) {
  const data = await api(`/api/projects/${projectId}`);
  state.project = data;
  if (!silent) { state.logCursor = 0; }
  const signature = projectSignature(data);
  if (silent && signature === state.renderedSignature) return;  // nothing new to draw
  state.renderedSignature = signature;
  renderProjectPage();
}

/* ---------------------------------------------------------- project page -- */
/* Remember what the user was doing, so a re-render does not reset it. */
function captureDomState() {
  const snapshot = { focus: null, media: [], scroll: 0, bodyScroll: 0, logAtBottom: false };
  const active = document.activeElement;
  if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT')) {
    snapshot.focus = {
      tag: active.tagName, value: active.value,
      start: active.selectionStart, end: active.selectionEnd,
      list: active.getAttribute('list'), type: active.getAttribute('type'),
      key: active.dataset ? active.dataset.key : null,
    };
  }
  document.querySelectorAll('video, audio').forEach((node, index) => {
    snapshot.media.push({ index, src: node.currentSrc || node.src, time: node.currentTime,
                          paused: node.paused, volume: node.volume, muted: node.muted });
  });
  snapshot.scroll = window.scrollY;
  const body = document.getElementById('projectBody');
  snapshot.bodyScroll = body ? body.scrollTop : 0;
  const pane = document.getElementById('logPane');
  snapshot.logAtBottom = pane ? (pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40) : false;
  return snapshot;
}

function restoreDomState(snapshot) {
  if (!snapshot) return;
  document.querySelectorAll('video, audio').forEach((node, index) => {
    const saved = snapshot.media[index];
    if (!saved) return;
    if (node.currentSrc !== saved.src && node.src !== saved.src) return;
    if (saved.volume !== undefined) node.volume = saved.volume;
    node.muted = saved.muted;
    try {
      if (saved.time > 0) node.currentTime = saved.time;
      if (!saved.paused) { const played = node.play(); if (played && played.catch) played.catch(() => {}); }
    } catch { /* element re-created before metadata: harmless */ }
  });
  if (snapshot.focus) {
    const candidates = Array.from(document.querySelectorAll('input, textarea, select'));
    const match = candidates.find((node) => (
      node.tagName === snapshot.focus.tag
      && node.getAttribute('list') === snapshot.focus.list
      && node.getAttribute('type') === snapshot.focus.type
      && (!snapshot.focus.key || (node.dataset && node.dataset.key === snapshot.focus.key))
      && (node.value === snapshot.focus.value || node.tagName === 'SELECT')));
    if (match) {
      if (snapshot.focus.value !== undefined && match.value !== snapshot.focus.value) {
        match.value = snapshot.focus.value;
      }
      match.focus({ preventScroll: true });
      try { match.setSelectionRange(snapshot.focus.start, snapshot.focus.end); } catch { /* not a text field */ }
    }
  }
  const body = document.getElementById('projectBody');
  if (body) body.scrollTop = snapshot.bodyScroll;
  const pane = document.getElementById('logPane');
  if (pane && snapshot.logAtBottom) pane.scrollTop = pane.scrollHeight;
  if (snapshot.scroll) window.scrollTo({ top: snapshot.scroll, behavior: 'instant' });
}

function renderProjectPage() {
  const data = state.project;
  if (!data) return;
  const snapshot = captureDomState();
  app.innerHTML = '';
  const project = data.project;
  const manifest = data.manifest;

  const actions = h('div', { class: 'row' });
  if (manifest) {
    actions.appendChild(h('button', { class: 'btn primary', onclick: () => runStages() }, 'Run pipeline'));
    if (['paused', 'needs_review', 'waiting_approval', 'waiting_human'].includes(project.status)) {
      actions.appendChild(h('button', { class: 'btn', onclick: async () => {
        const result = await api(`/api/projects/${project.id}/resume`, { method: 'POST' });
        if (result && result.warning) toast(result.warning, true); else toast('resumed');
        refresh();
      } }, 'Resume'));
    }
    if (project.status === 'running') {
      actions.appendChild(h('button', { class: 'btn', onclick: async () => { await api(`/api/projects/${project.id}/pause`, { method: 'POST' }); refresh(); } }, 'Pause'));
    }
    actions.appendChild(h('button', {
      class: 'btn', onclick: async () => {
        const kinds = prompt('Which stages? character,image,clip (comma separated), or "all"', 'all');
        if (!kinds) return;
        await runStages(kinds === 'all' ? null : kinds.split(',').map((s) => s.trim()), true);
      },
    }, 'Re-run…'));
    actions.appendChild(h('button', {
      class: 'btn danger',
      onclick: async () => {
        if (!confirm('Delete this project from the dashboard? Media files stay on disk.')) return;
        await api(`/api/projects/${project.id}`, { method: 'DELETE' });
        state.page = 'projects'; state.projectId = null; state.renderedSignature = null;
        stopPolling(); render();
      },
    }, 'Delete'));
  }

  app.appendChild(h('div', { class: 'row', style: 'margin-bottom:10px' },
    h('button', { class: 'btn ghost tiny', onclick: () => { state.page = 'projects'; state.projectId = null; state.renderedSignature = null; stopPolling(); render(); } }, '← Projects'),
    h('h1', {}, project.topic),
    statusPill(project.status),
    h('div', { class: 'spacer' }),
    actions,
  ));
  app.appendChild(h('div', { class: 'row small muted', style: 'margin-bottom:14px' },
    h('span', { class: 'mono' }, project.id),
    h('span', {}, `· created ${project.created_at?.slice(0, 16).replace('T', ' ')}`),
    data.active_account ? h('span', { class: 'pill info' }, `account: ${data.active_account.label}`) : h('span', { class: 'pill warn' }, 'no account selected'),
    project.output_path ? h('span', { class: 'pill ok' }, 'final video ready') : null,
  ));

  // A manifest that will not parse is a banner, not a 500 and not "no manifest"
  if (data.manifest_error) {
    app.appendChild(h('div', { class: 'banner err', style: 'margin-bottom:14px' },
      h('div', {},
        h('strong', {}, 'The manifest file is not readable. '),
        h('span', { class: 'small' }, data.manifest_error),
        h('div', { class: 'small muted', style: 'margin-top:6px' },
          'Fix project.json in the project folder, or paste a corrected manifest below to replace it.')),
      h('div', { class: 'spacer' }),
      h('button', {
        class: 'btn primary tiny', onclick: async () => {
          try { state.tab = 'manifest'; renderProjectPage(); } catch (error) { toast(error.message, true); }
        },
      }, 'Open the manifest tab'),
    ));
  }

  // Banners: pending switch approval / human action / errors
  if (data.pending_switch) {
    const request = data.pending_switch;
    app.appendChild(h('div', { class: 'banner warn', style: 'margin-bottom:14px' },
      h('div', {},
        h('strong', {}, 'Account switch needs your approval. '),
        h('span', {}, `${request.reason}${request.to_account ? ` → ${request.to_account}` : ''}`)),
      h('div', { class: 'spacer' }),
      h('button', {
        class: 'btn primary tiny', onclick: async () => {
          await api(`/api/switch-requests/${request.id}/approve`, { method: 'POST' });
          toast('switched'); refresh();
        },
      }, 'Approve switch'),
      h('button', {
        class: 'btn tiny', onclick: async () => {
          await api(`/api/switch-requests/${request.id}/deny`, { method: 'POST' });
          toast('denied'); refresh();
        },
      }, 'Deny'),
    ));
  }
  if (project.status === 'waiting_human') {
    const auth = /auth/i.test(project.error || '');
    app.appendChild(h('div', { class: 'banner err', style: 'margin-bottom:14px' },
      h('div', {},
        h('strong', {}, auth ? 'Google session expired. ' : 'Human action required. '),
        h('span', {}, project.error || 'Finish the sign-in or verification in the browser window that gflow opened.'),
        auth ? h('div', { class: 'small', style: 'margin-top:6px' },
          'Run flow_login.bat (it signs in the same profile the app uses), then press resume — '
          + 'the account is re-checked before any job is requeued.') : null),
      h('div', { class: 'spacer' }),
      h('button', {
        class: 'btn primary tiny', onclick: async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          try {
            const result = await api(`/api/projects/${project.id}/resume`, { method: 'POST' });
            if (result && result.warning) toast(result.warning, true);
            else toast('resumed');
            refresh();
          } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
        },
      }, 'I fixed it — resume'),
    ));
  }

  const tabs = h('div', { class: 'tabs' });
  for (const [key, label] of [['manifest', 'Manifest'], ['monitor', 'Monitor'], ['gallery', 'Gallery'], ['final', 'Final video']]) {
    tabs.appendChild(h('button', {
      class: 'tab' + (state.tab === key ? ' active' : ''),
      onclick: () => { state.tab = key; renderProjectPage(); },
    }, label));
  }
  app.appendChild(tabs);

  const body = h('div', { id: 'projectBody' });
  app.appendChild(body);
  if (state.tab === 'manifest') body.appendChild(manifestTab(data));
  if (state.tab === 'monitor') body.appendChild(monitorTab(data));
  if (state.tab === 'gallery') body.appendChild(galleryTab(data));
  if (state.tab === 'final') body.appendChild(finalTab(data));
  restoreDomState(snapshot);
  if (state.tab === 'monitor') loadLogs();   // the pane has to exist first
}

async function refresh() {
  if (state.projectId) await loadProject(state.projectId, { silent: true });
}

async function runStages(kinds = null, force = false) {
  try {
    await api(`/api/projects/${state.projectId}/run`, { method: 'POST', body: { kinds, force } });
    toast('pipeline started');
    state.tab = 'monitor';
    refresh();
  } catch (error) { toast(error.message, true); }
}

/* -------------------------------------------------------------- manifest -- */
function manifestTab(data) {
  const host = h('div', { class: 'stack' });
  const manifest = data.manifest;

  if (!manifest) {
    host.appendChild(h('div', { class: 'panel' },
      h('h2', {}, 'Stage A has not produced a manifest yet'),
      h('p', { class: 'muted' }, 'Either the API call failed, or this project was created in manual mode. Use the buttons below.'),
      h('div', { class: 'row' },
        h('button', { class: 'btn primary', onclick: () => rerunStageA() }, 'Generate with Claude API'),
        h('button', { class: 'btn', onclick: () => showPrompt() }, 'Show / copy prompt'),
      ),
      importBox(),
    ));
    return host;
  }

  const p = manifest.project;
  host.appendChild(h('div', { class: 'panel' },
    h('div', { class: 'row' }, h('h2', {}, 'Project'), h('div', { class: 'spacer' }),
      h('button', { class: 'btn tiny', onclick: () => rerunStageA() }, 'Regenerate with API'),
      h('button', { class: 'btn tiny', onclick: () => showPrompt() }, 'Show prompt')),
    h('div', { class: 'grid cols-3' },
      h('div', {}, h('h3', {}, 'Logline'), h('div', {}, p.logline || '—')),
      h('div', {}, h('h3', {}, 'Style'), h('div', {}, p.style || '—')),
      h('div', {}, h('h3', {}, 'Target'), h('div', {}, `${p.target_duration}s · ${p.aspect_ratio}`)),
    ),
    p.voiceover ? h('div', { style: 'margin-top:12px' },
      h('div', { class: 'row' },
        h('h3', {}, 'Narration script'),
        h('div', { class: 'spacer' }),
        h('button', { class: 'btn tiny', onclick: () => copyText(p.voiceover) }, 'Copy script')),
      h('div', { class: 'small', style: 'white-space:pre-wrap' }, p.voiceover)) : null,
    p.music_prompt ? h('div', { style: 'margin-top:12px' },
      h('h3', {}, 'Music brief'), h('div', { class: 'small' }, p.music_prompt)) : null,
  ));

  const charRows = manifest.characters.map((c) => h('tr', {},
    h('td', {}, h('strong', {}, c.name), h('div', { class: 'small mono muted' }, c.id)),
    h('td', { class: 'small' }, c.face_prompt),
    h('td', { class: 'small' }, c.body_prompt),
  ));
  const chars = h('div', { class: 'panel' },
    h('h2', {}, `Characters (${manifest.characters.length})`),
    manifest.characters.length
      ? h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, 'Name'), h('th', {}, 'Face prompt'), h('th', {}, 'Body prompt'))),
          h('tbody', {}, charRows))
      : h('p', { class: 'muted' }, 'No recurring characters in this manifest.'),
  );
  host.appendChild(chars);

  const sceneRows = manifest.scenes.map((s) => h('tr', {},
    h('td', {}, s.order),
    h('td', {}, h('strong', {}, s.title || s.id), h('div', { class: 'small mono muted' }, s.id)),
    h('td', { class: 'small' }, s.character_ids.join(', ') || '—'),
    h('td', { class: 'small' }, s.image_prompt),
    h('td', { class: 'small' }, s.animation_prompt),
    h('td', {}, `${s.clip_duration || '—'}s`),
  ));
  host.appendChild(h('div', { class: 'panel' },
    h('h2', {}, `Scenes (${manifest.scenes.length})`),
    h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, '#'), h('th', {}, 'Scene'), h('th', {}, 'Characters'),
        h('th', {}, 'Image prompt'), h('th', {}, 'Animation prompt'), h('th', {}, 'Dur'))),
      h('tbody', {}, sceneRows)),
  ));

  const editor = h('textarea', { class: 'code', spellcheck: 'false' }, JSON.stringify(manifest, null, 2));
  host.appendChild(h('div', { class: 'panel' },
    h('div', { class: 'row' }, h('h2', {}, 'Edit manifest JSON'), h('div', { class: 'spacer' }),
      h('button', {
        class: 'btn primary tiny', onclick: async () => {
          try {
            const parsed = JSON.parse(editor.value);
            await api(`/api/projects/${state.projectId}/manifest`, { method: 'PUT', body: parsed });
            toast('manifest saved'); refresh();
          } catch (error) { toast(error.message, true); }
        },
      }, 'Validate & save')),
    h('p', { class: 'muted small' }, 'Editing scene ids or character ids resets those jobs on the next run. Ids must stay kebab-case and character references must exist.'),
    editor,
    h('div', { style: 'margin-top:12px' }, importBox()),
  ));
  return host;
}

function importBox() {
  const area = h('textarea', { class: 'code', placeholder: 'Paste the JSON object Claude returned here…' });
  return h('div', { class: 'stack' },
    h('h3', {}, 'Import a manifest (manual Claude Desktop run)'),
    area,
    h('button', {
      class: 'btn', onclick: async () => {
        try {
          await api(`/api/projects/${state.projectId}/manifest/import`, { method: 'POST', body: { raw: area.value } });
          toast('manifest imported'); refresh();
        } catch (error) { toast(error.message, true); }
      },
    }, 'Import manifest'),
  );
}

async function showPrompt() {
  const data = await api(`/api/projects/${state.projectId}/prompt`);
  const text = data.manual_request || data.prompt || '(no prompt stored)';
  const win = window.open('', '_blank');
  if (!win) { toast('allow pop-ups to see the prompt, or use Import/Manifest tab', true); return; }
  win.document.write(`<pre style="white-space:pre-wrap;font:13px ui-monospace,Menlo,monospace;padding:24px;background:#0b0f14;color:#e6edf6">${
    text.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))}</pre>`);
  win.document.title = 'Stage A prompt';
}

async function rerunStageA() {
  try {
    const result = await api(`/api/projects/${state.projectId}/stage-a`, { method: 'POST', body: { mode: 'api' } });
    if (result.mode === 'manual') {
      toast('Prompt written to disk — run it in Claude Desktop, then import the JSON');
      showPrompt();
    } else { toast('manifest regenerated'); }
    refresh();
  } catch (error) { toast(error.message, true); }
}

/* --------------------------------------------------------------- monitor -- */
function jobActions(job) {
  const cells = [];
  if (['needs_review', 'failed', 'waiting_account', 'skipped'].includes(job.status)) {
    cells.push(btn('Retry', async () => {
      try { await api(`/api/jobs/${job.id}/retry`, { method: 'POST' }); toast('queued'); refresh(); }
      catch (error) { toast(error.message, true); }
    }));
  }
  cells.push(btn('Regenerate', async () => {
    if (!confirm('Regenerate this item? Video steps spend credits.')) return;
    try {
      await api(`/api/jobs/${job.id}/regenerate`, { method: 'POST' });
      toast('queued for regeneration'); refresh();
    } catch (error) { toast(error.message, true); }
  }));
  if (!['done', 'skipped'].includes(job.status)) {
    cells.push(btn('Skip', async () => {
      try { await api(`/api/jobs/${job.id}/skip`, { method: 'POST' }); refresh(); }
      catch (error) { toast(error.message, true); }
    }));
  }
  return h('td', { class: 'actions' }, h('div', { class: 'row' }, cells));
}

function monitorTab(data) {
  const host = h('div', { class: 'stack' });
  const stats = data.stats?.by_kind || {};
  const summary = h('div', { class: 'row' });
  for (const kind of ['character', 'image', 'clip']) {
    const bucket = stats[kind] || {};
    const parts = Object.entries(bucket).map(([key, value]) => `${value} ${key.replace(/_/g, ' ')}`).join(' · ');
    summary.appendChild(h('div', { class: 'panel', style: 'flex:1;min-width:200px' },
      h('h3', {}, kind === 'character' ? 'Characters' : kind === 'image' ? 'Stills' : 'Clips'),
      h('div', { class: 'small' }, parts || 'not queued yet')));
  }
  host.appendChild(summary);

  const rows = data.jobs || [];
  host.appendChild(h('div', { class: 'panel' },
    h('div', { class: 'row' }, h('h2', {}, 'Jobs'), h('div', { class: 'spacer' }),
      h('span', { class: 'muted small' }, 'retry / skip / regenerate per item')),
    rows.length ? h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, 'Kind'), h('th', {}, 'Target'), h('th', {}, 'Status'), h('th', {}, 'Attempts'),
        h('th', {}, 'Account'), h('th', {}, 'Error'), h('th', {}, ''))),
      h('tbody', {}, rows.map((job) => h('tr', {},
        h('td', {}, job.kind),
        h('td', { class: 'mono' }, job.target_id),
        h('td', {}, statusPill(job.status)),
        h('td', {}, job.attempts),
        h('td', { class: 'small' }, job.account_id || '—'),
        h('td', { class: 'small', style: 'max-width:420px' }, job.error || ''),
        jobActions(job),
      ))),
    ) : h('p', { class: 'muted' }, 'No jobs yet — press "Run pipeline".'),
  ));

  const logs = h('div', { class: 'logs', id: 'logPane' });
  logs.addEventListener('scroll', () => {
    state.logPinned = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 40;
  });
  host.appendChild(h('div', { class: 'panel' },
    h('div', { class: 'row' }, h('h2', {}, 'Live log'), h('div', { class: 'spacer' }),
      h('span', { class: 'muted small' }, 'auto-refreshes')),
    logs,
  ));
  return host;
}

async function loadLogs() {
  const pane = document.getElementById('logPane');
  if (!pane) return;
  try {
    const rows = await api(`/api/projects/${state.projectId}/logs?after_id=0&limit=300`);
    pane.innerHTML = '';
    if (!rows.length) pane.appendChild(h('div', { class: 'log-line muted' }, 'no log entries yet'));
    for (const row of rows) {
      pane.appendChild(h('div', { class: `log-line ${row.level}` },
        h('span', { class: 'stamp' }, fmtTime(row.created_at)),
        row.message));
    }
    if (state.logPinned !== false) pane.scrollTop = pane.scrollHeight;
  } catch { /* ignore */ }
}

function copyText(text) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => toast('copied'), () => toast('copy failed', true));
    return;
  }
  const area = h('textarea', {}, text);
  document.body.appendChild(area);
  area.select();
  document.execCommand('copy');
  area.remove();
  toast('copied');
}

/* --------------------------------------------------------------- gallery -- */
function galleryTab(data) {
  const host = h('div', { class: 'stack' });
  const gallery = data.assets || { characters: [], scenes: [] };
  const jobFor = (kind, target) => (data.jobs || []).find((j) => j.kind === kind && j.target_id === target);

  host.appendChild(h('div', { class: 'panel' },
    h('h2', {}, 'Characters'),
    gallery.characters.length ? h('div', { class: 'thumbs' }, gallery.characters.map((c) => {
      const job = jobFor('character', c.id);
      return h('div', { class: 'thumb' },
        h('div', { class: 'preview' }, c.image
          ? h('img', { src: c.image, alt: c.name, loading: 'lazy' })
          : h('span', { class: 'placeholder' }, job ? job.status.replace(/_/g, ' ') : 'not generated')),
        h('div', { class: 'body' },
          h('strong', {}, c.name),
          h('div', { class: 'small muted' }, c.description || ''),
          h('div', { class: 'row' }, job ? statusPill(job.status) : null,
            job ? btn('Regenerate', async () => {
              if (!confirm('Regenerate this character reference?')) return;
              await api(`/api/jobs/${job.id}/regenerate`, { method: 'POST' });
              toast('queued'); refresh();
            }) : null)),
      );
    })) : h('p', { class: 'muted' }, 'No characters in this manifest.'),
  ));

  host.appendChild(h('div', { class: 'panel' },
    h('h2', {}, 'Scenes'),
    gallery.scenes.length ? h('div', { class: 'thumbs' }, gallery.scenes.map((scene) => {
      const imageJob = jobFor('image', scene.id);
      const clipJob = jobFor('clip', scene.id);
      return h('div', { class: 'thumb' },
        h('div', { class: 'preview' }, scene.clip
          ? h('video', { src: scene.clip, muted: true, loop: true, playsinline: true,
              onmouseover: (e) => e.target.play(), onmouseout: (e) => { e.target.pause(); e.target.currentTime = 0; } })
          : scene.image
            ? h('img', { src: scene.image, loading: 'lazy' })
            : h('span', { class: 'placeholder' }, 'no still yet')),
        h('div', { class: 'body' },
          h('strong', {}, `${scene.order}. ${scene.title}`),
          h('div', { class: 'small muted' }, scene.character_ids.join(', ') || 'no characters'),
          h('div', { class: 'row' },
            imageJob ? statusPill(imageJob.status) : null,
            clipJob ? statusPill(clipJob.status) : null),
          h('div', { class: 'row' },
            imageJob ? btn(scene.image ? 'Regen still' : 'Generate still', async () => {
              if (!confirm('Regenerate this still?')) return;
              await api(`/api/jobs/${imageJob.id}/regenerate`, { method: 'POST' });
              toast('queued'); refresh();
            }) : null,
            clipJob ? btn(scene.clip ? 'Regen clip' : 'Generate clip', async () => {
              if (!confirm('Regenerate this clip? This spends Flow credits.')) return;
              await api(`/api/jobs/${clipJob.id}/regenerate`, { method: 'POST' });
              toast('queued'); refresh();
            }) : null)),
      );
    })) : h('p', { class: 'muted' }, 'No scenes yet.'),
  ));
  return host;
}

/* ----------------------------------------------------------------- final -- */
function finalTab(data) {
  const host = h('div', { class: 'stack' });
  const project = data.project;
  const finalUrl = data.output_url || null;

  host.appendChild(h('div', { class: 'panel stack' },
    h('h2', {}, 'Final video'),
    finalUrl
      ? h('div', { class: 'stack' }, h('video', { src: finalUrl, controls: true, style: 'width:100%;border-radius:10px;background:#000' }),
          h('div', { class: 'row' },
            h('a', { class: 'btn', href: finalUrl, download: true }, 'Download MP4'),
            h('span', { class: 'muted small mono' }, project.output_path)),
          data.download_path
            ? h('div', { class: 'small muted' }, 'also saved to: ', h('span', { class: 'mono' }, data.download_path))
            : h('div', { class: 'small muted' }, 'no copy in the downloads folder (Settings → copy finished videos)'),
          data.subtitle_url
            ? h('div', { class: 'row' }, h('a', { class: 'btn tiny', href: data.subtitle_url, download: true }, 'Download subtitles (.srt)'))
            : null)
      : h('p', { class: 'muted' }, 'Nothing assembled yet. Clips are joined in manifest order after every clip job is done.'),
  ));

  host.appendChild(narrationPanel(data));
  host.appendChild(assemblePanel(data));
  return host;
}

/* ------------------------------------------------------------- narration -- */
function narrationPanel(data) {
  const host = h('div', { class: 'panel stack' }, h('h2', {}, 'Narration (text to speech)'),
    h('div', { class: 'muted small' }, 'loading…'));
  fillNarration(host).catch((error) => {
    host.appendChild(h('div', { class: 'banner err' }, error.message));
  });
  return host;
}

async function fillNarration(host) {
  let info;
  let status;
  try {
    [info, status] = await Promise.all([
      api(`/api/projects/${state.projectId}/narration`),
      api('/api/voice/status'),
    ]);
  } catch (error) {
    host.innerHTML = '';
    host.appendChild(h('h2', {}, 'Narration (text to speech)'));
    host.appendChild(h('div', { class: 'banner err' }, error.message));
    return;
  }
  host.innerHTML = '';
  host.appendChild(h('h2', {}, 'Narration (text to speech)'));

  const plan = info.plan;
  const running = (info.state && info.state.state) === 'running';

  host.appendChild(status.ok
    ? h('div', { class: 'row small' },
        h('span', { class: 'pill ok' }, `voice: ${status.backend}`),
        h('span', { class: 'muted' }, status.detail || ''))
    : h('div', { class: 'banner warn' },
        `Voice server not reachable: ${status.detail || status.provider}. `,
        'Start it with scripts\\voice\\start-voicestudio.bat, or switch the backend to "mock placeholder" below to try the whole flow offline.'));
  if (status.ok && status.engine_available === false) {
    host.appendChild(h('div', { class: 'banner warn' },
      `The voice server is reachable but the engine "${status.model}" cannot run here. `,
      status.engine_hint || status.engine_reason || '',
      ' (Settings \u2192 Narration changes the engine; the server lists what is available.)'));
  }
  host.appendChild(h('div', { class: 'small muted' },
    `timestamps: ${status.aligner}`,
    (status.aligner === 'whisperx' && status.whisperx && !status.whisperx.ok)
      ? ' (whisperX is not installed - proportional spacing is used instead)' : '',
    ' \u00b7 captions: ', String(status.captions)));

  const script = info.script || [];
  if (!script.length) {
    host.appendChild(h('div', { class: 'banner' },
      'This manifest has no narration yet. Stage A writes a `narration` line per scene from the master prompt - regenerate the manifest, or add the lines in the Manifest tab.'));
    return;
  }
  host.appendChild(h('div', { class: 'stack' }, script.map((line) => h('div', { class: 'small' },
    h('strong', { class: 'mono' }, line.scene_id), ' ',
    h('span', { class: 'muted' }, line.text)))));

  const voice = h('input', { value: status.voice || 'default', placeholder: 'voice profile' });
  voice.setAttribute('list', 'voiceList');
  const voices = h('datalist', { id: 'voiceList' });
  api('/api/tts/voices').then((result) => {
    for (const entry of result.voices || []) {
      const option = document.createElement('option');
      option.value = entry.id;
      option.label = entry.name || entry.id;
      voices.appendChild(option);
    }
  }).catch(() => {});
  const speed = h('input', { type: 'number', step: '0.05', min: '0.5', max: '2', value: status.speed || 1 });
  const provider = h('select', {},
    h('option', { value: 'voicestudio', selected: status.provider !== 'mock' }, 'VoiceStudio (local)'),
    h('option', { value: 'mock', selected: status.provider === 'mock' }, 'mock placeholder (no model)'));

  host.appendChild(h('div', { class: 'grid cols-3' },
    h('label', { class: 'field' }, 'voice', voice),
    h('label', { class: 'field' }, 'speed', speed),
    h('label', { class: 'field' }, 'backend', provider)));
  host.appendChild(voices);

  const generate = h('button', {
    class: 'btn primary',
    disabled: running,
    onclick: async () => {
      try {
        await api('/api/settings', { method: 'PATCH', body: { values: { tts_provider: provider.value } } });
        await api(`/api/projects/${state.projectId}/narration`, {
          method: 'POST',
          body: { voice: voice.value.trim(), speed: Number(speed.value) },
        });
        toast('narration started - the first run loads the model, so give it a minute');
        renderProjectPage();
      } catch (error) { toast(error.message, true); }
    },
  }, running ? 'Generating\u2026' : (plan ? 'Regenerate narration' : 'Generate narration'));

  const preview = h('button', {
    class: 'btn',
    onclick: async () => {
      try {
        const response = await fetch('/api/tts/preview', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: script[0].text.slice(0, 200), voice: voice.value.trim(), speed: Number(speed.value) }),
        });
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.detail || 'preview failed');
        }
        const audio = h('audio', { controls: true, autoplay: true, style: 'margin-top:8px' });
        audio.src = URL.createObjectURL(await response.blob());
        host.appendChild(audio);
      } catch (error) { toast(error.message, true); }
    },
  }, 'Preview voice');

  host.appendChild(h('div', { class: 'row' }, generate, preview,
    plan ? h('a', { class: 'btn tiny', href: `/api/projects/${state.projectId}/narration/srt`, download: true }, 'Download .srt') : null,
    running ? h('span', { class: 'muted small' }, (info.state && info.state.message) || 'working\u2026') : null));

  if (info.state && info.state.error) {
    host.appendChild(h('div', { class: 'banner err' }, info.state.error));
  }

  if (plan) {
    const rows = (info.scenes || []).map((scene) => h('tr', {},
      h('td', {}, h('strong', { class: 'mono' }, scene.scene_id)),
      h('td', { class: 'small' }, `${scene.duration}s`),
      h('td', { class: 'small' }, scene.clip_duration !== '' ? `${scene.clip_duration}s` : '\u2014'),
      h('td', { class: 'small' }, String(scene.words)),
      h('td', { class: 'small' },
        scene.fits === null ? h('span', { class: 'muted' }, 'no clip yet')
          : scene.fits ? h('span', { class: 'pill ok' }, scene.speedup > 1 ? `fits (${scene.speedup}\u00d7 faster)` : 'fits')
            : h('span', { class: 'pill err', title: scene.dropped },
                scene.dropped ? `cut: ${scene.dropped.slice(0, 40)}\u2026` : 'too long for the clip')),
      h('td', { class: 'small mono' }, scene.exists ? 'ok' : 'missing')));

    host.appendChild(h('div', { class: 'stack' },
      h('div', { class: 'small muted' },
        `${plan.provider} \u00b7 ${plan.aligner} timestamps \u00b7 ${info.total_duration}s of speech \u00b7 ${plan.generated_at}`),
      h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, 'scene'), h('th', {}, 'speech'), h('th', {}, 'clip'),
          h('th', {}, 'words'), h('th', {}, 'fits?'), h('th', {}, 'audio'))),
        h('tbody', {}, rows)),
      (info.warnings || []).map((warning) => h('div', { class: 'banner warn' }, warning)),
      h('div', { class: 'small muted' },
        'The video is the timeline: a line that overflows its clip is sped up (up to ',
        String(status.narration_max_speedup || 1.15), '\u00d7) or cut at the last word that fits. ',
        'Assemble mixes the voice per scene and writes the .srt from the same word timestamps.')));
  }
}

function assemblePanel(data) {
  const bucket = state.assembleInputs[state.projectId] || (state.assembleInputs[state.projectId] = {});
  const uploading = data.project.status === 'assembling';
  const uploader = (kindLabel, key, kind) => {
    const input = h('input', { type: 'file', accept: 'audio/*', class: 'hide' });
    const label = h('div', { class: 'dropzone', onclick: () => input.click() },
      bucket[key] ? `${bucket[key].split('/').pop()} ✓ (click to replace)` : `Choose ${kindLabel} (optional)`);
    input.addEventListener('change', async () => {
      if (!input.files.length) return;
      const form = new FormData();
      form.append('file', input.files[0]);
      form.append('kind', kind);
      try {
        const result = await api(`/api/projects/${state.projectId}/upload`, { method: 'POST', form });
        bucket[key] = result.path;
        label.textContent = `${input.files[0].name} ✓`;
        toast(`${kindLabel} uploaded`);
      } catch (error) { toast(error.message, true); }
    });
    return h('div', {}, label, input);
  };

  const captionDefault = (state.settings && state.settings.captions) || 'sidecar';
  const captions = h('select', {},
    h('option', { value: 'sidecar', selected: captionDefault === 'sidecar' }, 'Write a .srt next to the video'),
    h('option', { value: 'burn', selected: captionDefault === 'burn' }, 'Burn subtitles into the picture'),
    h('option', { value: 'off', selected: captionDefault === 'off' }, 'No subtitles'));
  const useNarration = h('input', { type: 'checkbox', checked: true, style: 'width:auto' });
  const button = h('button', { class: 'btn primary', disabled: uploading }, uploading ? 'Assembling…' : 'Assemble final video');

  button.addEventListener('click', async () => {
    button.disabled = true;
    const previous = button.textContent;
    button.textContent = 'Assembling…';
    try {
      const result = await api(`/api/projects/${state.projectId}/assemble`, {
        method: 'POST',
        body: {
          music_file: bucket.music || null,
          voiceover_file: bucket.voice || null,
          captions: captions.value,
          use_narration: useNarration.checked,
        },
      });
      toast(`final video ready (${result.duration.toFixed(1)}s)${result.subtitle_path ? ' + subtitles' : ''}`);
      await refresh();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = previous === 'Assembling…' ? 'Assemble final video' : previous;
    }
  });

  return h('div', { class: 'panel stack' },
    h('h2', {}, 'Assemble'),
    h('p', { class: 'muted small' }, 'Every clip is normalized (resolution, fps) then concatenated in manifest order. The generated narration is fitted to each shot and mixed in; a music bed is optional.'),
    uploading ? h('div', { class: 'banner info' }, 'Assembling now — watch the log in the Monitor tab.') : null,
    h('div', { class: 'grid cols-2' },
      h('label', { class: 'field' }, 'subtitles', captions),
      h('label', { class: 'field' }, 'use the generated narration',
        h('div', { class: 'row' }, useNarration, h('span', { class: 'small muted' }, data.narration && data.narration.exists ? `${data.narration.scenes} scene(s) ready` : 'nothing generated yet')))),
    (data.manifest && data.manifest.project && data.manifest.project.voiceover)
      ? h('div', { class: 'panel', style: 'background:transparent;padding:10px 12px' },
          h('div', { class: 'row' },
            h('strong', { class: 'small' }, 'Narration script'),
            h('div', { class: 'spacer' }),
            h('button', {
              class: 'btn tiny',
              onclick: () => copyText(data.manifest.project.voiceover),
            }, 'Copy script')),
          h('div', { class: 'small muted', style: 'white-space:pre-wrap;margin-top:6px' },
            data.manifest.project.voiceover))
      : null,
    h('div', { class: 'grid cols-2' },
      uploader('music bed', 'music', 'music'), uploader('voiceover', 'voice', 'voiceover')),
    h('p', { class: 'muted small' },
      'The video length is the timeline: a shorter audio track is padded with silence, a longer one is trimmed. '
      + 'Generate the voice in the Narration panel above, or upload your own recording here (it replaces the generated narration in this mix).'),
    h('div', { class: 'row' }, button,
      h('span', { class: 'muted small' }, 'Requires every clip to be done.')),
  );
}

/* -------------------------------------------------------------- accounts -- */
async function renderAccounts() {
  stopPolling();
  app.innerHTML = '';
  const [accounts, events, switches, settings, usage] = await Promise.all([
    api('/api/accounts'), api('/api/accounts/events'), api('/api/switch-requests'),
    api('/api/settings'), api('/api/usage'),
  ]);
  state.accounts = accounts;
  state.settings = settings;

  app.appendChild(h('div', { class: 'row', style: 'margin-bottom:14px' },
    h('div', {}, h('h1', {}, 'Account pool'),
      h('div', { class: 'muted small' }, 'Each account is a separate gflow profile with its own persistent browser session.')),
    h('div', { class: 'spacer' }),
    h('button', {
      class: 'btn', onclick: async () => {
        for (const account of accounts) await api(`/api/accounts/${account.id}/refresh`, { method: 'POST' }).catch(() => {});
        toast('credits refreshed'); renderAccounts();
      },
    }, 'Refresh all credits'),
  ));

  if (!accounts.length) {
    app.appendChild(h('div', { class: 'banner info', style: 'margin-bottom:14px' },
      'No accounts yet. Add the Google accounts you control — one gflow profile each ' +
      '(`gflow auth login --profile <name>`). With the mock driver you can add a dummy account to exercise the flow.'));
  }

  app.appendChild(h('div', { class: 'panel', style: 'margin-bottom:14px' },
    h('h2', {}, 'Connected accounts'),
    accounts.length ? h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'Account'), h('th', {}, 'gflow profile'), h('th', {}, 'Status'),
        h('th', {}, 'Credits'), h('th', {}, '≈ clips left'), h('th', {}, 'Activity'), h('th', {}, ''))),
      h('tbody', {}, accounts.map((account) => h('tr', {},
        h('td', {}, h('strong', {}, account.label), h('div', { class: 'small mono muted' }, account.id)),
        h('td', { class: 'mono' }, account.profile),
        h('td', {}, statusPill(account.status),
          account.credits_age_seconds !== null && account.credits_age_seconds !== undefined
            ? h('div', { class: 'small muted' }, `checked ${Math.round(account.credits_age_seconds / 60)}m ago`) : null),
        h('td', {}, account.credits === null || account.credits === undefined ? '—' : account.credits),
        h('td', {}, account.estimated_clips === null ? '—' : account.estimated_clips),
        h('td', { class: 'small' }, Object.entries(account.usage || {}).map(([k, v]) => `${v} ${k}`).join(' · ') || '—'),
        h('td', { class: 'actions' }, h('div', { class: 'row' },
          btn('Refresh', async () => {
            try { await api(`/api/accounts/${account.id}/refresh`, { method: 'POST' }); toast('refreshed'); renderAccounts(); }
            catch (error) { toast(error.message, true); }
          }),
          btn('Renew session', async () => {
            try {
              const result = await api(`/api/accounts/${account.id}/renew`, { method: 'POST' });
              toast(result && result.renewed
                ? 'session renewed — retry the job'
                : 'headless renewal failed: this profile needs flow_login.bat (a readable balance '
                  + 'does not mean the session can create anything)', true);
              renderAccounts();
            }
            catch (error) { toast(error.message, true); }
          }),
          btn('Mark usable', async () => {
            try {
              await api(`/api/accounts/${account.id}/mark-active`, { method: 'POST' }); renderAccounts();
            } catch (error) { toast(error.message, true); }
          }),
          btn('Remove', async () => {
            if (!confirm(`Remove ${account.label} from the pool?`)) return;
            try {
              await api(`/api/accounts/${account.id}`, { method: 'DELETE' }); renderAccounts();
            } catch (error) { toast(error.message, true); }
          }, 'btn tiny danger'),
        )),
      ))),
    ) : h('p', { class: 'muted' }, 'No accounts in the pool yet.'),
  ));

  app.appendChild(addAccountPanel());

  app.appendChild(h('div', { class: 'grid cols-2' },
    h('div', { class: 'panel' },
      h('h2', {}, 'Failover policy'),
      h('p', { class: 'muted small' },
        'Account switching is the most platform-sensitive part of this pipeline. Human approval is the default: ' +
        'when a balance runs low the project pauses and asks you here.'),
      h('label', { class: 'field' }, 'Silent auto-failover (opt-in)',
        h('select', {
          onchange: async (e) => {
            try {
              await api('/api/settings', { method: 'PATCH', body: { values: { auto_failover: e.target.value === 'on' } } });
              toast('failover policy updated');
            } catch (error) { toast(error.message, true); }
          },
        }, h('option', { value: 'off', selected: !settings.auto_failover }, 'Off — ask me first (default)'),
           h('option', { value: 'on', selected: settings.auto_failover }, 'On — switch silently'))),
      h('label', { class: 'field' }, 'Low-credit threshold',
        h('input', {
          type: 'number', value: settings.credit_threshold,
          onchange: async (e) => {
            try {
              await api('/api/settings', { method: 'PATCH', body: { values: { credit_threshold: Number(e.target.value) } } });
              toast('threshold updated');
            } catch (error) { toast(error.message, true); }
          },
        })),
      h('label', { class: 'field' }, 'Credits per clip (estimate)',
        h('input', {
          type: 'number', value: settings.credits_per_clip,
          onchange: async (e) => {
            try {
              await api('/api/settings', { method: 'PATCH', body: { values: { credits_per_clip: Number(e.target.value) } } });
              toast('estimate updated');
            } catch (error) { toast(error.message, true); }
          },
        })),
    ),
    h('div', { class: 'panel' },
      h('h2', {}, 'Switch history'),
      switches.length ? h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, 'When'), h('th', {}, 'From → To'), h('th', {}, 'Status'), h('th', {}, 'Reason'))),
        h('tbody', {}, switches.slice(0, 12).map((row) => h('tr', {},
          h('td', { class: 'small' }, fmtTime(row.created_at)),
          h('td', { class: 'small' }, `${row.from_account || '—'} → ${row.to_account || '—'}`),
          h('td', {}, statusPill(row.status === 'approved' ? 'done' : row.status === 'denied' ? 'failed' : 'waiting_approval')),
          h('td', { class: 'small' }, row.reason),
        )))) : h('p', { class: 'muted' }, 'No switches yet.'),
    ),
  ));

  app.appendChild(usageChart(usage, settings));

  app.appendChild(h('div', { class: 'panel', style: 'margin-top:14px' },
    h('h2', {}, 'Account events'),
    events.length ? h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'When'), h('th', {}, 'Account'), h('th', {}, 'Action'), h('th', {}, 'Reason'), h('th', {}, 'Who'))),
      h('tbody', {}, events.slice(0, 30).map((row) => h('tr', {},
        h('td', { class: 'small' }, fmtTime(row.created_at)),
        h('td', { class: 'small mono' }, row.account_id || '—'),
        h('td', { class: 'small' }, row.action),
        h('td', { class: 'small' }, row.reason || ''),
        h('td', { class: 'small' }, row.actor),
      )))) : h('p', { class: 'muted' }, 'Nothing logged yet.'),
  ));
}

function usageChart(usage, settings) {
  const series = usage.series || [];
  const max = Math.max(1, ...series.map((row) => (row.clip || 0) + (row.image || 0) + (row.character || 0)));
  const bars = h('div', { class: 'bars' }, series.length
    ? series.map((row) => {
        const total = (row.clip || 0) + (row.image || 0) + (row.character || 0);
        return h('div', {
          class: 'bar',
          style: `height:${Math.max(2, (total / max) * 100)}%`,
          title: `${row.day}: ${row.clip || 0} clips, ${row.image || 0} stills, ${row.character || 0} characters`,
        }, h('span', {}, row.day.slice(5)));
      })
    : [h('div', { class: 'muted small' }, 'no generations yet')]);

  const perAccount = Object.entries(usage.by_account || {}).map(([account, counts]) =>
    `${account}: ${counts.clip || 0} clips (≈${(counts.clip || 0) * settings.credits_per_clip} credits)`);

  return h('div', { class: 'panel', style: 'margin-top:14px' },
    h('h2', {}, 'Generation history'),
    h('p', { class: 'muted small' }, 'Assets produced per day, all projects. Heights are relative to the busiest day.'),
    bars,
    h('div', { class: 'row small muted', style: 'margin-top:22px' },
      perAccount.length ? perAccount.map((line) => h('span', { class: 'pill' }, line)) : h('span', {}, '—')),
  );
}

function addAccountPanel() {
  const label = h('input', { placeholder: 'Account label, e.g. "studio-a"' });
  const profile = h('input', { placeholder: 'gflow profile name (defaults to the label slug)' });
  const notes = h('input', { placeholder: 'Notes (plan, which projects it owns…)' });
  return h('div', { class: 'panel', style: 'margin-bottom:14px' },
    h('h2', {}, 'Add an account'),
    h('div', { class: 'grid cols-3' },
      h('label', { class: 'field' }, 'Label', label),
      h('label', { class: 'field' }, 'gflow profile', profile),
      h('label', { class: 'field' }, 'Notes', notes)),
    h('div', { class: 'row', style: 'margin-top:12px' },
      h('button', {
        class: 'btn primary', onclick: async () => {
          try {
            await api('/api/accounts', { method: 'POST', body: { label: label.value, profile: profile.value, notes: notes.value } });
            toast('account added'); renderAccounts();
          } catch (error) { toast(error.message, true); }
        },
      }, 'Add account'),
      h('span', { class: 'muted small' }, 'Create its session first: gflow auth login --profile <name>')),
  );
}

/* -------------------------------------------------------------- settings -- */
async function renderSettings() {
  stopPolling();
  app.innerHTML = '';
  const [settings, templates, health] = await Promise.all([api('/api/settings'), api('/api/templates'), api('/api/health')]);
  state.settings = settings; state.templates = templates; state.health = health;
  // The header pill is owned by renderStatusPills(); refresh it here or it keeps
  // showing the driver from page load (save driver = gflow, pill still says mock).
  renderStatusPills();

  const numeric = ['max_attempts', 'backoff_seconds', 'job_timeout_seconds', 'clip_duration',
    'credit_threshold', 'credits_per_clip', 'output_width', 'output_height', 'output_fps',
    'output_crf'];
  const text = ['driver', 'gflow_bin', 'gflow_home', 'image_model', 'video_model_preference',
    'aspect_ratio', 'resolution', 'anthropic_model', 'prompt_template_id', 'flow_project_prefix',
    'downloads_dir'];

  const fields = h('div', { class: 'grid cols-3' });
  for (const key of text) {
    const input = h('input', { value: settings[key] ?? '' });
    fields.appendChild(h('label', { class: 'field' }, key.replace(/_/g, ' '), input));
    input.dataset.key = key;
  }
  for (const key of numeric) {
    const input = h('input', { type: 'number', value: settings[key] ?? 0 });
    fields.appendChild(h('label', { class: 'field' }, key.replace(/_/g, ' '), input));
    input.dataset.key = key;
  }

  app.appendChild(h('h1', {}, 'Settings'));
  app.appendChild(h('div', { class: 'panel', style: 'margin:14px 0' },
    h('h2', {}, 'Pipeline'),
    fields,
    h('div', { class: 'row' }, h('span', { class: 'muted small' }, 'driver = mock runs the whole pipeline offline with placeholder media. driver = gflow calls real Flow through gflow-cli.')),
    h('label', { class: 'field', style: 'margin-top:10px' }, 'copy finished videos into the downloads folder',
      h('select', {
        onchange: async (e) => {
          try {
            await api('/api/settings', { method: 'PATCH', body: { values: { copy_final_to_downloads: e.target.value === 'on' } } });
            toast('updated');
          } catch (error) { toast(error.message, true); }
        },
      }, h('option', { value: 'on', selected: settings.copy_final_to_downloads !== false }, 'Yes — keep a copy in downloads'),
         h('option', { value: 'off', selected: settings.copy_final_to_downloads === false }, 'No — only in the project folder'))),
    h('div', { class: 'row', style: 'margin-top:12px' },
      h('button', {
        class: 'btn primary', onclick: async (event) => {
          const button = event.currentTarget;
          const values = {};
          fields.querySelectorAll('input').forEach((input) => {
            const key = input.dataset.key;
            values[key] = input.type === 'number' ? Number(input.value) : input.value;
          });
          button.disabled = true;
          try {
            await api('/api/settings', { method: 'PATCH', body: { values } });
            toast('settings saved');
            renderSettings();
          } catch (error) {
            toast(error.message, true);   // nothing was saved
          } finally {
            button.disabled = false;
          }
        },
      }, 'Save settings')),
  ));

  // ---- narration: text to speech + word timestamps ----------------------
  app.appendChild(narrationSettingsPanel(settings));

  const key = h('input', { type: 'password', placeholder: 'sk-ant-… or sk-… (DeepSeek)' });
  const provider = h('select', {},
    h('option', { value: 'anthropic', selected: settings.stage_a_provider !== 'deepseek' }, 'anthropic — Claude'),
    h('option', { value: 'deepseek', selected: settings.stage_a_provider === 'deepseek' }, 'deepseek — OpenAI-compatible'));
  const stageAModel = h('input', { value: settings.stage_a_provider === 'deepseek' ? (settings.deepseek_model || '') : (settings.anthropic_model || '') });
  app.appendChild(h('div', { class: 'panel', style: 'margin-bottom:14px' },
    h('h2', {}, 'Stage A model (writes the manifest)'),
    h('p', { class: 'muted small' },
      'Claude or any OpenAI-compatible endpoint (DeepSeek and friends). Without a key, '
      + 'Stage A runs in manual mode: the pipeline writes the prompt and you paste the JSON back. '
      + 'Keys live in this process only — never on disk — and clearing them is just a restart.'),
    h('div', { class: 'grid cols-3' },
      h('label', { class: 'field' }, 'provider', provider),
      h('label', { class: 'field' }, 'model', stageAModel),
      h('label', { class: 'field' }, 'DeepSeek base URL',
        h('input', { value: settings.deepseek_base_url || 'https://api.deepseek.com', id: 'deepseekBase' }))),
    h('p', { class: 'muted small' },
      `anthropic key: ${health.anthropic_key_present ? 'present' : 'not set'} · `
      + `deepseek key: ${settings.deepseek_key_present ? 'present' : 'not set'}`),
    h('div', { class: 'row' }, h('div', { style: 'flex:1' }, key),
      h('button', {
        class: 'btn', onclick: async () => {
          const chosen = provider.value;
          const values = {
            stage_a_provider: chosen,
            deepseek_base_url: document.getElementById('deepseekBase').value,
          };
          if (chosen === 'deepseek') values.deepseek_model = stageAModel.value;
          else values.anthropic_model = stageAModel.value;
          if (key.value.trim()) values[chosen === 'deepseek' ? 'deepseek_api_key' : 'anthropic_api_key'] = key.value.trim();
          try {
            await api('/api/settings', { method: 'PATCH', body: { values } });
            toast('Stage A set to ' + chosen + (key.value.trim() ? ' (key held in memory)' : ''));
            renderSettings();
          } catch (error) { toast(error.message, true); }
        },
      }, 'Save Stage A')),
  ));

  const tools = health.tools || {};
  app.appendChild(h('div', { class: 'panel', style: 'margin-bottom:14px' },
    h('h2', {}, 'Toolchain'),
    h('p', { class: 'muted small' },
      'What this install resolved. On Windows, setup.bat puts all of it in one folder ' +
      '(D:\\Tools by default) and the app finds it automatically.'),
    h('table', {}, h('tbody', {},
      h('tr', {}, h('td', {}, 'tool folder'), h('td', { class: 'small mono' }, tools.tools_dir || '(PATH install)')),
      h('tr', {}, h('td', {}, 'python'), h('td', { class: 'small mono' }, `${tools.python || '?'} (${tools.python_version || '?'})`)),
      h('tr', {}, h('td', {}, 'ffmpeg'), h('td', { class: 'small mono' }, `${tools.ffmpeg || 'not found'}${tools.ffmpeg_source ? ` (${tools.ffmpeg_source})` : ''}`)),
      h('tr', {}, h('td', {}, 'ffprobe'), h('td', { class: 'small mono' }, tools.ffprobe || 'not found (durations are read from ffmpeg)')),
      h('tr', {}, h('td', {}, 'gflow'), h('td', { class: 'small mono' }, tools.gflow || 'not installed (mock driver only)')),
      h('tr', {}, h('td', {}, 'gflow profiles'), h('td', { class: 'small mono' }, tools.gflow_home || 'gflow default')),
      h('tr', {}, h('td', {}, 'data folder'), h('td', { class: 'small mono' }, health.data_dir || '?')),
      h('tr', {}, h('td', {}, 'downloads folder'), h('td', { class: 'small mono' }, (health.downloads && health.downloads.root) || tools.tools_dir || '?')),
      h('tr', {}, h('td', {}, 'flow downloads'), h('td', { class: 'small mono' }, (health.downloads && health.downloads.flow) || '?')),
    )),
  ));

  app.appendChild(h('div', { class: 'panel', style: 'margin-bottom:14px' },
    h('h2', {}, 'Drivers & models'),
    h('p', { class: 'muted small' }, 'Model lists are read from the driver at runtime. The video model preference is matched against whatever Flow currently offers — never hardcoded.'),
    (health.capabilities && health.capabilities.catalog_source === 'fallback')
      ? h('div', { class: 'banner warn' }, 'This list is a placeholder: gflow did not answer, so the model labels below are not verified.')
      : null,
    (!health.capabilities || health.capabilities.error)
      ? h('div', { class: 'banner err' }, `Driver unavailable: ${health.capabilities?.error || 'unknown error'}`)
      : h('div', { class: 'stack' },
          h('div', {}, h('h3', {}, 'Image models'), h('div', { class: 'small mono' }, (health.capabilities.image_models || []).join(', ') || '—')),
          h('div', {}, h('h3', {}, 'Video models'), h('div', { class: 'small mono' }, (health.capabilities.video_models || []).join(', ') || '—')),
          h('div', {}, h('h3', {}, 'Notes'), h('div', { class: 'small' }, (health.capabilities.notes || []).join(' · ') || '—'))),
  ));

  app.appendChild(h('div', { class: 'panel' },
    h('h2', {}, 'Master prompt templates'),
    h('p', { class: 'muted small' }, 'A template is the "skill": topic + template → manifest. Built-ins are read-only; saving with the same id overrides them via data/templates/<id>.json.'),
    h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'Template'), h('th', {}, 'Description'), h('th', {}, ''))),
      h('tbody', {}, templates.map((template) => h('tr', {},
        h('td', {}, h('strong', {}, template.name), h('div', { class: 'small mono muted' }, template.id)),
        h('td', { class: 'small' }, template.description),
        h('td', { class: 'actions' }, btn('Edit', () => editTemplate(template))),
      )))),
  ));
}

function narrationSettingsPanel(settings) {
  const pick = (key, choices) => {
    const select = h('select', {});
    for (const [value, label] of choices) {
      select.appendChild(h('option', { value, selected: String(settings[key]) === value }, label));
    }
    select.dataset.key = key;
    return select;
  };
  const field = (key, label, attrs = {}) => {
    const input = h('input', { value: settings[key] ?? '', ...attrs });
    input.dataset.key = key;
    return h('label', { class: 'field' }, label, input);
  };

  const row1 = h('div', { class: 'grid cols-3' },
    h('label', { class: 'field' }, 'tts provider',
      pick('tts_provider', [['off', 'Off — no narration'], ['voicestudio', 'VoiceStudio (local server)'],
                            ['mock', 'Mock placeholder (offline)']])),
    h('label', { class: 'field' }, 'word timestamps',
      pick('align_provider', [['proportional', 'proportional — no model needed'],
                              ['whisperx', 'whisperX — forced alignment']])),
    h('label', { class: 'field' }, 'subtitles',
      pick('captions', [['sidecar', 'Write a .srt next to the video'],
                        ['burn', 'Burn into the picture'], ['off', 'No subtitles']])));

  const row2 = h('div', { class: 'grid cols-3' },
    field('voicestudio_url', 'VoiceStudio URL', { placeholder: 'http://127.0.0.1:3900' }),
    field('tts_voice', 'voice profile', { placeholder: 'default' }),
    field('tts_model', 'model', { placeholder: 'omnivoice' }));

  const row3 = h('div', { class: 'grid cols-3' },
    field('tts_speed', 'speed', { type: 'number', step: '0.05', min: '0.25', max: '4' }),
    field('narration_max_speedup', 'max speed-up to fit a clip', { type: 'number', step: '0.05', min: '1', max: '2' }),
    field('caption_max_chars', 'subtitle line length', { type: 'number', step: '1', min: '10', max: '200' }));

  const row4 = h('div', { class: 'grid cols-3' },
    field('whisperx_model', 'whisperX model', { placeholder: 'base.en' }),
    field('whisperx_python', 'whisperX python (blank = this one)', { placeholder: '' }),
    field('narration_language', 'language', { placeholder: 'en' }));

  const probe = h('div', { class: 'small muted' }, 'checking the voice server…');
  const save = h('button', {
    class: 'btn primary', onclick: async () => {
      const values = {};
      panel.querySelectorAll('[data-key]').forEach((input) => {
        values[input.dataset.key] = input.type === 'number' ? Number(input.value) : input.value;
      });
      try {
        await api('/api/settings', { method: 'PATCH', body: { values } });
        toast('narration settings saved');
        renderSettings();
      } catch (error) { toast(error.message, true); }
    },
  }, 'Save narration settings');

  const panel = h('div', { class: 'panel', style: 'margin-bottom:14px' },
    h('h2', {}, 'Narration (text to speech)'),
    h('p', { class: 'muted small' },
      'The script comes from the manifest (Claude writes one line per scene). VoiceStudio runs on this machine and is '
      + 'reached over HTTP — start it with scripts\\voice\\start-voicestudio.bat. No cloud service is involved.'),
    probe, row1, row2, row3, row4,
    h('div', { class: 'row', style: 'margin-top:12px' }, save,
      h('button', { class: 'btn', onclick: () => renderSettings() }, 'Reload')));

  api('/api/voice/status?refresh=1').then((voice) => {
    probe.innerHTML = '';
    probe.appendChild(h('span', { class: `pill ${voice.ok ? 'ok' : 'warn'}` },
      `${voice.backend || voice.provider}: ${voice.ok ? 'reachable' : 'not reachable'}`));
    probe.appendChild(h('span', {}, ' ' + (voice.detail || '')));
    probe.appendChild(h('div', { class: 'small muted', style: 'margin-top:4px' },
      `timestamps: ${voice.aligner}`
      + (voice.whisperx && voice.whisperx.ok ? ` · ${voice.whisperx.detail}` : ' · whisperX not installed (proportional spacing)')
      + (voice.dir ? ` · ${voice.dir}` : '')));
    if (!voice.ok) {
      probe.appendChild(h('div', { class: 'small muted', style: 'margin-top:4px' },
        'Not installed? setup.bat step 6 installs it, or run scripts\\win\\install_voice.py --tools "D:\\Tools". '
        + 'Set the provider to "mock placeholder" to try the narration flow without a model.'));
    }
    if (voice.ok && voice.engine_available === false) {
      probe.appendChild(h('div', { class: 'banner warn', style: 'margin-top:6px' },
        `engine "${voice.model}" cannot run on this server: `,
        voice.engine_reason || 'no reason reported', '. ',
        voice.engine_hint || '',
        (voice.available_engines && voice.available_engines.length
          ? '' : ' No engine is available at all.')));
    }
    if (voice.ok && voice.available_engines && voice.available_engines.length) {
      probe.appendChild(h('div', { class: 'small muted', style: 'margin-top:4px' },
        `engines that can run here: ${voice.available_engines.join(', ')}`));
    }
  }).catch(() => { probe.textContent = 'voice server status unavailable'; });

  return panel;
}

function editTemplate(template) {
  const name = h('input', { value: template.name });
  const body = h('textarea', { class: 'code' }, template.body);
  const host = document.getElementById('templateHost');
  if (host) host.remove();
  const panel = h('div', { class: 'panel', id: 'templateHost', style: 'margin-top:14px' },
    h('h2', {}, `Edit ${template.id}`),
    h('label', { class: 'field' }, 'Name', name),
    h('div', { style: 'margin-top:10px' }, h('p', { class: 'muted small' },
      'Placeholders: {topic} {scene_count} {target_duration} {style} {tone} {aspect_ratio} {language} {schema}')),
    body,
    h('div', { class: 'row', style: 'margin-top:10px' },
      h('button', {
        class: 'btn primary', onclick: async () => {
          try {
            await api('/api/templates', { method: 'POST', body: { id: template.id, name: name.value, description: template.description, body: body.value } });
            toast('template saved'); renderSettings();
          } catch (error) { toast(error.message, true); }
        },
      }, 'Save template'),
      h('button', {
        class: 'btn', onclick: async () => {
          try {
            await api(`/api/templates/${template.id}`, { method: 'DELETE' });
            toast('reverted to built-in'); renderSettings();
          } catch (error) { toast(error.message, true); }
        },
      }, 'Revert'),
      h('button', { class: 'btn ghost', onclick: () => panel.remove() }, 'Close')),
  );
  app.appendChild(panel);
}

/* ------------------------------------------------------------------ help -- */
function renderHelp() {
  stopPolling();
  app.innerHTML = '';
  const sections = [
    ['How a run works', [
      'Stage A — topic + master prompt → one JSON manifest (Claude API, or manual Claude Desktop). Nothing is spent, everything is editable.',
      'Stage B — one character reference per character, minted in Flow (face + body triptych).',
      'Stage C — one still per scene, referencing the character images (uploaded into the acting account\'s Flow project).',
      'Stage D — one clip per scene: image-to-video with the animation prompt, optional end-frame interpolation toward the next scene.',
      'Stage E — narration: the per-scene script Claude wrote is spoken by the local VoiceStudio server, then every word gets a timestamp (whisperX, or proportional spacing when no model is installed).',
      'Stage F — clips normalized with ffmpeg (resolution, fps, SAR, no audio) then concatenated; each narration line is fitted to its clip (padded, sped up to 1.15x, or cut at the last word that fits), subtitles are written or burned in, and optional music is mixed under it.',
    ]],
    ['Accounts & credits', [
      'Each account is a gflow-cli profile with its own persistent Chrome profile, so re-auth only happens when a session genuinely expires.',
      'Credits are re-read before every paid (video) call. Below the threshold the account is marked low/exhausted.',
      'By default a low balance pauses the project and asks you to approve the switch. Silent auto-failover is opt-in in Accounts.',
      'Asset ids are account-scoped: after a switch, character images and stills are re-uploaded into the new account automatically. Nothing is reused across accounts.',
    ]],
    ['Narration', [
      'The script comes from the manifest: one `narration` line per scene, plus project.voiceover. Claude writes it in Stage A; edit it in the Manifest tab.',
      'Text to speech is local: VoiceStudio runs on 127.0.0.1:3900 (scripts\\voice\\start-voicestudio.bat). Set tts_provider = mock to try the flow without a model.',
      'Timestamps are word-level. Without whisperX they are spread across the audio by word length — good enough for clean TTS, and no model download.',
      'The picture is never stretched to fit the voice: a line that overflows is sped up (max 1.15x by default) or cut, and the log names the words that were dropped.',
      'Subtitles: sidecar writes final.srt next to the video and beside the copy in the downloads folder; burn re-encodes once with them in the picture.',
    ]],
    ['What this pipeline will not do', [
      'It will not solve CAPTCHAs or verification challenges. If Flow asks, the project pauses and you finish it in a real browser window — that is the only supported path.',
      'It will not strip watermarks or otherwise alter provenance/attribution in generated output.',
      'It will not create accounts, rotate them to evade quotas, or run more concurrent Flow sessions than you have accounts.',
      'Read docs/COMPLIANCE.md before pointing this at production, and price the official Veo/Imagen API route first — it removes the browser-automation risk entirely.',
    ]],
  ];
  app.appendChild(h('h1', {}, 'How to drive this'));
  app.appendChild(h('div', { class: 'grid cols-2', style: 'margin-top:14px' },
    sections.map(([title, lines]) => h('div', { class: 'panel' },
      h('h2', {}, title),
      h('ul', { class: 'small' }, lines.map((line) => h('li', {}, line))))),
  ));
  app.appendChild(h('div', { class: 'panel', style: 'margin-top:14px' },
    h('h2', {}, 'Before the first real run'),
    h('ol', { class: 'small' },
      h('li', {}, 'pipx install gflow-cli, then gflow auth login --profile <name> for each account.'),
      h('li', {}, 'python scripts/check_gflow.py --profile <name> — verifies the binary, model catalog, credits, and prints the exact argv each stage emits.'),
      h('li', {}, 'Set driver = gflow in Settings, add the accounts on the Accounts page, refresh credits.'),
      h('li', {}, 'Run one scene first (Re-run → image,clip) and check real output before launching a full project.')),
  ));
}

/* ---------------------------------------------------------------- router -- */
async function render() {
  try {
    if (state.page === 'projects') await renderProjects();
    else if (state.page === 'accounts') await renderAccounts();
    else if (state.page === 'settings') await renderSettings();
    else if (state.page === 'help') renderHelp();
    else if (state.page === 'project') renderProjectPage();
  } catch (error) {
    toast(error.message, true);
    app.appendChild(h('div', { class: 'banner err' }, error.message));
  }
}

boot();
