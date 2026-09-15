import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';

const html = await readFile(new URL('../../assistant_app/web/user/index.html', import.meta.url), 'utf8');
const script = await readFile(new URL('../../assistant_app/web/user/app.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
async function setup() {
  const dom = new JSDOM(html, {url: 'http://localhost/', runScripts: 'outside-only'});
  const w = dom.window;
  w.structuredClone = structuredClone;
  w.HTMLMediaElement.prototype.pause = function () {};
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  const calls = [];
  const project = {
    id:'project-1',title:'雨夜',premise:'原始创意',status:'awaiting_storyboard',one_click:true,
    target_seconds:60,aspect_ratio:'9:16',resolution:'768P',visual_style:'胶片',progress:38,
    planned_shots:1,completed_shots:0,agents:[],shots:[],storyboard_hash:'a'.repeat(64),
    personalization:{preferences:{visual_style:'<script>bad()</script>'},memories:[{content:'配乐轻一点'}]},
    storyboard:[{title:'重逢',positive_prompt:'雨中相遇',speech_text:'好久不见'}],
  };
  w.fetch = async (url, options={}) => {
    calls.push({url, options});
    if(url.endsWith('/auth/session')) return {status:401,ok:false,json:async()=>({detail:'login'})};
    const body = url.endsWith('/director/preferences') ? {visual_style:'复古胶片',use_memory:false}
      : url.includes('/director/projects/') ? project : [];
    return {status:200,ok:true,json:async()=>body};
  };
  w.eval(script); await tick();
  return {w, dom, calls, project};
}

test('studio is the default, preferences are editable and legacy upsells are removed', async () => {
  const {w,dom,calls}=await setup();
  try {
    assert.equal(w.document.querySelector('#studio-workspace').classList.contains('hidden'),false);
    assert.equal(w.document.querySelector('#points-btn'),null);
    w.document.querySelector('#creative-preferences-btn').click(); await tick();
    const form=w.document.querySelector('#creative-preferences-form');
    assert.equal(form.elements.visual_style.value,'复古胶片');
    assert.equal(form.elements.use_memory.checked,false);
    form.elements.visual_style.value='自然光';
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();
    const saved=calls.find(call=>call.options.method==='PUT');
    assert.equal(JSON.parse(saved.options.body).visual_style,'自然光');
  } finally {dom.window.close();}
});

test('director sound panel submits explicit settings and audition only after click', async () => {
  const {w,dom,calls,project}=await setup();
  try {
    project.production_mode='whiteboard';
    const fetch=w.fetch;
    w.fetch=async (url, options={}) => {
      if (url.endsWith('/director/audio-options')) return {ok:true,status:200,json:async()=>({voices:[
        {id:'edge:zh-CN-XiaoxiaoNeural',mode:'edge',name:'晓晓'},
        {id:'fixture-minimax',mode:'minimax',name:'MiniMax'}]})};
      if (url.endsWith('/sound')) {
        const sent=JSON.parse(options.body); project.postproduction=sent.settings;
        project.storyboard_hash='b'.repeat(64); calls.push({url,options});
        return {ok:true,status:200,json:async()=>project};
      }
      if (url.includes('/auditions/')) {
        calls.push({url,options}); return {ok:true,status:202,json:async()=>({id:'speech-1',
          status:'completed',voice_id:'edge:zh-CN-XiaoxiaoNeural',speed:1.1,duration_ms:2400,
          subtitle_timing:'edge',download_url:'/api/v1/speech/speech-1/download'})};
      }
      return fetch(url,options);
    };
    await w.openDirectorSound(project);
    const dialog=w.document.querySelector('#director-sound-editor'), form=dialog.querySelector('form');
    const preview=dialog.querySelector('[data-audition-start]');
    assert.equal(preview.disabled,true);
    assert.equal(calls.some(c=>c.options.method==='POST'),false);
    form.elements.voice.value='0'; form.elements.voice.dispatchEvent(new w.Event('change'));
    form.elements.speed.value='1.1'; form.elements.style.value='panel';
    assert.equal(preview.disabled,false); preview.click(); await tick(); await tick();
    const saved=calls.find(c=>c.url.endsWith('/sound'));
    assert.equal(JSON.parse(saved.options.body).settings.voice_mode,'edge');
    assert.equal(JSON.parse(saved.options.body).settings.subtitle_style,'panel');
    const audition=calls.find(c=>c.url.includes('/auditions/'));
    assert.equal(JSON.parse(audition.options.body).storyboard_hash,'b'.repeat(64));
    assert.match(dialog.querySelector('[data-audition-status]').textContent,/渠道时间戳/);
    assert.equal(dialog.querySelector('[data-audition-audio]').hidden,false);
    project.current_stage='whiteboard_annotation_review';
    await w.openDirectorSound(project);
    assert.equal(w.document.querySelector('[data-sound-save]').disabled,true);
  } finally {dom.window.close();}
});

test('new projects default to whiteboard and old projects preserve their video mode', async () => {
  const {w,dom,calls,project}=await setup();
  try {
    w.showDirectorStart(true);
    assert.equal(w.document.querySelector('#director-production-mode').value,'whiteboard');
    assert.match(w.document.querySelector('#director-start-boundary').textContent,/不调用视频模型/);
    w.document.querySelector('#director-premise').value='介绍光合作用';
    w.document.querySelector('#director-start-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
    await tick();
    const request=calls.find(call=>call.url.endsWith('/director/projects') && call.options.method==='POST');
    assert.equal(JSON.parse(request.options.body).production_mode,'whiteboard');
    w.showDirectorStart(true,project,true);
    assert.equal(w.document.querySelector('#director-production-mode').value,'video');
    project.production_mode='whiteboard';
    w.renderDirectorProject(project);
    assert.equal(w.document.querySelectorAll('[data-whiteboard-upload]').length,1);
    assert.match(w.document.querySelector('#storyboard-review-content').textContent,/跳过付费生图/);
  } finally {dom.window.close();}
});

test('storyboard approval submits the displayed digest and escapes creative memory', async () => {
  const {w,dom,calls,project}=await setup();
  try {
    w.renderDirectorProject(project);
    assert.equal(w.document.querySelector('#project-personalization script'),null);
    assert.match(w.document.querySelector('#storyboard-review-content').textContent,/好久不见/);
    assert.equal(calls.some(call=>call.options.method==='POST'),false);
    w.document.querySelector('#approve-storyboard-btn').click(); await tick();
    const sent=calls.find(call=>call.url.endsWith('/approve-storyboard'));
    assert.equal(JSON.parse(sent.options.body).storyboard_hash,project.storyboard_hash);
  } finally {dom.window.close();}
});

test('whiteboard annotation editor saves source-bound fields and protects unsaved edits', async () => {
  const {w,dom,calls,project}=await setup();
  try {
    project.production_mode='whiteboard';project.current_stage='whiteboard_annotation_review';
    const annotation={version:2,canvas:{width:320,height:180},imageSha256:'a'.repeat(64),sceneDurationMs:3000,
      subtitleAlignment:'estimated',inkPath:'grid',cues:[{id:1,startMs:0,endMs:2000,text:'好久不见'}],
      elements:[{id:'sun',label:'太阳<script>bad()</script>',sequence:1,narrativeRole:'开场',cueIds:[1],
        region:{x:10,y:10,width:50,height:50},reveal:{startMs:100,durationMs:1000,protectedRegions:[]}}]};
    project.shots=[{id:'shot1',sequence:1,status:'pending',image_url:'/safe.png',whiteboard:{annotation,note:'核对'}}];
    w.renderDirectorProject(project);
    assert.match(w.document.querySelector('#approve-storyboard-btn').textContent,/确认全部分区/);
    w.document.querySelector('[data-whiteboard-editor]').click();
    const editor=w.document.querySelector('#whiteboard-editor');
    assert.equal(editor.querySelector('script'),null);
    assert.equal(editor.querySelectorAll('svg rect').length,1);
    // Polling must not destroy the user's working edit.
    w.renderDirectorProject({...project,progress:55});
    assert.equal(w.document.querySelector('#whiteboard-editor'),editor);
    editor.querySelector('[data-wb-save]').click();await tick();
    const request=calls.find(call=>call.url.endsWith('/shots/shot1/annotation'));
    const payload=JSON.parse(request.options.body);
    assert.equal(payload.annotation.imageSha256,'a'.repeat(64));
    assert.equal(payload.annotation.elements[0].cueIds[0],1);
    assert.equal(payload.storyboard_hash,project.storyboard_hash);
  } finally {dom.window.close();}
});

test('feedback requires explicit opt in and draft editing uses PATCH on the selected project', async () => {
  const {w,dom,calls,project}=await setup();
  try {
    project.status='completed'; w.renderDirectorProject(project);
    w.document.querySelector('#project-feedback-btn').click();
    const form=w.document.querySelector('#creative-feedback-form');
    assert.equal(form.elements.remember.checked,false);
    form.elements.notes.value='第三镜节奏太快';
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();
    const sent=calls.find(call=>call.url.endsWith('/feedback'));
    assert.equal(JSON.parse(sent.options.body).remember,false);
    project.status='awaiting_confirmation'; w.renderDirectorProject(project);
    w.document.querySelector('#project-settings').click();
    w.document.querySelector('#director-premise').value='改成清晨相遇';
    w.document.querySelector('#director-start-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();
    const edit=calls.find(call=>call.options.method==='PATCH');
    assert.equal(edit.url,'/api/v1/director/projects/project-1');
    assert.equal(JSON.parse(edit.options.body).premise,'改成清晨相遇');
  } finally {dom.window.close();}
});

test('empty opted-in preference shows an error inside the dialog before sending', async () => {
  const {w,dom,calls,project}=await setup();
  try {
    project.status='completed'; w.renderDirectorProject(project);
    w.document.querySelector('#project-feedback-btn').click();
    const form=w.document.querySelector('#creative-feedback-form');
    form.elements.rating.value='4';
    form.elements.remember.checked=true;
    form.elements.reusable_preference.value='  ';
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();
    assert.equal(calls.some(call=>call.url.endsWith('/feedback')),false);
    const error=form.querySelector('[role="alert"]');
    assert.equal(error.classList.contains('hidden'),false);
    assert.match(error.textContent,/取消/);
    assert.equal(w.document.activeElement,form.elements.reusable_preference);
    assert.equal(form.querySelector('button').disabled,false);
    form.elements.remember.checked=false;
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();
    const sent=calls.find(call=>call.url.endsWith('/feedback'));
    assert.equal(JSON.parse(sent.options.body).rating,4);
    assert.equal(JSON.parse(sent.options.body).remember,false);
    assert.equal(w.document.querySelector('#creative-feedback-dialog').hasAttribute('open'),false);
  } finally {dom.window.close();}
});

test('server feedback errors stay visible with input intact and retry available', async () => {
  const {w,dom,project}=await setup();
  try {
    project.status='completed'; w.renderDirectorProject(project);
    w.document.querySelector('#project-feedback-btn').click();
    const form=w.document.querySelector('#creative-feedback-form');
    form.elements.notes.value='字幕再小一点';
    w.fetch=async()=>({status:409,ok:false,json:async()=>({detail:'制作完成后才能验收作品'})});
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); await tick();
    assert.match(form.querySelector('[role="alert"]').textContent,/制作完成/);
    assert.equal(form.querySelector('[role="alert"]').classList.contains('hidden'),false);
    assert.equal(w.document.querySelector('#creative-feedback-dialog').hasAttribute('open'),true);
    assert.equal(form.elements.notes.value,'字幕再小一点');
    assert.equal(form.querySelector('button').disabled,false);
    assert.equal(form.querySelector('button').textContent,'保存验收反馈');
  } finally {dom.window.close();}
});

test('saving shows progress, prevents duplicates and submits the explicit preference', async () => {
  const {w,dom,project}=await setup();
  try {
    project.status='completed'; w.renderDirectorProject(project);
    w.document.querySelector('#project-feedback-btn').click();
    const form=w.document.querySelector('#creative-feedback-form');
    form.elements.remember.checked=true;
    form.elements.reusable_preference.value=' 配乐轻一点 ';
    let resolve, count=0, saved;
    w.fetch=async(url,options)=>{count++; saved=JSON.parse(options.body); return new Promise(r=>{resolve=r;});};
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
    assert.equal(count,1);
    assert.equal(form.querySelector('button').textContent,'保存中…');
    assert.equal(saved.reusable_preference,'配乐轻一点');
    assert.equal(saved.remember,true);
    resolve({status:200,ok:true,json:async()=>({...project,feedback:saved})}); await tick();
    assert.equal(w.document.querySelector('#creative-feedback-dialog').hasAttribute('open'),false);
    assert.equal(form.querySelector('button').disabled,false);
  } finally {dom.window.close();}
});
