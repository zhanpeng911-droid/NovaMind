/* Shared composer attachments. No model call is made until the user sends. */
(function () {
  'use strict';
  const icon = name => {
    const paths = {image:'<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 3-3 6 6"/>',chat:'<path d="M4 4h16v13H9l-5 4z"/><path d="M8 9h8M8 13h5"/>',book:'<path d="M4 4h16v16H7a3 3 0 0 0-3 2zM4 4v17M8 8h8M8 12h7"/>',calendar:'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4M17 3v4M3 10h18M8 14h2m4 0h2m-8 3h2"/>',translate:'<path d="M3 6h12M9 3v3M5 6c0 6 6 9 8 10M12 6c0 5-6 9-9 10m11 5 4-12 4 12m-7-4h6"/>',summary:'<rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 8h8M8 12h8M8 16h5"/>',settings:'<path d="m12 3 8 4v10l-8 4-8-4V7z"/><circle cx="12" cy="12" r="3"/>',window:'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M9 9v11"/>',spark:'<path d="m12 2 2.8 7.2L22 12l-7.2 2.8L12 22l-2.8-7.2L2 12l7.2-2.8z"/>',arrow:'<path d="M12 20V4m-6 6 6-6 6 6"/>'};
    paths.plus='<path d="M12 5v14M5 12h14"/>';
    paths.close='<path d="m6 6 12 12M6 18 18 6"/>';
    paths.copy='<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>';
    return '<svg class="nm-icon" viewBox="0 0 24 24" aria-hidden="true">'+(paths[name]||paths.chat)+'</svg>';
  };
  async function json(url, options) {
    const response = await fetch(url, options);
    let data; try { data = await response.json(); } catch (_) { throw new Error('服务未返回有效数据'); }
    if (!response.ok) throw new Error(data?.error?.message || (typeof data?.detail==='string' ? data.detail : '请求失败（HTTP '+response.status+'）'));
    return data;
  }
  function dialogForm({title,description='',fields=[],submit='保存',danger=false,onSubmit,preview}) {
    return new Promise(resolve=>{
      const dialog=document.createElement('dialog');dialog.className='nm-dialog';
      const form=document.createElement('form'),heading=document.createElement('h2'),intro=document.createElement('p');
      heading.id='dialog-'+crypto.randomUUID();heading.textContent=title;dialog.setAttribute('aria-labelledby',heading.id);intro.textContent=description;form.append(heading,intro);
      const controls={};
      for(const field of fields){const label=document.createElement('label'),caption=document.createElement('span');caption.textContent=field.label;
        const control=document.createElement(field.type==='textarea'?'textarea':field.type==='select'?'select':'input');
        if(control.tagName==='INPUT')control.type=field.type||'text';
        if(field.options)for(const [value,text] of Object.entries(field.options)){const option=document.createElement('option');option.value=value;option.textContent=text;control.append(option);}
        control.value=field.value??'';control.name=field.name;control.required=!!field.required;
        for(const attr of ['min','max','step','maxLength','placeholder'])if(field[attr]!==undefined)control[attr]=field[attr];
        label.append(caption,control);form.append(label);controls[field.name]=control;
      }
      const values=()=>Object.fromEntries(Object.entries(controls).map(([key,node])=>[key,node.value]));
      if(typeof preview==='function'){
        const hint=document.createElement('p');hint.className='dialog-preview';hint.setAttribute('role','status');
        const update=()=>{hint.textContent=preview(values());};
        form.append(hint);form.addEventListener('input',update);form.addEventListener('change',update);update();
      }
      const error=document.createElement('div');error.className='dialog-error';error.setAttribute('role','alert');
      const actions=document.createElement('div');actions.className='dialog-actions';const cancel=document.createElement('button'),save=document.createElement('button');
      cancel.type='button';cancel.textContent='取消';save.type='submit';save.className=danger?'danger primary':'primary';save.textContent=submit;actions.append(cancel,save);form.append(error,actions);dialog.append(form);document.body.append(dialog);
      let finished=false,saving=false;function finish(value){if(finished)return;finished=true;dialog.close();dialog.remove();resolve(value);}
      cancel.onclick=()=>{if(!saving)finish(null);};dialog.oncancel=e=>{e.preventDefault();if(!saving)finish(null);};dialog.addEventListener('keydown',e=>{if(e.key==='Escape')e.stopPropagation();});
      dialog.onclick=e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)if(!saving)finish(null);}};
      form.onsubmit=async e=>{e.preventDefault();if(saving)return;saving=true;save.disabled=cancel.disabled=true;error.textContent='';const submitted=values();try{const result=onSubmit?await onSubmit(submitted):submitted;finish(result??submitted);}catch(err){error.textContent=err.message||'保存失败，请重试';}finally{saving=false;save.disabled=cancel.disabled=false;}};
      dialog.showModal();requestAnimationFrame(()=>Object.values(controls)[0]?.focus());
    });
  }
  function reasoningPicker(container) {
    const descriptions={
      auto:'寒暄优先快速，其他请求默认轻量思考。',
      quick:'优先快速响应；DeepSeek 适配会关闭思考，OpenAI 适配映射为 low。',
      low:'适合日常任务；请求适配器支持的轻量思考。',
      high:'适合复杂任务；请求适配器支持的深入思考。',
      max:'请求较高思考档位；OpenAI 适配与深入思考同为 high，不保证额外提升。',
    };
    const valid=value=>Object.hasOwn(descriptions,value)?value:'auto';
    const select=document.createElement('select');select.className='reasoning-select';select.setAttribute('aria-label','模型思考程度');
    for(const [value,label] of Object.entries({auto:'自动',quick:'快速',low:'轻量思考',high:'深入思考',max:'充分思考'})){const option=document.createElement('option');option.value=value;option.textContent=label;option.dataset.description=descriptions[value];select.append(option);}
    const describe=()=>{select.title=descriptions[select.value]+' 实际能力依供应商和模型而定；未适配的模型使用自身默认行为。';};
    select.value=valid(localStorage.getItem('novamind_reasoning'));describe();container.append(select);
    select.onchange=()=>{select.value=valid(select.value);describe();localStorage.setItem('novamind_reasoning',select.value);const node=select.closest('.nm-select')||select;if(!matchMedia('(prefers-reduced-motion:reduce)').matches)node.animate([{opacity:.5,transform:'translateY(3px)'},{opacity:1,transform:'translateY(0)'}],{duration:180});};
    addEventListener('storage',event=>{if(event.key==='novamind_reasoning'){select.value=valid(event.newValue);describe();}});
    return select;
  }
  function preview(id) {
    const dialog = document.createElement('dialog'); dialog.className='image-lightbox';
    const image=document.createElement('img'); image.src='/attachments/images/'+encodeURIComponent(id);
    const close=document.createElement('button'); close.textContent='关闭'; close.onclick=()=>dialog.close();
    dialog.append(image,close); document.body.append(dialog); dialog.onclose=()=>dialog.remove(); dialog.showModal();
  }
  function renderImages(parent, ids) {
    if (!ids?.length) return;
    const row=document.createElement('div'); row.className='message-images';
    ids.forEach(id=>{const img=document.createElement('img');img.src='/attachments/images/'+encodeURIComponent(id);img.alt='会话图片';img.tabIndex=0;img.onclick=()=>preview(id);img.onkeydown=e=>{if(e.key==='Enter')preview(id);};row.append(img);});parent.append(row);
  }
  class Images {
    constructor(input, container) {
      this.items=[];this.pending=0; this.input=input;
      this.row=document.createElement('div');this.row.className='image-drafts';
      this.error=document.createElement('div');this.error.className='image-error';this.error.setAttribute('role','status');
      container.prepend(this.row,this.error);
      this.button=document.createElement('button');this.button.type='button';this.button.className='image-upload';this.button.innerHTML=icon('image');this.button.title='添加图片，也可直接粘贴或拖入';this.button.setAttribute('aria-label','添加图片');
      this.file=document.createElement('input');this.file.type='file';this.file.accept='image/png,image/jpeg,image/webp';this.file.multiple=true;this.file.hidden=true;
      this.button.onclick=()=>this.file.click();this.file.onchange=()=>{this.add(this.file.files);this.file.value='';};container.append(this.file);
      input.addEventListener('paste',e=>{const files=[...(e.clipboardData?.files||[])].filter(f=>f.type.startsWith('image/'));if(files.length){e.preventDefault();const text=e.clipboardData.getData('text/plain');if(text)input.setRangeText(text,input.selectionStart,input.selectionEnd,'end');this.add(files);}});
      container.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault();});
      container.addEventListener('drop',e=>{if(e.dataTransfer?.files.length){e.preventDefault();this.add(e.dataTransfer.files);}});
    }
    async add(files) {
      if(this.input.readOnly){this.error.textContent='当前任务执行中，可以先停止后添加图片';return;}
      this.error.textContent='';const destination=this.items;
      for(const file of Array.from(files)) {
        if(destination.length+this.pending>=3){this.error.textContent='每条消息最多 3 张图片';break;}
        if(!['image/png','image/jpeg','image/webp'].includes(file.type)||file.size>10*1024*1024){this.error.textContent='支持 PNG/JPEG/WebP，每张不超过 10 MiB';continue;}
        this.pending++;this.paint();
        try{const item=await json('/attachments/images',{method:'POST',headers:{'Content-Type':file.type},body:file});destination.push(item.id);}catch(e){if(this.items===destination)this.error.textContent=e.message;}finally{this.pending--;this.paint();}
      }
    }
    paint(){this.row.replaceChildren();this.items.forEach((id,i)=>{const card=document.createElement('div');card.className='image-draft';const img=document.createElement('img');img.src='/attachments/images/'+id;img.alt='待发送图片';img.onclick=()=>preview(id);const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','移除图片');remove.onclick=()=>{this.items.splice(i,1);this.paint();};card.append(img,remove);this.row.append(card);});if(this.pending){const span=document.createElement('span');span.textContent='正在添加图片…';this.row.append(span);}}
    clear(){const ids=this.items.slice();this.items=[];this.paint();return ids;}
    restore(ids){this.items=ids;this.paint();}
  }
  const escaped=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function contextCards(parent, memories=[], schedules=[], openSchedule) {
    parent.querySelector('.personal-result')?.remove();
    const result=document.createElement('div');result.className='personal-result';
    for(const task of schedules){const card=document.createElement('article');card.className='schedule-card';const title=document.createElement('strong');title.textContent=task.description;const info=document.createElement('div');info.className='schedule-meta';info.textContent=(task.target_time||task.task_date)+(task.kind==='todo'?' · 待确认提醒时间':' · 提前 '+(task.remind_minutes||0)+' 分钟提醒')+' · '+(task.repeat||'单次');const open=document.createElement('button');open.textContent='查看 / 修改日程';open.onclick=()=>openSchedule?.(task.id);card.append(title,info,open);result.append(card);}
    if(memories.length){const details=document.createElement('details');details.className='memory-references';const label=document.createElement('summary');label.textContent='本轮使用的记忆 · '+memories.length;details.append(label);memories.forEach(m=>{const text=document.createElement('p');text.textContent=m.content||m.id;details.append(text);});result.append(details);}
    if(result.childNodes.length)parent.append(result);
  }
  async function frames(response, consume) {
    if(!response.ok){let data;try{data=await response.json();}catch(_){}throw new Error(data?.error?.message||'请求失败');}
    if(!response.body)throw new Error('连接未返回回答内容，请重试');
    const reader=response.body.getReader(), decoder=new TextDecoder();let buffer='',complete=false;
    const accept=frame=>{const data=frame.split(/\r?\n/).filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trimStart()).join('\n');if(data){let parsed;try{parsed=JSON.parse(data);}catch(_){throw new Error('回答数据格式异常，请重试');}consume(parsed);}};
    try{while(true){const {done,value}=await reader.read();buffer+=done?decoder.decode():decoder.decode(value,{stream:true});let match;while((match=/\r?\n\r?\n/.exec(buffer))){accept(buffer.slice(0,match.index));buffer=buffer.slice(match.index+match[0].length);}if(done){if(buffer.trim())accept(buffer);complete=true;break;}}}finally{if(!complete){try{await reader.cancel();}catch(_){}}reader.releaseLock();}
  }
  const clientId=crypto.randomUUID();
  function liveConversations(consume){
    const source=new EventSource('/workspace/events'),finished=new Set();
    const finish=event=>{if(finished.has(event.turn_id))return;consume(event);finished.add(event.turn_id);if(finished.size>64)finished.delete(finished.values().next().value);};
    source.onmessage=event=>{try{
      const data=JSON.parse(event.data);
      if(data.kind==='snapshot'){
        const turns=data.turns.filter(turn=>!turn.finish||!finished.has(turn.turn_id));
        consume({kind:'snapshot',turns:turns.map(turn=>turn.turn_id)});
        turns.forEach(turn=>{
          if(turn.client_id===clientId)return;
          consume(turn);
          (turn.frames||[]).forEach(frame=>consume({...turn,kind:'frame',frame}));
          if(turn.finish)finish({...turn,...turn.finish,kind:'finish'});
        });
      }else if(data.client_id!==clientId){
        if(data.kind==='finish')finish(data);else if(!finished.has(data.turn_id))consume(data);
      }
    }catch(error){console.warn('Conversation sync unavailable',error.name);}};
    addEventListener('pagehide',()=>source.close(),{once:true});return source;
  }
  window.NovaMindUI={icon,json,preview,renderImages,Images,escaped,frames,contextCards,dialogForm,reasoningPicker,clientId,liveConversations};
})();
