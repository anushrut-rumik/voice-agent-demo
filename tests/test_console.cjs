const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function consoleContext(){
 const elements=new Map();
 const document={querySelector(s){if(!elements.has(s))elements.set(s,{dataset:{},classList:{toggle(){},add(){},remove(){}},addEventListener(){},pause(){},removeAttribute(){},close(){},value:''});return elements.get(s);},addEventListener(){}};
 const context=vm.createContext({document,Intl,Date,Map,setTimeout(){},setInterval(){},fetch:()=>new Promise(()=>{}),assert});
 vm.runInContext(fs.readFileSync('static/app.js','utf8'),context);return context;
}
test('state refreshes coalesce and a mutation gets a subsequent fresh read',async()=>{
 const c=consoleContext();await vm.runInContext(`(async()=>{
  refreshPending=null;render=()=>{};let reads=0,release;
  api=async()=>{reads++;if(reads===1)await new Promise(resolve=>{release=resolve;});return {version:reads};};
  const first=refresh(),second=refresh(),afterMutation=refresh(true);
  assert.equal(reads,1);release();await Promise.all([first,second,afterMutation]);
  assert.equal(reads,2);assert.equal(state.version,2);assert.equal(refreshPending,null);
 })()`,c);
});
test('refresh cadence follows activity and visibility',()=>{
 const c=consoleContext();vm.runInContext(`
  state={customers:[],reminders:{run:null}};assert.equal(refreshDelay(),3000);
  state.reminders.run={status:'running'};assert.equal(refreshDelay(),1000);
  state.reminders.run=null;state.customers=[{calls:[{status:'calling'}]}];assert.equal(refreshDelay(),1000);
  document.hidden=true;assert.equal(refreshDelay(),15000);
 `,c);
});
test('run status shows the active call while an older brief is pending',()=>{
 const c=consoleContext();vm.runInContext(`
  state={clock:'2026-10-23T11:00:00+05:30',customers:[],reminders:{run:{status:'running',setup_checked:1,
   starts_at:'2026-10-23T10:00:00+05:30',finished:0,total:2,items:[
    {status:'summarizing',name:'Earlier customer'},{status:'calling',name:'Current customer'}]}}};
  renderRun();assert.match(document.querySelector('#run-description').textContent,/Calling Current customer/);
 `,c);
});
test('starting a reminder run submits one stable request and never requests browser audio',async()=>{
 const c=consoleContext();await vm.runInContext(`(async()=>{
  let ids=0,attempts=[];crypto={randomUUID:()=> 'request-'+(++ids)};state={reminders:{ready_count:2,configured:true,run:null}};
  refresh=async()=>{};renderRun=()=>{};
  api=async(path,body)=>{assert.equal(path,'reminders/start');attempts.push(body);throw new Error('Response lost');};
  await startReminders();await startReminders();
  assert.equal(attempts.length,2);assert.equal(attempts[0].request_id,attempts[1].request_id);
  assert.equal(ids,1);assert.equal(typeof microphonePreflight,'undefined');
 })()`,c);
});
test('saving a contact never starts a call or a reminder run',async()=>{
 const c=consoleContext();await vm.runInContext(`(async()=>{
  selected='priya';document.querySelector('#contact-phone').value='+12025550101';let paths=[];
  api=async(path,body)=>{paths.push(path);assert.equal(body.phone,'+12025550101');};refresh=async()=>{};
  await saveContact();assert.deepEqual(paths,['customers/priya/contact']);
 })()`,c);
});
test('pause state presents resume rather than creating another run',async()=>{
 const c=consoleContext();await vm.runInContext(`(async()=>{
  state={reminders:{run:{id:'saved-run',status:'paused'}}};renderRun=()=>{};refresh=async()=>{};
  api=async(path)=>{assert.equal(path,'reminders/saved-run/resume');};await startReminders();
 })()`,c);
});
test('a conversation brief shows the actual plan and next action without review controls',()=>{
 const c=consoleContext();vm.runInContext(`
  const call={id:'done',mode:'live',status:'completed',outcome:'promise_to_pay',saved_outcomes:[{summary:'Old note'}],conversation_summary:'Customer will pay in two to three days.',next_step:'Check the ledger after the agreed payment window.',payment_details:{promised_amount:'32500',payment_date_start:'2026-10-24',payment_date_end:'2026-10-25'}};
  renderAdmin({calls:[call]},call);
  const html=document.querySelector('#call-outcome').innerHTML;
  assert.match(html,/Customer will pay in two to three days/);assert.match(html,/2026-10-24 to 2026-10-25/);assert.match(html,/Check the ledger/);
  assert.doesNotMatch(html,/Old note|review|Gemini/i);
 `,c);
});
test('missing contact is a setup action and never inferred from a test-call number',()=>{
 const c=consoleContext();vm.runInContext(`
  const account={id:'priya',status:'active',balance:32500,threshold:5000,phone:'',calls:[{mode:'live',to_number:'+12025550101',status:'completed'}]};
  assert.equal(statusLabel(account),'Contact needed');renderDecision(account,account.calls[0]);
  assert.match(document.querySelector('#call-result').innerHTML,/Customer contact needed/);
 `,c);
});
test('pending runs clearly show the future calling window and cannot start twice',()=>{
 const c=consoleContext();vm.runInContext(`
  state={clock:'2026-10-22T22:00:00+05:30',voice_configured:true,reminders:{configured:true,ready_count:2,run:{id:'run',status:'running',starts_at:'2026-10-23T10:00:00+05:30',total:2,finished:0,items:[]}}};
  renderRun();assert.equal(document.querySelector('#start-reminders').disabled,true);
  assert.match(document.querySelector('#run-description').textContent,/Starts/);
 `,c);
});
test('an individual call submits only the selected customer and a new deliberate call gets a new ID',async()=>{
 const c=consoleContext();await vm.runInContext(`(async()=>{
  let ids=0,attempts=[];crypto={randomUUID:()=> 'individual-'+(++ids)};selected='priya';
  render=()=>{};refresh=async()=>{};
  api=async(path,body)=>{assert.equal(path,'reminders/start');attempts.push(body);};
  await callCustomer();await callCustomer();
  assert.equal(attempts.length,2);assert.equal(attempts[0].customer_id,'priya');
  assert.equal(attempts[1].customer_id,'priya');assert.notEqual(attempts[0].request_id,attempts[1].request_id);
  assert.equal(runRequest,null);
 })()`,c);
});
test('a lost individual response keeps the request ID per customer and cannot call the wrong account',async()=>{
 const c=consoleContext();await vm.runInContext(`(async()=>{
  let ids=0,attempts=[];crypto={randomUUID:()=> 'individual-'+(++ids)};
  render=()=>{};refresh=async()=>{};
  api=async(path,body)=>{attempts.push(body);throw new Error('Response lost');};
  selected='priya';await callCustomer();selected='rahul';await callCustomer();selected='priya';await callCustomer();
  assert.equal(attempts[0].request_id,attempts[2].request_id);
  assert.notEqual(attempts[0].request_id,attempts[1].request_id);
  assert.equal(attempts[1].customer_id,'rahul');assert.equal(ids,2);
 })()`,c);
});
test('Call all and Call now both remain available when all follow-ups are in the future',()=>{
 const c=consoleContext();vm.runInContext(`
  const account={id:'priya',name:'Priya',status:'active',balance:32500,threshold:5000,phone:'+12025550101',calls:[],job:{status:'scheduled',due_at:'2026-10-26T10:00:00+05:30'}};
  state={customers:[account],voice_configured:true,reminders:{ready_count:0,run:{status:'completed',finished:1,total:1}}};
  renderRun();renderDecision(account,null);
  assert.equal(document.querySelector('#start-reminders').textContent,'Call all');
  assert.equal(document.querySelector('#start-reminders').disabled,false);
  assert.match(document.querySelector('#run-description').textContent,/Call all to run everyone again/);
  assert.doesNotMatch(document.querySelector('#run-description').textContent,/No calls are due/);
  assert.match(document.querySelector('#call-result').innerHTML,/Call Priya now/);
  assert.doesNotMatch(document.querySelector('#call-result').innerHTML,/disabled/);
 `,c);
});
test('a deliberate repeat bulk run gets a new request even with zero due customers',async()=>{
 const c=consoleContext();await vm.runInContext(`(async()=>{
  let ids=0,attempts=[];crypto={randomUUID:()=> 'bulk-'+(++ids)};
  state={reminders:{ready_count:0,callable_count:2,run:{status:'completed'}}};
  renderRun=()=>{};refresh=async()=>{};
  api=async(path,body)=>{assert.equal(path,'reminders/start');attempts.push(body);};
  await startReminders();await startReminders();
  assert.equal(attempts.length,2);assert.notEqual(attempts[0].request_id,attempts[1].request_id);
  assert.equal(attempts[0].customer_id,undefined);assert.equal(attempts[1].customer_id,undefined);
 })()`,c);
});
test('individual call is disabled during another call and hidden for paid or stopped accounts',()=>{
 const c=consoleContext();vm.runInContext(`
  const account={id:'priya',name:'Priya',status:'active',balance:32500,threshold:5000,phone:'+12025550101',calls:[]};
  state={customers:[account,{calls:[{status:'uncertain'}]}],reminders:{run:null}};
  renderDecision(account,null);assert.match(document.querySelector('#call-result').innerHTML,/disabled/);
  for(const status of ['cleared','suppressed','paused']){
   renderDecision({...account,status},null);assert.doesNotMatch(document.querySelector('#call-result').innerHTML,/id="call-customer"/);
  }
 `,c);
});
