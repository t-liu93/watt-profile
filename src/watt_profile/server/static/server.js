"use strict";
(() => {
  const $ = id => document.getElementById(id), page = document.body.dataset.page;
  const id = document.body.dataset.objectId, prefix = '/api/admin/v1';
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let expired = false, busy = false, polling = false, generation = 0, summary = null;
  const offsets = {sites: 0, devices: 0, readings: 0, sessions: 0};
  const totals = {}, size = 20;
  let filters = {}, readingLimit = 50, metadataLoaded = false;
  const value = v => v == null ? 'Unavailable' : typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v);
  function clearSecrets() {
    generation++; summary = null;
    if ($('summary')) $('summary').value = '';
    if ($('registration-preview')) { $('registration-preview').hidden = true; $('preview').textContent = ''; }
    if ($('first-credential')) $('first-credential').elements.token.value = '';
  }
  function failure(error) {
    const code = error?.code || 'connection_failed';
    $('error').hidden = false;
    $('error').textContent = `${code}: Request could not be completed. Reload status / retry. A timeout does not prove rollback.`;
    if (code === 'authentication_required') {
      expired = true; clearSecrets(); $('login-link').hidden = false;
      $('error').textContent = 'Login expired. Log in again to continue.';
      document.querySelectorAll('main button, main input, main textarea').forEach(e => e.disabled = true);
    }
  }
  async function api(path, method = 'GET', body, raw = false) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 22000);
    try {
      const r = await fetch(prefix + path, {method, credentials:'same-origin', cache:'no-store', signal:controller.signal,
        headers:method === 'GET' ? {} : {'Content-Type':'application/json','X-CSRF-Token':csrf},
        ...(body === undefined ? {} : {body:raw ? body : JSON.stringify(body)})});
      if (r.status === 204) return null;
      const result = await r.json(); if (!r.ok) throw result.error; return result.data;
    } finally { clearTimeout(timer); }
  }
  async function run(action, mutation = false) {
    if (expired || (mutation && busy)) return;
    if (mutation) busy = true;
    $('error').hidden = true; $('message').textContent = '';
    try { await action(); } catch (e) { failure(e); } finally { if (mutation) busy = false; }
  }
  function form(name, action) {
    $(name)?.addEventListener('submit', event => { event.preventDefault(); run(() => action(event.target), true); });
  }
  function data(form) { return Object.fromEntries(new FormData(form)); }
  function node(tag, text) { const n = document.createElement(tag); n.textContent = value(text); return n; }
  function link(url, label, className = '') { const n = node('a',label); n.href = url; n.className = className; return n; }
  function details(target, entries) {
    target.replaceChildren(); entries.forEach(([k,v]) => target.append(node('dt',k),node('dd',v)));
  }
  function confirm(text) {
    return new Promise(resolve => {
      const dialog = $('confirmation'); $('confirmation-text').textContent = text;
      const finish = yes => { dialog.close(); dialog.oncancel = null; resolve(yes); };
      $('confirm').onclick = () => finish(true); $('cancel').onclick = () => finish(false);
      dialog.oncancel = event => { event.preventDefault(); finish(false); }; dialog.showModal();
      $('cancel').focus();
    });
  }
  function params(values) { return new URLSearchParams(values).toString(); }
  function buttons(kind, limit) {
    $(kind+'-prev').disabled = offsets[kind] === 0;
    $(kind+'-next').disabled = offsets[kind] + limit >= totals[kind];
  }
  function pagination(kind, loader, getSize = () => size) {
    for (const [suffix,delta] of [['prev',-1],['next',1]]) $(kind+'-'+suffix).onclick = () => run(async () => {
      offsets[kind] = Math.max(0,offsets[kind]+delta*getSize()); await loader();
    });
  }
  async function list(kind) {
    const search = kind === 'sites' ? Object.fromEntries(Object.entries(data($('search'))).filter(([,v])=>v)) : {};
    const result = await api('/'+kind+'?'+params({...search, offset:offsets[kind],limit:size}));
    totals[kind] = result.total; $(kind).replaceChildren();
    if (!result.items.length) $(kind).append(node('p','No '+kind+' found. Create or register one to begin.'));
    result.items.forEach(item => {
      const obj = kind === 'sites' ? item.site : item.device, uuid = obj.site_id || obj.device_id;
      const row = node('p',''); row.append(link('/'+kind+'/'+uuid,uuid));
      row.append(node('span', kind === 'sites' ? ` — ${obj.address}\n${obj.description}\nAccepted readings: ${item.reading_count}; latest reception (UTC): ${value(item.latest_reading_received_at)}; authorized devices: ${item.authorized_device_ids.join(', ') || 'None'}` : ` — Last authenticated contact (UTC): ${value(obj.last_authenticated_contact_at)}; latest accepted reading reception (UTC): ${value(item.latest_reading_received_at)}`));
      row.style.whiteSpace = 'pre-wrap'; $(kind).append(row);
    }); buttons(kind,size);
  }
  function registrationResult(result, target) {
    const actions = node('div', ''); actions.className = 'action-row';
    actions.append(link('/sites/'+result.site.site_id,'Open registered site','button-link'),
      link('/devices/'+result.device_id,'Open registered device','button-link'));
    target.replaceChildren(node('p', 'Registration applied. Actual actions: '+JSON.stringify(result.actions)),
      actions,node('pre',JSON.stringify(result.site,null,2)));
  }
  if (page === 'dashboard') {
    form('registration-form', async () => {
      clearSecretsPreviewOnly();
      const current = generation, raw = $('summary').value;
      const result = await api('/registrations/preview','POST',raw,true);
      if (current !== generation || expired) return;
      summary = raw; $('summary').value = '';
      const submitted=JSON.parse(raw);
      $('preview').textContent = 'Submitted metadata: '+JSON.stringify({address:submitted.address,description:submitted.description},null,2)+'\nServer preview: '+JSON.stringify(result,null,2) + (result.metadata_differs ? '\nExisting metadata differs. Server metadata above will be preserved. Use explicit metadata edit to change it.' : '\nExisting site metadata is preserved.');
      $('registration-preview').hidden = false;
    });
    $('register').onclick = () => run(async () => {
      if (!summary) return;
      const current = generation, raw = summary;
      const result = await api('/registrations','POST',raw,true);
      if (current !== generation || expired) return;
      clearSecrets(); registrationResult(result,$('registration-result'));
      const completed = generation;
      refreshStatus().catch(e => {
        if (completed !== generation || expired) return;
        failure(e);
        if (!expired) $('error').textContent = `${e?.code || 'connection_failed'}: Registration applied, but status could not be refreshed. Reload status / retry.`;
      });
    },true);
    $('cancel-registration').onclick = () => { clearSecrets(); $('registration-result').replaceChildren(); $('message').textContent = 'Registration cancelled.'; };
    $('summary').addEventListener('input',clearSecretsPreviewOnly);
    form('search',async () => { offsets.sites=0; await list('sites'); });
    form('create-site',async f => { const r=await api('/sites','POST',data(f)); $('message').replaceChildren(link('/sites/'+r.site_id,'Open site')); await list('sites'); });
    form('create-device',async f => { const r=await api('/devices','POST',data(f)); $('message').replaceChildren(link('/devices/'+r.device_id,'Open device')); await list('devices'); });
    pagination('sites',()=>list('sites')); pagination('devices',()=>list('devices'));
  }
  function clearSecretsPreviewOnly() { generation++; summary=null; $('registration-preview').hidden=true; $('preview').textContent=''; }
  async function siteStatus() {
    const r=await api('/sites/'+id);
    details($('status'),[['Site UUID',r.site.site_id],['Address',r.site.address],['Description',r.site.description],['Accepted reading count',r.reading_count],['Latest accepted reading reception (UTC)',r.latest_reading_received_at],['Authorized devices',r.authorized_device_ids.join(', ') || 'None']]);
    if (!metadataLoaded) { const f=$('edit-site'); f.elements.address.value=r.site.address; f.elements.description.value=r.site.description; metadataLoaded=true; }
  }
  async function deviceStatus() {
    const r=await api('/devices/'+id);
    details($('status'),[['Device UUID',id],['Last authenticated contact (UTC)',r.device.last_authenticated_contact_at],['Latest accepted reading reception (UTC)',r.status.latest_reading_received_at]]);
    $('credentials').replaceChildren(); $('first-credential').hidden = r.credentials.length > 0;
    r.credentials.forEach(c => {
      const row=node('p',`${c.credential_id}; created (UTC): ${c.created_at}; disabled (UTC): ${value(c.disabled_at)}`);
      if (!c.disabled_at) { const b=node('button','Disable credential'); b.onclick=()=>run(async () => {
        if (!await confirm('Disable this credential? Pending Logger records will stay pending. Historical readings remain. Re-enablement is unavailable.')) return;
        await api('/devices/'+id+'/credentials/'+c.credential_id+'/disable','POST',{}); await deviceStatus();
      },true); row.append(b); } $('credentials').append(row);
    }); if (!r.credentials.length) $('credentials').append(node('p','No credential registered.'));
    $('grants').replaceChildren();
    r.sites.forEach(s => { const row=node('p',''); row.append(link('/sites/'+s.site_id,s.site_id+' '+s.address)); const b=node('button','Revoke grant'); b.onclick=()=>run(async()=> {
      if (!await confirm('Revoke this site grant? Pending Logger records for this site will stay pending. Historical readings remain. Other site grants remain.')) return;
      await api('/devices/'+id+'/sites/'+s.site_id,'DELETE'); await deviceStatus();
    },true); row.append(b); $('grants').append(row); }); if (!r.sites.length) $('grants').append(node('p','No authorized sites. Add a grant to accept readings.'));
  }
  async function readings() {
    const r=await api('/sites/'+id+'/readings?'+params({...filters,offset:offsets.readings,limit:readingLimit}));
    totals.readings=r.total; $('reading-count').textContent=`${r.total} filtered readings; offset ${offsets.readings}. Export includes every filtered row.`;
    $('readings').replaceChildren();
    if (!r.items.length) { const row=node('tr',''); const cell=node('td','No accepted readings match these filters.'); cell.colSpan=5; row.append(cell); $('readings').append(row); }
    r.items.forEach(s => { const row=node('tr',''); for (const v of [s.received_at,s.device_id,s.metrics.energy_import_t1_kwh,s.metrics.power_import_kw]) row.append(node('td',v));
      const cell=node('td',''), b=node('button','View reading'); b.onclick=()=>run(async()=> {
        const snapshot=await api('/sites/'+id+'/readings/'+s.device_id+'/'+s.record_id);
        details($('reading-detail'),Object.entries(snapshot).filter(([k])=>!['metrics','fields','quality'].includes(k)));
        details($('metrics'),Object.entries(snapshot.metrics)); $('fields-quality').textContent=JSON.stringify({fields:snapshot.fields,quality:snapshot.quality},null,2); $('detail-dialog').showModal();
      }); cell.append(b); row.append(cell); $('readings').append(row); }); buttons('readings',readingLimit);
  }
  async function sessions() {
    const r=await api('/sites/'+id+'/sessions?'+params({...filters.device_id ? {device_id:filters.device_id} : {},offset:offsets.sessions,limit:size}));
    totals.sessions=r.total; $('sessions').textContent=r.items.length ? JSON.stringify(r.items,null,2) : 'No sessions match this device filter.'; buttons('sessions',size);
  }
  if (page === 'site') {
    form('edit-site',async f=> { if (!await confirm('Save this explicit site metadata edit? Site UUID and historical readings remain unchanged.')) return; await api('/sites/'+id,'PATCH',data(f)); $('message').textContent='Metadata saved.'; await siteStatus(); });
    form('filters',async f=> { const next=data(f); const limit=Number(next.limit); delete next.limit;
      for (const key of ['received_from','received_before']) if (next[key] && !/(Z|[+-]\d\d:\d\d)$/i.test(next[key])) throw {code:'invalid_input'};
      filters=Object.fromEntries(Object.entries(next).filter(([,v])=>v)); readingLimit=limit; offsets.readings=offsets.sessions=0; await readings(); await sessions();
    });
    pagination('readings',readings,()=>readingLimit); pagination('sessions',sessions);
    $('detail-close').onclick=()=>$('detail-dialog').close();
    $('export').onclick=()=>run(async()=> {
      // Validate the filters/session with a bounded request, then let the browser
      // stream the attachment to disk. A started request is not proof of completion.
      await api('/sites/'+id+'/readings?'+params({...filters,limit:1}));
      const a=link(prefix+'/sites/'+id+'/readings.csv?'+params(filters),'Download');
      a.target='_blank'; a.rel='noopener'; a.click();
      $('message').textContent='CSV download requested for all filtered readings. Check browser download completion; interrupted downloads may be incomplete. A pre-download error opens in a new tab; return here and reload status / retry. Separate requests may include newly accepted data.';
    });
  }
  if (page === 'device') {
    form('first-credential',async f=> { const token=f.elements.token.value; const current=generation; f.elements.token.value=''; await api('/devices/'+id+'/credentials','POST',{token}); if(current===generation && !expired) await deviceStatus(); });
    form('add-grant',async f=> { await api('/devices/'+id+'/sites/'+f.elements.site_id.value,'PUT',{}); await deviceStatus(); });
  }
  async function refreshStatus() {
    if (polling || expired || document.hidden) return;
    polling=true;
    try { if(page==='dashboard') { await list('sites'); await list('devices'); } else if(page==='site') await siteStatus(); else await deviceStatus(); }
    finally { polling=false; }
  }
  $('reload').onclick=()=>run(refreshStatus);
  $('logout').addEventListener('submit',clearSecrets);
  window.addEventListener('pagehide',clearSecrets);
  document.addEventListener('click',e=> { if(e.target.closest('a[href]')) clearSecrets(); });
  window.addEventListener('hashchange',clearSecrets);
  document.addEventListener('visibilitychange',()=> { if(!document.hidden) run(refreshStatus); });
  run(async()=> { await refreshStatus(); if(page==='site') { await readings(); await sessions(); } });
  setInterval(()=>run(refreshStatus),10000);
})();
