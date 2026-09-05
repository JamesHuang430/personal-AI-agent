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
