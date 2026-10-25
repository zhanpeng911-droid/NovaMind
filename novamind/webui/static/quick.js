(function(){
  'use strict';const {json,frames,renderImages,Images}=NovaMindUI;
  const by=id=>document.getElementById(id),input=by('input'),log=by('messages'),mode=by('action');
  const attachments=new Images(input,by('composer'));by('image-button').append(attachments.button);
  const reasoning=NovaMindUI.reasoningPicker(by('quick-reasoning'));
  by('personal-methods').onclick=()=>window.pywebview?.api?.open_workspace('/?view=skills');
  document.querySelectorAll('[data-icon]').forEach(n=>n.innerHTML=NovaMindUI.icon(n.dataset.icon));
  let thread=localStorage.getItem('novamind_quick_thread')||null,busy=false,controller=null,lastAnswer='',lastRender='',pendingImport=null;
  const remoteResults=new Map();
  const api=()=>window.pywebview?.api;
  function status(message){by('state').textContent=message;}
  function showEmpty(){log.innerHTML='<div class="quick-empty"><span class="quick-eyebrow">YOUR EVERYDAY COMPANION</span><h2>有什么想一起完成的？</h2><p>问一个问题，或粘贴一张图片。</p></div>';}
  function setBusy(value){busy=value;by('send').disabled=value;by('send').hidden=value;by('stop').disabled=!value;by('stop').hidden=!value;mode.disabled=value;input.readOnly=value;attachments.button.disabled=value;reasoning.disabled=value;by('new-chat').disabled=value;by('escalate').disabled=value;['memory-mode','scope','target'].forEach(id=>by(id).disabled=value);}
  function renderAnswer(body,text,note=''){body.classList.add('markdown-body');body.innerHTML=NovaMindMarkdown.render(text);if(note){const warning=document.createElement('p');warning.className='quick-error-note';warning.textContent=note;body.append(warning);}}
  function message(role,text,images=[],memories=[],schedules=[]){log.querySelector('.quick-empty')?.remove();const node=document.createElement('div');node.className='quick-message '+role;renderImages(node,images);const body=document.createElement('div');if(role==='ai')renderAnswer(body,text);else body.textContent=text;node.append(body);NovaMindUI.contextCards(node,memories,schedules,()=>api()?.open_schedule());log.append(node);log.scrollTop=log.scrollHeight;return body;}
  async function history(){if(!thread||pendingImport)return;const requested=thread;try{const data=await json('/history/'+encodeURIComponent(thread)+'?limit=100');if(busy||requested!==thread||pendingImport)return;const result=remoteResults.get(thread),snapshot=JSON.stringify([data.messages,result]);if(snapshot===lastRender)return;log.replaceChildren();if(!data.messages.length&&!result)showEmpty();data.messages.forEach(m=>message(m.role==='user'?'user':'ai',m.content,m.images,m.memories,m.schedules));if(result){message('user',result.message,result.images).parentElement.dataset.liveTurn=result.turnId;const body=message('ai',result.text,[],result.memories,result.schedules);body.parentElement.dataset.liveTurn=result.turnId;renderAnswer(body,result.text,result.note);status(result.state);}lastAnswer=result?.text||[...data.messages].reverse().find(m=>m.role==='ai')?.content||'';by('copy').hidden=!lastAnswer;lastRender=snapshot;}catch(e){status(e.message);}}
  function select(action){if(busy)return;mode.value=action;by('target-field').hidden=action!=='translate';input.placeholder=action==='record'?'例如：明天下午三点提醒我交报告…':'说说你的想法，或粘贴一张图片…';if(action==='translate')document.querySelector('.quick-options').open=true;input.focus();}
  window.novaMindSelectAction=select;mode.onchange=()=>select(mode.value);
  by('memory-mode').onchange=()=>{const value=by('memory-mode').value;document.querySelector('.quick-options').classList.toggle('is-private',value!=='normal');document.querySelector('.quick-options summary').title=value==='private'?'私密对话：不读取也不写入个人记忆':value==='no_record'?'本次不记录个人记忆':'记忆与语言选项';status(value==='private'?'私密对话 · 不读不写个人记忆':value==='no_record'?'本次不记录个人记忆':'已启用个人记忆');};
  window.novaMindOpen=async(requestedThread)=>{try{if(busy){input.focus();return;}const current=typeof requestedThread==='string'?requestedThread:localStorage.getItem('novamind_last_thread');if(current&&current!==thread){if(!input.value.trim()&&!attachments.items.length&&!pendingImport){thread=current;localStorage.setItem('novamind_quick_thread',thread);lastRender='';}else if(typeof requestedThread==='string'){status('已保留小窗当前草稿；发送或新建会话后可切换');}}const selected=await api()?.get_selection();if(selected){if(input.value.trim()||attachments.items.length){if(!await NovaMindUI.dialogForm({title:'导入新的选区？',description:'将替换当前文字草稿，已添加的图片保留。',submit:'导入选区'}))return;}input.value=selected;sessionStorage.setItem('novamind_quick_draft',input.value);}await history();input.focus();}catch(e){status(e.message);}};
  async function importQuick(){if(!pendingImport)return;const data=await json('/quick/escalate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pendingImport)});thread=data.thread_id;localStorage.setItem('novamind_quick_thread',thread);localStorage.setItem('novamind_last_thread',thread);pendingImport=null;}
  async function run(){
    const learningStarted=Date.now();
    const text=input.value.trim(),images=attachments.items.slice();if(busy||attachments.pending||(!text&&!images.length))return;
    by('hint').textContent=text?text.slice(0,18):'图片对话';by('hint').title=text;
    setBusy(true);status('处理中…');controller=new AbortController();let acc='',errorText='',done=false,memories=[],schedules=[];
    try{
      await importQuick();message('user',text,images);const body=message('ai','');const action=mode.value;
      const payload={message:action==='record'?'请把以下明确安排保存到我的日程，时间不明确时先询问：\n'+text:text,thread_id:thread,images,memory_mode:by('memory-mode').value,scope:by('scope').value||'global',reasoning:reasoning.value,client_id:NovaMindUI.clientId,source:'quick'};
      const quick=!['chat','record'].includes(action);const response=await fetch(quick?'/quick/actions':'/chat',{method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,body:JSON.stringify(quick?{action,text:text||'请处理图片中的内容',images,target_language:by('target').value,memory_mode:payload.memory_mode,scope:payload.scope,reasoning:reasoning.value}:payload)});
      await frames(response,data=>{if(data.type==='thread'){thread=data.thread_id;localStorage.setItem('novamind_quick_thread',thread);}if(data.type==='text'){acc+=data.content;renderAnswer(body,acc,errorText);log.scrollTop=log.scrollHeight;}if(data.type==='tool')status('正在使用 '+data.name);if(data.type==='memories'){memories=data.items||[];NovaMindUI.contextCards(body.parentElement,memories,schedules,()=>api()?.open_schedule());}if(data.type==='schedules'){schedules=data.items||[];NovaMindUI.contextCards(body.parentElement,memories,schedules,()=>api()?.open_schedule());}if(data.type==='error'){errorText=data.message;renderAnswer(body,acc,errorText);}if(data.type==='done')done=true;});
      if(errorText)throw new Error(errorText);if(!done)throw new Error('连接提前结束，请检查后重试');lastAnswer=acc;by('copy').hidden=!lastAnswer;
      if(quick){pendingImport={text:text||'请处理图片中的内容',images,answer:acc,session_id:thread,request_id:crypto.randomUUID()};await importQuick();}
      remoteResults.delete(thread);if(thread)localStorage.setItem('novamind_last_thread',thread);
      input.value='';sessionStorage.removeItem('novamind_quick_draft');attachments.clear();status('已完成');
    }catch(e){status(e.name==='AbortError'?'已停止，草稿已保留':e.message);message('error',e.name==='AbortError'?'已停止':e.message);}
    finally{controller=null;setBusy(false);if(!errorText&&mode.value==='chat')window.NovaMindLearning?.usage(thread,[...log.querySelectorAll('.quick-message.ai')].pop(),learningStarted);}
  }
  by('send').onclick=run;by('stop').onclick=()=>{if(controller)controller.abort();else if(remoteTurn)json('/workspace/turns/'+encodeURIComponent(remoteTurn)+'/cancel',{method:'POST'}).catch(error=>status(error.message));};by('close').onclick=()=>api()?.hide_panel();
  by('copy').onclick=async()=>{if(!lastAnswer)return;try{await navigator.clipboard.writeText(lastAnswer);status('已复制');}catch(_){status('复制失败，可选择回答文字复制');}};
  by('new-chat').onclick=async()=>{try{await importQuick();}catch(e){status(e.message);return;}thread=null;pendingImport=null;localStorage.removeItem('novamind_quick_thread');localStorage.removeItem('novamind_last_thread');sessionStorage.removeItem('novamind_quick_draft');log.innerHTML='<div class="quick-empty"><span class="quick-eyebrow">A FRESH START</span><h2>我们从这里开始。</h2><p>一个问题，一张图片，或一个待办。</p></div>';input.value='';attachments.clear();lastAnswer='';by('copy').hidden=true;lastRender='';status('新会话 · Enter 发送');input.focus();};
  by('escalate').onclick=async()=>{try{await importQuick();await api()?.open_workspace(null,thread);}catch(e){status(e.message);}};
  input.oninput=()=>{sessionStorage.setItem('novamind_quick_draft',input.value);};input.value=sessionStorage.getItem('novamind_quick_draft')||'';
  input.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();run();}};
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!e.defaultPrevented)api()?.hide_panel();});
  window.addEventListener('focus',()=>{if(!busy)history();});window.addEventListener('pywebviewready',window.novaMindOpen);
  let remoteTurn=null,remoteBody=null,remoteText='',remoteMemories=[],remoteSchedules=[],remoteError='',remoteTruncated=false,remoteMessage='',remoteImages=[];
  function remoteNote(ended=false,cancelled=false){return [remoteError|| (cancelled?'已停止':''),remoteTruncated?'同步内容不完整，较早内容已省略。'+(ended?'':'完成后会从历史校正。'):''].filter(Boolean).join(' · ');}
  function rememberRemote(state,note){
    const text=remoteText.slice(-32768),clipped=text.length!==remoteText.length;
    const result={turnId:remoteTurn,message:remoteMessage.slice(-16384),images:remoteImages.slice(0,3),text,note:note+(clipped?' · 同步内容不完整，较早内容已省略。':''),state,memories:remoteMemories,schedules:remoteSchedules};
    if(JSON.stringify(result).length>65536){result.memories=[];result.schedules=[];}
    remoteResults.delete(thread);remoteResults.set(thread,result);if(remoteResults.size>8)remoteResults.delete(remoteResults.keys().next().value);
  }
  NovaMindUI.liveConversations(event=>{
    if(event.kind==='snapshot'){if(remoteTurn&&!event.turns.includes(remoteTurn)){remoteTurn=null;setBusy(false);status('同步已结束；本轮结束状态未确认，正在核对历史');history();}return;}
    if(event.kind==='history'){if(!busy&&event.thread_id===thread){lastRender='';history();}return;}
    if(event.kind==='start'){
      if(busy&&!remoteTurn)return;
      if(event.thread_id!==thread&&(thread||input.value.trim()||attachments.items.length))return;
      if(remoteTurn===event.turn_id&&!event.frames)return;
      if(event.frames)log.querySelectorAll('[data-live-turn="'+event.turn_id+'"]').forEach(node=>node.remove());
      thread=event.thread_id;localStorage.setItem('novamind_quick_thread',thread);remoteResults.delete(thread);remoteTurn=event.turn_id;remoteText='';remoteMemories=[];remoteSchedules=[];remoteError='';remoteTruncated=!!event.truncated;remoteMessage=event.message||'';remoteImages=event.images||[];
      message('user',remoteMessage,remoteImages).parentElement.dataset.liveTurn=remoteTurn;remoteBody=message('ai','');remoteBody.parentElement.dataset.liveTurn=remoteTurn;renderAnswer(remoteBody,'',remoteNote());setBusy(true);status('与工作台同步 · 正在处理…');
    }else if(event.turn_id===remoteTurn&&event.kind==='frame'){
      const frame=event.frame;if(frame.type==='text'){remoteText+=frame.content;renderAnswer(remoteBody,remoteText,remoteNote());log.scrollTop=log.scrollHeight;}
      if(frame.type==='tool')status('正在使用 '+frame.name);
      if(frame.type==='memories')remoteMemories=frame.items||[];
      if(frame.type==='schedules')remoteSchedules=frame.items||[];
      if(frame.type==='error'){remoteError=frame.message||'回答未能完成，请重试';renderAnswer(remoteBody,remoteText,remoteNote());status(remoteError);}
      NovaMindUI.contextCards(remoteBody.parentElement,remoteMemories,remoteSchedules,()=>api()?.open_schedule());
    }else if(event.turn_id===remoteTurn&&event.kind==='finish'){
      const failed=!!event.error||!!remoteError;if(failed&&!remoteError)remoteError='回答未能完成，请重试';
      const state=failed?remoteError:event.cancelled?'已停止':'已完成',note=remoteNote(true,!!event.cancelled);
      renderAnswer(remoteBody,remoteText,note);lastAnswer=remoteText;by('copy').hidden=!lastAnswer;
      if(event.cancelled||failed)rememberRemote(state,note);else remoteResults.delete(thread);
      remoteTurn=null;setBusy(false);status(state);lastRender='';if(!event.cancelled&&!failed)history();
    }
  });
  setBusy(false);history();
})();
