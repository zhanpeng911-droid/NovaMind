(function () {
  'use strict';
  const {json,escaped:e,icon}=NovaMindUI;
  let renderToken=0;
  let scheduleSnapshot='',scheduleFilter='all',pollingSchedules=false;
  const presets={default:'NovaMind 默认',concise:'简洁直接',detailed:'详细讲解',warm:'温和自然',formal:'正式专业',custom:'自定义'};
  const select=(id,values,value)=>'<select id="'+id+'">'+Object.entries(values).map(([v,label])=>'<option value="'+e(v)+'"'+(String(value)===v?' selected':'')+'>'+e(label)+'</option>').join('')+'</select>';
  const check=(id,value)=>'<input id="'+id+'" type="checkbox"'+(value?' checked':'')+'>';
  const field=(name,control)=>'<label><span>'+name+'</span>'+control+'</label>';
  const feedback=message=>{const node=document.getElementById('personal-feedback');if(node)node.textContent=message;};
  function scheduleCard(task) {
    const description=String(task.description||''),unread=task.deliveries?.some(d=>!d.read);
    // No invented priority field: only explicit urgency words influence size.
    const important=/重要|紧急|优先|关键|截止|urgent|critical|high priority/i.test(description);
    const length=Array.from(description).reduce((n,c)=>n+(/[^\x00-\xff]/.test(c)?1:0.55),0);
    let span=length<=18?3:length<=45?6:length<=100?9:12;
    if(important)span=Math.max(span,9);
    const time=task.target_time||task.task_date||'待安排';
    const repeat={hourly:'每小时',daily:'每天',weekly:'每周',monthly:'每月'}[task.repeat]||'单次';
    const state={pending:'待处理',notified:'已提醒',completed:'已完成',cancelled:'已取消'}[task.status||'pending']||'待处理';
    const button=(action,label,danger='')=>'<button class="'+danger+'" data-task="'+e(task.id)+'" data-action="'+action+'">'+label+'</button>';
    return '<article id="task-'+e(task.id)+'" class="schedule-card'+(unread?' schedule-unread':'')+(important?' schedule-important':'')+'" data-span="'+span+'" style="--schedule-span:'+span+'">'
      +'<div class="schedule-heading"><div><h3>'+e(description)+'</h3><div class="schedule-time">'+e(time)+'</div></div>'
      +'<details class="nm-disclosure"><summary aria-label="日程操作：'+e(description)+'">⋯</summary><div class="nm-popover nm-menu">'
      +button('completed','标记完成')+button('edit','编辑日程')+button('read','标为已读')+button('cancelled','取消提醒')+button('delete','删除日程','danger')+'</div></details></div>'
      +'<div class="schedule-meta"><span>'+repeat+'</span><span>'+(task.kind==='todo'?'待办 · 尚无提醒时刻':'提前 '+e(task.remind_minutes||0)+' 分钟')+'</span><span class="schedule-state">'+state+'</span>'
      +(task.deliveries?.some(d=>d.missed&&!d.read)?'<span>有错过的提醒</span>':'')+'</div></article>';
  }
  function reminderPreview(values) {
    if(!values.time)return '将保存为待办，不设置定时提醒。';
    const lead=Number(values.remind),date=String(values.date||''),time=String(values.time||'');
    if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||!/^\d{2}:\d{2}$/.test(time)
      ||!Number.isInteger(lead)||lead<0||lead>525600)return '请填写有效的日期、事项时间与提前分钟数。';
    const at=new Date(date+'T'+time+':00'),parts=date.split('-').map(Number);
    if(!Number.isFinite(at.getTime())||at.getFullYear()!==parts[0]||at.getMonth()+1!==parts[1]
      ||at.getDate()!==parts[2]||at.getHours()!==Number(time.slice(0,2))||at.getMinutes()!==Number(time.slice(3)))
      return '请填写本机时区内有效的事项日期与时间。';
    const remindAt=new Date(at.getTime()-lead*60000);
    const formatted=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',
      hour:'2-digit',minute:'2-digit',hour12:false,timeZoneName:'short'}).format(remindAt);
    return '预计于 '+formatted+' 提醒（本机时区，程序运行期间有效）。';
  }
  async function editTask(task) {
    const editing=!!task, date=(task?.target_time||task?.task_date||new Date().toLocaleDateString('sv-SE')).slice(0,10);
    return NovaMindUI.dialogForm({title:editing?'编辑日程':'记录一个安排',description:'填写日期与时间；不填时间会保存为待办，不会承诺定时提醒。',submit:editing?'保存修改':'加入日程',fields:[
      {name:'description',label:'要做什么',type:'textarea',required:true,maxLength:2000,value:task?.description||'',placeholder:'例如：准备明天下午的项目会议'},
      {name:'date',label:'日期',type:'date',required:true,value:date},
      {name:'time',label:'事项时间（可选）',type:'time',value:(task?.target_time||'').slice(11,16)},
      {name:'remind',label:'提前提醒（分钟）',type:'number',min:0,max:525600,value:task?.remind_minutes||0},
      ...(!editing?[{name:'repeat',label:'重复',type:'select',value:'',options:{'':'单次',hourly:'每小时',daily:'每天',weekly:'每周',monthly:'每月'}}]:[])],
      preview:reminderPreview,
      onSubmit:async values=>{if(!values.time&&(Number(values.remind)||values.repeat))throw new Error('请先填写事项时间，再设置提前提醒或重复。');
        const body={description:values.description.trim(),target_time:values.time?values.date+' '+values.time+':00':'',task_date:values.date,remind_minutes:Number(values.remind)};
        if(!editing){body.repeat=values.repeat||null;body.source_id=crypto.randomUUID();}
        return json('/personal/schedules'+(editing?'/'+encodeURIComponent(task.id):''),{method:editing?'PATCH':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      }});
  }
  async function render(view) {
    const token=++renderToken,panel=document.getElementById('panel');
    panel.innerHTML='<div class="panel-loading">正在加载…</div>';
    try {
      if(view==='settings'){
        const data=await json('/personal/settings');if(token!==renderToken||!document.querySelector('[data-nav="settings"].active'))return;settingsPage(panel,data);window.NovaMindLearning?.mountSettings(panel);
      }else{const data=await json('/personal/schedules');if(token!==renderToken||!document.querySelector('[data-nav="schedule"].active'))return;schedulePage(panel,data.tasks);}
    }catch(error){if(token===renderToken&&document.querySelector('[data-nav="'+view+'"].active'))panel.innerHTML='<div class="panel-error">'+e(error.message)+'</div>';}
  }
  function settingsPage(panel,data) {
    const d=data.desktop,s=data.style;
    panel.innerHTML='<div class="panel-header">个人设置</div><div class="panel-sub">所有窗口共用一个工作区，设置保存后同步生效。</div><div class="personal-form">'
      +'<section class="personal-card"><div class="settings-heading"><h3>对话与表达</h3><details class="nm-disclosure"><summary aria-label="表达偏好更多选项">⋯</summary><div class="nm-popover nm-menu"><button id="reset-style">恢复默认表达</button></div></details></div><p>默认延续 NovaMind 的自然表达。当前对话中的明确要求优先。</p>'
      +field('说话风格',select('style-preset',presets,s.preset))+'<textarea id="style-custom" rows="3" placeholder="例如：先给结论，少用术语…">'+e(s.custom)+'</textarea>'
      +'<div class="personal-actions"><button id="save-style" class="primary">保存偏好</button></div></section>'
      +'<section class="personal-card"><div class="settings-heading"><h3>悬浮球与桌面</h3><details class="nm-disclosure"><summary aria-label="悬浮球更多选项">⋯</summary><div class="nm-popover nm-menu"><button id="reset-position">找回悬浮球 / 重置位置</button><button id="reset-desktop">恢复默认外观</button></div></details></div><div class="bubble-preview"><div id="preview-ball"></div><button id="icon-upload">更换图标</button><input type="file" id="icon-file" accept="image/png,image/jpeg,image/webp" hidden></div>'
      +field('显示悬浮球',check('ball-visible',d.bubble_visible))+'<div class="settings-grid">'
      +field('球体大小',select('ball-size',{'40':'紧凑 · 40','48':'默认 · 48','56':'大号 · 56'},d.size))
      +field('外观',select('ball-theme',{follow:'跟随工作台',light:'浅色',dark:'深色'},d.theme))
      +field('强调色',select('ball-accent',{blue:'浅蓝',slate:'灰蓝',green:'青绿'},d.accent))
      +field('图标',select('ball-icon',{brand:'NovaMind 标志',spark:'简约星形',...(d.icon!=='brand'&&d.icon!=='spark'?{[d.icon]:'我的图标'}:{})},d.icon))
      +field('自定义图标适配',select('ball-fit',{contain:'完整显示',cover:'居中裁切'},d.icon_fit))
      +field('透明度','<input id="ball-opacity" type="range" min="0.35" max="1" step="0.05" value="'+d.opacity+'">')
      +'</div><details class="settings-advanced"><summary>行为与启动</summary>'
      +field('启动时显示球',check('ball-startup',d.startup_bubble))+field('启动时打开工作台',check('workspace-open',d.open_workspace))+field('开机自启（仅打包程序）',check('desktop-autostart',d.autostart))
      +field('贴边吸附',check('ball-snap',d.snap))+field('动效',check('ball-motion',d.motion))+field('任务状态提示',check('ball-status',d.status))
      +field('全局热键','<input id="ball-hotkey" value="'+e(d.hotkey)+'" placeholder="Ctrl+Shift+Space">')
      +'<p id="desktop-status">'+e(data.desktop_status?.hotkey_error||'应用须在统一桌面模式运行，热键注册结果在此显示。')+'</p>'
      +'</details><div class="personal-actions"><button class="primary" id="save-desktop">应用并保存</button></div></section>'
      +'<section class="personal-card"><h3>记忆与日程</h3><p>沿用原有五层记忆内核。记忆中心不只管理偏好，也管理项目事实、任务上下文与经验；自动抽取先生成候选，确认后才用于跨会话回答。日程仍沿用原存储和提醒机制。</p><div class="personal-actions"><button id="memory-link">打开记忆中心</button><button id="schedule-link">打开日程</button></div></section>'
      +'<div id="personal-feedback" class="personal-feedback" role="status"></div></div>';
    const get=id=>document.getElementById(id), value=id=>get(id).value, checked=id=>get(id).checked;
    const readDesktop=()=>({bubble_visible:checked('ball-visible'),startup_bubble:checked('ball-startup'),autostart:checked('desktop-autostart'),open_workspace:checked('workspace-open'),icon_fit:value('ball-fit'),
      size:Number(value('ball-size')),theme:value('ball-theme'),accent:value('ball-accent'),icon:value('ball-icon'),
      opacity:Number(value('ball-opacity')),snap:checked('ball-snap'),motion:checked('ball-motion'),status:checked('ball-status'),hotkey:value('ball-hotkey')});
    const paint=()=>{const draft=readDesktop(),ball=get('preview-ball'),dark=draft.theme==='dark'||draft.theme==='follow'&&document.documentElement.dataset.theme==='dark',accent={blue:'#527cc8',slate:'#71829a',green:'#449a8c'}[draft.accent];ball.style.cssText='width:'+draft.size+'px;height:'+draft.size+'px;border-radius:50%;display:grid;place-items:center;border:1px solid '+accent+'60;background:'+(dark?'#162237':'#fff')+';color:'+(dark?'#ecf2ff':'#14213a')+';box-shadow:0 4px 14px '+accent+'25;opacity:'+draft.opacity;ball.replaceChildren();if(draft.icon==='spark'){ball.innerHTML=icon('spark');}else{const image=document.createElement('img');image.src=draft.icon==='brand'?'nova-mark.svg':'/attachments/images/'+draft.icon;image.style.width=image.style.height='28px';image.style.objectFit=draft.icon_fit;image.style.borderRadius=draft.icon_fit==='cover'?'50%':'0';image.style.filter=draft.icon==='brand'&&dark?'invert(1)':'';ball.append(image);}};
    panel.querySelectorAll('input,select').forEach(node=>node.addEventListener('input',paint));paint();
    const save=async(section,body)=>{try{await json('/personal/settings/'+section,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});feedback('已保存，下一次回答或当前悬浮球将使用新设置。');return true;}catch(err){feedback(err.message);return false;}};
    get('save-style').onclick=()=>save('style',{preset:value('style-preset'),custom:value('style-custom')});
    get('reset-style').onclick=()=>{get('style-preset').value='default';get('style-custom').value='';save('style',{preset:'default',custom:''});};
    get('save-desktop').onclick=()=>save('desktop',readDesktop());
    get('reset-position').onclick=async()=>{get('ball-visible').checked=true;await save('desktop',{bubble_x:null,bubble_y:null,bubble_visible:true});};
    get('reset-desktop').onclick=async()=>{if(await save('desktop',{size:48,opacity:1,theme:'follow',accent:'blue',icon:'brand',icon_fit:'contain',snap:true,motion:true,status:true}))render('settings');};
    get('icon-upload').onclick=()=>get('icon-file').click();get('icon-file').onchange=async()=>{const file=get('icon-file').files[0];if(!file)return;try{const image=await json('/attachments/images',{method:'POST',headers:{'Content-Type':file.type},body:file});const option=document.createElement('option');option.value=image.id;option.textContent='我的图标';get('ball-icon').append(option);get('ball-icon').value=image.id;paint();feedback('图标已预览，点击应用保存。');}catch(err){feedback(err.message);}};
    get('memory-link').onclick=()=>document.querySelector('[data-nav="memory"]').click();get('schedule-link').onclick=()=>document.querySelector('[data-nav="schedule"]').click();
  }
  function schedulePage(panel,tasks) {
    scheduleSnapshot=JSON.stringify(tasks);
    panel.innerHTML='<div class="panel-header">日程安排</div><div class="panel-sub">也可以直接说：“明天下午三点提醒我交报告”。当前时间以本机时区为准。</div><div class="panel-sub" id="notification-state" role="status">系统通知可能被专注模式隐藏，未读提醒始终保留在这里。</div>'
      +'<div class="personal-actions"><button id="add-schedule" class="primary">新增日程</button><button id="refresh-schedules">刷新</button>'+select('schedule-filter',{all:'全部事项',upcoming:'即将到来',today:'今天',unread:'未读提醒'},'all')+'</div><div class="schedule-board"><div id="schedule-list"></div></div><div class="personal-feedback" id="personal-feedback" role="status"></div>';
    const paint=()=>{const filter=document.getElementById('schedule-filter').value;const today=new Date().toLocaleDateString('sv-SE');
      document.getElementById('schedule-list').innerHTML=tasks.filter(t=>filter==='all'||filter==='unread'&&t.deliveries?.some(d=>!d.read)||filter==='today'&&(t.target_time||t.task_date)?.startsWith(today)||filter==='upcoming'&&(t.status||'pending')==='pending').map(scheduleCard).join('')||'<div class="panel-empty">暂无对应日程</div>';};
    document.getElementById('schedule-filter').value=scheduleFilter;paint();document.getElementById('schedule-filter').onchange=()=>{scheduleFilter=document.getElementById('schedule-filter').value;paint();};document.getElementById('refresh-schedules').onclick=()=>render('schedule');
    document.getElementById('add-schedule').onclick=async()=>{if(await editTask())render('schedule');};
    const selected=new URLSearchParams(location.search).get('task');if(selected){const card=document.getElementById('task-'+selected);if(card){card.scrollIntoView({block:'center'});card.classList.add('schedule-unread');}}
    panel.onclick=async event=>{const button=event.target.closest('[data-task]');if(!button)return;const id=button.dataset.task,action=button.dataset.action;let body={status:action};
      if(action==='edit'){if(await editTask(tasks.find(t=>t.id===id)))render('schedule');return;}
      if(action==='delete'||action==='cancelled'){const confirmed=await NovaMindUI.dialogForm({title:action==='delete'?'删除这项日程？':'取消这项提醒？',description:action==='delete'?'将移除日程和它的提醒记录，不会删除聊天或其他事项。':'日程会保留，之后不再提醒。',submit:action==='delete'?'删除日程':'取消提醒',danger:action==='delete'});if(!confirmed)return;}
      if(action==='delete'){try{await json('/personal/schedules/'+encodeURIComponent(id),{method:'DELETE'});render('schedule');}catch(err){feedback(err.message);}return;}
      try{await json('/personal/schedules/'+encodeURIComponent(id)+(action==='read'?'/read':''),{method:action==='read'?'POST':'PATCH',headers:{'Content-Type':'application/json'},...(action==='read'?{}:{body:JSON.stringify(body)})});render('schedule');}catch(err){feedback(err.message);}};
  }
  window.NovaMindPersonal={render};
  window.addEventListener('novamind-settings',async()=>{if(pollingSchedules||!document.querySelector('[data-nav="schedule"].active'))return;pollingSchedules=true;try{const data=await json('/personal/schedules');if(JSON.stringify(data.tasks)!==scheduleSnapshot&&document.querySelector('[data-nav="schedule"].active'))schedulePage(document.getElementById('panel'),data.tasks);}catch(_){}finally{pollingSchedules=false;}});
  window.addEventListener('novamind-settings',event=>{const node=document.getElementById('desktop-status');if(node)node.textContent=event.detail.desktop_status?.hotkey_error||'桌面状态正常；程序运行期间支持提醒。';const notice=document.getElementById('notification-state');if(notice)notice.textContent=event.detail.desktop_status?.notification||'提醒会保留在日程页。';});
})();
