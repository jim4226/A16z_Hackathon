// Credentials never enter browser storage, URLs, project files or markup.
const $ = (s, root = document) => root.querySelector(s);
async function api(path, body) {
  const response = await fetch(path, body === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.message || `Request failed (${response.status})`);
  return data;
}
function dialog(id, title, body) {
  const el = document.createElement('dialog');
  el.id = id; el.className = 'setup-dialog'; el.setAttribute('aria-labelledby', `${id}Title`);
  el.innerHTML = `<header><h2 id="${id}Title">${title}</h2><button class="btn ghost" type="button" data-close aria-label="Close">✕</button></header>${body}`;
  document.body.append(el); $('[data-close]', el).onclick = () => el.close(); return el;
}
export function setupLocal({ fixture, state, toast, reload }) {
  const settings = dialog('settingsDialog', 'Settings', `
    <p class="setup-intro">Connect Claude on this computer. The local app sends your key directly to Anthropic. R Cubed's servers do not receive it.</p>
    <form id="providerForm" autocomplete="off"><fieldset><legend>Claude</legend>
      <p id="providerStatus" class="setup-status" role="status" aria-live="polite"></p>
      <label for="providerKey">API key</label><input id="providerKey" type="password" autocomplete="new-password" spellcheck="false" maxlength="4096" placeholder="Paste a key to connect or replace it">
      <p class="setup-help">Never paste keys into design chat. Claude receives your request and design context; usage is billed to your Anthropic account.</p>
      <label for="providerModel">Claude model</label><input id="providerModel" list="providerModels" maxlength="128" required spellcheck="false"><datalist id="providerModels"></datalist>
      <p class="setup-help">Test connection to load models available to your account. Model choice affects cost and capabilities.</p>
      <label for="providerStorage">Store this key</label><select id="providerStorage"><option value="system">System credential store</option><option value="session">Until this app closes</option></select>
      <p class="setup-help" id="storageHelp"></p>
      <div class="setup-actions"><button class="btn" type="button" id="providerTest">Test connection</button><button class="btn primary" type="submit">Save key</button><button class="btn ghost" type="button" id="providerRemove">Remove key</button></div>
    </fieldset></form>
    <form id="preferencesForm"><fieldset><legend>Network and data sharing</legend>
      <label class="setup-check"><input type="checkbox" id="networkEnabled"><span>Allow network access<small>Turn off to edit and generate locally without provider requests.</small></span></label>
      <label class="setup-check"><input type="checkbox" id="sourcingEnabled"><span>Allow Darwin sourcing searches<small>Find agents sends component names and fabrication queries to Darwin. CAD files are not uploaded.</small></span></label>
      <label class="setup-check"><input type="checkbox" id="telemetryEnabled"><span>Share build events with my Keewano project<small>Optional developer integration requiring your own Keewano credentials. Sends project names, materials, components, timings and measurements. Automatic grip calibration is disabled in this beta.</small></span></label>
      <div class="setup-actions"><button class="btn" type="submit">Save preferences</button></div>
    </fieldset></form><p id="settingsMessage" class="setup-message" role="status" aria-live="polite"></p>`);
  const project = dialog('newProjectDialog', 'Create a project', `
    <p class="setup-intro">Start with a supported robot, then describe changes in chat or edit its parameters. Designs and files are saved on this computer.</p>
    <form id="newProjectForm"><label for="newProjectName">Project name</label>
      <input id="newProjectName" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,63}" placeholder="my-first-robot" autocomplete="off">
      <p class="setup-help">Use letters, numbers, hyphens or underscores.</p>
      <label for="newProjectTemplate">Starting design</label><select id="newProjectTemplate"><option value="egg-picker">Generated arm with force-sensing gripper</option><option value="so101">SO-101 open-source arm</option><option value="duplicate">Copy the current project</option></select>
      <p class="setup-help">Generation can take a few minutes. No API key is needed for an example. Outputs are engineering previews; review them before fabrication or motion.</p>
      <div class="setup-actions"><button class="btn primary" type="submit">Create project</button><button class="btn" id="connectFromWelcome" type="button">Connect Claude</button></div>
    </form><p id="projectMessage" class="setup-message" role="status" aria-live="polite"></p>`);
  const message = (text, error = false) => { const el = $('#settingsMessage'); el.textContent = text; el.classList.toggle('error', error); };
  const showStatus = (data, refreshForm = true) => {
    const sources = { system: 'system credential store', session: 'this session only', environment: 'developer environment' };
    const labels = { connected: 'Connected', untested: 'Key saved; connection not tested', not_configured: 'Not connected', offline: 'Network disabled or unavailable', invalid_key: 'Key rejected', model_unavailable: 'Model unavailable', rate_limited: 'Rate limited' };
    $('#providerStatus').textContent = `${labels[data.connection] || 'Connection needs attention'} · ${data.configured ? sources[data.credential_source] || '' : 'simple offline rules available'}`;
    if (!refreshForm) return;
    $('#providerModel').value = data.model || '';
    $('#networkEnabled').checked = data.network_enabled; $('#sourcingEnabled').checked = data.sourcing_enabled; $('#telemetryEnabled').checked = data.telemetry_enabled;
    $('#providerStorage option[value="system"]').disabled = !data.storage_available;
    $('#providerStorage').value = data.storage_available && data.credential_source !== 'session' ? 'system' : 'session';
    $('#storageHelp').textContent = data.storage_available ? 'Saved keys stay in your operating system’s credential store, outside projects and exports.' : 'No supported credential store is available. Session-only keys will not be saved to disk.';
  };
  async function run(button, fn) { button.disabled = true; try { await fn(); } catch (e) { message(e.message, true); } finally { button.disabled = false; } }
  async function openSettings() {
    if (fixture) return toast('This preview does not accept credentials. Download the local app to connect Claude.');
    message(''); $('#providerKey').value = ''; settings.showModal();
    try { showStatus(await api('/api/provider')); } catch (e) { message(e.message, true); }
  }
  settings.addEventListener('close', () => { $('#providerKey').value = ''; });
  $('#settingsBtn').onclick = openSettings;
  $('#connectFromWelcome').onclick = () => { project.close(); openSettings(); };
  $('#providerForm').onsubmit = (event) => {
    event.preventDefault(); const key = $('#providerKey').value.trim();
    if (!key) return message('Paste a key to save it. Use Save preferences to change only the model.', true);
    run(event.submitter, async () => { try { const result = await api('/api/provider/save', { key, storage: $('#providerStorage').value, model: $('#providerModel').value.trim() }); showStatus(result); message(result.warning || 'Key saved. Test the connection to check key and model access.'); } finally { $('#providerKey').value = ''; } });
  };
  $('#providerTest').onclick = (event) => run(event.currentTarget, async () => {
    const body = { model: $('#providerModel').value.trim() };
    if ($('#providerKey').value.trim()) body.key = $('#providerKey').value.trim();
    const result = await api('/api/provider/test', body); message(result.message, !result.ok);
    $('#providerModels').replaceChildren(...(result.models || []).map(item => { const option = document.createElement('option'); option.value = item.id; option.label = item.display_name; return option; }));
    if (!body.key) showStatus(await api('/api/provider'), false); delete body.key;
  });
  $('#providerRemove').onclick = (event) => run(event.currentTarget, async () => {
    $('#providerKey').value = ''; const result = await api('/api/provider/remove', {});
    showStatus(result); message(result.warning || 'Claude disconnected. Developer environment credentials are also disabled until you reconnect.');
  });
  $('#preferencesForm').onsubmit = (event) => { event.preventDefault(); run(event.submitter, async () => {
    showStatus(await api('/api/provider/preferences', { model: $('#providerModel').value.trim(), network_enabled: $('#networkEnabled').checked, sourcing_enabled: $('#sourcingEnabled').checked, telemetry_enabled: $('#telemetryEnabled').checked })); message('Preferences saved.'); await reload();
  }); };
  function openProject() {
    if (fixture) return toast('This is a precomputed preview. Download the local app to create projects.');
    $('#projectMessage').textContent = ''; $('#newProjectName').value = '';
    $('#newProjectTemplate option[value="duplicate"]').disabled = !state().current;
    $('#newProjectTemplate').value = 'egg-picker'; project.showModal();
  }
  $('#newProjectBtn').onclick = openProject;
  $('#newProjectForm').onsubmit = async (event) => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try {
      const name = $('#newProjectName').value.trim(), example = $('#newProjectTemplate').value;
      await api('/api/provider/preferences', { onboarding_complete: true });
      await api(example === 'duplicate' ? '/api/projects/duplicate' : '/api/projects/create', example === 'duplicate' ? { name } : { name, example });
      project.close(); toast('Creating your project. Progress appears in the pipeline.');
    } catch (e) { $('#projectMessage').textContent = e.message; } finally { button.disabled = false; }
  };
  return { async ready(st) {
    $('#presentBtn').hidden = !fixture && !st.demo_mode;
    if (fixture) { $('#releaseLabel').textContent = 'Preview · precomputed outputs'; $('#releaseLabel').title = 'Edits replay sample results; they do not compute new CAD, simulations or fabrication files.'; }
    else if (!st.current) openProject();
  } };
}
