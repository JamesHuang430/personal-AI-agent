import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
const html = await readFile(new URL('../../assistant_app/web/user/index.html', import.meta.url), 'utf8');
const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'outside-only' });
const {window} = dom;
let status = 'awaiting_confirmation';
const confirmations = [];
const job = () => ({id:'video-1',status,prompt:'review this exact prompt',seconds:'4',size:'1280x720',resolution:'768P',draft_hash:'bound-parameters',download_url:'/api/v1/videos/video-1/download'});
window.fetch = async (url, options = {}) => {
  if(url.endsWith('/auth/session')) return {status:401,ok:false,json:async()=>({detail:'login'})};
  if(url.endsWith('/auth/captcha')) return {status:200,ok:true,json:async()=>({captcha_id:'dummy',question:'1+1'})};
  if(url.endsWith('/videos/video-1/confirm')) {
    confirmations.push(JSON.parse(options.body)); status='queued';
  }
  let body = url.includes('/videos/video-1') ? job() : [];
  if(url.endsWith('/chat/conversations/conversation-1')) body = {messages:[{
    role:'assistant',content:'saved answer',model:'test',artifacts:{
      web_sources:[{title:'Saved source',url:'https://example.test/source'}],
      files:[{filename:'saved.txt',size_bytes:8,download_url:'/api/v1/files/file-1/download'}],
      video_jobs:[job()],
    },
  }]};
  return {status:200,ok:true,json:async()=>body};
};
window.eval(await readFile(new URL('../../assistant_app/web/user/app.js', import.meta.url), 'utf8'));
await new Promise(resolve=>setImmediate(resolve));
const message=window.addMessage('assistant','draft');
window.renderArtifacts(message,{video_jobs:[job()]});
let card=message.querySelector('.artifact-card');
await window.pollVideoJob('video-1',card);
assert.match(card.textContent,/review this exact prompt/);
assert.match(card.textContent,/4 秒/);
card.querySelector('button').click();
await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(confirmations,[{draft_hash:'bound-parameters'}]);
status='completed';
await window.pollVideoJob('video-1',card);
const download=card.querySelector('a');
assert.equal(download.getAttribute('href'),'/api/v1/videos/video-1/download');
let prevented;
download.addEventListener('click',e=>{prevented=e.defaultPrevented;e.preventDefault();});
download.click();
assert.equal(prevented,false);
await window.openConversation('conversation-1');
assert.match(window.document.querySelector('#messages').textContent,/Saved source/);
assert.match(window.document.querySelector('#messages').textContent,/saved.txt/);
window.close();
console.log('UI smoke passed: parameter confirmation, usable download links, history artifact restoration');
