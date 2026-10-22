'use strict';
let state,selected=null,selectedCall=null,busy=false,entryKind='payment',runRequest=null,creatingCustomer=false,uploading=false,importReport=null;
const customerCallRequests=new Map();
let refreshPending=null;
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const inr=v=>new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',minimumFractionDigits:0,maximumFractionDigits:2}).format(v);
const date=v=>v?new Date(v).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'numeric',month:'short',year:'numeric',hour:'numeric',minute:'2-digit'}):'Not scheduled';
const label=v=>String(v||'').replaceAll('_',' ');
const terminal=v=>['completed','failed','expired'].includes(v);
const customer=()=>state?.customers.find(c=>c.id===selected);
const latest=c=>c?.calls.find(x=>x.mode==='live');
const focusedCall=c=>c?.calls.find(x=>x.id===selectedCall)||latest(c);
function updateHTML(selector,html){const el=$(selector);if(el.dataset.rendered!==html){el.innerHTML=html;el.dataset.rendered=html;}}
function error(message){$('#page-error').textContent=message;$('#page-error').hidden=!message;}
async function api(path,body){
 const r=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Demo-Console':'1'},...(body===undefined?{}:{body:JSON.stringify(body)})});
 const data=await r.json();if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Check the entered values and try again.');return data;
}
async function refresh(force=false){
 if(refreshPending){await refreshPending;if(force)return refresh(true);return;}
 refreshPending=(async()=>{state=await api('state');render();})();
 try{await refreshPending;}finally{refreshPending=null;}
}
function outcomeLabel(v){return ({promise_to_pay:'Payment expected',callback_requested:'Callback requested',payment_claimed:'Payment to verify',dispute:'Bill disputed',human_requested:'Help requested',do_not_call:'Calls stopped',wrong_number:'Wrong contact',no_commitment:'Payment timing not confirmed',unreachable:'Could not reach customer',setup_failed:'Customer details unavailable',call_completed:'Conversation completed',completed:'Call completed',failed:'Call could not connect',starting:'Starting call',calling:'Calling customer',running:'Conversation in progress',in_progress:'Conversation in progress',uncertain:'Checking call connection'})[v]||label(v)||'Awaiting outcome';}
function statusLabel(c){
 const call=latest(c);
 if(c.ledger?.length===0)return 'Add invoice';
 if(c.status==='cleared')return 'Paid';if(c.status==='suppressed'||c.contact_blocked)return 'Calls stopped';if(c.status==='paused')return 'Needs assistance';
 if(call&&!terminal(call.status))return outcomeLabel(call.status);
 if(c.balance<=c.threshold)return 'Below threshold';if(!c.phone)return 'Contact needed';
 return c.ready_for_reminder?'Due for follow-up':'Follow-up scheduled';
}
function renderRun(){
 const reminders=state.reminders||{},run=reminders.run,open=run&&['running','paused','stopping'].includes(run.status);
 const primary=$('#start-reminders'),secondary=$('#pause-reminders');
 primary.textContent=busy?'Working…':run?.status==='paused'?'Resume run':'Call all';
 const activeCall=state.customers?.some(c=>c.calls.some(x=>!terminal(x.status)));
 primary.disabled=!!(busy||(open&&run.status!=='paused')||(!open&&(activeCall||reminders.local_only)));
 secondary.hidden=!open;secondary.disabled=busy||run?.status==='stopping';secondary.textContent=run?.status==='paused'?'End run':'Pause';
 let text='Asha calls customers, saves a brief, and updates the next step.';
 if(open){
  const current=run.items.find(x=>['calling','uncertain','dialing'].includes(x.status))||run.items.find(x=>x.status==='summarizing');
  text=run.status==='paused'?'Run paused. '+(run.error||'Resume when you’re ready.'):
       run.status==='stopping'?'Finishing the current call. Remaining customers will not be called.':
       new Date(run.starts_at)>new Date(state.clock)?'Starts '+date(run.starts_at)+' IST · '+run.total+' customer'+(run.total===1?'':'s'):
       !run.setup_checked&&!current?'Connecting Asha. Calls will begin shortly.':
       `${run.finished} of ${run.total} finished${current?' · '+(current.status==='summarizing'?'Saving brief for ':current.status==='uncertain'?'Checking connection for ':'Calling ')+current.name:''}`;
 }else if(run?.status==='completed')text=`Last run finished · ${run.finished} of ${run.total} customers. Call all to run everyone again, or use Call now for one customer.`;
 else if(reminders.callable_count)text=reminders.callable_count+' customer'+(reminders.callable_count===1?' is':'s are')+' ready. Call all starts one pass now, including scheduled follow-ups.';
 else if(reminders.missing_phone_count)text='Add phone numbers to get '+reminders.missing_phone_count+' customer'+(reminders.missing_phone_count===1?'':'s')+' ready for calling.';
 else text='Call all starts a full run. Add customer contacts and unpaid bills to get started.';
 if(reminders.local_only&&!open)text='Local preview · Connect Asha to enable phone reminders.';
 $('#run-description').textContent=text;
 $('#connection-label').textContent=reminders.local_only?'Local preview':state.voice_configured?'Asha configured':'Agent setup needed';
 $('#connection-dot').classList.toggle('configured',state.voice_configured);
 $('#agent-model').textContent='Asha calls customers directly. Conversation briefs are saved automatically.';
}
function renderImport(){
 if(uploading)return;
 const report=importReport||state.last_import;if(!report)return;
 $('#import-feedback').hidden=false;
 $('#upload-title').textContent='Import Excel';
 $('#import-status').textContent=(report.already_loaded?'Already loaded · ':'')+report.customer_count+' customer'+(report.customer_count===1?'':'s')+' · '+report.invoice_count+' bill'+(report.invoice_count===1?'':'s')+(report.skipped_count?' · '+report.skipped_count+' row'+(report.skipped_count===1?'':'s')+' to check':'. Names, numbers and context are saved.');
 $('#import-issues').hidden=!report.skipped_count;
 $('#import-issues-title').textContent=report.skipped_count+' row'+(report.skipped_count===1?'':'s')+' to check';
 updateHTML('#import-issue-list',(report.issues||[]).map(x=>`<p><strong>${esc(x.sheet)} · Row ${x.row}</strong> ${esc(x.message)}</p>`).join(''));
}
function render(){
 if(!selected)selected=state.customers.find(c=>c.ready_for_reminder)?.id||state.customers.find(c=>c.status==='active'&&c.balance>c.threshold)?.id||state.customers[0]?.id;
 renderRun();renderImport();$('#account-count').textContent=state.customers.length;
 const attention=state.customers.filter(c=>c.status==='paused').length;
 updateHTML('#overview',`<span>Total outstanding</span><strong>${inr(state.customers.reduce((n,c)=>n+c.balance,0))}</strong>${attention?`<p class="attention">${attention} account${attention===1?'':'s'} need assistance</p>`:''}`);
 const query=$('#customer-search').value.trim().toLowerCase();
 updateHTML('#customer-list',state.customers.filter(c=>(c.name+' '+c.company+' '+c.phone).toLowerCase().includes(query)).map(c=>`<button class="account-choice" data-customer="${esc(c.id)}" aria-pressed="${c.id===selected}"><span class="avatar" aria-hidden="true">${esc(c.name.split(' ').map(x=>x[0]).slice(0,2).join(''))}</span><span class="account-name"><strong>${esc(c.name)}</strong><small>${esc(statusLabel(c))}</small></span><span class="chevron" aria-hidden="true">›</span></button>`).join('')||'<p class="empty">No customers found.</p>');
 const c=customer();if(!c)return;
 const call=focusedCall(c),last=latest(c);
 updateHTML('#account-facts',`<div class="account-header"><div><h1>${esc(c.name)}</h1><p class="company">${esc(c.company)}</p></div><button class="button secondary" id="edit-contact">${c.phone?'Edit contact':'Add phone number'}</button></div><p class="customer-contact">${c.phone?esc(c.phone):'No phone number saved'}</p><div class="balance-row"><div><span class="muted">Outstanding balance</span><div class="balance">${inr(c.balance)}</div></div><button class="button primary" id="${c.ledger.length?'open-payment':'open-first-invoice'}" ${c.ledger.length&&c.balance<=0?'disabled':''}>${c.ledger.length?'Record payment':'Add first invoice'}</button></div>`);
 renderDecision(c,last);renderAdmin(c,call);
 const summary=last?.conversation_summary||last?.saved_outcomes?.at(-1)?.summary;
 const preparing=last&&terminal(last.status)&&['pending','queued','processing','retry_wait'].includes(last.brief_status);
 $('#latest-brief').hidden=!last;
 updateHTML('#latest-brief',last?`<div class="section-heading"><h2>Last conversation</h2><button class="text-button" data-call="${esc(last.id)}">View call</button></div><p>${esc(summary|| (preparing?'Saving the conversation brief…':last.outcome==='unreachable'?'No conversation was completed.':!terminal(last.status)?'The call is in progress. A brief will appear afterward.':'The call is saved. A conversation brief is not available.'))}</p>${last.next_step?`<p class="brief-next"><strong>Next step</strong> ${esc(last.next_step)}</p>`:''}`:'');
 $('#call-heading').textContent=call?date(call.created_at):'Conversation';
 $('#transcript-label').textContent=call?.provider?.durationSeconds?call.provider.durationSeconds+' seconds · Hinglish':'Conversation transcript';
 updateHTML('#transcript',call?.transcript?.length?call.transcript.map((t,i)=>`<div id="turn-${i+1}" class="utterance ${t.role==='assistant'?'agent':''}"><strong>${t.role==='assistant'?'Asha':'Customer'}</strong><p>${esc(t.content||t.text||'')}</p></div>`).join(''):'<p class="empty">The transcript will appear when it is available.</p>');
 $('#call-reference').textContent=call?'Call ID: '+call.id:'';
 $('#load-recording').hidden=!call||!terminal(call.status)||call.id.startsWith('phone-');
 $('#retry-brief').hidden=call?.brief_status!=='failed';
 if($('#recording-player').dataset.call!==call?.id){$('#recording-player').pause();$('#recording-player').hidden=true;$('#recording-player').removeAttribute('src');$('#recording-player').dataset.call=call?.id||'';$('#recording-status').textContent='';}
 updateHTML('#activity',c.activity.map(x=>`<div class="activity-row"><time>${date(x.created_at)}</time><strong>${esc(x.title)}</strong><p>${esc(x.detail)}</p></div>`).join(''));
 updateHTML('#imported-context',(c.imported_details||[]).map(b=>`<h3 class="imported-bill-heading">${esc(b.reference)}</h3><dl class="imported-fields">${Object.entries(b.fields).map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>`).join('')||'<p class="small-note">No spreadsheet details for this customer.</p>');
 updateHTML('#ledger','<table><thead><tr><th>Reference</th><th>Type</th><th>Amount</th></tr></thead><tbody>'+c.ledger.map(x=>`<tr><td>${esc(x.reference)}</td><td>${esc(label(x.kind))}</td><td>${x.kind==='payment'?'−':''}${inr(x.amount)}</td></tr>`).join('')+'</tbody></table><p class="small-note">Reminder threshold: '+inr(c.threshold)+'. Seeded purchase details are fictional demo data.</p>');
}
function renderAdmin(c,call){
 const calls=c.calls.filter(x=>x.mode==='live');$('#history-count').textContent=calls.length?String(calls.length):'';
 updateHTML('#call-history',calls.length?calls.map(x=>`<button class="call-row" data-call="${esc(x.id)}"><span class="call-copy"><strong>${esc(outcomeLabel(x.outcome||x.status))}</strong><span>${date(x.created_at)}${x.provider?.durationSeconds?' · '+x.provider.durationSeconds+' sec':''}</span></span><span class="chevron" aria-hidden="true">›</span></button>`).join(''):'<div class="empty"><h3>No conversations yet</h3><p>Asha’s calls, briefs, and outcomes will appear here.</p></div>');
 const summary=call?.conversation_summary||call?.saved_outcomes?.at(-1)?.summary,plan=call?.payment_details;
 const planText=plan?.promised_amount?inr(plan.promised_amount)+' · '+(plan.payment_date_start===plan.payment_date_end?plan.payment_date_start:plan.payment_date_start+' to '+plan.payment_date_end):'';
 updateHTML('#call-outcome',call?`<h3>${esc(outcomeLabel(call.outcome||call.status))}</h3>${summary?`<p>${esc(summary)}</p>`:''}${planText?`<p>Payment expected: ${esc(planText)}</p>`:''}${call.next_step?`<p><strong>Next step:</strong> ${esc(call.next_step)}</p>`:''}`:'');
}
function renderDecision(c,call){
 let title='No reminder needed',detail='The balance is below the reminder threshold.';
 if(c.ledger?.length===0){title='Add the first invoice';detail='Once a bill is saved, Asha can follow up when the balance is above the reminder threshold.';}
 else if(c.status==='cleared'){title='All settled';detail='The balance is clear. No further reminders.';}
 else if(c.status==='suppressed'||c.contact_blocked){title='Calls stopped';detail='This customer will not receive further reminders.';}
 else if(c.status==='paused'){title='Needs assistance';detail=call?.next_step||'Resolve the customer’s payment issue before another reminder.';}
 else if(call&&!terminal(call.status)){title=outcomeLabel(call.status);detail='Asha is handling this customer’s call. You can keep working.';}
 else if(c.balance>c.threshold&&!c.phone){title='Customer contact needed';detail='Add their number to include them in a reminder run.';}
 else if(c.balance>c.threshold&&['scheduled','ready'].includes(c.job?.status)){title=c.ready_for_reminder?'Ready for the next run':'Next follow-up';detail=c.ready_for_reminder?'Asha will call this customer when you start reminders.':date(c.job.due_at)+' IST';}
 const canCall=c.status==='active'&&!c.contact_blocked&&!!c.phone&&c.balance>c.threshold;
 const openRun=['running','paused','stopping'].includes(state?.reminders?.run?.status);
 const activeCall=state?.customers?.some(account=>account.calls.some(x=>!terminal(x.status)));
 const blocked=busy||openRun||activeCall||state?.reminders?.local_only;
 updateHTML('#call-result',`<div><h2>${esc(title)}</h2><p>${esc(detail)}</p></div>${canCall?`<button class="button primary" id="call-customer" aria-label="Call ${esc(c.name)} now" ${blocked?'disabled':''}>Call now</button>`:''}`);
}
async function callCustomer(){
 if(busy)return;
 const cid=selected;
 if(!cid)return;
 busy=true;error('');render();
 try{
  // Reuse this customer's request if the response was lost. A later,
  // deliberate call gets a new ID only after this request is acknowledged.
  if(!customerCallRequests.has(cid))customerCallRequests.set(cid,{request_id:crypto.randomUUID(),customer_id:cid});
  await api('reminders/start',customerCallRequests.get(cid));
  customerCallRequests.delete(cid);
  await refresh(true);
 }catch(e){error(e.message);try{await refresh(true);}catch{}}
 finally{busy=false;render();}
}
async function startReminders(){
 if(busy)return;busy=true;error('');renderRun();
 try{
  const run=state.reminders?.run;
  if(run?.status==='paused')await api('reminders/'+encodeURIComponent(run.id)+'/resume',{});
  else{
   if(!runRequest)runRequest={request_id:crypto.randomUUID()};
   await api('reminders/start',runRequest);
  }
  runRequest=null;await refresh(true);
 }catch(e){error(e.message);try{await refresh(true);}catch{}}
 finally{busy=false;renderRun();}
}
async function importFile(file){
 if(busy||!file)return;
 if(!/\.(xlsx|xls|csv)$/i.test(file.name)){error('Choose an Excel (.xlsx or .xls) or CSV file.');return;}
 if(file.size>5*1024*1024){error('Choose a file smaller than 5 MB.');return;}
 busy=true;uploading=true;error('');renderRun();
 $('#choose-file').disabled=true;$('#upload-title').textContent='Importing…';$('#import-feedback').hidden=false;
 $('#import-status').textContent='Reading customers, bills and notes. No calls are being started.';
 try{
  const response=await fetch('/api/imports',{method:'POST',headers:{'X-Demo-Console':'1','X-Filename':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file});
  const report=await response.json();if(!response.ok)throw new Error(typeof report.detail==='string'?report.detail:'The file could not be loaded. Try saving it as Excel or CSV.');
  importReport=report;selected=report.customer_ids[0]||selected;selectedCall=null;await refresh(true);
 }catch(e){error(e.message);$('#upload-title').textContent='Import Excel';$('#import-status').textContent='The file was not loaded. Check the message and try again.';}
 finally{busy=false;uploading=false;$('#upload-title').textContent='Import Excel';$('#choose-file').disabled=false;$('#customer-file').value='';renderRun();if(importReport)renderImport();}
}
async function saveContact(){
 if(busy)return;busy=true;$('#save-contact').disabled=true;$('#contact-error').hidden=true;
 try{
  const phone=$('#contact-phone').value.trim();
  if(creatingCustomer){const added=await api('customers',{name:$('#contact-name').value.trim(),company:$('#contact-company').value.trim(),phone});selected=added.id;selectedCall=null;}
  else await api('customers/'+encodeURIComponent(selected)+'/contact',{phone});
  $('#contact-dialog').close();await refresh(true);
 }
 catch(e){$('#contact-error').textContent=e.message;$('#contact-error').hidden=false;}
 finally{busy=false;$('#save-contact').disabled=false;}
}
$('#customer-search').addEventListener('input',()=>render());
$('#choose-file').addEventListener('click',()=>$('#customer-file').click());
$('#customer-file').addEventListener('change',event=>importFile(event.target.files[0]));
for(const type of ['dragenter','dragover'])$('#choose-file').addEventListener(type,event=>{event.preventDefault();$('#choose-file').classList.add('drag-over');});
$('#choose-file').addEventListener('dragleave',()=>$('#choose-file').classList.remove('drag-over'));
document.addEventListener('dragover',event=>{if(event.dataTransfer?.types.includes('Files'))event.preventDefault();});
document.addEventListener('drop',event=>{
 if(!event.dataTransfer?.files.length)return;event.preventDefault();$('#choose-file').classList.remove('drag-over');
 if(event.dataTransfer.files.length!==1){error('Drop one customer file at a time.');return;}
 importFile(event.dataTransfer.files[0]);
});
$('#call-dialog').addEventListener('close',()=>$('#recording-player').pause());
$('#contact-form').addEventListener('submit',async e=>{e.preventDefault();await saveContact();});
document.addEventListener('keydown',()=>document.body.classList.add('keyboard'));
document.addEventListener('pointerdown',()=>document.body.classList.remove('keyboard'));
document.addEventListener('click',async event=>{
 const button=event.target.closest('button');if(!button)return;
 if(button.dataset.customer&&!busy){selected=button.dataset.customer;selectedCall=null;error('');render();}
 if(button.dataset.call){selectedCall=button.dataset.call;render();$('#conversation-details').open=false;$('#call-dialog').showModal();}
 if(button.dataset.close)$('#'+button.dataset.close).close();
 if(['edit-contact','add-customer'].includes(button.id)){creatingCustomer=button.id==='add-customer';$('#contact-title').textContent=creatingCustomer?'Add customer':'Customer contact';$('#save-contact').textContent=creatingCustomer?'Add customer':'Save contact';$('#contact-note').textContent=creatingCustomer?'Save their details once, then add an invoice. Asha will call only when you start reminders.':'Asha will use this number for the customer’s bill reminders. Leave it empty to remove it.';$('#new-customer-fields').hidden=!creatingCustomer;$('#contact-name').required=creatingCustomer;$('#contact-company').required=creatingCustomer;$('#contact-name').value='';$('#contact-company').value='';$('#contact-description').textContent=creatingCustomer?'Save their contact details, then add their invoice.':customer().name+' · '+customer().company;$('#contact-phone').value=creatingCustomer?'':customer().phone||'';$('#contact-error').hidden=true;$('#contact-dialog').showModal();}
 if(button.id==='start-reminders')await startReminders();
 if(button.id==='call-customer')await callCustomer();
 if(button.id==='pause-reminders'&&!busy){busy=true;try{const run=state.reminders.run;await api('reminders/'+encodeURIComponent(run.id)+'/'+(run.status==='paused'?'cancel':'pause'),{});await refresh(true);}catch(e){error(e.message);}finally{busy=false;renderRun();}}
 if(button.id==='check-setup'){button.disabled=true;$('#setup-status').textContent='Checking connection…';try{await api('setup/check',{});$('#setup-status').textContent='Agent and customer-context connection are ready.';}catch(e){$('#setup-status').textContent=e.message;}finally{button.disabled=false;}}
 if(button.id==='load-recording'){const id=focusedCall(customer()).id;button.disabled=true;try{const data=await api('calls/'+encodeURIComponent(id)+'/recording');if(focusedCall(customer())?.id!==id)return;$('#recording-player').src=data.url;$('#recording-player').hidden=false;$('#recording-status').textContent='Recording ready';}catch(e){$('#recording-status').textContent=e.message;}finally{button.disabled=false;}}
 if(button.id==='retry-brief'){button.disabled=true;try{await api('calls/'+encodeURIComponent(focusedCall(customer()).id)+'/extract',{});await refresh(true);}catch(e){error(e.message);}finally{button.disabled=false;}}
 if(['open-payment','open-invoice','open-first-invoice'].includes(button.id)){
  entryKind=button.id==='open-payment'?'payment':'invoice';const form=$('#entry-form');form.reset();form.elements.amount.value=entryKind==='payment'?customer().balance:'';
  $('#entry-title').textContent=entryKind==='payment'?'Record a reconciled payment':'Add an invoice';$('#entry-description').textContent=customer().name+' · '+customer().company;$('#save-entry').textContent='Save '+entryKind;$('#entry-error').hidden=true;$('#entry-dialog').showModal();
 }
 if(button.id==='close-entry')$('#entry-dialog').close();
});
$('#entry-form').addEventListener('submit',async event=>{event.preventDefault();const button=$('#save-entry'),form=event.currentTarget;button.disabled=true;try{await api(entryKind==='payment'?'payments':'invoices',{customer_id:selected,amount:form.elements.amount.value,reference:form.elements.reference.value.trim()});$('#entry-dialog').close();await refresh(true);}catch(e){$('#entry-error').textContent=e.message;$('#entry-error').hidden=false;}finally{button.disabled=false;}});
refresh().catch(e=>error('The workspace could not load. '+e.message));
function refreshDelay(){
 if(document.hidden)return 15000;
 const open=['running','paused','stopping'].includes(state?.reminders?.run?.status);
 const active=state?.customers?.some(c=>c.calls.some(x=>!terminal(x.status)));
 return open||active?1000:3000;
}
async function pollWorkspace(){
 try{if(!busy)await refresh();}catch(e){error('Connection to the server was lost. '+e.message);}
 finally{setTimeout(pollWorkspace,refreshDelay());}
}
setTimeout(pollWorkspace,3000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!busy)refresh().catch(e=>error(e.message));});
