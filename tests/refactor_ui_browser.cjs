/* Behavioral browser comparison. Every HTTP/WebSocket operation is synthetic. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { chromium } = require('playwright');
const candidate = path.resolve(__dirname, '..');
const roots = process.argv[2] ? [path.resolve(process.argv[2]), candidate] : [candidate];
const evidence = process.env.AUDIT_EVIDENCE;
const stamp = '2026-09-12T12:00:00+00:00';
const profiles = [
  {id:'codex-openai', label:'Codex', harness:'codex', allowed_users:['devagent','hostagent'],
   supports_model:true, supports_effort:true, effort_levels:['low','medium','high'], default_effort:'high',
   permission_modes:{full:[],standard:[]}, default_permission_mode:'full', prompt_arg:{mode:'positional'},
   modes:{options:[{id:'default',label:'Normale'},{id:'plan',label:'Plan',note:'Pianifica'}]},
   goal:{command:'/goal'}, login:{label:'Login Codex'}},
  {id:'claude-anthropic', label:'Claude', harness:'claude', allowed_users:['devagent','hostagent'],
   supports_model:true, supports_effort:true, effort_levels:['low','medium','high'], default_effort:'medium',
   permission_modes:{full:[]}, default_permission_mode:'full', modes:{options:[]}, login:{label:'Login Claude'}},
];
const catalog = ['devagent','hostagent'].reduce((out,user) => ({...out,[user]:[
  {provider:'codex',label:'Codex',status:'available',models:[{model_id:'gpt-test',display_name:'gpt-test',efforts:['low','medium','high']}]},
  {provider:'claude',label:'Claude',status:'available',models:[{model_id:'no-effort',display_name:'no-effort',efforts:[]},
    {model_id:'reasoning',display_name:'reasoning',efforts:['medium','high']}]},
]}),{});
const defaults = {profile_id:'codex-openai',model:'gpt-test',effort:'high'};
const boot = {user:'sandbox-user',csrf:'synthetic-csrf',profiles,catalog,
  account_defaults:{devagent:defaults,hostagent:defaults},unix_users:{project:'devagent',server:'hostagent',service:'agenthub'},
  projects_root:'/synthetic/projects',server_home:'/synthetic/host',max_upload:1000000,allowed_doc_ext:['.txt'],asset_version:''};
const project = {slug:'demo',path:'/synthetic/projects/demo',source:'local'};
const doc = {id:'doc1',name:'note.txt',path:'/synthetic/note.txt',scope:'private',size:12,project_slug:'demo'};
const session = {id:'s1',name:'Synthetic session',environment:'PROJECT',unix_user:'devagent',project_slug:'demo',
  profile_id:'codex-openai',workdir:project.path,alive:true,status:'running',lifecycle:'RUNNING',messages_total:1,
  initial_prompt:'initial',created_at:stamp,report_summary:''};
const runtime = {unix_user:'devagent',live:{state:'live',model:'gpt-test',effort:'high'},configured:{model:'gpt-test',effort:'high'},
  capabilities:{...profiles[0],profile_id:profiles[0].id,modes:profiles[0].modes.options,supports_goal:true}};
const meeting = {id:'m1',title:'Synthetic meeting',project_slug:'demo',status:'completed',created_at:stamp,
  meeting_date:stamp,round:1,approval_count:2,proposal:{summary:'Summary',decisions:[],actions:[],open_questions:[]}};
const idea = {id:'idea1',title:'Idea',description:'Backlog example',status:'ready',environment:'PROJECT',
  prepared_prompt:'Prepared prompt',source:'telegram_text',created_at:stamp};

async function exercise(browser, root, viewport) {
  const context = await browser.newContext({viewport,locale:'it-IT',timezoneId:'Europe/Rome',serviceWorkers:'block'});
  const page = await context.newPage();
  const errors = [], unexpected = [], mutations = [], snapshots = {};
  let createFailure = '', sendFailure = false;
  await page.addInitScript(() => {
    const NativeDate = Date;
    globalThis.Date = class extends NativeDate {
      constructor(...args) { super(...(args.length ? args : ['2026-09-12T12:00:00Z'])); }
      static now() { return new NativeDate('2026-09-12T12:00:00Z').getTime(); }
    };
  });
  page.on('pageerror', e => errors.push(e.message));
  await page.routeWebSocket('**/*', socket => socket.close());
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname;
    if (p === '/' || p.startsWith('/static/')) {
      const asset = path.join(root,'app/static',p === '/' ? 'index.html' : p.slice(8));
      if (!fs.existsSync(asset)) return route.abort();
      let body = fs.readFileSync(asset);
      if (p === '/') body = body.toString().replace(/<script[\s\S]*?<\/script>/g,'');
      const mime = {'.js':'text/javascript','.css':'text/css','.html':'text/html'};
      return route.fulfill({body,contentType:mime[path.extname(asset)] || 'image/png'});
    }
    if (request.method() !== 'GET') {
      const type = request.headers()['content-type'] || '';
      let body;
      if (type.includes('json')) body = request.postDataJSON();
      else {
        const raw = request.postData() || '';
        body = {files:[...raw.matchAll(/filename="([^"]+)"/g)].map(m => m[1]),
          fields:[...raw.matchAll(/name="([^"]+)"\r\n\r\n([^\r]*)/g)].map(m => [m[1],m[2]])};
      }
      mutations.push({path:p,method:request.method(),body});
      assert.equal(request.headers()['x-csrf-token'],'synthetic-csrf');
      if (p === '/api/sessions' && createFailure) return route.fulfill({status:400,json:{detail:'synthetic creation failure'},
        headers:createFailure === 'persisted' ? {'X-Agent-Hub-Error-Code':'launch_failed','X-Agent-Hub-Session-Id':'s1'} : {}});
      if (p.endsWith('/messages') && sendFailure) return route.fulfill({status:503,json:{detail:'synthetic delivery failure'}});
      if (p === '/api/documents') return route.fulfill({json:{documents:[{...doc,id:'uploaded',name:'upload.txt'}],errors:[]}});
      return route.fulfill({json:{ok:true,registered:true,session,defaults,delivery:'paste',created:[],output:'synthetic',rc:0}});
    }
    if (p.endsWith('/transcript')) return route.fulfill({contentType:'text/plain',body:'Synthetic transcript\n',headers:{'X-Transcript-Lines':'1'}});
    const fixtures = {
      '/api/bootstrap':boot, '/api/projects':{projects:[project],unregistered:[]},
      '/api/projects/demo':{project,documents:[doc],ports:[],status:{compose:true,branch:'main'}},
      '/api/projects/demo/context':{context:{target_architecture:'Target'},package_ready:true},
      '/api/documents':{documents:[doc],allowed_ext:['.txt'],max_upload:1000000},
      '/api/backlog':{ideas:[idea]}, '/api/backlog/idea1':{idea},
      '/api/sessions':{sessions:[session],unread_count:0},
      '/api/sessions/s1':{session,documents:[doc],messages:[],reports:[],diagnostics:{}},
      '/api/sessions/s1/state':session, '/api/sessions/s1/runtime':runtime,
      '/api/sessions/s1/messages':{messages:[{id:'msg1',text:'initial',status:'sent',kind:'initial',created_at:stamp}]},
      '/api/accounts':{accounts:{devagent:{},hostagent:{}},profiles,catalog,account_defaults:boot.account_defaults,meta:{}},
      '/api/status':{service:'active',tmux_sessions:{devagent:[],hostagent:[]},versions:{python:'test'},dirs:{},meta:{}},
      '/api/status/docker':{project_docker_rootless:{},server_docker_privileged:{},meta:{}},
      '/api/health':{last:{overall:'OK',checks:[],generated_at:stamp},history:[]},
      '/api/harness-update':{last:{}},'/api/meetings':{meetings:[meeting]},
      '/api/meetings/m1':{meeting,transcript:'Meeting transcript'}, '/api/push':{enabled:false},'/api/usage':{usage:[]},
      '/api/fs':{path:'/synthetic',parent:'/',dirs:['projects']},
    };
    if (p in fixtures) return route.fulfill({json:fixtures[p]});
    unexpected.push(p); return route.abort();
  });
  await page.goto('http://agent-hub.test/');
  for (const asset of ['vendor/xterm.js','vendor/addon-fit.js']) await page.addScriptTag({path:path.join(root,'app/static',asset)});
  await page.addScriptTag({content:fs.readFileSync(path.join(root,'app/static/app.js'),'utf8').split('(async function boot() {')[0]});
  await page.evaluate(value => { BOOT = value; }, boot);
  await page.evaluate(() => {
    globalThis.pendingAuditRequests = 0;
    const original = api;
    api = async (...args) => {
      pendingAuditRequests++;
      try { return await original(...args); }
      finally { pendingAuditRequests--; }
    };
  });
  snapshots.runtimeMatrix = await page.evaluate(() => {
    const oldCatalog = BOOT.catalog;
    const models = [{model_id:'effort',efforts:['low','high']}, {model_id:'plain',efforts:[]},
      {model_id:'alias',kind:'alias'}, {model_id:'unspecified'}];
    BOOT.catalog = {devagent:[{provider:'claude',status:'available',models}]};
    const results = [];
    for (const supports_effort of [true,false]) for (const supports_model of [true,false])
      for (const model of ['','effort','plain','alias','unspecified','custom'])
        for (const state of ['live','pending']) for (const unix_user of ['devagent','unknown']) {
          results.push(runtimeBody({unix_user,live:{state,model,effort:'high'},configured:{},
            capabilities:{profile_id:'claude-anthropic',supports_effort,supports_model,
              effort_levels:['low','medium','high'],modes:[]}}));
        }
    BOOT.catalog = oldCatalog;
    return results;
  });
  async function navigate(hash) {
    await page.waitForFunction(() => pendingAuditRequests === 0);
    await page.evaluate(async hash => { location.hash = hash; await route(); }, hash);
    assert.equal(await page.locator('#banner .errbox').count(),0,hash);
  }
  async function snapshot(name) {
    snapshots[name] = await page.evaluate(() => {
      const el = document.querySelector('#view').cloneNode(true);
      el.querySelectorAll('#term,#ws-state,#tr-meta').forEach(n => n.remove());
      return {html:el.innerHTML,controls:[...document.querySelectorAll('#view input,#view textarea,#view select')].map(n =>
        ({id:n.id,value:n.value,checked:n.checked,disabled:n.disabled,options:n.options ? [...n.options].map(o => [o.value,o.text]) : undefined}))};
    });
  }
  for (const hash of ['#/','#/projects','#/projects/demo','#/projects/demo?tab=documents',
    '#/projects/demo?tab=meetings','#/projects/demo?tab=context','#/backlog','#/new?project=demo',
    '#/accounts','#/status','#/meetings?project=demo','#/meetings?project=demo&action=new','#/meetings/m1',
    '#/session/s1/transcript?source=native&tail=500','#/documents']) {
    await navigate(hash); await snapshot(hash);
  }
  assert.match(snapshots['#/documents'].html,/Pagina non trovata/);
  await navigate('#/new?project=demo');
  await page.locator('#s-prompt').fill('first line\nsecond line');
  await navigate('#/projects'); await navigate('#/new?project=demo');
  assert.equal(await page.locator('#s-prompt').inputValue(),'first line\nsecond line');
  await page.locator('#s-profile').selectOption('claude-anthropic');
  await page.locator('#s-model').selectOption('no-effort');
  assert.equal(await page.locator('#s-effort').isDisabled(),true);
  await page.locator('#s-model').selectOption('reasoning');
  assert.equal(await page.locator('#s-effort').isEnabled(),true);
  await page.locator('#s-model').selectOption('__other__');
  await page.locator('#s-model-other').fill(' custom-model ');
  await snapshot('custom-model');
  await page.locator('#s-go').click();
  await page.waitForFunction(() => location.hash === '#/session/s1');
  assert.equal(mutations.filter(m => m.path === '/api/sessions').at(-1).body.model,'custom-model');
  assert.equal(await page.evaluate(() => readNewSessionPromptDraft()),'');
  for (const mode of ['', 'plan', 'goal']) {
    await navigate('#/new?project=demo');
    await page.locator('#s-prompt').fill('Mode '+(mode || 'normal'));
    await page.locator('#s-mode').selectOption(mode);
    await page.locator('#s-docs-toggle').click();
    await page.locator('.sel-doc').check();
    await snapshot('new-'+(mode || 'normal'));
    await page.locator('#s-go').click();
    await page.waitForFunction(() => location.hash === '#/session/s1');
  }
  await navigate('#/new'); await page.locator('#s-env').selectOption('SERVER');
  await page.locator('#s-prompt').fill('Server task'); await page.locator('#s-go').click();
  await page.waitForFunction(() => location.hash === '#/session/s1');
  await navigate('#/new?backlog=idea1');
  assert.equal(await page.locator('#s-prompt').inputValue(),'Prepared prompt');
  await page.locator('#s-use-backlog-prompt').uncheck(); await page.locator('#s-go').click();
  await page.waitForFunction(() => location.hash === '#/session/s1');
  assert.equal(mutations.filter(m => m.path === '/api/sessions').at(-1).body.prompt,'');
  await navigate('#/new'); await page.locator('#s-prompt').fill('recoverable');
  createFailure = 'before'; await page.locator('#s-go').click();
  await page.waitForFunction(() => document.querySelector('#s-err').textContent.includes('failure'));
  assert.equal(await page.evaluate(() => readNewSessionPromptDraft()),'recoverable');
  createFailure = 'persisted'; await page.locator('#s-go').click();
  await page.waitForFunction(() => location.hash === '#/session/s1');
  assert.equal(await page.evaluate(() => readNewSessionPromptDraft()),''); createFailure = '';
  await navigate('#/session/s1');
  await page.locator('#msg').fill('draft message'); await navigate('#/projects'); await navigate('#/session/s1');
  assert.equal(await page.locator('#msg').inputValue(),'draft message');
  sendFailure = true; await page.locator('#send').click();
  await page.waitForFunction(() => document.querySelector('#sess-err').textContent.includes('failure'));
  assert.equal(await page.locator('#msg').inputValue(),'draft message'); sendFailure = false;
  await page.locator('#attach').click(); await page.locator('[data-attach-doc]').check();
  await page.locator('[data-attach-files]').setInputFiles({name:'upload.txt',mimeType:'text/plain',buffer:Buffer.from('synthetic')});
  await page.getByRole('button',{name:'Aggiungi al messaggio',exact:true}).click();
  await page.locator('#send').click(); await page.waitForFunction(() => document.querySelector('#msg').value === '');
  assert.equal(await page.evaluate(() => readSessionMessageDraft('s1')),'');
  assert.deepEqual(mutations.filter(m => m.path.endsWith('/messages')).at(-1).body.document_ids,['doc1','uploaded']);
  await page.locator('#msg-toggle').click(); await page.waitForSelector('[data-mcopy]');
  await page.locator('#runtime-toggle').click(); await page.waitForSelector('#rt-model');
  await snapshot('active-session');
  await page.locator('#rt-model').selectOption('__other__');
  await page.locator('#rt-model-other').fill(' runtime-custom ');
  await page.locator('#rt-apply').click();
  await page.waitForFunction(() => pendingAuditRequests === 0);
  assert.equal(mutations.filter(m => m.path.endsWith('/runtime')).at(-1).body.model,'runtime-custom');
  await page.locator('#runtime-toggle').click();
  await page.locator('#msg-toggle').click();
  for (const button of ['#a-up','#a-down','#a-enter','#a-pause','#term-page-up','#term-page-down','#term-live']) {
    await page.locator(button).click();
  }
  assert.deepEqual(errors,[]);
  await navigate('#/accounts'); await page.locator('[data-defaults="devagent"] [data-default-model]').selectOption('__other__');
  await page.locator('[data-defaults="devagent"] [data-default-model-other]').fill('other-account-model');
  await page.locator('[data-defaults="devagent"] [data-default-save]').click();
  await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('salvato'));
  await page.waitForFunction(() => pendingAuditRequests === 0);
  if (evidence) {
    await navigate('#/new');
    await page.waitForFunction(() => !document.querySelector('#toast').classList.contains('shown'));
    const file = path.join(evidence,`${path.basename(root)}-${viewport.width}.png`);
    const shot = await page.screenshot({path:file,fullPage:true,animations:'disabled',caret:'hide'});
    snapshots.screenshot = crypto.createHash('sha256').update(shot).digest('hex');
  }
  // Characterize the pre-existing Accounts bug: currentTarget is null after await.
  assert.deepEqual(errors,["Cannot set properties of null (setting 'disabled')"]);
  assert.deepEqual(unexpected,[]);
  await page.evaluate(() => {if (cleanup) cleanup();});
  await context.close();
  return {snapshots,mutations,errors};
}

(async () => {
  const browser = await chromium.launch({executablePath:process.env.CHROMIUM || '/usr/bin/chromium'});
  try {
    const summary = [];
    for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
      const runs = [];
      for (const root of roots) runs.push(await exercise(browser,root,viewport));
      if (runs.length === 2) assert.deepEqual(runs[1],runs[0],`UI changed at ${viewport.width}px`);
      summary.push({width:viewport.width,views:Object.keys(runs[0].snapshots).length-1,
        runtimeVariants:runs[0].snapshots.runtimeMatrix.length,
        mutations:runs[0].mutations.length,equivalent:runs.length === 2});
    }
    console.log(JSON.stringify(summary,null,2));
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
