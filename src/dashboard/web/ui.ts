/**
 * The dashboard's browser UI (D3): read-only screens over the contract API.
 *
 * Shipped as strings so the host image needs no extra build step or asset
 * copy. Three files, all same-origin, so the CSP can forbid inline code:
 *
 * - the page is static markup with no data and no inline script or style;
 * - the script renders every value with `textContent` (never `innerHTML`),
 *   keeps the CSRF token in memory only (no web storage), and sends no
 *   request outside `/api/v1`;
 * - any 401 returns to the login form.
 *
 * Text is Italian, like the operator's other tools.
 */

export const UI_CSP =
  "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; " +
  "frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

export const INDEX_HTML = `<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="referrer" content="no-referrer" />
    <title>NanoClaw · Amministrazione</title>
    <link rel="stylesheet" href="/app.css" />
    <script src="/app.js" defer></script>
  </head>
  <body>
    <div id="login" class="login" hidden>
      <form id="login-form" class="card login-card" autocomplete="off">
        <div class="brand dark">NanoClaw<small>Console privata</small></div>
        <label class="field">Password
          <input id="password" type="password" autocomplete="current-password" required minlength="12" maxlength="1024" />
        </label>
        <button class="primary" type="submit">Accedi</button>
        <p id="login-message" class="message" role="alert"></p>
      </form>
    </div>
    <div id="app" class="shell" hidden>
      <aside>
        <div class="brand">NanoClaw<small>Console privata</small></div>
        <nav aria-label="Sezioni">
          <button type="button" data-view="overview">Panoramica</button>
          <button type="button" data-view="agents">Agenti</button>
          <button type="button" data-view="channels">Canali</button>
          <button type="button" data-view="sessions">Sessioni</button>
          <button type="button" data-view="model">Modello e provider</button>
          <button type="button" data-view="backups">Backup</button>
          <button type="button" data-view="updates">Aggiornamenti</button>
        </nav>
        <p class="aside-note">Sola lettura: le operazioni arriveranno nelle prossime versioni.</p>
      </aside>
      <main>
        <header>
          <div><p class="eyebrow">Amministrazione</p><h1 id="title">Panoramica</h1></div>
          <div class="account">
            <button id="refresh" class="secondary" type="button">Aggiorna</button>
            <button id="logout" class="secondary" type="button">Esci</button>
          </div>
        </header>
        <p id="message" class="message" role="status" aria-live="polite"></p>
        <section id="view" aria-labelledby="title"></section>
      </main>
    </div>
  </body>
</html>
`;

export const APP_CSS = `:root {
  color-scheme: light;
  --ink: #18221c; --muted: #52635a; --line: #dce5de; --surface: #fff; --canvas: #f3f7f2;
  --brand: #195d43; --brand-dark: #124630; --soft: #e7f2e9; --warning: #7b4b00; --warning-bg: #fff5dd;
  --danger: #8a1f1f; --danger-bg: #fdeaea;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; font: 16px/1.5 system-ui, sans-serif; color: var(--ink); background: var(--canvas); }
button { font: inherit; cursor: pointer; }
button:focus-visible, input:focus-visible { outline: 3px solid #a8d6b8; outline-offset: 2px; }
.shell { min-height: 100vh; display: grid; grid-template-columns: 238px minmax(0, 1fr); }
aside { background: #10271c; color: #fff; padding: 30px 18px; }
.brand { font-size: 1.45rem; font-weight: 760; letter-spacing: -.04em; padding: 0 12px 28px; }
.brand small { display: block; color: #b5d2bd; font-size: .75rem; font-weight: 500; letter-spacing: .07em; text-transform: uppercase; }
.brand.dark { color: var(--ink); padding: 0 0 18px; }
.brand.dark small { color: var(--muted); }
nav { display: grid; gap: 5px; }
nav button { border: 0; border-radius: 10px; text-align: left; width: 100%; color: #d6e7da; background: transparent; padding: 11px 13px; }
nav button[aria-current="page"] { background: #286b4c; color: #fff; font-weight: 700; }
.aside-note { margin: 34px 12px 0; color: #b5d2bd; font-size: .85rem; }
main { min-width: 0; max-width: 1420px; padding: 28px clamp(20px, 4vw, 52px) 70px; }
header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 18px; }
.eyebrow { margin: 0; color: var(--brand); font-size: .8rem; font-weight: 750; text-transform: uppercase; letter-spacing: .09em; }
h1 { margin: 3px 0 0; font-size: clamp(1.65rem, 3vw, 2.2rem); letter-spacing: -.045em; }
h2 { margin: 0 0 12px; font-size: 1.15rem; letter-spacing: -.02em; }
p { margin: 0 0 12px; }
.account { display: flex; gap: 9px; }
.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 20px; box-shadow: 0 8px 26px #12352208; }
.card.wide { grid-column: 1 / -1; }
.metric { font-size: 2rem; line-height: 1.1; font-weight: 760; letter-spacing: -.05em; margin: 6px 0; }
.muted { color: var(--muted); }
.status { display: inline-flex; align-items: center; gap: 7px; border-radius: 100px; padding: 4px 10px; background: var(--soft); color: var(--brand-dark); font-size: .83rem; font-weight: 700; white-space: nowrap; }
.status::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: #29875b; }
.status.warn { background: var(--warning-bg); color: var(--warning); }
.status.warn::before { background: #b87a18; }
.status.bad { background: var(--danger-bg); color: var(--danger); }
.status.bad::before { background: #c43b3b; }
.row { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px; padding: 14px 0; border-top: 1px solid var(--line); }
.row:first-of-type { border-top: 0; }
.row p { margin: 1px 0 0; color: var(--muted); font-size: .9rem; }
.row button.link { border: 0; background: none; padding: 0; color: var(--brand); font-weight: 700; text-align: left; }
.primary, .secondary { border-radius: 9px; padding: 9px 13px; font-weight: 680; }
.primary { background: var(--brand); color: #fff; border: 1px solid var(--brand); }
.secondary { background: #fff; color: var(--brand); border: 1px solid #a6c9b3; }
.message { min-height: 1.5em; color: var(--brand-dark); font-size: .92rem; }
.message.error { color: var(--danger); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 18px; margin: 0; }
dt { color: var(--muted); }
dd { margin: 0; font-weight: 600; }
.login { min-height: 100vh; display: grid; place-items: center; padding: 20px; }
.login-card { width: min(420px, 100%); display: grid; gap: 14px; }
.field { display: grid; gap: 5px; font-weight: 650; }
.field input { border: 1px solid #abc0b0; border-radius: 9px; padding: 10px 11px; font: inherit; }
@media (max-width: 800px) {
  .shell { display: block; }
  aside { padding: 15px; }
  nav { display: flex; overflow-x: auto; }
  nav button { white-space: nowrap; width: auto; }
  .aside-note { display: none; }
  .grid { grid-template-columns: 1fr; }
}
`;

export const APP_JS = String.raw`'use strict';
(() => {
  let csrf = null;
  let agentLabels = new Map();
  let current = 'overview';
  const $ = (id) => document.getElementById(id);

  const TITLES = {
    overview: 'Panoramica', agents: 'Agenti', channels: 'Canali', sessions: 'Sessioni',
    model: 'Modello e provider', backups: 'Backup', updates: 'Aggiornamenti',
  };
  const ERRORS = {
    invalid_credentials: 'Password errata.',
    login_throttled: 'Troppi tentativi. Riprova più tardi.',
    unauthenticated: 'Sessione scaduta: accedi di nuovo.',
    not_implemented: 'Non ancora disponibile: arriverà con il servizio operativo.',
    rate_limited: 'Troppe richieste: attendi un minuto.',
    upstream_unavailable: 'Il servizio NanoClaw non risponde.',
    upstream_invalid: 'Risposta del servizio non valida.',
    release_unknown: 'Release installata non determinabile.',
    not_found: 'Elemento non trovato.',
  };
  const STATES = {
    healthy: ['Operativo', ''], unhealthy: ['Non sano', 'bad'], stopped: ['Fermo', 'warn'], unknown: ['Sconosciuto', 'warn'],
    connected: ['Connesso', ''], disconnected: ['Disconnesso', 'bad'], reachable: ['Raggiungibile', ''], unreachable: ['Non raggiungibile', 'bad'],
    running: ['In esecuzione', ''], idle: ['Inattivo', 'warn'], active: ['Attiva', ''], closed: ['Chiusa', 'warn'],
    local: ['LLM locale', ''], external: ['Provider esterno', 'warn'], mixed: ['Configurazione mista', 'warn'], unconfigured: ['Non configurato', 'warn'],
  };

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (name === 'class') node.className = value;
      else if (name === 'onclick') node.addEventListener('click', value);
      else node.setAttribute(name, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  const status = (key) => {
    const [label, tone] = STATES[key] || [String(key), 'warn'];
    return el('span', { class: 'status ' + tone }, label);
  };
  const when = (iso) => (iso ? new Date(iso).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' }) : '—');
  const card = (title, ...body) => el('article', { class: 'card' }, el('h2', {}, title), ...body);
  const wide = (title, ...body) => el('article', { class: 'card wide' }, title ? el('h2', {}, title) : null, ...body);
  const row = (main, sub, right) => el('div', { class: 'row' }, el('div', {}, main, sub ? el('p', {}, sub) : null), right);
  const pairs = (entries) => el('dl', {}, entries.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v)]));

  function say(text, error) {
    const box = $('message');
    box.textContent = text || '';
    box.className = 'message' + (error ? ' error' : '');
  }

  async function api(method, path, body) {
    const headers = {};
    if (body !== undefined) headers['content-type'] = 'application/json';
    if (method !== 'GET' && csrf) headers['x-csrf-token'] = csrf;
    let response;
    try {
      response = await fetch('/api/v1' + path, {
        method, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      return { status: 0, data: null, code: 'upstream_unavailable' };
    }
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    const code = data && data.error ? data.error.code : null;
    if (response.status === 401 && path !== '/session') showLogin(ERRORS.unauthenticated);
    return { status: response.status, data, code };
  }

  function showLogin(text) {
    csrf = null;
    $('app').hidden = true;
    $('login').hidden = false;
    $('login-message').textContent = text || '';
    $('password').focus();
  }

  function showApp() {
    $('login').hidden = true;
    $('app').hidden = false;
    render(current);
  }

  async function renderOverview(view) {
    const [overview, agents] = await Promise.all([api('GET', '/overview'), api('GET', '/agents')]);
    if (overview.status !== 200) return fail(view, overview);
    const o = overview.data;
    const agentItems = agents.status === 200 ? agents.data.items : [];
    view.append(el('div', { class: 'grid' },
      card('Release', el('div', { class: 'metric' }, o.release.version), el('p', { class: 'muted' }, 'Revisione ' + o.release.revision.slice(0, 8))),
      card('Canali', el('div', { class: 'metric' }, o.channels.connected + ' / ' + o.channels.total), el('p', { class: 'muted' }, 'connessi')),
      card('Agenti', el('div', { class: 'metric' }, agentItems.length), el('p', { class: 'muted' }, agentItems.filter((a) => a.state === 'running').length + ' in esecuzione')),
      wide('Servizi', o.services.map((s) => row(s.name, null, status(s.state))), row('Modello', null, status(o.llm.state))),
      o.alerts.length ? wide('Avvisi', o.alerts.map((a) => row(a.code.replaceAll('_', ' '), null, status(a.severity === 'info' ? 'active' : 'unhealthy')))) : null,
    ), el('p', { class: 'muted' }, 'Controllo delle ' + when(o.checked_at)));
  }

  async function renderAgents(view) {
    const agents = await api('GET', '/agents');
    if (agents.status !== 200) return fail(view, agents);
    agentLabels = new Map(agents.data.items.map((a) => [a.id, a.label]));
    const detail = el('div', {});
    view.append(el('div', { class: 'grid' },
      wide(null, agents.data.items.length ? agents.data.items.map((a) => row(
        el('button', { class: 'link', type: 'button', onclick: () => renderAgent(detail, a.id) }, a.label),
        (a.provider || 'provider predefinito') + ' · ' + (a.model || 'modello predefinito') + ' · ' +
          a.sessions.active + (a.sessions.active === 1 ? ' sessione attiva' : ' sessioni attive') + ' su ' + a.sessions.total,
        status(a.state),
      )) : el('p', { class: 'muted' }, 'Nessun agente.')),
    ), detail);
  }

  async function renderAgent(target, id) {
    target.replaceChildren();
    const agent = await api('GET', '/agents/' + encodeURIComponent(id));
    if (agent.status !== 200) return fail(target, agent);
    const a = agent.data;
    target.append(wide('Dettaglio: ' + a.label, pairs([
      ['Stato', status(a.state)],
      ['Creato', when(a.created_at)],
      ['Provider', a.provider || 'predefinito'],
      ['Modello', a.model || 'predefinito'],
      ['Container', status(a.container.state)],
      ['Immagine personalizzata', a.container.image.derived ? 'sì' : 'no'],
      ['Accesso a ncl', a.capabilities.cli_scope],
      ['Skill', a.capabilities.skills === 'all' ? 'tutte' : 'selezionate'],
      ['Pacchetti', a.capabilities.packages],
      ['Server MCP', a.capabilities.mcp_servers],
      ['Mount aggiuntivi', a.capabilities.additional_mounts],
    ])));
  }

  async function renderChannels(view) {
    const channels = await api('GET', '/channels');
    if (channels.status !== 200) return fail(view, channels);
    view.append(wide(null, channels.data.items.length
      ? channels.data.items.map((c) => row(c.type, c.chats + ' chat', status(c.state)))
      : el('p', { class: 'muted' }, 'Nessun canale.')));
  }

  async function renderSessions(view, cursor) {
    if (!agentLabels.size) {
      const agents = await api('GET', '/agents');
      if (agents.status === 200) agentLabels = new Map(agents.data.items.map((a) => [a.id, a.label]));
    }
    const sessions = await api('GET', '/sessions' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''));
    if (sessions.status !== 200) return fail(view, sessions);
    const list = wide(null, sessions.data.items.length ? sessions.data.items.map((s) => row(
      agentLabels.get(s.agent) || 'Agente',
      'Ultima attività: ' + when(s.last_activity) + ' · container ' + (STATES[s.container] || [s.container])[0].toLowerCase(),
      status(s.state),
    )) : el('p', { class: 'muted' }, 'Nessuna sessione.'));
    view.append(list);
    if (sessions.data.next_cursor) {
      const more = el('button', { class: 'secondary', type: 'button', onclick: () => { more.remove(); renderSessions(view, sessions.data.next_cursor); } }, 'Carica altre');
      view.append(more);
    }
  }

  async function renderModel(view) {
    const model = await api('GET', '/model-settings');
    if (model.status !== 200) return fail(view, model);
    const m = model.data;
    view.append(el('div', { class: 'grid' },
      wide('Configurazione per tutti gli agenti', pairs([
        ['Modalità', status(m.mode)],
        ['Provider', m.provider || '—'],
        ['Modello', m.model || '—'],
        ['Endpoint configurato', m.endpoint_status.configured ? 'sì' : 'no'],
        ['Raggiungibilità', status(m.endpoint_status.state)],
        ['Agenti allineati', m.agents.matching + ' su ' + m.agents.total],
      ])),
      wide(null, el('p', { class: 'muted' }, 'Il cambio di provider e modello per tutti gli agenti arriverà in una prossima versione, con verifica preventiva e rollback.')),
    ));
  }

  async function renderPending(view, path) {
    const result = await api('GET', path);
    if (result.status === 200) {
      view.append(wide(null, el('p', { class: 'muted' }, 'Dati disponibili.')));
      return;
    }
    fail(view, result);
  }

  function fail(view, result) {
    if (result.status === 401) return;
    view.append(wide(null, el('p', { class: 'muted' }, ERRORS[result.code] || 'Errore (' + (result.code || result.status) + ').')));
  }

  async function render(name) {
    current = name;
    $('title').textContent = TITLES[name];
    for (const button of document.querySelectorAll('nav button')) {
      if (button.dataset.view === name) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
    say('');
    const view = $('view');
    view.replaceChildren();
    const renderers = {
      overview: renderOverview, agents: renderAgents, channels: renderChannels, sessions: renderSessions, model: renderModel,
      backups: (v) => renderPending(v, '/backups'), updates: (v) => renderPending(v, '/releases'),
    };
    await renderers[name](view);
  }

  document.addEventListener('DOMContentLoaded', async () => {
    for (const button of document.querySelectorAll('nav button')) button.addEventListener('click', () => render(button.dataset.view));
    $('refresh').addEventListener('click', () => render(current));
    $('logout').addEventListener('click', async () => {
      await api('DELETE', '/session');
      showLogin('Sei uscito.');
    });
    $('login-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const field = $('password');
      const result = await api('POST', '/session', { password: field.value });
      field.value = '';
      if (result.status === 200) {
        csrf = result.data.csrf_token;
        showApp();
      } else {
        $('login-message').textContent = ERRORS[result.code] || 'Accesso non riuscito.';
      }
    });
    const session = await api('GET', '/session');
    if (session.status === 200) {
      csrf = session.data.csrf_token;
      showApp();
    } else showLogin('');
  });
})();
`;
