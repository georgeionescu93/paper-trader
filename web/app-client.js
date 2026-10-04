/* =====================================================================
   AI Stock Portfolio & Paper Trader - web client

   Talks only to the frozen JSON API in app_web.py (see API.md). Every
   panel mirrors a widget of the desktop build, and every number is
   formatted the way the Tk labels formatted it, so the two front ends
   read identically.

   The engine itself lives in the SERVER process (engine.py +
   app_web.py's Scheduler): this file is pure presentation. Closing the
   tab, or the whole laptop, does not stop the trading loop.
   ===================================================================== */
'use strict';

/* ------------------------------------------------------------ config */
const POLL_MS = 5000;          // dashboard refresh (server caches 2s)
const LOG_POLL = true;         // pull new activity lines on every poll

/* ------------------------------------------------------------- state */
const S = {
  booted: false,
  profiles: [],
  profileId: null,
  timeframes: [],
  timeframe: 'Auto (all timeframes)',
  models: [],
  model: '',
  state: null,
  toasts: new Set(),
  logSeq: 0,
  selectedTrade: null,
  selectedRec: null,
  autoPending: false,
  timer: null,
  busy: false,
  health: null,
  /* accounts */
  user: null,               // {id, email, is_owner, created_at} | null
  authMode: 'login',        // 'login' | 'register'
  registrationOpen: true,   // false => the server requires an invite code
  authPending: false,       // a /api/login or /api/register call is in flight
};

/* --------------------------------------------------------------- dom */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

/* =====================================================================
   FORMATTERS - mirroring the Python f-strings of the desktop build
   ===================================================================== */
function money(value, decimals = 2) {
  const v = Number(value);
  if (!isFinite(v)) return '0.00';
  return v.toLocaleString('en-US', {
    minimumFractionDigits: decimals, maximumFractionDigits: decimals,
  });
}

/** Python's `:+,.2f` - always shows the sign. */
function signedMoney(value, decimals = 2) {
  const v = Number(value) || 0;
  return (v >= 0 ? '+' : '-') + money(Math.abs(v), decimals);
}

function pct(value, decimals = 2) {
  const v = Number(value) || 0;
  return (v >= 0 ? '+' : '-') + Math.abs(v).toFixed(decimals) + '%';
}

function trimZeros(text) {
  if (text.indexOf('.') === -1) return text;
  return text.replace(/\.?0+$/, '');
}

/**
 * Python's `f"{v:.4g}"` - 4 significant digits, exponent form when the
 * exponent is < -4 or >= 4 (so 1,000,000 renders as 1e+06, as Python does).
 * Quantities in the desktop tables used exactly this format.
 */
function pyG(value, precision = 4) {
  const v = Number(value);
  if (!isFinite(v)) return '0';
  if (v === 0) return '0';
  const exp = Math.floor(Math.log10(Math.abs(v)));
  if (exp < -4 || exp >= precision) {
    const parts = v.toExponential(precision - 1).split('e');
    const mant = trimZeros(parts[0]);
    const sign = parts[1][0] === '-' ? '-' : '+';
    let digits = parts[1].replace(/^[+-]/, '');
    if (digits.length < 2) digits = '0' + digits;
    return `${mant}e${sign}${digits}`;
  }
  const decimals = Math.max(0, precision - 1 - exp);
  return trimZeros(v.toFixed(decimals));
}

function currencySymbol(state) {
  return (state && state.account && state.account.symbol) || '$';
}

/* =====================================================================
   HTTP
   ===================================================================== */
/**
 * `opts.suppressAuthHandling` keeps a 401 inside the caller: the login and
 * registration endpoints answer 401/429 with their own error text, and the
 * panel has to show that text instead of bouncing to "session expired".
 */
async function api(method, path, body, opts) {
  const options = {
    method,
    headers: { 'Accept': 'application/json' },
    credentials: 'same-origin',
  };
  if (body !== undefined && body !== null) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (err) {
    payload = null;
  }
  const suppress = Boolean(opts && opts.suppressAuthHandling);
  if (response.status === 401 && !suppress) {
    showLogin('Your session expired - please sign in again.');
    throw new Error('Not authenticated');
  }
  if (!response.ok) {
    let message = (payload && payload.error) || '';
    if (!message && response.status === 429) {
      message = 'Too many attempts - please wait a moment and try again.';
    }
    if (!message) message = `${response.status} ${response.statusText}`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return payload || {};
}

/* =====================================================================
   TOASTS - the web stand-in for the desktop's themed dark popups
   ===================================================================== */
function toast(title, message, kind = 'info', ttl = 8000) {
  const key = `${title}|${message}|${kind}`;
  if (S.toasts.has(key)) return;
  S.toasts.add(key);
  setTimeout(() => S.toasts.delete(key), ttl);

  const node = el('div', `toast ${kind}`);
  node.appendChild(el('h4', null, title));
  if (message) node.appendChild(el('p', null, message));
  node.addEventListener('click', () => node.remove());
  $('toasts').appendChild(node);
  setTimeout(() => node.remove(), ttl);
}

function fail(err, title = 'Error') {
  const message = (err && err.message) || String(err);
  toast(title, message, 'error', 12000);
}

/* =====================================================================
   MODALS - dark replacements for messagebox / simpledialog
   ===================================================================== */
function modalOpen({ title, message, icon = 'ℹ️', kind = 'alert',
                     value = '', numeric = false, choices = null,
                     okLabel = 'OK', cancelLabel = 'Cancel' }) {
  return new Promise((resolve) => {
    const root = $('modal-root');
    const input = $('modal-input');
    const select = $('modal-select');
    const error = $('modal-error');
    const ok = $('modal-ok');
    const cancel = $('modal-cancel');

    $('modal-title').textContent = title || 'Notice';
    $('modal-message').textContent = message || '';
    $('modal-icon').textContent = icon;
    error.textContent = '';
    ok.textContent = okLabel;
    cancel.textContent = cancelLabel;
    cancel.classList.toggle('hidden', kind === 'alert');

    input.classList.add('hidden');
    select.classList.add('hidden');
    if (choices) {
      select.innerHTML = '';
      choices.forEach((choice) => {
        const option = el('option', null, choice.label !== undefined ? choice.label : choice);
        option.value = choice.value !== undefined ? choice.value : choice;
        select.appendChild(option);
      });
      select.value = value;
      select.classList.remove('hidden');
    } else if (kind === 'prompt') {
      input.type = numeric ? 'text' : 'text';
      input.inputMode = numeric ? 'decimal' : 'text';
      input.value = value === null || value === undefined ? '' : String(value);
      input.classList.remove('hidden');
    }

    root.classList.remove('hidden');
    root.setAttribute('aria-hidden', 'false');
    setTimeout(() => {
      if (!input.classList.contains('hidden')) { input.focus(); input.select(); }
      else if (!select.classList.contains('hidden')) select.focus();
      else ok.focus();
    }, 20);

    function close(result) {
      root.classList.add('hidden');
      root.setAttribute('aria-hidden', 'true');
      ok.removeEventListener('click', onOk);
      cancel.removeEventListener('click', onCancel);
      input.removeEventListener('keydown', onKey);
      select.removeEventListener('keydown', onKey);
      root.querySelector('.modal-backdrop').removeEventListener('click', onCancel);
      resolve(result);
    }
    function onOk() {
      if (choices) { close(select.value); return; }
      if (kind === 'prompt') {
        const raw = input.value.trim();
        if (!raw) { error.textContent = 'Please enter a value.'; return; }
        if (numeric) {
          const parsed = Number(raw.replace(',', '.'));
          if (!isFinite(parsed)) {
            error.textContent = 'Please enter a valid number.';
            return;
          }
          close(parsed);
          return;
        }
        close(raw);
        return;
      }
      close(true);
    }
    function onCancel() { close(null); }
    function onKey(event) {
      if (event.key === 'Enter') { event.preventDefault(); onOk(); }
      if (event.key === 'Escape') { event.preventDefault(); onCancel(); }
    }

    ok.addEventListener('click', onOk);
    cancel.addEventListener('click', onCancel);
    input.addEventListener('keydown', onKey);
    select.addEventListener('keydown', onKey);
    root.querySelector('.modal-backdrop').addEventListener('click', onCancel);
  });
}

const alertModal = (title, message, kind = 'info') => {
  const icons = { info: 'ℹ️', warning: '⚠️', error: '⛔', success: '✅' };
  return modalOpen({ title, message, kind: 'alert', icon: icons[kind] || 'ℹ️' });
};

const confirmModal = (title, message) =>
  modalOpen({ title, message, kind: 'confirm', icon: '❓',
              okLabel: 'Yes', cancelLabel: 'Cancel' });

const promptModal = (title, message, value = '', numeric = false) =>
  modalOpen({ title, message, kind: 'prompt', icon: '✏️', value, numeric });

/* =====================================================================
   LOGIN / REGISTRATION  (multi-user accounts, one session cookie)
   ===================================================================== */
async function loadSession() {
  const session = await api('GET', '/api/session', null,
                            { suppressAuthHandling: true });
  S.user = session.user || null;
  S.registrationOpen = session.registration_open !== false;
  return session;
}

/** Switches the one login card between sign-in and registration. */
function setAuthMode(mode, keepError) {
  S.authMode = mode === 'register' ? 'register' : 'login';
  const register = S.authMode === 'register';
  const needsCode = register && !S.registrationOpen;

  $('login-hint').textContent = register
    ? (S.registrationOpen
        ? 'Create an account with your e-mail address, a password of at\n'
          + 'least 8 characters and the paper currency to trade in.\n'
          + 'You are signed in straight away.'
        : 'Registration needs an invite code from the operator.\n'
          + 'Pick an e-mail, a password (8+ characters) and a currency.')
    : 'Sign in with your e-mail and password.\n'
      + 'Leave the e-mail blank to use the owner account printed by the server.';

  $('login-email').placeholder = register ? 'E-mail' : 'E-mail (blank = owner account)';
  $('login-email').autocomplete = register ? 'email' : 'username';
  $('register-currency').classList.toggle('hidden', !register);
  $('register-code').classList.toggle('hidden', !needsCode);
  $('btn-show-register').classList.toggle('hidden', register);
  $('btn-show-login').classList.toggle('hidden', !register);
  $('login-submit').textContent = register ? 'Create account' : 'Sign in';
  if (!keepError) $('login-error').textContent = '';

  if (needsCode) $('register-code').focus();
  else $('login-email').focus();
}

function showLogin(errorText) {
  stopPolling();
  $('app-view').classList.add('hidden');
  $('login-view').classList.remove('hidden');
  setAuthMode('login');
  $('login-error').textContent = errorText || '';
  $('login-email').focus();
  // Pick up a change of `registration_open` while keeping the message.
  loadSession().then(() => setAuthMode(S.authMode, true)).catch(() => {});
}

function showApp() {
  $('login-view').classList.add('hidden');
  $('app-view').classList.remove('hidden');
}

function renderUser(user) {
  const pill = $('user-pill');
  if (!pill) return;
  const info = user || null;
  if (!info) {
    pill.textContent = '';
    pill.title = '';
    pill.classList.add('hidden');
    return;
  }
  const owner = Number(info.is_owner) ? ' · owner' : '';
  pill.textContent = (info.email ? String(info.email) : 'owner account') + owner;
  pill.title = `Signed in as ${info.email || 'the owner account'}${owner}`
    + (info.created_at ? ` · account created ${info.created_at}` : '');
  pill.classList.remove('hidden');
}

/** Submits whichever form the login card is showing. */
async function submitAuth(event) {
  event.preventDefault();
  if (S.authPending) return;
  const register = S.authMode === 'register';
  const button = $('login-submit');
  const email = $('login-email').value.trim();
  const password = $('login-password').value;

  const body = register
    ? { email, password, currency: $('register-currency').value || 'USD' }
    : { email, password };
  if (register && !S.registrationOpen) body.code = $('register-code').value.trim();

  S.authPending = true;
  button.disabled = true;
  $('login-error').textContent = '';
  try {
    const payload = await api('POST', register ? '/api/register' : '/api/login',
                              body, { suppressAuthHandling: true });
    S.user = payload.user || S.user;
    $('login-password').value = '';
    if (register) $('register-code').value = '';
    showApp();
    renderUser(S.user);
    try {
      await boot();
    } catch (err) {
      fail(err, 'Could not load the dashboard');
    }
  } catch (err) {
    // The server's own wording, verbatim.
    $('login-error').textContent = err.message || (register
      ? 'Could not create the account.' : 'Sign in failed.');
  } finally {
    S.authPending = false;
    button.disabled = false;
  }
}

/** Drops every cached account-specific value before showing the login card. */
function clearClientState() {
  stopPolling();
  S.booted = false;
  S.state = null;
  S.user = null;
  S.profiles = [];
  S.profileId = null;
  S.selectedTrade = null;
  S.selectedRec = null;
  S.logSeq = 0;
  S.timeframe = 'Auto (all timeframes)';
  S.models = [];
  S.model = '';
  S.autoPending = false;
  if ($('log-list')) $('log-list').innerHTML = '';
  if ($('profile-select')) $('profile-select').innerHTML = '';
  const history = $('history-body');
  if (history) history.innerHTML = '';
  const holdings = $('holdings-body');
  if (holdings) holdings.innerHTML = '';
  const recs = $('recs-body');
  if (recs) recs.innerHTML = '';
  renderUser(null);
}

async function logout() {
  try {
    await api('POST', '/api/logout', {}, { suppressAuthHandling: true });
  } catch (err) { /* the cookie is cleared server-side anyway */ }
  clearClientState();
  showLogin('Signed out.');
}

/* =====================================================================
   BOOTSTRAP + POLLING
   ===================================================================== */
async function boot() {
  if (!S.booted) {
    try { S.health = await api('GET', '/api/health'); } catch (err) { S.health = null; }
    await loadModels();
    S.booted = true;
  }
  await poll(true);
  startPolling();
}

function startPolling() {
  stopPolling();
  if (!$('chk-live').checked) return;
  S.timer = setInterval(() => { poll(false).catch(() => {}); }, POLL_MS);
}

function stopPolling() {
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
}

async function poll(force) {
  const query = force ? '?force=1' : '';
  const payload = await api('GET', `/api/state${query}`);
  S.state = payload;
  S.profiles = payload.profiles || [];
  if (!S.profileId || !S.profiles.some((p) => p.id === payload.current_profile_id)) {
    S.profileId = payload.current_profile_id;
  } else {
    S.profileId = payload.current_profile_id;
  }
  S.timeframes = (payload.trading_timeframes || []).map((t) => t.label);
  if (payload.engine && payload.engine.label) S.timeframe = payload.engine.label;

  renderAll(payload);
  if (LOG_POLL) await pollLog();
  if (activeTab() === 'history') await loadHistory();
}

async function pollLog() {
  try {
    const payload = await api('GET', `/api/log?since=${S.logSeq}`);
    if (payload.next !== undefined) S.logSeq = payload.next;
    if (payload.lines && payload.lines.length) {
      const list = $('log-list');
      const nearBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 40;
      payload.lines.forEach((line) => {
        const item = el('li');
        item.appendChild(el('time', null, line.t));
        item.appendChild(el('span', null, line.text));
        list.appendChild(item);
      });
      while (list.children.length > 400) list.removeChild(list.firstChild);
      if (nearBottom) list.scrollTop = list.scrollHeight;
    }
  } catch (err) { /* the log is best-effort */ }
}

async function loadHistory() {
  if (!S.profileId) { renderHistory([]); return; }
  try {
    const payload = await api('GET', `/api/history?profile_id=${S.profileId}`);
    renderHistory(payload.trades || []);
  } catch (err) { /* keep the previous table */ }
}

/* =====================================================================
   RENDER
   ===================================================================== */
function renderAll(payload) {
  renderTopbar(payload);
  renderProfiles(payload);
  renderAccount(payload);
  renderRisk(payload);
  renderEngine(payload);
  renderRecommendations(payload);
  renderPositions(payload);
  renderModelStatus();
  renderFoot();
}

/* ------------------------------------------------- trading-mode scope
   The engine is shared by every account, so `engine.running` alone says
   nothing about THIS portfolio: only `your_profile_armed` does.        */
function armedForMe(engine) {
  return Boolean(engine && engine.your_profile_armed);
}

function othersArmed(engine) {
  const armed = (engine && engine.armed_profiles) || [];
  return armed.length > 0 && !armedForMe(engine);
}

function tradingScopeText(engine) {
  if (armedForMe(engine)) return 'Trading Mode: your portfolio is armed';
  if (othersArmed(engine)) {
    return 'Trading Mode: the engine is busy with another account\'s portfolio '
      + '- your portfolio is NOT trading';
  }
  if (engine && engine.running) return 'Trading Mode: engine running';
  return 'Trading Mode: idle';
}

function renderTopbar(payload) {
  const engine = payload.engine || {};
  $('server-time').textContent = payload.server_time || '';
  renderUser(payload.user || S.user);

  const market = $('market-pill');
  if (payload.market_open === true) {
    market.textContent = 'market open'; market.className = 'pill on';
  } else if (payload.market_open === false) {
    market.textContent = 'market closed'; market.className = 'pill off';
  } else {
    market.textContent = 'market –'; market.className = 'pill muted';
  }

  const pill = $('engine-pill');
  if (armedForMe(engine)) {
    pill.textContent = `trading mode · your portfolio armed · cycle ${engine.cycle || 0}`;
    pill.className = 'pill on';
  } else if (othersArmed(engine)) {
    pill.textContent = 'engine busy · another account';
    pill.className = 'pill warn';
  } else if (engine.running) {
    pill.textContent = `engine running · cycle ${engine.cycle || 0}`;
    pill.className = 'pill on';
  } else {
    pill.textContent = 'engine idle';
    pill.className = 'pill off';
  }

  const tvBanner = $('banner-tv');
  if (engine.data_available === false) {
    tvBanner.textContent = '⚠️ TradingView data layer unavailable: '
      + (engine.data_error || 'unknown reason')
      + ' — Trading Mode is disabled, existing holdings still refresh.';
    tvBanner.classList.remove('hidden');
  } else {
    tvBanner.classList.add('hidden');
  }

  const engineBanner = $('banner-engine');
  if (engine.last_error) {
    engineBanner.textContent = `⚠️ Last engine problem: ${engine.last_error}`;
    engineBanner.classList.remove('hidden');
  } else {
    engineBanner.classList.add('hidden');
  }
}

function renderProfiles(payload) {
  const select = $('profile-select');
  const wanted = String(S.profileId || '');
  const existing = Array.from(select.options).map((o) => o.value);
  const incoming = S.profiles.map((p) => String(p.id));
  const same = existing.length === incoming.length
    && existing.every((v, i) => v === incoming[i]);
  if (!same) {
    select.innerHTML = '';
    if (!S.profiles.length) {
      const option = el('option', null, '– no profiles yet –');
      option.value = '';
      select.appendChild(option);
    }
    S.profiles.forEach((p) => {
      const option = el('option', null, `${p.name} (${p.currency})`);
      option.value = String(p.id);
      select.appendChild(option);
    });
  }
  if (wanted) select.value = wanted;

  const profile = S.profiles.find((p) => String(p.id) === wanted);
  $('profile-meta').textContent = profile
    ? `Cash ${money(profile.balance)} ${profile.currency} · deposited `
      + `${money(profile.total_deposited)} · auto-trade `
      + `${profile.auto_mode ? 'ON' : 'off'}`
    : 'No profile selected.';

  const hasProfile = Boolean(profile);
  ['btn-deposit', 'btn-delete-profile', 'btn-analyse', 'btn-trading-mode',
   'btn-one-cycle', 'btn-buy', 'btn-sell', 'btn-edit-price',
   'btn-clear-history'].forEach((id) => { $(id).disabled = !hasProfile; });
  $('chk-auto').disabled = !hasProfile;

  if (!S.autoPending) {
    $('chk-auto').checked = Boolean(payload.auto_mode);
  }
}

function renderAccount(payload) {
  const account = payload.account;
  if (!account) {
    $('lbl-cash').textContent = 'Cash Balance: –';
    $('lbl-equity').textContent = 'Portfolio Value: –';
    $('lbl-total').textContent = 'Net Worth: –';
    $('lbl-updated').textContent = 'Prices: –';
    ['lbl-realized', 'lbl-unrealized', 'lbl-overall', 'lbl-avg-pos'].forEach((id) => {
      $(id).textContent = $(id).textContent.split(':')[0] + ': –';
      $(id).className = 'pl muted';
    });
    $('lbl-avg-pos').textContent = 'Avg position P/L: –';
    return;
  }

  const symbol = account.symbol || '$';
  $('lbl-cash').textContent = `Cash Balance: ${symbol}${money(account.cash)}`;
  $('lbl-equity').textContent =
    `Portfolio Value: ${symbol}${money(account.total_portfolio_value)}`;
  $('lbl-total').textContent = `Net Worth: ${symbol}${money(account.net_worth)}`;
  $('lbl-updated').textContent = `Prices as of ${account.prices_as_of || '–'}`
    + (account.stale_count ? ' (some unavailable)' : '');

  // Realized P/L · average % per closed trade
  let realized = `Realized P/L: ${symbol}${signedMoney(account.realized_pnl)}`;
  if (account.avg_trade_pct !== null && account.avg_trade_pct !== undefined) {
    realized += ` · avg ${pct(account.avg_trade_pct)}/trade`;
  }
  setPl('lbl-realized', realized, account.realized_pnl);

  // Unrealized P/L · % of cost basis
  let unrealized = `Unrealized P/L: ${symbol}${signedMoney(account.unrealized_pnl)}`;
  if (account.cost_basis > 0) {
    unrealized += ` · ${pct(account.unrealized_pnl / account.cost_basis * 100)} of cost`;
  }
  setPl('lbl-unrealized', unrealized, account.unrealized_pnl);

  // Overall P/L · % of deposits
  let overall = `Overall P/L: ${symbol}${signedMoney(account.overall_pnl)}`;
  if (account.total_deposited > 0) {
    overall += ` · ${pct(account.overall_pnl / account.total_deposited * 100)} of deposits`;
  }
  setPl('lbl-overall', overall, account.overall_pnl);

  if (account.avg_position_pct === null || account.avg_position_pct === undefined) {
    $('lbl-avg-pos').textContent = 'Avg position P/L: –';
    $('lbl-avg-pos').className = 'pl muted';
  } else {
    $('lbl-avg-pos').textContent = `Avg position P/L: ${pct(account.avg_position_pct)}`;
    $('lbl-avg-pos').className = 'pl ' + (account.avg_position_pct >= 0 ? 'up' : 'down');
  }
}

function setPl(id, text, value) {
  const node = $(id);
  node.textContent = text;
  node.className = 'pl ' + (Number(value) >= 0 ? 'up' : 'down');
}

function renderRisk(payload) {
  const risk = payload.risk;
  const rules = (risk && risk.rules) || null;
  if (rules) {
    $('lbl-risk-rules').textContent =
      `Risk/trade ${rules.risk_per_trade_pct.toFixed(2)}%  •  `
      + `max ${rules.max_open_positions} positions  •  `
      + `max position ${rules.max_position_pct.toFixed(0)}%  •  `
      + `cash floor ${rules.cash_floor_pct.toFixed(0)}%  •  `
      + `stop→breakeven at +${rules.breakeven_at_r.toFixed(0)}R  •  `
      + `trail ${rules.trail_atr_mult}xATR  •  `
      + `target ${rules.take_profit_r.toFixed(0)}R  •  `
      + `slip ${rules.slippage_bps}bps`;
  } else {
    $('lbl-risk-rules').textContent = '';
  }

  const node = $('lbl-risk-state');
  const account = payload.account;
  if (!account || !risk) {
    node.textContent = 'Risk state: –';
    node.className = 'pl muted';
    return;
  }
  const open = (payload.positions || []).filter((p) => p.quantity > 0).length;
  const maxPos = rules ? rules.max_open_positions : '?';
  let text = `Equity ${money(account.net_worth)}`;
  if (risk.day_start_equity) text += ` · today ${pct(risk.day_pnl_pct)}`;
  if (risk.peak_equity) text += ` · from peak ${pct(risk.drawdown_pct)}`;
  text += ` · ${open}/${maxPos} positions · `;
  text += risk.halted ? '⛔ NEW ENTRIES HALTED' : '✅ trading enabled';
  if (risk.halted && risk.halt_reason) text += ` (${risk.halt_reason})`;
  node.textContent = text;
  node.className = 'pl ' + (risk.halted ? 'down' : 'up');
}

function renderEngine(payload) {
  const engine = payload.engine || {};
  const running = Boolean(engine.running);      // the shared engine process
  const mine = armedForMe(engine);              // ... armed for MY portfolio
  const busyElsewhere = othersArmed(engine);    // ... armed for someone else

  const button = $('btn-trading-mode');
  button.textContent = mine ? '⏹️ Trading Mode: your portfolio is armed'
    : busyElsewhere ? '▶️ Trading Mode (engine busy on another account)'
      : '▶️ Trading Mode';
  button.className = mine || !busyElsewhere ? 'btn accent' : 'btn';
  const tvOk = engine.data_available !== false;
  button.disabled = mine ? false : (!tvOk || !S.profileId);
  button.title = !tvOk
    ? 'Trading Mode needs the TradingView data layer.'
    : busyElsewhere
      ? 'The engine is trading another account\'s portfolio right now. '
        + 'Starting Trading Mode arms YOUR portfolio with it.'
      : '';

  const select = $('timeframe-select');
  const wanted = engine.label || S.timeframe;
  if (select.options.length !== S.timeframes.length) {
    select.innerHTML = '';
    S.timeframes.forEach((label) => {
      const option = el('option', null, label);
      option.value = label;
      select.appendChild(option);
    });
  }
  if (wanted) select.value = wanted;

  $('btn-one-cycle').disabled = !tvOk || !S.profileId || running;

  const analysis = payload.analysis || {};
  const statusText = engine.last_status || 'Ready';
  let status = `Status: ${statusText}`;
  status += `  ·  ${tradingScopeText(engine)}`;
  if (mine && engine.next_cycle_in_s !== null
      && engine.next_cycle_in_s !== undefined) {
    status += ` (next scan in ${Math.floor(engine.next_cycle_in_s / 60)}m `
      + `${engine.next_cycle_in_s % 60}s)`;
  }
  if (analysis.running) {
    status += '  ⏳ AI analysis running…';
  }
  $('lbl-status').textContent = status;

  $('btn-analyse').disabled = Boolean(analysis.running) || running || !S.profileId;
  $('btn-analyse').textContent = analysis.running
    ? '⏳ Analysing…' : '🚀 Start AI Market Analysis';

  $('m-cycle').textContent = engine.cycle || 0;
  $('m-scanned').textContent = engine.scanned || 0;
  $('m-fetch').textContent = engine.fetch_s ? `${Number(engine.fetch_s).toFixed(0)}s` : '–';
  $('m-decisions').textContent = engine.decisions || 0;
  $('m-next').textContent = (engine.next_cycle_in_s === null
    || engine.next_cycle_in_s === undefined) ? '–'
    : `${Math.floor(engine.next_cycle_in_s / 60)}m ${engine.next_cycle_in_s % 60}s`;
  $('m-last').textContent = engine.last_cycle_at || '–';
}

function renderRecommendations(payload) {
  const rows = payload.recommendations || [];
  const body = $('recs-body');
  const previousHeight = body.parentElement.scrollHeight;
  body.innerHTML = '';
  $('recs-empty').classList.toggle('hidden', rows.length > 0);

  rows.forEach((rec, index) => {
    const action = String(rec.action || '').toUpperCase();
    const tr = el('tr', 'clickable');
    tr.dataset.index = String(index);

    const actionCell = el('td', `action-${action.split(' ')[0]}`, rec.action || '–');
    tr.appendChild(actionCell);
    tr.appendChild(el('td', null, rec.symbol || '–'));
    tr.appendChild(el('td', null, rec.quantity === undefined || rec.quantity === null
      ? '–' : pyG(rec.quantity)));
    tr.appendChild(el('td', null, rec.amount === undefined || rec.amount === null
      ? '–' : money(rec.amount)));
    tr.appendChild(el('td', 'wide', rec.reason || ''));

    // Clicking a row fills the Symbol / Qty boxes, exactly like the desktop.
    tr.addEventListener('click', () => {
      body.querySelectorAll('tr').forEach((other) => other.classList.remove('selected'));
      tr.classList.add('selected');
      $('inp-symbol').value = rec.symbol || '';
      $('inp-qty').value = (rec.quantity === undefined || rec.quantity === null)
        ? '' : pyG(rec.quantity);
      S.selectedRec = rec;
    });
    body.appendChild(tr);
  });

  const source = $('recs-source');
  const context = armedForMe(payload.engine)
    ? 'from the continuous Trading Mode' : 'from the AI analysis';
  source.textContent = rows.length ? ` — ${rows.length} row(s) ${context}` : '';
  if (previousHeight && body.parentElement.scrollHeight < previousHeight) {
    body.parentElement.scrollTop = 0;
  }
}

function renderPositions(payload) {
  const positions = payload.positions || [];
  const symbol = (payload.account && payload.account.symbol) || '$';
  const body = $('holdings-body');
  body.innerHTML = '';
  $('holdings-empty').classList.toggle('hidden', positions.length > 0);

  positions.forEach((position) => {
    const tr = el('tr');
    tr.appendChild(el('td', null, position.symbol));
    tr.appendChild(el('td', null, pyG(position.quantity)));
    tr.appendChild(el('td', null, `${symbol}${money(position.avg_price)}`));
    tr.appendChild(el('td', null, `${symbol}${money(position.price)}`));
    tr.appendChild(el('td', null, `${symbol}${money(position.value)}`));
    const pnl = el('td', position.pnl_pct >= 0 ? 'up' : 'down', pct(position.pnl_pct));
    pnl.title = `${symbol}${signedMoney(position.pnl_money)}`;
    tr.appendChild(pnl);

    // Protective levels the risk engine stores: never a hidden assumption.
    const parts = [];
    if (position.stop_loss !== null && position.stop_loss !== undefined) {
      parts.push(`${position.breakeven_done ? '🔒' : ''}SL ${money(position.stop_loss)}`);
    }
    if (position.take_profit !== null && position.take_profit !== undefined) {
      parts.push(`TP ${money(position.take_profit)}`);
    }
    const stopCell = el('td', 'muted', parts.join(' · ') || '–');
    if (position.initial_stop) {
      stopCell.title = `initial stop ${money(position.initial_stop)} · `
        + `high-water ${money(position.high_water)} · `
        + `risk/share ${money(position.risk_per_share)}`;
    }
    tr.appendChild(stopCell);
    body.appendChild(tr);
  });
}

function renderHistory(trades) {
  const body = $('history-body');
  body.innerHTML = '';
  $('history-empty').classList.toggle('hidden', trades.length > 0);

  trades.forEach((trade) => {
    const tr = el('tr', 'clickable');
    tr.dataset.tradeId = String(trade.id);
    tr.appendChild(el('td', null, trade.trade_time || '–'));
    const action = String(trade.action || '');
    const cls = action === 'BUY' ? 'action-BUY'
      : action === 'SELL' ? 'action-SELL'
        : action === 'DEPOSIT' ? 'up' : action === 'WITHDRAW' ? 'down' : 'muted';
    tr.appendChild(el('td', cls, action));
    tr.appendChild(el('td', null, trade.symbol || '–'));
    tr.appendChild(el('td', null, trade.quantity === null ? '–' : pyG(trade.quantity)));
    tr.appendChild(el('td', null, trade.price === null ? '–' : money(trade.price)));
    tr.appendChild(el('td', null, trade.total === null ? '–' : money(trade.total)));

    let pnlText = '';
    let pnlClass = '';
    if (action === 'SELL' && trade.realized_pnl !== null
        && trade.realized_pnl !== undefined) {
      pnlText = signedMoney(trade.realized_pnl);
      pnlClass = trade.realized_pnl >= 0 ? 'up' : 'down';
    }
    tr.appendChild(el('td', pnlClass, pnlText));
    tr.appendChild(el('td', 'wide', trade.reason || ''));

    tr.addEventListener('click', () => selectTradeRow(tr, trade));
    body.appendChild(tr);
  });

  if (S.selectedTrade
      && !trades.some((t) => String(t.id) === String(S.selectedTrade.id))) {
    S.selectedTrade = null;
  }
  if (S.selectedTrade) {
    const row = body.querySelector(`tr[data-trade-id="${S.selectedTrade.id}"]`);
    if (row) row.classList.add('selected');
  }
}

function selectTradeRow(tr, trade) {
  $('history-body').querySelectorAll('tr').forEach((other) => {
    other.classList.remove('selected');
  });
  if (S.selectedTrade && String(S.selectedTrade.id) === String(trade.id)) {
    S.selectedTrade = null;
    return;
  }
  tr.classList.add('selected');
  S.selectedTrade = trade;
}

function renderModelStatus() {
  const status = $('model-status');
  if (!S.models.length) {
    status.textContent = 'Ollama offline – start it (ollama serve), then Refresh';
    status.style.color = 'var(--red-soft)';
  } else {
    status.textContent = `${S.models.length} models available (incl. cloud) – `
      + 'pick one or type an exact name';
    status.style.color = 'var(--green-soft)';
  }
  const input = $('model-select');
  if (document.activeElement !== input) input.value = S.model || '';
  const list = $('model-options');
  list.innerHTML = '';
  S.models.forEach((model) => {
    const option = el('option');
    option.value = model;
    list.appendChild(option);
  });
}

function renderFoot() {
  const health = S.health || {};
  $('foot-patterns').textContent = health.talib ? 'TA-Lib (61 patterns)'
    : 'built-in pattern engine';
  const engine = (S.state && S.state.engine) || {};
  $('foot-build').textContent = engine.data_available === false
    ? 'market data offline' : 'server-side engine · runs 24/7';
}

async function loadModels() {
  try {
    const payload = await api('GET', '/api/models');
    S.models = payload.models || [];
    S.model = payload.selected || '';
  } catch (err) {
    S.models = [];
    S.model = '';
  }
  renderModelStatus();
}

/* =====================================================================
   ACTIONS
   ===================================================================== */
async function refresh(force = true) {
  try {
    await poll(force);
    if (activeTab() === 'history') await loadHistory();
  } catch (err) { fail(err, 'Refresh failed'); }
}

async function createProfile() {
  const name = await promptModal('New Profile', 'Enter profile name:');
  if (!name) return;
  const currency = await modalOpen({
    title: `New Profile: ${name}`,
    message: 'Choose paper currency.\n\nAccount starts empty (0.00).\n'
      + 'Use "Deposit Cash" to add funds.',
    icon: '💱',
    kind: 'prompt',
    choices: ['USD', 'EUR', 'GBP'],
    value: 'USD',
    okLabel: 'Create (empty account)',
  });
  if (!currency) return;

  try {
    await api('POST', '/api/profiles', {
      name, currency, initial_balance: 0,
    });
    toast('Profile created', `${name} created with 0.00 ${currency} – deposit `
      + 'cash to begin trading.', 'success');
    S.booted = true;
    await refresh(true);
  } catch (err) {
    fail(err, 'Could not create profile');
  }
}

async function depositCash() {
  if (!S.profileId) return;
  const amount = await promptModal('Deposit Cash',
    'Enter paper money amount to add:', '', true);
  if (amount === null) return;
  try {
    await api('POST', `/api/profiles/${S.profileId}/funds`, {
      amount, note: 'Deposit',
    });
    toast('Deposit', `Deposited ${money(amount)} paper money.`, 'success');
    await refresh(true);
  } catch (err) {
    fail(err, 'Deposit failed');
  }
}

async function deleteProfile() {
  const profile = S.profiles.find((p) => String(p.id) === String(S.profileId));
  if (!profile) return;
  const ok = await confirmModal('Confirm Delete',
    `Delete profile '${profile.name}' and ALL its positions and trade history?`);
  if (!ok) return;
  try {
    await api('DELETE', `/api/profiles/${profile.id}`);
    toast('Profile deleted', `Profile '${profile.name}' deleted.`, 'success');
    await refresh(true);
  } catch (err) {
    fail(err, 'Delete failed');
  }
}

async function selectProfile(profileId) {
  if (!profileId) return;
  try {
    await api('POST', '/api/profiles/select', { profile_id: Number(profileId) });
    S.profileId = Number(profileId);
    S.selectedTrade = null;
    await refresh(true);
    await loadHistory();
  } catch (err) {
    fail(err, 'Could not select profile');
  }
}

async function toggleAutoMode() {
  if (!S.profileId) return;
  const enabled = $('chk-auto').checked;
  S.autoPending = true;
  try {
    await api('POST', `/api/profiles/${S.profileId}/auto_mode`, { enabled });
    toast('Auto-trade', enabled
      ? 'Auto-trade ENABLED – AI orders will execute automatically.'
      : 'Auto-trade disabled.', enabled ? 'warn' : 'info');
    await refresh(true);
  } catch (err) {
    $('chk-auto').checked = !enabled;
    fail(err, 'Auto-trade');
  } finally {
    S.autoPending = false;
  }
}

function currentLens() {
  const fundamental = $('chk-fundamental').checked;
  const technical = $('chk-technical').checked;
  if (fundamental && technical) return 'combined';
  if (fundamental) return 'fundamental';
  if (technical) return 'technical';
  return 'general';
}

async function startAnalysis() {
  if (!S.profileId) { alertModal('Warning', 'Select or create a profile first.', 'warning'); return; }
  const engine = (S.state && S.state.engine) || {};
  if (engine.running) {
    alertModal('Warning', armedForMe(engine)
      ? 'Stop Trading Mode for your portfolio before running an AI analysis.'
      : 'The engine is trading another account\'s portfolio right now - '
        + 'an AI analysis has to wait for it to finish.', 'warning');
    return;
  }
  const lens = currentLens();
  try {
    await api('POST', '/api/analyse', {
      profile_id: S.profileId, lens, web: $('chk-web').checked,
    });
    toast('AI analysis', 'Analysis started – the server is working on it. '
      + 'You can close this page; it keeps running.', 'info');
    await refresh(true);
  } catch (err) {
    fail(err, 'Analysis failed');
  }
}

async function toggleTradingMode() {
  const engine = (S.state && S.state.engine) || {};
  // Per account: only "my portfolio is armed" turns this button into Stop.
  if (armedForMe(engine)) {
    try {
      await api('POST', '/api/engine/stop', {});
      toast('Trading Mode', 'Stopping your portfolio after the current cycle.', 'info');
      await refresh(true);
    } catch (err) { fail(err, 'Could not stop the engine'); }
    return;
  }
  if (!S.profileId) { alertModal('Warning', 'Select or create a profile first.', 'warning'); return; }
  if (othersArmed(engine)) {
    toast('Trading Mode', 'The engine is busy with another account\'s portfolio - '
      + 'asking the server to arm yours too.', 'warn', 12000);
  }
  const label = $('timeframe-select').value || S.timeframe;
  try {
    await api('POST', '/api/engine/start', {
      profile_id: S.profileId, timeframe: label,
    });
    const config = ((S.state && S.state.trading_timeframes) || [])
      .find((t) => t.label === label);
    toast('Trading Mode started',
      `Continuously scanning the S&P 500 on '${label}'`
      + (config ? `, re-scan every ${config.rescan_min} min.` : '.')
      + '\n\nIt runs on the server, so closing this page will not stop it.',
      'success', 12000);
    await refresh(true);
  } catch (err) {
    fail(err, 'Could not start Trading Mode');
  }
}

async function runOneCycle() {
  if (!S.profileId) return;
  try {
    await api('POST', '/api/engine/cycle', {
      profile_id: S.profileId, timeframe: $('timeframe-select').value,
    });
    toast('One cycle', 'Running a single scan/decide/execute cycle now.', 'info');
    setTimeout(() => refresh(true).catch(() => {}), 4000);
  } catch (err) {
    fail(err, 'Could not run a cycle');
  }
}

async function onTimeframeChange() {
  const label = $('timeframe-select').value;
  S.timeframe = label;
  const engine = (S.state && S.state.engine) || {};
  // Only re-arm when THIS portfolio is the one being traded.
  if (!armedForMe(engine)) return;
  try {
    await api('POST', '/api/engine/start', {
      profile_id: S.profileId, timeframe: label,
    });
    toast('Trading Mode', `Timeframe switched to '${label}'.`, 'info');
    await refresh(true);
  } catch (err) {
    fail(err, 'Could not switch timeframe');
  }
}

async function trade(action) {
  if (!S.profileId) { alertModal('Warning', 'Select or create a profile first.', 'warning'); return; }
  let symbol = $('inp-symbol').value.trim().toUpperCase();
  const rawQty = $('inp-qty').value.trim();

  if (!symbol && S.selectedRec) symbol = String(S.selectedRec.symbol || '').toUpperCase();
  if (!symbol && !rawQty) {
    alertModal('Warning', 'Select a recommendation or enter a symbol and quantity.',
      'warning');
    return;
  }
  if (!symbol) {
    symbol = await promptModal('Stock Symbol', 'Enter stock symbol (e.g., AAPL):');
    if (!symbol) return;
    symbol = symbol.trim().toUpperCase();
  }
  const quantity = Number(String(rawQty).replace(',', '.'));
  if (!rawQty || !isFinite(quantity) || quantity <= 0) {
    alertModal('Error', 'Enter a valid positive quantity.', 'error');
    return;
  }

  const button = action === 'BUY' ? $('btn-buy') : $('btn-sell');
  button.disabled = true;
  try {
    const payload = await api('POST', '/api/trade', { action, symbol, quantity });
    const price = payload.price !== undefined ? money(payload.price) : '?';
    toast(`${action} executed`, `${action} ${pyG(quantity)} ${symbol} @ ${price} `
      + 'executed.', 'success');
    $('inp-qty').value = '';
    await refresh(true);
    await loadHistory();
  } catch (err) {
    fail(err, `${action} rejected`);
  } finally {
    button.disabled = false;
  }
}

async function editSelectedPrice() {
  if (!S.selectedTrade) {
    alertModal('Warning', 'Select a trade in the history first.', 'warning');
    return;
  }
  const trade = S.selectedTrade;
  const newPrice = await promptModal('Edit Trade Price',
    `${trade.action} ${pyG(trade.quantity)} ${trade.symbol}\n`
    + 'Enter the price you actually executed at:', money(trade.price), true);
  if (newPrice === null) return;
  if (Math.abs(newPrice - Number(trade.price)) < 1e-9) return;
  const ok = await confirmModal('Confirm Price Correction',
    `Update ${trade.action} ${pyG(trade.quantity)} ${trade.symbol} from `
    + `${money(trade.price)} to ${money(newPrice)}?\n\n`
    + 'Cash, average cost basis and realized P/L will be recalculated '
    + 'automatically.');
  if (!ok) return;
  try {
    await api('POST', `/api/history/${trade.id}/price`, { price: newPrice });
    toast('Trade corrected', `${trade.action} ${pyG(trade.quantity)} `
      + `${trade.symbol} @ ${money(newPrice)} (was ${money(trade.price)}).`,
      'success');
    S.selectedTrade = null;
    await refresh(true);
    await loadHistory();
  } catch (err) {
    fail(err, 'Could not update the trade');
  }
}

async function clearHistory() {
  if (!S.profileId) return;
  const ok = await confirmModal('Clear Trade History',
    'Delete EVERY trade of this profile?\n\nThis cannot be undone: the ledger '
    + 'that carries the realized P/L and the per-symbol cost basis will be '
    + 'gone. Cash and open positions are not touched.');
  if (!ok) return;
  try {
    const payload = await api('POST', '/api/history/clear', { profile_id: S.profileId });
    toast('History cleared', `${payload.deleted || 0} trade(s) deleted.`, 'warn');
    S.selectedTrade = null;
    await refresh(true);
    await loadHistory();
  } catch (err) {
    fail(err, 'Could not clear the history');
  }
}

async function saveModel() {
  const model = $('model-select').value.trim();
  if (!model || model === S.model) return;
  try {
    await api('POST', '/api/models', { model });
    S.model = model;
    toast('AI model', `Analysis model set to ${model}.`, 'success');
  } catch (err) {
    fail(err, 'Could not save the model');
  }
}

async function refreshModels() {
  $('model-status').textContent = 'Loading models from Ollama…';
  $('model-status').style.color = 'var(--muted)';
  await loadModels();
  await refresh(true);
}

/* =====================================================================
   TABS
   ===================================================================== */
function activeTab() {
  const tab = document.querySelector('.tab.active');
  return tab ? tab.dataset.tab : 'holdings';
}

function setTab(name) {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.tab === name);
  });
  ['holdings', 'history', 'activity'].forEach((panel) => {
    $(`panel-${panel}`).classList.toggle('hidden', panel !== name);
  });
  if (name === 'history') loadHistory().catch(() => {});
  if (name === 'activity') pollLog().catch(() => {});
}

/* =====================================================================
   WIRING
   ===================================================================== */
function wire() {
  $('login-form').addEventListener('submit', submitAuth);
  $('btn-show-register').addEventListener('click', () => setAuthMode('register'));
  $('btn-show-login').addEventListener('click', () => setAuthMode('login'));
  $('btn-logout').addEventListener('click', logout);

  $('profile-select').addEventListener('change', (event) => {
    selectProfile(event.target.value);
  });
  $('btn-new-profile').addEventListener('click', createProfile);
  $('btn-deposit').addEventListener('click', depositCash);
  $('btn-delete-profile').addEventListener('click', deleteProfile);
  $('chk-auto').addEventListener('change', toggleAutoMode);

  $('btn-refresh-models').addEventListener('click', refreshModels);
  $('model-select').addEventListener('change', saveModel);
  $('model-select').addEventListener('blur', saveModel);

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => setTab(tab.dataset.tab));
  });

  $('chk-live').addEventListener('change', () => {
    if ($('chk-live').checked) { startPolling(); refresh(true); } else stopPolling();
  });
  $('chk-web').addEventListener('change', () => savePrefs());
  $('chk-fundamental').addEventListener('change', () => savePrefs());
  $('chk-technical').addEventListener('change', () => savePrefs());

  $('btn-analyse').addEventListener('click', startAnalysis);
  $('btn-trading-mode').addEventListener('click', toggleTradingMode);
  $('btn-one-cycle').addEventListener('click', runOneCycle);
  $('timeframe-select').addEventListener('change', onTimeframeChange);

  $('btn-buy').addEventListener('click', () => trade('BUY'));
  $('btn-sell').addEventListener('click', () => trade('SELL'));
  $('inp-qty').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') trade('BUY');
  });
  $('inp-symbol').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') $('inp-qty').focus();
  });

  $('btn-edit-price').addEventListener('click', editSelectedPrice);
  $('btn-clear-history').addEventListener('click', clearHistory);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('modal-root').classList.contains('hidden')) {
      $('modal-cancel').click();
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh(true).catch(() => {});
  });
}

/* ------------------------------------------------------- preferences */
const PREF_KEY = 'paperTrader.prefs';

function savePrefs() {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({
      web: $('chk-web').checked,
      live: $('chk-live').checked,
      fundamental: $('chk-fundamental').checked,
      technical: $('chk-technical').checked,
    }));
  } catch (err) { /* private mode */ }
}

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREF_KEY);
    if (!raw) return;
    const prefs = JSON.parse(raw);
    if (typeof prefs.web === 'boolean') $('chk-web').checked = prefs.web;
    if (typeof prefs.live === 'boolean') $('chk-live').checked = prefs.live;
    if (typeof prefs.fundamental === 'boolean') {
      $('chk-fundamental').checked = prefs.fundamental;
    }
    if (typeof prefs.technical === 'boolean') {
      $('chk-technical').checked = prefs.technical;
    }
  } catch (err) { /* ignore */ }
}

/* =====================================================================
   START
   ===================================================================== */
async function main() {
  wire();
  loadPrefs();
  setAuthMode('login');           // paint the card before the session call
  $('live-secs').textContent = String(Math.round(POLL_MS / 1000));
  try {
    const session = await loadSession();
    if (!session.authed) { showLogin(); return; }
    showApp();
    renderUser(S.user);
    await boot();
  } catch (err) {
    showLogin('Cannot reach the server.');
  }
  window.addEventListener('beforeunload', stopPolling);
}

document.addEventListener('DOMContentLoaded', main);
