/* Real HTTP, multipart, CSRF, SQLite and WebSocket transport in a network namespace.
 * Only the host/provider adapters and terminal producer are synthetic. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium,firefox} = require('playwright');
const candidate = path.resolve(__dirname,'..');
const baseline = path.resolve(process.argv[2]);
const output = process.env.AUDIT_EVIDENCE || path.join(candidate,'../evidence/fullstack');
fs.mkdirSync(output,{recursive:true});
const configurations = [
  {engine:'chromium',width:320,height:568,theme:'light',storage:'normal',mobile:true},
  {engine:'chromium',width:390,height:844,theme:'dark',storage:'quota',mobile:true},
  {engine:'chromium',width:768,height:1024,theme:'light',storage:'normal'},
  {engine:'chromium',width:1440,height:1000,theme:'dark',storage:'normal'},
  {engine:'firefox',width:390,height:844,theme:'light',storage:'normal'},
  {engine:'firefox',width:1440,height:1000,theme:'dark',storage:'quota'},
];

async function exercise(browser, root, config, tag) {
  const work = path.join(os.tmpdir(),'agent-hub-http-fixture');
  fs.mkdirSync(work);
  const server = spawn('python',[path.join(__dirname,'audit_backend_fixture.py'),root,work,'18761'],{stdio:['ignore','pipe','pipe']});
  let serverOutput='';
  server.stderr.on('data',chunk=>serverOutput+=chunk);
  const url='http://127.0.0.1:18761';
  let context;
  try {
    let ready=false;
    for(let i=0;i<100;i++) {
      if(server.exitCode!==null) throw Error(serverOutput);
      try {if((await fetch(url+'/api/bootstrap')).ok){ready=true;break;}}catch{}
      await new Promise(r=>setTimeout(r,100));
    }
    assert.ok(ready,serverOutput);
    context=await browser.newContext({viewport:{width:config.width,height:config.height},
      colorScheme:config.theme,locale:'it-IT',timezoneId:'Europe/Rome',serviceWorkers:'block',
      ...(config.mobile ? {isMobile:true,hasTouch:true}: {})});
    await context.tracing.start({screenshots:true,snapshots:true,sources:true});
    await context.addInitScript(({theme,storage})=>{
      const NativeDate=Date;
      globalThis.Date=class extends NativeDate {
        constructor(...args){super(...(args.length?args:['2026-09-12T12:00:00Z']));}
        static now(){return new NativeDate('2026-09-12T12:00:00Z').getTime();}
      };
      localStorage.setItem('agenthub-theme',theme);
      if(storage==='quota') {
        const set=Storage.prototype.setItem;
        Storage.prototype.setItem=function(key,value){
          if(key.startsWith('agenthub-new-session-prompt:') || key.startsWith('agenthub-session-message:'))
            throw new DOMException('Synthetic full storage','QuotaExceededError');
          return set.call(this,key,value);
        };
      }
    },config);
    const page=await context.newPage();
    page.setDefaultTimeout(15000);
    const errors=[], messageResponses=[], snapshots=[], checks=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('dialog',dialog=>dialog.accept());
    page.on('response',r=>{if(r.request().method()==='POST' && /\/messages$/.test(new URL(r.url()).pathname))messageResponses.push(r.status());});
    let csrf='audit-csrf';
    const control=async body=>{
      const r=await context.request.post(url+'/_audit/control',{headers:{'x-csrf-token':csrf},data:body});
      assert.equal(r.status(),200);
      if(body.csrf)csrf=body.csrf;
    };
    const state=async()=> (await context.request.get(url+'/_audit/state')).json();
    const navigate=async(hash,selector)=>{
      await page.evaluate(hash=>location.hash=hash,hash);
      await page.waitForSelector(selector);
    };
    const screenshot=async(name)=>{
      await page.waitForFunction(()=>!document.querySelector('#toast').classList.contains('shown'));
      await page.evaluate(async()=>{document.activeElement?.blur();await document.fonts.ready;});
      await page.mouse.move(0,0);
      await page.screenshot({path:path.join(output,tag+'-'+name+'.png'),fullPage:true,animations:'disabled',caret:'hide'});
      snapshots.push(await page.evaluate(()=>({
        html:document.querySelector('#view').innerHTML.replace(/<canvas[\s\S]*?<\/canvas>/g,'<canvas></canvas>'),
        controls:[...document.querySelectorAll('#view input,#view textarea,#view select')].map(e=>({id:e.id,value:e.value,disabled:e.disabled,checked:e.checked}))
      })));
    };
    await page.goto(url+'/#/new?project=demo');
    await page.waitForSelector('#s-prompt');
    const draft='Riga italiana é\n日本語 🧪 <script>literal</script>';
    await page.locator('#s-prompt').fill(draft);
    await page.reload();await page.waitForSelector('#s-prompt');
    assert.equal(await page.locator('#s-prompt').inputValue(),config.storage==='quota'?'':draft);
    checks.push('draft-reload-'+config.storage);
    await page.locator('#s-prompt').fill(draft);
    await page.locator('#s-files').setInputFiles([
      {name:'good.txt',mimeType:'text/plain',buffer:Buffer.from('Uploaded via the real API')},
      {name:'bad.exe',mimeType:'application/octet-stream',buffer:Buffer.from('rejected')},
    ]);
    await screenshot('new');
    await page.locator('#s-go').click();
    await page.waitForSelector('#msg');
    await page.waitForFunction(()=>document.querySelector('#ws-state').textContent.startsWith('connesso'));
    const sid=await page.evaluate(()=>location.hash.split('/')[2]);
    let saved=await state();
    assert.equal(saved.database.documents.length,1);
    assert.ok(saved.database.sessions[0].initial_prompt.includes(draft));
    checks.push('new-partial-upload-and-persisted-prompt');
    for(const selector of ['#a-left','#a-right','#a-space','#a-tab','#a-context'])await page.locator(selector).click();
    for(let i=0;i<50;i++) {saved=await state();if(saved.events.filter(e=>e[0]==='terminal-input').length===5)break;await page.waitForTimeout(20);}
    assert.deepEqual(saved.events.filter(e=>e[0]==='terminal-input').map(e=>e[1]),['\x1b[D','\x1b[C',' ','\t','\x14']);
    checks.push('five-terminal-keys-over-websocket');
    await page.locator('#msg').fill('Draft after navigation');
    await navigate('#/projects','#view .project-tile');
    await navigate('#/session/'+sid,'#msg');
    assert.equal(await page.locator('#msg').inputValue(),config.storage==='quota'?'':'Draft after navigation');
    checks.push('active-draft-navigation-'+config.storage);
    await page.locator('#msg').fill('Retry same message');
    await context.setOffline(true);await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('#sess-err').textContent.length>0);
    assert.equal(await page.locator('#msg').inputValue(),'Retry same message');
    await context.setOffline(false);
    checks.push('network-failure-preserves-text');
    await control({reject_message:true});
    await page.locator('#send').click();
    await page.waitForFunction(()=>!document.querySelector('#send').disabled);
    assert.equal(await page.locator('#msg').inputValue(),'Retry same message');
    assert.equal(messageResponses.at(-1),500);
    checks.push('sqlite-failure-preserves-text');
    await control({csrf:'rotated-csrf'});
    await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('#msg').value==='');
    assert.deepEqual(messageResponses.slice(-2),[403,200]);
    checks.push('csrf-refresh-and-single-retry');
    await page.locator('#attach').click();
    await page.locator('[data-attach-doc]').first().check();
    await page.getByRole('button',{name:'Aggiungi al messaggio',exact:true}).click();
    await page.locator('#send').click();
    await page.waitForFunction(()=>!document.querySelector('#send').disabled);
    saved=await state();
    assert.ok(saved.database.messages.at(-1).text.includes('Documenti allegati'));
    checks.push('attachment-only-message');
    await page.locator('#msg-toggle').click();await page.waitForSelector('[data-mcopy]');
    assert.equal(await page.locator('#messages script').count(),0);
    checks.push('escaped-history');
    await screenshot('history');
    await navigate('#/projects/demo?tab=documents','a[download]');
    const download=page.waitForEvent('download');
    await page.locator('a[href*="/api/documents/"][download]').first().click();
    const downloaded=await download;
    assert.equal(fs.readFileSync(await downloaded.path(),'utf8'),'Uploaded via the real API');
    checks.push('real-document-download');
    for(const mode of ['plan','goal']) {
      await navigate('#/new?project=demo','#s-prompt');
      await page.locator('#s-mode').selectOption(mode);
      await page.locator('#s-prompt').fill('Objective '+mode);
      await page.locator('#s-go').click();await page.waitForSelector('#msg');
      checks.push('create-'+mode);
    }
    await navigate('#/new?project=demo','#s-prompt');
    await page.locator('#s-prompt').fill('Failed launch text');
    await control({fail_start:true});await page.locator('#s-go').click();
    await page.waitForSelector('#msg');
    saved=await state();
    assert.equal(saved.database.sessions.at(-1).status,'failed');
    assert.equal(saved.database.messages.at(-1).text,'Failed launch text');
    checks.push('failed-launch-opens-persisted-session');
    await control({fail_start:false});
    const relevant={};
    for(const key of ['sessions','messages','documents','session_documents','account_session_defaults'])relevant[key]=saved.database[key];
    // Polling work probes/receipt scheduling are clock observations, not user data.
    for(const row of relevant.sessions)for(const key of ['work_probe_at','work_probe_mtime'])delete row[key];
    assert.deepEqual(errors,[]);
    return JSON.parse(JSON.stringify({checks,snapshots,database:relevant,
      terminal:saved.events.filter(e=>e[0]==='terminal-input'),messageResponses}).split(work).join('<fixture>'));
  } finally {
    if(context){await context.tracing.stop({path:path.join(output,tag+'.zip')});await context.close();}
    server.kill('SIGTERM');
    await new Promise(resolve=>{if(server.exitCode!==null)return resolve();server.once('exit',resolve);});
    if(serverOutput)fs.writeFileSync(path.join(output,tag+'-server.log'),serverOutput);
    fs.rmSync(work,{recursive:true,force:true});
  }
}

(async()=>{
  const summary=[];
  for(const config of configurations.filter(c=>!process.env.AUDIT_ENGINE || c.engine===process.env.AUDIT_ENGINE)){
    const browser=await ({chromium,firefox}[config.engine]).launch(config.engine==='firefox'
      ? {env:{...process.env,MOZ_DISABLE_CONTENT_SANDBOX:'1'}} : {});
    const tag=config.engine+'-'+config.width+'-'+config.theme+'-'+config.storage;
    try {
      const a=await exercise(browser,baseline,config,'baseline-'+tag);
      const b=await exercise(browser,candidate,config,'candidate-'+tag);
      fs.writeFileSync(path.join(output,tag+'-observations.json'),JSON.stringify({baseline:a,candidate:b},null,2));
      assert.deepEqual(b,a,tag);
      summary.push({...config,checks:a.checks.length,equivalent:true});
      console.log(JSON.stringify(summary.at(-1)));
    }finally{await browser.close();}
  }
  fs.writeFileSync(path.join(output,'summary.json'),JSON.stringify(summary,null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
