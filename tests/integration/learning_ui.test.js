'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture(candidate, handle) {
  class Node {
    constructor(tag){this.tag=tag;this.children=[];this.nodes=new Map();this.isConnected=true;this.dataset={};this.classList={add(){}};}
    append(...nodes){this.children.push(...nodes);}
    querySelector(selector){if(!this.nodes.has(selector))this.nodes.set(selector,new Node(selector));return this.nodes.get(selector);}
    showModal(){this.open=true;}
    focus(){}
    close(){this.onclose?.();}
    remove(){this.isConnected=false;}
  }
  const calls=[],notices=[],body=new Node('body');
  const context={window:{NovaMindControls:{notify:message=>notices.push(message)}},document:{body,createElement:tag=>new Node(tag)},
    NovaMindUI:{dialogForm:async()=>({}),json:async(url,options)=>{calls.push({url,options});return options?handle?.(url,options):candidate;}},
    crypto:{randomUUID:()=> 'test'},setTimeout,Date,Promise};
  const source=fs.readFileSync(path.join(__dirname,'../../novamind/webui/static/learning_ui.js'),'utf8')
    .replace('window.NovaMindLearning={mountSkills,mountSettings,usage};','window.NovaMindLearning={mountSkills,mountSettings,usage,review};');
  vm.runInNewContext(source,context);
  return {context,calls,notices,body};
}
const candidate={id:'candidate',title:'方法',scope:'global',revision:2,hash:'hash',body:'safe body',state:'review_ready',evaluation:'static_pass',sources:[]};

test('review shows model/source markup as text and retains edits on failed save',async()=>{
  const ui=fixture({...candidate,body:'<img src=x onerror=boom>',source_preview:['<script>boom</script>']},()=>{throw new Error('版本已更新');});
  await ui.context.window.NovaMindLearning.review('candidate',null);
  const dialog=ui.body.children[0],editor=dialog.querySelector('textarea');
  assert.equal(editor.value,'<img src=x onerror=boom>');
  assert.equal(dialog.querySelector('.learning-sources').textContent,'<script>boom</script>');
  assert.ok(!dialog.innerHTML.includes('<script>boom'));
  editor.value='unsaved draft';
  const save=dialog.querySelector('.learning-actions').children.find(node=>node.textContent==='保存草稿');
  await save.onclick();
  assert.equal(editor.value,'unsaved draft');
  assert.equal(ui.notices[0],'版本已更新');
  assert.equal(JSON.parse(ui.calls.find(c=>c.options).options.body).revision,2);
});

test('publishing unchanged verified content does not invalidate its evaluation by PATCH',async()=>{
  const ui=fixture({...candidate,evaluation:'behavior_pass'},()=>({skill_id:'skill'}));
  await ui.context.window.NovaMindLearning.review('candidate',null);
  const dialog=ui.body.children[0];
  await dialog.querySelector('.learning-actions').children.find(node=>node.textContent==='启用方法').onclick();
  const writes=ui.calls.filter(c=>c.options);
  assert.equal(writes.length,1);
  assert.equal(writes[0].options.method,'POST');
  assert.equal(JSON.parse(writes[0].options.body).mode,'verified');
  assert.equal(JSON.parse(writes[0].options.body).confirmed,true);
});
