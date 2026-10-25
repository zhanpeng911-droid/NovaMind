/* Reviewed personal methods. Opening pages and polling never starts model jobs. */
(function () {
  'use strict';
  const {json, dialogForm} = NovaMindUI;
  const base = '/personal/learning';
  const labels = {not_run:'尚未验证',static_failed:'待完善',static_pass:'静态检查通过 · 行为未验证',behavior_pass:'列出的文本检查通过',behavior_fail:'文本检查未通过',behavior_partial:'部分验证'};
  let serial = 0, filter = 'all';
  const notify = error => window.NovaMindControls?.notify(error.message || String(error));
  const request = (path, method, body) => json(base + path, {method,headers:{'Content-Type':'application/json'},...(body ? {body:JSON.stringify(body)} : {})});
  function button(text, action, primary=false) {
    const node=document.createElement('button');node.type='button';node.textContent=text;if(primary)node.className='primary';
    node.onclick=async()=>{node.disabled=true;try{await action();}catch(error){notify(error);}finally{node.disabled=false;}};return node;
  }
  function card(title, description) {
    const node=document.createElement('article');node.className='learning-card';
    const heading=document.createElement('h3'),sub=document.createElement('p');heading.textContent=title;sub.textContent=description;node.append(heading,sub);return node;
  }
  async function mountSkills(panel) {
    const token=++serial;
    try {
      const [patterns,candidates,jobs,settings]=await Promise.all([json(base+'/patterns'),json(base+'/candidates'),json(base+'/jobs'),json(base+'/settings')]);
      if(token!==serial || panel.dataset.view!=='skills')return;
      panel.querySelector('.learning-section')?.remove();
      const section=document.createElement('section');section.className='learning-section';
      const intro=document.createElement('div');intro.className='learning-heading';
      intro.innerHTML='<div><span class="learning-eyebrow">GROW WITH YOU</span><h2>把熟悉的方法，留给下一次。</h2><p>从你做过的事情里整理技能，由你决定何时启用。</p></div>';
      intro.append(button('手动整理方法',()=>newCandidate(panel),true));section.append(intro);
      const tabs=document.createElement('div');tabs.className='learning-tabs';
      [['all','全部技能'],['personal','我的方法'],['pending','待整理']].forEach(([key,text])=>{
        const tab=button(text,()=>{filter=key;applyFilter();});tab.dataset.filter=key;tabs.append(tab);
      });section.append(tabs);
      const grid=document.createElement('div');grid.className='learning-grid';section.append(grid);
      const info=card(settings.enabled?'正在本地发现可复用方法':'本地观察尚未开启','观察不额外调用模型；整理和评估前会单独确认预算。');
      info.dataset.group='pending';info.append(button(settings.enabled?'暂停观察':'开启本地观察',async()=>{await request('/settings','PUT',{enabled:!settings.enabled});await mountSkills(panel);}));grid.append(info);
      for(const pattern of patterns.patterns){
        const node=card(pattern.label,pattern.count+' 个去重示例 · 效果仍需审阅');node.dataset.group='pending';
        node.append(button('选取示例',()=>chooseSources(panel,pattern),true),button('不再建议',async()=>{await request('/patterns/'+pattern.id+'/dismiss','POST',{revision:pattern.revision});await mountSkills(panel);}));grid.append(node);
      }
      for(const item of candidates.candidates){
        const published=item.state==='published',node=card(item.title,
          item.scope+' · '+(published?(item.publication.mode==='trial'?'试用中 · 未充分验证':'已发布 · 有限文本验证'):labels[item.evaluation]||item.state));
        node.dataset.group=published?'personal':'pending';
        if(['rejected','revoked'].includes(item.state)){node.querySelector('p').textContent=item.state==='rejected'?'已拒绝':'来源已撤销，方法暂停';node.dataset.group='personal';}
        node.append(button(published?'查看方法':'审阅草稿',()=>review(item.id,panel),!published));
        if(published){
          node.append(button('改进此方法',async()=>{const next=await request('/candidates','POST',{title:item.title,scope:item.scope,sources:item.sources,baseline_id:item.published_skill_id});await request('/candidates/'+next.id,'PATCH',{revision:next.revision,body:item.body});await review(next.id,panel);}));
          node.append(button('暂停并撤销',async()=>{if(await dialogForm({title:'撤销这项个人方法？',description:'停止后续加载；不会删除原始聊天或修改普通技能。',submit:'撤销方法',danger:true})){await request('/skills/'+item.published_skill_id,'DELETE');await mountSkills(panel);}}));
          node.append(button('回退到此版本',async()=>{const skills=await json('/skills?limit=100');const current=(skills.skills||[]).find(s=>s.name===item.name);if(!current)throw new Error('当前版本不在此页，请先刷新技能列表。');if(await dialogForm({title:'回退个人方法',description:'保留当前开关，恢复这个仍有效的已批准版本。',submit:'回退'})){await request('/skills/'+item.published_skill_id+'/rollback?expected_active='+encodeURIComponent(current.id||current.skill_id),'POST');await mountSkills(panel);}}));
        }grid.append(node);
      }
      for(const job of jobs.jobs){
        const states={queued:'排队中',running:'正在整理或验证',completed:'完成',failed:'失败',cancelled:'已停止',cancel_requested:'正在停止',interrupted:'上次退出时中断'};
        const node=card('学习作业 · '+(states[job.state]||job.state),(job.message||'')+' · 已知费用 ¥'+Number(job.spent||0).toFixed(4)+(job.reserved?' · 另有在途/未知预留 ¥'+job.reserved.toFixed(4):''));node.dataset.group='pending';
        if(['queued','running','cancel_requested'].includes(job.state))node.append(button('停止',async()=>{await request('/jobs/'+job.id+'/cancel','POST');await mountSkills(panel);}));
        if(job.reserved&&!job.risk_acknowledged)node.append(button('核对未知费用',async()=>{if(await dialogForm({title:'确认已核对账单与剩余额度？',description:'未知预留仍保留在记录中。确认后可用你重新明确的预算启动新作业，旧额度不会恢复。',submit:'已核对'})){await request('/jobs/'+job.id+'/acknowledge-risk?confirmed=true','POST');await mountSkills(panel);}}));grid.append(node);
      }
      panel.querySelector('.panel-sub')?.after(section);
      function applyFilter(){tabs.querySelectorAll('button').forEach(tab=>tab.classList.toggle('active',tab.dataset.filter===filter));grid.querySelectorAll('[data-group]').forEach(node=>node.hidden=filter!=='all'&&node.dataset.group!==filter);const ordinary=panel.querySelector('.skill-grid');if(ordinary)ordinary.hidden=filter!=='all';}
      applyFilter();
      if(jobs.jobs.some(job=>['queued','running','cancel_requested'].includes(job.state))){setTimeout(()=>{if(section.isConnected&&!document.querySelector('.learning-review'))mountSkills(panel);},2000);}
    }catch(error){if(token===serial&&panel.dataset.view==='skills'){const notice=card('个人方法暂不可用',error.message);notice.classList.add('learning-section');panel.prepend(notice);}}
  }
  async function newCandidate(panel,sources=[],scope='global',title='') {
    const result=await dialogForm({title:'整理一项自己的方法',description:'先创建草稿，不调用模型。将范围改为 global 即明确允许这套方法跨项目使用，请先移除具体项目细节。',submit:'创建草稿',fields:[
      {name:'title',label:'方法名称',required:true,maxLength:80,value:title,placeholder:'例如：我的论文阅读方法'},
      {name:'scope',label:'适用范围',required:true,maxLength:120,value:scope}],
      onSubmit:values=>request('/candidates','POST',{...values,sources,global_confirmed:values.scope==='global'&&scope!=='global'})});
    if(result){await mountSkills(panel);await review(result.id,panel);}
  }
  async function chooseSources(panel,pattern){
    const data=await json(base+'/patterns/'+pattern.id+'/sources');
    const dialog=document.createElement('dialog');dialog.className='nm-dialog learning-review';
    const heading=document.createElement('h2');heading.textContent='选取真正属于这套方法的示例';dialog.append(heading);
    const intro=document.createElement('p');intro.textContent='下列为受限长度预览，仅勾选你允许用于整理方法的内容。相同主题不一定是同一种做法。';dialog.append(intro);
    const selected=[];
    for(const source of data.sources){const details=document.createElement('details'),summary=document.createElement('summary'),check=document.createElement('input'),pre=document.createElement('pre');check.type='checkbox';check.checked=true;selected.push([check,source.id]);summary.append(check,document.createTextNode(' '+new Date(source.created*1000).toLocaleString()+' · '+source.outcome));pre.textContent=source.preview.join('\n\n');details.append(summary,pre);dialog.append(details);}
    const actions=document.createElement('div');actions.className='dialog-actions';actions.append(button('取消',()=>dialog.close()),button('使用勾选示例',async()=>{const ids=selected.filter(([node])=>node.checked).map(([,id])=>id);if(!ids.length)throw new Error('请至少选择一个示例。');dialog.close();await newCandidate(panel,ids,pattern.scope,pattern.label);},true));dialog.append(actions);dialog.onclose=()=>dialog.remove();document.body.append(dialog);dialog.showModal();
  }
  async function review(id,panel){
    let item=await json(base+'/candidates/'+id);
    const dialog=document.createElement('dialog');dialog.className='nm-dialog learning-review';
    dialog.innerHTML='<div class="learning-heading"><div><span class="learning-eyebrow">PERSONAL METHOD</span><h2></h2></div></div><div class="learning-review-grid"><label class="learning-editor"><span>技能方法 · Markdown</span><textarea spellcheck="false" aria-label="个人技能正文"></textarea></label><aside><p class="learning-state" role="status"></p><details><summary>选定来源 · 有界预览</summary><pre class="learning-sources"></pre></details><details><summary>验证记录与限制</summary><pre class="learning-evidence"></pre></details><p>只对审批过的版本生效；编辑后原验证与审批失效。</p></aside></div><div class="learning-actions"></div>';
    dialog.querySelector('h2').textContent=item.title;
    const editor=dialog.querySelector('textarea'),state=dialog.querySelector('.learning-state'),actions=dialog.querySelector('.learning-actions');editor.value=item.body;editor.readOnly=!['draft','review_ready'].includes(item.state);
    dialog.querySelector('.learning-sources').textContent=(item.source_preview||[]).join('\n\n')||'手工方法，未选取聊天正文。';
    const paint=()=>{state.textContent=(labels[item.evaluation]||item.state)+' · '+item.scope+(item.checks?.length?'\n'+item.checks.join('；'):'');dialog.querySelector('.learning-evidence').textContent=item.behavior?[item.behavior.limitations,...item.behavior.cases.map((c,i)=>'样例 '+(i+1)+'：原方法'+(c.baseline_pass?'通过':'未通过')+'，候选方法'+(c.candidate_pass?'通过':'未通过'))].join('\n'):'尚未进行真实模型行为检查。静态检查不代表任务效果已验证。';};paint();
    async function save(){if(editor.readOnly||editor.value===item.body)return;item=await request('/candidates/'+id,'PATCH',{revision:item.revision,body:editor.value});paint();}
    async function close(){if(!editor.readOnly&&editor.value!==item.body&&!await dialogForm({title:'关闭未保存的草稿？',description:'你也可以取消后先保存。',submit:'放弃本次修改'}))return;dialog.close();}
    actions.append(button('关闭',close));
    if(!editor.readOnly){
      actions.append(button('保存草稿',save),button('用模型整理',async()=>{await save();if(await startJob(item,'generate'))await close();}),button('验证文本行为',async()=>{await save();if(await startJob(item,'evaluate'))await close();}));
      actions.append(button('启用方法',async()=>{await save();const mode=item.evaluation==='behavior_pass'?'verified':'trial';if(!await dialogForm({title:mode==='trial'?'启用尚未充分验证的方法？':'启用这份已审阅方法？',description:mode==='trial'?'仅通过静态检查，将明确标记为试用。后续可随时关闭或回退。':'仅验证过列出的文本约束，请核对适用范围。',submit:mode==='trial'?'确认试用':'确认启用'}))return;await request('/candidates/'+id+'/publish','POST',{revision:item.revision,hash:item.hash,expected_base_id:item.baseline_id,mode,enable:true,confirmed:true});editor.readOnly=true;dialog.close();},true));
      actions.append(button('拒绝候选',async()=>{if(await dialogForm({title:'拒绝这份候选？',submit:'拒绝',danger:true})){await request('/candidates/'+id+'/reject','POST',{revision:item.revision});editor.readOnly=true;dialog.close();}}));
    }
    dialog.oncancel=event=>{event.preventDefault();close();};dialog.onclose=()=>{dialog.remove();if(panel?.isConnected)mountSkills(panel);};document.body.append(dialog);dialog.showModal();editor.focus();
  }
  async function startJob(item,kind){
    const settings=await json(base+'/settings');
    const fields=[{name:'provider',label:'供应商类型',type:'select',value:settings.provider,options:{other:'OpenAI 兼容',openai:'OpenAI',aliyun:'阿里云',dashscope:'DashScope','z.ai':'智谱',tencent:'腾讯'}},{name:'model',label:'模型名称（使用已有供应商凭据）',required:true,maxLength:100,value:settings.model},
      {name:'budget_yuan',label:'本批次金额上限（人民币）',type:'number',required:true,min:.000001,step:'any'},
      {name:'input_price',label:'输入单价（人民币 / 百万 token）',type:'number',required:true,min:.000001,step:'any'},
      {name:'output_price',label:'输出单价（人民币 / 百万 token）',type:'number',required:true,min:.000001,step:'any'}];
    if(kind==='evaluate')for(let i=1;i<=2;i++)fields.push({name:'input'+i,label:'独立样例 '+i+' 的输入',type:'textarea',required:true,maxLength:4000},{name:'required'+i,label:'必须包含的文本（每行一项）',type:'textarea',required:true,maxLength:1000},{name:'forbidden'+i,label:'不得出现的文本（可选，每行一项）',type:'textarea',maxLength:1000});
    const key=crypto.randomUUID();
    return dialogForm({title:kind==='generate'?'确认用模型整理方法':'确认隔离文本检查',description:(kind==='generate'?'最多一次生成调用。':'两个样例各比较基线与候选，共最多四次模型调用；不会执行工具。')+' 请输入已核对的模型单价。额度不足将停止，费用未知时保留预留。不会自动启用技能。',fields,submit:'确认费用并开始',onSubmit:values=>{
      const budget={provider:values.provider,model:values.model,budget_yuan:Number(values.budget_yuan),input_price:Number(values.input_price),output_price:Number(values.output_price)};
      const lines=text=>String(text||'').split('\n').map(t=>t.trim()).filter(Boolean);
      const cases=kind==='evaluate'?[1,2].map(i=>({input:values['input'+i],required:lines(values['required'+i]),forbidden:lines(values['forbidden'+i])})):[];
      return request('/candidates/'+item.id+'/'+kind,'POST',{revision:item.revision,idempotency_key:key,budget,cases});
    }});
  }
  async function mountSettings(panel){
    const host=card('从日常任务积累自己的方法','本地发现重复模式；候选由你审阅，模型调用另行明确预算。私密或不记录对话不参与学习。');host.classList.add('personal-card','learning-settings');
    panel.querySelector('.personal-form')?.append(host);
    try{const settings=await json(base+'/settings');if(!host.isConnected)return;const actions=document.createElement('div');actions.className='personal-actions';actions.append(button(settings.enabled?'暂停本地观察':'开启本地观察',async()=>{await request('/settings','PUT',{enabled:!settings.enabled});host.remove();await mountSettings(panel);},true),button('管理个人方法',()=>document.querySelector('[data-nav="skills"]')?.click()),button('清理观察与派生方法',async()=>{if(await dialogForm({title:'清理学习观察？',description:'清理观察并暂停由这些来源派生的方法；原始聊天和普通技能保留。',submit:'清理',danger:true})){await request('/observations?confirmed=true','DELETE');}}));host.append(actions);}catch(error){host.append(document.createTextNode(error.message));}
  }
  async function usage(thread,host,since){
    if(!host||document.getElementById('memory-mode')?.value!=='normal')return;
    await new Promise(resolve=>setTimeout(resolve,250));
    try{const data=await json(base+'/usage?thread_id='+encodeURIComponent(thread));if(!host.isConnected||!data.skills.length||data.created*1000<since)return;host.querySelector('.learning-usage')?.remove();const details=document.createElement('details');details.className='learning-usage';const summary=document.createElement('summary');summary.textContent='本轮使用的个人方法';details.append(summary);for(const skill of data.skills){const row=document.createElement('div');row.textContent=skill.title+' ';for(const [value,label] of [['helpful','符合要求'],['problem','有问题']])row.append(button(label,async()=>{const updated=await request('/feedback','POST',{run_id:data.run_id,skill_id:skill.id,value,revision:data.revision});data.revision=updated.revision;row.dataset.feedback=value;}));details.append(row);}host.append(details);}catch(_){/* Optional feedback must not change the completed chat state. */}
  }
  window.NovaMindLearning={mountSkills,mountSettings,usage};
})();
