/* Deterministic input sequences against both shipped JavaScript sources. */
const fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const assert=require('node:assert/strict'), crypto=require('node:crypto');
const candidate=path.resolve(__dirname,'..');
function exercise(root){
  const context=vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(root,'app/static/app.js'),'utf8').split('(async function boot() {')[0],context);
  return JSON.parse(vm.runInContext(`(()=>{
    let seed=91723;
    const next=n=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return Math.floor(seed/4294967296*n);};
    const storage=new Map();let denied=false;
    globalThis.localStorage={getItem:k=>{if(denied)throw Error('denied');return storage.get(k)??null;},
      setItem:(k,v)=>{if(denied)throw Error('quota');storage.set(k,String(v));},
      removeItem:k=>{if(denied)throw Error('denied');storage.delete(k);}};
    const profiles=['codex-openai','claude-anthropic'].map(id=>({id,harness:id.split('-')[0]}));
    BOOT={user:'one',profiles,catalog:{devagent:[
      {provider:'codex',status:'available',models:[{model_id:'reason',efforts:['high','low']},{model_id:'plain',efforts:[]},{model_id:'alias',kind:'alias'}]},
      {provider:'claude',status:'unverified',models:[{model_id:'reason',efforts:['medium','high']},{model_id:'plain',efforts:[]},{model_id:'alias',kind:'alias'}]},
    ]}};
    const reads=[];
    const texts=['',' ','one\\ntwo','日本語 🧪','<script>literal</script>','x'.repeat(8192)];
    for(let i=0;i<2048;i++){
      BOOT.user=['one','two',''][next(3)];denied=next(7)===0;
      const sid=['s1','s2','s3'][next(3)],value=texts[next(texts.length)];
      if(next(2))writeNewSessionPromptDraft(value);else writeSessionMessageDraft(sid,value);
      reads.push([readNewSessionPromptDraft(),readSessionMessageDraft(sid)]);
    }
    const runtime=[];
    for(let i=0;i<512;i++){
      const model=['','reason','plain','alias','custom'][next(5)];
      const cap={profile_id:profiles[next(2)].id,supports_effort:!!next(2),supports_model:!!next(2),
        effort_levels:next(2)?['low','medium','high']:[],modes:[],supports_goal:!!next(2)};
      runtime.push(runtimeBody({unix_user:next(2)?'devagent':'missing',
        configured:{model:next(2)?model:'',effort:next(2)?'high':''},
        live:{state:['live','pending','unsupported','error'][next(4)],model:next(2)?model:'',effort:'medium'},
        capabilities:cap}));
    }
    return JSON.stringify({reads,storage:[...storage.entries()].sort(),runtime});
  })()`,context));
}
const a=exercise(path.resolve(process.argv[2])), b=exercise(candidate);
assert.deepEqual(b,a);
console.log(JSON.stringify({equivalent:true,draft_operations:2048,runtime_variants:512,
  seed:91723,distinct_runtime_outputs:new Set(a.runtime).size,observation_sha256:crypto.createHash('sha256').update(JSON.stringify(a)).digest('hex')},null,2));
